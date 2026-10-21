import json
import logging
import os
import subprocess
import sys
from typing import Any

import flask
from flask import Blueprint, jsonify, request

from app.auth.guards import require_admin
from app.config_store import (
    apply_app_scheduler_side_effects,
    hydrate_runtime_config_inplace,
    read_combined,
    to_pydantic_config,
)
from app.pricing_client import helper_env
from app.runtime_config import config as runtime_config
from app.writer.client import writer_client

logger = logging.getLogger("global_logger")


config_bp = Blueprint("config", __name__)


def _mask_secret(value: Any | None) -> str | None:
    if value is None:
        return None
    try:
        secret = str(value).strip()
    except Exception:  # pragma: no cover - defensive  # noqa: BLE001
        return None

    if not secret:
        return None
    if len(secret) <= 8:
        return secret
    return f"{secret[:4]}...{secret[-4:]}"


def _sanitize_config_for_client(cfg: dict[str, Any]) -> dict[str, Any]:
    try:
        data: dict[str, Any] = dict(cfg)
        llm: dict[str, Any] = dict(data.get("llm", {}))
        whisper: dict[str, Any] = dict(data.get("whisper", {}))

        llm_api_key = llm.pop("llm_api_key", None)
        if llm_api_key:
            llm["llm_api_key_preview"] = _mask_secret(llm_api_key)

        whisper_api_key = whisper.pop("api_key", None)
        if whisper_api_key:
            whisper["api_key_preview"] = _mask_secret(whisper_api_key)

        # Apprise URLs are an editable list the admin manages directly, so they
        # are returned as-is (this endpoint is already admin-only, like the
        # Discord client_id / redirect_uri fields). Masking them would make the
        # textarea impossible to edit without wiping existing entries.

        data["llm"] = llm
        data["whisper"] = whisper
        return data
    except Exception:  # noqa: BLE001
        return {}


@config_bp.route("/api/config", methods=["GET"])
def api_get_config() -> flask.Response:
    _, error_response = require_admin()
    if error_response:
        return error_response

    try:
        data = read_combined()

        _hydrate_runtime_config(data)

        env_metadata = _build_env_override_metadata(data)

        return flask.jsonify(
            {
                "config": _sanitize_config_for_client(data),
                "env_overrides": env_metadata,
            }
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"Failed to read configuration: {e}")
        return flask.make_response(
            jsonify({"error": "Failed to read configuration"}), 500
        )


def _hydrate_runtime_config(data: dict[str, Any]) -> None:
    _hydrate_llm_config(data)
    _hydrate_whisper_config(data)
    _hydrate_app_config(data)
    _hydrate_notifications_config(data)


def _hydrate_notifications_config(data: dict[str, Any]) -> None:
    data.setdefault("notifications", {})
    notif = data["notifications"]
    rt_notif = getattr(runtime_config, "notifications", None)
    if rt_notif is None:
        return
    notif["enabled"] = _get_attr_or_value(rt_notif, "enabled", notif.get("enabled"))
    notif["apprise_urls"] = list(
        _get_attr_or_value(rt_notif, "apprise_urls", notif.get("apprise_urls") or [])
        or []
    )
    notif["notify_on_failure"] = _get_attr_or_value(
        rt_notif, "notify_on_failure", notif.get("notify_on_failure")
    )
    notif["notify_on_success"] = _get_attr_or_value(
        rt_notif, "notify_on_success", notif.get("notify_on_success")
    )
    notif["notify_on_rust_fallback"] = _get_attr_or_value(
        rt_notif, "notify_on_rust_fallback", notif.get("notify_on_rust_fallback")
    )
    notif["include_llm_explanation"] = _get_attr_or_value(
        rt_notif, "include_llm_explanation", notif.get("include_llm_explanation")
    )


