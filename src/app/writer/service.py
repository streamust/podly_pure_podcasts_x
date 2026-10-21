import json
import logging
import os
import sys
import threading
import time

from app.ipc import get_queue, make_server_manager
from app.logger import setup_logger
from app.memory_pressure import release_memory_to_os
from app.writer.protocol import WriteCommand, WriteCommandType

from .executor import CommandExecutor

# Under pytest, skip attaching the file handler so tests that happen to import
# anything from this module don't end up appending writer logs into the real
# `src/instance/logs/app.log`. The console handler stays so test failures are
# still debuggable; pytest's own log capture works regardless.
if "pytest" in sys.modules:
    logger = logging.getLogger("writer")
    logger.setLevel(logging.INFO)
    logger.propagate = False
else:
    logger = setup_logger("writer", "src/instance/logs/app.log", level=logging.INFO)


def _idle_trim_interval_seconds() -> int:
    raw = os.environ.get("PODLY_WRITER_IDLE_TRIM_INTERVAL_SEC", "900")
    try:
        return int(raw)
    except ValueError:
        return 900


def _start_idle_trim_thread(activity_counter: list[int]) -> threading.Thread | None:
    """Periodically trim allocator arenas if any commands were processed
    since the last trim. Skips when truly idle so we don't churn syscalls
    while waiting on an empty queue.
    """
    interval = _idle_trim_interval_seconds()
    if interval <= 0:
        return None

    def _loop() -> None:
        last_count = 0
        while True:
            time.sleep(interval)
            current = activity_counter[0]
            if current == last_count:
                continue
            last_count = current
            try:
                release_memory_to_os("writer idle tick", logger)
            except Exception:
                logger.debug("writer idle trim failed", exc_info=True)

    thread = threading.Thread(target=_loop, name="writer-idle-trim", daemon=True)
    thread.start()
    return thread


MEMORY_TRIM_ACTIONS = {
    "insert_identifications",
    "insert_transcript_segments",
    "finish_transcription_replace",
    "finish_transcription_replace_from_artifact",
    "refresh_feed",
    "replace_audio_segments",
    "replace_identifications",
    "replace_transcription",
    "start_transcription_replace",
    "upsert_model_call",
}

# Timestamp-only writes allocate little; the periodic writer trim handles their
# accumulated allocator overhead without a full collection on every RSS read.
DEFERRED_MEMORY_TRIM_ACTIONS = {
    "dequeue_job",
    "touch_feed_access_token",
    "update_user_last_active",
}


def _writer_timing_enabled() -> bool:
    return os.environ.get("PODLY_WRITER_TIMING_LOG", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _writer_timing_payload(
    cmd: WriteCommand,
    *,
    dequeued_monotonic_ns: int,
    finished_monotonic_ns: int,
    success: bool,
) -> dict[str, object]:
    enqueued_ns = cmd.enqueued_monotonic_ns
    queue_ms = (
        max(0, dequeued_monotonic_ns - enqueued_ns) / 1_000_000
        if enqueued_ns is not None
        else None
    )
    total_ms = (
        max(0, finished_monotonic_ns - enqueued_ns) / 1_000_000
        if enqueued_ns is not None
        else None
    )
    return {
        "command_id": cmd.id,
        "operation": cmd.type.value,
        "action": _action_name(cmd),
        "queue_ms": queue_ms,
        "execution_ms": max(0, finished_monotonic_ns - dequeued_monotonic_ns)
        / 1_000_000,
        "total_ms": total_ms,
        "success": success,
    }


def _action_name(cmd: object) -> str | None:
    data = getattr(cmd, "data", None)
    if not isinstance(data, dict):
        return None
    action = data.get("action")
    return action if isinstance(action, str) else None


def _memory_trim_context_for_command(cmd: object) -> str | None:
    if getattr(cmd, "type", None) != WriteCommandType.ACTION:
        return None
    action = _action_name(cmd)
    if action in DEFERRED_MEMORY_TRIM_ACTIONS:
        return None
    if action not in MEMORY_TRIM_ACTIONS:
        return f"writer action {action or 'unknown'}"
    return f"writer large action {action}"


def _discard_processed_command_payload(cmd: WriteCommand) -> None:
    try:
        cmd.data = {}
        cmd.reply_queue = None
    except Exception:  # noqa: BLE001
        return


def run_writer_service() -> None:
    from app import create_writer_app

    logger.info("Starting Writer Service...")

    # 1. Start the IPC Server
    manager = make_server_manager()
    server = manager.get_server()

    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.daemon = True
    server_thread.start()
    logger.info("IPC Server started on port 50001")

    # 2. Get the queue
    queue = get_queue()

    # 3. Initialize App and Executor
    app = create_writer_app()
    executor = CommandExecutor(app)

    # Activity counter consumed by the idle-trim watchdog. Using a single-element
    # list avoids the `nonlocal`/`global` plumbing a bare int would need.
    activity_counter = [0]
    _start_idle_trim_thread(activity_counter)

    logger.info("Writer Loop starting...")

    # 4. Writer Loop
    while True:
        cmd = None
        result = None
        trim_context = None
        dequeued_monotonic_ns = None
        try:
            cmd = queue.get()
            dequeued_monotonic_ns = time.monotonic_ns()
            activity_counter[0] += 1
            trim_context = _memory_trim_context_for_command(cmd)

            # Check if this is a polling command (dequeue_job)
            is_polling = (
                getattr(cmd, "type", None) == WriteCommandType.ACTION
                and isinstance(getattr(cmd, "data", None), dict)
                and cmd.data.get("action") == "dequeue_job"
            )

            if not is_polling:
                logger.info(
                    "[WRITER] Received command: id=%s type=%s model=%s has_reply=%s",
                    getattr(cmd, "id", None),
                    getattr(cmd, "type", None),
                    getattr(cmd, "model", None),
                    bool(getattr(cmd, "reply_queue", None)),
                )

            result = executor.process_command(cmd)

            if _writer_timing_enabled():
                logger.info(
                    "[WRITER_TIMING] %s",
                    json.dumps(
                        _writer_timing_payload(
                            cmd,
                            dequeued_monotonic_ns=dequeued_monotonic_ns,
                            finished_monotonic_ns=time.monotonic_ns(),
                            success=bool(result and result.success),
                        ),
                        separators=(",", ":"),
                    ),
                )

            # Only log finished/reply if not polling or if polling actually did something
            if not is_polling or (result and result.data):
                logger.info(
                    "[WRITER] Finished command: id=%s success=%s error=%s",
                    getattr(result, "command_id", None),
                    getattr(result, "success", None),
                    getattr(result, "error", None),
                )

            if cmd.reply_queue:
                if not is_polling or (result and result.data):
                    logger.info(
                        "[WRITER] Sending reply for command id=%s",
                        getattr(cmd, "id", None),
                    )
                cmd.reply_queue.put(result)

        except Exception as e:
            logger.error("Error in writer loop: %s", e, exc_info=True)
            time.sleep(1)
        finally:
            if cmd is not None:
                _discard_processed_command_payload(cmd)
                cmd = None
                result = None
            if trim_context is not None:
                release_memory_to_os(trim_context, logger)
