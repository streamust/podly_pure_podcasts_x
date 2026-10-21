"""Differential checks for generic model commands and transaction semantics."""

from __future__ import annotations

import sqlite3
from typing import Any, TypedDict

from app.writer.protocol import WriteCommand, WriteCommandType
from tests.writer_parity_fixtures import WriterParityPair

WRITER_DIFFERENTIAL_CASES = (
    {
        "case_id": "generic_update_ignores_unknown_model_field",
        "owner_group": "transaction",
        "operation": "update",
        "model": "Post",
    },
    {
        "case_id": "generic_update_feed_ignores_unknown_model_field",
        "owner_group": "transaction",
        "operation": "update",
        "model": "Feed",
    },
    {
        "case_id": "generic_update_model_call_ignores_unknown_model_field",
        "owner_group": "transaction",
        "operation": "update",
        "model": "ModelCall",
    },
    {
        "case_id": "generic_update_noop",
        "owner_group": "transaction",
        "operation": "update",
        "model": "Post",
    },
    {
        "case_id": "generic_update_explicit_null",
        "owner_group": "transaction",
        "operation": "update",
        "model": "Feed",
    },
    {
        "case_id": "generic_update_missing_record",
        "owner_group": "transaction",
        "operation": "update",
        "model": "Post",
        "expect_success": False,
    },
    {
        "case_id": "generic_delete_missing_record",
        "owner_group": "transaction",
        "operation": "delete",
        "model": "Post",
    },
    {
        "case_id": "transaction_ordered_results",
        "owner_group": "transaction",
        "operation": "transaction",
    },
    {
        "case_id": "transaction_failure_rolls_back_earlier_update",
        "owner_group": "transaction",
        "operation": "transaction",
        "expect_success": False,
    },
    {
        "case_id": "transaction_constraint_failure_rolls_back_earlier_update",
        "owner_group": "transaction",
        "operation": "transaction",
        "expect_success": False,
    },
    {
        "case_id": "transaction_deferred_constraint_commit_rolls_back_earlier_update",
        "owner_group": "transaction",
        "operation": "transaction",
        "expect_success": False,
    },
)


class _UpdateCommand(TypedDict):
    id: str
    type: str
    model: str
    data: dict[str, Any]


def _update(model: str, record_id: int, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation": "update",
        "model": model,
        "id": record_id,
        "data": data,
    }


def _delete(model: str, record_id: int) -> dict[str, Any]:
    return {"operation": "delete", "model": model, "id": record_id}


