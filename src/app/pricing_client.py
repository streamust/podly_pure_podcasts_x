"""Resolve LiteLLM prices without retaining its SDK imports in server processes."""

import json
import logging
import math
import os
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path

logger = logging.getLogger("global_logger")
MAX_CACHED_MODELS = 256
CACHE_TTL_SECONDS = 3600
RateKey = tuple[str, str | None]
Rates = tuple[float, float, float]
_cache: OrderedDict[RateKey, tuple[float, Rates]] = OrderedDict()
_lock = threading.Lock()


def helper_env() -> dict[str, str]:
    """Environment for `python -m app.<helper>` subprocesses of a server."""
    env = os.environ.copy()
    source_root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (source_root, env.get("PYTHONPATH")) if part
    )
    return env


def lookup_model_rates(models: list[RateKey]) -> dict[RateKey, Rates]:
    """Batch cache misses in one disposable helper; serialize concurrent misses.

    Errors return zero rates, matching the existing unavailable-LiteLLM behavior,
    but are not cached so subsequent requests can recover.
    """
    with _lock:
        now = time.monotonic()
        result: dict[RateKey, Rates] = {}
        missing = []
        for pair in dict.fromkeys(models):
            cached = _cache.get(pair)
            if cached is not None and now - cached[0] < CACHE_TTL_SECONDS:
                result[pair] = cached[1]
                _cache.move_to_end(pair)
            else:
                missing.append(pair)
        if not missing:
            return result

        try:
            completed = subprocess.run(
                [sys.executable, "-m", "app.pricing_worker"],
                input=json.dumps(missing),
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
                env=helper_env(),
            )
            payload = json.loads(completed.stdout)
            if not isinstance(payload, list) or len(payload) != len(missing):
                raise ValueError("pricing helper returned an invalid rate count")
            resolved: list[Rates] = []
            for entry in payload:
                if (
                    not isinstance(entry, list)
                    or len(entry) != 3
                    or any(
                        isinstance(rate, bool)
                        or not isinstance(rate, int | float)
                        or not math.isfinite(rate)
                        for rate in entry
                    )
                ):
                    raise ValueError("pricing helper returned invalid rates")
                resolved.append((float(entry[0]), float(entry[1]), float(entry[2])))
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            logger.warning("Pricing helper unavailable: %s", exc)
            result.update(dict.fromkeys(missing, (0.0, 0.0, 0.0)))
            return result

        for pair, rates in zip(missing, resolved, strict=True):
            result[pair] = rates
            _cache[pair] = (time.monotonic(), rates)
            _cache.move_to_end(pair)
            while len(_cache) > MAX_CACHED_MODELS:
                _cache.popitem(last=False)
        return result
