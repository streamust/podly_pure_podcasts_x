"""Isolated, migration-built fixtures for Python/Rust writer parity tests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from flask import Flask
from flask_migrate import upgrade

from app.extensions import db, migrate, migrations_dir
from app.models import (
    AppSettings,
    AudioSegment,
    Feed,
    FeedAccessToken,
    Identification,
    JobsManagerRun,
    ModelCall,
    Post,
    ProcessingJob,
    TranscriptSegment,
    User,
    UserFeed,
)

SYNTHETIC_PASSWORD = "parity-password-not-a-secret"
SYNTHETIC_TOKEN_SECRET = "parity-token-secret-not-a-secret"
LARGE_JSON_MIN_BYTES = 512 * 1024

# This is the executed differential subset, not a claim that every registered
# writer operation has parity coverage. Add a case only when the Python and
# Rust implementations are both invoked against isolated copies and their
# outcomes plus database effects are compared.
BASE_WRITER_DIFFERENTIAL_CASES = (
    {
        "case_id": "generic_update_post_duration",
        "operation": "update",
        "model": "Post",
        "owner": "generic_update",
        "source": "src/app/writer/model_ops.py:execute_model_command",
    },
    {
        "case_id": "generic_update_feed_auto_whitelist",
        "operation": "update",
        "model": "Feed",
        "owner": "generic_update",
        "source": "src/app/writer/model_ops.py:execute_model_command",
    },
    {
        "case_id": "generic_update_model_call_response",
        "operation": "update",
        "model": "ModelCall",
        "owner": "generic_update",
        "source": "src/app/writer/model_ops.py:execute_model_command",
    },
    {
        "case_id": "action_increment_download_count",
        "operation": "action",
        "action": "increment_download_count",
        "owner": "increment_download_count",
        "source": "src/app/writer/actions.py:CommandExecutor._register_default_actions",
    },
    {
        "case_id": "action_whitelist_post",
        "operation": "action",
        "action": "whitelist_post",
        "owner": "whitelist_post",
        "source": "src/app/writer/actions.py:CommandExecutor._register_default_actions",
    },
)


@dataclass(frozen=True)
class WriterParityManifest:
    user_id: int
    feed_ids: tuple[int, int]
    post_ids: tuple[int, int]
    post_guids: tuple[str, str]
    job_ids: tuple[str, str, str]
    token_ids: tuple[str, str, str]
    missing_post_id: int = 999_001
    missing_job_id: str = "00000000-0000-0000-0000-000000999001"
    missing_token_id: str = "missing-parity-token"


@dataclass(frozen=True)
class WriterParitySnapshot:
    root: Path
    db_path: Path
    manifest: WriterParityManifest


@dataclass(frozen=True)
class WriterParityBackend:
    name: str
    root: Path
    instance_dir: Path
    db_path: Path
    database_uri: str
    data_in: Path
    data_srv: Path


@dataclass(frozen=True)
class WriterParityPair:
    source: WriterParitySnapshot
    python: WriterParityBackend
    rust: WriterParityBackend
    manifest: WriterParityManifest


def _sqlite_uri(path: Path) -> str:
    return f"sqlite:///{path.resolve()}"


def _large_word_timestamps() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    index = 0
    while len(json.dumps(rows, separators=(",", ":")).encode()) < LARGE_JSON_MIN_BYTES:
        rows.extend(
            {
                "word": f"synthetic-{item:06d}-" + ("x" * 48),
                "start": item / 10,
                "end": (item + 1) / 10,
            }
            for item in range(index, index + 512)
        )
        index += 512
    return rows


def _seed_snapshot(
    app: Flask, db_path: Path, *, benchmark_post_count: int = 2
) -> WriterParityManifest:
    fixed = datetime(2026, 1, 2, 3, 4, 5)
    manifest = WriterParityManifest(
        user_id=101,
        feed_ids=(201, 202),
        post_ids=(301, 302),
        post_guids=("parity-integral-duration", "parity-fractional-duration"),
        job_ids=(
            "00000000-0000-0000-0000-000000000401",
            "00000000-0000-0000-0000-000000000402",
            "00000000-0000-0000-0000-000000000403",
        ),
        token_ids=("feed-token-active", "aggregate-token-active", "feed-token-revoked"),
    )

    with app.app_context():
        db.session.execute(db.text("PRAGMA foreign_keys=ON"))

        user = User(
            id=manifest.user_id,
            username="Parity.User",
            role="admin",
            feed_allowance=2,
            feed_subscription_status="active",
            manual_feed_allowance=3,
            created_at=fixed,
            updated_at=fixed,
        )
        user.set_password(SYNTHETIC_PASSWORD)
        feeds = [
            Feed(
                id=manifest.feed_ids[0],
                title="Parity feed one",
                description="Synthetic parity fixture",
                author="example.invalid",
                rss_url="https://one.example.invalid/feed.xml",
                last_changed_at=fixed,
            ),
            Feed(
                id=manifest.feed_ids[1],
                title="Parity feed two",
                rss_url="https://two.example.invalid/feed.xml",
                ad_detection_strategy="chapter",
                last_changed_at=fixed,
            ),
        ]
        db.session.add_all([user, *feeds])
        db.session.flush()
        db.session.add(UserFeed(id=501, feed_id=feeds[0].id, user_id=user.id))

        posts = [
            Post(
                id=manifest.post_ids[0],
                feed_id=feeds[0].id,
                guid=manifest.post_guids[0],
                download_url="https://one.example.invalid/integral.mp3",
                title="Integral duration episode",
                release_date=fixed,
                duration=3600,
                whitelisted=True,
                download_count=2,
                transcript_word_timestamps=_large_word_timestamps(),
                bleep_windows=[[10.0, 12.5]],
                refined_ad_boundaries=[{"start": 10.25, "end": 12.25}],
                refined_ad_boundaries_updated_at=fixed,
            ),
            Post(
                id=manifest.post_ids[1],
                feed_id=feeds[1].id,
                guid=manifest.post_guids[1],
                download_url="https://two.example.invalid/fractional.mp3",
                title="Fractional duration episode",
                release_date=fixed,
                duration=90,
                whitelisted=False,
            ),
        ]
        db.session.add_all(posts)
        db.session.flush()

        for offset in range(max(0, benchmark_post_count - len(posts))):
            ordinal = offset + 3
            db.session.add(
                Post(
                    id=300 + ordinal,
                    feed_id=feeds[0].id,
                    guid=f"parity-benchmark-{ordinal:04d}",
                    download_url=f"https://one.example.invalid/{ordinal:04d}.mp3",
                    title=f"Benchmark episode {ordinal:04d}",
                    description="Synthetic benchmark episode",
                    release_date=fixed - timedelta(hours=ordinal),
                    duration=1800 + ordinal,
                    whitelisted=False,
                    download_count=ordinal,
                )
            )
        db.session.flush()

        if benchmark_post_count >= 200:
            app_settings = db.session.get(AppSettings, 1)
            if app_settings is not None:
                app_settings.background_update_interval_minute = 1440
                app_settings.post_cleanup_retention_days = 0

            # Aggregate RSS caps each feed at three episodes. Seed 67 small
            # feeds so the benchmark exercises a 201-item RSS document without
            # touching the read-triggered refresh path on /feed/<id>.
            for feed_offset in range(67):
                aggregate_feed_id = 1_000 + feed_offset
                aggregate_feed = Feed(
                    id=aggregate_feed_id,
                    title=f"Aggregate parity feed {feed_offset:03d}",
                    rss_url=f"https://aggregate-{feed_offset:03d}.example.invalid/feed.xml",
                    last_changed_at=fixed,
                )
                db.session.add(aggregate_feed)
                db.session.flush()
                db.session.add(
                    UserFeed(
                        id=2_000 + feed_offset,
                        feed_id=aggregate_feed_id,
                        user_id=user.id,
                    )
                )
                for post_offset in range(3):
                    post_id = 10_000 + feed_offset * 3 + post_offset
                    db.session.add(
                        Post(
                            id=post_id,
                            feed_id=aggregate_feed_id,
                            guid=f"aggregate-{feed_offset:03d}-{post_offset}",
                            download_url=(
                                f"https://aggregate-{feed_offset:03d}.example.invalid/"
                                f"{post_offset}.mp3"
                            ),
                            title=(
                                f"Aggregate episode {feed_offset:03d}-{post_offset}"
                            ),
                            release_date=fixed
                            - timedelta(days=feed_offset, hours=post_offset),
                            duration=1_800 + post_offset,
                            whitelisted=True,
                            download_count=post_offset,
                        )
                    )
            db.session.flush()

        model_call = ModelCall(
            id=601,
            post_id=posts[0].id,
            first_segment_sequence_num=0,
            last_segment_sequence_num=0,
            model_name="synthetic/parity-model",
            prompt="Synthetic prompt",
            response="Synthetic response",
            timestamp=fixed,
            status="success",
            prompt_tokens=10,
            completion_tokens=4,
            total_tokens=14,
        )
        transcript = TranscriptSegment(
            id=701,
            post_id=posts[0].id,
            sequence_num=0,
            start_time=0.0,
            end_time=10.5,
            text="Synthetic transcript segment",
            speaker_label="A",
        )
        db.session.add_all([model_call, transcript])
        db.session.flush()
        db.session.add_all(
            [
                Identification(
                    id=801,
                    transcript_segment_id=transcript.id,
                    model_call_id=model_call.id,
                    confidence=0.875,
                    label="ad",
                ),
                AudioSegment(
                    id=901,
                    post_id=posts[0].id,
                    model_call_id=model_call.id,
                    label="speech",
                    start_time=0.0,
                    end_time=10.5,
                ),
            ]
        )

        run = JobsManagerRun(
            id="00000000-0000-0000-0000-000000000400",
            status="running",
            trigger="parity-fixture",
            started_at=fixed,
            total_jobs=3,
            running_jobs=1,
            completed_jobs=1,
            context_json={"source": "synthetic", "nested": {"enabled": True}},
            created_at=fixed,
            updated_at=fixed,
        )
        db.session.add(run)
        db.session.flush()
        jobs = [
            ProcessingJob(
                id=manifest.job_ids[0],
                jobs_manager_run_id=run.id,
                post_guid=posts[0].guid,
                status="completed",
                current_step=4,
                step_name="Complete",
                progress_percentage=100.0,
                started_at=fixed,
                completed_at=fixed,
                created_at=fixed,
                requested_by_user_id=user.id,
                billing_user_id=user.id,
                stage_history=[
                    {
                        "step": 1,
                        "step_name": "Download",
                        "started_at": fixed.isoformat(),
                    }
                ],
                ad_windows_count=1,
            ),
            ProcessingJob(
                id=manifest.job_ids[1],
                jobs_manager_run_id=run.id,
                post_guid=posts[1].guid,
                status="running",
                current_step=2,
                step_name="Transcribing",
                progress_percentage=42.5,
                started_at=fixed,
                created_at=fixed,
                requested_by_user_id=user.id,
                billing_user_id=user.id,
                stage_history=[
                    {
                        "step": 2,
                        "step_name": "Transcribing",
                        "started_at": fixed.isoformat(),
                    }
                ],
            ),
            ProcessingJob(
                id=manifest.job_ids[2],
                jobs_manager_run_id=run.id,
                post_guid=posts[0].guid,
                status="cancelled",
                current_step=1,
                step_name="Cancelled",
                progress_percentage=5.0,
                completed_at=fixed,
                error_message="Synthetic cancellation",
                created_at=fixed,
                requested_by_user_id=user.id,
                billing_user_id=user.id,
                stage_history=[],
            ),
        ]
        db.session.add_all(jobs)

        token_hash = hashlib.sha256(SYNTHETIC_TOKEN_SECRET.encode()).hexdigest()
        db.session.add_all(
            [
                FeedAccessToken(
                    id=1001,
                    token_id=manifest.token_ids[0],
                    token_hash=token_hash,
                    token_secret=SYNTHETIC_TOKEN_SECRET,
                    feed_id=feeds[0].id,
                    user_id=user.id,
                    created_at=fixed,
                    revoked=False,
                ),
                FeedAccessToken(
                    id=1002,
                    token_id=manifest.token_ids[1],
                    token_hash=token_hash,
                    token_secret=SYNTHETIC_TOKEN_SECRET,
                    feed_id=None,
                    user_id=user.id,
                    created_at=fixed,
                    revoked=False,
                ),
                FeedAccessToken(
                    id=1003,
                    token_id=manifest.token_ids[2],
                    token_hash=token_hash,
                    token_secret=SYNTHETIC_TOKEN_SECRET,
                    feed_id=feeds[1].id,
                    user_id=user.id,
                    created_at=fixed,
                    revoked=True,
                ),
            ]
        )
        db.session.commit()

        # Force both SQLite storage classes despite the INTEGER column affinity.
        db.session.execute(
            db.text("UPDATE post SET duration = 3600 WHERE id = :id"),
            {"id": manifest.post_ids[0]},
        )
        db.session.execute(
            db.text("UPDATE post SET duration = 90.5 WHERE id = :id"),
            {"id": manifest.post_ids[1]},
        )
        db.session.commit()
        violations = db.session.execute(db.text("PRAGMA foreign_key_check")).all()
        if violations:
            raise AssertionError(
                f"writer parity seed violates foreign keys: {violations}"
            )
        db.session.execute(db.text("PRAGMA wal_checkpoint(TRUNCATE)"))
        db.session.remove()
        db.engine.dispose()

    if not db_path.is_file():
        raise AssertionError(f"writer parity database was not created: {db_path}")
    return manifest


def build_writer_parity_snapshot(
    root: Path, *, benchmark_post_count: int = 2
) -> WriterParitySnapshot:
    """Build a closed, migration-current synthetic source database."""
    root = root.resolve()
    instance_dir = root / "instance"
    instance_dir.mkdir(parents=True, exist_ok=False)
    db_path = instance_dir / "sqlite3.db"
    app = Flask("writer-parity-fixture", instance_path=str(instance_dir))
    app.config.update(
        SQLALCHEMY_DATABASE_URI=_sqlite_uri(db_path),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(app)
    migrate.init_app(app, db)
    with app.app_context():
        upgrade(directory=migrations_dir)
    manifest = _seed_snapshot(app, db_path, benchmark_post_count=benchmark_post_count)
    return WriterParitySnapshot(root=root, db_path=db_path, manifest=manifest)


def _clone_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (
        sqlite3.connect(source) as source_conn,
        sqlite3.connect(destination) as dest_conn,
    ):
        source_conn.backup(dest_conn)


def export_writer_parity_instance(
    instance_dir: Path, *, benchmark_post_count: int = 200
) -> WriterParityManifest:
    """Export an isolated fixture whose persisted paths are container-local."""
    instance_dir = instance_dir.resolve()
    if instance_dir.exists() and any(instance_dir.iterdir()):
        raise ValueError(f"refusing to overwrite non-empty directory: {instance_dir}")
    instance_dir.mkdir(parents=True, exist_ok=True)
    db_path = instance_dir / "sqlite3.db"

    with tempfile.TemporaryDirectory(prefix="podly-writer-parity-source-") as raw:
        snapshot = build_writer_parity_snapshot(
            Path(raw) / "source", benchmark_post_count=benchmark_post_count
        )
        _clone_database(snapshot.db_path, db_path)
        manifest = snapshot.manifest

    data_in = instance_dir / "data" / "in"
    data_srv = instance_dir / "data" / "srv"
    for path in (data_in, data_srv, instance_dir / "logs", instance_dir / "config"):
        path.mkdir(parents=True, exist_ok=True)
    (data_in / "synthetic-input.mp3").write_bytes(
        b"synthetic unprocessed parity audio\n"
    )
    (data_srv / "synthetic-output.mp3").write_bytes(
        b"synthetic processed parity audio\n"
    )
    (data_in / "synthetic-transcript.json").write_text(
        '{"segments":[{"text":"synthetic"}]}\n'
    )

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "UPDATE post SET unprocessed_audio_path=?, processed_audio_path=? WHERE id=?",
            (
                "/app/src/instance/data/in/synthetic-input.mp3",
                "/app/src/instance/data/srv/synthetic-output.mp3",
                manifest.post_ids[0],
            ),
        )
        conn.commit()
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise AssertionError(
                "exported writer parity database violates foreign keys"
            )
    return manifest


def _make_backend(
    root: Path, name: str, source_db: Path, manifest: WriterParityManifest
) -> WriterParityBackend:
    backend_root = root / name
    instance_dir = backend_root / "instance"
    data_in = instance_dir / "data" / "in"
    data_srv = instance_dir / "data" / "srv"
    data_in.mkdir(parents=True)
    data_srv.mkdir(parents=True)
    db_path = instance_dir / "sqlite3.db"
    _clone_database(source_db, db_path)

    unprocessed = data_in / "synthetic-input.mp3"
    processed = data_srv / "synthetic-output.mp3"
    unprocessed.write_bytes(b"synthetic unprocessed parity audio\n")
    processed.write_bytes(b"synthetic processed parity audio\n")
    artifact = data_in / "synthetic-transcript.json"
    artifact.write_text('{"segments":[{"text":"synthetic"}]}\n')

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "UPDATE post SET unprocessed_audio_path=?, processed_audio_path=? WHERE id=?",
            (str(unprocessed), str(processed), manifest.post_ids[0]),
        )
        conn.commit()
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise AssertionError(f"{name} writer parity clone violates foreign keys")

    return WriterParityBackend(
        name=name,
        root=backend_root,
        instance_dir=instance_dir,
        db_path=db_path,
        database_uri=_sqlite_uri(db_path),
        data_in=data_in,
        data_srv=data_srv,
    )


@pytest.fixture(scope="session")
def writer_parity_snapshot(
    tmp_path_factory: pytest.TempPathFactory,
) -> WriterParitySnapshot:
    root = tmp_path_factory.mktemp("writer-parity-source")
    return build_writer_parity_snapshot(root)


@pytest.fixture
def writer_parity_pair(
    tmp_path: Path, writer_parity_snapshot: WriterParitySnapshot
) -> WriterParityPair:
    python = _make_backend(
        tmp_path,
        "python",
        writer_parity_snapshot.db_path,
        writer_parity_snapshot.manifest,
    )
    rust = _make_backend(
        tmp_path,
        "rust",
        writer_parity_snapshot.db_path,
        writer_parity_snapshot.manifest,
    )
    return WriterParityPair(
        source=writer_parity_snapshot,
        python=python,
        rust=rust,
        manifest=writer_parity_snapshot.manifest,
    )
