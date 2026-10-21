import logging
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any, cast

from sqlalchemy import case
from sqlalchemy.orm import joinedload, load_only

from app.db_guard import db_guard, reset_session
from app.extensions import db as _db
from app.extensions import scheduler
from app.feeds import refresh_feed
from app.job_manager import JobManager as SingleJobManager
from app.memory_pressure import release_memory_to_os
from app.models import Feed, JobsManagerRun, ModelCall, Post, ProcessingJob
from app.writer.client import writer_client
from podcast_processor.processing_status_manager import ProcessingStatusManager
from shared.processing_paths import find_existing_processed_audio_path

logger = logging.getLogger("global_logger")
TERMINAL_JOB_STATUSES = {"completed", "failed", "cancelled", "skipped"}


def _scheduler_app_context() -> Any:
    scheduler_app = scheduler.app
    if scheduler_app is None:
        raise RuntimeError("Scheduler app is not initialized")
    return scheduler_app.app_context()


_TIER_IN_FLIGHT_STATUSES = ("pending", "retrying")


def _summarize_service_tiers_for_posts(
    post_ids: list[int],
) -> dict[int, dict[str, Any]]:
    """Bulk variant of `_summarize_service_tier_for_post` for the jobs-list
    endpoints. Returns {post_id: {label, latest, mixed, in_flight?}} only for
    posts that have at least one ModelCall row with a non-null service_tier.

    `in_flight` is included only when the most recent tiered ModelCall for the
    post is in a non-terminal state (pending or retrying). The frontend uses
    it to render text like "Flex Processing (Attempt 2/5)" beside the chip.
    """
    if not post_ids:
        return {}
    rows = (
        ModelCall.query.with_entities(
            ModelCall.post_id,
            ModelCall.service_tier,
            ModelCall.status,
            ModelCall.retry_attempts,
            ModelCall.next_retry_at,
            ModelCall.first_segment_sequence_num,
            ModelCall.last_segment_sequence_num,
            ModelCall.timestamp,
        )
        .filter(
            ModelCall.post_id.in_(post_ids),
            ModelCall.service_tier.isnot(None),
        )
        .order_by(ModelCall.timestamp.desc())
        .all()
    )
    max_retries = _resolve_max_retries()
    per_post: dict[int, dict[str, Any]] = {}
    for (
        post_id,
        tier,
        status,
        retry_attempts,
        next_retry_at,
        first_seq,
        last_seq,
        _ts,
    ) in rows:
        bucket = per_post.setdefault(
            post_id,
            {
                "label": tier,
                "latest": tier,
                "latest_status": status,
                "latest_attempt": retry_attempts or 0,
                "latest_next_retry_at": next_retry_at,
                "latest_call_label": _in_flight_call_label(first_seq, last_seq),
                "tiers": set(),
            },
        )
        bucket["tiers"].add(tier)
    return {
        pid: _build_tier_summary(bucket, max_retries)
        for pid, bucket in per_post.items()
    }


def _summarize_service_tier_for_post(post_id: int) -> dict[str, Any] | None:
    """Build a small tier summary for the per-episode status endpoint. See
    `_summarize_service_tiers_for_posts` for the shape; returns None when the
    post has no tiered ModelCall rows yet.
    """
    summary = _summarize_service_tiers_for_posts([post_id])
    return summary.get(post_id)


def _in_flight_call_label(first_seq: Any, last_seq: Any) -> str | None:
    """Short human label for what kind of LLM call is in flight.

    Chapter-phase calls use sentinel segment ranges (see chapter_fallback.py);
    naming them keeps the jobs caption sensible when an LLM call is awaiting a
    response during a stage like "Processing audio" (chapters are generated
    inside that stage). Real segment ranges are classification/boundary calls
    where the stage label already says what's happening, so return None.
    """
    try:
        key = (int(first_seq), int(last_seq))
    except Exception:  # noqa: BLE001
        return None
    return {
        (-100, -100): "chapter titles",
        (-200, -200): "chapter topic plan",
        (-201, -201): "chapter topic plan",
    }.get(key)


