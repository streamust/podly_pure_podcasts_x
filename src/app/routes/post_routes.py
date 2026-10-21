import json
import logging
import math
import os
import re
from pathlib import Path
from typing import Any

import flask
from flask import Blueprint, g, jsonify, request, send_file
from flask.typing import ResponseReturnValue
from sqlalchemy import func
from sqlalchemy.orm import defer

from app.auth.guards import require_admin
from app.auth.service import update_user_last_active
from app.extensions import db
from app.failure_explainer import (
    build_troubleshoot_context,
    explain_failure,
    format_troubleshoot_entries,
    get_app_log_path,
    select_troubleshoot_entries,
    tail_log_lines,
)
from app.feeds import (
    build_post_feed_description_html,
    feed_chapters,
    post_feed_render_defers,
)
from app.jobs_manager import get_jobs_manager
from app.model_call_utils import whisper_model_call_filter
from app.models import (
    AudioSegment,
    Feed,
    Identification,
    ModelCall,
    Post,
    ProcessingJob,
    TranscriptSegment,
)
from app.posts import (
    clear_post_processing_data,
    clear_post_processing_data_keep_transcript,
)
from app.routes.post_stats_utils import (
    build_edited_timeline_ad_markers,
    build_edited_timeline_bleep_windows,
    build_speaker_breakdown,
    count_model_calls,
    count_primary_labels,
    group_identifications_by_segment,
    is_mixed_segment,
    parse_refined_windows,
    parse_time_windows,
)
from app.routes.post_utils import (
    ensure_whitelisted_for_download,
    increment_download_count,
    missing_processed_audio_response,
    should_increment_download_count_for_request,
)
from app.runtime_config import config as runtime_config
from app.writer.client import writer_client
from podcast_processor.chapter_filter import parse_filter_strings
from podcast_processor.chapter_reader import read_chapters
from shared import defaults as DEFAULTS
from shared.processing_paths import (
    get_in_root,
    get_instance_dir,
    get_processed_audio_path_candidates,
    get_srv_root,
)
from shared.rust_sidecar import try_render_post_stats

logger = logging.getLogger("global_logger")


post_bp = Blueprint("post", __name__)


def _current_user_is_admin() -> bool:
    current_user = getattr(g, "current_user", None)
    if current_user is None:
        return False

    role = getattr(current_user, "role", None)
    if role == "admin":
        return True

    user_id = getattr(current_user, "id", None)
    if user_id is None:
        return False

    from app.models import User

    user = db.session.get(User, int(user_id))
    return bool(user and user.role == "admin")


def _llm_costs_by_call_id(post_id: int) -> dict[int, float]:
    """Map of ``ModelCall.id`` → persisted ``estimated_cost_usd`` for a post.

    Reads only the billable-LLM rows: Whisper and INA rows are priced by
    audio duration (computed elsewhere) and their column is left NULL by
    the worker. Used to enrich the per-call breakdown the stats modal
    renders. Cheap pure-SQL — no litellm import.
    """
    rows = (
        db.session.query(ModelCall.id, ModelCall.estimated_cost_usd)
        .filter(
            ModelCall.post_id == post_id,
            ModelCall.status == "success",
            ModelCall.estimated_cost_usd.isnot(None),
        )
        .all()
    )
    return {int(call_id): float(cost) for call_id, cost in rows}


def _maybe_add_admin_cost_estimate(stats_data: dict[str, Any], post: Post) -> None:
    """Populate ``processing_stats.estimated_cost`` and per-call costs.

    The overview shows total compute cost (LLM + Whisper + INA). The
    per-call breakdown attached to ``stats_data["model_calls"]`` lets the
    modal render a Cost column: billable LLM rows show the persisted
    ``estimated_cost_usd``; Whisper / INA rows show ``rate * hours`` split
    evenly across success calls of that type for the post.
    """
    if not _current_user_is_admin():
        return
    processing_stats = stats_data.get("processing_stats")
    if not isinstance(processing_stats, dict):
        return

    from app.config_store import read_combined
    from app.llm_pricing import is_ina_call, is_whisper_call

    app_config = read_combined().get("app", {})
    whisper_rate = float(
        app_config.get(
            "whisper_cost_rate_per_hour", DEFAULTS.APP_WHISPER_COST_RATE_PER_HOUR
        )
        or 0.0
    )
    ina_rate = float(
        app_config.get("ina_cost_rate_per_hour", DEFAULTS.APP_INA_COST_RATE_PER_HOUR)
        or 0.0
    )
    # Whisper and INA run on the unprocessed audio, so bill its length.
    duration_seconds = processing_stats.get("original_duration_seconds")
    if not isinstance(duration_seconds, (int, float)):
        duration_seconds = float(post.duration or 0.0)
    duration_hours = max(0.0, float(duration_seconds)) / 3600.0

    calls = stats_data.get("model_calls") or []

    # Count success Whisper / INA rows to split the per-hour fee across.
    whisper_success_count = sum(
        1
        for c in calls
        if isinstance(c, dict)
        and c.get("status") == "success"
        and is_whisper_call(c.get("model_name") or "", c.get("prompt"))
    )
    ina_success_count = sum(
        1
        for c in calls
        if isinstance(c, dict)
        and c.get("status") == "success"
        and is_ina_call(c.get("model_name") or "")
    )

    whisper_total = whisper_rate * duration_hours if whisper_success_count else 0.0
    ina_total = ina_rate * duration_hours if ina_success_count else 0.0
    whisper_per_call = (
        whisper_total / whisper_success_count if whisper_success_count else 0.0
    )
    ina_per_call = ina_total / ina_success_count if ina_success_count else 0.0

    llm_costs = _llm_costs_by_call_id(int(post.id))

    llm_total = 0.0
    for call in calls:
        if not isinstance(call, dict):
            continue
        if call.get("status") != "success":
            call["estimated_cost_usd"] = 0.0
            continue
        model_name = call.get("model_name") or ""
        prompt = call.get("prompt")
        if is_whisper_call(model_name, prompt):
            call["estimated_cost_usd"] = round(whisper_per_call, 6)
        elif is_ina_call(model_name):
            call["estimated_cost_usd"] = round(ina_per_call, 6)
        else:
            cost = float(llm_costs.get(int(call.get("id", 0)), 0.0))
            call["estimated_cost_usd"] = round(cost, 6)
            llm_total += cost

    total = llm_total + whisper_total + ina_total
    processing_stats["estimated_cost"] = round(total, 4)


_LOG_LINE_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) "
    r"(?P<level>[A-Z]+)\s+(?P<message>.*)$"
)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _sqlite_db_path_for_sidecar() -> Path:
    return get_instance_dir() / "sqlite3.db"


def _build_file_debug(path_value: str | None) -> dict[str, Any]:
    info: dict[str, Any] = {
        "path": path_value,
        "absolute_path": None,
        "exists": False,
        "is_file": False,
        "size_bytes": None,
    }
    if not path_value:
        return info

    path_obj = Path(path_value)
    try:
        info["absolute_path"] = str(path_obj.resolve())
        exists = path_obj.exists()
        is_file = path_obj.is_file()
        info["exists"] = exists
        info["is_file"] = is_file
        if exists and is_file:
            info["size_bytes"] = path_obj.stat().st_size
    except OSError as exc:
        info["error"] = str(exc)
    return info


