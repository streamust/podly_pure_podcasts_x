from __future__ import annotations

import logging
import os
import warnings
from typing import Any

from flask import current_app

from app.db_commit import safe_commit
from app.extensions import db, scheduler
from app.models import (
    AppSettings,
    LLMSettings,
    NotificationSettings,
    OutputSettings,
    ProcessingSettings,
    WhisperSettings,
)
from app.runtime_config import config as runtime_config
from shared import defaults as DEFAULTS
from shared.config import Config as PydanticConfig
from shared.config import (
    GroqWhisperConfig,
    NotificationConfig,
    RemoteWhisperConfig,
    TestWhisperConfig,
)

logger = logging.getLogger("global_logger")


def _is_empty(value: Any) -> bool:
    return value is None or value == ""


def _parse_int(val: Any, *, env_name: str = "") -> int | None:
    if val is None or val == "":
        return None
    try:
        return int(val)
    except (ValueError, TypeError) as exc:
        logger.warning(
            "Environment variable %s has non-integer value %r; ignoring override: %s",
            env_name,
            val,
            exc,
        )
        return None


def _parse_bool(val: Any, *, env_name: str = "") -> bool | None:
    if val is None or val == "":
        return None
    s = str(val).strip().lower()
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    logger.warning(
        "Environment variable %s has unrecognized boolean value %r; ignoring override. "
        "Valid values: true/false, yes/no, 1/0, on/off",
        env_name,
        val,
    )
    return None


def _ensure_row(model: type, defaults: dict[str, Any]) -> Any:
    row = db.session.get(model, 1)
    if row is None:
        role = None
        try:
            role = current_app.config.get("PODLY_APP_ROLE")
        except Exception:  # noqa: BLE001
            role = None

        # Web and processing apps are read-only. The runtime writer and exclusive
        # one-shot bootstrap may create missing singleton settings rows.
        if role in {"writer", "bootstrap"}:
            row = model(id=1, **defaults)
            db.session.add(row)
            safe_commit(
                db.session,
                must_succeed=True,
                context="ensure_settings_row",
                logger_obj=logger,
            )
        else:
            logger.warning(
                "Settings row %s missing; returning defaults without persisting (role=%s)",
                getattr(model, "__name__", str(model)),
                role,
            )
            return model(id=1, **defaults)
    return row


def ensure_defaults() -> None:
    _ensure_row(
        LLMSettings,
        {
            "llm_model": DEFAULTS.LLM_DEFAULT_MODEL,
            "openai_timeout": DEFAULTS.OPENAI_DEFAULT_TIMEOUT_SEC,
            "openai_max_tokens": DEFAULTS.OPENAI_DEFAULT_MAX_TOKENS,
            "llm_max_concurrent_calls": DEFAULTS.LLM_DEFAULT_MAX_CONCURRENT_CALLS,
            "llm_max_retry_attempts": DEFAULTS.LLM_DEFAULT_MAX_RETRY_ATTEMPTS,
            "llm_enable_token_rate_limiting": DEFAULTS.LLM_ENABLE_TOKEN_RATE_LIMITING,
            "enable_boundary_refinement": DEFAULTS.ENABLE_BOUNDARY_REFINEMENT,
            "enable_word_level_boundary_refinder": DEFAULTS.ENABLE_WORD_LEVEL_BOUNDARY_REFINDER,
            "enable_llm_chapter_fallback_tagging": DEFAULTS.ENABLE_LLM_CHAPTER_FALLBACK_TAGGING,
            "chapter_full_block_text": DEFAULTS.CHAPTER_FULL_BLOCK_TEXT,
            "llm_service_tier": DEFAULTS.LLM_SERVICE_TIER,
        },
    )

    _ensure_row(
        WhisperSettings,
        {
            "whisper_type": DEFAULTS.WHISPER_DEFAULT_TYPE,
            "remote_model": DEFAULTS.WHISPER_REMOTE_MODEL,
            "remote_base_url": DEFAULTS.WHISPER_REMOTE_BASE_URL,
            "remote_language": DEFAULTS.WHISPER_REMOTE_LANGUAGE,
            "remote_timeout_sec": DEFAULTS.WHISPER_REMOTE_TIMEOUT_SEC,
            "remote_chunksize_mb": DEFAULTS.WHISPER_REMOTE_CHUNKSIZE_MB,
            "remote_diarize": DEFAULTS.WHISPER_REMOTE_DIARIZE,
            "remote_speaker_embeddings": DEFAULTS.WHISPER_REMOTE_SPEAKER_EMBEDDINGS,
            "groq_model": DEFAULTS.WHISPER_GROQ_MODEL,
            "groq_language": DEFAULTS.WHISPER_GROQ_LANGUAGE,
            "groq_max_retries": DEFAULTS.WHISPER_GROQ_MAX_RETRIES,
        },
    )

    _ensure_row(
        ProcessingSettings,
        {
            "num_segments_to_input_to_prompt": DEFAULTS.PROCESSING_NUM_SEGMENTS_TO_INPUT_TO_PROMPT,
        },
    )

    _ensure_row(
        OutputSettings,
        {
            "fade_ms": DEFAULTS.OUTPUT_FADE_MS,
            "bleep_padding_start_ms": DEFAULTS.OUTPUT_BLEEP_PADDING_START_MS,
            "bleep_padding_end_ms": DEFAULTS.OUTPUT_BLEEP_PADDING_END_MS,
            "min_ad_segement_separation_seconds": DEFAULTS.OUTPUT_MIN_AD_SEGMENT_SEPARATION_SECONDS,
            "min_ad_segment_length_seconds": DEFAULTS.OUTPUT_MIN_AD_SEGMENT_LENGTH_SECONDS,
            "min_confidence": DEFAULTS.OUTPUT_MIN_CONFIDENCE,
            "auto_retry_zero_ads_on_parse_error": DEFAULTS.OUTPUT_AUTO_RETRY_ZERO_ADS_ON_PARSE_ERROR,
        },
    )

    _ensure_row(
        AppSettings,
        {
            "background_update_interval_minute": DEFAULTS.APP_BACKGROUND_UPDATE_INTERVAL_MINUTE,
            "automatically_whitelist_new_episodes": DEFAULTS.APP_AUTOMATICALLY_WHITELIST_NEW_EPISODES,
            "post_cleanup_retention_days": DEFAULTS.APP_POST_CLEANUP_RETENTION_DAYS,
            "number_of_episodes_to_whitelist_from_archive_of_new_feed": DEFAULTS.APP_NUM_EPISODES_TO_WHITELIST_FROM_ARCHIVE_OF_NEW_FEED,
            "enable_public_landing_page": DEFAULTS.APP_ENABLE_PUBLIC_LANDING_PAGE,
            "user_limit_total": DEFAULTS.APP_USER_LIMIT_TOTAL,
            "autoprocess_on_download": DEFAULTS.APP_AUTOPROCESS_ON_DOWNLOAD,
            "cost_rate_per_hour": DEFAULTS.APP_COST_RATE_PER_HOUR,
            "whisper_cost_rate_per_hour": DEFAULTS.APP_WHISPER_COST_RATE_PER_HOUR,
            "ina_cost_rate_per_hour": DEFAULTS.APP_INA_COST_RATE_PER_HOUR,
        },
    )

    _ensure_row(
        NotificationSettings,
        {
            "enabled": DEFAULTS.NOTIFY_ENABLED,
            "apprise_urls": None,
            "notify_on_failure": DEFAULTS.NOTIFY_ON_FAILURE,
            "notify_on_success": DEFAULTS.NOTIFY_ON_SUCCESS,
            "notify_on_rust_fallback": DEFAULTS.NOTIFY_ON_RUST_FALLBACK,
            "include_llm_explanation": DEFAULTS.NOTIFY_INCLUDE_LLM_EXPLANATION,
        },
    )