def _hydrate_llm_config(data: dict[str, Any]) -> None:
    data.setdefault("llm", {})
    llm = data["llm"]
    llm["llm_api_key"] = getattr(runtime_config, "llm_api_key", llm.get("llm_api_key"))
    llm["llm_model"] = getattr(runtime_config, "llm_model", llm.get("llm_model"))
    llm["openai_base_url"] = getattr(
        runtime_config, "openai_base_url", llm.get("openai_base_url")
    )
    llm["openai_timeout"] = getattr(
        runtime_config, "openai_timeout", llm.get("openai_timeout")
    )
    llm["openai_max_tokens"] = getattr(
        runtime_config, "openai_max_tokens", llm.get("openai_max_tokens")
    )
    llm["llm_max_concurrent_calls"] = getattr(
        runtime_config, "llm_max_concurrent_calls", llm.get("llm_max_concurrent_calls")
    )
    llm["llm_max_retry_attempts"] = getattr(
        runtime_config, "llm_max_retry_attempts", llm.get("llm_max_retry_attempts")
    )
    llm["llm_max_input_tokens_per_call"] = getattr(
        runtime_config,
        "llm_max_input_tokens_per_call",
        llm.get("llm_max_input_tokens_per_call"),
    )
    llm["llm_enable_token_rate_limiting"] = getattr(
        runtime_config,
        "llm_enable_token_rate_limiting",
        llm.get("llm_enable_token_rate_limiting"),
    )
    llm["llm_max_input_tokens_per_minute"] = getattr(
        runtime_config,
        "llm_max_input_tokens_per_minute",
        llm.get("llm_max_input_tokens_per_minute"),
    )
    llm["enable_boundary_refinement"] = getattr(
        runtime_config,
        "enable_boundary_refinement",
        llm.get("enable_boundary_refinement"),
    )
    llm["enable_word_level_boundary_refinder"] = getattr(
        runtime_config,
        "enable_word_level_boundary_refinder",
        llm.get("enable_word_level_boundary_refinder"),
    )
    llm["enable_llm_chapter_fallback_tagging"] = getattr(
        runtime_config,
        "enable_llm_chapter_fallback_tagging",
        llm.get("enable_llm_chapter_fallback_tagging"),
    )
    llm["chapter_full_block_text"] = getattr(
        runtime_config,
        "chapter_full_block_text",
        llm.get("chapter_full_block_text"),
    )
    llm["llm_service_tier"] = getattr(
        runtime_config,
        "llm_service_tier",
        llm.get("llm_service_tier"),
    )


def _hydrate_whisper_config(data: dict[str, Any]) -> None:
    data.setdefault("whisper", {})
    whisper = data["whisper"]
    rt_whisper = getattr(runtime_config, "whisper", None)

    if isinstance(rt_whisper, dict):
        _overlay_whisper_dict(whisper, rt_whisper)
        return

    if rt_whisper is not None and hasattr(rt_whisper, "whisper_type"):
        _overlay_whisper_object(whisper, rt_whisper)


def _overlay_whisper_dict(target: dict[str, Any], source: dict[str, Any]) -> None:
    wtype = source.get("whisper_type")
    target["whisper_type"] = wtype or target.get("whisper_type")
    if wtype == "remote":
        _overlay_remote_whisper_fields(target, source)
    elif wtype == "groq":
        _overlay_groq_whisper_fields(target, source)


def _overlay_whisper_object(target: dict[str, Any], source: Any) -> None:
    wtype = source.whisper_type
    target["whisper_type"] = wtype
    if wtype == "remote":
        _overlay_remote_whisper_fields(target, source)
    elif wtype == "groq":
        _overlay_groq_whisper_fields(target, source)