def _build_candidate_file_debug(candidates: list[Path]) -> list[dict[str, Any]]:
    candidate_details: list[dict[str, Any]] = []
    for candidate in candidates:
        detail: dict[str, Any] = {
            "path": str(candidate),
            "exists": False,
            "size_bytes": None,
        }
        try:
            exists = candidate.exists() and candidate.is_file()
            detail["exists"] = exists
            if exists:
                detail["size_bytes"] = candidate.stat().st_size
        except OSError as exc:
            detail["error"] = str(exc)
        candidate_details.append(detail)
    return candidate_details


def _extract_log_field(message: str, field_name: str) -> str | None:
    match = re.search(rf"\b{re.escape(field_name)}=([^\s]+)", message)
    if match is None:
        return None
    return match.group(1)


# Sentinel (first_seq, last_seq) pairs that chapter_fallback.py uses to
# create ModelCall rows for chapter-only LLM phases (no real transcript
# segment range). Mirror keep in sync with the constants in chapter_fallback.
_SEGMENT_RANGE_LABELS: dict[tuple[int, int], str] = {
    (-100, -100): "chapter titles (LLM)",
    (-200, -200): "chapter topic plan (LLM)",
    (-201, -201): "chapter topic plan: continuation (LLM)",
}


def _format_segment_range_label(first_seq: int, last_seq: int) -> str:
    label = _SEGMENT_RANGE_LABELS.get((first_seq, last_seq))
    if label is not None:
        return label
    return f"{first_seq}-{last_seq}"


def _extract_step_name(message: str) -> str | None:
    match = re.search(r"\bstep_name=(.*?)(?:\s+\w+=|$)", message)
    if match is None:
        return None

    step_name = match.group(1).strip()
    return step_name or None


def _infer_log_stage(message: str, step_name: str | None) -> str:
    searchable = f"{step_name or ''} {message}".lower()

    if "download" in searchable:
        return "download"
    if "transcrib" in searchable or "whisper" in searchable:
        return "transcription"
    if "chapter" in searchable:
        return "chapters"
    if (
        "identifying ads" in searchable
        or "classif" in searchable
        or "identification" in searchable
        or "boundary" in searchable
        or "modelcall" in searchable
        or "model call" in searchable
        or "llm" in searchable
    ):
        return "classification"
    if (
        "processing audio" in searchable
        or "audio processor" in searchable
        or "ffmpeg" in searchable
        or "removed segment" in searchable
        or "processed audio" in searchable
    ):
        return "audio"
    if (
        "[job_status" in searchable
        or "starting processing" in searchable
        or "processing complete" in searchable
        or "cancel" in searchable
        or "unexpected error" in searchable
        or "failed" in searchable
    ):
        return "job"
    return "general"


def _build_related_logs(
    *,
    post: Post,
    recent_jobs: list[ProcessingJob],
) -> dict[str, Any]:
    log_path = get_app_log_path()
    lines = tail_log_lines(log_path)
    if not lines:
        return {
            "latest_job_id": recent_jobs[0].id if recent_jobs else None,
            "entries": [],
        }

    recent_job_ids = {job.id for job in recent_jobs if job.id}
    related_entries: list[dict[str, Any]] = []
    post_id_patterns = (
        re.compile(rf"\bpost_id={post.id}\b"),
        re.compile(rf"\bpost {post.id}\b"),
    )

    for line in lines:
        match = _LOG_LINE_RE.match(line)
        if match is None:
            continue

        message = match.group("message")
        job_id = _extract_log_field(message, "job_id")
        post_guid = _extract_log_field(message, "post_guid")

        if not any(
            [
                job_id in recent_job_ids if job_id is not None else False,
                post_guid == post.guid,
                any(pattern.search(message) for pattern in post_id_patterns),
            ]
        ):
            continue

        step_name = _extract_step_name(message)
        related_entries.append(
            {
                "timestamp": match.group("timestamp"),
                "level": match.group("level"),
                "stage": _infer_log_stage(message, step_name),
                "message": message,
                "job_id": job_id,
                "step_name": step_name,
            }
        )

    return {
        "latest_job_id": recent_jobs[0].id if recent_jobs else None,
        "entries": related_entries[-120:],
    }


@post_bp.route("/api/posts/<path:p_guid>/troubleshoot", methods=["POST"])
def api_post_troubleshoot(p_guid: str) -> flask.Response:
    """Explain why an episode failed, in plain English, using the chat LLM.

    Captures a raw log window around the failure (including the Python
    traceback and the underlying exception -- see
    ``_build_troubleshoot_context``), falling back to the structured
    post-tagged entries when no failure line can be located. Asks the
    configured chat model to summarize the root cause and a concrete fix.
    The result is ephemeral -- nothing is persisted. Gated to admins like the
    other cost-incurring post actions (process/reprocess), since it makes a
    billable LLM call.
    """
    _, error = require_admin("troubleshoot this episode")
    if error:
        return error

    post = Post.query.filter_by(guid=p_guid).first()
    if post is None:
        return flask.make_response(flask.jsonify({"error": "Post not found"}), 404)

    payload = request.get_json(silent=True) or {}
    target_job_id = payload.get("job_id") if isinstance(payload, dict) else None

    recent_jobs = (
        ProcessingJob.query.filter_by(post_guid=post.guid)
        .order_by(ProcessingJob.created_at.desc())
        .limit(5)
        .all()
    )

    # Prefer the raw failure window (traceback + root exception). Only fall
    # back to the structured, post-tagged entries when no failure line can be
    # anchored -- those rarely carry the real cause but are better than nothing.
    context_text = build_troubleshoot_context(
        post_guid=post.guid,
        post_id=post.id,
        job_ids={job.id for job in recent_jobs if job.id},
        target_job_id=target_job_id,
    )
    used_traceback = context_text is not None
    if context_text is None:
        related = _build_related_logs(post=post, recent_jobs=recent_jobs)
        entries = select_troubleshoot_entries(related)
        if not entries:
            return flask.jsonify(
                {
                    "ok": True,
                    "explanation": None,
                    "message": (
                        "No related log lines were found for this episode in "
                        "the current log window, so there is nothing to "
                        "diagnose."
                    ),
                }
            )
        context_text = format_troubleshoot_entries(entries)

    if not getattr(runtime_config, "llm_api_key", None):
        return flask.make_response(
            flask.jsonify(
                {
                    "ok": False,
                    "error": (
                        "No LLM API key is configured, so the error cannot be "
                        "explained. Add an LLM API key in Settings."
                    ),
                }
            ),
            400,
        )

    try:
        explanation = explain_failure(context_text, runtime_config)
    except Exception as exc:  # noqa: BLE001
        logger.error("Troubleshoot LLM call failed for post %s: %s", post.id, exc)
        return flask.make_response(
            flask.jsonify(
                {
                    "ok": False,
                    "error": "Failed to generate an explanation. Please try again.",
                }
            ),
            502,
        )

    return flask.jsonify(
        {
            "ok": True,
            "explanation": explanation,
            "used_traceback": used_traceback,
        }
    )