def _apprise_urls_to_list(raw: str | None) -> list[str]:
    """Split the stored newline-separated Apprise URLs into a clean list."""
    if not raw:
        return []
    return [line.strip() for line in raw.splitlines() if line.strip()]


def read_combined() -> dict[str, Any]:
    ensure_defaults()

    llm = db.session.get(LLMSettings, 1)
    whisper = db.session.get(WhisperSettings, 1)
    processing = db.session.get(ProcessingSettings, 1)
    output = db.session.get(OutputSettings, 1)
    app_s = db.session.get(AppSettings, 1)
    notifications = db.session.get(NotificationSettings, 1)

    assert llm and whisper and processing and output and app_s and notifications

    whisper_payload: dict[str, Any] = {"whisper_type": whisper.whisper_type}
    if whisper.whisper_type == "local":
        raise RuntimeError(
            "Stored whisper_type='local' is no longer supported. Configure "
            "WHISPER_TYPE=remote with WHISPER_REMOTE_BASE_URL for a dedicated "
            "OpenAI-compatible transcription service."
        )
    elif whisper.whisper_type == "remote":
        whisper_payload.update(
            {
                "model": whisper.remote_model,
                "api_key": whisper.remote_api_key,
                "base_url": whisper.remote_base_url,
                "language": whisper.remote_language,
                "timeout_sec": whisper.remote_timeout_sec,
                "chunksize_mb": whisper.remote_chunksize_mb,
                "diarize": whisper.remote_diarize,
                "speaker_embeddings": whisper.remote_speaker_embeddings,
            }
        )
    elif whisper.whisper_type == "groq":
        whisper_payload.update(
            {
                "api_key": whisper.groq_api_key,
                "model": whisper.groq_model,
                "language": whisper.groq_language,
                "max_retries": whisper.groq_max_retries,
            }
        )
    elif whisper.whisper_type == "test":
        whisper_payload.update({})

    return {
        "llm": {
            "llm_api_key": llm.llm_api_key,
            "llm_model": llm.llm_model,
            "openai_base_url": llm.openai_base_url,
            "openai_timeout": llm.openai_timeout,
            "openai_max_tokens": llm.openai_max_tokens,
            "llm_max_concurrent_calls": llm.llm_max_concurrent_calls,
            "llm_max_retry_attempts": llm.llm_max_retry_attempts,
            "llm_max_input_tokens_per_call": llm.llm_max_input_tokens_per_call,
            "llm_enable_token_rate_limiting": llm.llm_enable_token_rate_limiting,
            "llm_max_input_tokens_per_minute": llm.llm_max_input_tokens_per_minute,
            "enable_boundary_refinement": llm.enable_boundary_refinement,
            "enable_word_level_boundary_refinder": llm.enable_word_level_boundary_refinder,
            "enable_llm_chapter_fallback_tagging": llm.enable_llm_chapter_fallback_tagging,
            "chapter_full_block_text": llm.chapter_full_block_text,
            "llm_service_tier": llm.llm_service_tier,
        },
        "whisper": whisper_payload,
        "processing": {
            "num_segments_to_input_to_prompt": processing.num_segments_to_input_to_prompt,
        },
        "output": {
            "fade_ms": output.fade_ms,
            "bleep_padding_start_ms": output.bleep_padding_start_ms,
            "bleep_padding_end_ms": output.bleep_padding_end_ms,
            "min_ad_segement_separation_seconds": output.min_ad_segement_separation_seconds,
            "min_ad_segment_length_seconds": output.min_ad_segment_length_seconds,
            "min_confidence": output.min_confidence,
            "auto_retry_zero_ads_on_parse_error": output.auto_retry_zero_ads_on_parse_error,
        },
        "app": {
            "background_update_interval_minute": app_s.background_update_interval_minute,
            "automatically_whitelist_new_episodes": app_s.automatically_whitelist_new_episodes,
            "post_cleanup_retention_days": app_s.post_cleanup_retention_days,
            "number_of_episodes_to_whitelist_from_archive_of_new_feed": app_s.number_of_episodes_to_whitelist_from_archive_of_new_feed,
            "enable_public_landing_page": app_s.enable_public_landing_page,
            "user_limit_total": app_s.user_limit_total,
            "autoprocess_on_download": app_s.autoprocess_on_download,
            "cost_rate_per_hour": app_s.cost_rate_per_hour,
            "whisper_cost_rate_per_hour": app_s.whisper_cost_rate_per_hour,
            "ina_cost_rate_per_hour": app_s.ina_cost_rate_per_hour,
        },
        "notifications": {
            "enabled": notifications.enabled,
            "apprise_urls": _apprise_urls_to_list(notifications.apprise_urls),
            "notify_on_failure": notifications.notify_on_failure,
            "notify_on_success": notifications.notify_on_success,
            "notify_on_rust_fallback": notifications.notify_on_rust_fallback,
            "include_llm_explanation": notifications.include_llm_explanation,
        },
    }


