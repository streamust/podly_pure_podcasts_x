"""Reply-routing behaviour of WriterClient._await_reply.

The writer answers one command at a time on a thread-local reply queue. When a
caller gives up on a slow command, the writer still delivers that command's
reply afterwards. Those late replies must be discarded rather than handed to
whoever is waiting next -- otherwise one slow command desynchronises every
subsequent write on the thread.
"""

from __future__ import annotations

import queue
import time
from typing import Any

import pytest

from app.writer.client import (
    DEFAULT_SUBMIT_TIMEOUT_SECONDS,
    WriterClient,
    _default_submit_timeout,
)
from app.writer.protocol import WriteCommand, WriteCommandType, WriteResult


def _cmd(cmd_id: str) -> WriteCommand:
    return WriteCommand(
        id=cmd_id, type=WriteCommandType.ACTION, model=None, data={"action": "noop"}
    )


def test_await_reply_returns_matching_reply() -> None:
    q: queue.Queue[Any] = queue.Queue()
    q.put(WriteResult("cmd-1", True, data={"ok": True}))

    result = WriterClient._await_reply(q, _cmd("cmd-1"), timeout=5)

    assert result.command_id == "cmd-1"
    assert result.data == {"ok": True}


def test_await_reply_discards_late_reply_for_abandoned_command() -> None:
    """A reply for a command we already gave up on must not be returned."""
    q: queue.Queue[Any] = queue.Queue()
    # The writer finally answered the command that timed out a moment ago...
    q.put(WriteResult("abandoned-cmd", True, data={"stale": True}))
    # ...and then answered ours.
    q.put(WriteResult("cmd-2", True, data={"fresh": True}))

    result = WriterClient._await_reply(q, _cmd("cmd-2"), timeout=5)

    assert result.command_id == "cmd-2"
    assert result.data == {"fresh": True}
    assert q.empty()


def test_await_reply_discards_several_late_replies() -> None:
    q: queue.Queue[Any] = queue.Queue()
    for stale_id in ("old-1", "old-2", "old-3"):
        q.put(WriteResult(stale_id, True, data={"id": stale_id}))
    q.put(WriteResult("cmd-3", True, data={"id": "cmd-3"}))

    result = WriterClient._await_reply(q, _cmd("cmd-3"), timeout=5)

    assert result.command_id == "cmd-3"


def test_await_reply_times_out_when_no_reply_arrives() -> None:
    q: queue.Queue[Any] = queue.Queue()

    with pytest.raises(TimeoutError, match="Writer service did not respond"):
        WriterClient._await_reply(q, _cmd("cmd-4"), timeout=1)


def test_await_reply_timeout_is_a_deadline_not_per_get() -> None:
    """Discarding stale replies must not extend the caller's total wait.

    The old code restarted the full timeout after a mismatch, so a stream of
    late replies could block a caller indefinitely.
    """
    q: queue.Queue[Any] = queue.Queue()
    for i in range(5):
        q.put(WriteResult(f"stale-{i}", True))

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        WriterClient._await_reply(q, _cmd("cmd-5"), timeout=1)
    elapsed = time.monotonic() - started

    # Five stale replies are drained instantly, then one 1s wait -- not 5s.
    assert elapsed < 3


def test_default_submit_timeout_uses_default_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PODLY_WRITER_TIMEOUT_SECONDS", raising=False)
    assert _default_submit_timeout() == DEFAULT_SUBMIT_TIMEOUT_SECONDS


def test_default_submit_timeout_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PODLY_WRITER_TIMEOUT_SECONDS", "90")
    assert _default_submit_timeout() == 90


def test_submit_stamps_command_before_enqueue() -> None:
    client = WriterClient()
    client.queue = queue.Queue()
    cmd = _cmd("timed-command")

    assert client.submit(cmd, wait=False) is None

    queued = client.queue.get_nowait()
    assert queued is cmd
    assert cmd.enqueued_monotonic_ns is not None
    assert cmd.enqueued_monotonic_ns <= time.monotonic_ns()


@pytest.mark.parametrize("bad", ["not-a-number", "0", "-5", ""])
def test_default_submit_timeout_rejects_bad_values(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    monkeypatch.setenv("PODLY_WRITER_TIMEOUT_SECONDS", bad)
    assert _default_submit_timeout() == DEFAULT_SUBMIT_TIMEOUT_SECONDS