@post_bp.route("/api/feeds/<int:feed_id>/posts", methods=["GET"])
def api_feed_posts(feed_id: int) -> flask.Response:
    """Return a paginated JSON list of posts for a specific feed."""
    from shared.rust_sidecar import (
        FEED_POSTS_NOT_FOUND,
        rust_feed_posts_enabled,
        try_render_feed_posts,
    )

    # Ensure we have fresh data
    db.session.expire_all()

    # Pagination and filtering
    try:
        page = int(request.args.get("page", 1))
    except TypeError, ValueError:
        page = 1
    page = max(page, 1)

    try:
        page_size = int(request.args.get("page_size", 25))
    except TypeError, ValueError:
        page_size = 25
    page_size = max(1, min(page_size, 200))

    whitelisted_only = str(request.args.get("whitelisted_only", "false")).lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    # Rust sidecar path: a short-lived subprocess reads the rows, builds the
    # envelope, and exits. We pass its raw stdout bytes straight to the HTTP
    # response — going through json.loads + flask.jsonify would re-allocate
    # the entire ~290 KB envelope in the long-lived Python heap, defeating
    # the memory win the port is supposed to deliver. Falls through to the
    # Python query if the flag is off or the sidecar fails for any reason.
    if rust_feed_posts_enabled():
        from shared.processing_paths import get_instance_dir

        rust_bytes = try_render_feed_posts(
            db_path=get_instance_dir() / "sqlite3.db",
            feed_id=feed_id,
            page=page,
            page_size=page_size,
            whitelisted_only=whitelisted_only,
        )
        if rust_bytes is FEED_POSTS_NOT_FOUND:
            flask.abort(404)
        if isinstance(rust_bytes, bytes):
            return flask.Response(rust_bytes, mimetype="application/json")

    feed = db.get_or_404(Feed, feed_id)

    # Query posts directly to avoid stale relationship cache. Defer the
    # heavyweight JSON columns that this endpoint never serializes — each
    # transcript_word_timestamps blob can be 1-3 MB for an hour-long episode,
    # so loading a 25-row page without defers can spike RSS by 50+ MB per call.
    # chapter_data stays loaded because build_post_feed_description_html reads it.
    base_query = Post.query.filter_by(feed_id=feed.id).options(
        *post_feed_render_defers()
    )
    if whitelisted_only:
        base_query = base_query.filter_by(whitelisted=True)

    ordered_query = base_query.order_by(
        Post.release_date.desc().nullslast(), Post.id.desc()
    )

    total_posts = ordered_query.count()
    whitelisted_total = Post.query.filter_by(feed_id=feed.id, whitelisted=True).count()

    db_posts = ordered_query.offset((page - 1) * page_size).limit(page_size).all()

    posts = [
        {
            "id": post.id,
            "guid": post.guid,
            "title": post.title,
            "description": post.description,
            "podly_description_html": build_post_feed_description_html(post),
            "release_date": (
                post.release_date.isoformat() if post.release_date else None
            ),
            "duration": post.duration,
            "whitelisted": post.whitelisted,
            "has_processed_audio": post.processed_audio_path is not None,
            "has_unprocessed_audio": post.unprocessed_audio_path is not None,
            "download_url": post.download_url,
            "image_url": post.image_url,
            "download_count": post.download_count,
        }
        for post in db_posts
    ]

    total_pages = math.ceil(total_posts / page_size) if total_posts else 0

    return flask.jsonify(
        {
            "items": posts,
            "page": page,
            "page_size": page_size,
            "total": total_posts,
            "total_pages": total_pages,
            "whitelisted_total": whitelisted_total,
        }
    )


@post_bp.route("/api/posts/<path:p_guid>/processing-estimate", methods=["GET"])
def api_post_processing_estimate(p_guid: str) -> ResponseReturnValue:
    post = Post.query.filter_by(guid=p_guid).first()
    if post is None:
        return flask.make_response(flask.jsonify({"error": "Post not found"}), 404)

    feed = db.session.get(Feed, post.feed_id)
    if feed is None:
        return flask.make_response(flask.jsonify({"error": "Feed not found"}), 404)

    _, error = require_admin("estimate processing costs")
    if error:
        return error

    minutes = max(1.0, float(post.duration or 0) / 60.0) if post.duration else 60.0

    return flask.jsonify(
        {
            "post_guid": post.guid,
            "estimated_minutes": minutes,
            "can_process": True,
            "reason": None,
        }
    )


@post_bp.route("/post/<string:p_guid>/json", methods=["GET"])
def get_post_json(p_guid: str) -> flask.Response:
    logger.info(f"API request for post details with GUID: {p_guid}")
    post = Post.query.filter_by(guid=p_guid).first()
    if post is None:
        return flask.make_response(jsonify({"error": "Post not found"}), 404)

    segment_count = post.segments.count()
    transcript_segments = []

    if segment_count > 0:
        sample_segments = post.segments.limit(5).all()
        for segment in sample_segments:
            transcript_segments.append(
                {
                    "id": segment.id,
                    "sequence_num": segment.sequence_num,
                    "start_time": segment.start_time,
                    "end_time": segment.end_time,
                    "text": (
                        segment.text[:100] + "..."
                        if len(segment.text) > 100
                        else segment.text
                    ),
                }
            )

    whisper_model_calls = []
    for model_call in post.model_calls.filter(whisper_model_call_filter()).all():
        whisper_model_calls.append(
            {
                "id": model_call.id,
                "model_name": model_call.model_name,
                "status": model_call.status,
                "first_segment": model_call.first_segment_sequence_num,
                "last_segment": model_call.last_segment_sequence_num,
                "timestamp": (
                    model_call.timestamp.isoformat() if model_call.timestamp else None
                ),
                "response": (
                    model_call.response[:100] + "..."
                    if model_call.response and len(model_call.response) > 100
                    else model_call.response
                ),
                "error": model_call.error_message,
                "service_tier": model_call.service_tier,
            }
        )

    post_data = {
        "id": post.id,
        "guid": post.guid,
        "title": post.title,
        "feed_id": post.feed_id,
        "unprocessed_audio_path": post.unprocessed_audio_path,
        "processed_audio_path": post.processed_audio_path,
        "has_unprocessed_audio": post.unprocessed_audio_path is not None,
        "has_processed_audio": post.processed_audio_path is not None,
        "transcript_segment_count": segment_count,
        "transcript_sample": transcript_segments,
        "model_call_count": post.model_calls.count(),
        "whisper_model_calls": whisper_model_calls,
        "whitelisted": post.whitelisted,
        "download_count": post.download_count,
    }

    return flask.jsonify(post_data)


