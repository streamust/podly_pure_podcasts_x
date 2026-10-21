"""Bounded HTTP-level differential checks for the Python and Rust writers.

Each case starts from independent fixture clones, runs the same operation on
both writers, then compares the returned result and every persisted table row.
The case list is an explicit subset; it is not an action-registry completeness
claim.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from flask import Flask
from sqlalchemy import event

from app.extensions import db
from app.writer.executor import CommandExecutor
from app.writer.protocol import WriteCommand, WriteCommandType
from tests.writer_parity_cleanup import (
    WRITER_DIFFERENTIAL_CASES as CLEANUP_WRITER_DIFFERENTIAL_CASES,
)
from tests.writer_parity_cleanup import (
    assert_writer_cleanup_parity,
    build_writer_cleanup_case,
    release_writer_cleanup_interruption,
    snapshot_writer_cleanup_files,
)
from tests.writer_parity_feeds import (
    WRITER_DIFFERENTIAL_CASES as FEED_WRITER_DIFFERENTIAL_CASES,
)
from tests.writer_parity_feeds import (
    assert_writer_feed_parity,
    build_writer_feed_case,
    prepare_writer_feed_case,
)
from tests.writer_parity_fixtures import (
    BASE_WRITER_DIFFERENTIAL_CASES,
    WriterParityBackend,
    WriterParityPair,
)
from tests.writer_parity_jobs import (
    WRITER_DIFFERENTIAL_CASES as JOB_WRITER_DIFFERENTIAL_CASES,
)
from tests.writer_parity_jobs import (
    assert_writer_job_parity,
    build_writer_job_case,
)
from tests.writer_parity_processor import (
    WRITER_DIFFERENTIAL_CASES as PROCESSOR_WRITER_DIFFERENTIAL_CASES,
)
from tests.writer_parity_processor import (
    _semantic_projection as _semantic_processor_projection,
)
from tests.writer_parity_processor import (
    _table_columns as _processor_table_columns,
)
from tests.writer_parity_processor import (
    _word_timestamp_payload as _processor_word_timestamp_payload,
)
from tests.writer_parity_processor import (
    assert_writer_processor_parity,
    build_writer_processor_case,
    build_writer_processor_replacement_workflow,
)
from tests.writer_parity_system import (
    WRITER_DIFFERENTIAL_CASES as SYSTEM_WRITER_DIFFERENTIAL_CASES,
)
from tests.writer_parity_system import (
    assert_writer_system_parity,
    build_writer_system_case,
)
from tests.writer_parity_transactions import (
    WRITER_DIFFERENTIAL_CASES as TRANSACTION_WRITER_DIFFERENTIAL_CASES,
)
from tests.writer_parity_transactions import (
    assert_writer_transaction_parity,
    build_writer_transaction_case,
)
from tests.writer_parity_users import (
    WRITER_DIFFERENTIAL_CASES as USER_WRITER_DIFFERENTIAL_CASES,
)
from tests.writer_parity_users import (
    assert_writer_user_parity,
    build_writer_user_case,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
AUTH_KEY = "synthetic-writer-parity-auth-key"
WRITER_DIFFERENTIAL_CASES = (
    *BASE_WRITER_DIFFERENTIAL_CASES,
    *USER_WRITER_DIFFERENTIAL_CASES,
    *FEED_WRITER_DIFFERENTIAL_CASES,
    *JOB_WRITER_DIFFERENTIAL_CASES,
    *PROCESSOR_WRITER_DIFFERENTIAL_CASES,
    *SYSTEM_WRITER_DIFFERENTIAL_CASES,
    *CLEANUP_WRITER_DIFFERENTIAL_CASES,
    *TRANSACTION_WRITER_DIFFERENTIAL_CASES,
)


def _writer_wire_value(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"Unsupported writer parity JSON value: {type(value).__name__}")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _rust_writer_command() -> list[str]:
    configured = os.environ.get("PODLY_RUST_WRITER_BIN")
    if configured:
        return [configured]
    mise = shutil.which("mise")
    if mise is None:
        pytest.fail(
            "Rust writer parity requires `mise` or PODLY_RUST_WRITER_BIN; "
            "the Rust operation was not executed"
        )
    return [
        mise,
        "exec",
        "--",
        "cargo",
        "run",
        "--quiet",
        "--manifest-path",
        str(REPO_ROOT / "rust" / "Cargo.toml"),
        "--bin",
        "podly_writer",
        "--",
    ]


class RustWriterParityServer:
    def __init__(
        self,
        db_path: Path,
        tmp_path: Path,
        env_overrides: dict[str, str] | None = None,
    ):
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.log_path = tmp_path / "rust-writer-parity.log"
        self._log = self.log_path.open("wb")
        env = os.environ.copy()
        env["PODLY_IPC_AUTHKEY"] = AUTH_KEY
        env.update(env_overrides or {})
        command = [
            *_rust_writer_command(),
            "--db",
            str(db_path),
            "--port",
            str(self.port),
            "--enable-test-actions",
        ]
        if env.get("PODLY_TEST_DEFERRED_FOREIGN_KEYS") == "1":
            command.append("--enable-test-foreign-keys")
        self.process = subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=self._log,
        )
        try:
            self._wait_ready()
        except (
            RuntimeError,
            TimeoutError,
            urllib.error.URLError,
            OSError,
            json.JSONDecodeError,
        ) as error:
            self.close()
            detail = self.log_path.read_text(errors="replace")[-4000:]
            raise AssertionError(f"Rust writer failed to start:\n{detail}") from error

    def _request(self, endpoint: str, payload: dict[str, Any] | None = None) -> dict:
        headers = {
            "Authorization": "PodlyWriter "
            + base64.urlsafe_b64encode(AUTH_KEY.encode()).decode().rstrip("="),
        }
        data = None
        method = "GET"
        if payload is not None:
            method = "POST"
            data = json.dumps(payload, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + endpoint,
            data=data,
            headers=headers,
            method=method,
        )
        with urllib.request.urlopen(request, timeout=40) as response:
            return json.loads(response.read())

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + 180
        last_error: str | None = None
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"Rust writer exited with {self.process.returncode}")
            try:
                readiness = self._request("/v1/ready")
                if readiness.get("ready") is True:
                    return
                last_error = f"Rust writer not ready: {readiness}"
            except (urllib.error.URLError, OSError, json.JSONDecodeError) as error:
                last_error = str(error)
            time.sleep(0.1)
        raise TimeoutError(f"Rust writer readiness timed out: {last_error}")

    def execute(self, operation: dict[str, Any]) -> Any:
        outcome = self.execute_outcome(operation)
        assert outcome["success"] is True, outcome
        return outcome.get("result")

    def execute_outcome(self, operation: dict[str, Any]) -> dict[str, Any]:
        command_id = str(uuid.uuid4())
        payload = {
            "version": 1,
            "command_id": command_id,
            "operation": operation["operation"],
            "wait": True,
            **{key: value for key, value in operation.items() if key != "operation"},
        }
        response = self._request("/v1/commands", payload)
        assert response["state"] == "completed", response
        assert response["command_id"] == command_id, response
        return response

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        self._log.close()


def writer_database_projection(
    db_path: Path, instance_root: Path
) -> dict[str, list[tuple[Any, ...]]]:
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        projection: dict[str, list[tuple[Any, ...]]] = {}
        for table in tables:
            columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
            quoted = ",".join(f'"{column}"' for column in columns)
            rows = conn.execute(f'SELECT {quoted} FROM "{table}"').fetchall()
            normalized = []
            for row in rows:
                values = []
                for value in row:
                    normalized_value = value
                    if isinstance(normalized_value, str):
                        try:
                            normalized_value = str(
                                Path(normalized_value).relative_to(instance_root)
                            )
                        except ValueError:
                            pass
                    values.append(normalized_value)
                normalized.append(tuple(values))
            projection[table] = sorted(normalized, key=repr)
    return projection


def execute_python_parity_command(
    backend: WriterParityBackend,
    command: WriteCommand,
    env_overrides: dict[str, str] | None = None,
) -> tuple[bool, Any, str | None]:
    """Execute one real legacy writer command against a fixture clone."""
    with patch.dict(os.environ, env_overrides or {}):
        app = Flask(f"python-writer-parity-{command.id}")
        app.config.update(
            SQLALCHEMY_DATABASE_URI=backend.database_uri,
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(app)
        executor = CommandExecutor(app)
        # process_command owns its per-command app context and transaction;
        # keep an outer context only for scoped-session cleanup and disposal.
        with app.app_context():
            if os.environ.get("PODLY_TEST_DEFERRED_FOREIGN_KEYS") == "1":
                event.listen(db.engine, "connect", _enable_sqlite_foreign_keys)
            try:
                result = executor.process_command(command)
                return result.success, result.data, result.error
            finally:
                db.session.remove()
                db.engine.dispose()


def _enable_sqlite_foreign_keys(connection: Any, _connection_record: Any) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def _run_processor_replacement_workflow(
    case: dict[str, Any], pair: WriterParityPair, tmp_path: Path
) -> None:
    case_id = case["case_id"]
    workflow = build_writer_processor_replacement_workflow(case_id, pair)
    python_initial = writer_database_projection(
        pair.python.db_path, pair.python.instance_dir
    )
    rust_initial = writer_database_projection(pair.rust.db_path, pair.rust.instance_dir)
    assert python_initial == rust_initial

    rust = RustWriterParityServer(pair.rust.db_path, tmp_path)
    python_outcomes: list[tuple[bool, Any, str | None]] = []
    rust_outcomes: list[dict[str, Any]] = []
    python_states = []
    try:
        for operation, command in workflow:
            rust_operation = json.loads(json.dumps(operation))
            python_outcome = execute_python_parity_command(pair.python, command)
            rust_outcome = rust.execute_outcome(rust_operation)
            python_outcomes.append(python_outcome)
            rust_outcomes.append(rust_outcome)
            python_success, python_result, python_error = python_outcome
            assert python_success is rust_outcome["success"], {
                "case_id": case_id,
                "action": command.data["action"],
                "python_error": python_error,
                "rust_error": rust_outcome.get("error"),
            }
            if python_success:
                assert python_result == rust_outcome.get("result"), case_id
            else:
                assert python_error
                assert rust_outcome.get("error")

            python_state = writer_database_projection(
                pair.python.db_path, pair.python.instance_dir
            )
            rust_state = writer_database_projection(
                pair.rust.db_path, pair.rust.instance_dir
            )
            python_semantic_state = _semantic_processor_projection(
                python_state, _processor_table_columns(pair.python.db_path)
            )
            rust_semantic_state = _semantic_processor_projection(
                rust_state, _processor_table_columns(pair.rust.db_path)
            )
            assert python_semantic_state == rust_semantic_state, (
                f"{case_id} diverged after {command.data['action']}"
            )
            python_states.append(python_semantic_state)
            if case["workflow"] == "replacement_success":
                post_id = pair.manifest.post_ids[0]
                with sqlite3.connect(pair.python.db_path) as connection:
                    transcript_json = connection.execute(
                        "SELECT transcript_word_timestamps FROM post WHERE id=?",
                        (post_id,),
                    ).fetchone()[0]
                    segment_rows = connection.execute(
                        "SELECT sequence_num,start_time,end_time,text,speaker_label "
                        "FROM transcript_segment WHERE post_id=? ORDER BY sequence_num",
                        (post_id,),
                    ).fetchall()
                    identification_count = connection.execute(
                        "SELECT COUNT(*) FROM identification "
                        "WHERE transcript_segment_id=701"
                    ).fetchone()[0]
                    model_call = connection.execute(
                        "SELECT status,first_segment_sequence_num,last_segment_sequence_num,"
                        "retry_attempts,response,error_message "
                        "FROM model_call WHERE id=601"
                    ).fetchone()
                if len(python_outcomes) == 1:
                    assert json.loads(transcript_json) is None
                    assert segment_rows == []
                    assert identification_count == 0
                    assert model_call == ("pending", 0, -1, 0, None, None)
                elif len(python_outcomes) == 2:
                    assert json.loads(transcript_json) is None
                    assert segment_rows == [
                        (
                            8,
                            0.125000000123,
                            0.987654321987,
                            "replacement segment café",
                            "Speaker A",
                        ),
                        (
                            9,
                            1.234567890123,
                            2.345678901234,
                            "replacement segment 東京",
                            None,
                        ),
                    ]
                    assert model_call == ("pending", 0, -1, 0, None, None)
                else:
                    assert len(python_outcomes) == 3
                    assert json.loads(transcript_json) == (
                        _processor_word_timestamp_payload()
                    )
    finally:
        rust.close()

    post_id = pair.manifest.post_ids[0]
    if case["workflow"] == "replacement_insert_failure":
        assert [outcome[0] for outcome in python_outcomes] == [True, False]
        assert [outcome["success"] for outcome in rust_outcomes] == [True, False]
        # The earlier start RPC committed. The later failed insert RPC rolled
        # back only its own first row, so the state remains exactly post-start.
        assert python_states[0] == python_states[1]
        for backend in (pair.python, pair.rust):
            with sqlite3.connect(backend.db_path) as connection:
                assert (
                    connection.execute(
                        "SELECT COUNT(*) FROM transcript_segment WHERE post_id=?",
                        (post_id,),
                    ).fetchone()[0]
                    == 0
                )
                assert (
                    connection.execute(
                        "SELECT COUNT(*) FROM identification WHERE transcript_segment_id=701"
                    ).fetchone()[0]
                    == 0
                )
                assert connection.execute(
                    "SELECT status,retry_attempts,response FROM model_call WHERE id=601"
                ).fetchone() == ("pending", 0, None)
                post_word_timestamps = connection.execute(
                    "SELECT transcript_word_timestamps FROM post WHERE id=?",
                    (post_id,),
                ).fetchone()[0]
                assert json.loads(post_word_timestamps) is None
        return

    assert [outcome[0] for outcome in python_outcomes] == [True, True, True]
    assert [outcome["success"] for outcome in rust_outcomes] == [True, True, True]
    expected_segments = [
        (
            8,
            0.125000000123,
            0.987654321987,
            "replacement segment café",
            "Speaker A",
        ),
        (
            9,
            1.234567890123,
            2.345678901234,
            "replacement segment 東京",
            None,
        ),
    ]
    for backend in (pair.python, pair.rust):
        with sqlite3.connect(backend.db_path) as connection:
            rows = connection.execute(
                "SELECT sequence_num,start_time,end_time,text,speaker_label "
                "FROM transcript_segment WHERE post_id=? ORDER BY sequence_num",
                (post_id,),
            ).fetchall()
            assert rows == expected_segments
            assert connection.execute(
                "SELECT status,last_segment_sequence_num,response "
                "FROM model_call WHERE id=601"
            ).fetchone() == ("success", 1, "2 segments transcribed.")
            stored_word_timestamps = connection.execute(
                "SELECT transcript_word_timestamps FROM post WHERE id=?", (post_id,)
            ).fetchone()[0]
            assert json.loads(stored_word_timestamps) == (
                _processor_word_timestamp_payload()
            )


def _make_operation(
    case_id: str, pair: WriterParityPair
) -> tuple[dict[str, Any], WriteCommand]:
    command_id = f"parity-{case_id}"
    if case_id == "generic_update_post_duration":
        model = "Post"
        model_id = pair.manifest.post_ids[1]
        data = {"id": model_id, "duration": 91.25}
        return (
            {
                "operation": "update",
                "model": model,
                "id": model_id,
                "data": {"duration": data["duration"]},
            },
            WriteCommand(command_id, WriteCommandType.UPDATE, model, data),
        )
    if case_id == "generic_update_feed_auto_whitelist":
        model = "Feed"
        model_id = pair.manifest.feed_ids[1]
        data = {"id": model_id, "auto_whitelist_new_episodes_override": True}
        return (
            {
                "operation": "update",
                "model": model,
                "id": model_id,
                "data": {"auto_whitelist_new_episodes_override": True},
            },
            WriteCommand(command_id, WriteCommandType.UPDATE, model, data),
        )
    if case_id == "generic_update_model_call_response":
        model = "ModelCall"
        model_id = 601
        data = {"id": model_id, "response": "Differentially updated response"}
        return (
            {
                "operation": "update",
                "model": model,
                "id": model_id,
                "data": {"response": data["response"]},
            },
            WriteCommand(command_id, WriteCommandType.UPDATE, model, data),
        )
    post_id = pair.manifest.post_ids[1]
    if case_id == "action_increment_download_count":
        params = {"post_id": post_id}
        return (
            {
                "operation": "action",
                "action": "increment_download_count",
                "params": params,
            },
            WriteCommand(
                command_id,
                WriteCommandType.ACTION,
                None,
                {"action": "increment_download_count", "params": params},
            ),
        )
    if case_id == "action_whitelist_post":
        params = {"post_id": post_id}
        return (
            {"operation": "action", "action": "whitelist_post", "params": params},
            WriteCommand(
                command_id,
                WriteCommandType.ACTION,
                None,
                {"action": "whitelist_post", "params": params},
            ),
        )
    raise AssertionError(f"No operation factory for parity case {case_id}")


@pytest.mark.parametrize(
    "case",
    WRITER_DIFFERENTIAL_CASES,
    ids=[case["case_id"] for case in WRITER_DIFFERENTIAL_CASES],
)
def test_writer_differential_parity(
    case: dict[str, Any],
    writer_parity_pair: WriterParityPair,
    tmp_path: Path,
) -> None:
    pair = writer_parity_pair
    owner_group = case.get("owner_group")
    if case.get("workflow"):
        _run_processor_replacement_workflow(case, pair, tmp_path)
        return
    builders = {
        "user": build_writer_user_case,
        "feed": build_writer_feed_case,
        "job": build_writer_job_case,
        "processor": build_writer_processor_case,
        "system": build_writer_system_case,
        "cleanup": build_writer_cleanup_case,
        "transaction": build_writer_transaction_case,
    }
    if isinstance(owner_group, str) and owner_group in builders:
        built = builders[owner_group](case["case_id"], pair)
    else:
        built = _make_operation(case["case_id"], pair)
    if len(built) == 3:
        operation, python_command, environment_overrides = built
    else:
        operation, python_command = built
        environment_overrides = {}
    # Model the JSON boundary before Python executes: some legacy action
    # handlers normalize command values in place (for example ISO timestamps
    # into datetime objects), and builders may share nested payload objects.
    # Rust receives this detached, JSON-safe copy, never Python's mutations.
    rust_operation = json.loads(json.dumps(operation, default=_writer_wire_value))

    if owner_group == "feed":
        prepare_writer_feed_case(case["case_id"], pair)

    python_before = writer_database_projection(
        pair.python.db_path, pair.python.instance_dir
    )
    rust_before = writer_database_projection(pair.rust.db_path, pair.rust.instance_dir)

    rust = RustWriterParityServer(
        pair.rust.db_path,
        tmp_path,
        environment_overrides.get("rust"),
    )
    try:
        python_results = []
        rust_outcomes = []
        recovery_snapshot = None
        for attempt_index in range(case.get("repeat_count", 1)):
            python_results.append(
                execute_python_parity_command(
                    pair.python,
                    python_command,
                    environment_overrides.get("python"),
                )
            )
            rust_outcomes.append(rust.execute_outcome(rust_operation))
            if case.get("recovery_after_failure") and attempt_index == 0:
                recovery_snapshot = {
                    "python_rows": writer_database_projection(
                        pair.python.db_path, pair.python.instance_dir
                    ),
                    "rust_rows": writer_database_projection(
                        pair.rust.db_path, pair.rust.instance_dir
                    ),
                    "python_files": snapshot_writer_cleanup_files(pair.python),
                    "rust_files": snapshot_writer_cleanup_files(pair.rust),
                }
                # The first attempt has rolled back. Stop the isolated Rust
                # writer before changing the fixture trigger, then restart it
                # against the same clone to prove the action can recover.
                rust.close()
                release_writer_cleanup_interruption(pair)
                rust = RustWriterParityServer(
                    pair.rust.db_path,
                    tmp_path,
                    environment_overrides.get("rust"),
                )
    finally:
        rust.close()

    python_success, python_data, python_error = python_results[-1]
    rust_outcome = rust_outcomes[-1]
    rust_success = rust_outcome["success"]
    rust_result = rust_outcome.get("result")
    rust_error = rust_outcome.get("error")
    python_rows = writer_database_projection(
        pair.python.db_path, pair.python.instance_dir
    )
    rust_rows = writer_database_projection(pair.rust.db_path, pair.rust.instance_dir)
    observation = {
        "case": case,
        "pair": pair,
        "operation": rust_operation,
        "python_success": python_success,
        "python_data": python_data,
        "python_error": python_error,
        "python_results": [result[1] for result in python_results],
        "python_successes": [result[0] for result in python_results],
        "python_errors": [result[2] for result in python_results],
        "rust_success": rust_success,
        "rust_result": rust_result,
        "rust_error": rust_error,
        "rust_results": [outcome.get("result") for outcome in rust_outcomes],
        "rust_successes": [outcome["success"] for outcome in rust_outcomes],
        "rust_errors": [outcome.get("error") for outcome in rust_outcomes],
        "recovery_snapshot": recovery_snapshot,
        "python_before": python_before,
        "rust_before": rust_before,
        "python_rows": python_rows,
        "rust_rows": rust_rows,
    }
    comparators = {
        "user": assert_writer_user_parity,
        "feed": assert_writer_feed_parity,
        "job": assert_writer_job_parity,
        "processor": assert_writer_processor_parity,
        "system": assert_writer_system_parity,
        "cleanup": assert_writer_cleanup_parity,
        "transaction": assert_writer_transaction_parity,
    }
    comparator = comparators.get(owner_group) if isinstance(owner_group, str) else None
    if comparator is not None:
        comparator(observation)
        return

    expected_success = case.get("expect_success", True)
    assert python_success is expected_success, python_error
    assert rust_success is expected_success, rust_outcome
    if not expected_success:
        assert python_rows == python_before, (
            f"Python failure mutated state: {case['case_id']}"
        )
        assert rust_rows == rust_before, (
            f"Rust failure mutated state: {case['case_id']}"
        )
        return
    assert rust_result == python_data, {
        "case_id": case["case_id"],
        "python_result": python_data,
        "rust_result": rust_result,
    }
    assert rust_rows == python_rows, f"database effects differ for {case['case_id']}"
