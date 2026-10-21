from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass
class FailureState:
    attempts: int
    blocked_until: datetime | None
    last_attempt: datetime


class FailureRateLimiter:
    """Simple in-memory exponential backoff tracker for authentication failures."""

    def __init__(
        self,
        *,
        storage: MutableMapping[str, FailureState] | None = None,
        max_backoff_seconds: int = 300,
        warm_up_attempts: int = 3,
        max_entries: int = 10_000,
    ) -> None:
        self._storage = storage if storage is not None else {}
        self._max_backoff_seconds = max_backoff_seconds
        self._warm_up_attempts = warm_up_attempts
        # Keys can come from client input (feed token ids), so storage is bounded.
        self._max_entries = max_entries
        self._last_prune: datetime | None = None

    def register_failure(self, key: str) -> int:
        now = datetime.now(UTC).replace(tzinfo=None)
        state = self._storage.get(key)

        if state is None:
            state = FailureState(attempts=1, blocked_until=None, last_attempt=now)
        else:
            state.attempts += 1
            state.last_attempt = now

        backoff_seconds = 0
        if state.attempts > self._warm_up_attempts:
            exponent = state.attempts - self._warm_up_attempts
            backoff_seconds = min(2**exponent, self._max_backoff_seconds)
            state.blocked_until = now + timedelta(seconds=backoff_seconds)
        else:
            state.blocked_until = None

        self._storage[key] = state
        self._prune(now)
        return backoff_seconds

    def register_success(self, key: str) -> None:
        if key in self._storage:
            del self._storage[key]

    def retry_after(self, key: str) -> int | None:
        state = self._storage.get(key)
        if state is None or state.blocked_until is None:
            return None

        now = datetime.now(UTC).replace(tzinfo=None)
        if state.blocked_until <= now:
            del self._storage[key]
            return None

        remaining = int((state.blocked_until - now).total_seconds())
        if remaining <= 0:
            del self._storage[key]
            return None

        return remaining

    def _prune(self, now: datetime) -> None:
        if self._last_prune is None or now - self._last_prune > timedelta(minutes=1):
            self._last_prune = now
            stale_keys = [
                key
                for key, state in self._storage.items()
                if now - state.last_attempt > timedelta(hours=1)
            ]
            for key in stale_keys:
                del self._storage[key]

        # Dict order is first-failure order, so this evicts the oldest keys.
        while len(self._storage) > self._max_entries:
            del self._storage[next(iter(self._storage))]