@post_bp.route("/post/<string:p_guid>/debug", methods=["GET"])
def post_debug(p_guid: str) -> flask.Response:
    """Debug view for a post, showing model calls, transcript segments, and identifications."""
    post = Post.query.filter_by(guid=p_guid).first()
    if post is None:
        return flask.make_response(("Post not found", 404))

    model_calls = (
        ModelCall.query.filter_by(post_id=post.id)
        .order_by(ModelCall.model_name, ModelCall.first_segment_sequence_num)
        .all()
    )

    transcript_segments = post.segments.all()

    identifications = (
        Identification.query.join(TranscriptSegment)
        .filter(TranscriptSegment.post_id == post.id)
        .order_by(TranscriptSegment.sequence_num)
        .all()
    )

    model_call_statuses, model_types = count_model_calls(model_calls)

    identifications_by_segment = group_identifications_by_segment(identifications)
    content_segments, ad_segments = count_primary_labels(
        transcript_segments, identifications_by_segment
    )

    stats = {
        "total_segments": len(transcript_segments),
        "total_model_calls": len(model_calls),
        "total_identifications": len(identifications),
        "content_segments": content_segments,
        "ad_segments_count": ad_segments,
        "model_call_statuses": model_call_statuses,
        "model_types": model_types,
        "download_count": post.download_count,
    }

    return flask.make_response(
        flask.render_template(
            "post_debug.html",
            post=post,
            model_calls=model_calls,
            transcript_segments=transcript_segments,
            identifications=identifications,
            stats=stats,
        ),
        200,
    )


def _get_chapter_stats(post: Post, feed: Feed) -> dict[str, Any]:
    """Get chapter statistics for chapter-based processing."""

    # Try to read stored chapter data first (set during processing)
    if post.chapter_data:
        try:
            data = json.loads(post.chapter_data)
            chapters_kept = [
                {**ch, "label": "content"} for ch in data.get("chapters_kept", [])
            ]
            chapters_removed = [
                {**ch, "label": "ad"} for ch in data.get("chapters_removed", [])
            ]
            if not chapters_kept and not chapters_removed:
                chapters_for_output = data.get("chapters_for_output", [])
                if isinstance(chapters_for_output, list):
                    chapters_kept = [
                        {**ch, "label": "content"} for ch in chapters_for_output
                    ]
            # Sort by original start time to maintain order from the file
            all_chapters = sorted(
                chapters_kept + chapters_removed, key=lambda c: c["start_time"]
            )
            return {
                "total_chapters": len(chapters_kept) + len(chapters_removed),
                "chapters_kept": len(chapters_kept),
                "chapters_removed": len(chapters_removed),
                "filter_strings": data.get("filter_strings", []),
                "chapters": all_chapters,
            }
        except json.JSONDecodeError, TypeError:
            pass

    # Fallback: read from processed audio (only shows kept chapters)
    from podcast_processor.chapter_reader import read_chapters

    chapters = read_chapters(post.processed_audio_path or "")
    filter_csv = feed.chapter_filter_strings or DEFAULTS.CHAPTER_FILTER_DEFAULT_STRINGS
    filter_strings = parse_filter_strings(filter_csv)

    chapters_kept = [
        {
            "title": ch.title,
            "start_time": round(ch.start_time_ms / 1000.0, 1),
            "end_time": round(ch.end_time_ms / 1000.0, 1),
            "label": "content",
        }
        for ch in chapters
    ]

    return {
        "total_chapters": len(chapters_kept),
        "chapters_kept": len(chapters_kept),
        "chapters_removed": 0,
        "filter_strings": filter_strings,
        "chapters": chapters_kept,
        "note": "Removed chapters not available (processed before this feature)",
    }


def _resolve_original_duration_seconds(
    *,
    post_duration: float | None,
    transcript_segments: list[TranscriptSegment],
    bleep_windows: list[tuple[float, float]],
    ad_time_seconds: float,
) -> float:
    cut_duration_seconds = float(post_duration or 0)
    if cut_duration_seconds <= 0 and bleep_windows:
        cut_duration_seconds = max(end for _, end in bleep_windows)

    transcript_duration_seconds = (
        max(float(seg.end_time) for seg in transcript_segments)
        if transcript_segments
        else 0.0
    )

    if post_duration:
        # post.duration is the cut (ad-removed) audio length, so reconstruct the
        # original episode length by adding back the removed ad time. The
        # transcript is of the unprocessed audio, so its end is a lower bound when
        # current ad settings yield fewer windows than were cut.
        return max(cut_duration_seconds + ad_time_seconds, transcript_duration_seconds)

    return max(
        transcript_duration_seconds,
        cut_duration_seconds,
    )


