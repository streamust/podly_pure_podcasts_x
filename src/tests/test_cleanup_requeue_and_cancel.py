from datetime import UTC, datetime

from app.extensions import db
from app.models import Feed, ModelCall, Post, ProcessingJob
from app.writer.actions.cleanup import cleanup_missing_audio_paths_action
from app.writer.actions.jobs import (
    cancel_existing_jobs_action,
    mark_cancelled_action,
    update_job_status_action,
)
from app.writer.actions.processor import (
    delete_model_calls_for_post_by_model_name_action,
    upsert_model_call_action,
)


def _create_feed_and_post(app, *, guid="test-guid", audio_path="/tmp/nonexistent.mp3"):
    feed = Feed(
        title="Test Feed",
        description="desc",
        author="author",
        rss_url="https://example.com/feed.xml",
    )
    db.session.add(feed)
    db.session.commit()

    post = Post(
        guid=guid,
        title="Test Episode",
        download_url="https://example.com/ep.mp3",
        feed_id=feed.id,
        whitelisted=True,
        unprocessed_audio_path=audio_path,
    )
    db.session.add(post)
    db.session.commit()
    return feed, post


class TestCleanupRequeuesWhenAudioMissing:
    """Whitelisted posts with missing audio should be re-queued for reprocessing
    regardless of previous job status (except pending/running which are already active)."""

    def test_completed_job_requeued(self, app):
        with app.app_context():
            _, post = _create_feed_and_post(app)
            job = ProcessingJob(
                post_guid=post.guid,
                status="completed",
                completed_at=datetime.now(UTC).replace(tzinfo=None),
            )
            db.session.add(job)
            db.session.commit()

            cleanup_missing_audio_paths_action({})
            db.session.commit()
            db.session.refresh(job)

            assert job.status == "pending"
            assert job.step_name == "Not started"

    def test_failed_job_requeued(self, app):
        with app.app_context():
            _, post = _create_feed_and_post(app, guid="failed-guid")
            job = ProcessingJob(
                post_guid=post.guid,
                status="failed",
                error_message="some error",
                completed_at=datetime.now(UTC).replace(tzinfo=None),
            )
            db.session.add(job)
            db.session.commit()

            cleanup_missing_audio_paths_action({})
            db.session.commit()
            db.session.refresh(job)

            assert job.status == "pending"
            assert job.error_message is None
            assert job.step_name == "Not started"

    def test_pending_job_not_reset(self, app):
        with app.app_context():
            _, post = _create_feed_and_post(app, guid="pending-guid")
            job = ProcessingJob(
                post_guid=post.guid,
                status="pending",
                step_name="Queued",
            )
            db.session.add(job)
            db.session.commit()

            cleanup_missing_audio_paths_action({})
            db.session.commit()
            db.session.refresh(job)

            assert job.status == "pending"
            assert job.step_name == "Queued"


class TestMarkCancelledAction:
    def test_sets_step_name(self, app):
        with app.app_context():
            job = ProcessingJob(post_guid="cancel-test", status="running")
            db.session.add(job)
            db.session.commit()

            mark_cancelled_action({"job_id": job.id, "reason": "Duplicate episode"})
            db.session.commit()
            db.session.refresh(job)

            assert job.status == "cancelled"
            assert job.step_name == "Duplicate episode"
            assert job.error_message == "Duplicate episode"

    def test_default_reason_when_none(self, app):
        with app.app_context():
            job = ProcessingJob(post_guid="cancel-default", status="running")
            db.session.add(job)
            db.session.commit()

            mark_cancelled_action({"job_id": job.id})
            db.session.commit()
            db.session.refresh(job)

            assert job.status == "cancelled"
            assert job.step_name == "Cancelled by user request"
            assert job.error_message == "Cancelled by user request"

    def test_late_worker_update_cannot_resurrect_cancelled_job(self, app):
        with app.app_context():
            job = ProcessingJob(post_guid="cancel-race", status="running")
            db.session.add(job)
            db.session.commit()

            mark_cancelled_action({"job_id": job.id, "reason": "Stopped"})
            cancelled_at = job.completed_at
            result = update_job_status_action(
                {
                    "job_id": job.id,
                    "status": "completed",
                    "step": 4,
                    "step_name": "Completed",
                    "progress": 100.0,
                }
            )
            db.session.commit()
            db.session.refresh(job)

            assert result == {"job_id": job.id, "status": "cancelled"}
            assert job.status == "cancelled"
            assert job.step_name == "Stopped"
            assert job.error_message == "Stopped"
            assert job.completed_at == cancelled_at


