#!/usr/bin/env python3
"""Fail CI when the schema changes without a matching Rust writer update.

The Rust writer uses hand-written SQL, so a model change it does not know about
(a new column, a changed default, a nullability change) would drift silently.
This compares the SQLAlchemy models with the snapshot the Rust writer was last
reviewed against, and checks that the Rust schema pin matches the Alembic head.

After updating the Rust writer for a schema change, refresh the snapshot with:
    uv run python scripts/check_writer_schema.py --update
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "scripts/writer_schema_snapshot.json"
PIN_FILES = {
    ROOT / "rust/src/writer/database.rs": r'EXPECTED_SCHEMA_REVISION: &str = "(\w+)"',
    ROOT / "scripts/bench_service_migration.py": r'EXPECTED_SCHEMA_REVISION = "(\w+)"',
    ROOT / "scripts/test_rust_writer_container.sh": r'upgraded_revision" = (\w+)',
}


def alembic_head() -> str:
    from alembic.script import ScriptDirectory

    heads = ScriptDirectory(str(ROOT / "src/migrations")).get_heads()
    if len(heads) != 1:
        raise SystemExit(f"expected one Alembic head, found {heads}")
    return heads[0]


def model_schema() -> dict[str, Any]:
    sys.path.insert(0, str(ROOT / "src"))
    from app import models  # noqa: F401 -- registers every table on the metadata
    from app.extensions import db

    return {
        table.name: {
            column.name: {
                "type": str(column.type),
                "nullable": column.nullable,
                "primary_key": column.primary_key,
                "python_default": column.default is not None,
                "server_default": column.server_default is not None,
            }
            for column in table.columns
        }
        for table in sorted(db.metadata.tables.values(), key=lambda t: t.name)
    }


def schema_differences(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    differences = []
    for table in sorted(expected.keys() | actual.keys()):
        if table not in actual:
            differences.append(f"table removed: {table}")
            continue
        if table not in expected:
            differences.append(f"table added: {table}")
            continue
        for column in sorted(expected[table].keys() | actual[table].keys()):
            before = expected[table].get(column)
            after = actual[table].get(column)
            if before != after:
                differences.append(f"{table}.{column}: {before} -> {after}")
    return differences


def pin_errors(head: str) -> list[str]:
    errors = []
    for path, pattern in PIN_FILES.items():
        match = re.search(pattern, path.read_text())
        pinned = match.group(1) if match else None
        if pinned != head:
            errors.append(
                f"{path.relative_to(ROOT)} pins {pinned}, Alembic head is {head}"
            )
    return errors


def main() -> int:
    head = alembic_head()
    schema = model_schema()
    if "--update" in sys.argv:
        SNAPSHOT.write_text(
            json.dumps({"revision": head, "tables": schema}, indent=2) + "\n"
        )
        print(f"Wrote {SNAPSHOT.relative_to(ROOT)} for revision {head}.")
        return 0

    snapshot = json.loads(SNAPSHOT.read_text())
    errors = pin_errors(head)
    if snapshot["revision"] != head:
        errors.append(f"snapshot is for {snapshot['revision']}, Alembic head is {head}")
    differences = schema_differences(snapshot["tables"], schema)
    if differences:
        errors.append(
            "models differ from the schema the Rust writer was reviewed against:\n  "
            + "\n  ".join(differences)
        )
    if errors:
        print("Writer schema gate failed:", *errors, sep="\n- ", file=sys.stderr)
        print(
            "Update the Rust writer SQL (inserts must set Python-side defaults;"
            " update allowlists must include new fields), bump the schema pins,"
            " then run: uv run python scripts/check_writer_schema.py --update",
            file=sys.stderr,
        )
        return 1
    print(f"Writer schema gate passed: models and Rust pins match revision {head}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