def _overlay_remote_whisper_fields(target: dict[str, Any], source: Any) -> None:
    target["model"] = _get_attr_or_value(source, "model", target.get("model"))
    target["api_key"] = _get_attr_or_value(source, "api_key", target.get("api_key"))
    target["base_url"] = _get_attr_or_value(source, "base_url", target.get("base_url"))
    target["language"] = _get_attr_or_value(source, "language", target.get("language"))
    target["timeout_sec"] = _get_attr_or_value(
        source, "timeout_sec", target.get("timeout_sec")
    )
    target["chunksize_mb"] = _get_attr_or_value(
        source, "chunksize_mb", target.get("chunksize_mb")
    )
    target["diarize"] = _get_attr_or_value(source, "diarize", target.get("diarize"))
    target["speaker_embeddings"] = _get_attr_or_value(
        source, "speaker_embeddings", target.get("speaker_embeddings")
    )


def _overlay_groq_whisper_fields(target: dict[str, Any], source: Any) -> None:
    target["api_key"] = _get_attr_or_value(source, "api_key", target.get("api_key"))
    target["model"] = _get_attr_or_value(source, "model", target.get("model"))
    target["language"] = _get_attr_or_value(source, "language", target.get("language"))
    target["max_retries"] = _get_attr_or_value(
        source, "max_retries", target.get("max_retries")
    )


def _get_attr_or_value(source: Any, key: str, default: Any) -> Any:
    if isinstance(source, dict):
        return source.get(key, default)
    return getattr(source, key, default)


def _hydrate_app_config(data: dict[str, Any]) -> None:
    data.setdefault("app", {})
    app_cfg = data["app"]
    app_cfg["post_cleanup_retention_days"] = getattr(
        runtime_config,
        "post_cleanup_retention_days",
        app_cfg.get("post_cleanup_retention_days"),
    )
    app_cfg["enable_public_landing_page"] = getattr(
        runtime_config,
        "enable_public_landing_page",
        app_cfg.get("enable_public_landing_page"),
    )
    app_cfg["user_limit_total"] = getattr(
        runtime_config, "user_limit_total", app_cfg.get("user_limit_total")
    )
    app_cfg["autoprocess_on_download"] = getattr(
        runtime_config,
        "autoprocess_on_download",
        app_cfg.get("autoprocess_on_download"),
    )


def _first_env(env_names: list[str]) -> tuple[str | None, str | None]:
    """Return first found environment variable name and value."""
    for name in env_names:
        value = os.environ.get(name)
        if value is not None and value != "":
            return name, value
    return None, None


def _register_override(
    overrides: dict[str, Any],
    path: str,
    env_var: str | None,
    value: Any | None,
    *,
    secret: bool = False,
) -> None:
    """Register an environment override in the metadata dict.

    Fields with env overrides are marked as read_only since env vars are
    authoritative and cannot be modified via the UI.
    """
    if not env_var or value is None:
        return
    entry: dict[str, Any] = {"env_var": env_var, "read_only": True}
    if secret:
        entry["is_secret"] = True
        entry["value_preview"] = _mask_secret(value)
    else:
        entry["value"] = value
    overrides[path] = entry


# Simple 1:1 env var → field path mappings for LLM settings.
# Shared across metadata registration, override detection, and field stripping.
_SIMPLE_LLM_ENV_MAP: dict[str, str] = {
    "OPENAI_BASE_URL": "llm.openai_base_url",
    "LLM_MODEL": "llm.llm_model",
    "OPENAI_TIMEOUT": "llm.openai_timeout",
    "OPENAI_MAX_TOKENS": "llm.openai_max_tokens",
    "LLM_MAX_CONCURRENT_CALLS": "llm.llm_max_concurrent_calls",
    "LLM_MAX_RETRY_ATTEMPTS": "llm.llm_max_retry_attempts",
    "LLM_ENABLE_TOKEN_RATE_LIMITING": "llm.llm_enable_token_rate_limiting",
    "LLM_MAX_INPUT_TOKENS_PER_CALL": "llm.llm_max_input_tokens_per_call",
    "LLM_MAX_INPUT_TOKENS_PER_MINUTE": "llm.llm_max_input_tokens_per_minute",
    "LLM_SERVICE_TIER": "llm.llm_service_tier",
}


