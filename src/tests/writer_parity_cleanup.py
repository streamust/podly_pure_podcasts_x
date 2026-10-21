"""Differential cases for cleanup actions on isolated Python/Rust clones."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from app.writer.protocol import WriteCommand, WriteCommandType
from tests.writer_parity_fixtures import WriterParityPair

WRITER_DIFFERENTIAL_CASES = (
    {
        "case_id": "cleanup_missing_audio_paths_recover_and_requeue",
        "operation": "action",
        "action": "cleanup_missing_audio_paths",
        "owner": "cleanup_missing_audio_paths",
        "owner_group": "cleanup",
        "source": "src/app/writer/actions/cleanup.py:cleanup_missing_audio_paths_action",
        "semantics": "path_recovery_and_requeue",
    },
    {
        "case_id": "cleanup_missing_audio_paths_legacy_title_candidate",
        "operation": "action",
        "action": "cleanup_missing_audio_paths",
        "owner": "cleanup_missing_audio_paths",
        "owner_group": "cleanup",
        "source": "src/app/writer/actions/cleanup.py:cleanup_missing_audio_paths_action",
        "semantics": "legacy_title_path_recovery",
    },
    {
        "case_id": "cleanup_missing_audio_paths_preserves_valid_paths",
        "operation": "action",
        "action": "cleanup_missing_audio_paths",
        "owner": "cleanup_missing_audio_paths",
        "owner_group": "cleanup",
        "source": "src/app/writer/actions/cleanup.py:cleanup_missing_audio_paths_action",
        "semantics": "valid_stored_path_is_noop",
    },
    {
        "case_id": "cleanup_missing_audio_paths_preserves_active_job",
        "operation": "action",
        "action": "cleanup_missing_audio_paths",
        "owner": "cleanup_missing_audio_paths",
        "owner_group": "cleanup",
        "source": "src/app/writer/actions/cleanup.py:cleanup_missing_audio_paths_action",
        "semantics": "missing_candidates_do_not_reset_active_job",
    },
    {
        "case_id": "cleanup_missing_audio_paths_ignores_empty_and_directory_candidates",
        "operation": "action",
        "action": "cleanup_missing_audio_paths",
        "owner": "cleanup_missing_audio_paths",
        "owner_group": "cleanup",
        "source": "src/app/writer/actions/cleanup.py:cleanup_missing_audio_paths_action",
        "semantics": "only_nonempty_regular_candidate_is_recoverable",
    },
    {
        "case_id": "cleanup_missing_audio_paths_ignores_unwhitelisted",
        "operation": "action",
        "action": "cleanup_missing_audio_paths",
        "owner": "cleanup_missing_audio_paths",
        "owner_group": "cleanup",
        "source": "src/app/writer/actions/cleanup.py:cleanup_missing_audio_paths_action",
        "semantics": "whitelist_selection_predicate",
    },
    {
        "case_id": "cleanup_clear_all_processing_data",
        "operation": "action",
        "action": "clear_post_processing_data",
        "owner": "clear_post_processing_data",
        "owner_group": "cleanup",
        "source": "src/app/writer/actions/cleanup.py:clear_post_processing_data_action",
    },
    {
        "case_id": "cleanup_clear_all_missing_post",
        "operation": "action",
        "action": "clear_post_processing_data",
        "owner": "clear_post_processing_data",
        "owner_group": "cleanup",
        "source": "src/app/writer/actions/cleanup.py:clear_post_processing_data_action",
        "semantics": "missing_post_rejected_without_effects",
        "expect_success": False,
        "missing_record": True,
    },
    {
        "case_id": "cleanup_clear_outputs_keep_transcript",
        "operation": "action",
        "action": "clear_post_processing_data_keep_transcript",
        "owner": "clear_post_processing_data_keep_transcript",
        "owner_group": "cleanup",
        "source": "src/app/writer/actions/cleanup.py:clear_post_processing_data_keep_transcript_action",
    },
    {
        "case_id": "cleanup_clear_outputs_keep_transcript_missing_post",
        "operation": "action",
        "action": "clear_post_processing_data_keep_transcript",
        "owner": "clear_post_processing_data_keep_transcript",
        "owner_group": "cleanup",
        "source": "src/app/writer/actions/cleanup.py:clear_post_processing_data_keep_transcript_action",
        "semantics": "missing_post_rejected_without_effects",
        "expect_success": False,
        "missing_record": True,
    },
    {
        "case_id": "cleanup_prepare_auto_retry_preserves_input",
        "operation": "action",
        "action": "prepare_post_for_auto_retry",
        "owner": "prepare_post_for_auto_retry",
        "owner_group": "cleanup",
        "source": "src/app/writer/actions/cleanup.py:prepare_post_for_auto_retry_action",
        "semantics": "retry_preserves_unprocessed_and_transcript",
    },
    {
        "case_id": "cleanup_prepare_auto_retry_missing_post",
        "operation": "action",
        "action": "prepare_post_for_auto_retry",
        "owner": "prepare_post_for_auto_retry",
        "owner_group": "cleanup",
        "source": "src/app/writer/actions/cleanup.py:prepare_post_for_auto_retry_action",
        "semantics": "missing_post_rejected_without_effects",
        "expect_success": False,
        "missing_record": True,
    },
    {
        "case_id": "cleanup_prepare_auto_retry_db_failure_after_unlink",
        "operation": "action",
        "action": "prepare_post_for_auto_retry",
        "owner": "prepare_post_for_auto_retry",
        "owner_group": "cleanup",
        "source": "src/app/writer/actions/cleanup.py:prepare_post_for_auto_retry_action",
        "semantics": "precommit_unlink_and_db_rollback",
        "expect_success": True,
        "recovery_after_failure": True,
        "repeat_count": 2,
    },
    {
        "case_id": "cleanup_processed_post_clears_db_state",
        "operation": "action",
        "action": "cleanup_processed_post",
        "owner": "cleanup_processed_post",
        "owner_group": "cleanup",
        "source": "src/app/writer/actions/cleanup.py:cleanup_processed_post_action",
    },
    {
        "case_id": "cleanup_processed_post_missing_post",
        "operation": "action",
        "action": "cleanup_processed_post",
        "owner": "cleanup_processed_post",
        "owner_group": "cleanup",
        "source": "src/app/writer/actions/cleanup.py:cleanup_processed_post_action",
        "semantics": "missing_post_rejected_without_effects",
        "expect_success": False,
        "missing_record": True,
    },
    {
        "case_id": "cleanup_processed_post_files_only",
        "operation": "action",
        "action": "cleanup_processed_post_files_only",
        "owner": "cleanup_processed_post_files_only",
        "owner_group": "cleanup",
        "source": "src/app/writer/actions/cleanup.py:cleanup_processed_post_files_only_action",
        "semantics": "unlink_files_keep_processing_metadata",
    },
    {
        "case_id": "cleanup_processed_post_files_only_missing_files",
        "operation": "action",
        "action": "cleanup_processed_post_files_only",
        "owner": "cleanup_processed_post_files_only",
        "owner_group": "cleanup",
        "source": "src/app/writer/actions/cleanup.py:cleanup_processed_post_files_only_action",
        "semantics": "missing_files_are_idempotent",
        "repeat_count": 2,
    },
    {
        "case_id": "cleanup_processed_post_files_only_skips_directories",
        "operation": "action",
        "action": "cleanup_processed_post_files_only",
        "owner": "cleanup_processed_post_files_only",
        "owner_group": "cleanup",
        "source": "src/app/writer/actions/cleanup.py:cleanup_processed_post_files_only_action",
        "semantics": "directory_path_is_not_unlinked",
    },
    {
        "case_id": "cleanup_processed_post_files_only_missing_post",
        "operation": "action",
        "action": "cleanup_processed_post_files_only",
        "owner": "cleanup_processed_post_files_only",
        "owner_group": "cleanup",
        "source": "src/app/writer/actions/cleanup.py:cleanup_processed_post_files_only_action",
        "semantics": "missing_post_rejected_without_effects",
        "expect_success": False,
        "missing_record": True,
    },
    {
        "case_id": "cleanup_prepare_auto_retry_skips_directories",
        "operation": "action",
        "action": "prepare_post_for_auto_retry",
        "owner": "prepare_post_for_auto_retry",
        "owner_group": "cleanup",
        "source": "src/app/writer/actions/cleanup.py:prepare_post_for_auto_retry_action",
        "semantics": "directory_candidates_are_not_unlinked",
    },
)

_POST_ID = 301
_POST_GUID = "parity-integral-duration"
_INPUT_NAME = "cleanup-parity-input.mp3"


def _case(case_id: str) -> dict[str, Any]:
    return next(
        item for item in WRITER_DIFFERENTIAL_CASES if item["case_id"] == case_id
    )


def _paths(backend: Any) -> tuple[Path, Path, Path]:
    input_path = backend.data_in / _INPUT_NAME
    processed_path = (
        backend.data_srv / "Parity_feed_one" / "cleanup-parity-processed.mp3"
    )
    processed_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_bytes(b"synthetic unprocessed audio\n")
    processed_path.write_bytes(b"synthetic processed audio\n")
    return input_path, processed_path, backend.data_srv


def _install_cleanup_order_guards(connection: sqlite3.Connection) -> None:
    """Fail if cleanup deletes a parent before its dependent rows."""
    connection.execute(
        """CREATE TRIGGER parity_identification_delete_order
           BEFORE DELETE ON transcript_segment
           WHEN EXISTS (SELECT 1 FROM identification
                        WHERE transcript_segment_id=OLD.id)
           BEGIN SELECT RAISE(ABORT, 'identifications must be deleted first'); END"""
    )
    connection.execute(
        """CREATE TRIGGER parity_audio_segment_delete_order
           BEFORE DELETE ON model_call
           WHEN EXISTS (SELECT 1 FROM audio_segment
                        WHERE model_call_id=OLD.id)
           BEGIN SELECT RAISE(ABORT, 'audio segments must be deleted first'); END"""
    )


def _seed_additional_transcript_graph(connection: sqlite3.Connection) -> None:
    connection.executemany(
        """INSERT INTO transcript_segment
           (id,post_id,sequence_num,start_time,end_time,text,speaker_label)
           VALUES (?,301,?,?,?,'Synthetic chunk boundary segment','A')""",
        [
            (10_000 + index, index + 1, index * 10.0, (index + 1) * 10.0)
            for index in range(500)
        ],
    )
    connection.executemany(
        """INSERT INTO identification
           (id,transcript_segment_id,model_call_id,confidence,label)
           VALUES (?, ?, 601, 0.5, 'synthetic')""",
        [(11_000 + index, 10_000 + index) for index in range(500)],
    )


def _seed_backend(  # noqa: PLR0912 - scenario-specific isolated fixture setup
    backend: Any, case_id: str
) -> None:
    input_path, processed_path, srv_root = _paths(backend)
    missing_input = backend.data_in / "intentionally-missing-input.mp3"
    stale_processed = backend.data_srv / "intentionally-missing-processed.mp3"
    if case_id == "cleanup_missing_audio_paths_recover_and_requeue":
        # Rust and Python both search the sanitized feed directory for a
        # processed copy derived from the unprocessed basename.
        derived_path = srv_root / "Parity_feed_one" / missing_input.name
        derived_path.write_bytes(b"recoverable derived processed audio\n")
        input_value = str(missing_input)
        processed_value = str(stale_processed)
    elif case_id == "cleanup_missing_audio_paths_legacy_title_candidate":
        legacy_path = srv_root / "Parity feed one" / "Integral duration episode.mp3"
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_bytes(b"legacy-layout recovery candidate\n")
        input_value = str(missing_input)
        processed_value = str(stale_processed)
    elif case_id in {
        "cleanup_prepare_auto_retry_preserves_input",
        "cleanup_prepare_auto_retry_db_failure_after_unlink",
    }:
        derived_path = srv_root / "Parity_feed_one" / input_path.name
        derived_path.write_bytes(b"synthetic derived processed audio\n")
        input_value = str(input_path)
        processed_value = str(processed_path)
    elif case_id in {
        "cleanup_missing_audio_paths_preserves_active_job",
        "cleanup_missing_audio_paths_ignores_unwhitelisted",
    }:
        input_path.unlink(missing_ok=True)
        processed_path.unlink(missing_ok=True)
        input_value = str(missing_input)
        processed_value = str(stale_processed)
    elif case_id == (
        "cleanup_missing_audio_paths_ignores_empty_and_directory_candidates"
    ):
        input_path.unlink(missing_ok=True)
        processed_path.unlink(missing_ok=True)
        processed_path.touch()
        derived_directory = srv_root / "Parity_feed_one" / missing_input.name
        derived_directory.mkdir()
        input_value = str(missing_input)
        processed_value = str(processed_path)
    elif case_id == "cleanup_processed_post_files_only_missing_files":
        input_path.unlink(missing_ok=True)
        processed_path.unlink(missing_ok=True)
        input_value = str(input_path)
        processed_value = str(processed_path)
    elif case_id == "cleanup_processed_post_files_only_skips_directories":
        input_path.unlink(missing_ok=True)
        input_path.mkdir()
        processed_path.unlink(missing_ok=True)
        input_value = str(input_path)
        processed_value = str(backend.data_srv / "missing-processed-audio.mp3")
    elif case_id == "cleanup_prepare_auto_retry_skips_directories":
        processed_path.unlink(missing_ok=True)
        processed_path.mkdir()
        derived_directory = srv_root / "Parity_feed_one" / input_path.name
        derived_directory.mkdir()
        input_value = str(input_path)
        processed_value = str(processed_path)
    else:
        input_value = str(input_path)
        processed_value = str(processed_path)

    with sqlite3.connect(backend.db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """UPDATE post SET whitelisted=1,unprocessed_audio_path=?,
               processed_audio_path=?,duration=123.5,chapter_data='[1]',
               bleep_windows='[[1,2]]',transcript_word_timestamps='[{\"word\":\"kept\"}]',
               refined_ad_boundaries='[{\"start\":1,\"end\":2}]',
               refined_ad_boundaries_updated_at='2026-01-02 03:04:05.000000'
               WHERE id=?""",
            (input_value, processed_value, _POST_ID),
        )
        if case_id == "cleanup_missing_audio_paths_ignores_unwhitelisted":
            connection.execute("UPDATE post SET whitelisted=0 WHERE id=?", (_POST_ID,))
        # Keep each supported Whisper-recognition predicate independent from
        # the others: exact prompt, model name containing "whisper", and the
        # local_ model prefix. The classifier row must still be deleted.
        if case_id in {
            "cleanup_clear_outputs_keep_transcript",
            "cleanup_prepare_auto_retry_preserves_input",
            "cleanup_prepare_auto_retry_db_failure_after_unlink",
        }:
            connection.execute(
                """UPDATE model_call SET model_name='synthetic/prompt-only',
                   prompt='Whisper transcription job' WHERE id=601"""
            )
            connection.executemany(
                """INSERT INTO model_call
                   (id,post_id,first_segment_sequence_num,last_segment_sequence_num,
                    model_name,prompt,response,timestamp,status,prompt_tokens,
                    completion_tokens,total_tokens,retry_attempts)
                   VALUES (?,301,0,0,?,?,?,'2026-01-02 03:04:05.000000',
                           'success',1,1,2,0)""",
                [
                    (
                        603,
                        "synthetic/custom-whisper",
                        "Custom transcription",
                        "Whisper name",
                    ),
                    (604, "local_speech", "Local transcription", "Local model"),
                ],
            )
        else:
            connection.execute(
                """UPDATE model_call SET model_name='whisper-large-v3',
                   prompt='Whisper transcription job' WHERE id=601"""
            )
        connection.execute(
            """INSERT INTO model_call
               (id,post_id,first_segment_sequence_num,last_segment_sequence_num,
                model_name,prompt,response,timestamp,status,prompt_tokens,
                completion_tokens,total_tokens,retry_attempts)
               VALUES (602,301,10,10,'synthetic/classifier','Synthetic classifier',
                       'Synthetic response','2026-01-02 03:04:05.000000','success',1,1,2,0)"""
        )
        if case_id in {
            "cleanup_missing_audio_paths_recover_and_requeue",
            "cleanup_missing_audio_paths_legacy_title_candidate",
            "cleanup_missing_audio_paths_preserves_valid_paths",
        }:
            connection.execute(
                """UPDATE processing_job SET status='failed',current_step=4,
                   step_name='Failed',error_message='missing output',started_at=?,
                   completed_at=?,created_at=?,stage_history='[]'
                   WHERE id='00000000-0000-0000-0000-000000000403'""",
                (
                    "2026-01-02 03:04:05.000000",
                    "2026-01-02 03:04:06.000000",
                    "2999-01-02 03:04:07.000000",
                ),
            )
        if case_id == "cleanup_missing_audio_paths_preserves_active_job":
            updated = connection.execute(
                """UPDATE processing_job SET status='running',current_step=3,
                   step_name='Segmenting',progress_percentage=42.5,
                   error_message=NULL,started_at=?,completed_at=NULL,created_at=?
                   WHERE id='00000000-0000-0000-0000-000000000403'""",
                ("2026-01-02 03:04:05.000000", "2026-01-02 03:04:07.000000"),
            )
            assert updated.rowcount == 1, "active cleanup parity job fixture is missing"
        if case_id in {
            "cleanup_prepare_auto_retry_db_failure_after_unlink",
        }:
            connection.execute(
                """CREATE TRIGGER parity_interrupt_cleanup_model_call
                   BEFORE DELETE ON model_call
                   WHEN OLD.id=602
                   BEGIN SELECT RAISE(ABORT, 'synthetic interrupted cleanup'); END"""
            )
        if case_id in {
            "cleanup_clear_all_processing_data",
            "cleanup_clear_outputs_keep_transcript",
            "cleanup_prepare_auto_retry_preserves_input",
            "cleanup_prepare_auto_retry_db_failure_after_unlink",
        }:
            _seed_additional_transcript_graph(connection)
        if case_id in {
            "cleanup_clear_all_processing_data",
            "cleanup_processed_post_clears_db_state",
        }:
            _install_cleanup_order_guards(connection)
        if case_id == "cleanup_processed_post_clears_db_state":
            connection.execute(
                """INSERT INTO jobs_manager_run
                   (id,status,trigger,started_at,completed_at,total_jobs,queued_jobs,
                    running_jobs,completed_jobs,failed_jobs,skipped_jobs,context_json,
                    counters_reset_at,created_at,updated_at)
                   VALUES ('jobs-manager-singleton','running','cleanup-parity',
                           '2026-01-02 03:04:05.000000',NULL,99,0,99,0,0,0,'{}',
                           '2026-01-02 03:04:04.000000','2026-01-02 03:04:05.000000',
                           '2026-01-02 03:04:05.000000')"""
            )
            connection.execute(
                "UPDATE processing_job SET jobs_manager_run_id='jobs-manager-singleton'"
            )


def build_writer_cleanup_case(
    case_id: str, pair: WriterParityPair
) -> tuple[dict[str, Any], WriteCommand, dict[str, dict[str, str]]]:
    """Build each action against an isolated data root and matching DB clone."""
    case = _case(case_id)
    for backend in (pair.python, pair.rust):
        _seed_backend(backend, case_id)

    action = case["action"]
    post_id = pair.manifest.missing_post_id if case.get("missing_record") else _POST_ID
    params = {} if action == "cleanup_missing_audio_paths" else {"post_id": post_id}
    operation = {
        "operation": "action",
        "action": action,
        "params": json.loads(json.dumps(params)),
    }
    command = WriteCommand(
        f"parity-{case_id}",
        WriteCommandType.ACTION,
        None,
        {"action": action, "params": json.loads(json.dumps(params))},
    )
    environment_overrides = {
        backend_name: {
            "PODLY_INSTANCE_DIR": str(backend.instance_dir),
            "PODLY_PODCAST_DATA_DIR": str(backend.instance_dir / "data"),
        }
        for backend_name, backend in (("python", pair.python), ("rust", pair.rust))
    }
    return operation, command, environment_overrides


def release_writer_cleanup_interruption(pair: WriterParityPair) -> None:
    """Remove only the synthetic failure hook between recovery attempts."""
    for backend in (pair.python, pair.rust):
        with sqlite3.connect(backend.db_path) as connection:
            connection.execute("DROP TRIGGER parity_interrupt_cleanup_model_call")


def _projection_records(
    observation: dict[str, Any], backend: str, table: str, *, before: bool = False
) -> list[dict[str, Any]]:
    db_path = getattr(observation["pair"], backend).db_path
    with sqlite3.connect(db_path) as connection:
        columns = [
            row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
        ]
    projection_key = f"{backend}_{'before' if before else 'rows'}"
    return [
        dict(zip(columns, row, strict=True))
        for row in observation[projection_key][table]
    ]


def _database_row_diff_summary(
    python_rows: dict[str, list[tuple[Any, ...]]],
    rust_rows: dict[str, list[tuple[Any, ...]]],
    db_path: Path,
) -> str:
    """Describe differing table/column names without logging database values."""
    summaries = []
    for table in sorted(python_rows.keys() | rust_rows.keys()):
        python_table = python_rows.get(table, [])
        rust_table = rust_rows.get(table, [])
        if python_table == rust_table:
            continue

        with sqlite3.connect(db_path) as connection:
            info = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        columns = [row[1] for row in info]
        primary_key = [
            index
            for _, index in sorted(
                (row[5], index) for index, row in enumerate(info) if row[5]
            )
        ]
        if primary_key:
            python_by_key = {
                tuple(row[index] for index in primary_key): row for row in python_table
            }
            rust_by_key = {
                tuple(row[index] for index in primary_key): row for row in rust_table
            }
            common_keys = python_by_key.keys() & rust_by_key.keys()
            differing_columns = Counter(
                columns[index]
                for key in common_keys
                for index, (python_value, rust_value) in enumerate(
                    zip(python_by_key[key], rust_by_key[key], strict=True)
                )
                if python_value != rust_value
            )
            python_only = len(python_by_key.keys() - rust_by_key.keys())
            rust_only = len(rust_by_key.keys() - python_by_key.keys())
        else:
            differing_columns = Counter()
            python_only = len(python_table)
            rust_only = len(rust_table)

        names = sorted(
            differing_columns, key=lambda name: (-differing_columns[name], name)
        )
        shown_columns = names[:12]
        if len(names) > len(shown_columns):
            shown_columns.append(f"...+{len(names) - len(shown_columns)}")
        summaries.append(
            f"{table}(python_only_rows={python_only},rust_only_rows={rust_only},"
            f"differing_columns={shown_columns})"
        )
        if len(summaries) == 6:
            summaries.append("...")
            break
    return "; ".join(summaries)


def _normalize_times(
    projection: dict[str, list[tuple[Any, ...]]],
    before: dict[str, list[tuple[Any, ...]]],
    db_path: Path,
) -> dict[str, list[tuple[Any, ...]]]:
    normalized = dict(projection)
    with sqlite3.connect(db_path) as connection:
        table_columns = {
            table: [
                row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
            ]
            for table in ("processing_job", "jobs_manager_run")
            if table in normalized
        }
    for table, columns in table_columns.items():
        time_columns = {
            "processing_job": {"started_at", "completed_at"},
            "jobs_manager_run": {
                "started_at",
                "completed_at",
                "updated_at",
                "counters_reset_at",
            },
        }[table]
        indices = [columns.index(name) for name in time_columns if name in columns]
        history_index = (
            columns.index("stage_history") if table == "processing_job" else None
        )
        prior_by_id = {row[0]: row for row in before[table]}
        rows = []
        for row in normalized[table]:
            values = list(row)
            prior = prior_by_id.get(row[0])
            for index in indices:
                if (
                    prior is not None
                    and values[index] != prior[index]
                    and values[index]
                ):
                    values[index] = "<changed-time>"
            if history_index is not None and values[history_index]:
                history = json.loads(values[history_index])
                previous = (
                    json.loads(prior[history_index])
                    if prior and prior[history_index]
                    else []
                )
                for item_index, entry in enumerate(history):
                    if not isinstance(entry, dict) or not entry.get("started_at"):
                        continue
                    previous_started = (
                        previous[item_index].get("started_at")
                        if item_index < len(previous)
                        and isinstance(previous[item_index], dict)
                        else None
                    )
                    if entry["started_at"] != previous_started:
                        entry["started_at"] = "<changed-time>"
                values[history_index] = json.dumps(
                    history, sort_keys=True, separators=(",", ":")
                )
            rows.append(tuple(values))
        normalized[table] = rows
    return normalized


def _files(backend: Any) -> dict[str, bytes]:
    root = backend.instance_dir / "data"
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def snapshot_writer_cleanup_files(backend: Any) -> dict[str, bytes]:
    """Capture fixture-only audio effects between interrupted attempts."""
    return _files(backend)


def assert_writer_cleanup_parity(  # noqa: PLR0912 - action-specific parity branches
    observation: dict[str, Any],
) -> None:
    """Compare writer outcome, all rows, and clone-local file effects."""
    case = observation["case"]
    case_id = case["case_id"]
    pair = observation["pair"]
    expected_success = case.get("expect_success", True)
    assert observation["python_success"] is expected_success, observation[
        "python_error"
    ]
    assert observation["rust_success"] is expected_success, observation["rust_error"]

    if not expected_success:
        assert observation["python_error"] and observation["rust_error"]
        assert observation["python_before"] == observation["python_rows"]
        assert observation["rust_before"] == observation["rust_rows"]
        if case_id == "cleanup_prepare_auto_retry_db_failure_after_unlink":
            # Filesystem unlink is outside the DB transaction: this action
            # removes its processed candidate before the injected SQL failure.
            for backend in (pair.python, pair.rust):
                input_path, processed_path, _ = _paths_for_assertion(backend)
                assert input_path.is_file()
                assert not processed_path.exists()
        else:
            # Missing-record validation must fail before touching fixture files.
            for backend in (pair.python, pair.rust):
                input_path, processed_path, _ = _paths_for_assertion(backend)
                assert input_path.is_file()
                assert processed_path.is_file()
        assert _files(pair.python) == _files(pair.rust)
        return

    if case.get("recovery_after_failure"):
        assert observation["python_successes"] == [False, True]
        assert observation["rust_successes"] == [False, True]
        assert observation["python_errors"][0]
        assert observation["rust_errors"][0]
        assert observation["python_errors"][1] is None
        assert observation["rust_errors"][1] is None
        recovery = observation["recovery_snapshot"]
        assert recovery is not None
        assert recovery["python_rows"] == observation["python_before"]
        assert recovery["rust_rows"] == observation["rust_before"]
        assert recovery["python_files"] == recovery["rust_files"]
        assert "in/cleanup-parity-input.mp3" in recovery["python_files"]
        assert (
            "srv/Parity_feed_one/cleanup-parity-processed.mp3"
            not in recovery["python_files"]
        )
        assert (
            "srv/Parity_feed_one/cleanup-parity-input.mp3"
            not in recovery["python_files"]
        )
        # The failed attempt unlinks every processed candidate before its SQL
        # error; the input file remains for the retried transcription.
        for backend in (pair.python, pair.rust):
            _assert_retry_files(backend, processed_exists=False)
            derived_path = backend.data_srv / "Parity_feed_one" / _INPUT_NAME
            assert not derived_path.exists()

    assert observation["rust_result"] == observation["python_data"], {
        "case_id": case_id,
        "python_result": observation["python_data"],
        "rust_result": observation["rust_result"],
    }
    python_rows = _normalize_times(
        observation["python_rows"], observation["python_before"], pair.python.db_path
    )
    rust_rows = _normalize_times(
        observation["rust_rows"], observation["rust_before"], pair.rust.db_path
    )
    if python_rows != rust_rows:
        raise AssertionError(
            f"database effects differ for {case_id}; "
            "value-free table/column summary: "
            f"{_database_row_diff_summary(python_rows, rust_rows, pair.python.db_path)}"
        )
    assert _files(pair.python) == _files(pair.rust), f"filesystem differs for {case_id}"

    python_post = next(
        row
        for row in _projection_records(observation, "python", "post")
        if row["id"] == _POST_ID
    )
    python_transcripts = _projection_records(
        observation, "python", "transcript_segment"
    )
    python_transcripts_before = _projection_records(
        observation, "python", "transcript_segment", before=True
    )
    python_transcripts_for_post_before = [
        row for row in python_transcripts_before if row["post_id"] == _POST_ID
    ]
    transcript_ids_before = {
        row["id"] for row in python_transcripts_before if row["post_id"] == _POST_ID
    }
    python_identifications = _projection_records(
        observation, "python", "identification"
    )
    python_audio_segments = _projection_records(observation, "python", "audio_segment")
    python_model_calls = [
        row
        for row in _projection_records(observation, "python", "model_call")
        if row["post_id"] == _POST_ID
    ]
    python_jobs = [
        row
        for row in _projection_records(observation, "python", "processing_job")
        if row["post_guid"] == _POST_GUID
    ]

    if case_id == "cleanup_missing_audio_paths_recover_and_requeue":
        assert observation["python_data"] == 1
        assert python_post["unprocessed_audio_path"] is None
        assert python_post["processed_audio_path"].endswith(
            "intentionally-missing-input.mp3"
        )
        latest = max(python_jobs, key=lambda row: row["created_at"])
        assert latest["status"] == "pending"
        assert latest["current_step"] == 0
        assert latest["step_name"] == "Not started"
    elif case_id == "cleanup_missing_audio_paths_legacy_title_candidate":
        assert observation["python_data"] == 1
        assert python_post["unprocessed_audio_path"] is None
        assert python_post["processed_audio_path"].endswith(
            "Parity feed one/Integral duration episode.mp3"
        )
        latest = max(python_jobs, key=lambda row: row["created_at"])
        assert latest["status"] == "pending"
    elif case_id == "cleanup_missing_audio_paths_preserves_valid_paths":
        assert observation["python_data"] == 0
        assert python_post["unprocessed_audio_path"].endswith(_INPUT_NAME)
        assert python_post["processed_audio_path"].endswith(
            "cleanup-parity-processed.mp3"
        )
        latest = max(python_jobs, key=lambda row: row["created_at"])
        assert latest["status"] == "failed"
    elif case_id == "cleanup_missing_audio_paths_preserves_active_job":
        assert observation["python_data"] == 1
        assert python_post["unprocessed_audio_path"] is None
        assert python_post["processed_audio_path"] is None
        active = next(row for row in python_jobs if row["id"].endswith("0403"))
        assert active["status"] == "running"
        assert active["current_step"] == 3
        assert active["step_name"] == "Segmenting"
        assert active["progress_percentage"] == 42.5
    elif case_id == (
        "cleanup_missing_audio_paths_ignores_empty_and_directory_candidates"
    ):
        assert observation["python_data"] == 1
        assert python_post["unprocessed_audio_path"] is None
        assert python_post["processed_audio_path"] is None
        for backend in (pair.python, pair.rust):
            assert (
                backend.data_srv / "Parity_feed_one" / "cleanup-parity-processed.mp3"
            ).is_file()
            assert (
                backend.data_srv / "Parity_feed_one" / "intentionally-missing-input.mp3"
            ).is_dir()
    elif case_id == "cleanup_missing_audio_paths_ignores_unwhitelisted":
        assert observation["python_data"] == 0
        assert python_post["whitelisted"] in (False, 0)
        assert python_post["unprocessed_audio_path"].endswith(
            "intentionally-missing-input.mp3"
        )
        assert python_post["processed_audio_path"].endswith(
            "intentionally-missing-processed.mp3"
        )
    elif case_id == "cleanup_clear_all_processing_data":
        assert observation["python_data"] == {"post_id": _POST_ID}
        assert len(transcript_ids_before) == 501
        assert not [row for row in python_transcripts if row["post_id"] == _POST_ID]
        assert (
            not {row["transcript_segment_id"] for row in python_identifications}
            & transcript_ids_before
        )
        assert not [row for row in python_audio_segments if row["post_id"] == _POST_ID]
        assert not python_model_calls
        assert not python_jobs
        assert python_post["duration"] is None
        assert python_post["transcript_word_timestamps"] == "null"
    elif case_id == "cleanup_clear_outputs_keep_transcript":
        assert observation["python_data"] == {"post_id": _POST_ID}
        assert len(transcript_ids_before) == 501
        assert [row for row in python_transcripts if row["post_id"] == _POST_ID] == (
            python_transcripts_for_post_before
        )
        assert (
            not {row["transcript_segment_id"] for row in python_identifications}
            & transcript_ids_before
        )
        assert not [row for row in python_audio_segments if row["post_id"] == _POST_ID]
        assert {row["id"] for row in python_model_calls} == {601, 603, 604}
        assert not python_jobs
        assert python_post["duration"] is None
        assert python_post["transcript_word_timestamps"] is not None
    elif case_id == "cleanup_prepare_auto_retry_preserves_input":
        assert observation["python_data"] == {"post_id": _POST_ID}
        assert python_post["processed_audio_path"] is None
        assert python_post["unprocessed_audio_path"].endswith(_INPUT_NAME)
        assert len(transcript_ids_before) == 501
        assert [row for row in python_transcripts if row["post_id"] == _POST_ID] == (
            python_transcripts_for_post_before
        )
        assert {row["id"] for row in python_model_calls} == {601, 603, 604}
        assert (
            not {row["transcript_segment_id"] for row in python_identifications}
            & transcript_ids_before
        )
        assert len(python_jobs) == 2
        _assert_retry_files(pair.python, processed_exists=False)
        _assert_retry_files(pair.rust, processed_exists=False)
        for backend in (pair.python, pair.rust):
            assert not (backend.data_srv / "Parity_feed_one" / _INPUT_NAME).exists()
    elif case_id == "cleanup_prepare_auto_retry_db_failure_after_unlink":
        assert observation["python_data"] == {"post_id": _POST_ID}
        assert python_post["processed_audio_path"] is None
        assert python_post["unprocessed_audio_path"].endswith(_INPUT_NAME)
        assert [row for row in python_transcripts if row["post_id"] == _POST_ID] == (
            python_transcripts_for_post_before
        )
        assert {row["id"] for row in python_model_calls} == {601, 603, 604}
        assert len(python_jobs) == 2
        _assert_retry_files(pair.python, processed_exists=False)
        _assert_retry_files(pair.rust, processed_exists=False)
    elif case_id == "cleanup_prepare_auto_retry_skips_directories":
        assert observation["python_data"] == {"post_id": _POST_ID}
        for backend in (pair.python, pair.rust):
            assert (backend.data_in / _INPUT_NAME).is_file()
            assert (
                backend.data_srv / "Parity_feed_one" / "cleanup-parity-processed.mp3"
            ).is_dir()
            assert (backend.data_srv / "Parity_feed_one" / _INPUT_NAME).is_dir()
    elif case_id == "cleanup_processed_post_clears_db_state":
        assert observation["python_data"] == {"post_id": _POST_ID}
        assert python_post["whitelisted"] in (False, 0)
        assert python_post["duration"] is None
        assert not [row for row in python_transcripts if row["post_id"] == _POST_ID]
        assert (
            not {row["transcript_segment_id"] for row in python_identifications}
            & transcript_ids_before
        )
        assert not [row for row in python_audio_segments if row["post_id"] == _POST_ID]
        assert not python_model_calls
        assert not python_jobs
        singleton = next(
            row
            for row in _projection_records(observation, "python", "jobs_manager_run")
            if row["id"] == "jobs-manager-singleton"
        )
        assert singleton["status"] == "running"
        assert singleton["total_jobs"] == 1
        assert singleton["queued_jobs"] == 0
        assert singleton["running_jobs"] == 1
        assert singleton["completed_jobs"] == 0
        assert singleton["failed_jobs"] == 0
    elif case_id in {
        "cleanup_processed_post_files_only",
        "cleanup_processed_post_files_only_missing_files",
    }:
        assert observation["python_data"] == {"post_id": _POST_ID}
        assert python_post["whitelisted"] in (False, 0)
        assert python_post["unprocessed_audio_path"] is None
        assert python_post["processed_audio_path"] is None
        assert python_post["duration"] == 123.5
        assert [row for row in python_transcripts if row["post_id"] == _POST_ID]
        assert python_model_calls
        assert python_jobs
        _assert_files_only_deleted(pair.python)
        _assert_files_only_deleted(pair.rust)
    elif case_id == "cleanup_processed_post_files_only_skips_directories":
        assert observation["python_data"] == {"post_id": _POST_ID}
        assert python_post["whitelisted"] in (False, 0)
        assert python_post["unprocessed_audio_path"] is None
        assert python_post["processed_audio_path"] is None
        assert python_post["duration"] == 123.5
        for backend in (pair.python, pair.rust):
            assert (backend.data_in / _INPUT_NAME).is_dir()
            assert not (backend.data_srv / "missing-processed-audio.mp3").exists()


def _paths_for_assertion(backend: Any) -> tuple[Path, Path, Path]:
    input_path = backend.data_in / _INPUT_NAME
    processed_path = (
        backend.data_srv / "Parity_feed_one" / "cleanup-parity-processed.mp3"
    )
    return input_path, processed_path, backend.data_srv


def _assert_retry_files(backend: Any, *, processed_exists: bool) -> None:
    input_path, processed_path, _ = _paths_for_assertion(backend)
    assert input_path.is_file()
    assert processed_path.exists() is processed_exists


def _assert_files_only_deleted(backend: Any) -> None:
    input_path, processed_path, _ = _paths_for_assertion(backend)
    assert not input_path.exists()
    assert not processed_path.exists()
