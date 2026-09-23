from __future__ import annotations

import os
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest
from sqlalchemy import event

import app.jobs_manager as jobs_manager_module
from app.extensions import db
from app.jobs_manager import JobsManager
from app.models import Feed, JobsManagerRun, Post, ProcessingJob


def _create_feed() -> Feed:
    feed = Feed(
        title="Test Feed",
        rss_url="https://example.com/feed.xml",
    )
    db.session.add(feed)
    db.session.commit()
    return feed


def _create_post(
    *,
    feed_id: int,
    guid: str,
    download_url: str,
    whitelisted: bool,
    processed_audio_path: str | None,
) -> Post:
    post = Post(
        feed_id=feed_id,
        guid=guid,
        download_url=download_url,
        title=guid,
        whitelisted=whitelisted,
        processed_audio_path=processed_audio_path,
    )
    db.session.add(post)
    db.session.commit()
    return post


def test_ensure_jobs_skips_whitelisted_posts_with_existing_processed_audio(
    app, monkeypatch, tmp_path: Path
) -> None:
    feed = _create_feed()

    processed_file = tmp_path / "processed.mp3"
    processed_file.write_bytes(b"test")

    processed_post = _create_post(
        feed_id=feed.id,
        guid="processed-guid",
        download_url="https://example.com/processed.mp3",
        whitelisted=True,
        processed_audio_path=str(processed_file),
    )
    unprocessed_post = _create_post(
        feed_id=feed.id,
        guid="unprocessed-guid",
        download_url="https://example.com/unprocessed.mp3",
        whitelisted=True,
        processed_audio_path=None,
    )
    _create_post(
        feed_id=feed.id,
        guid="not-whitelisted-guid",
        download_url="https://example.com/not-whitelisted.mp3",
        whitelisted=False,
        processed_audio_path=None,
    )

    created_guids: list[str] = []

    class FakeSingleJobManager:
        def __init__(self, post_guid: str, *_args, **_kwargs) -> None:
            self.post_guid = post_guid

        def ensure_job(self) -> None:
            created_guids.append(self.post_guid)

    monkeypatch.setattr(jobs_manager_module, "SingleJobManager", FakeSingleJobManager)

    manager = JobsManager.__new__(JobsManager)
    manager._status_manager = object()
    created = manager._ensure_jobs_for_all_posts("run-id")

    assert created == 1
    assert created_guids == [unprocessed_post.guid]
    assert processed_post.guid not in created_guids


def test_ensure_jobs_creates_job_when_processed_path_is_missing_file(
    app, monkeypatch, tmp_path: Path
) -> None:
    feed = _create_feed()

    missing_processed_path = tmp_path / "missing.mp3"
    post = _create_post(
        feed_id=feed.id,
        guid="missing-file-guid",
        download_url="https://example.com/missing-file.mp3",
        whitelisted=True,
        processed_audio_path=str(missing_processed_path),
    )

    created_guids: list[str] = []

    class FakeSingleJobManager:
        def __init__(self, post_guid: str, *_args, **_kwargs) -> None:
            self.post_guid = post_guid

        def ensure_job(self) -> None:
            created_guids.append(self.post_guid)

    monkeypatch.setattr(jobs_manager_module, "SingleJobManager", FakeSingleJobManager)

    manager = JobsManager.__new__(JobsManager)
    manager._status_manager = object()
    created = manager._ensure_jobs_for_all_posts("run-id")

    assert created == 1
    assert created_guids == [post.guid]


def test_ensure_jobs_repair_is_bounded_per_cycle(app, monkeypatch) -> None:
    """The repair path creates at most _REPAIR_JOB_CAP_PER_CYCLE jobs per call.

    Regression test for the 2026-09-23 incident where a mass-whitelist of
    1,277 posts produced 1,273 jobs in ~3 minutes.
    """
    feed = _create_feed()
    for index in range(8):
        _create_post(
            feed_id=feed.id,
            guid=f"cap-guid-{index}",
            download_url=f"https://example.com/cap-{index}.mp3",
            whitelisted=True,
            processed_audio_path=None,
        )

    created_guids: list[str] = []

    class FakeSingleJobManager:
        def __init__(self, post_guid: str, *_args, **_kwargs) -> None:
            self.post_guid = post_guid

        def ensure_job(self) -> None:
            created_guids.append(self.post_guid)

    monkeypatch.setattr(jobs_manager_module, "SingleJobManager", FakeSingleJobManager)

    manager = JobsManager.__new__(JobsManager)
    manager._status_manager = object()
    created = manager._ensure_jobs_for_all_posts("run-id")

    assert created == JobsManager._REPAIR_JOB_CAP_PER_CYCLE == 5
    assert len(created_guids) == 5
    # Newest posts are repaired first.
    assert created_guids[0] == "cap-guid-7"
    assert created_guids[-1] == "cap-guid-3"


