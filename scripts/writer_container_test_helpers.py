"""Helpers used only by the disposable Rust writer container lifecycle gate."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def prepare_older_schema() -> None:
    """Build a real Alembic database one revision behind the deployed head."""
    from flask import Flask
    from flask_migrate import upgrade

    from app import models  # noqa: F401
    from app.extensions import db, migrate

    instance_dir = Path(os.environ["PODLY_INSTANCE_DIR"])
    migration_dir = Path("/app/src/migrations")
    app = Flask("writer-container-older-schema-fixture")
    app.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{instance_dir / 'sqlite3.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(app)
    migrate.init_app(app, db, directory=str(migration_dir))
    with app.app_context():
        upgrade(directory=str(migration_dir), revision="88710a0fe69c")


def submit_interrupted_transaction() -> None:
    """Start a transaction that the container gate kills before commit."""
    from app.writer.client import writer_client
    from app.writer.protocol import WriteCommand, WriteCommandType

    Path("/tmp/podly-interrupted-writer-command-started").touch()
    command = WriteCommand(
        id="container-interrupted-transaction",
        type=WriteCommandType.TRANSACTION,
        model=None,
        data={
            "commands": [
                {
                    "id": "write-before-interruption",
                    "type": "action",
                    "data": {
                        "action": "__test_insert",
                        "params": {"value": "must-roll-back"},
                    },
                },
                {
                    "id": "hold-transaction-open",
                    "type": "action",
                    "data": {
                        "action": "__test_sleep",
                        "params": {"milliseconds": 30000},
                    },
                },
            ]
        },
    )
    writer_client.submit(command, wait=True, timeout=60)


def assert_database_write_lock_held() -> None:
    """Return successfully only while the in-flight writer transaction owns SQLite."""
    import sqlite3

    database = Path(os.environ["PODLY_INSTANCE_DIR"]) / "sqlite3.db"
    connection = sqlite3.connect(database, timeout=0.1)
    try:
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as error:
            if "locked" not in str(error).lower():
                raise
            return
        connection.rollback()
        raise SystemExit("writer has not acquired its transaction lock")
    finally:
        connection.close()


if __name__ == "__main__":
    if sys.argv[1:] == ["prepare-older-schema"]:
        prepare_older_schema()
    elif sys.argv[1:] == ["submit-interrupted-transaction"]:
        submit_interrupted_transaction()
    elif sys.argv[1:] == ["assert-write-lock"]:
        assert_database_write_lock_held()
    else:
        raise SystemExit(
            "expected prepare-older-schema, submit-interrupted-transaction, or assert-write-lock"
        )