def _register_llm_overrides(overrides: dict[str, Any]) -> None:
    """Register LLM-related environment overrides."""
    env_var, env_value = _first_env(["LLM_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY"])
    _register_override(overrides, "llm.llm_api_key", env_var, env_value, secret=True)

    for env_key, field_path in _SIMPLE_LLM_ENV_MAP.items():
        val = os.environ.get(env_key)
        if val:
            _register_override(overrides, field_path, env_key, val)


def _register_groq_shared_overrides(overrides: dict[str, Any]) -> None:
    """Register shared Groq API key override metadata."""
    groq_key = os.environ.get("GROQ_API_KEY")
    if groq_key:
        _register_override(
            overrides, "groq.api_key", "GROQ_API_KEY", groq_key, secret=True
        )


def _register_remote_whisper_overrides(overrides: dict[str, Any]) -> None:
    """Register remote whisper environment overrides."""
    remote_key = _first_env(["WHISPER_REMOTE_API_KEY", "OPENAI_API_KEY"])
    _register_override(
        overrides, "whisper.api_key", remote_key[0], remote_key[1], secret=True
    )

    remote_base = _first_env(["WHISPER_REMOTE_BASE_URL", "OPENAI_BASE_URL"])
    _register_override(overrides, "whisper.base_url", remote_base[0], remote_base[1])

    remote_model = os.environ.get("WHISPER_REMOTE_MODEL")
    if remote_model:
        _register_override(
            overrides, "whisper.model", "WHISPER_REMOTE_MODEL", remote_model
        )

    remote_timeout = os.environ.get("WHISPER_REMOTE_TIMEOUT_SEC")
    if remote_timeout:
        _register_override(
            overrides,
            "whisper.timeout_sec",
            "WHISPER_REMOTE_TIMEOUT_SEC",
            remote_timeout,
        )

    remote_chunksize = os.environ.get("WHISPER_REMOTE_CHUNKSIZE_MB")
    if remote_chunksize:
        _register_override(
            overrides,
            "whisper.chunksize_mb",
            "WHISPER_REMOTE_CHUNKSIZE_MB",
            remote_chunksize,
        )

    remote_diarize = os.environ.get("WHISPER_REMOTE_DIARIZE")
    if remote_diarize:
        _register_override(
            overrides,
            "whisper.diarize",
            "WHISPER_REMOTE_DIARIZE",
            remote_diarize,
        )

    remote_speaker_embeddings = os.environ.get("WHISPER_REMOTE_SPEAKER_EMBEDDINGS")
    if remote_speaker_embeddings:
        _register_override(
            overrides,
            "whisper.speaker_embeddings",
            "WHISPER_REMOTE_SPEAKER_EMBEDDINGS",
            remote_speaker_embeddings,
        )


def _register_groq_whisper_overrides(overrides: dict[str, Any]) -> None:
    """Register groq whisper environment overrides."""
    groq_key = os.environ.get("GROQ_API_KEY")
    if groq_key:
        _register_override(
            overrides, "whisper.api_key", "GROQ_API_KEY", groq_key, secret=True
        )

    groq_model_env, groq_model_val = _first_env(
        ["GROQ_WHISPER_MODEL", "WHISPER_GROQ_MODEL"]
    )
    _register_override(overrides, "whisper.model", groq_model_env, groq_model_val)

    groq_retries = os.environ.get("GROQ_MAX_RETRIES")
    if groq_retries:
        _register_override(
            overrides, "whisper.max_retries", "GROQ_MAX_RETRIES", groq_retries
        )


def _determine_whisper_type_for_metadata(data: dict[str, Any]) -> str | None:
    """Determine whisper type for environment metadata (with auto-detection)."""
    whisper_cfg = data.get("whisper", {}) or {}
    wtype = whisper_cfg.get("whisper_type")

    env_whisper_type = os.environ.get("WHISPER_TYPE")

    # Auto-detect whisper type from API key environment variables if not explicitly set
    # (matching the logic in config_store._apply_whisper_type_override)
    if not env_whisper_type:
        if os.environ.get("WHISPER_REMOTE_API_KEY"):
            env_whisper_type = "remote"
        elif os.environ.get("GROQ_API_KEY") and not os.environ.get("LLM_API_KEY"):
            env_whisper_type = "groq"

    if env_whisper_type:
        wtype = env_whisper_type.strip().lower()

    return wtype if isinstance(wtype, str) else None