@post_bp.route("/api/posts/<path:p_guid>/stats", methods=["GET"])
def api_post_stats(p_guid: str) -> flask.Response:
    """Get processing statistics for a post in JSON format."""
    # Word-level transcripts can be megabytes and are not part of stats output.
    post = (
        Post.query.options(defer(Post.transcript_word_timestamps))
        .filter_by(guid=p_guid)
        .first()
    )
    if post is None:
        return flask.make_response(flask.jsonify({"error": "Post not found"}), 404)

    feed = db.session.get(Feed, post.feed_id)
    ad_detection_strategy = feed.ad_detection_strategy if feed else "llm"

    rust_stats = try_render_post_stats(
        db_path=_sqlite_db_path_for_sidecar(),
        post_guid=p_guid,
        min_confidence=float(runtime_config.output.min_confidence),
        min_ad_segment_separation_seconds=float(
            runtime_config.output.min_ad_segment_separation_seconds
        ),
        enable_boundary_refinement=bool(runtime_config.enable_boundary_refinement),
        stats_debug=_env_bool("PODLY_STATS_DEBUG", default=False),
        log_path=get_instance_dir() / "logs" / "app.log",
        in_root=get_in_root(),
        srv_root=get_srv_root(),
    )
    if rust_stats is not None:
        _maybe_add_admin_cost_estimate(rust_stats, post)
        return flask.jsonify(rust_stats)

    model_calls = (
        ModelCall.query.filter_by(post_id=post.id)
        .order_by(ModelCall.model_name, ModelCall.first_segment_sequence_num)
        .all()
    )
    recent_jobs = (
        ProcessingJob.query.filter_by(post_guid=post.guid)
        .order_by(ProcessingJob.created_at.desc())
        .limit(3)
        .all()
    )

    transcript_segments = post.segments.all()
    audio_segments = (
        AudioSegment.query.filter_by(post_id=post.id)
        .order_by(AudioSegment.start_time)
        .all()
    )

    identifications = (
        Identification.query.join(TranscriptSegment)
        .filter(TranscriptSegment.post_id == post.id)
        .order_by(TranscriptSegment.sequence_num)
        .all()
    )

    model_call_statuses: dict[str, int] = {}
    model_types: dict[str, int] = {}

    for call in model_calls:
        if call.status not in model_call_statuses:
            model_call_statuses[call.status] = 0
        model_call_statuses[call.status] += 1

        if call.model_name not in model_types:
            model_types[call.model_name] = 0
        model_types[call.model_name] += 1

    identifications_by_segment = group_identifications_by_segment(identifications)
    content_segments, ad_segments = count_primary_labels(
        transcript_segments, identifications_by_segment
    )
    speaker_breakdown = build_speaker_breakdown(transcript_segments)

    # Refined ad windows are written by boundary refinement and are used for precise
    # cutting. We also derive a UI-only "mixed" flag for segments that overlap a
    # refined ad window but are not fully contained by it (i.e., segment contains
    # both content and ad).
    raw_refined = getattr(post, "refined_ad_boundaries", None) or []
    refined_windows = parse_refined_windows(raw_refined)
    raw_bleep_windows = getattr(post, "bleep_windows", None) or []
    bleep_windows = parse_time_windows(raw_bleep_windows)

    model_call_details = []
    for call in model_calls:
        recorded_attempts = max(int(call.retry_attempts or 0), 0)
        model_call_details.append(
            {
                "id": call.id,
                "model_name": call.model_name,
                "status": call.status,
                "segment_range": _format_segment_range_label(
                    call.first_segment_sequence_num,
                    call.last_segment_sequence_num,
                ),
                "first_segment_sequence_num": call.first_segment_sequence_num,
                "last_segment_sequence_num": call.last_segment_sequence_num,
                "timestamp": call.timestamp.isoformat() if call.timestamp else None,
                "retry_attempts": recorded_attempts,
                # Historically, retry_attempts has been used as an attempt counter
                # for LLM calls, so expose a UI-safe retry count separately.
                "retry_count": max(recorded_attempts - 1, 0),
                "error_message": call.error_message,
                "prompt": call.prompt,
                "response": call.response,
                "service_tier": call.service_tier,
                "prompt_tokens": call.prompt_tokens,
                "cached_prompt_tokens": call.cached_prompt_tokens,
                "completion_tokens": call.completion_tokens,
                "total_tokens": call.total_tokens,
            }
        )

    transcript_segments_data = []
    segment_mixed_by_id: dict[int, bool] = {}
    for segment in transcript_segments:
        segment_identifications = identifications_by_segment.get(segment.id, [])

        has_ad_label = any(i.label == "ad" for i in segment_identifications)
        primary_label = "ad" if has_ad_label else "content"

        seg_start = float(segment.start_time)
        seg_end = float(segment.end_time)
        mixed = bool(has_ad_label) and is_mixed_segment(
            seg_start=seg_start, seg_end=seg_end, refined_windows=refined_windows
        )
        segment_mixed_by_id[int(segment.id)] = mixed

        transcript_segments_data.append(
            {
                "id": segment.id,
                "sequence_num": segment.sequence_num,
                "start_time": round(segment.start_time, 1),
                "end_time": round(segment.end_time, 1),
                "speaker_label": segment.speaker_label,
                "text": segment.text,
                "primary_label": primary_label,
                "mixed": mixed,
                "identifications": [
                    {
                        "id": ident.id,
                        "label": ident.label,
                        "confidence": (
                            round(ident.confidence, 2) if ident.confidence else None
                        ),
                        "model_call_id": ident.model_call_id,
                    }
                    for ident in segment_identifications
                ],
            }
        )

    identifications_data = []
    for identification in identifications:
        segment = identification.transcript_segment
        identifications_data.append(
            {
                "id": identification.id,
                "transcript_segment_id": identification.transcript_segment_id,
                "label": identification.label,
                "confidence": (
                    round(identification.confidence, 2)
                    if identification.confidence
                    else None
                ),
                "model_call_id": identification.model_call_id,
                "segment_sequence_num": segment.sequence_num,
                "segment_start_time": round(segment.start_time, 1),
                "segment_end_time": round(segment.end_time, 1),
                "segment_text": segment.text,
                "mixed": bool(segment_mixed_by_id.get(int(segment.id), False)),
            }
        )

    audio_segments_data = [
        {
            "id": segment.id,
            "start_time": round(float(segment.start_time), 1),
            "end_time": round(float(segment.end_time), 1),
            "label": segment.label,
            "model_call_id": segment.model_call_id,
        }
        for segment in audio_segments
    ]

    # Build chapter data for chapter-based processing
    chapters_data = None
    if (
        ad_detection_strategy in ("chapter", "chapter_insert")
        and post.processed_audio_path
        and feed
    ):
        chapters_data = _get_chapter_stats(post, feed)

    # Keep stats aligned with the audio cutter's final cut-ready windows.
    from podcast_processor.audio_processor import AudioProcessor

    ad_blocks = AudioProcessor(
        config=runtime_config,
        logger=logger,
        db_session=db.session,
    ).get_ad_segments(post)
    ad_time_seconds = sum(end - start for start, end in ad_blocks if end > start)

    original_duration_seconds = _resolve_original_duration_seconds(
        post_duration=post.duration,
        transcript_segments=transcript_segments,
        bleep_windows=bleep_windows,
        ad_time_seconds=ad_time_seconds,
    )

    ad_percentage = (
        (ad_time_seconds / original_duration_seconds) * 100
        if original_duration_seconds > 0
        else 0.0
    )
    bleep_time_seconds = sum(end - start for start, end in bleep_windows if end > start)
    bleep_percentage = (
        (bleep_time_seconds / original_duration_seconds) * 100
        if original_duration_seconds > 0
        else 0.0
    )
    edited_duration_seconds = max(0.0, original_duration_seconds - ad_time_seconds)
    edited_ad_markers = build_edited_timeline_ad_markers(ad_blocks)
    edited_bleep_windows = build_edited_timeline_bleep_windows(
        bleep_windows,
        ad_blocks,
    )

    stats_data = {
        "post": {
            "guid": post.guid,
            "title": post.title,
            "duration": post.duration,
            "release_date": (
                post.release_date.isoformat() if post.release_date else None
            ),
            "whitelisted": post.whitelisted,
            "has_processed_audio": post.processed_audio_path is not None,
            "download_count": post.download_count,
        },
        "ad_detection_strategy": ad_detection_strategy,
        "processing_stats": {
            "total_segments": len(transcript_segments),
            "total_model_calls": len(model_calls),
            "total_identifications": len(identifications),
            "audio_segments_count": len(audio_segments),
            "content_segments": content_segments,
            "ad_segments_count": ad_segments,
            "ad_percentage": round(ad_percentage, 1),
            "estimated_ad_time_seconds": round(ad_time_seconds, 1),
            "original_duration_seconds": round(original_duration_seconds, 1),
            "edited_duration_seconds": round(edited_duration_seconds, 1),
            "ad_blocks": [
                {
                    "start_time": round(start, 1),
                    "end_time": round(end, 1),
                }
                for start, end in ad_blocks
            ],
            "edited_ad_markers": edited_ad_markers,
            "has_bleep_windows": bool(bleep_windows),
            "bleeped_time_seconds": round(bleep_time_seconds, 1),
            "bleeped_percentage": round(bleep_percentage, 1),
            "bleep_windows": [
                {
                    "start_time": round(start, 3),
                    "end_time": round(end, 3),
                }
                for start, end in bleep_windows
            ],
            "edited_bleep_windows": edited_bleep_windows,
            "speaker_breakdown": speaker_breakdown,
            "model_call_statuses": model_call_statuses,
            "model_types": model_types,
        },
        "model_calls": model_call_details,
        "transcript_segments": transcript_segments_data,
        "audio_segments": audio_segments_data,
        "identifications": identifications_data,
        "related_logs": _build_related_logs(post=post, recent_jobs=recent_jobs),
        "chapters": chapters_data,
    }

    if _env_bool("PODLY_STATS_DEBUG", default=False):
        candidates = get_processed_audio_path_candidates(
            processed_audio_path=post.processed_audio_path,
            unprocessed_audio_path=post.unprocessed_audio_path,
            feed_title=feed.title if feed else None,
            post_title=post.title,
        )
        stats_data["debug_info"] = {
            "post_id": post.id,
            "feed_id": post.feed_id,
            "guid": post.guid,
            "download_url": post.download_url,
            "download_count": post.download_count,
            "has_processed_audio": post.processed_audio_path is not None,
            "has_unprocessed_audio": post.unprocessed_audio_path is not None,
            "processed_audio": _build_file_debug(post.processed_audio_path),
            "unprocessed_audio": _build_file_debug(post.unprocessed_audio_path),
            "processed_audio_path_candidates": _build_candidate_file_debug(candidates),
            "processing_roots": {
                "in_root": str(get_in_root()),
                "srv_root": str(get_srv_root()),
            },
            "record_counts": {
                "transcript_segments": len(transcript_segments),
                "audio_segments": len(audio_segments),
                "model_calls": len(model_calls),
                "identifications": len(identifications),
            },
        }

    _maybe_add_admin_cost_estimate(stats_data, post)
    return flask.jsonify(stats_data)


