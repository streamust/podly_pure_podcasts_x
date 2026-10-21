from __future__ import annotations

import json

from scripts.check_writer_schema import (
    SNAPSHOT,
    alembic_head,
    model_schema,
    pin_errors,
    schema_differences,
)


def test_current_models_and_pins_match_reviewed_snapshot() -> None:
    snapshot = json.loads(SNAPSHOT.read_text())
    head = alembic_head()
    assert snapshot["revision"] == head
    assert pin_errors(head) == []
    assert schema_differences(snapshot["tables"], model_schema()) == []


def test_new_column_or_changed_default_is_reported() -> None:
    column = {
        "type": "VARCHAR",
        "nullable": True,
        "primary_key": False,
        "python_default": False,
        "server_default": False,
    }
    reviewed = {"feed": {"title": column}}
    changed = {
        "feed": {
            "title": {**column, "python_default": True},
            "whisper_language": column,
        }
    }
    assert schema_differences(reviewed, changed) == [
        f"feed.title: {column} -> {changed['feed']['title']}",
        f"feed.whisper_language: None -> {column}",
    ]


def test_stale_rust_pin_is_reported() -> None:
    errors = pin_errors("abc123")
    assert len(errors) == 3
    assert all("Alembic head is abc123" in error for error in errors)