def _build_env_override_metadata(data: dict[str, Any]) -> dict[str, Any]:
    overrides: dict[str, Any] = {}

    _register_llm_overrides(overrides)
    _register_groq_shared_overrides(overrides)

    env_whisper_type = os.environ.get("WHISPER_TYPE")
    if env_whisper_type:
        _register_override(
            overrides, "whisper.whisper_type", "WHISPER_TYPE", env_whisper_type
        )

    wtype = _determine_whisper_type_for_metadata(data)

    if wtype == "remote":
        _register_remote_whisper_overrides(overrides)
    elif wtype == "groq":
        _register_groq_whisper_overrides(overrides)

    return overrides


def _get_llm_overridden_fields() -> set[str]:
    """Return set of LLM field paths overridden by environment variables.

    Only considers env vars with non-empty values.
    """
    overridden: set[str] = set()

    if (
        os.environ.get("LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("GROQ_API_KEY")
    ):
        overridden.add("llm.llm_api_key")

    for env_key, field_path in _SIMPLE_LLM_ENV_MAP.items():
        if os.environ.get(env_key):
            overridden.add(field_path)

    return overridden


def _get_whisper_overridden_fields() -> set[str]:
    """Return set of whisper field paths overridden by environment variables."""
    overridden: set[str] = set()

    if os.environ.get("WHISPER_TYPE"):
        overridden.add("whisper.whisper_type")

    # Remote whisper
    if os.environ.get("WHISPER_REMOTE_API_KEY") or os.environ.get("OPENAI_API_KEY"):
        overridden.add("whisper.api_key")
    if os.environ.get("WHISPER_REMOTE_BASE_URL") or os.environ.get("OPENAI_BASE_URL"):
        overridden.add("whisper.base_url")
    if os.environ.get("WHISPER_REMOTE_MODEL"):
        overridden.add("whisper.model")
    if os.environ.get("WHISPER_REMOTE_TIMEOUT_SEC"):
        overridden.add("whisper.timeout_sec")
    if os.environ.get("WHISPER_REMOTE_CHUNKSIZE_MB"):
        overridden.add("whisper.chunksize_mb")
    if os.environ.get("WHISPER_REMOTE_DIARIZE"):
        overridden.add("whisper.diarize")
    if os.environ.get("WHISPER_REMOTE_SPEAKER_EMBEDDINGS"):
        overridden.add("whisper.speaker_embeddings")

    # Groq whisper
    if os.environ.get("GROQ_API_KEY"):
        overridden.add("whisper.api_key")
    if os.environ.get("GROQ_WHISPER_MODEL") or os.environ.get("WHISPER_GROQ_MODEL"):
        overridden.add("whisper.model")
    if os.environ.get("GROQ_MAX_RETRIES"):
        overridden.add("whisper.max_retries")

    return overridden


def _get_env_overridden_fields() -> set[str]:
    """Return set of field paths that are overridden by environment variables.

    These fields should not be modified via the API - env vars are authoritative.
    """
    return _get_llm_overridden_fields() | _get_whisper_overridden_fields()


def _strip_env_overridden_fields(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Remove env-overridden fields from payload, return cleaned payload and list of stripped fields."""
    overridden = _get_env_overridden_fields()
    stripped: list[str] = []

    cleaned = dict(payload)

    llm = cleaned.get("llm")
    if isinstance(llm, dict):
        llm = dict(llm)
        # llm_api_key has multi-env fallback, so it's not in _SIMPLE_LLM_ENV_MAP
        llm_field_paths = ["llm.llm_api_key", *_SIMPLE_LLM_ENV_MAP.values()]
        for field_path in llm_field_paths:
            field_key = field_path.split(".", 1)[1]
            if field_path in overridden and field_key in llm:
                llm.pop(field_key)
                stripped.append(field_path)
        cleaned["llm"] = llm

    whisper = cleaned.get("whisper")
    if isinstance(whisper, dict):
        whisper = dict(whisper)
        whisper_field_paths = [
            "whisper.whisper_type",
            "whisper.api_key",
            "whisper.base_url",
            "whisper.model",
            "whisper.timeout_sec",
            "whisper.chunksize_mb",
            "whisper.diarize",
            "whisper.speaker_embeddings",
            "whisper.max_retries",
        ]
        for field_path in whisper_field_paths:
            field_key = field_path.split(".", 1)[1]
            if field_path in overridden and field_key in whisper:
                whisper.pop(field_key)
                stripped.append(field_path)
        cleaned["whisper"] = whisper

    return cleaned, stripped


@config_bp.route("/api/config", methods=["PUT"])
def api_put_config() -> flask.Response:
    _, error_response = require_admin()
    if error_response:
        return error_response

    payload = request.get_json(silent=True) or {}

    llm_payload = payload.get("llm")
    if isinstance(llm_payload, dict):
        llm_payload.pop("llm_api_key_preview", None)

    whisper_payload = payload.get("whisper")
    if isinstance(whisper_payload, dict):
        whisper_payload.pop("api_key_preview", None)

    # Strip env-overridden fields - env vars are authoritative and cannot be overwritten via API
    payload, stripped_fields = _strip_env_overridden_fields(payload)
    if stripped_fields:
        logger.info(
            "Stripped env-overridden fields from config update: %s",
            ", ".join(stripped_fields),
        )

    try:
        previous_app_settings = dict(read_combined().get("app", {}))
        try:
            result = writer_client.action(
                "update_combined_config",
                {"payload": payload},
                wait=True,
            )
        finally:
            # The writer contract deliberately commits config sections one at a
            # time. Reconcile this process even when a later section fails.
            try:
                current_app_settings = dict(read_combined().get("app", {}))
                apply_app_scheduler_side_effects(
                    previous_app_settings,
                    current_app_settings,
                )
            except Exception as scheduler_err:  # noqa: BLE001
                logger.warning(
                    "Post-update scheduler reconciliation failed: %s",
                    scheduler_err,
                )
        if not result or not result.success:
            raise RuntimeError(getattr(result, "error", "Writer update failed"))
        data = result.data or {}

        try:
            db_cfg = to_pydantic_config()
        except Exception as hydrate_err:  # noqa: BLE001
            logger.error(f"Post-update config hydration failed: {hydrate_err}")
            return flask.make_response(
                jsonify(
                    {"error": "Invalid configuration", "details": str(hydrate_err)}
                ),
                400,
            )

        # The database is only the base configuration. Reapply environment
        # overlays before resetting the processor so a token-only save cannot
        # replace an env-selected model with the stale database value.
        hydrate_runtime_config_inplace(db_cfg)
        _reset_processor_if_loaded()

        # The writer returns database values. Return effective runtime values
        # so the UI does not appear to revert environment-managed fields.
        _hydrate_runtime_config(data)
        return flask.jsonify(_sanitize_config_for_client(data))
    except Exception as e:  # noqa: BLE001
        logger.error(f"Failed to update configuration: {e}")
        return flask.make_response(
            jsonify({"error": "Failed to update configuration", "details": str(e)}), 400
        )


@config_bp.route("/api/config/test-notification", methods=["POST"])
def api_test_notification() -> flask.Response:
    _, error_response = require_admin()
    if error_response:
        return error_response

    from app.notifications import notification_service

    payload: dict[str, Any] = request.get_json(silent=True) or {}
    notif: dict[str, Any] = dict(payload.get("notifications", {}))

    raw_urls = notif.get("apprise_urls")
    urls: list[str] | None = None
    if isinstance(raw_urls, list):
        cleaned = [u.strip() for u in raw_urls if isinstance(u, str) and u.strip()]
        urls = cleaned or None

    ok, error = notification_service.send_test(urls)
    if ok:
        return flask.jsonify({"ok": True, "message": "Test notification sent"})
    return flask.make_response(
        jsonify({"ok": False, "error": error or "Failed to send notification"}), 400
    )


@config_bp.route("/api/config/test-llm", methods=["POST"])
def api_test_llm() -> flask.Response:
    _, error_response = require_admin()
    if error_response:
        return error_response

    payload: dict[str, Any] = request.get_json(silent=True) or {}
    llm: dict[str, Any] = dict(payload.get("llm", {}))

    api_key: str | None = llm.get("llm_api_key") or getattr(
        runtime_config, "llm_api_key", None
    )
    model_val = llm.get("llm_model")
    model: str = (
        model_val
        if isinstance(model_val, str)
        else getattr(runtime_config, "llm_model", "gpt-4o")
    )
    base_url: str | None = llm.get("openai_base_url") or getattr(
        runtime_config, "openai_base_url", None
    )
    timeout_val = llm.get("openai_timeout")
    timeout: int = (
        int(timeout_val)
        if timeout_val is not None
        else int(getattr(runtime_config, "openai_timeout", 30))
    )

    if not api_key:
        return flask.make_response(
            jsonify({"ok": False, "error": "Missing llm_api_key"}), 400
        )

    # LiteLLM pulls in ~160 MiB of SDK modules; keep them out of the web process.
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "app.llm_probe_worker"],
            input=json.dumps(
                {
                    "api_key": api_key,
                    "model": model,
                    "base_url": base_url,
                    "timeout": timeout,
                }
            ),
            capture_output=True,
            text=True,
            timeout=timeout + 30,
            check=True,
            env=helper_env(),
        )
        probe = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        probe = {"ok": False, "error": f"LLM probe helper failed: {e}"}

    if probe.get("ok") is True:
        return flask.jsonify(
            {
                "ok": True,
                "message": "LLM connection OK",
                "model": model,
                "base_url": base_url,
            }
        )
    logger.error(f"LLM connection test failed: {probe.get('error')}")
    return flask.make_response(
        jsonify({"ok": False, "error": str(probe.get("error"))}), 400
    )


def _make_error_response(error_msg: str, status_code: int = 400) -> flask.Response:
    return flask.make_response(jsonify({"ok": False, "error": error_msg}), status_code)


def _make_success_response(message: str, **extra_data: Any) -> flask.Response:
    response_data = {"ok": True, "message": message}
    response_data.update(extra_data)
    return flask.jsonify(response_data)


def _get_whisper_config_value(
    whisper_cfg: dict[str, Any], key: str, default: Any | None = None
) -> Any | None:
    value = whisper_cfg.get(key)
    if value is not None:
        return value
    try:
        runtime_whisper = getattr(runtime_config, "whisper", None)
        if runtime_whisper is not None:
            return getattr(runtime_whisper, key, default)
    except Exception:  # pragma: no cover - defensive  # noqa: BLE001
        pass
    return default


def _get_env_whisper_api_key(whisper_type: str) -> str | None:
    if whisper_type == "remote":
        return os.environ.get("WHISPER_REMOTE_API_KEY") or os.environ.get(
            "OPENAI_API_KEY"
        )
    if whisper_type == "groq":
        return os.environ.get("GROQ_API_KEY")
    return None


def _determine_whisper_type(whisper_cfg: dict[str, Any]) -> str | None:
    wtype_any = whisper_cfg.get("whisper_type")
    if isinstance(wtype_any, str):
        return wtype_any
    try:
        runtime_whisper = getattr(runtime_config, "whisper", None)
        if runtime_whisper is not None and hasattr(runtime_whisper, "whisper_type"):
            rt_type = runtime_whisper.whisper_type
            return rt_type if isinstance(rt_type, str) else None
    except Exception:  # pragma: no cover - defensive  # noqa: BLE001
        pass
    return None


def _test_remote_whisper(whisper_cfg: dict[str, Any]) -> flask.Response:
    """Test remote whisper configuration."""
    api_key_any = _get_whisper_config_value(whisper_cfg, "api_key")
    base_url_any = _get_whisper_config_value(
        whisper_cfg, "base_url", "https://api.openai.com/v1"
    )
    timeout_any = _get_whisper_config_value(whisper_cfg, "timeout_sec", 30)

    api_key: str | None = api_key_any if isinstance(api_key_any, str) else None
    base_url: str | None = base_url_any if isinstance(base_url_any, str) else None
    timeout: int = int(timeout_any) if timeout_any is not None else 30

    if not api_key:
        api_key = _get_env_whisper_api_key("remote")

    if not api_key:
        return _make_error_response("Missing whisper.api_key")

    from openai import OpenAI

    _ = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout).models.list()
    return _make_success_response("Remote whisper connection OK", base_url=base_url)


def _test_groq_whisper(whisper_cfg: dict[str, Any]) -> flask.Response:
    """Test groq whisper configuration."""
    groq_api_key_any = _get_whisper_config_value(whisper_cfg, "api_key")
    groq_api_key: str | None = (
        groq_api_key_any if isinstance(groq_api_key_any, str) else None
    )

    if not groq_api_key:
        groq_api_key = _get_env_whisper_api_key("groq")

    if not groq_api_key:
        return _make_error_response("Missing whisper.api_key")

    from groq import Groq

    _ = Groq(api_key=groq_api_key).models.list()
    return _make_success_response("Groq whisper connection OK")


@config_bp.route("/api/config/test-whisper", methods=["POST"])
def api_test_whisper() -> flask.Response:
    """Test whisper configuration based on whisper_type."""
    _, error_response = require_admin()
    if error_response:
        return error_response

    payload: dict[str, Any] = request.get_json(silent=True) or {}
    whisper_cfg: dict[str, Any] = dict(payload.get("whisper", {}))

    wtype = _determine_whisper_type(whisper_cfg)
    if not wtype:
        return _make_error_response("Missing whisper_type")

    try:
        if wtype == "local":
            return _make_error_response(
                "WHISPER_TYPE=local is no longer supported. Configure "
                "WHISPER_TYPE=remote with WHISPER_REMOTE_BASE_URL for a dedicated "
                "OpenAI-compatible transcription service."
            )
        if wtype == "remote":
            return _test_remote_whisper(whisper_cfg)
        if wtype == "groq":
            return _test_groq_whisper(whisper_cfg)
        return _make_error_response(f"Unknown whisper_type '{wtype}'")
    except Exception as e:  # noqa: BLE001
        logger.error(f"Whisper connection test failed: {e}")
        return _make_error_response(str(e))


def _reset_processor_if_loaded() -> None:
    processor_module = sys.modules.get("app.processor")
    if processor_module is None:
        return
    processor_singleton = getattr(processor_module, "ProcessorSingleton", None)
    if processor_singleton is not None:
        processor_singleton.reset_instance()


@config_bp.route("/api/config/api_configured_check", methods=["GET"])
def api_configured_check() -> flask.Response:
    """Return whether the API configuration is sufficient to process.

    For our purposes, this means an LLM API key is present either in the
    persisted config or the runtime overlay.
    """
    _, error_response = require_admin()
    if error_response:
        return error_response

    try:
        data = read_combined()
        _hydrate_runtime_config(data)

        llm = data.get("llm", {}) if isinstance(data, dict) else {}
        api_key = llm.get("llm_api_key")
        configured = bool(api_key)
        return flask.jsonify({"configured": configured})
    except Exception as e:  # noqa: BLE001
        logger.error(f"Failed to check API configuration: {e}")
        # Be conservative: report not configured on error
        return flask.jsonify({"configured": False})
