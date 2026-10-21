"""Differential parity cases for the writer's user and account actions.

Each builder sends the same synthetic payload to the real Python executor and
the isolated Rust writer. Time-dependent values and bcrypt salts are validated
semantically by the shared differential runner.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import Any

from app.auth.passwords import verify_password
from app.writer.protocol import WriteCommand, WriteCommandType
from tests.writer_parity_fixtures import WriterParityPair

ParamsFactory = Callable[[WriterParityPair], Any]


def build_writer_user_case(
    case_id: str, pair: WriterParityPair
) -> tuple[dict[str, Any], WriteCommand]:
    case = next(
        item for item in WRITER_DIFFERENTIAL_CASES if item["case_id"] == case_id
    )
    _seed_case_preconditions(pair, case_id)
    params = _USER_PARAMS[case_id](pair)
    action = case["action"]
    if case_id == "user_upsert_discord_repeat_same_identity":
        first_params, second_params = params
        operation = {
            "operation": "transaction",
            "commands": [
                {
                    "command_id": command_id,
                    "operation": "action",
                    "action": action,
                    "params": command_params,
                }
                for command_id, command_params in (
                    ("discord-upsert-first", first_params),
                    ("discord-upsert-repeat", second_params),
                )
            ],
        }
        command = WriteCommand(
            id=f"parity-{case_id}",
            type=WriteCommandType.TRANSACTION,
            model=None,
            data={
                "commands": [
                    {
                        "id": command_id,
                        "type": WriteCommandType.ACTION.value,
                        "model": None,
                        "data": {"action": action, "params": command_params},
                    }
                    for command_id, command_params in (
                        ("discord-upsert-first", first_params),
                        ("discord-upsert-repeat", second_params),
                    )
                ]
            },
        )
        return operation, command
    operation = {"operation": "action", "action": action, "params": params}
    command = WriteCommand(
        id=f"parity-{case_id}",
        type=WriteCommandType.ACTION,
        model=None,
        data={"action": action, "params": params},
    )
    return operation, command


def _seed_case_preconditions(pair: WriterParityPair, case_id: str) -> None:
    """Prepare equivalent synthetic state in the two isolated database clones."""
    statements = {
        "user_create_duplicate_fails_without_mutation": (
            "UPDATE users SET username=? WHERE id=?",
            ("parity.user", pair.manifest.user_id),
        ),
        "user_upsert_discord_update_existing": (
            "UPDATE users SET discord_id=? WHERE id=?",
            ("parity-discord-existing", pair.manifest.user_id),
        ),
        "user_set_billing_by_customer_id_found": (
            "UPDATE users SET stripe_customer_id=? WHERE id=?",
            ("cus_parity_existing", pair.manifest.user_id),
        ),
        "user_set_billing_by_customer_id_partial": (
            "UPDATE users SET stripe_customer_id=? WHERE id=?",
            ("cus_parity_existing", pair.manifest.user_id),
        ),
        "user_set_billing_subscription_only": (
            "UPDATE users SET stripe_customer_id=?,stripe_subscription_id=? WHERE id=?",
            ("cus_parity_existing", "sub_parity_old", pair.manifest.user_id),
        ),
        "user_set_billing_explicit_nulls": (
            "UPDATE users SET stripe_customer_id=?,stripe_subscription_id=? WHERE id=?",
            ("cus_parity_clear", "sub_parity_clear", pair.manifest.user_id),
        ),
        "user_upsert_discord_username_collision": (
            "UPDATE users SET username=? WHERE id=?",
            ("parity_collision", pair.manifest.user_id),
        ),
    }
    statement = statements.get(case_id)
    if statement is None:
        return
    sql, values = statement
    for backend in (pair.python, pair.rust):
        with sqlite3.connect(backend.db_path) as connection:
            connection.execute(sql, values)


def _table_columns(db_path: Any) -> dict[str, list[str]]:
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


def _assert_projection_equal(
    observation: dict[str, Any],
    *,
    user_id: int | None = None,
    variable_user_fields: frozenset[str] = frozenset(),
) -> None:
    python_rows = observation["python_rows"]
    rust_rows = observation["rust_rows"]
    assert python_rows.keys() == rust_rows.keys()
    columns = _table_columns(observation["pair"].python.db_path)

    def normalize(
        projection: dict[str, list[tuple[Any, ...]]],
    ) -> dict[str, list[tuple[Any, ...]]]:
        normalized: dict[str, list[tuple[Any, ...]]] = {}
        for table, rows in projection.items():
            names = columns[table]
            values = []
            for row in rows:
                record = dict(zip(names, row, strict=True))
                if table == "users" and record.get("id") == user_id:
                    for field in variable_user_fields:
                        record[field] = f"<nondeterministic:{field}>"
                values.append(tuple(record[name] for name in names))
            normalized[table] = sorted(values, key=repr)
        return normalized

    assert normalize(python_rows) == normalize(rust_rows), (
        f"database state differs for {observation['case']['case_id']}"
    )


def _read_user(db_path: Any, user_id: int) -> dict[str, Any]:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM users WHERE id=?", (user_id,)
        ).fetchone()
        assert row is not None, f"expected user {user_id} to exist"
        return dict(row)


def _parse_timestamp(value: Any) -> datetime:
    assert isinstance(value, str) and value
    return datetime.fromisoformat(value)


def _error_message(error: Any) -> str:
    if isinstance(error, dict):
        error = error.get("message", error)
    return str(error).lower()


def _assert_failed_user_action(observation: dict[str, Any]) -> None:
    case = observation["case"]
    case_id = case["case_id"]
    assert observation["python_error"]
    assert observation["rust_error"]
    expected_message = {
        "user_create_duplicate_fails_without_mutation": "user with that username already exists",
        "user_create_missing_username_fails": "username is required",
        "user_create_empty_password_fails": "password is required",
        "user_create_invalid_role_fails": "role",
        "user_update_password_missing_user_fails": "user 999101 not found",
        "user_set_role_invalid_role_fails": "role",
        "user_upsert_discord_registration_denied": "self-registration via discord is disabled",
        "user_update_password_empty_fails": "new_password is required",
        "user_set_role_missing_user_fails": "user 999101 not found",
        "user_set_manual_feed_allowance_invalid_fails": "allowance must be an integer or none",
        "user_set_manual_feed_allowance_missing_user_fails": "user 999101 not found",
        "user_upsert_discord_missing_identity_fails": "discord_id and discord_username are required",
        "user_set_billing_fields_invalid_allowance_rolls_back": "invalid literal for int() with base 10",
        "user_update_last_active_missing_user_fails": "user 999101 not found",
    }[case_id]
    assert expected_message in _error_message(observation["python_error"])
    assert expected_message in _error_message(observation["rust_error"])
    assert observation["python_before"] == observation["python_rows"]
    assert observation["rust_before"] == observation["rust_rows"]


def _assert_created_user(observation: dict[str, Any]) -> None:
    case = observation["case"]
    case_id = case["case_id"]
    pair = observation["pair"]
    python_data = observation["python_data"]
    rust_result = observation["rust_result"]
    assert python_data["user_id"] == rust_result["user_id"]
    user_id = int(python_data["user_id"])
    python_user = _read_user(pair.python.db_path, user_id)
    rust_user = _read_user(pair.rust.db_path, user_id)
    assert python_user["username"] == rust_user["username"]
    assert python_user["role"] == rust_user["role"] == "user"
    assert python_user["feed_allowance"] == rust_user["feed_allowance"] == 0
    assert (
        python_user["feed_subscription_status"]
        == rust_user["feed_subscription_status"]
        == "inactive"
    )
    assert (
        python_user["manual_feed_allowance"]
        == rust_user["manual_feed_allowance"]
        is None
    )
    for user in (python_user, rust_user):
        _parse_timestamp(user["created_at"])
        _parse_timestamp(user["updated_at"])
    if case_id == "user_create_defaults_and_password_hash":
        assert python_user["username"] == "parity.created.user"
        for user in (python_user, rust_user):
            assert user["password_hash"].startswith(("$2a$", "$2b$", "$2y$"))
            assert verify_password("synthetic-new-user-password", user["password_hash"])
    else:
        assert python_user["username"] == "parity_new_member"
        assert python_user["password_hash"] == rust_user["password_hash"] == ""
        assert (
            python_user["discord_id"] == rust_user["discord_id"] == "parity-discord-new"
        )
        assert (
            python_user["discord_username"]
            == rust_user["discord_username"]
            == "Parity New Member"
        )
    _assert_projection_equal(
        observation,
        user_id=user_id,
        variable_user_fields=frozenset({"created_at", "updated_at", "password_hash"}),
    )


def _assert_password_update(observation: dict[str, Any]) -> None:
    user_id = observation["pair"].manifest.user_id
    assert (
        observation["python_data"] == observation["rust_result"] == {"user_id": user_id}
    )
    for backend in (observation["pair"].python, observation["pair"].rust):
        user = _read_user(backend.db_path, user_id)
        assert user["password_hash"].startswith(("$2a$", "$2b$", "$2y$"))
        assert verify_password("synthetic-rotated-password", user["password_hash"])
    _assert_projection_equal(
        observation,
        user_id=user_id,
        variable_user_fields=frozenset({"password_hash", "updated_at"}),
    )


def _assert_last_active(observation: dict[str, Any]) -> None:
    pair = observation["pair"]
    user_id = pair.manifest.user_id
    python_data = observation["python_data"]
    rust_result = observation["rust_result"]
    assert python_data["user_id"] == rust_result["user_id"] == user_id
    python_result_time = _parse_timestamp(python_data["last_active"])
    rust_result_time = _parse_timestamp(rust_result["last_active"])
    assert abs((python_result_time - rust_result_time).total_seconds()) < 2
    for backend in (pair.python, pair.rust):
        stored_time = _parse_timestamp(
            _read_user(backend.db_path, user_id)["last_active"]
        )
        result_time = (
            python_result_time if backend.name == "python" else rust_result_time
        )
        assert abs((stored_time - result_time).total_seconds()) < 0.001
    _assert_projection_equal(
        observation,
        user_id=user_id,
        variable_user_fields=frozenset({"last_active", "updated_at"}),
    )


def _assert_noop_user_action(observation: dict[str, Any]) -> None:
    assert observation["python_data"] == observation["rust_result"]
    assert observation["python_before"] == observation["python_rows"]
    assert observation["rust_before"] == observation["rust_rows"]


def _assert_delete_user(observation: dict[str, Any]) -> None:
    pair = observation["pair"]
    assert observation["python_data"] == observation["rust_result"] == {"deleted": True}
    assert all(
        row["id"] != pair.manifest.user_id
        for backend in (pair.python, pair.rust)
        for row in _read_users(backend.db_path)
    )
    for backend in (pair.python, pair.rust):
        _assert_deleted_user_side_effects(backend.db_path, pair.manifest.user_id)
    _assert_projection_equal(observation)


def _assert_updated_user(observation: dict[str, Any]) -> None:
    assert observation["python_data"] == observation["rust_result"]
    user_id = int(observation["python_data"]["user_id"])
    _assert_projection_equal(
        observation,
        user_id=user_id,
        variable_user_fields=frozenset({"updated_at"}),
    )


def _assert_billing_fields(observation: dict[str, Any]) -> None:
    assert observation["python_data"] == observation["rust_result"]
    case_id = observation["case"]["case_id"]
    expected = {
        "user_set_billing_subscription_only": {
            "stripe_customer_id": "cus_parity_existing",
            "stripe_subscription_id": "sub_parity_partial",
        },
        "user_set_billing_explicit_nulls": {
            "stripe_customer_id": None,
            "stripe_subscription_id": None,
        },
        "user_set_billing_zero_and_empty": {
            "feed_allowance": 0,
            "feed_subscription_status": "",
        },
        "user_set_billing_by_customer_id_partial": {
            "stripe_customer_id": "cus_parity_existing",
            "stripe_subscription_id": "sub_parity_partial_customer",
            "feed_allowance": 0,
            "feed_subscription_status": "active",
        },
    }[case_id]
    user_id = observation["pair"].manifest.user_id
    for backend in (observation["pair"].python, observation["pair"].rust):
        user = _read_user(backend.db_path, user_id)
        for field, value in expected.items():
            assert user[field] == value, (case_id, backend.name, field, user[field])
    _assert_projection_equal(
        observation,
        user_id=user_id,
        variable_user_fields=frozenset({"updated_at"}),
    )


def _assert_discord_username_collision(observation: dict[str, Any]) -> None:
    assert observation["python_data"] == observation["rust_result"]
    user_id = int(observation["python_data"]["user_id"])
    assert observation["python_data"]["created"] is True
    for backend in (observation["pair"].python, observation["pair"].rust):
        user = _read_user(backend.db_path, user_id)
        assert user["username"] == "parity_collision_1"
        assert user["discord_id"] == "parity-discord-collision"
    _assert_projection_equal(
        observation,
        user_id=user_id,
        variable_user_fields=frozenset({"created_at", "updated_at"}),
    )


def _assert_repeated_discord_upsert(observation: dict[str, Any]) -> None:
    results = observation["python_data"]["results"]
    assert observation["python_data"] == observation["rust_result"]
    assert [item["command_id"] for item in results] == [
        "discord-upsert-first",
        "discord-upsert-repeat",
    ]
    assert results[0]["data"]["created"] is True
    assert results[1]["data"]["created"] is False
    assert results[0]["data"]["user_id"] == results[1]["data"]["user_id"]
    user_id = int(results[0]["data"]["user_id"])
    for backend in (observation["pair"].python, observation["pair"].rust):
        user = _read_user(backend.db_path, user_id)
        assert user["discord_username"] == "Second Parity Name"
    _assert_projection_equal(
        observation,
        user_id=user_id,
        variable_user_fields=frozenset({"created_at", "updated_at"}),
    )


def assert_writer_user_parity(observation: dict[str, Any]) -> None:
    case = observation["case"]
    expected_success = case.get("expect_success", True)
    assert observation["python_success"] is expected_success
    assert observation["rust_success"] is expected_success
    if not expected_success:
        _assert_failed_user_action(observation)
        return

    handlers = {
        "created_user": _assert_created_user,
        "password_update": _assert_password_update,
        "last_active_timestamp": _assert_last_active,
        "no_op": _assert_noop_user_action,
        "delete_user": _assert_delete_user,
        "updated_user": _assert_updated_user,
        "billing_fields": _assert_billing_fields,
        "discord_username_collision": _assert_discord_username_collision,
        "repeated_discord_upsert": _assert_repeated_discord_upsert,
    }
    handler = handlers.get(case.get("semantics"))
    if handler is None:
        assert observation["python_data"] == observation["rust_result"]
        _assert_projection_equal(observation)
    else:
        handler(observation)


def _read_users(db_path: Any) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute("SELECT * FROM users")]


def _assert_deleted_user_side_effects(db_path: Any, user_id: int) -> None:
    with sqlite3.connect(db_path) as connection:
        tables = _table_columns(db_path)
        for table, columns in tables.items():
            if table == "processing_job":
                for column in ("requested_by_user_id", "billing_user_id"):
                    if column not in columns:
                        continue
                    remaining = connection.execute(
                        f'SELECT COUNT(*) FROM "{table}" WHERE "{column}"=?',
                        (user_id,),
                    ).fetchone()[0]
                    assert remaining == 0, (
                        f"user attribution remains in {table}.{column}"
                    )
                continue
            if table == "users" or "user_id" not in columns:
                continue
            remaining = connection.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE user_id=?', (user_id,)
            ).fetchone()[0]
            assert remaining == 0, f"user-linked rows remain in {table}"


def _user(pair: WriterParityPair) -> dict[str, Any]:
    return {"user_id": pair.manifest.user_id}


def _merge_user(pair: WriterParityPair, **values: Any) -> dict[str, Any]:
    return {**_user(pair), **values}


WRITER_DIFFERENTIAL_CASES = (
    {
        "case_id": "user_create_defaults_and_password_hash",
        "operation": "action",
        "action": "create_user",
        "owner": "create_user",
        "owner_group": "user",
        "source": "src/app/writer/actions/users.py",
        "semantics": "created_user",
    },
    {
        "case_id": "user_create_duplicate_fails_without_mutation",
        "operation": "action",
        "action": "create_user",
        "owner": "create_user",
        "owner_group": "user",
        "source": "src/app/writer/actions/users.py",
        "expect_success": False,
    },
    {
        "case_id": "user_create_missing_username_fails",
        "operation": "action",
        "action": "create_user",
        "owner": "create_user",
        "owner_group": "user",
        "source": "src/app/writer/actions/users.py",
        "expect_success": False,
    },
    {
        "case_id": "user_create_empty_password_fails",
        "operation": "action",
        "action": "create_user",
        "owner": "create_user",
        "owner_group": "user",
        "source": "src/app/writer/actions/users.py",
        "expect_success": False,
    },
    {
        "case_id": "user_create_invalid_role_fails",
        "operation": "action",
        "action": "create_user",
        "owner": "create_user",
        "owner_group": "user",
        "source": "src/app/writer/actions/users.py",
        "expect_success": False,
    },
    {
        "case_id": "user_update_password_hash",
        "operation": "action",
        "action": "update_user_password",
        "owner": "update_user_password",
        "owner_group": "user",
        "source": "src/app/writer/actions/users.py",
        "semantics": "password_update",
    },
    {
        "case_id": "user_update_password_missing_user_fails",
        "operation": "action",
        "action": "update_user_password",
        "owner": "update_user_password",
        "owner_group": "user",
        "source": "src/app/writer/actions/users.py",
        "expect_success": False,
    },
    {
        "case_id": "user_update_password_empty_fails",
        "operation": "action",
        "action": "update_user_password",
        "owner": "update_user_password",
        "owner_group": "user",
        "source": "src/app/writer/actions/users.py",
        "expect_success": False,
    },
    {
        "case_id": "user_delete_removes_related_state",
        "operation": "action",
        "action": "delete_user",
        "owner": "delete_user",
        "owner_group": "user",
        "source": "src/app/writer/actions/users.py",
        "semantics": "delete_user",
    },
    {
        "case_id": "user_delete_missing_user_is_noop",
        "operation": "action",
        "action": "delete_user",
        "owner": "delete_user",
        "owner_group": "user",
        "source": "src/app/writer/actions/users.py",
        "semantics": "no_op",
    },
    {
        "case_id": "user_set_role",
        "operation": "action",
        "action": "set_user_role",
        "owner": "set_user_role",
        "owner_group": "user",
        "source": "src/app/writer/actions/users.py",
        "semantics": "updated_user",
    },
    {
        "case_id": "user_set_role_invalid_role_fails",
        "operation": "action",
        "action": "set_user_role",
        "owner": "set_user_role",
        "owner_group": "user",
        "source": "src/app/writer/actions/users.py",
        "expect_success": False,
    },
    {
        "case_id": "user_set_role_missing_user_fails",
        "operation": "action",
        "action": "set_user_role",
        "owner": "set_user_role",
        "owner_group": "user",
        "source": "src/app/writer/actions/users.py",
        "expect_success": False,
    },
    {
        "case_id": "user_set_manual_feed_allowance_value",
        "operation": "action",
        "action": "set_manual_feed_allowance",
        "owner": "set_manual_feed_allowance",
        "owner_group": "user",
        "source": "src/app/writer/actions/users.py",
        "semantics": "updated_user",
    },
    {
        "case_id": "user_set_manual_feed_allowance_null",
        "operation": "action",
        "action": "set_manual_feed_allowance",
        "owner": "set_manual_feed_allowance",
        "owner_group": "user",
        "source": "src/app/writer/actions/users.py",
        "semantics": "updated_user",
    },
    {
        "case_id": "user_set_manual_feed_allowance_zero",
        "operation": "action",
        "action": "set_manual_feed_allowance",
        "owner": "set_manual_feed_allowance",
        "owner_group": "user",
        "source": "src/app/writer/actions/users.py",
        "semantics": "updated_user",
    },
    {
        "case_id": "user_set_manual_feed_allowance_negative_boundary",
        "operation": "action",
        "action": "set_manual_feed_allowance",
        "owner": "set_manual_feed_allowance",
        "owner_group": "user",
        "source": "src/app/writer/actions/users.py",
        "semantics": "updated_user",
    },
    {
        "case_id": "user_set_manual_feed_allowance_invalid_fails",
        "operation": "action",
        "action": "set_manual_feed_allowance",
        "owner": "set_manual_feed_allowance",
        "owner_group": "user",
        "source": "src/app/writer/actions/users.py",
        "expect_success": False,
    },
    {
        "case_id": "user_set_manual_feed_allowance_missing_user_fails",
        "operation": "action",
        "action": "set_manual_feed_allowance",
        "owner": "set_manual_feed_allowance",
        "owner_group": "user",
        "source": "src/app/writer/actions/users.py",
        "expect_success": False,
    },
    {
        "case_id": "user_upsert_discord_create",
        "operation": "action",
        "action": "upsert_discord_user",
        "owner": "upsert_discord_user",
        "owner_group": "user",
        "source": "src/app/writer/actions/users.py",
        "semantics": "created_user",
    },
    {
        "case_id": "user_upsert_discord_update_existing",
        "operation": "action",
        "action": "upsert_discord_user",
        "owner": "upsert_discord_user",
        "owner_group": "user",
        "source": "src/app/writer/actions/users.py",
        "semantics": "updated_user",
    },
    {
        "case_id": "user_upsert_discord_username_collision",
        "operation": "action",
        "action": "upsert_discord_user",
        "owner": "upsert_discord_user",
        "owner_group": "user",
        "source": "src/app/writer/actions/users.py",
        "semantics": "discord_username_collision",
    },
    {
        "case_id": "user_upsert_discord_repeat_same_identity",
        "operation": "transaction",
        "action": "upsert_discord_user",
        "owner": "upsert_discord_user",
        "owner_group": "user",
        "source": "src/app/writer/actions/users.py",
        "semantics": "repeated_discord_upsert",
    },
    {
        "case_id": "user_upsert_discord_registration_denied",
        "operation": "action",
        "action": "upsert_discord_user",
        "owner": "upsert_discord_user",
        "owner_group": "user",
        "source": "src/app/writer/actions/users.py",
        "expect_success": False,
    },
    {
        "case_id": "user_upsert_discord_missing_identity_fails",
        "operation": "action",
        "action": "upsert_discord_user",
        "owner": "upsert_discord_user",
        "owner_group": "user",
        "source": "src/app/writer/actions/users.py",
        "expect_success": False,
    },
    {
        "case_id": "user_set_billing_fields",
        "operation": "action",
        "action": "set_user_billing_fields",
        "owner": "set_user_billing_fields",
        "owner_group": "user",
        "source": "src/app/writer/actions/users.py",
        "semantics": "updated_user",
    },
    {
        "case_id": "user_set_billing_subscription_only",
        "operation": "action",
        "action": "set_user_billing_fields",
        "owner": "set_user_billing_fields",
        "owner_group": "user",
        "source": "src/app/writer/actions/users.py",
        "semantics": "billing_fields",
    },
    {
        "case_id": "user_set_billing_explicit_nulls",
        "operation": "action",
        "action": "set_user_billing_fields",
        "owner": "set_user_billing_fields",
        "owner_group": "user",
        "source": "src/app/writer/actions/users.py",
        "semantics": "billing_fields",
    },
    {
        "case_id": "user_set_billing_zero_and_empty",
        "operation": "action",
        "action": "set_user_billing_fields",
        "owner": "set_user_billing_fields",
        "owner_group": "user",
        "source": "src/app/writer/actions/users.py",
        "semantics": "billing_fields",
    },
    {
        "case_id": "user_set_billing_fields_invalid_allowance_rolls_back",
        "operation": "action",
        "action": "set_user_billing_fields",
        "owner": "set_user_billing_fields",
        "owner_group": "user",
        "source": "src/app/writer/actions/users.py",
        "expect_success": False,
    },
    {
        "case_id": "user_set_billing_by_customer_id_found",
        "operation": "action",
        "action": "set_user_billing_by_customer_id",
        "owner": "set_user_billing_by_customer_id",
        "owner_group": "user",
        "source": "src/app/writer/actions/users.py",
        "semantics": "updated_user",
    },
    {
        "case_id": "user_set_billing_by_customer_id_partial",
        "operation": "action",
        "action": "set_user_billing_by_customer_id",
        "owner": "set_user_billing_by_customer_id",
        "owner_group": "user",
        "source": "src/app/writer/actions/users.py",
        "semantics": "billing_fields",
    },
    {
        "case_id": "user_set_billing_by_customer_id_missing_is_noop",
        "operation": "action",
        "action": "set_user_billing_by_customer_id",
        "owner": "set_user_billing_by_customer_id",
        "owner_group": "user",
        "source": "src/app/writer/actions/users.py",
        "semantics": "no_op",
    },
    {
        "case_id": "user_update_last_active_timestamp",
        "operation": "action",
        "action": "update_user_last_active",
        "owner": "update_user_last_active",
        "owner_group": "user",
        "source": "src/app/writer/actions/users.py",
        "semantics": "last_active_timestamp",
    },
    {
        "case_id": "user_update_last_active_missing_user_fails",
        "operation": "action",
        "action": "update_user_last_active",
        "owner": "update_user_last_active",
        "owner_group": "user",
        "source": "src/app/writer/actions/users.py",
        "expect_success": False,
    },
)


_USER_PARAMS: dict[str, ParamsFactory] = {
    "user_create_defaults_and_password_hash": lambda _pair: {
        "username": "  Parity.Created.User  ",
        "password": "synthetic-new-user-password",
    },
    "user_create_duplicate_fails_without_mutation": lambda _pair: {
        "username": "parity.user",
        "password": "another-password",
    },
    "user_create_missing_username_fails": lambda _pair: {
        "password": "synthetic-invalid-user-password",
    },
    "user_create_empty_password_fails": lambda _pair: {
        "username": "parity.empty.password",
        "password": "",
    },
    "user_create_invalid_role_fails": lambda _pair: {
        "username": "parity.invalid.role",
        "password": "synthetic-invalid-user-password",
        "role": "owner",
    },
    "user_update_password_hash": lambda pair: _merge_user(
        pair, new_password="synthetic-rotated-password"
    ),
    "user_update_password_missing_user_fails": lambda _pair: {
        "user_id": 999_101,
        "new_password": "unused-password",
    },
    "user_update_password_empty_fails": lambda pair: _merge_user(pair, new_password=""),
    "user_delete_removes_related_state": _user,
    "user_delete_missing_user_is_noop": lambda _pair: {"user_id": 999_101},
    "user_set_role": lambda pair: _merge_user(pair, role="user"),
    "user_set_role_invalid_role_fails": lambda pair: _merge_user(pair, role="owner"),
    "user_set_role_missing_user_fails": lambda _pair: {
        "user_id": 999_101,
        "role": "admin",
    },
    "user_set_manual_feed_allowance_value": lambda pair: _merge_user(
        pair, allowance="7"
    ),
    "user_set_manual_feed_allowance_null": lambda pair: _merge_user(
        pair, allowance=None
    ),
    "user_set_manual_feed_allowance_zero": lambda pair: _merge_user(pair, allowance=0),
    "user_set_manual_feed_allowance_negative_boundary": lambda pair: _merge_user(
        pair, allowance=-1
    ),
    "user_set_manual_feed_allowance_invalid_fails": lambda pair: _merge_user(
        pair, allowance="not-an-int"
    ),
    "user_set_manual_feed_allowance_missing_user_fails": lambda _pair: {
        "user_id": 999_101,
        "allowance": 5,
    },
    "user_upsert_discord_create": lambda _pair: {
        "discord_id": "parity-discord-new",
        "discord_username": "Parity New Member",
        "allow_registration": True,
    },
    "user_upsert_discord_update_existing": lambda _pair: {
        "discord_id": "parity-discord-existing",
        "discord_username": "Updated Parity Name",
    },
    "user_upsert_discord_username_collision": lambda _pair: {
        "discord_id": "parity-discord-collision",
        "discord_username": "Parity Collision",
        "allow_registration": True,
    },
    "user_upsert_discord_repeat_same_identity": lambda _pair: (
        {
            "discord_id": "parity-discord-repeat",
            "discord_username": "First Parity Name",
            "allow_registration": True,
        },
        {
            "discord_id": "parity-discord-repeat",
            "discord_username": "Second Parity Name",
            "allow_registration": True,
        },
    ),
    "user_upsert_discord_registration_denied": lambda _pair: {
        "discord_id": "parity-discord-denied",
        "discord_username": "Denied Member",
        "allow_registration": False,
    },
    "user_upsert_discord_missing_identity_fails": lambda _pair: {
        "discord_username": "Missing Discord ID",
    },
    "user_set_billing_fields": lambda pair: _merge_user(
        pair,
        stripe_customer_id="cus_parity_updated",
        stripe_subscription_id="sub_parity_updated",
        feed_allowance=9,
        feed_subscription_status="active",
    ),
    "user_set_billing_subscription_only": lambda pair: _merge_user(
        pair, stripe_subscription_id="sub_parity_partial"
    ),
    "user_set_billing_explicit_nulls": lambda pair: _merge_user(
        pair, stripe_customer_id=None, stripe_subscription_id=None
    ),
    "user_set_billing_zero_and_empty": lambda pair: _merge_user(
        pair, feed_allowance=0, feed_subscription_status=""
    ),
    "user_set_billing_fields_invalid_allowance_rolls_back": lambda pair: _merge_user(
        pair,
        stripe_customer_id="cus_parity_partial_should_rollback",
        stripe_subscription_id="sub_parity_partial_should_rollback",
        feed_allowance="not-an-int",
    ),
    "user_set_billing_by_customer_id_found": lambda _pair: {
        "stripe_customer_id": "cus_parity_existing",
        "stripe_subscription_id": "sub_parity_by_customer",
        "feed_allowance": 12,
        "feed_subscription_status": "past_due",
    },
    "user_set_billing_by_customer_id_partial": lambda _pair: {
        "stripe_customer_id": "cus_parity_existing",
        "stripe_subscription_id": "sub_parity_partial_customer",
        "feed_allowance": 0,
        "feed_subscription_status": "active",
    },
    "user_set_billing_by_customer_id_missing_is_noop": lambda _pair: {
        "stripe_customer_id": "cus_parity_absent",
        "feed_allowance": 99,
    },
    "user_update_last_active_timestamp": _user,
    "user_update_last_active_missing_user_fails": lambda _pair: {
        "user_id": 999_101,
    },
}
