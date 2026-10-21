from __future__ import annotations

import ctypes
import gc
import logging
import os
import sys
import threading
import time
from functools import lru_cache
from typing import Any

from flask import g

logger = logging.getLogger("global_logger")
_TRIM_CONTEXTS_ATTR = "_podly_memory_trim_after_context"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@lru_cache(maxsize=1)
def _malloc_trim() -> Any | None:
    if not sys.platform.startswith("linux"):
        return None
    try:
        libc = ctypes.CDLL("libc.so.6")
        trim = libc.malloc_trim
        trim.argtypes = [ctypes.c_size_t]
        trim.restype = ctypes.c_int
        return trim
    except Exception:  # noqa: BLE001
        return None


# jemalloc's `arena.<i>.purge` mallctl key takes the arena index. The
# special value MALLCTL_ARENAS_ALL == 4096 targets every arena in one
# call, which is what we want for periodic / post-burst trimming.
_JEMALLOC_ARENAS_ALL = 4096


@lru_cache(maxsize=1)
def _jemalloc_mallctl() -> Any | None:
    """Resolve jemalloc's mallctl symbol when jemalloc is preloaded.

    Returns None when running on glibc — the only signal we have is whether
    the symbol resolves in the process-wide symbol table (RTLD_DEFAULT via
    ctypes.CDLL(None)). glibc has no `mallctl`, so resolving it implies
    jemalloc replaced the allocator.
    """
    if not sys.platform.startswith("linux"):
        return None
    try:
        proc = ctypes.CDLL(None)
    except OSError:
        return None
    try:
        mallctl = proc.mallctl
    except AttributeError:
        return None

    mallctl.argtypes = [
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_void_p,
        ctypes.c_size_t,
    ]
    mallctl.restype = ctypes.c_int

    # Sanity-probe via "epoch": a write-only u64 that refreshes jemalloc's
    # cached stats. rc==0 confirms jemalloc is the active allocator and our
    # mallctl pointer is callable. We use epoch (not "version") because
    # "version" returns a `const char **` and demands the caller pass a
    # `char **` output buffer; getting the buffer shape wrong silently
    # returns EINVAL and the symbol looks broken even when jemalloc is
    # fine. epoch only takes a write side, no output buffer needed.
    epoch = ctypes.c_uint64(1)
    try:
        rc = mallctl(b"epoch", None, None, ctypes.byref(epoch), ctypes.sizeof(epoch))
    except Exception:  # noqa: BLE001
        return None
    if rc != 0:
        return None
    return mallctl


def _jemalloc_purge_all_arenas(active_logger: logging.Logger) -> bool:
    mallctl = _jemalloc_mallctl()
    if mallctl is None:
        return False
    key = f"arena.{_JEMALLOC_ARENAS_ALL}.purge".encode("ascii")
    try:
        rc = mallctl(key, None, None, None, 0)
    except Exception as exc:
        active_logger.debug("jemalloc purge raised: %s", exc, exc_info=True)
        return False
    if rc != 0:
        active_logger.debug("jemalloc purge returned non-zero: %s", rc)
        return False
    return True


def release_memory_to_os(
    context: str,
    log: logging.Logger | None = None,
) -> None:
    """Ask Python and the allocator to return freed memory to the OS.

    Under glibc/ptmalloc we call malloc_trim(0). Under jemalloc (preloaded
    via /etc/ld.so.preload) malloc_trim is a no-op stub, so we call
    jemalloc's mallctl arena-purge instead. Both branches are best-effort;
    the goal is to nudge the allocator after a known burst of allocations.
    """
    if not _env_bool("PODLY_MEMORY_TRIM_ENABLED", default=True):
        return

    active_logger = log or logger
    collected = gc.collect()

    purged_jemalloc = _jemalloc_purge_all_arenas(active_logger)
    trimmed_glibc = False
    if not purged_jemalloc:
        # Only fall back to glibc malloc_trim if jemalloc isn't loaded —
        # otherwise calling it returns False and pollutes logs.
        trim = _malloc_trim()
        if trim is not None:
            try:
                trimmed_glibc = bool(trim(0))
            except Exception as exc:
                active_logger.debug(
                    "Memory trim failed after %s: %s", context, exc, exc_info=True
                )

    active_logger.debug(
        "Memory cleanup after %s: gc_collected=%s jemalloc_purge=%s malloc_trim=%s",
        context,
        collected,
        purged_jemalloc,
        trimmed_glibc,
    )


def request_memory_trim_after_context(context: str) -> None:
    """Request a trim after Flask tears down session/app-context state."""
    try:
        contexts = getattr(g, _TRIM_CONTEXTS_ATTR, None)
        if contexts is None:
            contexts = []
            setattr(g, _TRIM_CONTEXTS_ATTR, contexts)
        contexts.append(context)
    except RuntimeError:
        release_memory_to_os(context, logger)


def consume_memory_trim_contexts() -> list[str]:
    try:
        contexts = getattr(g, _TRIM_CONTEXTS_ATTR, None)
        if not contexts:
            return []
        setattr(g, _TRIM_CONTEXTS_ATTR, [])
        return list(contexts)
    except RuntimeError:
        return []


def collect_incremental(
    context: str,
    log: logging.Logger | None = None,
) -> None:
    """Run a low-pause incremental collection during long batch loops."""
    active_logger = log or logger
    collected = gc.collect(1)
    active_logger.debug(
        "Incremental memory cleanup after %s: gc_collected=%s",
        context,
        collected,
    )


class RequestBurstTrim:
    """Track in-flight web requests; claim one trim after each quiet burst.

    Bursts leave freed allocator pages and extra pooled SQLite connections
    (whose descriptors SQLite keeps open while any connection holds the WAL
    read lock) until the next scheduled trim, which can be many minutes away.
    """

    def __init__(self, quiet_seconds: float = 1.0) -> None:
        self.quiet_seconds = quiet_seconds
        self._lock = threading.Lock()
        self._active = 0
        self._pending = False
        self._last_finished = time.monotonic()

    def started(self) -> None:
        with self._lock:
            self._active += 1
            self._pending = True

    def finished(self) -> None:
        with self._lock:
            self._active -= 1
            self._last_finished = time.monotonic()

    def claim(self, now: float | None = None) -> bool:
        with self._lock:
            now = time.monotonic() if now is None else now
            if (
                self._active
                or not self._pending
                or now - self._last_finished < self.quiet_seconds
            ):
                return False
            self._pending = False
            return True