@post_bp.route("/api/posts/<path:p_guid>/whitelist", methods=["POST"])
def api_toggle_whitelist(p_guid: str) -> ResponseReturnValue:
    """Toggle whitelist status for a post via API (admins only)."""
    post = Post.query.filter_by(guid=p_guid).first()
    if post is None:
        return flask.make_response(flask.jsonify({"error": "Post not found"}), 404)

    feed = db.session.get(Feed, post.feed_id)
    if feed is None:
        return flask.make_response(flask.jsonify({"error": "Feed not found"}), 404)

    user, error = require_admin("whitelist this episode")
    if error:
        return error
    if user is not None and user.role != "admin":
        return (
            flask.jsonify(
                {
                    "error": "FORBIDDEN",
                    "message": "Only admins can change whitelist status.",
                }
            ),
            403,
        )

    data = request.get_json()
    if data is None or "whitelisted" not in data:
        return flask.make_response(
            flask.jsonify({"error": "Missing whitelisted field"}), 400
        )

    try:
        writer_client.update(
            "Post", post.id, {"whitelisted": bool(data["whitelisted"])}, wait=True
        )
        # Refresh post object
        db.session.expire(post)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Failed to toggle whitelist: {e}")
        return (
            flask.jsonify(
                {
                    "error": "Failed to update post",
                }
            ),
            500,
        )

    response_body: dict[str, Any] = {
        "guid": post.guid,
        "whitelisted": post.whitelisted,
        "message": "Whitelist status updated successfully",
    }

    trigger_processing = bool(data.get("trigger_processing"))
    if post.whitelisted and trigger_processing:
        billing_user_id = getattr(user, "id", None)
        job_response = get_jobs_manager().start_post_processing(
            post.guid,
            priority="interactive",
            requested_by_user_id=billing_user_id,
            billing_user_id=billing_user_id,
        )
        response_body["processing_job"] = job_response

    return flask.jsonify(response_body)


@post_bp.route("/api/feeds/<int:feed_id>/toggle-whitelist-all", methods=["POST"])
def api_toggle_whitelist_all(feed_id: int) -> ResponseReturnValue:
    """Intelligently toggle whitelist status for all posts in a feed.

    Admin only.
    """
    feed = db.get_or_404(Feed, feed_id)

    _, error = require_admin("toggle whitelist for all posts")
    if error:
        return error

    # Count in SQLite rather than materializing the entire archive (including
    # large transcript JSON) in a long-lived request worker.
    total, whitelisted = (
        db.session.query(
            func.count(Post.id),
            func.count(Post.id).filter(Post.whitelisted.is_(True)),
        )
        .filter(Post.feed_id == feed.id)
        .one()
    )
    if not total:
        return flask.jsonify(
            {
                "message": "No posts found in this feed",
                "whitelisted_count": 0,
                "total_count": 0,
            }
        )

    all_whitelisted = whitelisted == total
    new_status = not all_whitelisted

    try:
        result = writer_client.action(
            "toggle_whitelist_all_for_feed",
            {"feed_id": feed.id, "new_status": new_status},
            wait=True,
        )
        if not result or not result.success:
            raise RuntimeError(getattr(result, "error", "Unknown writer error"))
        updated = int((result.data or {}).get("updated_count") or 0)
    except Exception:  # noqa: BLE001
        return (
            flask.jsonify(
                {
                    "error": "Database busy, please retry",
                    "retry_after_seconds": 1,
                }
            ),
            503,
        )

    whitelisted_count = Post.query.filter_by(feed_id=feed.id, whitelisted=True).count()
    total_count = Post.query.filter_by(feed_id=feed.id).count()

    return flask.jsonify(
        {
            "message": f"{'Whitelisted' if new_status else 'Unwhitelisted'} all posts",
            "whitelisted_count": whitelisted_count,
            "total_count": total_count,
            "all_whitelisted": new_status,
            "updated_count": updated,
        }
    )


@post_bp.route("/api/posts/<path:p_guid>/process", methods=["POST"])
def api_process_post(p_guid: str) -> ResponseReturnValue:
    """Start processing a post and return immediately.

    Admin only.
    """
    post = Post.query.filter_by(guid=p_guid).first()
    if not post:
        return (
            flask.jsonify(
                {
                    "status": "error",
                    "error_code": "NOT_FOUND",
                    "message": "Post not found",
                }
            ),
            404,
        )

    feed = db.session.get(Feed, post.feed_id)
    if feed is None:
        return (
            flask.jsonify(
                {
                    "status": "error",
                    "error_code": "FEED_NOT_FOUND",
                    "message": "Feed not found",
                }
            ),
            404,
        )

    user, error = require_admin("process this episode")
    if error:
        return error

    if not post.whitelisted:
        return (
            flask.jsonify(
                {
                    "status": "error",
                    "error_code": "NOT_WHITELISTED",
                    "message": "Post not whitelisted",
                }
            ),
            400,
        )

    if post.processed_audio_path and os.path.exists(post.processed_audio_path):
        return flask.jsonify(
            {
                "status": "completed",
                "message": "Post already processed",
                "download_url": f"/api/posts/{p_guid}/download",
            }
        )

    billing_user_id = getattr(user, "id", None)

    try:
        result = get_jobs_manager().start_post_processing(
            p_guid,
            priority="interactive",
            requested_by_user_id=billing_user_id,
            billing_user_id=billing_user_id,
        )
        status_code = 200 if result.get("status") in ("started", "completed") else 400
        return flask.jsonify(result), status_code
    except Exception as e:  # noqa: BLE001
        logger.error(f"Failed to start processing job for {p_guid}: {e}")
        return (
            flask.jsonify(
                {
                    "status": "error",
                    "error_code": "JOB_START_FAILED",
                    "message": f"Failed to start processing job: {e!s}",
                }
            ),
            500,
        )