class TestCancelExistingJobsCleansOrphanedModelCalls:
    """When a new processing job supersedes an in-flight one, any non-terminal
    ModelCall rows for the post must be marked cancelled. Otherwise they
    stay `pending` forever (visible in the UI as orphans) and collide with
    the new run's upsert against the unique
    (post_id, first_seq, last_seq, model_name) index.

    Regression: see app.log around 14:48-14:52 -- ModelCall 2515 was left
    pending after a tier-retry-backoff worker was killed by a new job, then
    a later upsert hit IntegrityError on the same key.
    """

    def _create_post(self, guid: str) -> Post:
        feed = Feed(
            title="Cancel Cleanup Feed",
            rss_url=f"https://example.com/{guid}.xml",
        )
        db.session.add(feed)
        db.session.flush()
        post = Post(
            feed_id=feed.id,
            guid=guid,
            download_url=f"https://example.com/{guid}.mp3",
            title=guid,
        )
        db.session.add(post)
        db.session.commit()
        return post

    def test_cancels_pending_model_calls_for_post(self, app):
        with app.app_context():
            post = self._create_post("orphan-pending-guid")
            old_job = ProcessingJob(post_guid=post.guid, status="running")
            db.session.add(old_job)
            db.session.commit()

            stuck = ModelCall(
                post_id=post.id,
                first_segment_sequence_num=2339,
                last_segment_sequence_num=2363,
                model_name="gemini/gemini-3-flash-preview",
                prompt="word boundary refine",
                status="pending",
            )
            succeeded = ModelCall(
                post_id=post.id,
                first_segment_sequence_num=0,
                last_segment_sequence_num=2363,
                model_name="gemini/gemini-3-flash-preview",
                prompt="classify",
                status="success",
                response="{}",
            )
            db.session.add_all([stuck, succeeded])
            db.session.commit()

            cancelled = cancel_existing_jobs_action(
                {"post_guid": post.guid, "current_job_id": "new-job-id"}
            )
            db.session.commit()

            assert cancelled == 1
            db.session.refresh(stuck)
            db.session.refresh(succeeded)
            assert stuck.status == "cancelled"
            assert stuck.error_message == "Superseded by a new processing job"
            # Terminal statuses are untouched.
            assert succeeded.status == "success"

    def test_upsert_resets_cancelled_row_back_to_pending(self, app):
        """After cancellation, the next run's upsert for the same chunk must
        reuse the cancelled row (clearing its error_message) instead of
        leaving the UI showing a stale `Superseded` message OR crashing on
        the unique-index collision.
        """
        with app.app_context():
            post = self._create_post("cancelled-then-reupsert-guid")
            existing = ModelCall(
                post_id=post.id,
                first_segment_sequence_num=2339,
                last_segment_sequence_num=2363,
                model_name="gemini/gemini-3-flash-preview",
                prompt="old prompt",
                status="cancelled",
                error_message="Superseded by a new processing job",
                response=None,
                retry_attempts=2,
            )
            db.session.add(existing)
            db.session.commit()

            result = upsert_model_call_action(
                {
                    "post_id": post.id,
                    "model_name": "gemini/gemini-3-flash-preview",
                    "first_segment_sequence_num": 2339,
                    "last_segment_sequence_num": 2363,
                    "prompt": "fresh prompt",
                }
            )
            db.session.commit()

            assert result == {"model_call_id": existing.id}
            db.session.refresh(existing)
            assert existing.status == "pending"
            assert existing.prompt == "fresh prompt"
            assert existing.retry_attempts == 0
            assert existing.error_message is None

    def test_upsert_resets_failed_permanent_row_for_a_later_job(self, app):
        """A corrected configuration must get a fresh provider attempt."""
        with app.app_context():
            post = self._create_post("failed-permanent-then-reupsert-guid")
            existing = ModelCall(
                post_id=post.id,
                first_segment_sequence_num=0,
                last_segment_sequence_num=24,
                model_name="gpt-5.6-luna",
                prompt="old prompt",
                status="failed_permanent",
                error_message="max_tokens is too large: 948576",
                response=None,
                retry_attempts=1,
            )
            db.session.add(existing)
            db.session.commit()

            result = upsert_model_call_action(
                {
                    "post_id": post.id,
                    "model_name": "gpt-5.6-luna",
                    "first_segment_sequence_num": 0,
                    "last_segment_sequence_num": 24,
                    "prompt": "fresh prompt",
                }
            )
            db.session.commit()

            assert result == {"model_call_id": existing.id}
            db.session.refresh(existing)
            assert existing.status == "pending"
            assert existing.prompt == "fresh prompt"
            assert existing.retry_attempts == 0
            assert existing.error_message is None
            assert existing.response is None

    def test_upsert_keeps_successful_row_reusable(self, app):
        with app.app_context():
            post = self._create_post("successful-then-reupsert-guid")
            existing = ModelCall(
                post_id=post.id,
                first_segment_sequence_num=0,
                last_segment_sequence_num=24,
                model_name="gpt-5.6-luna",
                prompt="successful prompt",
                status="success",
                response='{"ads": []}',
                retry_attempts=1,
            )
            db.session.add(existing)
            db.session.commit()

            result = upsert_model_call_action(
                {
                    "post_id": post.id,
                    "model_name": "gpt-5.6-luna",
                    "first_segment_sequence_num": 0,
                    "last_segment_sequence_num": 24,
                    "prompt": "new prompt that should not replace cached success",
                }
            )
            db.session.commit()

            assert result == {"model_call_id": existing.id}
            db.session.refresh(existing)
            assert existing.status == "success"
            assert existing.prompt == "successful prompt"
            assert existing.response == '{"ads": []}'