def _build_tier_summary(
    bucket: dict[str, Any], max_retries: int | None
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "label": bucket["latest"],
        "latest": bucket["latest"],
        "mixed": len(bucket["tiers"]) > 1,
    }
    latest_status = bucket["latest_status"]
    if latest_status in _TIER_IN_FLIGHT_STATUSES:
        in_flight: dict[str, Any] = {
            "status": latest_status,
            # retry_attempts is 0 until the first attempt's bump lands (and some
            # call paths only set it on completion), but a pending/retrying row
            # always means an attempt is underway — never show "attempt 0".
            "attempt": max(1, int(bucket["latest_attempt"] or 0)),
        }
        if max_retries is not None:
            in_flight["max_retries"] = max_retries
        next_retry_at = bucket.get("latest_next_retry_at")
        if latest_status == "retrying" and next_retry_at is not None:
            # Naive UTC in the DB; mark it explicitly so the frontend's Date
            # parse doesn't reinterpret it in the browser's local zone.
            iso = next_retry_at.isoformat()
            in_flight["backoff_until"] = iso if iso.endswith("Z") else f"{iso}Z"
        call_label = bucket.get("latest_call_label")
        if call_label:
            in_flight["call_label"] = call_label
        summary["in_flight"] = in_flight
    return summary


def _resolve_max_retries() -> int | None:
    """Look up the configured llm_max_retry_attempts so the UI can render
    \"Attempt N/M\". Returns None if the runtime config can't be read."""
    try:
        from app.runtime_config import config as runtime_config

        value = getattr(runtime_config, "llm_max_retry_attempts", None)
        return int(value) if value is not None else None
    except Exception:  # noqa: BLE001
        return None