def test_refresh_feed_in_short_context_starts_discovery_jobs(app, monkeypatch) -> None:
    """Guids returned by refresh_feed get discovery-time processing starts."""
    feed = _create_feed()

    monkeypatch.setattr(jobs_manager_module, "_scheduler_app_context", app.app_context)
    monkeypatch.setattr(
        jobs_manager_module, "refresh_feed", lambda _feed: ["guid-a", "guid-b"]
    )

    started: list[tuple[str, str]] = []
    manager = JobsManager.__new__(JobsManager)
    monkeypatch.setattr(
        manager,
        "start_post_processing",
        lambda guid, priority="interactive": started.append((guid, priority)),
    )

    manager._refresh_feed_in_short_context(feed.id)

    assert started == [("guid-a", "background"), ("guid-b", "background")]


def test_start_refresh_all_feeds_refreshes_each_feed_in_short_session(
    app, monkeypatch
) -> None:
    first_feed = _create_feed()
    second_feed = Feed(
        title="Second Feed",
        rss_url="https://example.com/second.xml",
    )
    db.session.add(second_feed)
    db.session.commit()

    refreshed_ids: list[int] = []

    def fake_refresh_feed(feed: Feed) -> None:
        feed_count_in_session = sum(
            isinstance(obj, Feed) for obj in db.session.identity_map.values()
        )
        assert feed_count_in_session == 1
        refreshed_ids.append(feed.id)

    monkeypatch.setattr(jobs_manager_module, "_scheduler_app_context", app.app_context)
    monkeypatch.setattr(jobs_manager_module, "refresh_feed", fake_refresh_feed)
    monkeypatch.setattr(
        jobs_manager_module, "release_memory_to_os", lambda *_args: None
    )

    manager = JobsManager.__new__(JobsManager)
    monkeypatch.setattr(manager, "_cleanup_inconsistent_posts", lambda: None)
    monkeypatch.setattr(
        manager,
        "enqueue_pending_jobs",
        lambda trigger, context: {"status": "ok", "trigger": trigger},
    )

    result = manager.start_refresh_all_feeds(trigger="scheduled")

    assert result == {"status": "ok", "trigger": "scheduled"}
    assert refreshed_ids == [first_feed.id, second_feed.id]


class FakeStatusManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int, str, float | None]] = []

    def update_job_status(
        self,
        job: ProcessingJob,
        status: str,
        step: int,
        step_name: str,
        progress: float | None = None,
    ) -> None:
        self.calls.append((job.id, status, step, step_name, progress))


class FakeProcess:
    pid = 1234

    def __init__(self, exit_code: int | None = 0) -> None:
        self.exit_code = exit_code
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.terminated = True
        self.exit_code = -15

    def kill(self) -> None:
        self.killed = True
        self.exit_code = -9

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self.exit_code if self.exit_code is not None else 0


def _create_job(post_guid: str, status: str = "running") -> ProcessingJob:
    job = ProcessingJob(
        id=f"job-{post_guid}",
        post_guid=post_guid,
        status=status,
        current_step=2,
        progress_percentage=50.0,
    )
    db.session.add(job)
    db.session.commit()
    return job


def _manager() -> tuple[JobsManager, FakeStatusManager]:
    status_manager = FakeStatusManager()
    manager = JobsManager.__new__(JobsManager)
    manager._status_manager = status_manager
    return manager, status_manager