def build_writer_transaction_case(
    case_id: str, pair: WriterParityPair
) -> (
    tuple[dict[str, Any], WriteCommand]
    | tuple[dict[str, Any], WriteCommand, dict[str, dict[str, str]]]
):
    """Build equivalent wire and legacy commands from an isolated fixture pair."""
    post_id = pair.manifest.post_ids[1]
    missing_post_id = pair.manifest.missing_post_id
    if case_id == "transaction_deferred_constraint_commit_rolls_back_earlier_update":
        _seed_deferred_commit_failure(pair)
    if case_id == "generic_update_explicit_null":
        for backend in (pair.python, pair.rust):
            with sqlite3.connect(backend.db_path) as connection:
                connection.execute(
                    "UPDATE feed SET auto_whitelist_new_episodes_override=1 WHERE id=?",
                    (pair.manifest.feed_ids[1],),
                )
    if case_id in {
        "generic_update_ignores_unknown_model_field",
        "generic_update_feed_ignores_unknown_model_field",
        "generic_update_model_call_ignores_unknown_model_field",
        "generic_update_noop",
        "generic_update_explicit_null",
    }:
        model, record_id = {
            "generic_update_ignores_unknown_model_field": (
                "Post",
                post_id,
            ),
            "generic_update_feed_ignores_unknown_model_field": (
                "Feed",
                pair.manifest.feed_ids[1],
            ),
            "generic_update_model_call_ignores_unknown_model_field": (
                "ModelCall",
                601,
            ),
            "generic_update_noop": ("Post", post_id),
            "generic_update_explicit_null": (
                "Feed",
                pair.manifest.feed_ids[1],
            ),
        }[case_id]
        data = (
            {}
            if case_id == "generic_update_noop"
            else {"auto_whitelist_new_episodes_override": None}
            if case_id == "generic_update_explicit_null"
            else {"not_a_model_column": "ignored"}
        )
        return (
            _update(model, record_id, data),
            WriteCommand(
                f"parity-{case_id}",
                WriteCommandType.UPDATE,
                model,
                {"id": record_id, **data},
            ),
        )
    if case_id == "generic_update_missing_record":
        data = {"duration": 73.25}
        return (
            _update("Post", missing_post_id, data),
            WriteCommand(
                f"parity-{case_id}",
                WriteCommandType.UPDATE,
                "Post",
                {"id": missing_post_id, **data},
            ),
        )
    if case_id == "generic_delete_missing_record":
        return (
            _delete("Post", missing_post_id),
            WriteCommand(
                f"parity-{case_id}",
                WriteCommandType.DELETE,
                "Post",
                {"id": missing_post_id},
            ),
        )
    if case_id in {
        "transaction_ordered_results",
        "transaction_failure_rolls_back_earlier_update",
        "transaction_constraint_failure_rolls_back_earlier_update",
        "transaction_deferred_constraint_commit_rolls_back_earlier_update",
    }:
        commands: list[_UpdateCommand] = [
            {
                "id": "ordered-post-update",
                "type": "update",
                "model": "Post",
                "data": {"id": post_id, "duration": 81.5},
            },
            {
                "id": "ordered-feed-update",
                "type": "update",
                "model": "Feed",
                "data": {
                    "id": pair.manifest.feed_ids[1],
                    "chapter_filter_strings": "synthetic filter",
                },
            },
            {
                "id": "ordered-post-update-again",
                "type": "update",
                "model": "Post",
                "data": {"id": post_id, "duration": 82.5},
            },
        ]
        if case_id == "transaction_failure_rolls_back_earlier_update":
            commands[1] = {
                "id": "missing-post-update",
                "type": "update",
                "model": "Post",
                "data": {"id": missing_post_id, "duration": 99},
            }
        elif case_id == "transaction_constraint_failure_rolls_back_earlier_update":
            # Python assigns this valid model attribute and fails at commit on
            # ModelCall.status's NOT NULL constraint. Rust observes the constraint
            # while executing the UPDATE; both must roll back the first update.
            commands[1] = {
                "id": "null-model-call-status",
                "type": "update",
                "model": "ModelCall",
                "data": {"id": 601, "status": None},
            }
        elif (
            case_id
            == "transaction_deferred_constraint_commit_rolls_back_earlier_update"
        ):
            commands = [
                {
                    "id": "preceding-feed-update",
                    "type": "update",
                    "model": "Feed",
                    "data": {
                        "id": pair.manifest.feed_ids[1],
                        "chapter_filter_strings": "must roll back at commit",
                    },
                },
                {
                    "id": "deferred-constraint-post-update",
                    "type": "update",
                    "model": "Post",
                    "data": {"id": post_id, "duration": 83.75},
                },
            ]
        wire_commands = [
            {
                "command_id": command["id"],
                **_update(
                    command["model"],
                    command["data"]["id"],
                    {
                        key: value
                        for key, value in command["data"].items()
                        if key != "id"
                    },
                ),
            }
            for command in commands
        ]
        operation = {"operation": "transaction", "commands": wire_commands}
        command = WriteCommand(
            f"parity-{case_id}",
            WriteCommandType.TRANSACTION,
            None,
            {"commands": commands},
        )
        if (
            case_id
            == "transaction_deferred_constraint_commit_rolls_back_earlier_update"
        ):
            foreign_key_env = {"PODLY_TEST_DEFERRED_FOREIGN_KEYS": "1"}
            return (
                operation,
                command,
                {"python": foreign_key_env, "rust": foreign_key_env},
            )
        return (operation, command)
    raise AssertionError(f"No transaction parity builder for {case_id}")


