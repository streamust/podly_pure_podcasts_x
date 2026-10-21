from __future__ import annotations

import time

import pytest

from app.writer import executor, service
from app.writer.protocol import WriteCommand, WriteCommandType


def test_memory_trim_context_detects_large_writer_actions() -> None:
    cmd = WriteCommand(
        id="cmd-1",
        type=WriteCommandType.ACTION,
        model=None,
        data={"action": "replace_transcription", "params": {"segments": []}},
    )

    assert service._memory_trim_context_for_command(cmd) == (
        "writer large action replace_transcription"
    )


def test_memory_trim_context_ignores_polling_actions() -> None:
    cmd = WriteCommand(
        id="cmd-1",
        type=WriteCommandType.ACTION,
        model=None,
        data={"action": "dequeue_job", "params": {}},
    )

    assert service._memory_trim_context_for_command(cmd) is None


@pytest.mark.parametrize(
    "action", ["touch_feed_access_token", "update_user_last_active"]
)
def test_memory_trim_context_defers_timestamp_actions(action: str) -> None:
    cmd = WriteCommand(
        id="cmd-1",
        type=WriteCommandType.ACTION,
        model=None,
        data={"action": action, "params": {}},
    )

    assert service._memory_trim_context_for_command(cmd) is None


def test_memory_trim_context_preserves_cleanup_for_unknown_actions() -> None:
    cmd = WriteCommand(
        id="cmd-1",
        type=WriteCommandType.ACTION,
        model=None,
        data={"action": "future_action", "params": {}},
    )

    assert (
        service._memory_trim_context_for_command(cmd) == "writer action future_action"
    )


def test_memory_trim_context_detects_other_writer_actions() -> None:
    cmd = WriteCommand(
        id="cmd-1",
        type=WriteCommandType.ACTION,
        model=None,
        data={"action": "mark_model_call_failed", "params": {}},
    )

    assert service._memory_trim_context_for_command(cmd) == (
        "writer action mark_model_call_failed"
    )


def test_writer_executor_detects_dequeue_job_polling_commands() -> None:
    cmd = WriteCommand(
        id="cmd-1",
        type=WriteCommandType.ACTION,
        model=None,
        data={"action": "dequeue_job", "params": {}},
    )

    assert executor._command_action_name(cmd) == "dequeue_job"
    assert executor._is_dequeue_job_poll(cmd) is True


def test_writer_executor_action_name_ignores_non_action_commands() -> None:
    cmd = WriteCommand(
        id="cmd-1",
        type=WriteCommandType.TRANSACTION,
        model=None,
        data={"action": "dequeue_job", "params": {}},
    )

    assert executor._command_action_name(cmd) is None
    assert executor._is_dequeue_job_poll(cmd) is False


def test_discard_processed_command_payload_releases_payload_refs() -> None:
    payload = {"action": "replace_transcription", "params": {"segments": ["large"]}}
    reply_queue = object()
    cmd = WriteCommand(
        id="cmd-1",
        type=WriteCommandType.ACTION,
        model=None,
        data=payload,
        reply_queue=reply_queue,
    )

    service._discard_processed_command_payload(cmd)

    assert cmd.data == {}
    assert cmd.reply_queue is None


def test_writer_timing_payload_reports_queue_execution_and_total() -> None:
    cmd = WriteCommand(
        id="timed-command",
        type=WriteCommandType.ACTION,
        model=None,
        data={"action": "update_user_last_active", "params": {"user_id": 1}},
        enqueued_monotonic_ns=1_000_000_000,
    )

    payload = service._writer_timing_payload(
        cmd,
        dequeued_monotonic_ns=1_003_000_000,
        finished_monotonic_ns=1_008_000_000,
        success=True,
    )

    assert payload == {
        "command_id": "timed-command",
        "operation": "action",
        "action": "update_user_last_active",
        "queue_ms": 3.0,
        "execution_ms": 5.0,
        "total_ms": 8.0,
        "success": True,
    }


def test_idle_trim_thread_skips_when_no_activity(monkeypatch) -> None:
    """If activity_counter never moves, the watchdog must not trim — otherwise
    a fully idle writer burns syscalls trimming an empty heap forever.
    """
    monkeypatch.setenv("PODLY_WRITER_IDLE_TRIM_INTERVAL_SEC", "1")
    calls: list[str] = []
    monkeypatch.setattr(
        service,
        "release_memory_to_os",
        lambda ctx, _log: calls.append(ctx),
    )

    counter = [0]
    thread = service._start_idle_trim_thread(counter)
    assert thread is not None
    time.sleep(2.5)
    assert calls == []


def test_idle_trim_thread_trims_after_activity(monkeypatch) -> None:
    monkeypatch.setenv("PODLY_WRITER_IDLE_TRIM_INTERVAL_SEC", "1")
    calls: list[str] = []
    monkeypatch.setattr(
        service,
        "release_memory_to_os",
        lambda ctx, _log: calls.append(ctx),
    )

    counter = [0]
    thread = service._start_idle_trim_thread(counter)
    assert thread is not None
    counter[0] += 1
    time.sleep(2.5)
    assert any("writer idle tick" in c for c in calls)


def test_idle_trim_thread_disabled_when_interval_non_positive(monkeypatch) -> None:
    monkeypatch.setenv("PODLY_WRITER_IDLE_TRIM_INTERVAL_SEC", "0")
    assert service._start_idle_trim_thread([0]) is None