class TestDeleteModelCallsByPostAndModelName:
    """The INA re-run path depends on this action to wipe rows whose segment
    range will differ between runs (placeholder ``(0, 0)`` on the next run
    would otherwise collide with the prior run's ``(0, N-1)`` row on the
    final UPDATE)."""

    def _create_post(self, guid: str) -> Post:
        feed = Feed(
            title="Feed",
            description="d",
            author="a",
            rss_url=f"https://example.com/{guid}.xml",
        )
        db.session.add(feed)
        db.session.commit()
        post = Post(
            guid=guid,
            title="Episode",
            download_url=f"https://example.com/{guid}.mp3",
            feed_id=feed.id,
        )
        db.session.add(post)
        db.session.commit()
        return post

    def test_deletes_all_rows_for_post_and_model_name(self, app) -> None:
        with app.app_context():
            post = self._create_post("ina-cleanup-guid")
            # Prior run wrote the row at its real segment range.
            stale = ModelCall(
                post_id=post.id,
                first_segment_sequence_num=0,
                last_segment_sequence_num=629,
                model_name="ina:speech_music_noise",
                prompt="prior run",
                status="success",
                response="[...]",
            )
            # An unrelated model_call for the same post should NOT be touched.
            unrelated = ModelCall(
                post_id=post.id,
                first_segment_sequence_num=10,
                last_segment_sequence_num=20,
                model_name="gemini/gemini-3-flash-preview",
                prompt="ad classifier",
                status="success",
            )
            db.session.add_all([stale, unrelated])
            db.session.commit()
            unrelated_id = unrelated.id

            result = delete_model_calls_for_post_by_model_name_action(
                {
                    "post_id": post.id,
                    "model_name": "ina:speech_music_noise",
                }
            )
            db.session.commit()

            assert result == {"deleted": 1}
            remaining = db.session.query(ModelCall).filter_by(post_id=post.id).all()
            assert [mc.id for mc in remaining] == [unrelated_id]