def _seed_deferred_commit_failure(pair: WriterParityPair) -> None:
    """Defer a trigger-created FK violation until the outer transaction commits.

    Runtime keeps foreign-key enforcement off for ORM compatibility. This one
    isolated case opts into SQLite FK enforcement only in test mode so it can
    verify rollback after `COMMIT` itself rejects an otherwise successful
    sequence of subcommands.
    """
    schema = """
        CREATE TABLE parity_deferred_parent(id INTEGER PRIMARY KEY);
        CREATE TABLE parity_deferred_child(
            id INTEGER PRIMARY KEY,
            parent_id INTEGER NOT NULL,
            FOREIGN KEY(parent_id) REFERENCES parity_deferred_parent(id)
                DEFERRABLE INITIALLY DEFERRED
        );
        CREATE TRIGGER parity_deferred_commit_failure
        AFTER UPDATE OF duration ON post
        BEGIN
            INSERT INTO parity_deferred_child(id,parent_id) VALUES (1,999999);
        END;
    """
    for backend in (pair.python, pair.rust):
        with sqlite3.connect(backend.db_path) as connection:
            connection.executescript(schema)


def assert_writer_transaction_parity(observation: dict[str, Any]) -> None:
    """Compare outcome and persisted state; retain distinct legacy error text."""
    case_id = observation["case"]["case_id"]
    python_success = observation["python_success"]
    rust_success = observation["rust_success"]
    expected_success = observation["case"].get("expect_success", True)
    assert python_success is expected_success, {
        "case_id": case_id,
        "python_error": observation["python_error"],
    }
    assert rust_success is expected_success, {
        "case_id": case_id,
        "rust_error": observation["rust_error"],
    }
    assert python_success is rust_success, {
        "case_id": case_id,
        "python_error": observation["python_error"],
        "rust_error": observation["rust_error"],
    }
    if not python_success:
        assert observation["python_rows"] == observation["python_before"], (
            f"Python failure mutated state: {case_id}"
        )
        assert observation["rust_rows"] == observation["rust_before"], (
            f"Rust failure mutated state: {case_id}"
        )
        assert observation["python_rows"] == observation["rust_rows"], (
            f"failure rollback differs for {case_id}"
        )
        if case_id == "generic_update_missing_record":
            assert "not found" in str(observation["python_error"]).casefold()
            rust_error = observation["rust_error"]
            assert isinstance(rust_error, dict)
            assert rust_error.get("code") == "not_found"
        elif (
            case_id
            == "transaction_deferred_constraint_commit_rolls_back_earlier_update"
        ):
            assert (
                "foreign key constraint failed"
                in str(observation["python_error"]).casefold()
            )
            rust_error = observation["rust_error"]
            assert isinstance(rust_error, dict)
            assert rust_error.get("code") == "commit_failed"
        return

    assert observation["python_data"] == observation["rust_result"], {
        "case_id": case_id,
        "python_result": observation["python_data"],
        "rust_result": observation["rust_result"],
    }
    assert observation["python_rows"] == observation["rust_rows"], (
        f"database effects differ for {case_id}"
    )

    if case_id in {
        "generic_update_ignores_unknown_model_field",
        "generic_update_feed_ignores_unknown_model_field",
        "generic_update_model_call_ignores_unknown_model_field",
        "generic_update_noop",
    }:
        assert observation["python_rows"] == observation["python_before"], (
            f"ignored/no-op update changed state: {case_id}"
        )
        assert observation["rust_rows"] == observation["rust_before"], (
            f"ignored/no-op update changed state: {case_id}"
        )
    elif case_id == "generic_update_explicit_null":
        pair = observation["pair"]
        with sqlite3.connect(pair.python.db_path) as connection:
            value = connection.execute(
                "SELECT auto_whitelist_new_episodes_override FROM feed WHERE id=?",
                (pair.manifest.feed_ids[1],),
            ).fetchone()
        assert value == (None,)
    elif case_id == "transaction_ordered_results":
        results = observation["python_data"]["results"]
        assert [result["command_id"] for result in results] == [
            "ordered-post-update",
            "ordered-feed-update",
            "ordered-post-update-again",
        ]