def _update_section_llm(data: dict[str, Any]) -> None:
    row = db.session.get(LLMSettings, 1)
    assert row is not None
    for key in [
        "llm_api_key",
        "llm_model",
        "openai_base_url",
        "openai_timeout",
        "openai_max_tokens",
        "llm_max_concurrent_calls",
        "llm_max_retry_attempts",
        "llm_max_input_tokens_per_call",
        "llm_enable_token_rate_limiting",
        "llm_max_input_tokens_per_minute",
        "enable_boundary_refinement",
        "enable_word_level_boundary_refinder",
        "enable_llm_chapter_fallback_tagging",
        "chapter_full_block_text",
        "llm_service_tier",
    ]:
        if key in data:
            new_val = data[key]
            if key == "llm_api_key" and _is_empty(new_val):
                continue
            setattr(row, key, new_val)
    safe_commit(
        db.session,
        must_succeed=True,
        context="update_llm_settings",
        logger_obj=logger,
    )


def _update_section_whisper(data: dict[str, Any]) -> None:
    row = db.session.get(WhisperSettings, 1)
    assert row is not None
    if "whisper_type" in data and data["whisper_type"] in {
        "remote",
        "groq",
        "test",
    }:
        row.whisper_type = data["whisper_type"]
    if row.whisper_type == "remote":
        for key_map in [
            ("model", "remote_model"),
            ("api_key", "remote_api_key"),
            ("base_url", "remote_base_url"),
            ("language", "remote_language"),
            ("timeout_sec", "remote_timeout_sec"),
            ("chunksize_mb", "remote_chunksize_mb"),
            ("diarize", "remote_diarize"),
            ("speaker_embeddings", "remote_speaker_embeddings"),
        ]:
            src, dst = key_map
            if src in data:
                new_val = data[src]
                if src == "api_key" and _is_empty(new_val):
                    continue
                setattr(row, dst, new_val)
    elif row.whisper_type == "groq":
        for key_map in [
            ("api_key", "groq_api_key"),
            ("model", "groq_model"),
            ("language", "groq_language"),
            ("max_retries", "groq_max_retries"),
        ]:
            src, dst = key_map
            if src in data:
                new_val = data[src]
                if src == "api_key" and _is_empty(new_val):
                    continue
                setattr(row, dst, new_val)
    else:
        # test type has no extra fields
        pass
    safe_commit(
        db.session,
        must_succeed=True,
        context="update_whisper_settings",
        logger_obj=logger,
    )


def _update_section_processing(data: dict[str, Any]) -> None:
    row = db.session.get(ProcessingSettings, 1)
    assert row is not None
    for key in [
        "num_segments_to_input_to_prompt",
    ]:
        if key in data:
            setattr(row, key, data[key])
    safe_commit(
        db.session,
        must_succeed=True,
        context="update_processing_settings",
        logger_obj=logger,
    )


def _update_section_output(data: dict[str, Any]) -> None:
    row = db.session.get(OutputSettings, 1)
    assert row is not None
    for key in [
        "fade_ms",
        "bleep_padding_start_ms",
        "bleep_padding_end_ms",
        "min_ad_segement_separation_seconds",
        "min_ad_segment_length_seconds",
        "min_confidence",
        "auto_retry_zero_ads_on_parse_error",
    ]:
        if key in data:
            setattr(row, key, data[key])
    safe_commit(
        db.session,
        must_succeed=True,
        context="update_output_settings",
        logger_obj=logger,
    )


def _update_section_app(data: dict[str, Any]) -> tuple[int | None, int | None]:
    row = db.session.get(AppSettings, 1)
    assert row is not None
    old_interval: int | None = row.background_update_interval_minute
    old_retention: int | None = row.post_cleanup_retention_days
    for key in [
        "background_update_interval_minute",
        "automatically_whitelist_new_episodes",
        "post_cleanup_retention_days",
        "number_of_episodes_to_whitelist_from_archive_of_new_feed",
        "enable_public_landing_page",
        "user_limit_total",
        "autoprocess_on_download",
        "cost_rate_per_hour",
        "whisper_cost_rate_per_hour",
        "ina_cost_rate_per_hour",
    ]:
        if key in data:
            setattr(row, key, data[key])
    safe_commit(
        db.session,
        must_succeed=True,
        context="update_app_settings",
        logger_obj=logger,
    )
    return old_interval, old_retention


