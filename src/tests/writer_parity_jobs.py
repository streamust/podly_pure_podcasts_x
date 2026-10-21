"""Executed differential cases for the Python and Rust job writer actions."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.writer.protocol import WriteCommand, WriteCommandType
from tests.writer_parity_fixtures import WriterParityPair

_JOBS = (
    ("dequeue_job", "dequeue_job_claim"),
    ("dequeue_job", "dequeue_job_empty_queue_noop"),
    ("cleanup_stale_jobs", "cleanup_stale_jobs"),
    ("cleanup_stale_jobs", "cleanup_stale_jobs_cutoff_sides"),
    ("clear_all_jobs", "clear_all_jobs"),
    ("clear_active_jobs", "clear_active_jobs"),
    ("create_job", "create_job"),
    ("create_job_if_missing", "create_job_if_missing_existing"),
    ("create_job_if_missing", "create_job_if_missing_new"),
    ("cancel_existing_jobs", "cancel_existing_jobs"),
    ("cancel_existing_jobs", "cancel_existing_jobs_noop"),
    ("update_job_attribution", "update_job_attribution"),
    ("update_job_status", "update_job_status_cancelled_late_update"),
    ("update_job_status", "update_job_status_completed"),
    ("update_job_status", "update_job_status_repeat_same_step"),
    ("update_job_status", "update_job_status_invalid_total_steps_rollback"),
    ("mark_cancelled", "mark_cancelled"),
    ("mark_cancelled", "mark_cancelled_repeat"),
    ("mark_classification_parse_error", "mark_classification_parse_error"),
    ("record_ad_windows_count", "record_ad_windows_count_zero"),
    ("mark_auto_retry_attempted", "mark_auto_retry_attempted"),
    ("reassign_pending_jobs", "reassign_pending_jobs"),
    ("reassign_pending_jobs", "reassign_pending_jobs_repeat"),
)

WRITER_DIFFERENTIAL_CASES = (
    {
        "case_id": "action_dequeue_job_claim",
        "operation": "action",
        "action": "dequeue_job",
        "owner_group": "job",
        "owner": "dequeue_job",
        "source": "src/app/writer/actions/jobs.py:dequeue_job_action",
    },
    {
        "case_id": "action_dequeue_job_empty_queue_noop",
        "operation": "action",
        "action": "dequeue_job",
        "owner_group": "job",
        "owner": "dequeue_job",
        "source": "src/app/writer/actions/jobs.py:dequeue_job_action",
    },
    {
        "case_id": "action_cleanup_stale_jobs",
        "operation": "action",
        "action": "cleanup_stale_jobs",
        "owner_group": "job",
        "owner": "cleanup_stale_jobs",
        "source": "src/app/writer/actions/jobs.py:cleanup_stale_jobs_action",
    },
    {
        "case_id": "action_cleanup_stale_jobs_cutoff_sides",
        "operation": "action",
        "action": "cleanup_stale_jobs",
        "owner_group": "job",
        "owner": "cleanup_stale_jobs",
        "source": "src/app/writer/actions/jobs.py:cleanup_stale_jobs_action",
    },
    {
        "case_id": "action_clear_all_jobs",
        "operation": "action",
        "action": "clear_all_jobs",
        "owner_group": "job",
        "owner": "clear_all_jobs",
        "source": "src/app/writer/actions/jobs.py:clear_all_jobs_action",
    },
    {
        "case_id": "action_clear_active_jobs",
        "operation": "action",
        "action": "clear_active_jobs",
        "owner_group": "job",
        "owner": "clear_active_jobs",
        "source": "src/app/writer/actions/jobs.py:clear_active_jobs_action",
    },
    {
        "case_id": "action_create_job",
        "operation": "action",
        "action": "create_job",
        "owner_group": "job",
        "owner": "create_job",
        "source": "src/app/writer/actions/jobs.py:create_job_action",
    },
    {
        "case_id": "action_create_job_if_missing_existing",
        "operation": "action",
        "action": "create_job_if_missing",
        "owner_group": "job",
        "owner": "create_job_if_missing",
        "source": "src/app/writer/actions/jobs.py:create_job_if_missing_action",
    },
    {
        "case_id": "action_create_job_if_missing_new",
        "operation": "action",
        "action": "create_job_if_missing",
        "owner_group": "job",
        "owner": "create_job_if_missing",
        "source": "src/app/writer/actions/jobs.py:create_job_if_missing_action",
    },
    {
        "case_id": "action_cancel_existing_jobs",
        "operation": "action",
        "action": "cancel_existing_jobs",
        "owner_group": "job",
        "owner": "cancel_existing_jobs",
        "source": "src/app/writer/actions/jobs.py:cancel_existing_jobs_action",
    },
    {
        "case_id": "action_cancel_existing_jobs_noop",
        "operation": "action",
        "action": "cancel_existing_jobs",
        "owner_group": "job",
        "owner": "cancel_existing_jobs",
        "source": "src/app/writer/actions/jobs.py:cancel_existing_jobs_action",
    },
    {
        "case_id": "action_update_job_attribution",
        "operation": "action",
        "action": "update_job_attribution",
        "owner_group": "job",
        "owner": "update_job_attribution",
        "source": "src/app/writer/actions/jobs.py:update_job_attribution_action",
    },
    {
        "case_id": "action_update_job_status_cancelled_late_update",
        "operation": "action",
        "action": "update_job_status",
        "owner_group": "job",
        "owner": "update_job_status",
        "source": "src/app/writer/actions/jobs.py:update_job_status_action",
    },
    {
        "case_id": "action_update_job_status_completed",
        "operation": "action",
        "action": "update_job_status",
        "owner_group": "job",
        "owner": "update_job_status",
        "source": "src/app/writer/actions/jobs.py:update_job_status_action",
    },
    {
        "case_id": "action_update_job_status_repeat_same_step",
        "operation": "action",
        "action": "update_job_status",
        "owner_group": "job",
        "owner": "update_job_status",
        "source": "src/app/writer/actions/jobs.py:update_job_status_action",
    },
    {
        "case_id": "action_update_job_status_invalid_total_steps_rollback",
        "operation": "action",
        "action": "update_job_status",
        "owner_group": "job",
        "owner": "update_job_status",
        "source": "src/app/writer/actions/jobs.py:update_job_status_action",
        "expect_success": False,
    },
    {
        "case_id": "action_mark_cancelled",
        "operation": "action",
        "action": "mark_cancelled",
        "owner_group": "job",
        "owner": "mark_cancelled",
        "source": "src/app/writer/actions/jobs.py:mark_cancelled_action",
    },
    {
        "case_id": "action_mark_cancelled_repeat",
        "operation": "action",
        "action": "mark_cancelled",
        "owner_group": "job",
        "owner": "mark_cancelled",
        "source": "src/app/writer/actions/jobs.py:mark_cancelled_action",
    },
    {
        "case_id": "action_mark_classification_parse_error",
        "operation": "action",
        "action": "mark_classification_parse_error",
        "owner_group": "job",
        "owner": "mark_classification_parse_error",
        "source": "src/app/writer/actions/jobs.py:mark_classification_parse_error_action",
    },
    {
        "case_id": "action_record_ad_windows_count_zero",
        "operation": "action",
        "action": "record_ad_windows_count",
        "owner_group": "job",
        "owner": "record_ad_windows_count",
        "source": "src/app/writer/actions/jobs.py:record_ad_windows_count_action",
    },
    {
        "case_id": "action_mark_auto_retry_attempted",
        "operation": "action",
        "action": "mark_auto_retry_attempted",
        "owner_group": "job",
        "owner": "mark_auto_retry_attempted",
        "source": "src/app/writer/actions/jobs.py:mark_auto_retry_attempted_action",
    },
    {
        "case_id": "action_reassign_pending_jobs",
        "operation": "action",
        "action": "reassign_pending_jobs",
        "owner_group": "job",
        "owner": "reassign_pending_jobs",
        "source": "src/app/writer/actions/jobs.py:reassign_pending_jobs_action",
    },
    {
        "case_id": "action_reassign_pending_jobs_repeat",
        "operation": "action",
        "action": "reassign_pending_jobs",
        "owner_group": "job",
        "owner": "reassign_pending_jobs",
        "source": "src/app/writer/actions/jobs.py:reassign_pending_jobs_action",
    },
)

_SINGLETON_RUN = "jobs-manager-singleton"
_FIXED_TIME = "2026-01-02T03:04:05"


def _prepare_singleton_run(pair: WriterParityPair) -> None:
    """Give run-count actions a real singleton and fixture-linked jobs."""
    for backend in (pair.python, pair.rust):
        with sqlite3.connect(backend.db_path) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                """INSERT OR IGNORE INTO jobs_manager_run
                   (id,status,trigger,started_at,completed_at,total_jobs,queued_jobs,
                    running_jobs,completed_jobs,failed_jobs,skipped_jobs,context_json,
                    counters_reset_at,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    _SINGLETON_RUN,
                    "running",
                    "parity-job-actions",
                    "2026-01-02 03:04:05.000000",
                    None,
                    3,
                    0,
                    1,
                    1,
                    1,
                    0,
                    '{"source":"synthetic-job-parity"}',
                    "2026-01-01 00:00:00.000000",
                    "2026-01-02 03:04:05.000000",
                    "2026-01-02 03:04:05.000000",
                ),
            )
            connection.execute(
                "UPDATE processing_job SET jobs_manager_run_id=? WHERE jobs_manager_run_id=?",
                (_SINGLETON_RUN, "00000000-0000-0000-0000-000000000400"),
            )