@post_bp.route("/api/posts/<path:p_guid>/reprocess", methods=["POST"])
def api_reprocess_post(p_guid: str) -> ResponseReturnValue:
    """Clear all processing data for a post and start processing from scratch.

    Admin only.
    """
    logger.info("[API] Reprocess requested for post_guid=%s", p_guid)

    post = Post.query.filter_by(guid=p_guid).first()
    if not post:
        logger.warning("[API] Reprocess: post not found for guid=%s", p_guid)
        return (
            flask.jsonify(
                {
                    "status": "error",
                    "error_code": "NOT_FOUND",
                    "message": "Post not found",
                }
            ),
            404,
        )

    feed = db.session.get(Feed, post.feed_id)
    if feed is None:
        logger.warning(
            "[API] Reprocess: feed not found for guid=%s feed_id=%s",
            p_guid,
            getattr(post, "feed_id", None),
        )
        return (
            flask.jsonify(
                {
                    "status": "error",
                    "error_code": "FEED_NOT_FOUND",
                    "message": "Feed not found",
                }
            ),
            404,
        )

    user, error = require_admin("reprocess this episode")
    if error:
        logger.warning("[API] Reprocess: auth error for guid=%s", p_guid)
        return error
    if user and user.role != "admin":
        logger.warning(
            "[API] Reprocess: non-admin user attempted reprocess guid=%s user_id=%s role=%s",
            p_guid,
            getattr(user, "id", None),
            getattr(user, "role", None),
        )
        return (
            flask.jsonify(
                {
                    "status": "error",
                    "error_code": "REPROCESS_FORBIDDEN",
                    "message": "Only admins can reprocess episodes.",
                }
            ),
            403,
        )

    if not post.whitelisted:
        logger.info(
            "[API] Reprocess: post not whitelisted guid=%s post_id=%s",
            p_guid,
            getattr(post, "id", None),
        )
        return (
            flask.jsonify(
                {
                    "status": "error",
                    "error_code": "NOT_WHITELISTED",
                    "message": "Post not whitelisted",
                }
            ),
            400,
        )

    billing_user_id = getattr(user, "id", None)

    try:
        logger.info(
            "[API] Reprocess: cancelling jobs and clearing processing data guid=%s post_id=%s",
            p_guid,
            getattr(post, "id", None),
        )
        get_jobs_manager().cancel_post_jobs(p_guid)
        clear_post_processing_data(post)
        logger.info(
            "[API] Reprocess: starting post processing guid=%s post_id=%s",
            p_guid,
            getattr(post, "id", None),
        )
        result = get_jobs_manager().start_post_processing(
            p_guid,
            priority="interactive",
            requested_by_user_id=billing_user_id,
            billing_user_id=billing_user_id,
        )
        status_code = 200 if result.get("status") in ("started", "completed") else 400
        if result.get("status") == "started":
            result["message"] = "Post cleared and reprocessing started"
        logger.info(
            "[API] Reprocess: completed guid=%s status=%s code=%s",
            p_guid,
            result.get("status"),
            status_code,
        )
        return flask.jsonify(result), status_code
    except Exception as e:
        logger.error(f"Failed to reprocess post {p_guid}: {e}", exc_info=True)
        return (
            flask.jsonify(
                {
                    "status": "error",
                    "error_code": "REPROCESS_FAILED",
                    "message": f"Failed to reprocess post: {e!s}",
                }
            ),
            500,
        )


@post_bp.route(
    "/api/posts/<path:p_guid>/reprocess/keep-transcript",
    methods=["POST"],
)
def api_reprocess_post_keep_transcript(p_guid: str) -> ResponseReturnValue:
    """Clear processing outputs but preserve transcript, then reprocess."""
    logger.info(
        "[API] Reprocess (keep transcript) requested for post_guid=%s",
        p_guid,
    )

    post = Post.query.filter_by(guid=p_guid).first()
    if not post:
        logger.warning(
            "[API] Reprocess (keep transcript): post not found for guid=%s", p_guid
        )
        return (
            flask.jsonify(
                {
                    "status": "error",
                    "error_code": "NOT_FOUND",
                    "message": "Post not found",
                }
            ),
            404,
        )

    feed = db.session.get(Feed, post.feed_id)
    if feed is None:
        logger.warning(
            "[API] Reprocess (keep transcript): feed not found for guid=%s feed_id=%s",
            p_guid,
            getattr(post, "feed_id", None),
        )
        return (
            flask.jsonify(
                {
                    "status": "error",
                    "error_code": "FEED_NOT_FOUND",
                    "message": "Feed not found",
                }
            ),
            404,
        )

    user, error = require_admin("reprocess this episode (keep transcript)")
    if error:
        logger.warning(
            "[API] Reprocess (keep transcript): auth error for guid=%s",
            p_guid,
        )
        return error
    if user and user.role != "admin":
        return (
            flask.jsonify(
                {
                    "status": "error",
                    "error_code": "REPROCESS_FORBIDDEN",
                    "message": "Only admins can reprocess episodes.",
                }
            ),
            403,
        )

    if not post.whitelisted:
        return (
            flask.jsonify(
                {
                    "status": "error",
                    "error_code": "NOT_WHITELISTED",
                    "message": "Post not whitelisted",
                }
            ),
            400,
        )

    from podcast_processor.transcription_manager import TranscriptionManager

    transcription_manager = TranscriptionManager(
        logger=logger,
        config=runtime_config,
        db_session=db.session,
    )
    reusable_transcript_segments = transcription_manager.get_reusable_transcription(
        post
    )
    if reusable_transcript_segments is None:
        transcript_count = (
            db.session.query(TranscriptSegment.id).filter_by(post_id=post.id).count()
        )
        logger.warning(
            "[API] Reprocess (keep transcript): no reusable transcript for guid=%s "
            "post_id=%s transcript_count=%s active_whisper_model=%s",
            p_guid,
            post.id,
            transcript_count,
            transcription_manager.transcriber.model_name,
        )
        return (
            flask.jsonify(
                {
                    "status": "error",
                    "error_code": "NO_REUSABLE_TRANSCRIPT",
                    "message": (
                        "No reusable transcript found for the currently configured "
                        "transcription model. Use full reprocess instead."
                    ),
                }
            ),
            400,
        )

    billing_user_id = getattr(user, "id", None)

    try:
        logger.info(
            "[API] Reprocess (keep transcript): cancelling jobs and clearing outputs "
            "guid=%s post_id=%s",
            p_guid,
            post.id,
        )
        get_jobs_manager().cancel_post_jobs(p_guid)
        clear_post_processing_data_keep_transcript(post)

        logger.info(
            "[API] Reprocess (keep transcript): starting post processing "
            "guid=%s post_id=%s",
            p_guid,
            post.id,
        )
        result = get_jobs_manager().start_post_processing(
            p_guid,
            priority="interactive",
            requested_by_user_id=billing_user_id,
            billing_user_id=billing_user_id,
        )
        status_code = 200 if result.get("status") in ("started", "completed") else 400
        if result.get("status") == "started":
            result["message"] = "Post reprocessing started (keeping transcript)"
        logger.info(
            "[API] Reprocess (keep transcript): completed guid=%s status=%s code=%s",
            p_guid,
            result.get("status"),
            status_code,
        )
        return flask.jsonify(result), status_code
    except Exception as e:
        logger.error(
            "Failed to reprocess post (keep transcript) %s: %s",
            p_guid,
            e,
            exc_info=True,
        )
        return (
            flask.jsonify(
                {
                    "status": "error",
                    "error_code": "REPROCESS_FAILED",
                    "message": f"Failed to reprocess post (keep transcript): {e!s}",
                }
            ),
            500,
        )


