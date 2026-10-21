from datetime import UTC, datetime, timedelta
from typing import Any

from app.extensions import db
from app.job_stage_history import initial_stage_history
from app.jobs_manager_run_service import recalculate_run_counts
from app.models import ModelCall, Post, ProcessingJob


def dequeue_job_action(params: dict[str, Any]) -> dict[str, Any] | None:
    run_id = params.get("run_id")

    # Check for running jobs
    running_job = (
        ProcessingJob.query.filter(ProcessingJob.status == "running")
        .order_by(ProcessingJob.started_at.desc().nullslast())
        .first()
    )
    if running_job:
        return None

    job = (
        ProcessingJob.query.filter(ProcessingJob.status == "pending")
        .order_by(ProcessingJob.created_at.asc())
        .first()
    )
    if not job:
        return None

    job.status = "running"
    job.started_at = datetime.now(UTC).replace(tzinfo=None)

    if run_id and job.jobs_manager_run_id != run_id:
        job.jobs_manager_run_id = run_id

    return {"job_id": job.id, "post_guid": job.post_guid}


def cleanup_stale_jobs_action(params: dict[str, Any]) -> dict[str, Any]:
    older_than_seconds = params.get("older_than_seconds", 3600)
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
        seconds=older_than_seconds
    )

    old_jobs = ProcessingJob.query.filter(ProcessingJob.created_at < cutoff).all()

    count = len(old_jobs)
    for job in old_jobs:
        db.session.delete(job)

    return {"count": count}


def clear_all_jobs_action(params: dict[str, Any]) -> int:
    all_jobs = ProcessingJob.query.all()
    count = len(all_jobs)
    for job in all_jobs:
        db.session.delete(job)
    return count


def clear_active_jobs_action(params: dict[str, Any]) -> int:
    """Clear only pending and running jobs. Preserves completed/failed/skipped/cancelled."""
    active_jobs = ProcessingJob.query.filter(
        ProcessingJob.status.in_(["pending", "running"])
    ).all()
    count = len(active_jobs)
    for job in active_jobs:
        db.session.delete(job)
    if count > 0:
        recalculate_run_counts(db.session)
    return count


def create_job_action(params: dict[str, Any]) -> dict[str, Any]:
    job_data = params.get("job_data")
    if not isinstance(job_data, dict):
        raise ValueError("job_data must be a dictionary")

    # Convert date strings back to datetime objects if necessary
    if "created_at" in job_data and isinstance(job_data["created_at"], str):
        job_data["created_at"] = datetime.fromisoformat(job_data["created_at"])

    job = ProcessingJob(**job_data)
    # Seed the queue (step 0) entry from creation time so the UI can report
    # how long the job sat queued before it started running.
    if not job.stage_history:
        job.stage_history = initial_stage_history(
            step=job.current_step or 0,
            step_name=job.step_name or "Queued",
            at=job.created_at,
        )
    db.session.add(job)

    if job.jobs_manager_run_id:
        recalculate_run_counts(db.session)

    db.session.flush()
    return {"job_id": job.id}


def create_job_if_missing_action(params: dict[str, Any]) -> dict[str, Any]:
    """Create a new pending job only if no completed/skipped job already exists for the post."""
    job_data = params.get("job_data")
    if not isinstance(job_data, dict):
        raise ValueError("job_data must be a dictionary")

    post_guid = job_data.get("post_guid")
    if not post_guid:
        raise ValueError("job_data must contain post_guid")

    existing = ProcessingJob.query.filter_by(post_guid=post_guid).first()
    if existing:
        return {"job_id": None, "skipped": True}

    return create_job_action({"job_data": job_data})


def cancel_existing_jobs_action(params: dict[str, Any]) -> int:
    post_guid = params.get("post_guid")
    current_job_id = params.get("current_job_id")

    existing_jobs = (
        ProcessingJob.query.filter_by(post_guid=post_guid)
        .filter(
            ProcessingJob.status.in_(["pending", "running"]),
            ProcessingJob.id != current_job_id,
        )
        .all()
    )

    count = len(existing_jobs)
    for existing_job in existing_jobs:
        db.session.delete(existing_job)

    # Mark any non-terminal ModelCall rows for this post as cancelled. The
    # processing worker we just cancelled may have been mid-LLM-call (e.g.
    # mid-retry-backoff), leaving a row in `pending` forever. That dangling
    # row also collides with the next run's upsert against the
    # (post_id, first_seq, last_seq, model_name) unique index.
    if count > 0:
        post = Post.query.filter_by(guid=post_guid).first()
        if post is not None:
            # Anything not terminal-success / terminal-failed / already-cancelled.
            # Catches "pending" (never started or mid-call), "retrying" (mid-backoff),
            # and "failed_retries" (transient failure waiting to be picked up).
            ModelCall.query.filter(
                ModelCall.post_id == post.id,
                ModelCall.status.notin_(("success", "failed_permanent", "cancelled")),
            ).update(
                {
                    "status": "cancelled",
                    "error_message": "Superseded by a new processing job",
                },
                synchronize_session=False,
            )
        recalculate_run_counts(db.session)

    return count


