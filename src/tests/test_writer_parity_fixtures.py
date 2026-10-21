"""Safety and coverage checks for the shared writer parity snapshot."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from tests.writer_parity_fixtures import (
    LARGE_JSON_MIN_BYTES,
    WriterParityPair,
    export_writer_parity_instance,
)


def _query_value(path: Path, sql: str, params: tuple[object, ...] = ()) -> object:
    with sqlite3.connect(path) as conn:
        row = conn.execute(sql, params).fetchone()
    assert row is not None
    return row[0]


def _logical_projection(path: Path, root: Path) -> dict[str, object]:
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        posts = []
        for row in conn.execute(
            "SELECT id,guid,duration,typeof(duration) AS storage_type,"
            "unprocessed_audio_path,processed_audio_path FROM post ORDER BY id"
        ):
            item = dict(row)
            for key in ("unprocessed_audio_path", "processed_audio_path"):
                value = item[key]
                if value:
                    item[key] = str(Path(value).relative_to(root))
            posts.append(item)
        return {
            "posts": posts,
            "jobs": [
                tuple(row)
                for row in conn.execute(
                    "SELECT id,status FROM processing_job ORDER BY id"
                )
            ],
            "tokens": [
                tuple(row)
                for row in conn.execute(
                    "SELECT token_id,feed_id,revoked FROM feed_access_token ORDER BY id"
                )
            ],
        }


def test_writer_parity_fixture_contains_required_cases(
    writer_parity_pair: WriterParityPair,
) -> None:
    backend = writer_parity_pair.python
    manifest = writer_parity_pair.manifest
    with sqlite3.connect(backend.db_path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        conn.execute("PRAGMA foreign_keys=ON")
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT COUNT(*) FROM alembic_version").fetchone() == (1,)
        assert {
            row[0] for row in conn.execute("SELECT status FROM processing_job")
        } == {
            "completed",
            "running",
            "cancelled",
        }
        assert conn.execute("SELECT COUNT(*) FROM feed_supporter").fetchone() == (1,)
        assert conn.execute("SELECT COUNT(*) FROM identification").fetchone() == (1,)
        assert conn.execute("SELECT COUNT(*) FROM audio_segment").fetchone() == (1,)
        assert conn.execute(
            "SELECT COUNT(*) FROM feed_access_token WHERE feed_id IS NULL"
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT COUNT(*) FROM feed_access_token WHERE revoked = 1"
        ).fetchone() == (1,)
        durations = set(conn.execute("SELECT typeof(duration) FROM post"))
        assert durations == {("integer",), ("real",)}
        payload = conn.execute(
            "SELECT transcript_word_timestamps FROM post WHERE id=?",
            (manifest.post_ids[0],),
        ).fetchone()[0]
        assert len(payload.encode()) >= LARGE_JSON_MIN_BYTES
        assert isinstance(json.loads(payload), list)
        assert (
            conn.execute(
                "SELECT 1 FROM post WHERE id=?", (manifest.missing_post_id,)
            ).fetchone()
            is None
        )
        assert (
            conn.execute(
                "SELECT 1 FROM processing_job WHERE id=?", (manifest.missing_job_id,)
            ).fetchone()
            is None
        )
        assert (
            conn.execute(
                "SELECT 1 FROM feed_access_token WHERE token_id=?",
                (manifest.missing_token_id,),
            ).fetchone()
            is None
        )


def test_writer_parity_backends_are_isolated(
    writer_parity_pair: WriterParityPair,
) -> None:
    source = writer_parity_pair.source
    python = writer_parity_pair.python
    rust = writer_parity_pair.rust
    paths = {source.db_path.resolve(), python.db_path.resolve(), rust.db_path.resolve()}
    assert len(paths) == 3
    assert python.root.resolve() != rust.root.resolve()
    assert python.database_uri.startswith("sqlite:////")
    assert rust.database_uri.startswith("sqlite:////")
    assert Path("src/instance").resolve() not in python.db_path.resolve().parents
    assert Path("src/instance").resolve() not in rust.db_path.resolve().parents

    with sqlite3.connect(python.db_path) as conn:
        conn.execute(
            "UPDATE post SET title='python-only mutation' WHERE id=?",
            (writer_parity_pair.manifest.post_ids[0],),
        )
        conn.commit()
    (python.data_in / "python-only.txt").write_text("isolated\n")

    sql = "SELECT title FROM post WHERE id=?"
    params = (writer_parity_pair.manifest.post_ids[0],)
    assert _query_value(python.db_path, sql, params) == "python-only mutation"
    assert _query_value(rust.db_path, sql, params) == "Integral duration episode"
    assert _query_value(source.db_path, sql, params) == "Integral duration episode"
    assert not (rust.data_in / "python-only.txt").exists()


def test_writer_parity_clones_have_equivalent_logical_content(
    writer_parity_pair: WriterParityPair,
) -> None:
    assert _logical_projection(
        writer_parity_pair.python.db_path, writer_parity_pair.python.root
    ) == _logical_projection(
        writer_parity_pair.rust.db_path, writer_parity_pair.rust.root
    )


def test_writer_parity_exporter_uses_container_paths(tmp_path: Path) -> None:
    instance_dir = tmp_path / "exported" / "instance"
    manifest = export_writer_parity_instance(instance_dir, benchmark_post_count=5)

    with sqlite3.connect(instance_dir / "sqlite3.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM post").fetchone() == (5,)
        paths = conn.execute(
            "SELECT unprocessed_audio_path,processed_audio_path FROM post WHERE id=?",
            (manifest.post_ids[0],),
        ).fetchone()
        assert paths == (
            "/app/src/instance/data/in/synthetic-input.mp3",
            "/app/src/instance/data/srv/synthetic-output.mp3",
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert (instance_dir / "data" / "in" / "synthetic-input.mp3").is_file()
    assert (instance_dir / "data" / "srv" / "synthetic-output.mp3").is_file()