@post_bp.route("/api/posts/<path:p_guid>/status", methods=["GET"])
def api_post_status(p_guid: str) -> ResponseReturnValue:
    """Get the current processing status of a post via JobsManager."""
    result = get_jobs_manager().get_post_status(p_guid)
    status_code = (
        200
        if result.get("status") != "error"
        else (404 if result.get("error_code") == "NOT_FOUND" else 400)
    )
    return flask.jsonify(result), status_code


@post_bp.route("/api/posts/<path:p_guid>/audio", methods=["GET"])
def api_get_post_audio(p_guid: str) -> ResponseReturnValue:
    """API endpoint to serve processed audio files with proper CORS headers."""
    current_user = getattr(g, "current_user", None)
    if current_user:
        update_user_last_active(current_user.id)

    logger.info(f"API request for audio file with GUID: {p_guid}")

    post = Post.query.filter_by(guid=p_guid).first()
    if post is None:
        logger.warning(f"Post with GUID: {p_guid} not found")
        return flask.make_response(
            jsonify({"error": "Post not found", "error_code": "NOT_FOUND"}), 404
        )

    whitelist_response = ensure_whitelisted_for_download(post, p_guid)
    if whitelist_response:
        return whitelist_response

    if not post.processed_audio_path or not Path(post.processed_audio_path).exists():
        return missing_processed_audio_response(post, p_guid)

    try:
        response = send_file(
            path_or_file=Path(post.processed_audio_path).resolve(),
            mimetype="audio/mpeg",
            as_attachment=False,
            conditional=True,
        )
        response.headers["Accept-Ranges"] = "bytes"
        if should_increment_download_count_for_request():
            increment_download_count(post)
        return response
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error serving audio file for {p_guid}: {e}")
        return flask.make_response(
            jsonify(
                {"error": "Error serving audio file", "error_code": "SERVER_ERROR"}
            ),
            500,
        )


@post_bp.route("/api/posts/<path:p_guid>/chapters", methods=["GET"])
def api_get_post_chapters(p_guid: str) -> flask.Response:
    """Return chapter metadata embedded in the processed MP3, if present."""
    post = Post.query.filter_by(guid=p_guid).first()
    if post is None:
        return flask.make_response(
            jsonify({"error": "Post not found", "error_code": "NOT_FOUND"}), 404
        )

    whitelist_response = ensure_whitelisted_for_download(post, p_guid)
    if whitelist_response:
        return whitelist_response

    if not post.processed_audio_path or not Path(post.processed_audio_path).exists():
        return missing_processed_audio_response(post, p_guid)

    chapters = read_chapters(str(Path(post.processed_audio_path).resolve()))
    return flask.jsonify(
        {
            "chapters": [
                {
                    "title": chapter.title,
                    "start_time": round(chapter.start_time_ms / 1000.0, 3),
                    "end_time": round(chapter.end_time_ms / 1000.0, 3),
                }
                for chapter in chapters
            ]
        }
    )


@post_bp.route("/api/posts/<path:p_guid>/download", methods=["GET"])
def api_download_post(p_guid: str) -> flask.Response:
    """API endpoint to download processed audio files."""
    current_user = getattr(g, "current_user", None)
    if current_user:
        update_user_last_active(current_user.id)

    logger.info(f"Request to download post with GUID: {p_guid}")
    post = Post.query.filter_by(guid=p_guid).first()
    if post is None:
        logger.warning(f"Post with GUID: {p_guid} not found")
        return flask.make_response(("Post not found", 404))

    whitelist_response = ensure_whitelisted_for_download(post, p_guid)
    if whitelist_response:
        return whitelist_response

    if not post.processed_audio_path or not Path(post.processed_audio_path).exists():
        return missing_processed_audio_response(post, p_guid)

    try:
        response = send_file(
            path_or_file=Path(post.processed_audio_path).resolve(),
            mimetype="audio/mpeg",
            as_attachment=True,
            download_name=f"{post.title}.mp3",
            conditional=True,
        )
        response.headers["Accept-Ranges"] = "bytes"
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error serving file for {p_guid}: {e}")
        return flask.make_response(("Error serving file", 500))

    if should_increment_download_count_for_request():
        increment_download_count(post)
    return response


@post_bp.route("/api/posts/<path:p_guid>/download/original", methods=["GET"])
def api_download_original_post(p_guid: str) -> flask.Response:
    """API endpoint to download original (unprocessed) audio files."""
    logger.info(f"Request to download original post with GUID: {p_guid}")
    post = Post.query.filter_by(guid=p_guid).first()
    if post is None:
        logger.warning(f"Post with GUID: {p_guid} not found")
        return flask.make_response(("Post not found", 404))

    if not post.whitelisted:
        logger.warning(f"Post: {post.title} is not whitelisted")
        return flask.make_response(("Post not whitelisted", 403))

    if (
        not post.unprocessed_audio_path
        or not Path(post.unprocessed_audio_path).exists()
    ):
        logger.warning(f"Original audio not found for post: {post.id}")
        return flask.make_response(("Original audio not found", 404))

    try:
        response = send_file(
            path_or_file=Path(post.unprocessed_audio_path).resolve(),
            mimetype="audio/mpeg",
            as_attachment=True,
            download_name=f"{post.title}_original.mp3",
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error serving original file for {p_guid}: {e}")
        return flask.make_response(("Error serving file", 500))

    if should_increment_download_count_for_request():
        increment_download_count(post)
    return response


@post_bp.route("/post/<path:p_guid>/chapters.json", methods=["GET"])
def podcast_chapters_json(p_guid: str) -> flask.Response:
    """Podcasting 2.0 JSON chapters for the feed's `<podcast:chapters>` tag."""
    post = Post.query.filter_by(guid=p_guid).first()
    if post is None:
        return flask.make_response(
            jsonify({"error": "Post not found", "error_code": "NOT_FOUND"}), 404
        )

    whitelist_response = ensure_whitelisted_for_download(post, p_guid)
    if whitelist_response:
        return whitelist_response

    return jsonify(
        {
            "version": "1.2.0",
            "chapters": [
                {"startTime": start_time, "title": title}
                for start_time, title in feed_chapters(post)
            ],
        }
    )


# Legacy endpoints for backward compatibility
@post_bp.route("/post/<path:p_guid>.mp3", methods=["GET"])
def download_post_legacy(p_guid: str) -> ResponseReturnValue:
    return api_get_post_audio(p_guid)


@post_bp.route("/post/<path:p_guid>/original.mp3", methods=["GET"])
def download_original_post_legacy(p_guid: str) -> flask.Response:
    return api_download_original_post(p_guid)
