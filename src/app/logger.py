import fcntl
import json
import logging
import os
from collections.abc import Generator
from contextlib import contextmanager, suppress
from logging.handlers import RotatingFileHandler

from app.sanitize import redact_secrets

DEFAULT_LOG_MAX_BYTES = 50_000_000
DEFAULT_LOG_BACKUP_COUNT = 5


class MultiProcessRotatingFileHandler(RotatingFileHandler):
    """A ``RotatingFileHandler`` that is safe when several processes share a file.

    Podly writes ``app.log`` from more than one process: the writer service
    (``python3 -m app.writer``), the Flask app (``src/main.py``), and each
    per-job ``processing_worker`` subprocess. With the stock handler each of
    them rotates independently -- one process renames ``app.log`` to
    ``app.log.1`` while the others keep writing to the inode they already hold
    open. The result is that "the current log" silently becomes whichever
    backup a given process happens to be holding, and a reader tailing
    ``app.log`` sees only a fraction of the output.

    Two changes make that safe:

    - Rotation is serialised across processes with an ``flock``-ed sidecar
      lock file, so only one process rolls the numbered backups at a time.
    - Under that lock the handler re-checks whether the file it holds open is
      still the one at ``baseFilename``. If another process rotated first,
      this process simply drops its stale stream and reopens instead of
      rotating a second time.

    The staleness check runs before every emit. It costs two ``stat`` calls per
    record, which is small next to the JSON extras encoding and secret
    redaction this logger's formatter already does, and it is what keeps a
    process from writing a further ``maxBytes`` of records into a file that
    was rotated away -- the exact symptom that made ``app.log`` stop being the
    current log for hours at a time.

    Requires POSIX ``flock``, which is what podly runs on everywhere.
    """

    def __init__(
        self,
        filename: str,
        mode: str = "a",
        maxBytes: int = 0,
        backupCount: int = 0,
        encoding: str | None = None,
        delay: bool = False,
        errors: str | None = None,
    ) -> None:
        super().__init__(
            filename,
            mode=mode,
            maxBytes=maxBytes,
            backupCount=backupCount,
            encoding=encoding,
            delay=delay,
            errors=errors,
        )
        self._lock_path = f"{self.baseFilename}.lock"

    @contextmanager
    def _rotation_lock(self) -> Generator[None]:
        """Hold an exclusive inter-process lock for the duration of a rotation."""
        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _rotated_by_another_process(self) -> bool:
        """True if ``baseFilename`` no longer refers to the inode we hold open."""
        if self.stream is None:
            return False
        try:
            held = os.fstat(self.stream.fileno())
        except OSError, ValueError:
            return True
        try:
            current = os.stat(self.baseFilename)
        except OSError:
            # Another process has renamed the file and not yet recreated it.
            return True
        return (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino)

    def _drop_stream(self) -> None:
        """Close the held stream so ``FileHandler.emit`` reopens ``baseFilename``."""
        stream = self.stream
        self.stream = None  # type: ignore[assignment]
        if stream is not None:
            with suppress(Exception):
                stream.flush()
            with suppress(Exception):
                stream.close()

    def emit(self, record: logging.LogRecord) -> None:
        if self._rotated_by_another_process():
            self._drop_stream()
        super().emit(record)

    def doRollover(self) -> None:
        with self._rotation_lock():
            if self._rotated_by_another_process():
                # Someone else already rolled the files while we waited for the
                # lock. Re-point at the new current file rather than rotating
                # again, which would shift their fresh log into a backup.
                self._drop_stream()
                return
            super().doRollover()


class ExtraFormatter(logging.Formatter):
    """Formatter that appends structured extras to log lines.

    Any LogRecord attributes not in the standard set are captured into a JSON
    object and appended as ``extra={...}`` so contextual fields are visible in
    plain-text logs.
    """

    _standard_attrs = {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
        "asctime",
    }

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            k: v for k, v in record.__dict__.items() if k not in self._standard_attrs
        }
        if extras:
            try:
                extras_json = json.dumps(extras, ensure_ascii=True, default=str)
            except Exception:  # noqa: BLE001
                extras_json = str(extras)
            final_str = f"{base} | extra={extras_json}"
        else:
            final_str = base

        return redact_secrets(final_str)


def setup_logger(
    name: str, log_file: str, level: int = logging.DEBUG
) -> logging.Logger:
    """Create or return a configured logger.

    - Writes to the specified log_file
    - Emits to console exactly once (no duplicates)
    - Disables propagation to avoid duplicate root handling
    - Guards against adding duplicate handlers across repeated calls
    """
    file_formatter = ExtraFormatter("%(asctime)s %(levelname)s %(message)s")
    console_formatter = ExtraFormatter("%(levelname)s  [%(name)s] %(message)s")

    logger = logging.getLogger(name)
    logger.setLevel(level)
    # Prevent records from also bubbling up to root logger handlers (which can cause duplicates)
    logger.propagate = False

    # Ensure directory exists for log file
    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    # Add file handler if not already present for this file
    abs_log_file = os.path.abspath(log_file)
    has_file_handler = any(
        isinstance(h, logging.FileHandler)
        and getattr(h, "baseFilename", None) == abs_log_file
        for h in logger.handlers
    )
    if not has_file_handler:
        max_bytes = int(os.environ.get("PODLY_LOG_MAX_BYTES", DEFAULT_LOG_MAX_BYTES))
        backup_count = int(
            os.environ.get("PODLY_LOG_BACKUP_COUNT", DEFAULT_LOG_BACKUP_COUNT)
        )
        # delay=True: per-job processing_worker subprocesses import the logger
        # but may never write. Without delay, every spawn bumps the file mtime
        # and risks spurious rotation triggers across processes.
        # MultiProcessRotatingFileHandler: several processes write this same
        # file, so rotation has to be locked and stale streams reopened.
        file_handler = MultiProcessRotatingFileHandler(
            abs_log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=True,
        )
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)

    # Add a single console handler if not already present
    has_stream_handler = any(
        isinstance(h, logging.StreamHandler) for h in logger.handlers
    )
    if not has_stream_handler:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(console_formatter)
        logger.addHandler(stream_handler)

    return logger