def _update_section_notifications(data: dict[str, Any]) -> None:
    row = db.session.get(NotificationSettings, 1)
    assert row is not None
    if "enabled" in data:
        row.enabled = bool(data["enabled"])
    if "notify_on_failure" in data:
        row.notify_on_failure = bool(data["notify_on_failure"])
    if "notify_on_success" in data:
        row.notify_on_success = bool(data["notify_on_success"])
    if "notify_on_rust_fallback" in data:
        row.notify_on_rust_fallback = bool(data["notify_on_rust_fallback"])
    if "include_llm_explanation" in data:
        row.include_llm_explanation = bool(data["include_llm_explanation"])
    if "apprise_urls" in data:
        urls = data["apprise_urls"]
        if isinstance(urls, str):
            url_list = _apprise_urls_to_list(urls)
        elif isinstance(urls, list):
            url_list = [str(u).strip() for u in urls if str(u).strip()]
        else:
            url_list = []
        row.apprise_urls = "\n".join(url_list) if url_list else None
    safe_commit(
        db.session,
        must_succeed=True,
        context="update_notification_settings",
        logger_obj=logger,
    )


def _maybe_reschedule_refresh_job(
    old_interval: int | None, new_interval: int | None
) -> None:
    if old_interval == new_interval:
        return

    job_id = "refresh_all_feeds"
    job = scheduler.get_job(job_id)

    if new_interval is None:
        if job:
            try:
                scheduler.remove_job(job_id)
            except Exception:  # noqa: BLE001
                pass
        return

    if not job:
        return

    # Avoid importing app.background here (it creates a circular import).
    # Use best-effort rescheduling on the underlying APScheduler instance.
    scheduler_obj = getattr(scheduler, "scheduler", scheduler)
    reschedule = getattr(scheduler_obj, "reschedule_job", None)
    if callable(reschedule):
        reschedule(job_id, trigger="interval", minutes=int(new_interval))


def _maybe_disable_cleanup_job(
    old_retention: int | None, new_retention: int | None
) -> None:
    if old_retention == new_retention:
        return

    job_id = "cleanup_processed_posts"
    job = scheduler.get_job(job_id)

    if new_retention is None or new_retention <= 0:
        if job:
            try:
                scheduler.remove_job(job_id)
            except Exception:  # noqa: BLE001
                pass


def apply_app_scheduler_side_effects(
    previous: dict[str, Any], current: dict[str, Any]
) -> None:
    """Reconcile scheduler state in the process that serves config updates."""
    _maybe_reschedule_refresh_job(
        previous.get("background_update_interval_minute"),
        current.get("background_update_interval_minute"),
    )
    _maybe_disable_cleanup_job(
        previous.get("post_cleanup_retention_days"),
        current.get("post_cleanup_retention_days"),
    )


def update_combined(payload: dict[str, Any]) -> dict[str, Any]:
    if "llm" in payload:
        _update_section_llm(payload["llm"] or {})
    if "whisper" in payload:
        _update_section_whisper(payload["whisper"] or {})
    if "processing" in payload:
        _update_section_processing(payload["processing"] or {})
    if "output" in payload:
        _update_section_output(payload["output"] or {})
    if "app" in payload:
        old_interval, old_retention = _update_section_app(payload["app"] or {})

        app_s = db.session.get(AppSettings, 1)
        if app_s:
            _maybe_reschedule_refresh_job(
                old_interval, app_s.background_update_interval_minute
            )
            _maybe_disable_cleanup_job(old_retention, app_s.post_cleanup_retention_days)
    if "notifications" in payload:
        _update_section_notifications(payload["notifications"] or {})

    return read_combined()