def _env_float(name: str, default: float, *, minimum: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using %.2f", name, raw, default)
        return default
    return max(minimum, value)


class JobsManager:
    """
    Centralized manager for starting, tracking, listing, and cancelling
    podcast processing jobs.

    Owns a shared worker pool and coordinates with ProcessingStatusManager.
    """

    # Class-level lock to ensure only one job processes at a time across ALL instances
    _global_processing_lock = Lock()

    def __init__(self) -> None:
        # Status manager for DB interactions
        self._status_manager = ProcessingStatusManager(
            db_session=_db.session, logger=logger
        )

        # Track the singleton run id with thread-safe access
        self._run_lock = Lock()
        self._run_id: str | None = None

        # Persistent worker thread coordination
        self._stop_event = Event()
        self._work_event = Event()
        self._worker_thread = Thread(
            target=self._worker_loop, name="jobs-manager-worker", daemon=True
        )
        self._worker_thread.start()

        # Initialize run via writer
        with _scheduler_app_context():
            try:
                result = writer_client.action(
                    "ensure_active_run",
                    {"trigger": "startup", "context": {"source": "init"}},
                    wait=True,
                )
                if result and result.success and result.data:
                    self._set_run_id(result.data["run_id"])
            except Exception as e:  # noqa: BLE001
                logger.error(f"Failed to initialize run: {e}")

    def _set_run_id(self, run_id: str | None) -> None:
        with self._run_lock:
            self._run_id = run_id

    def _get_run_id(self) -> str | None:
        with self._run_lock:
            return self._run_id

    def _wake_worker(self) -> None:
        self._work_event.set()

    def _wait_for_work(self, timeout: float = 5.0) -> None:
        triggered = self._work_event.wait(timeout)
        if triggered:
            self._work_event.clear()

    # ------------------------ Public API ------------------------
    def start_post_processing(
        self,
        post_guid: str,
        priority: str = "interactive",
        *,
        requested_by_user_id: int | None = None,
        billing_user_id: int | None = None,
    ) -> dict[str, Any]:
        """
        Idempotently start processing for a post. If an active job exists, return it.
        """
        with _scheduler_app_context():
            ensure_result = writer_client.action(
                "ensure_active_run",
                {
                    "trigger": "interactive_start",
                    "context": {"post_guid": post_guid, "priority": priority},
                },
                wait=True,
            )
            run_id = None
            if ensure_result and ensure_result.success and ensure_result.data:
                run_id = ensure_result.data.get("run_id")
            self._set_run_id(run_id)
            start_result = SingleJobManager(
                post_guid,
                self._status_manager,
                logger,
                run_id,
                requested_by_user_id=requested_by_user_id,
                billing_user_id=billing_user_id,
            ).start_processing(priority)
        if start_result.get("status") in {"started", "running"}:
            self._wake_worker()
        return start_result

    def enqueue_pending_jobs(
        self,
        trigger: str = "system",
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Ensure all posts have job records and enqueue pending work.

        Returns basic stats for logging/monitoring.
        """
        with _scheduler_app_context():
            result = writer_client.action(
                "ensure_active_run", {"trigger": trigger, "context": context}, wait=True
            )

            run_id = None
            if result and result.success and result.data:
                run_id = result.data["run_id"]
            self._set_run_id(run_id)

            active_run = _db.session.get(JobsManagerRun, run_id) if run_id else None

            created_count, pending_count = self._cleanup_and_process_new_posts(
                active_run
            )

            response = {
                "status": "ok",
                "created": created_count,
                "pending": pending_count,
                "enqueued": pending_count,
                "run_id": run_id,
            }
        if pending_count:
            self._wake_worker()
        return response

    # Bound on repair jobs created per cycle. 2026-09-23: the feed page's
    # "Enable all episodes" toggle whitelisted all 1,277 Odd Lots posts and the
    # unbounded repair below created 1,273 jobs in ~3 minutes. New episodes now
    # get jobs at discovery time (see refresh_feed), so this path is only a
    # safety net for posts orphaned without a job row (e.g. startup
    # clear_active_jobs killed an in-flight episode). Five per cycle drains a
    # handful of orphans quickly while a mass-whitelist can never again become
    # a mass-queue.
    _REPAIR_JOB_CAP_PER_CYCLE = 5

    def _ensure_jobs_for_all_posts(self, run_id: str | None) -> int:
        """Repair whitelisted posts left without a job, bounded per cycle.

        This is a repair path only: new episodes get jobs at discovery time
        via :func:`refresh_feed`, so this exists to recover whitelisted posts
        orphaned without a job row (e.g. startup ``clear_active_jobs``
        deleted a pending job for an in-flight episode). Creates at most
        ``_REPAIR_JOB_CAP_PER_CYCLE`` jobs per call so a mass-whitelist can
        never again turn into a mass-queue; leftovers are repaired in later
        cycles, newest first.
        """
        posts_without_jobs = (
            Post.query.outerjoin(ProcessingJob, ProcessingJob.post_guid == Post.guid)
            .options(
                load_only(
                    Post.id,
                    Post.guid,
                    Post.feed_id,
                    Post.title,
                    Post.processed_audio_path,
                    Post.unprocessed_audio_path,
                ),
                joinedload(Post.feed).load_only(Feed.title),
            )
            .filter(ProcessingJob.id.is_(None), Post.whitelisted.is_(True))
            .order_by(Post.id.desc())
            .all()
        )

        created = 0
        for index, post in enumerate(posts_without_jobs):
            if created >= self._REPAIR_JOB_CAP_PER_CYCLE:
                logger.warning(
                    "Bounded job repair hit its cap of %d; %d whitelisted "
                    "posts still have no job and will be repaired in later "
                    "cycles",
                    self._REPAIR_JOB_CAP_PER_CYCLE,
                    len(posts_without_jobs) - index,
                )
                break
            # Avoid recreating jobs for posts that already have processed audio.
            existing_processed_path = find_existing_processed_audio_path(
                processed_audio_path=post.processed_audio_path,
                unprocessed_audio_path=post.unprocessed_audio_path,
                feed_title=getattr(post.feed, "title", None),
                post_title=post.title,
            )
            if existing_processed_path:
                processed_path_str = str(existing_processed_path)
                if post.processed_audio_path != processed_path_str:
                    result = writer_client.update(
                        "Post",
                        post.id,
                        {"processed_audio_path": processed_path_str},
                        wait=True,
                    )
                    if not result or not result.success:
                        logger.warning(
                            "Failed to update recovered processed path for post %s",
                            post.guid,
                        )
                continue

            SingleJobManager(
                post.guid,
                self._status_manager,
                logger,
                run_id,
            ).ensure_job()
            created += 1
        return created

    def get_post_status(self, post_guid: str) -> dict[str, Any]:
        with _scheduler_app_context():
            post = Post.query.filter_by(guid=post_guid).first()
            if not post:
                return {
                    "status": "error",
                    "error_code": "NOT_FOUND",
                    "message": "Post not found",
                }

            job = (
                ProcessingJob.query.filter_by(post_guid=post_guid)
                .order_by(ProcessingJob.created_at.desc())
                .first()
            )

            if not job:
                existing_processed_path = find_existing_processed_audio_path(
                    processed_audio_path=post.processed_audio_path,
                    unprocessed_audio_path=post.unprocessed_audio_path,
                    feed_title=getattr(post.feed, "title", None),
                    post_title=post.title,
                )
                if existing_processed_path:
                    return {
                        "status": "skipped",
                        "step": 4,
                        "step_name": "Processing skipped",
                        "total_steps": 4,
                        "progress_percentage": 100.0,
                        "message": "Post already processed",
                        "download_url": f"/api/posts/{post_guid}/download",
                    }
                return {
                    "status": "not_started",
                    "step": 0,
                    "step_name": "Not started",
                    "total_steps": 4,
                    "progress_percentage": 0.0,
                    "message": "No processing job found",
                }

            response = {
                "status": job.status,
                "step": job.current_step,
                "step_name": job.step_name or "Unknown",
                "total_steps": job.total_steps,
                "progress_percentage": job.progress_percentage,
                "message": job.step_name
                or f"Step {job.current_step} of {job.total_steps}",
                # Exposed so EpisodeProcessingStatus can render the same per-stage
                # duration row that the JobsPage card already shows (Queue 4s /
                # Download 2s / ...). Empty list when the job predates stage-
                # history capture.
                "stage_history": job.stage_history or [],
                "completed_at": (
                    job.completed_at.isoformat() if job.completed_at else None
                ),
            }
            tier_info = _summarize_service_tier_for_post(post.id)
            if tier_info is not None:
                response["service_tier"] = tier_info
            if job.started_at:
                response["started_at"] = job.started_at.isoformat()
            if job.status in {
                "completed",
                "skipped",
            } and find_existing_processed_audio_path(
                processed_audio_path=post.processed_audio_path,
                unprocessed_audio_path=post.unprocessed_audio_path,
                feed_title=getattr(post.feed, "title", None),
                post_title=post.title,
            ):
                response["download_url"] = f"/api/posts/{post_guid}/download"
            if job.status == "failed" and job.error_message:
                response["error"] = job.error_message
            if job.status == "cancelled" and job.error_message:
                response["message"] = job.error_message
                response["step_name"] = job.error_message
            return response

    def get_job_status(self, job_id: str) -> dict[str, Any]:
        with _scheduler_app_context():
            job = _db.session.get(ProcessingJob, job_id)
            if not job:
                return {
                    "status": "error",
                    "error_code": "NOT_FOUND",
                    "message": "Job not found",
                }
            return {
                "job_id": job.id,
                "post_guid": job.post_guid,
                "status": job.status,
                "step": job.current_step,
                "step_name": job.step_name,
                "total_steps": job.total_steps,
                "progress_percentage": job.progress_percentage,
                "started_at": job.started_at.isoformat() if job.started_at else None,
                "completed_at": (
                    job.completed_at.isoformat() if job.completed_at else None
                ),
                "error": job.error_message,
            }

    def list_active_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        with _scheduler_app_context():
            # Derive a simple priority from status: running > pending
            priority_order = case(
                (ProcessingJob.status == "running", 2),
                (ProcessingJob.status == "pending", 1),
                else_=0,
            ).label("priority")

            rows = (
                _db.session.query(ProcessingJob, Post, priority_order)
                .outerjoin(Post, ProcessingJob.post_guid == Post.guid)
                .filter(ProcessingJob.status.in_(["pending", "running"]))
                .order_by(priority_order.desc(), ProcessingJob.created_at.desc())
                .limit(limit)
                .all()
            )

            tier_by_post = _summarize_service_tiers_for_posts(
                [post.id for _job, post, _prio in rows if post is not None]
            )
            results: list[dict[str, Any]] = []
            for job, post, prio in rows:
                entry: dict[str, Any] = {
                    "job_id": job.id,
                    "post_guid": job.post_guid,
                    "post_title": post.title if post else None,
                    "feed_title": post.feed.title if post and post.feed else None,
                    "status": job.status,
                    "priority": int(prio) if prio is not None else 0,
                    "step": job.current_step,
                    "step_name": job.step_name,
                    "total_steps": job.total_steps,
                    "progress_percentage": job.progress_percentage,
                    "created_at": (
                        job.created_at.isoformat() if job.created_at else None
                    ),
                    "started_at": (
                        job.started_at.isoformat() if job.started_at else None
                    ),
                    "completed_at": (
                        job.completed_at.isoformat() if job.completed_at else None
                    ),
                    "error_message": job.error_message,
                    "stage_history": job.stage_history or [],
                    "ad_windows_count": job.ad_windows_count,
                    "had_classification_parse_error": bool(
                        job.had_classification_parse_error
                    ),
                    "auto_retry_attempted": bool(job.auto_retry_attempted),
                }
                if post is not None and post.id in tier_by_post:
                    entry["service_tier"] = tier_by_post[post.id]
                results.append(entry)

            return results

    def list_all_jobs_detailed(self, limit: int = 200) -> list[dict[str, Any]]:
        with _scheduler_app_context():
            # Priority by status, others ranked lowest
            priority_order = case(
                (ProcessingJob.status == "running", 2),
                (ProcessingJob.status == "pending", 1),
                else_=0,
            ).label("priority")

            rows = (
                _db.session.query(ProcessingJob, Post, priority_order)
                .outerjoin(Post, ProcessingJob.post_guid == Post.guid)
                .order_by(priority_order.desc(), ProcessingJob.created_at.desc())
                .limit(limit)
                .all()
            )

            tier_by_post = _summarize_service_tiers_for_posts(
                [post.id for _job, post, _prio in rows if post is not None]
            )
            results: list[dict[str, Any]] = []
            for job, post, prio in rows:
                entry: dict[str, Any] = {
                    "job_id": job.id,
                    "post_guid": job.post_guid,
                    "post_title": post.title if post else None,
                    "feed_title": post.feed.title if post and post.feed else None,
                    "status": job.status,
                    "priority": int(prio) if prio is not None else 0,
                    "step": job.current_step,
                    "step_name": job.step_name,
                    "total_steps": job.total_steps,
                    "progress_percentage": job.progress_percentage,
                    "created_at": (
                        job.created_at.isoformat() if job.created_at else None
                    ),
                    "started_at": (
                        job.started_at.isoformat() if job.started_at else None
                    ),
                    "completed_at": (
                        job.completed_at.isoformat() if job.completed_at else None
                    ),
                    "error_message": job.error_message,
                    "stage_history": job.stage_history or [],
                    "ad_windows_count": job.ad_windows_count,
                    "had_classification_parse_error": bool(
                        job.had_classification_parse_error
                    ),
                    "auto_retry_attempted": bool(job.auto_retry_attempted),
                }
                if post is not None and post.id in tier_by_post:
                    entry["service_tier"] = tier_by_post[post.id]
                results.append(entry)

            return results

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        with _scheduler_app_context():
            job = _db.session.get(ProcessingJob, job_id)
            if not job:
                return {
                    "status": "error",
                    "error_code": "NOT_FOUND",
                    "message": "Job not found",
                }

            if job.status in ["completed", "failed", "cancelled", "skipped"]:
                return {
                    "status": "error",
                    "error_code": "ALREADY_FINISHED",
                    "message": f"Job already {job.status}",
                }

            # Mark job as cancelled in database
            self._status_manager.mark_cancelled(job_id, "Cancelled by user request")

            return {
                "status": "cancelled",
                "job_id": job_id,
                "message": "Job cancelled",
            }

    def cancel_post_jobs(self, post_guid: str) -> dict[str, Any]:
        with _scheduler_app_context():
            # Find active jobs for this post in database
            active_jobs = (
                ProcessingJob.query.filter_by(post_guid=post_guid)
                .filter(ProcessingJob.status.in_(["pending", "running"]))
                .all()
            )

            job_ids = [job.id for job in active_jobs]
            for job in active_jobs:
                self._status_manager.mark_cancelled(job.id, "Cancelled by user request")

            return {
                "status": "cancelled",
                "post_guid": post_guid,
                "job_ids": job_ids,
                "message": f"Cancelled {len(job_ids)} jobs",
            }

    def cancel_queued_jobs(self) -> dict[str, Any]:
        """Cancel all queued (pending) jobs."""
        with _scheduler_app_context():
            queued_jobs = (
                ProcessingJob.query.filter(ProcessingJob.status == "pending")
                .order_by(ProcessingJob.created_at.asc())
                .all()
            )

            cancelled_job_ids: list[str] = []
            for job in queued_jobs:
                self._status_manager.mark_cancelled(job.id, "Cancelled by user request")
                cancelled_job_ids.append(job.id)

            return {
                "status": "cancelled",
                "cancelled_count": len(cancelled_job_ids),
                "message": f"Cancelled {len(cancelled_job_ids)} queued jobs",
            }

    def cleanup_stale_jobs(self, older_than: timedelta) -> int:
        try:
            result = writer_client.action(
                "cleanup_stale_jobs",
                {"older_than_seconds": older_than.total_seconds()},
                wait=True,
            )
            if result and result.success and result.data:
                return cast(int, result.data.get("count", 0))
            return 0
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to cleanup stale jobs: {e}")
            return 0

    def cleanup_stuck_pending_jobs(self, stuck_threshold_minutes: int = 10) -> int:
        """
        Clean up jobs that have been stuck in 'pending' status for too long.
        This indicates they were never picked up by the thread pool.
        """
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
            minutes=stuck_threshold_minutes
        )
        with _scheduler_app_context():
            stuck_jobs = ProcessingJob.query.filter(
                ProcessingJob.status == "pending", ProcessingJob.created_at < cutoff
            ).all()

            count = len(stuck_jobs)
            for job in stuck_jobs:
                try:
                    logger.warning(
                        f"Marking stuck pending job {job.id} as failed (created at {job.created_at})"
                    )
                    self._status_manager.update_job_status(
                        job,
                        "failed",
                        job.current_step,
                        f"Job was stuck in pending status for over {stuck_threshold_minutes} minutes",
                    )
                except Exception as e:  # noqa: BLE001
                    logger.error(f"Failed to update stuck job {job.id}: {e}")

            return count

    def clear_all_jobs(self) -> dict[str, Any]:
        """
        Clear all processing jobs from the database.
        This is typically called during application startup to ensure a clean state.
        """
        try:
            result = writer_client.action("clear_all_jobs", {}, wait=True)
            count = result.data if result and result.success else 0
            logger.info(f"Cleared {count} processing jobs on startup")
            return {
                "status": "success",
                "cleared_jobs": count,
                "message": f"Cleared {count} jobs from database",
            }
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error clearing all jobs: {e}")
            return {"status": "error", "message": f"Failed to clear jobs: {e!s}"}

    def clear_active_jobs(self) -> dict[str, Any]:
        """
        Clear only pending and running jobs on startup.
        Completed, failed, skipped, and cancelled jobs are preserved for history.
        """
        try:
            result = writer_client.action("clear_active_jobs", {}, wait=True)
            count = result.data if result and result.success else 0
            logger.info(f"Cleared {count} active (pending/running) jobs on startup")
            return {
                "status": "success",
                "cleared_jobs": count,
                "message": f"Cleared {count} active jobs from database",
            }
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error clearing active jobs: {e}")
            return {"status": "error", "message": f"Failed to clear active jobs: {e!s}"}

    def start_refresh_all_feeds(
        self,
        trigger: str = "scheduled",
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Refresh feeds and enqueue per-post processing into internal worker pool.
        """
        with _scheduler_app_context():
            feed_ids = [feed_id for (feed_id,) in _db.session.query(Feed.id).all()]

        for feed_id in feed_ids:
            self._refresh_feed_in_short_context(feed_id)

        # Clean up posts with missing audio files
        self._cleanup_inconsistent_posts()

        # Process new posts
        return self.enqueue_pending_jobs(trigger=trigger, context=context)

    # ------------------------ Helpers ------------------------
    def _refresh_feed_in_short_context(self, feed_id: int) -> None:
        """Refresh one feed without keeping the whole scheduled batch in memory.

        Newly discovered whitelisted posts get a processing job at discovery
        time (via :meth:`start_post_processing`, which is idempotent when an
        active job already exists) instead of waiting for a later sweep.
        """
        new_whitelisted_guids: list[str] = []
        with _scheduler_app_context():
            try:
                feed = _db.session.get(Feed, feed_id)
                if feed is None:
                    logger.warning(
                        "Skipping missing feed during refresh: id=%s", feed_id
                    )
                    return
                new_whitelisted_guids = refresh_feed(feed) or []
            finally:
                try:
                    _db.session.expunge_all()
                except Exception as exc:
                    logger.debug(
                        "Failed to expunge session after feed refresh id=%s: %s",
                        feed_id,
                        exc,
                        exc_info=True,
                    )
                try:
                    _db.session.remove()
                except Exception as exc:
                    logger.debug(
                        "Failed to remove session after feed refresh id=%s: %s",
                        feed_id,
                        exc,
                        exc_info=True,
                    )
                # refresh_feed requests one trim at app-context teardown, once
                # both its payloads and this session have been released.

        for guid in new_whitelisted_guids:
            try:
                self.start_post_processing(guid, priority="background")
            except Exception as exc:
                logger.error(
                    "Failed to start discovery-time processing for post %s: %s",
                    guid,
                    exc,
                    exc_info=True,
                )

    def _cleanup_inconsistent_posts(self) -> None:
        """Clean up posts with missing audio files."""
        try:
            writer_client.action("cleanup_missing_audio_paths", {}, wait=True)
        except Exception as e:
            logger.error(
                f"Failed to cleanup inconsistent posts: {e}",
                exc_info=True,
            )

    def _cleanup_and_process_new_posts(
        self, active_run: JobsManagerRun | None
    ) -> tuple[int, int]:
        """Ensure all posts have jobs and return counts for monitoring."""
        run_id = active_run.id if active_run else None
        created_jobs = self._ensure_jobs_for_all_posts(run_id)

        pending_count = ProcessingJob.query.filter(
            ProcessingJob.status == "pending"
        ).count()

        if active_run and pending_count:
            try:
                writer_client.action(
                    "reassign_pending_jobs", {"run_id": run_id}, wait=True
                )
            except Exception as e:  # noqa: BLE001
                logger.error("Failed to reassign pending jobs: %s", e)

        if created_jobs:
            logger.info("Created %s new job records", created_jobs)

        logger.info(
            "Pending jobs ready for worker: count=%s run_id=%s",
            pending_count,
            run_id,
        )

        return created_jobs, pending_count

    # Removed _get_active_job_for_guid - now using direct database queries

    # ------------------------ Internal helpers ------------------------

    def _dequeue_next_job(self) -> tuple[str, str] | None:
        """Return the next pending job id and post guid, or None if idle.

        CRITICAL: This method atomically marks the job as "running" when dequeuing
        to prevent race conditions where multiple jobs could be dequeued before
        any is marked as running.
        """
        # writer_client.action requires an active Flask app context. The
        # worker loop runs in a daemon thread that has none by default, and
        # every other writer_client.action call site in this file wraps in
        # _scheduler_app_context() -- wrap here too so callers don't have to.
        try:
            with _scheduler_app_context():
                run_id = self._get_run_id()
                result = writer_client.action(
                    "dequeue_job", {"run_id": run_id}, wait=True
                )

                if result and result.success and result.data:
                    job_id = result.data["job_id"]
                    post_guid = result.data["post_guid"]

                    logger.info(
                        "[JOB_DEQUEUE] Successfully dequeued and marked running: job_id=%s post_guid=%s",
                        job_id,
                        post_guid,
                    )
                    return job_id, post_guid

                return None
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error dequeuing job: {e}")
            return None

    def _worker_loop(self) -> None:
        """Background loop that continuously processes pending jobs.

        CRITICAL: This runs in a single dedicated daemon thread. Combined with
        the _global_processing_lock in _process_job, this ensures truly sequential
        job execution with no parallelism.
        """
        import threading

        logger.info(
            "[WORKER_LOOP] Started single worker thread: thread_name=%s thread_id=%s",
            threading.current_thread().name,
            threading.current_thread().ident,
        )
        while not self._stop_event.is_set():
            try:
                job_details = self._dequeue_next_job()
                if not job_details:
                    self._wait_for_work()
                    continue
                job_id, post_guid = job_details
                self._process_job(job_id, post_guid)
            except Exception as exc:
                logger.error("Worker loop error: %s", exc, exc_info=True)
                reset_session(_db.session, logger, "worker_loop_exception", exc)

    def _process_job(self, job_id: str, post_guid: str) -> None:
        """Execute a single job in a short-lived child process.

        Uses a global processing lock to absolutely guarantee single-job execution.
        """
        # Acquire global lock to ensure only one job runs at a time
        logger.info(
            "[JOB_PROCESS] Waiting for processing lock: job_id=%s post_guid=%s",
            job_id,
            post_guid,
        )
        with JobsManager._global_processing_lock:
            logger.info(
                "[JOB_PROCESS] Acquired processing lock: job_id=%s post_guid=%s",
                job_id,
                post_guid,
            )
            with _scheduler_app_context():
                with db_guard("process_job", _db.session, logger):
                    try:
                        # Clear any failed transaction state from prior work on this session.
                        try:
                            _db.session.rollback()
                        except Exception:  # noqa: BLE001
                            pass

                        # Expire all cached objects to ensure fresh reads
                        _db.session.expire_all()

                        logger.debug(
                            "Worker starting job_id=%s post_guid=%s", job_id, post_guid
                        )
                        process = self._launch_processing_worker(job_id, post_guid)
                        exit_code = self._wait_for_processing_worker(
                            process, job_id, post_guid
                        )
                        if exit_code:
                            self._fail_job_if_nonterminal(
                                job_id,
                                f"Processing worker exited with status {exit_code}",
                            )
                    except Exception as exc:
                        logger.error(
                            "Unexpected error in job %s: %s", job_id, exc, exc_info=True
                        )
                        self._fail_job_if_nonterminal(
                            job_id, f"Job execution failed: {exc}"
                        )
                    finally:
                        # Always clean up session state after job processing to release any locks
                        try:
                            _db.session.rollback()
                        except Exception:  # noqa: BLE001
                            pass
                        try:
                            _db.session.remove()
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "Failed to remove session after job: %s", exc
                            )
                        release_memory_to_os(
                            f"web supervisor after processing job {job_id}", logger
                        )
            logger.info(
                "[JOB_PROCESS] Released processing lock: job_id=%s post_guid=%s",
                job_id,
                post_guid,
            )

    def _launch_processing_worker(
        self, job_id: str, post_guid: str
    ) -> subprocess.Popen[bytes]:
        command = [
            sys.executable,
            "-m",
            "app.processing_worker",
            "--job-id",
            job_id,
            "--post-guid",
            post_guid,
        ]
        logger.info(
            "Launching processing worker: job_id=%s post_guid=%s command=%s",
            job_id,
            post_guid,
            command,
        )
        return subprocess.Popen(command, env=self._processing_worker_env())

    def _processing_worker_env(self) -> dict[str, str]:
        env = os.environ.copy()
        src_root = Path(__file__).resolve().parents[1]
        existing_pythonpath = env.get("PYTHONPATH")
        src_root_str = str(src_root)
        if existing_pythonpath:
            entries = existing_pythonpath.split(os.pathsep)
            if src_root_str not in entries:
                env["PYTHONPATH"] = os.pathsep.join([src_root_str, existing_pythonpath])
        else:
            env["PYTHONPATH"] = src_root_str
        return env

    def _wait_for_processing_worker(
        self,
        process: Any,
        job_id: str,
        post_guid: str,
    ) -> int:
        poll_seconds = _env_float("PODLY_PROCESS_WORKER_POLL_SEC", 2.0, minimum=0.1)
        grace_seconds = _env_float(
            "PODLY_PROCESS_WORKER_TERMINATE_GRACE_SEC", 10.0, minimum=0.0
        )

        while True:
            exit_code = process.poll()
            if exit_code is not None:
                logger.info(
                    "Processing worker exited: job_id=%s post_guid=%s exit_code=%s",
                    job_id,
                    post_guid,
                    exit_code,
                )
                return int(exit_code)

            status = self._job_status(job_id)
            if status == "cancelled":
                logger.info(
                    "Processing worker job cancelled; terminating child: "
                    "job_id=%s post_guid=%s pid=%s",
                    job_id,
                    post_guid,
                    getattr(process, "pid", None),
                )
                process.terminate()
                try:
                    return int(process.wait(timeout=grace_seconds))
                except subprocess.TimeoutExpired:
                    logger.warning(
                        "Processing worker did not exit after cancellation; killing: "
                        "job_id=%s post_guid=%s pid=%s",
                        job_id,
                        post_guid,
                        getattr(process, "pid", None),
                    )
                    process.kill()
                    return int(process.wait())

            time.sleep(poll_seconds)

    def _job_status(self, job_id: str) -> str | None:
        _db.session.expire_all()
        job = _db.session.get(ProcessingJob, job_id)
        return None if job is None else job.status

    def _fail_job_if_nonterminal(self, job_id: str, message: str) -> None:
        try:
            _db.session.expire_all()
            failed_job = _db.session.get(ProcessingJob, job_id)
            if failed_job and failed_job.status not in TERMINAL_JOB_STATUSES:
                self._status_manager.update_job_status(
                    failed_job,
                    "failed",
                    failed_job.current_step or 0,
                    message,
                    failed_job.progress_percentage or 0.0,
                )
        except Exception as cleanup_error:
            logger.error(
                "Failed to update job status after error: %s",
                cleanup_error,
                exc_info=True,
            )


# Singleton accessor
_jobs_manager_instance: JobsManager | None = None


def get_jobs_manager() -> JobsManager:
    global _jobs_manager_instance
    if _jobs_manager_instance is None:
        _jobs_manager_instance = JobsManager()
    return _jobs_manager_instance


def scheduled_refresh_all_feeds() -> None:
    """Top-level function for APScheduler to invoke periodically."""
    try:
        get_jobs_manager().start_refresh_all_feeds(trigger="scheduled")
    except Exception as e:  # noqa: BLE001
        logger.error(f"Scheduled refresh failed: {e}")