def test_ensure_jobs_reads_only_scheduling_fields(app, monkeypatch, tmp_path):
    processed = tmp_path / "processed.mp3"
    processed.write_bytes(b"audio")
    feed = _create_feed()
    _create_post(
        feed_id=feed.id,
        guid="lightweight-schedule",
        download_url="https://example.com/audio.mp3",
        whitelisted=True,
        processed_audio_path=str(processed),
    )
    db.session.remove()
    statements = []

    def capture_sql(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    engine = db.engine
    event.listen(engine, "before_cursor_execute", capture_sql)
    try:
        manager, _ = _manager()
        assert manager._ensure_jobs_for_all_posts(None) == 0
    finally:
        event.remove(engine, "before_cursor_execute", capture_sql)

    assert len(statements) == 1  # Includes the feed title; no lazy Feed query.
    assert "transcript_word_timestamps" not in statements[0]
    assert "description" not in statements[0]


@pytest.mark.parametrize("pending_count", [0, 3])
def test_cleanup_counts_pending_jobs_without_loading_entities(
    app, monkeypatch, pending_count
):
    for index in range(pending_count):
        _create_job(f"pending-{index}", status="pending")
    _create_job("completed", status="completed")
    db.session.remove()
    manager, _ = _manager()
    monkeypatch.setattr(manager, "_ensure_jobs_for_all_posts", lambda run_id: 2)
    action = Mock()
    monkeypatch.setattr(jobs_manager_module.writer_client, "action", action)
    loaded = []

    def on_load(job, context):
        loaded.append(job.id)

    event.listen(ProcessingJob, "load", on_load)
    try:
        assert manager._cleanup_and_process_new_posts(JobsManagerRun(id="run-id")) == (
            2,
            pending_count,
        )
    finally:
        event.remove(ProcessingJob, "load", on_load)

    assert loaded == []
    if pending_count:
        action.assert_called_once_with(
            "reassign_pending_jobs", {"run_id": "run-id"}, wait=True
        )
    else:
        action.assert_not_called()


def test_dequeue_next_job_enters_app_context_for_writer_call(app, monkeypatch) -> None:
    """`_dequeue_next_job` must enter the scheduler app context before calling
    writer_client.action, because the worker loop runs in a daemon thread
    with no Flask context. Regression test for the "Error dequeuing job:
    Working outside of application context" log.
    """
    from threading import Lock

    manager = JobsManager.__new__(JobsManager)
    manager._run_lock = Lock()
    manager._run_id = "run-1"

    entered: list[bool] = []
    monkeypatch.setattr(
        jobs_manager_module,
        "_scheduler_app_context",
        lambda: (entered.append(True), app.app_context())[1],
    )

    class _Result:
        success = True
        data = {"job_id": "job-1", "post_guid": "guid-1"}

    def fake_action(action: str, payload: dict, wait: bool):
        import flask

        # The bug was: this raised "Working outside of application context".
        assert flask.has_app_context(), (
            f"writer_client.action({action!r}) must run inside an app context"
        )
        return _Result()

    monkeypatch.setattr(jobs_manager_module.writer_client, "action", fake_action)

    result = manager._dequeue_next_job()

    assert result == ("job-1", "guid-1")
    assert entered == [True]


def test_process_job_spawns_expected_processing_worker_command(
    app, monkeypatch
) -> None:
    job = _create_job("spawn-guid")
    job_id = job.id
    manager, status_manager = _manager()
    launched: dict[str, object] = {}

    def fake_popen(command, env=None):
        launched["command"] = command
        launched["env"] = env
        return FakeProcess(0)

    monkeypatch.setattr(jobs_manager_module, "_scheduler_app_context", nullcontext)
    monkeypatch.setattr(jobs_manager_module.subprocess, "Popen", fake_popen)

    manager._process_job(job_id, "spawn-guid")

    assert launched["command"] == [
        sys.executable,
        "-m",
        "app.processing_worker",
        "--job-id",
        job_id,
        "--post-guid",
        "spawn-guid",
    ]
    env = cast(dict[str, str], launched["env"])
    assert "PYTHONPATH" in env
    assert str(Path(jobs_manager_module.__file__).resolve().parents[1]) in env[
        "PYTHONPATH"
    ].split(os.pathsep)
    assert status_manager.calls == []


def test_nonzero_processing_worker_exit_marks_nonterminal_job_failed(
    app, monkeypatch
) -> None:
    job = _create_job("nonzero-guid", status="running")
    job_id = job.id
    manager, status_manager = _manager()

    monkeypatch.setattr(jobs_manager_module, "_scheduler_app_context", nullcontext)
    monkeypatch.setattr(
        jobs_manager_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: FakeProcess(7),
    )

    manager._process_job(job_id, "nonzero-guid")

    assert status_manager.calls == [
        (
            job_id,
            "failed",
            2,
            "Processing worker exited with status 7",
            50.0,
        )
    ]


def test_nonzero_processing_worker_exit_leaves_terminal_job_alone(
    app, monkeypatch
) -> None:
    job = _create_job("terminal-guid", status="completed")
    job_id = job.id
    manager, status_manager = _manager()

    monkeypatch.setattr(jobs_manager_module, "_scheduler_app_context", nullcontext)
    monkeypatch.setattr(
        jobs_manager_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: FakeProcess(7),
    )

    manager._process_job(job_id, "terminal-guid")

    assert status_manager.calls == []


def test_cancelled_job_terminates_processing_worker(monkeypatch) -> None:
    manager, _status_manager = _manager()
    process = FakeProcess(None)
    statuses = iter([None, "cancelled"])

    monkeypatch.setattr(manager, "_job_status", lambda _job_id: next(statuses))
    monkeypatch.setattr(jobs_manager_module.time, "sleep", lambda _seconds: None)

    exit_code = manager._wait_for_processing_worker(
        process, "cancel-job", "cancel-guid"
    )

    assert exit_code == -15
    assert process.terminated is True
    assert process.killed is False


def test_process_job_trims_web_memory_after_worker_exit(app, monkeypatch) -> None:
    job = _create_job("trim-guid")
    job_id = job.id
    manager, _status_manager = _manager()
    trim_contexts: list[str] = []

    monkeypatch.setattr(jobs_manager_module, "_scheduler_app_context", nullcontext)
    monkeypatch.setattr(
        jobs_manager_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: FakeProcess(0),
    )
    monkeypatch.setattr(
        jobs_manager_module,
        "release_memory_to_os",
        lambda context, _logger: trim_contexts.append(context),
    )

    manager._process_job(job_id, "trim-guid")

    assert trim_contexts == [f"web supervisor after processing job {job_id}"]


def test_build_tier_summary_never_reports_attempt_zero() -> None:
    summary = jobs_manager_module._build_tier_summary(
        {
            "label": "flex",
            "latest": "flex",
            "latest_status": "pending",
            "latest_attempt": 0,
            "latest_next_retry_at": None,
            "tiers": {"flex"},
        },
        5,
    )
    in_flight = summary["in_flight"]
    # A pending row means an attempt is underway; never render "attempt 0".
    assert in_flight["attempt"] == 1
    assert in_flight["status"] == "pending"
    assert "backoff_until" not in in_flight


def test_build_tier_summary_includes_backoff_deadline_when_retrying() -> None:
    from datetime import datetime

    summary = jobs_manager_module._build_tier_summary(
        {
            "label": "flex",
            "latest": "flex",
            "latest_status": "retrying",
            "latest_attempt": 2,
            "latest_next_retry_at": datetime(2026, 6, 10, 12, 0, 30),
            "tiers": {"flex"},
        },
        5,
    )
    in_flight = summary["in_flight"]
    assert in_flight["status"] == "retrying"
    assert in_flight["attempt"] == 2
    assert in_flight["backoff_until"] == "2026-06-10T12:00:30Z"


def test_build_tier_summary_names_chapter_calls() -> None:
    summary = jobs_manager_module._build_tier_summary(
        {
            "label": "flex",
            "latest": "flex",
            "latest_status": "pending",
            "latest_attempt": 1,
            "latest_next_retry_at": None,
            "latest_call_label": jobs_manager_module._in_flight_call_label(-200, -200),
            "tiers": {"flex"},
        },
        5,
    )
    assert summary["in_flight"]["call_label"] == "chapter topic plan"

    # Real segment ranges (classification/boundary calls) get no label — the
    # stage caption already says what's happening.
    assert jobs_manager_module._in_flight_call_label(0, 1890) is None
    assert jobs_manager_module._in_flight_call_label(-100, -100) == "chapter titles"