def to_pydantic_config() -> PydanticConfig:
    data = read_combined()
    # Map whisper section to discriminated union config
    whisper_obj: RemoteWhisperConfig | TestWhisperConfig | GroqWhisperConfig | None = (
        None
    )
    w = data["whisper"]
    wtype = w.get("whisper_type")
    if wtype == "local":
        raise RuntimeError(
            "Stored whisper_type='local' is no longer supported. Configure "
            "WHISPER_TYPE=remote with WHISPER_REMOTE_BASE_URL for a dedicated "
            "OpenAI-compatible transcription service."
        )
    elif wtype == "remote":
        whisper_obj = RemoteWhisperConfig(
            model=w.get("model", "whisper-1"),
            # Allow boot without a remote API key so the UI can be used to set it
            api_key=w.get("api_key") or "",
            base_url=w.get("base_url", "https://api.openai.com/v1"),
            language=w.get("language", "en"),
            timeout_sec=w.get("timeout_sec", 600),
            chunksize_mb=w.get("chunksize_mb", 24),
            diarize=bool(w.get("diarize", DEFAULTS.WHISPER_REMOTE_DIARIZE)),
            speaker_embeddings=bool(
                w.get(
                    "speaker_embeddings",
                    DEFAULTS.WHISPER_REMOTE_SPEAKER_EMBEDDINGS,
                )
            ),
        )
    elif wtype == "groq":
        whisper_obj = GroqWhisperConfig(
            # Allow boot without a Groq API key so the UI can be used to set it
            api_key=w.get("api_key") or "",
            model=w.get("model", DEFAULTS.WHISPER_GROQ_MODEL),
            language=w.get("language", "en"),
            max_retries=w.get("max_retries", 3),
        )
    elif wtype == "test":
        whisper_obj = TestWhisperConfig()

    return PydanticConfig(
        llm_api_key=data["llm"].get("llm_api_key"),
        llm_model=data["llm"].get("llm_model", DEFAULTS.LLM_DEFAULT_MODEL),
        openai_base_url=data["llm"].get("openai_base_url"),
        openai_max_tokens=int(
            data["llm"].get("openai_max_tokens", DEFAULTS.OPENAI_DEFAULT_MAX_TOKENS)
            or DEFAULTS.OPENAI_DEFAULT_MAX_TOKENS
        ),
        openai_timeout=int(
            data["llm"].get("openai_timeout", DEFAULTS.OPENAI_DEFAULT_TIMEOUT_SEC)
            or DEFAULTS.OPENAI_DEFAULT_TIMEOUT_SEC
        ),
        llm_max_concurrent_calls=int(
            data["llm"].get(
                "llm_max_concurrent_calls", DEFAULTS.LLM_DEFAULT_MAX_CONCURRENT_CALLS
            )
            or DEFAULTS.LLM_DEFAULT_MAX_CONCURRENT_CALLS
        ),
        llm_max_retry_attempts=int(
            data["llm"].get(
                "llm_max_retry_attempts", DEFAULTS.LLM_DEFAULT_MAX_RETRY_ATTEMPTS
            )
            or DEFAULTS.LLM_DEFAULT_MAX_RETRY_ATTEMPTS
        ),
        llm_max_input_tokens_per_call=data["llm"].get("llm_max_input_tokens_per_call"),
        llm_enable_token_rate_limiting=bool(
            data["llm"].get(
                "llm_enable_token_rate_limiting",
                DEFAULTS.LLM_ENABLE_TOKEN_RATE_LIMITING,
            )
        ),
        llm_max_input_tokens_per_minute=data["llm"].get(
            "llm_max_input_tokens_per_minute"
        ),
        enable_boundary_refinement=bool(
            data["llm"].get(
                "enable_boundary_refinement",
                DEFAULTS.ENABLE_BOUNDARY_REFINEMENT,
            )
        ),
        enable_word_level_boundary_refinder=bool(
            data["llm"].get(
                "enable_word_level_boundary_refinder",
                DEFAULTS.ENABLE_WORD_LEVEL_BOUNDARY_REFINDER,
            )
        ),
        enable_llm_chapter_fallback_tagging=bool(
            data["llm"].get(
                "enable_llm_chapter_fallback_tagging",
                DEFAULTS.ENABLE_LLM_CHAPTER_FALLBACK_TAGGING,
            )
        ),
        chapter_full_block_text=bool(
            data["llm"].get(
                "chapter_full_block_text",
                DEFAULTS.CHAPTER_FULL_BLOCK_TEXT,
            )
        ),
        llm_service_tier=str(
            data["llm"].get(
                "llm_service_tier",
                DEFAULTS.LLM_SERVICE_TIER,
            )
            or DEFAULTS.LLM_SERVICE_TIER
        ),
        output=data["output"],
        processing=data["processing"],
        background_update_interval_minute=data["app"].get(
            "background_update_interval_minute"
        ),
        post_cleanup_retention_days=data["app"].get("post_cleanup_retention_days"),
        whisper=whisper_obj,
        automatically_whitelist_new_episodes=bool(
            data["app"].get(
                "automatically_whitelist_new_episodes",
                DEFAULTS.APP_AUTOMATICALLY_WHITELIST_NEW_EPISODES,
            )
        ),
        number_of_episodes_to_whitelist_from_archive_of_new_feed=int(
            data["app"].get(
                "number_of_episodes_to_whitelist_from_archive_of_new_feed",
                DEFAULTS.APP_NUM_EPISODES_TO_WHITELIST_FROM_ARCHIVE_OF_NEW_FEED,
            )
            or DEFAULTS.APP_NUM_EPISODES_TO_WHITELIST_FROM_ARCHIVE_OF_NEW_FEED
        ),
        enable_public_landing_page=bool(
            data["app"].get(
                "enable_public_landing_page",
                DEFAULTS.APP_ENABLE_PUBLIC_LANDING_PAGE,
            )
        ),
        user_limit_total=data["app"].get(
            "user_limit_total", DEFAULTS.APP_USER_LIMIT_TOTAL
        ),
        autoprocess_on_download=bool(
            data["app"].get(
                "autoprocess_on_download",
                DEFAULTS.APP_AUTOPROCESS_ON_DOWNLOAD,
            )
        ),
        cost_rate_per_hour=float(
            data["app"].get(
                "cost_rate_per_hour",
                DEFAULTS.APP_COST_RATE_PER_HOUR,
            )
        ),
        whisper_cost_rate_per_hour=float(
            data["app"].get(
                "whisper_cost_rate_per_hour",
                DEFAULTS.APP_WHISPER_COST_RATE_PER_HOUR,
            )
        ),
        ina_cost_rate_per_hour=float(
            data["app"].get(
                "ina_cost_rate_per_hour",
                DEFAULTS.APP_INA_COST_RATE_PER_HOUR,
            )
        ),
        notifications=NotificationConfig(
            enabled=bool(data["notifications"].get("enabled", DEFAULTS.NOTIFY_ENABLED)),
            apprise_urls=list(data["notifications"].get("apprise_urls", []) or []),
            notify_on_failure=bool(
                data["notifications"].get(
                    "notify_on_failure", DEFAULTS.NOTIFY_ON_FAILURE
                )
            ),
            notify_on_success=bool(
                data["notifications"].get(
                    "notify_on_success", DEFAULTS.NOTIFY_ON_SUCCESS
                )
            ),
            notify_on_rust_fallback=bool(
                data["notifications"].get(
                    "notify_on_rust_fallback", DEFAULTS.NOTIFY_ON_RUST_FALLBACK
                )
            ),
            include_llm_explanation=bool(
                data["notifications"].get(
                    "include_llm_explanation",
                    DEFAULTS.NOTIFY_INCLUDE_LLM_EXPLANATION,
                )
            ),
        ),
    )


