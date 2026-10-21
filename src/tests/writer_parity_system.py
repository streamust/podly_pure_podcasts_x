"""Differential parity cases for writer system and configuration actions."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from app.writer.protocol import WriteCommand, WriteCommandType
from tests.writer_parity_fixtures import WriterParityPair

_SINGLETON_RUN_ID = "jobs-manager-singleton"
_VARIABLE_TIME = "<nondeterministic:timestamp>"

WRITER_DIFFERENTIAL_CASES = (
    {
        "case_id": "system_ensure_active_run_create",
        "operation": "action",
        "action": "ensure_active_run",
        "owner_group": "system",
        "owner": "ensure_active_run",
        "source": "src/app/writer/actions/system.py:ensure_active_run_action",
        "semantics": "ensure_active_run",
    },
    {
        "case_id": "system_ensure_active_run_update_existing",
        "operation": "action",
        "action": "ensure_active_run",
        "owner_group": "system",
        "owner": "ensure_active_run",
        "source": "src/app/writer/actions/system.py:ensure_active_run_action",
        "semantics": "ensure_active_run_existing",
    },
    {
        "case_id": "system_update_discord_settings_create",
        "operation": "action",
        "action": "update_discord_settings",
        "owner_group": "system",
        "owner": "update_discord_settings",
        "source": "src/app/writer/actions/system.py:update_discord_settings_action",
        "semantics": "discord_settings",
    },
    {
        "case_id": "system_update_discord_settings_partial_null",
        "operation": "action",
        "action": "update_discord_settings",
        "owner_group": "system",
        "owner": "update_discord_settings",
        "source": "src/app/writer/actions/system.py:update_discord_settings_action",
        "semantics": "discord_settings_partial",
    },
    {
        "case_id": "system_update_discord_settings_invalid_type_rollback",
        "operation": "action",
        "action": "update_discord_settings",
        "owner_group": "system",
        "owner": "update_discord_settings",
        "source": "src/app/writer/actions/system.py:update_discord_settings_action",
        "semantics": "discord_settings_invalid",
        "expect_success": False,
    },
    {
        "case_id": "system_update_combined_config_multiple_sections",
        "operation": "action",
        "action": "update_combined_config",
        "owner_group": "system",
        "owner": "update_combined_config",
        "source": "src/app/writer/actions/system.py:update_combined_config_action",
        "semantics": "combined_config",
    },
    {
        "case_id": "system_update_combined_config_keeps_prior_section_commit",
        "operation": "action",
        "action": "update_combined_config",
        "owner_group": "system",
        "owner": "update_combined_config",
        "source": "src/app/writer/actions/system.py:update_combined_config_action",
        "semantics": "combined_config_partial_commit",
        "expect_success": False,
    },
    {
        "case_id": "system_update_combined_config_empty_sections_noop",
        "operation": "action",
        "action": "update_combined_config",
        "owner_group": "system",
        "owner": "update_combined_config",
        "source": "src/app/writer/actions/system.py:update_combined_config_action",
        "semantics": "combined_config_noop",
    },
    {
        "case_id": "system_update_combined_config_invalid_payload",
        "operation": "action",
        "action": "update_combined_config",
        "owner_group": "system",
        "owner": "update_combined_config",
        "source": "src/app/writer/actions/system.py:update_combined_config_action",
        "semantics": "combined_config_invalid",
        "expect_success": False,
    },
)


def build_writer_system_case(
    case_id: str, pair: WriterParityPair
) -> tuple[dict[str, Any], WriteCommand]:
    case = next(
        item for item in WRITER_DIFFERENTIAL_CASES if item["case_id"] == case_id
    )
    _seed_case_preconditions(pair, case_id)
    params = _params_for_case(case_id)
    action = case["action"]
    operation = {"operation": "action", "action": action, "params": params}
    command = WriteCommand(
        id=f"parity-{case_id}",
        type=WriteCommandType.ACTION,
        model=None,
        data={"action": action, "params": params},
    )
    return operation, command


def _seed_case_preconditions(pair: WriterParityPair, case_id: str) -> None:
    for backend in (pair.python, pair.rust):
        with sqlite3.connect(backend.db_path) as connection:
            if case_id == "system_ensure_active_run_update_existing":
                connection.execute(
                    "INSERT OR REPLACE INTO jobs_manager_run "
                    "(id,status,trigger,started_at,completed_at,total_jobs,queued_jobs,"
                    "running_jobs,completed_jobs,failed_jobs,skipped_jobs,context_json,"
                    "counters_reset_at,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        _SINGLETON_RUN_ID,
                        "completed",
                        "previous-trigger",
                        None,
                        "2026-01-02 04:05:06.000000",
                        17,
                        2,
                        3,
                        7,
                        4,
                        1,
                        '{"prior":"preserved"}',
                        None,
                        "2026-01-02 03:04:05.000000",
                        "2026-01-02 03:04:05.000000",
                    ),
                )
            elif case_id in {
                "system_update_discord_settings_partial_null",
                "system_update_discord_settings_invalid_type_rollback",
            }:
                connection.execute(
                    "INSERT OR REPLACE INTO discord_settings "
                    "(id,client_id,client_secret,redirect_uri,guild_ids,allow_registration,"
                    "created_at,updated_at) VALUES (1,?,?,?,?,?,?,?)",
                    (
                        "synthetic-existing-client",
                        "synthetic-existing-secret",
                        "https://old.example.invalid/callback",
                        "old-guild",
                        1,
                        "2026-01-02 03:04:05.000000",
                        "2026-01-02 03:04:05.000000",
                    ),
                )
            elif case_id in {
                "system_update_combined_config_multiple_sections",
                "system_update_combined_config_empty_sections_noop",
            }:
                connection.execute(
                    "UPDATE llm_settings SET llm_api_key=? WHERE id=1",
                    ("synthetic-existing-api-key",),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO notification_settings "
                    "(id,enabled,notify_on_failure,notify_on_success,"
                    "notify_on_rust_fallback,include_llm_explanation,created_at,updated_at) "
                    "VALUES (1,0,1,0,0,0,?,?)",
                    ("2026-01-01 00:00:00.000000", "2026-01-01 00:00:00.000000"),
                )


def _params_for_case(case_id: str) -> dict[str, Any]:
    factories = {
        "system_ensure_active_run_create": lambda: {
            "trigger": "parity-system-trigger",
            "context": {"source": "synthetic", "attempt": 7, "nested": {"ok": True}},
        },
        "system_ensure_active_run_update_existing": lambda: {
            "trigger": "parity-existing-run-trigger",
            "context": {"source": "synthetic-update", "attempt": 8},
        },
        "system_update_discord_settings_create": lambda: {
            "client_id": "synthetic-discord-client",
            "client_secret": "synthetic-discord-secret",
            "redirect_uri": "https://example.invalid/synthetic-callback",
            "guild_ids": "synthetic-guild-id",
            "allow_registration": False,
        },
        "system_update_discord_settings_partial_null": lambda: {
            "client_secret": None,
            "allow_registration": False,
        },
        "system_update_discord_settings_invalid_type_rollback": lambda: {
            "client_id": "synthetic-must-roll-back",
            "allow_registration": "not-a-boolean",
        },
        "system_update_combined_config_multiple_sections": lambda: {
            "payload": {
                "llm": {"llm_model": "synthetic-parity-model", "llm_api_key": ""},
                "whisper": {"whisper_type": "test"},
                "processing": {"num_segments_to_input_to_prompt": 73},
                "output": {
                    "fade_ms": 2222,
                    "min_confidence": 0.731234,
                    "auto_retry_zero_ads_on_parse_error": True,
                },
                "app": {"enable_public_landing_page": True},
                "notifications": {
                    "enabled": True,
                    "apprise_urls": [" synthetic://notify "],
                },
            }
        },
        "system_update_combined_config_keeps_prior_section_commit": lambda: {
            "payload": {
                "llm": {"llm_model": "synthetic-first-section-committed"},
                "output": {"min_confidence": {"invalid": "synthetic"}},
            }
        },
        "system_update_combined_config_empty_sections_noop": lambda: {
            "payload": {
                "llm": None,
                "whisper": {},
                "processing": None,
                "output": {},
                "app": None,
                "notifications": {},
            }
        },
        "system_update_combined_config_invalid_payload": lambda: {
            "payload": None,
        },
    }
    try:
        return factories[case_id]()
    except KeyError as exc:
        raise AssertionError(f"No system payload factory for {case_id}") from exc


def _table_columns(db_path: Path) -> dict[str, list[str]]:
    with sqlite3.connect(db_path) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {
            table: [
                row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
            ]
            for table in tables
        }


def _timestamp_sentinel(value: Any) -> str:
    assert isinstance(value, str) and value
    datetime.fromisoformat(value)
    return _VARIABLE_TIME


def _normalize_projection(
    projection: dict[str, list[tuple[Any, ...]]], columns: dict[str, list[str]]
) -> dict[str, list[tuple[Any, ...]]]:
    normalized: dict[str, list[tuple[Any, ...]]] = {}
    for table, rows in projection.items():
        names = columns[table]
        normalized_rows = []
        for row in rows:
            record = dict(zip(names, row, strict=True))
            if table == "jobs_manager_run" and record.get("id") == _SINGLETON_RUN_ID:
                for field in (
                    "started_at",
                    "completed_at",
                    "counters_reset_at",
                    "created_at",
                    "updated_at",
                ):
                    if record.get(field) is not None:
                        record[field] = _timestamp_sentinel(record[field])
                context = json.loads(record["context_json"])
                if "last_trigger_at" in context:
                    context["last_trigger_at"] = _VARIABLE_TIME
                record["context_json"] = json.dumps(
                    context, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
            elif table == "discord_settings" and record.get("id") == 1:
                for field in ("created_at", "updated_at"):
                    if record.get(field) is not None:
                        record[field] = _timestamp_sentinel(record[field])
            normalized_rows.append(tuple(record[name] for name in names))
        normalized[table] = sorted(normalized_rows, key=repr)
    return normalized


def assert_writer_system_parity(observation: dict[str, Any]) -> None:
    case = observation["case"]
    case_id = case["case_id"]
    pair = observation["pair"]
    semantics = case["semantics"]
    expected_success = case.get("expect_success", True)

    assert observation["python_success"] is expected_success, observation[
        "python_error"
    ]
    assert observation["rust_success"] is expected_success, observation["rust_error"]

    columns = _table_columns(pair.python.db_path)
    python_rows = _normalize_projection(observation["python_rows"], columns)
    rust_rows = _normalize_projection(observation["rust_rows"], columns)
    if semantics == "combined_config_partial_commit":
        assert observation["python_error"]
        assert observation["rust_error"]
        assert python_rows == rust_rows, f"system database state differs for {case_id}"
        for projection in (python_rows, rust_rows):
            llm = dict(
                zip(columns["llm_settings"], projection["llm_settings"][0], strict=True)
            )
            output = dict(
                zip(
                    columns["output_settings"],
                    projection["output_settings"][0],
                    strict=True,
                )
            )
            assert llm["llm_model"] == "synthetic-first-section-committed"
            assert output["min_confidence"] == 0.8
        before_columns = columns
        before_python = _normalize_projection(
            observation["python_before"], before_columns
        )
        before_llm = dict(
            zip(columns["llm_settings"], before_python["llm_settings"][0], strict=True)
        )
        assert before_llm["llm_model"] != "synthetic-first-section-committed"
        return

    if semantics == "combined_config_invalid":
        assert observation["python_error"]
        assert observation["rust_error"]
        assert observation["python_rows"] == observation["python_before"]
        assert observation["rust_rows"] == observation["rust_before"]
        assert observation["python_rows"] == observation["rust_rows"]
        return

    if semantics == "combined_config_noop":
        assert observation["python_data"] == observation["rust_result"]
        assert observation["python_rows"] == observation["python_before"]
        assert observation["rust_rows"] == observation["rust_before"]
        assert observation["python_rows"] == observation["rust_rows"]
        assert set(observation["python_data"]) == {
            "llm",
            "whisper",
            "processing",
            "output",
            "app",
            "notifications",
        }
        return

    if semantics == "discord_settings_invalid":
        assert observation["python_error"]
        assert observation["rust_error"]
        assert observation["python_rows"] == observation["python_before"]
        assert observation["rust_rows"] == observation["rust_before"]
        assert observation["python_rows"] == observation["rust_rows"]
        return

    assert observation["python_data"] == observation["rust_result"], {
        "case_id": case_id,
        "python_result": observation["python_data"],
        "rust_result": observation["rust_result"],
    }
    assert python_rows == rust_rows, f"system database state differs for {case_id}"

    if semantics == "ensure_active_run":
        with sqlite3.connect(pair.python.db_path) as connection:
            row = connection.execute(
                "SELECT status,trigger,context_json,started_at,created_at,updated_at "
                "FROM jobs_manager_run WHERE id=?",
                (_SINGLETON_RUN_ID,),
            ).fetchone()
        assert row is not None
        assert row[0:2] == ("running", "parity-system-trigger")
        context = json.loads(row[2])
        assert context["source"] == "synthetic"
        assert context["attempt"] == 7
        assert context["nested"] == {"ok": True}
        assert context["last_trigger"] == "parity-system-trigger"
        datetime.fromisoformat(context["last_trigger_at"])
        for timestamp in row[3:]:
            datetime.fromisoformat(timestamp)
    elif semantics == "ensure_active_run_existing":
        with sqlite3.connect(pair.python.db_path) as connection:
            row = connection.execute(
                "SELECT status,trigger,started_at,completed_at,total_jobs,queued_jobs,"
                "running_jobs,completed_jobs,failed_jobs,skipped_jobs,context_json,"
                "counters_reset_at,created_at,updated_at FROM jobs_manager_run WHERE id=?",
                (_SINGLETON_RUN_ID,),
            ).fetchone()
        assert row is not None
        assert row[0:2] == ("completed", "parity-existing-run-trigger")
        assert row[2] is not None
        assert row[3] == "2026-01-02 04:05:06.000000"
        assert row[4:10] == (17, 2, 3, 7, 4, 1)
        context = json.loads(row[10])
        assert context["source"] == "synthetic-update"
        assert context["attempt"] == 8
        assert context["last_trigger"] == "parity-existing-run-trigger"
        datetime.fromisoformat(context["last_trigger_at"])
        assert row[11] is not None
        assert row[12] == "2026-01-02 03:04:05.000000"
        datetime.fromisoformat(row[13])
    elif semantics == "discord_settings":
        with sqlite3.connect(pair.python.db_path) as connection:
            row = connection.execute(
                "SELECT client_id,client_secret,redirect_uri,guild_ids,allow_registration,"
                "created_at,updated_at FROM discord_settings WHERE id=1"
            ).fetchone()
        assert row is not None
        assert row[:5] == (
            "synthetic-discord-client",
            "synthetic-discord-secret",
            "https://example.invalid/synthetic-callback",
            "synthetic-guild-id",
            0,
        )
        datetime.fromisoformat(row[5])
        datetime.fromisoformat(row[6])
    elif semantics == "discord_settings_partial":
        with sqlite3.connect(pair.python.db_path) as connection:
            row = connection.execute(
                "SELECT client_id,client_secret,redirect_uri,guild_ids,allow_registration,"
                "created_at,updated_at FROM discord_settings WHERE id=1"
            ).fetchone()
        assert row is not None
        assert row[:5] == (
            "synthetic-existing-client",
            None,
            "https://old.example.invalid/callback",
            "old-guild",
            0,
        )
        assert row[5] == "2026-01-02 03:04:05.000000"
        datetime.fromisoformat(row[6])
    elif semantics == "combined_config":
        result = observation["python_data"]
        assert set(result) == {
            "llm",
            "whisper",
            "processing",
            "output",
            "app",
            "notifications",
        }
        assert result["llm"]["llm_model"] == "synthetic-parity-model"
        assert result["whisper"]["whisper_type"] == "test"
        assert result["processing"]["num_segments_to_input_to_prompt"] == 73
        assert result["output"]["fade_ms"] == 2222
        assert result["app"]["enable_public_landing_page"] is True
        assert result["notifications"]["apprise_urls"] == ["synthetic://notify"]
        with sqlite3.connect(pair.python.db_path) as connection:
            llm = connection.execute(
                "SELECT llm_api_key,llm_model FROM llm_settings WHERE id=1"
            ).fetchone()
            whisper = connection.execute(
                "SELECT whisper_type FROM whisper_settings WHERE id=1"
            ).fetchone()
            processing = connection.execute(
                "SELECT num_segments_to_input_to_prompt FROM processing_settings WHERE id=1"
            ).fetchone()
            output = connection.execute(
                "SELECT fade_ms,min_confidence,auto_retry_zero_ads_on_parse_error "
                "FROM output_settings WHERE id=1"
            ).fetchone()
            app = connection.execute(
                "SELECT enable_public_landing_page FROM app_settings WHERE id=1"
            ).fetchone()
            notifications = connection.execute(
                "SELECT enabled,apprise_urls FROM notification_settings WHERE id=1"
            ).fetchone()
        assert llm == ("synthetic-existing-api-key", "synthetic-parity-model")
        assert whisper == ("test",)
        assert processing == (73,)
        assert output == (2222, 0.731234, 1)
        assert app == (1,)
        assert notifications == (1, "synthetic://notify")