def update_job_attribution_action(params: dict[str, Any]) -> dict[str, Any]:
    """Update a job's `jobs_manager_run_id`, `requested_by_user_id`, and/or
    `billing_user_id` fields.

    Used by `JobManager.ensure_job()` to backfill attribution onto an already-
    active job when a new request arrives for the same post. The web/processing
    sessions are read-only, so this mutation has to go through the writer
    service. Only fields explicitly provided in `params` are touched, and the
    action no-ops if no field actually changes (idempotent).
    """
    job_id = params.get("job_id")
    if not job_id:
        raise ValueError("job_id is required")

    job = db.session.get(ProcessingJob, job_id)
    if not job:
        raise ValueError(f"Job {job_id} not found")

    changed = False
    if "run_id" in params:
        run_id = params.get("run_id")
        if run_id and job.jobs_manager_run_id != run_id:
            job.jobs_manager_run_id = run_id
            changed = True
    if "requested_by_user_id" in params:
        requested_by_user_id = params.get("requested_by_user_id")
        if requested_by_user_id and job.requested_by_user_id is None:
            job.requested_by_user_id = requested_by_user_id
            changed = True
    if "billing_user_id" in params:
        billing_user_id = params.get("billing_user_id")
        if billing_user_id is not None and job.billing_user_id != billing_user_id:
            job.billing_user_id = billing_user_id
            changed = True

    return {"job_id": job.id, "changed": changed}


def update_job_status_action(params: dict[str, Any]) -> dict[str, Any]:
    job_id = params.get("job_id")
    status = params.get("status")
    step = params.get("step")
    step_name = params.get("step_name")
    progress = params.get("progress")
    error_message = params.get("error_message")

    job = db.session.get(ProcessingJob, job_id)
    if not job:
        raise ValueError(f"Job {job_id} not found")

    # A worker can finish between the supervisor observing cancellation and
    # terminating its process. Cancellation owns the terminal state once set;
    # ignore that late worker update rather than resurrecting the job.
    if job.status == "cancelled" and status != "cancelled":
        return {"job_id": job.id, "status": "cancelled"}

    now = datetime.now(UTC).replace(tzinfo=None)

    job.status = status
    job.current_step = step
    job.step_name = step_name
    if progress is not None:
        job.progress_percentage = progress
    total_steps = params.get("total_steps")
    if total_steps is not None:
        job.total_steps = int(total_steps)

    if error_message:
        job.error_message = error_message

    if status == "running" and not job.started_at:
        job.started_at = now
    elif (
        status in ["completed", "failed", "cancelled", "skipped"]
        and not job.completed_at
    ):
        job.completed_at = now

    # Record stage transitions so the UI can show per-stage durations without
    # having to track them client-side. Append a new entry each time the step
    # actually changes (or on the very first status update).
    history = list(job.stage_history or [])
    if not history or history[-1].get("step") != step:
        history.append(
            {
                "step": step,
                "step_name": step_name,
                "started_at": now.isoformat(),
            }
        )
        job.stage_history = history

    if job.jobs_manager_run_id:
        recalculate_run_counts(db.session)

    return {"job_id": job.id, "status": job.status}


def mark_cancelled_action(params: dict[str, Any]) -> dict[str, Any]:
    job_id = params.get("job_id")
    reason = params.get("reason") or "Cancelled by user request"

    job = db.session.get(ProcessingJob, job_id)
    if not job:
        raise ValueError(f"Job {job_id} not found")

    job.status = "cancelled"
    job.step_name = reason
    job.error_message = reason
    job.completed_at = datetime.now(UTC).replace(tzinfo=None)

    if job.jobs_manager_run_id:
        recalculate_run_counts(db.session)

    return {"job_id": job.id, "status": "cancelled"}


def mark_classification_parse_error_action(params: dict[str, Any]) -> dict[str, Any]:
    """Flag a job's run as having hit at least one LLM-response parse error.

    Pairs with ``ad_windows_count == 0`` in the zero-ads-guard logic to drive
    the optional auto-retry. Idempotent: subsequent batches that also fail to
    parse just leave the flag at True.
    """
    job_id = params.get("job_id")
    job = db.session.get(ProcessingJob, job_id)
    if not job:
        raise ValueError(f"Job {job_id} not found")
    job.had_classification_parse_error = True
    return {"job_id": job.id}


def record_ad_windows_count_action(params: dict[str, Any]) -> dict[str, Any]:
    """Record the final ad-window count for a run.

    Set after ad detection has produced its segments-to-remove list. ``0``
    is meaningful (no ads found) and different from ``None`` (not yet
    recorded / wasn't an LLM run).
    """
    job_id = params.get("job_id")
    count = params.get("count")
    job = db.session.get(ProcessingJob, job_id)
    if not job:
        raise ValueError(f"Job {job_id} not found")
    if count is not None:
        job.ad_windows_count = int(count)
    return {"job_id": job.id}


def mark_auto_retry_attempted_action(params: dict[str, Any]) -> dict[str, Any]:
    """Idempotency guard for the zero-ads auto-retry.

    Set just before enqueueing a retry job; the worker checks this flag
    before deciding to retry, so we can never retry twice for the same
    post even if the retry itself also produces zero ads with parse errors.
    """
    job_id = params.get("job_id")
    job = db.session.get(ProcessingJob, job_id)
    if not job:
        raise ValueError(f"Job {job_id} not found")
    job.auto_retry_attempted = True
    return {"job_id": job.id}


def reassign_pending_jobs_action(params: dict[str, Any]) -> int:
    run_id = params.get("run_id")
    if not run_id:
        return 0

    pending_jobs = (
        ProcessingJob.query.filter(ProcessingJob.status == "pending")
        .order_by(ProcessingJob.created_at.asc())
        .all()
    )

    reassigned = 0
    for job in pending_jobs:
        if job.jobs_manager_run_id != run_id:
            job.jobs_manager_run_id = run_id
            reassigned += 1

    if reassigned:
        recalculate_run_counts(db.session)

    return reassigned