def hydrate_runtime_config_inplace(db_config: PydanticConfig | None = None) -> None:
    """Hydrate the in-process runtime config from DB-backed settings in-place.

    Preserves the identity of the `app.config` Pydantic instance so any modules
    that imported it by value continue to see updated fields.
    """
    cfg = db_config or to_pydantic_config()

    _log_initial_snapshot(cfg)

    _apply_top_level_env_overrides(cfg)

    _apply_whisper_env_overrides(cfg)

    _apply_llm_model_override(cfg)

    _apply_whisper_type_override(cfg)

    _commit_runtime_config(cfg)
    _log_final_snapshot()


def _log_initial_snapshot(cfg: PydanticConfig) -> None:
    logger.info(
        "Config hydration: starting with DB values | whisper_type=%s llm_model=%s openai_base_url=%s llm_api_key_set=%s whisper_api_key_set=%s",
        getattr(getattr(cfg, "whisper", None), "whisper_type", None),
        getattr(cfg, "llm_model", None),
        getattr(cfg, "openai_base_url", None),
        bool(getattr(cfg, "llm_api_key", None)),
        bool(getattr(getattr(cfg, "whisper", None), "api_key", None)),
    )


def _apply_top_level_env_overrides(cfg: PydanticConfig) -> None:
    env_llm_key = (
        os.environ.get("LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("GROQ_API_KEY")
    )
    if env_llm_key:
        cfg.llm_api_key = env_llm_key

    env_openai_base_url = os.environ.get("OPENAI_BASE_URL")
    if env_openai_base_url:
        cfg.openai_base_url = env_openai_base_url

    env_openai_timeout = _parse_int(
        os.environ.get("OPENAI_TIMEOUT"), env_name="OPENAI_TIMEOUT"
    )
    if env_openai_timeout is not None:
        cfg.openai_timeout = env_openai_timeout

    env_openai_max_tokens = _parse_int(
        os.environ.get("OPENAI_MAX_TOKENS"), env_name="OPENAI_MAX_TOKENS"
    )
    if env_openai_max_tokens is not None:
        cfg.openai_max_tokens = env_openai_max_tokens

    env_llm_max_concurrent = _parse_int(
        os.environ.get("LLM_MAX_CONCURRENT_CALLS"), env_name="LLM_MAX_CONCURRENT_CALLS"
    )
    if env_llm_max_concurrent is not None:
        cfg.llm_max_concurrent_calls = env_llm_max_concurrent

    env_llm_max_retries = _parse_int(
        os.environ.get("LLM_MAX_RETRY_ATTEMPTS"), env_name="LLM_MAX_RETRY_ATTEMPTS"
    )
    if env_llm_max_retries is not None:
        cfg.llm_max_retry_attempts = env_llm_max_retries

    env_llm_enable_token_rl = _parse_bool(
        os.environ.get("LLM_ENABLE_TOKEN_RATE_LIMITING"),
        env_name="LLM_ENABLE_TOKEN_RATE_LIMITING",
    )
    if env_llm_enable_token_rl is not None:
        cfg.llm_enable_token_rate_limiting = env_llm_enable_token_rl

    env_llm_max_input_per_call = _parse_int(
        os.environ.get("LLM_MAX_INPUT_TOKENS_PER_CALL"),
        env_name="LLM_MAX_INPUT_TOKENS_PER_CALL",
    )
    if env_llm_max_input_per_call is not None:
        cfg.llm_max_input_tokens_per_call = env_llm_max_input_per_call

    env_llm_max_input_per_min = _parse_int(
        os.environ.get("LLM_MAX_INPUT_TOKENS_PER_MINUTE"),
        env_name="LLM_MAX_INPUT_TOKENS_PER_MINUTE",
    )
    if env_llm_max_input_per_min is not None:
        cfg.llm_max_input_tokens_per_minute = env_llm_max_input_per_min

    env_service_tier = os.environ.get("LLM_SERVICE_TIER")
    if env_service_tier:
        env_service_tier = env_service_tier.strip().lower()
        if env_service_tier in {"default", "flex", "priority", "auto"}:
            cfg.llm_service_tier = env_service_tier
        else:
            logger.warning(
                "Ignoring invalid LLM_SERVICE_TIER=%r (expected one of "
                "default|flex|priority|auto)",
                env_service_tier,
            )


def _apply_remote_whisper_runtime_overrides(whisper: RemoteWhisperConfig) -> None:
    """Apply env var overrides to remote whisper runtime config.

    Falls back to OPENAI_API_KEY and OPENAI_BASE_URL when whisper-specific
    env vars are not set.
    """
    remote_key = os.environ.get("WHISPER_REMOTE_API_KEY") or os.environ.get(
        "OPENAI_API_KEY"
    )
    if remote_key:
        whisper.api_key = remote_key
    remote_base = os.environ.get("WHISPER_REMOTE_BASE_URL") or os.environ.get(
        "OPENAI_BASE_URL"
    )
    if remote_base:
        whisper.base_url = remote_base
    remote_model = os.environ.get("WHISPER_REMOTE_MODEL")
    if remote_model:
        whisper.model = remote_model
    remote_timeout = _parse_int(
        os.environ.get("WHISPER_REMOTE_TIMEOUT_SEC"),
        env_name="WHISPER_REMOTE_TIMEOUT_SEC",
    )
    if remote_timeout is not None:
        whisper.timeout_sec = remote_timeout
    remote_chunksize = _parse_int(
        os.environ.get("WHISPER_REMOTE_CHUNKSIZE_MB"),
        env_name="WHISPER_REMOTE_CHUNKSIZE_MB",
    )
    if remote_chunksize is not None:
        whisper.chunksize_mb = remote_chunksize
    remote_diarize = _parse_bool(
        os.environ.get("WHISPER_REMOTE_DIARIZE"),
        env_name="WHISPER_REMOTE_DIARIZE",
    )
    if remote_diarize is not None:
        whisper.diarize = remote_diarize
    remote_speaker_embeddings = _parse_bool(
        os.environ.get("WHISPER_REMOTE_SPEAKER_EMBEDDINGS"),
        env_name="WHISPER_REMOTE_SPEAKER_EMBEDDINGS",
    )
    if remote_speaker_embeddings is not None:
        whisper.speaker_embeddings = remote_speaker_embeddings

    validated_whisper = RemoteWhisperConfig(**whisper.model_dump())
    for field_name in validated_whisper.__class__.model_fields.keys():
        setattr(whisper, field_name, getattr(validated_whisper, field_name))


def _apply_groq_whisper_runtime_overrides(whisper: GroqWhisperConfig) -> None:
    """Apply env var overrides to groq whisper runtime config.

    Accepts WHISPER_GROQ_MODEL as an alias for GROQ_WHISPER_MODEL.
    """
    groq_key = os.environ.get("GROQ_API_KEY")
    if groq_key:
        whisper.api_key = groq_key
    groq_model = os.environ.get("GROQ_WHISPER_MODEL") or os.environ.get(
        "WHISPER_GROQ_MODEL"
    )
    if groq_model:
        whisper.model = groq_model
    groq_max_retries = _parse_int(
        os.environ.get("GROQ_MAX_RETRIES"), env_name="GROQ_MAX_RETRIES"
    )
    if groq_max_retries is not None:
        whisper.max_retries = groq_max_retries


def _apply_whisper_env_overrides(cfg: PydanticConfig) -> None:
    if cfg.whisper is None:
        return
    wtype = getattr(cfg.whisper, "whisper_type", None)
    if wtype == "remote" and isinstance(cfg.whisper, RemoteWhisperConfig):
        _apply_remote_whisper_runtime_overrides(cfg.whisper)
    elif wtype == "groq" and isinstance(cfg.whisper, GroqWhisperConfig):
        _apply_groq_whisper_runtime_overrides(cfg.whisper)


def _apply_llm_model_override(cfg: PydanticConfig) -> None:
    env_llm_model = os.environ.get("LLM_MODEL")
    if env_llm_model:
        cfg.llm_model = env_llm_model


def _configure_remote_whisper(cfg: PydanticConfig) -> None:
    """Configure remote whisper type."""
    existing_model_any = getattr(cfg.whisper, "model", "whisper-1")
    existing_model = (
        existing_model_any if isinstance(existing_model_any, str) else "whisper-1"
    )
    rem_model_env = os.environ.get("WHISPER_REMOTE_MODEL")
    rem_model: str = (
        rem_model_env
        if isinstance(rem_model_env, str) and rem_model_env
        else existing_model
    )

    existing_key_any = getattr(cfg.whisper, "api_key", "")
    existing_key = existing_key_any if isinstance(existing_key_any, str) else ""
    rem_api_key_env = os.environ.get("WHISPER_REMOTE_API_KEY") or os.environ.get(
        "OPENAI_API_KEY"
    )
    rem_api_key: str = (
        rem_api_key_env
        if isinstance(rem_api_key_env, str) and rem_api_key_env
        else existing_key
    )

    existing_base_any = getattr(cfg.whisper, "base_url", "https://api.openai.com/v1")
    existing_base = (
        existing_base_any
        if isinstance(existing_base_any, str)
        else "https://api.openai.com/v1"
    )
    rem_base_env = os.environ.get("WHISPER_REMOTE_BASE_URL") or os.environ.get(
        "OPENAI_BASE_URL"
    )
    rem_base_url: str = (
        rem_base_env
        if isinstance(rem_base_env, str) and rem_base_env
        else existing_base
    )

    existing_lang_any = getattr(cfg.whisper, "language", "en")
    lang: str = existing_lang_any if isinstance(existing_lang_any, str) else "en"

    parsed_timeout = _parse_int(
        os.environ.get("WHISPER_REMOTE_TIMEOUT_SEC"),
        env_name="WHISPER_REMOTE_TIMEOUT_SEC",
    )
    timeout_sec: int = (
        parsed_timeout
        if parsed_timeout is not None
        else int(getattr(cfg.whisper, "timeout_sec", 600))
    )

    parsed_chunksize = _parse_int(
        os.environ.get("WHISPER_REMOTE_CHUNKSIZE_MB"),
        env_name="WHISPER_REMOTE_CHUNKSIZE_MB",
    )
    chunksize_mb: int = (
        parsed_chunksize
        if parsed_chunksize is not None
        else int(getattr(cfg.whisper, "chunksize_mb", 24))
    )

    existing_diarize_any = getattr(
        cfg.whisper, "diarize", DEFAULTS.WHISPER_REMOTE_DIARIZE
    )
    diarize: bool = (
        existing_diarize_any
        if isinstance(existing_diarize_any, bool)
        else DEFAULTS.WHISPER_REMOTE_DIARIZE
    )
    parsed_diarize = _parse_bool(
        os.environ.get("WHISPER_REMOTE_DIARIZE"),
        env_name="WHISPER_REMOTE_DIARIZE",
    )
    if parsed_diarize is not None:
        diarize = parsed_diarize

    existing_speaker_embeddings_any = getattr(
        cfg.whisper,
        "speaker_embeddings",
        DEFAULTS.WHISPER_REMOTE_SPEAKER_EMBEDDINGS,
    )
    speaker_embeddings: bool = (
        existing_speaker_embeddings_any
        if isinstance(existing_speaker_embeddings_any, bool)
        else DEFAULTS.WHISPER_REMOTE_SPEAKER_EMBEDDINGS
    )
    parsed_speaker_embeddings = _parse_bool(
        os.environ.get("WHISPER_REMOTE_SPEAKER_EMBEDDINGS"),
        env_name="WHISPER_REMOTE_SPEAKER_EMBEDDINGS",
    )
    if parsed_speaker_embeddings is not None:
        speaker_embeddings = parsed_speaker_embeddings

    cfg.whisper = RemoteWhisperConfig(
        model=rem_model,
        api_key=rem_api_key,
        base_url=rem_base_url,
        language=lang,
        timeout_sec=timeout_sec,
        chunksize_mb=chunksize_mb,
        diarize=diarize,
        speaker_embeddings=speaker_embeddings,
    )


def _configure_groq_whisper(cfg: PydanticConfig) -> None:
    """Configure groq whisper type."""
    existing_key_any = getattr(cfg.whisper, "api_key", "")
    existing_key = existing_key_any if isinstance(existing_key_any, str) else ""
    groq_key_env = os.environ.get("GROQ_API_KEY")
    groq_api_key: str = (
        groq_key_env if isinstance(groq_key_env, str) and groq_key_env else existing_key
    )

    existing_model_any = getattr(cfg.whisper, "model", DEFAULTS.WHISPER_GROQ_MODEL)
    existing_model = (
        existing_model_any
        if isinstance(existing_model_any, str)
        else DEFAULTS.WHISPER_GROQ_MODEL
    )
    groq_model_env = os.environ.get("GROQ_WHISPER_MODEL") or os.environ.get(
        "WHISPER_GROQ_MODEL"
    )
    groq_model_val: str = (
        groq_model_env
        if isinstance(groq_model_env, str) and groq_model_env
        else existing_model
    )

    existing_lang_any = getattr(cfg.whisper, "language", "en")
    groq_lang: str = existing_lang_any if isinstance(existing_lang_any, str) else "en"

    parsed_max_retries = _parse_int(
        os.environ.get("GROQ_MAX_RETRIES"), env_name="GROQ_MAX_RETRIES"
    )
    max_retries: int = (
        parsed_max_retries
        if parsed_max_retries is not None
        else int(getattr(cfg.whisper, "max_retries", 3))
    )

    cfg.whisper = GroqWhisperConfig(
        api_key=groq_api_key,
        model=groq_model_val,
        language=groq_lang,
        max_retries=max_retries,
    )


def _apply_whisper_type_override(cfg: PydanticConfig) -> None:
    env_whisper_type = os.environ.get("WHISPER_TYPE")

    # Auto-detect whisper type from API key environment variables if not explicitly set
    if not env_whisper_type:
        if os.environ.get("WHISPER_REMOTE_API_KEY"):
            env_whisper_type = "remote"
            logger.info(
                "Auto-detected WHISPER_TYPE=remote from WHISPER_REMOTE_API_KEY environment variable"
            )
        elif os.environ.get("GROQ_API_KEY") and not os.environ.get("LLM_API_KEY"):
            # Only auto-detect groq for whisper if LLM_API_KEY is not set
            # (to avoid confusion when GROQ_API_KEY is only meant for LLM)
            env_whisper_type = "groq"
            logger.info(
                "Auto-detected WHISPER_TYPE=groq from GROQ_API_KEY environment variable"
            )

    if not env_whisper_type:
        return

    wtype = env_whisper_type.strip().lower()
    if wtype == "local":
        raise RuntimeError(
            "WHISPER_TYPE=local is no longer supported. Run a dedicated "
            "OpenAI-compatible transcription service and configure "
            "WHISPER_TYPE=remote with WHISPER_REMOTE_BASE_URL."
        )
    elif wtype == "remote":
        _configure_remote_whisper(cfg)
    elif wtype == "groq":
        _configure_groq_whisper(cfg)
    elif wtype == "test":
        cfg.whisper = TestWhisperConfig()


def _commit_runtime_config(cfg: PydanticConfig) -> None:
    logger.info(
        "Config hydration: after env overrides | whisper_type=%s llm_model=%s openai_base_url=%s llm_api_key_set=%s whisper_api_key_set=%s",
        getattr(getattr(cfg, "whisper", None), "whisper_type", None),
        getattr(cfg, "llm_model", None),
        getattr(cfg, "openai_base_url", None),
        bool(getattr(cfg, "llm_api_key", None)),
        bool(getattr(getattr(cfg, "whisper", None), "api_key", None)),
    )
    # Copy values from cfg to runtime_config, preserving Pydantic model
    # instances. This intentionally copies every field, including the
    # `@deprecated` legacy whisper fields, so suppress the deprecation
    # warnings those reads would otherwise raise.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        for key in type(cfg).model_fields.keys():
            setattr(runtime_config, key, getattr(cfg, key))


def _log_final_snapshot() -> None:
    logger.info(
        "Config hydration: runtime set | whisper_type=%s llm_model=%s openai_base_url=%s",
        getattr(getattr(runtime_config, "whisper", None), "whisper_type", None),
        getattr(runtime_config, "llm_model", None),
        getattr(runtime_config, "openai_base_url", None),
    )


def ensure_defaults_and_hydrate() -> None:
    """Ensure default rows exist, then hydrate the runtime config from DB.

    Environment variables are applied as runtime overlays only - they are never
    persisted to the database. This follows the 12-factor app principle where
    env vars take precedence at runtime without modifying stored configuration.
    """
    ensure_defaults()
    hydrate_runtime_config_inplace()
