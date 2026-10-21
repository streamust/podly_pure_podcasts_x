from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock

import pytest

from app.writer.protocol import WriteCommand, WriteCommandType
from tests.test_writer_differential_parity import (
    RustWriterParityServer,
    execute_python_parity_command,
    writer_database_projection,
)
from tests.writer_parity_fixtures import WriterParityPair
from tests.writer_parity_jobs import (
    _normalize_time_columns,
    build_writer_job_case,
    writer_job_records,
)


def test_dequeue_job_concurrent_polls_never_claim_the_same_or_multiple_jobs(
    writer_parity_pair: WriterParityPair, tmp_path: Path
) -> None:
    pair = writer_parity_pair
    operation, base_command = build_writer_job_case("action_dequeue_job_claim", pair)
    python_before = writer_database_projection(
        pair.python.db_path, pair.python.instance_dir
    )
    rust_before = writer_database_projection(pair.rust.db_path, pair.rust.instance_dir)

    rust = RustWriterParityServer(pair.rust.db_path, tmp_path)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            rust_outcomes = list(
                pool.map(lambda _index: rust.execute_outcome(operation), range(8))
            )
    finally:
        rust.close()

    python_writer_lock = Lock()

    def python_poll(index: int):
        command = WriteCommand(
            f"{base_command.id}-{index}",
            WriteCommandType.ACTION,
            None,
            json.loads(json.dumps(base_command.data)),
        )
        # The production Python writer owns one serial execution loop. Model
        # that ownership while the eight callers race to submit commands.
        with python_writer_lock:
            success, result, error = execute_python_parity_command(pair.python, command)
        assert success, error
        return result

    with ThreadPoolExecutor(max_workers=8) as pool:
        python_results = list(pool.map(python_poll, range(8)))

    rust_results = [outcome["result"] for outcome in rust_outcomes]
    assert sum(result is not None for result in rust_results) == 1
    assert sum(result is not None for result in python_results) == 1
    expected_job = "00000000-0000-0000-0000-000000000451"
    assert next(result for result in rust_results if result is not None) == {
        "job_id": expected_job,
        "post_guid": pair.manifest.post_guids[0],
    }
    assert next(result for result in python_results if result is not None) == {
        "job_id": expected_job,
        "post_guid": pair.manifest.post_guids[0],
    }

    python_after = writer_database_projection(
        pair.python.db_path, pair.python.instance_dir
    )
    rust_after = writer_database_projection(pair.rust.db_path, pair.rust.instance_dir)
    python_after["processing_job"] = _normalize_time_columns(
        pair.python.db_path,
        "processing_job",
        python_after["processing_job"],
        python_before["processing_job"],
    )
    rust_after["processing_job"] = _normalize_time_columns(
        pair.rust.db_path,
        "processing_job",
        rust_after["processing_job"],
        rust_before["processing_job"],
    )
    assert rust_after == python_after
    jobs = writer_job_records(pair.rust.db_path, rust_after["processing_job"])
    assert jobs[expected_job]["status"] == "running"
    assert jobs["00000000-0000-0000-0000-000000000452"]["status"] == "pending"


@pytest.mark.parametrize(
    ("case_name", "sequence", "expected_status", "expected_error"),
    [
        (
            "cancel_then_late_completion",
            [
                ("mark_cancelled", {"reason": "serial cancellation"}),
                (
                    "update_job_status",
                    {
                        "status": "completed",
                        "step": 4,
                        "step_name": "Completed late",
                        "progress": 100.0,
                    },
                ),
            ],
            "cancelled",
            "serial cancellation",
        ),
        (
            "completion_then_cancellation",
            [
                (
                    "update_job_status",
                    {
                        "status": "completed",
                        "step": 4,
                        "step_name": "Completed",
                        "progress": 100.0,
                    },
                ),
                ("mark_cancelled", {"reason": "cancel after completion"}),
            ],
            "cancelled",
            "cancel after completion",
        ),
        (
            "reassign_repeat_is_noop",
            [("reassign_pending_jobs", {"run_id": "jobs-manager-singleton"})] * 2,
            "pending",
            None,
        ),
    ],
    ids=["cancel-then-complete", "complete-then-cancel", "reassign-repeat"],
)
def test_job_actions_preserve_serial_order_and_repeat_semantics(
    case_name: str,
    sequence: list[tuple[str, dict]],
    expected_status: str,
    expected_error: str | None,
    writer_parity_pair: WriterParityPair,
    tmp_path: Path,
) -> None:
    pair = writer_parity_pair
    base_case = (
        "action_reassign_pending_jobs"
        if case_name == "reassign_repeat_is_noop"
        else "action_mark_cancelled"
    )
    _, seed_command = build_writer_job_case(base_case, pair)
    job_id = (
        "00000000-0000-0000-0000-000000000458"
        if case_name == "reassign_repeat_is_noop"
        else pair.manifest.job_ids[1]
    )
    python_before = writer_database_projection(
        pair.python.db_path, pair.python.instance_dir
    )
    rust_before = writer_database_projection(pair.rust.db_path, pair.rust.instance_dir)

    rust = RustWriterParityServer(pair.rust.db_path, tmp_path)
    try:
        rust_results = []
        for action, params in sequence:
            if action in {"mark_cancelled", "update_job_status"}:
                action_params = {"job_id": job_id, **params}
            else:
                action_params = params
            operation = {
                "operation": "action",
                "action": action,
                "params": action_params,
            }
            rust_results.append(rust.execute_outcome(operation))
    finally:
        rust.close()

    python_results = []
    for index, (action, params) in enumerate(sequence):
        if action in {"mark_cancelled", "update_job_status"}:
            action_params = {"job_id": job_id, **params}
        else:
            action_params = params
        command = WriteCommand(
            f"{seed_command.id}-{case_name}-{index}",
            WriteCommandType.ACTION,
            None,
            {"action": action, "params": action_params},
        )
        success, result, error = execute_python_parity_command(pair.python, command)
        assert success, error
        python_results.append(result)

    assert [outcome["result"] for outcome in rust_results] == python_results
    assert all(outcome["success"] for outcome in rust_results)
    python_after = writer_database_projection(
        pair.python.db_path, pair.python.instance_dir
    )
    rust_after = writer_database_projection(pair.rust.db_path, pair.rust.instance_dir)
    for backend, after in (("python", python_after), ("rust", rust_after)):
        before = python_before if backend == "python" else rust_before
        db_path = getattr(pair, backend).db_path
        for table in ("processing_job", "jobs_manager_run"):
            after[table] = _normalize_time_columns(
                db_path, table, after[table], before[table]
            )
    assert rust_after == python_after

    jobs = writer_job_records(pair.rust.db_path, rust_after["processing_job"])
    assert jobs[job_id]["status"] == expected_status
    if expected_error is not None:
        assert jobs[job_id]["error_message"] == expected_error
    if case_name == "reassign_repeat_is_noop":
        assert [outcome["result"] for outcome in rust_results] == [1, 0]