def _insert_job(
    connection: sqlite3.Connection,
    job_id: str,
    post_guid: str,
    status: str,
    *,
    run_id: str | None = _SINGLETON_RUN,
    created_at: str = "2026-01-02 03:04:05.000000",
    requested_by_user_id: int | None = 101,
    billing_user_id: int | None = 101,
) -> None:
    connection.execute(
        """INSERT INTO processing_job
           (id,jobs_manager_run_id,post_guid,status,current_step,step_name,total_steps,
            progress_percentage,created_at,requested_by_user_id,billing_user_id,
            stage_history,ad_windows_count,had_classification_parse_error,auto_retry_attempted)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            job_id,
            run_id,
            post_guid,
            status,
            0,
            "Queued",
            4,
            0.0,
            created_at,
            requested_by_user_id,
            billing_user_id,
            "[]",
            None,
            0,
            0,
        ),
    )


def _seed_case(pair: WriterParityPair, case_id: str) -> dict[str, Any]:
    """Set up the same isolated rows in both cloned fixture databases."""
    manifest = pair.manifest
    ids = {
        "claim": "00000000-0000-0000-0000-000000000451",
        "claim_second": "00000000-0000-0000-0000-000000000452",
        "new": "00000000-0000-0000-0000-000000000453",
        "attribution": "00000000-0000-0000-0000-000000000454",
        "cancel_current": "00000000-0000-0000-0000-000000000455",
        "cancel_pending": "00000000-0000-0000-0000-000000000456",
        "cancel_running": "00000000-0000-0000-0000-000000000457",
        "reassign": "00000000-0000-0000-0000-000000000458",
        "stale_old": "00000000-0000-0000-0000-000000000459",
        "stale_recent": "00000000-0000-0000-0000-000000000460",
    }
    stale_reference_time = datetime.now(UTC).replace(tzinfo=None)
    for backend in (pair.python, pair.rust):
        with sqlite3.connect(backend.db_path) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            if case_id == "dequeue_job_claim":
                connection.execute(
                    "DELETE FROM processing_job WHERE id=?", (manifest.job_ids[1],)
                )
                _insert_job(connection, ids["claim"], manifest.post_guids[0], "pending")
                _insert_job(
                    connection,
                    ids["claim_second"],
                    manifest.post_guids[1],
                    "pending",
                    created_at="2026-01-03 03:04:05.000000",
                )
            elif case_id == "dequeue_job_empty_queue_noop":
                connection.execute("DELETE FROM processing_job")
            elif case_id == "cleanup_stale_jobs_cutoff_sides":
                connection.execute("DELETE FROM processing_job")
                _insert_job(
                    connection,
                    ids["stale_old"],
                    manifest.post_guids[0],
                    "pending",
                    run_id=None,
                    created_at=(
                        stale_reference_time - timedelta(seconds=122)
                    ).isoformat(sep=" "),
                )
                _insert_job(
                    connection,
                    ids["stale_recent"],
                    manifest.post_guids[1],
                    "pending",
                    run_id=None,
                    created_at=(
                        stale_reference_time - timedelta(seconds=118)
                    ).isoformat(sep=" "),
                )
            elif case_id == "create_job_if_missing_new":
                pass
            elif case_id == "cancel_existing_jobs":
                _insert_job(
                    connection,
                    ids["cancel_current"],
                    manifest.post_guids[0],
                    "running",
                )
                _insert_job(
                    connection,
                    ids["cancel_pending"],
                    manifest.post_guids[0],
                    "pending",
                )
                _insert_job(
                    connection,
                    ids["cancel_running"],
                    manifest.post_guids[0],
                    "running",
                )
                connection.execute(
                    """INSERT INTO model_call
                       (post_id,first_segment_sequence_num,last_segment_sequence_num,
                        model_name,prompt,timestamp,status,retry_attempts)
                       VALUES (301,100,100,'parity-job-cancel','synthetic',?, 'pending',0)""",
                    ("2026-01-02 03:04:05.000000",),
                )
            elif case_id == "update_job_attribution":
                _insert_job(
                    connection,
                    ids["attribution"],
                    manifest.post_guids[0],
                    "pending",
                    requested_by_user_id=None,
                    billing_user_id=None,
                )
            elif case_id in {
                "reassign_pending_jobs",
                "reassign_pending_jobs_repeat",
            }:
                _insert_job(
                    connection,
                    ids["reassign"],
                    manifest.post_guids[0],
                    "pending",
                    run_id=(
                        "00000000-0000-0000-0000-000000000400"
                        if case_id == "reassign_pending_jobs"
                        else _SINGLETON_RUN
                    ),
                )
    return ids


def _simple_params(case_id: str) -> dict[str, Any] | None:
    if case_id in {"dequeue_job_claim", "dequeue_job_empty_queue_noop"}:
        return {"run_id": _SINGLETON_RUN}
    if case_id == "cleanup_stale_jobs":
        return {"older_than_seconds": 86400}
    if case_id == "cleanup_stale_jobs_cutoff_sides":
        return {"older_than_seconds": 120}
    if case_id in {"clear_all_jobs", "clear_active_jobs"}:
        return {}
    return None


def _creation_params(
    pair: WriterParityPair, case_id: str, ids: dict[str, Any]
) -> dict[str, Any] | None:
    manifest = pair.manifest
    if case_id == "create_job":
        return {
            "job_data": {
                "id": ids["new"],
                "jobs_manager_run_id": _SINGLETON_RUN,
                "post_guid": manifest.post_guids[1],
                "status": "pending",
                "current_step": 0,
                "step_name": "Queued",
                "total_steps": 4,
                "progress_percentage": 0.0,
                "created_at": _FIXED_TIME,
                "requested_by_user_id": manifest.user_id,
                "billing_user_id": manifest.user_id,
            }
        }
    if case_id == "create_job_if_missing_existing":
        return {
            "job_data": {
                "id": ids["new"],
                "post_guid": manifest.post_guids[0],
                "status": "pending",
                "created_at": _FIXED_TIME,
            }
        }
    if case_id == "create_job_if_missing_new":
        return {
            "job_data": {
                "id": ids["new"],
                "jobs_manager_run_id": _SINGLETON_RUN,
                "post_guid": "job-parity-new-guid",
                "status": "pending",
                "created_at": _FIXED_TIME,
            }
        }
    return None


def _transition_params(  # noqa: PLR0912 - action-specific parameter matrix
    pair: WriterParityPair, case_id: str, ids: dict[str, Any]
) -> dict[str, Any] | None:
    manifest = pair.manifest
    if case_id == "cancel_existing_jobs":
        return {
            "post_guid": manifest.post_guids[0],
            "current_job_id": ids["cancel_current"],
        }
    if case_id == "cancel_existing_jobs_noop":
        return {"post_guid": "parity-no-active-jobs", "current_job_id": "new-job"}
    if case_id == "update_job_attribution":
        return {
            "job_id": ids["attribution"],
            "run_id": _SINGLETON_RUN,
            "requested_by_user_id": manifest.user_id,
            "billing_user_id": manifest.user_id,
        }
    if case_id == "update_job_status_cancelled_late_update":
        return {
            "job_id": manifest.job_ids[2],
            "status": "completed",
            "step": 4,
            "step_name": "Late completion",
            "progress": 100.0,
            "total_steps": 4,
            "error_message": "late worker result",
        }
    if case_id == "update_job_status_completed":
        return {
            "job_id": manifest.job_ids[1],
            "status": "completed",
            "step": 4,
            "step_name": "Completed",
            "progress": 100.0,
            "total_steps": 4,
        }
    if case_id == "update_job_status_repeat_same_step":
        return {
            "job_id": manifest.job_ids[1],
            "status": "running",
            "step": 2,
            "step_name": "Transcribing",
            "progress": 42.5,
            "total_steps": 4,
        }
    if case_id == "update_job_status_invalid_total_steps_rollback":
        return {
            "job_id": manifest.job_ids[1],
            "status": "completed",
            "step": 4,
            "step_name": "Must roll back",
            "progress": 100.0,
            # Status/step/progress are written before this validation fails.
            "total_steps": "not-an-integer",
        }
    if case_id == "mark_cancelled":
        return {"job_id": manifest.job_ids[1], "reason": "Parity cancellation"}
    if case_id == "mark_cancelled_repeat":
        return {"job_id": manifest.job_ids[2], "reason": "Synthetic cancellation"}
    if case_id == "mark_classification_parse_error":
        return {"job_id": manifest.job_ids[1]}
    if case_id == "record_ad_windows_count_zero":
        return {"job_id": manifest.job_ids[1], "count": 0}
    if case_id == "mark_auto_retry_attempted":
        return {"job_id": manifest.job_ids[1]}
    if case_id in {"reassign_pending_jobs", "reassign_pending_jobs_repeat"}:
        return {"run_id": _SINGLETON_RUN}
    return None


def _action_params(
    pair: WriterParityPair, case_id: str, ids: dict[str, Any]
) -> dict[str, Any]:
    for params in (
        _simple_params(case_id),
        _creation_params(pair, case_id, ids),
        _transition_params(pair, case_id, ids),
    ):
        if params is not None:
            return params
    raise AssertionError(f"Unknown job parity case: {case_id}")


def build_writer_job_case(
    case_id: str, pair: WriterParityPair
) -> tuple[dict[str, Any], WriteCommand]:
    """Build the equivalent Rust RPC and Python writer command for a case."""
    case_id = case_id.removeprefix("action_")
    action = next((action for action, name in _JOBS if name == case_id), None)
    if action is None:
        raise AssertionError(f"Unknown job parity case: {case_id}")
    _prepare_singleton_run(pair)
    ids = _seed_case(pair, case_id)
    params = _action_params(pair, case_id, ids)
    command_id = f"parity-action-{case_id}"
    # Python's create_job action converts created_at in its input dictionary.
    # Keep the wire payload independent so Python execution cannot mutate the
    # later Rust request (the two backends must receive the same JSON values).
    operation_params = json.loads(json.dumps(params))
    python_params = json.loads(json.dumps(params))
    operation = {
        "operation": "action",
        "action": action,
        "params": operation_params,
    }
    command = WriteCommand(
        command_id,
        WriteCommandType.ACTION,
        None,
        {"action": action, "params": python_params},
    )
    return operation, command


def _normalize_time_columns(
    db_path: Path,
    table: str,
    rows: list[tuple[Any, ...]],
    before_rows: list[tuple[Any, ...]],
) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as connection:
        columns = [
            row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
        ]
    time_columns: set[str] = set()
    if table == "processing_job":
        time_columns.update({"started_at", "completed_at"})
    elif table == "jobs_manager_run":
        time_columns.update(
            {"started_at", "completed_at", "updated_at", "counters_reset_at"}
        )
    indices = {columns.index(name) for name in time_columns if name in columns}
    history_index = (
        columns.index("stage_history") if table == "processing_job" else None
    )
    primary_key_index = 0
    before_by_id = {row[primary_key_index]: row for row in before_rows}
    normalized = []
    for row in rows:
        values = list(row)
        before = before_by_id.get(row[primary_key_index])
        for index in indices:
            if (
                before is not None
                and values[index] != before[index]
                and values[index] is not None
            ):
                values[index] = "<utc-time>"
        if history_index is not None and values[history_index]:
            history = json.loads(values[history_index])
            prior_history = (
                json.loads(before[history_index])
                if before is not None and before[history_index]
                else []
            )
            for index, entry in enumerate(history):
                if isinstance(entry, dict) and entry.get("started_at") is not None:
                    prior_started = (
                        prior_history[index].get("started_at")
                        if index < len(prior_history)
                        and isinstance(prior_history[index], dict)
                        else None
                    )
                    if entry["started_at"] != prior_started:
                        entry["started_at"] = "<utc-time>"
            values[history_index] = json.dumps(
                history, sort_keys=True, separators=(",", ":")
            )
        normalized.append(tuple(values))
    return normalized


def _normalized_projection(
    observation: dict[str, Any], backend: str
) -> dict[str, list[tuple[Any, ...]]]:
    pair = observation["pair"]
    projection = observation[f"{backend}_rows"]
    before_projection = observation[f"{backend}_before"]
    db_path = getattr(pair, backend).db_path
    normalized = dict(projection)
    for table in ("processing_job", "jobs_manager_run"):
        if table in normalized:
            normalized[table] = _normalize_time_columns(
                db_path, table, normalized[table], before_projection[table]
            )
    return normalized


def writer_job_records(
    db_path: Path, rows: list[tuple[Any, ...]]
) -> dict[str, dict[str, Any]]:
    """Return job rows keyed by id without assuming physical column order."""
    with sqlite3.connect(db_path) as connection:
        columns = [
            row[1] for row in connection.execute('PRAGMA table_info("processing_job")')
        ]
    return {
        record["id"]: record
        for record in (dict(zip(columns, row, strict=True)) for row in rows)
    }


def _assert_dequeue_effects(case_id: str, jobs: dict[str, dict[str, Any]]) -> None:
    if case_id == "dequeue_job_empty_queue_noop":
        assert not jobs
        return
    if case_id != "dequeue_job_claim":
        return
    claimed = "00000000-0000-0000-0000-000000000451"
    assert jobs[claimed]["status"] == "running"
    assert sum(row["status"] == "running" for row in jobs.values()) == 1
    assert jobs["00000000-0000-0000-0000-000000000452"]["status"] == "pending"


def _assert_delete_effects(
    case_id: str,
    observation: dict[str, Any],
    jobs: dict[str, dict[str, Any]],
    job_ids: tuple[str, ...],
) -> None:
    if case_id == "cleanup_stale_jobs":
        assert observation["python_data"] == {"count": 3}
        assert not jobs
    elif case_id == "cleanup_stale_jobs_cutoff_sides":
        assert observation["python_data"] == {"count": 1}
        assert "00000000-0000-0000-0000-000000000459" not in jobs
        assert "00000000-0000-0000-0000-000000000460" in jobs
    elif case_id == "clear_all_jobs":
        assert observation["python_data"] == 3
        assert not jobs
    elif case_id == "clear_active_jobs":
        assert observation["python_data"] == 1
        assert jobs[job_ids[0]]["status"] == "completed"
        assert jobs[job_ids[2]]["status"] == "cancelled"
        assert job_ids[1] not in jobs


def _assert_creation_effects(
    case_id: str, observation: dict[str, Any], jobs: dict[str, dict[str, Any]]
) -> None:
    created_id = "00000000-0000-0000-0000-000000000453"
    if case_id == "create_job":
        created = jobs[created_id]
        assert observation["python_data"] == {"job_id": created["id"]}
        assert json.loads(created["stage_history"])[0]["step_name"] == "Queued"
    elif case_id == "create_job_if_missing_existing":
        assert observation["python_data"] == {"job_id": None, "skipped": True}
        assert created_id not in jobs
    elif case_id == "create_job_if_missing_new":
        assert created_id in jobs


def _assert_cancellation_effects(
    case_id: str,
    observation: dict[str, Any],
    rows: dict[str, list[tuple[Any, ...]]],
    jobs: dict[str, dict[str, Any]],
    job_ids: tuple[str, ...],
) -> None:
    if case_id == "cancel_existing_jobs":
        ids = {row[0] for row in rows["processing_job"]}
        assert "00000000-0000-0000-0000-000000000455" in ids
        assert "00000000-0000-0000-0000-000000000456" not in ids
        assert "00000000-0000-0000-0000-000000000457" not in ids
        calls = observation["python_model_calls"]
        calls_by_id = {row["id"]: row for row in calls}
        assert calls_by_id[601]["status"] == "success"
        cancelled_call = next(
            row for row in calls if row["model_name"] == "parity-job-cancel"
        )
        assert cancelled_call["status"] == "cancelled"
    elif case_id == "update_job_status_cancelled_late_update":
        assert jobs[job_ids[2]]["status"] == "cancelled"
        assert jobs[job_ids[2]]["error_message"] == "Synthetic cancellation"
    elif case_id == "update_job_status_completed":
        assert jobs[job_ids[1]]["status"] == "completed"
        assert jobs[job_ids[1]]["completed_at"] is not None
    elif case_id == "update_job_status_repeat_same_step":
        assert jobs[job_ids[1]]["status"] == "running"
        history = json.loads(jobs[job_ids[1]]["stage_history"])
        assert len(history) == 1
        assert history[0]["step"] == 2
    elif case_id == "mark_cancelled_repeat":
        assert jobs[job_ids[2]]["status"] == "cancelled"
        assert jobs[job_ids[2]]["error_message"] == "Synthetic cancellation"
    elif case_id == "mark_cancelled":
        assert jobs[job_ids[1]]["status"] == "cancelled"
        assert jobs[job_ids[1]]["error_message"] == "Parity cancellation"


def _assert_field_effects(case_id: str, jobs: dict[str, dict[str, Any]]) -> None:
    if case_id == "update_job_attribution":
        attributed = jobs["00000000-0000-0000-0000-000000000454"]
        assert attributed["requested_by_user_id"] == 101
        assert attributed["billing_user_id"] == 101
    elif case_id == "record_ad_windows_count_zero":
        assert jobs["00000000-0000-0000-0000-000000000402"]["ad_windows_count"] == 0
    elif case_id == "mark_classification_parse_error":
        assert (
            jobs["00000000-0000-0000-0000-000000000402"][
                "had_classification_parse_error"
            ]
            == 1
        )
    elif case_id == "mark_auto_retry_attempted":
        assert jobs["00000000-0000-0000-0000-000000000402"]["auto_retry_attempted"] == 1
    elif case_id == "reassign_pending_jobs":
        reassigned = jobs["00000000-0000-0000-0000-000000000458"]
        assert reassigned["jobs_manager_run_id"] == _SINGLETON_RUN


def assert_writer_job_parity(observation: dict[str, Any]) -> None:
    """Compare action results and full DB state, normalizing only wall-clock fields."""
    case_id = observation["case"]["case_id"].removeprefix("action_")
    if case_id == "update_job_status_invalid_total_steps_rollback":
        assert observation["python_success"] is False
        assert observation["rust_success"] is False
        assert observation["python_error"]
        assert observation["rust_error"]
        assert observation["python_rows"] == observation["python_before"]
        assert observation["rust_rows"] == observation["rust_before"]
        assert observation["python_rows"] == observation["rust_rows"]
        return

    assert observation["python_success"] is True, observation["python_error"]
    assert observation["rust_success"] is True, observation["rust_error"]
    assert observation["rust_result"] == observation["python_data"], {
        "case_id": case_id,
        "python_result": observation["python_data"],
        "rust_result": observation["rust_result"],
    }
    python_rows = _normalized_projection(observation, "python")
    rust_rows = _normalized_projection(observation, "rust")
    assert rust_rows == python_rows, f"database effects differ for {case_id}"

    job_ids = observation["pair"].manifest.job_ids

    def records(backend: str, table: str) -> list[dict[str, Any]]:
        db_path = getattr(observation["pair"], backend).db_path
        with sqlite3.connect(db_path) as connection:
            columns = [
                row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
            ]
        return [
            dict(zip(columns, row, strict=True))
            for row in observation[f"{backend}_rows"][table]
        ]

    jobs = {row["id"]: row for row in records("python", "processing_job")}
    observation["python_model_calls"] = records("python", "model_call")
    _assert_dequeue_effects(case_id, jobs)
    _assert_delete_effects(case_id, observation, jobs, job_ids)
    _assert_creation_effects(case_id, observation, jobs)
    _assert_cancellation_effects(case_id, observation, python_rows, jobs, job_ids)
    _assert_field_effects(case_id, jobs)
