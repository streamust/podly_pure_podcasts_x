"""Configuration visibility through Rust writer, web, and worker startup."""

from __future__ import annotations

import multiprocessing
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from flask import Flask

from app.extensions import db
from app.writer import client as writer_client_module
from app.writer.client import writer_client
from tests.test_writer_differential_parity import AUTH_KEY, RustWriterParityServer
from tests.writer_parity_fixtures import WriterParityPair

_ENV_CONFIG_KEYS = (
    "LLM_API_KEY",
    "OPENAI_API_KEY",
    "GROQ_API_KEY",
    "LLM_MODEL",
    "OPENAI_BASE_URL",
    "OPENAI_TIMEOUT",
    "OPENAI_MAX_TOKENS",
    "LLM_MAX_CONCURRENT_CALLS",
    "LLM_MAX_RETRY_ATTEMPTS",
    "LLM_ENABLE_TOKEN_RATE_LIMITING",
    "LLM_MAX_INPUT_TOKENS_PER_CALL",
    "LLM_MAX_INPUT_TOKENS_PER_MINUTE",
    "LLM_SERVICE_TIER",
    "WHISPER_TYPE",
    "WHISPER_REMOTE_API_KEY",
    "WHISPER_REMOTE_BASE_URL",
    "WHISPER_REMOTE_MODEL",
    "WHISPER_REMOTE_TIMEOUT_SEC",
    "WHISPER_REMOTE_CHUNKSIZE_MB",
    "WHISPER_REMOTE_DIARIZE",
    "WHISPER_REMOTE_SPEAKER_EMBEDDINGS",
    "WHISPER_GROQ_MODEL",
    "GROQ_WHISPER_MODEL",
    "GROQ_MAX_RETRIES",
)


def _worker_config_probe(instance_dir: str, result_queue: Any) -> None:
    """Start the actual processing app in a fresh Python process and report config."""
    from flask import Flask

    import app as app_module

    def _create_worker_flask_app() -> Flask:
        return Flask(
            "writer-config-visibility-worker",
            instance_path=instance_dir,
        )

    app_module._create_flask_app = _create_worker_flask_app
    worker_app = app_module.create_processing_app()
    with worker_app.app_context():
        try:
            from app.runtime_config import config

            result_queue.put(
                {
                    "pid": os.getpid(),
                    "llm_model": config.llm_model,
                    "processing_segments": config.processing.num_segments_to_input_to_prompt,
                    "fade_ms": config.output.fade_ms,
                    "autoprocess_on_download": config.autoprocess_on_download,
                    "notifications_enabled": config.notifications.enabled,
                    "whisper_type": (
                        config.whisper.whisper_type
                        if config.whisper is not None
                        else None
                    ),
                }
            )
        finally:
            from app.extensions import db as worker_db

            worker_db.session.remove()
            worker_db.engine.dispose()


def _make_config_web_app(instance_dir: Path) -> Flask:
    app = Flask(
        "writer-config-visibility-web",
        instance_path=str(instance_dir),
    )
    app.config.update(
        SQLALCHEMY_DATABASE_URI="sqlite:///sqlite3.db",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        TESTING=True,
    )
    db.init_app(app)

    from app.routes.config_routes import config_bp

    app.register_blueprint(config_bp)
    return app


def _seed_missing_notification_settings(db_path: Path) -> None:
    """Match the other config parity fixture's required singleton seed."""
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO notification_settings "
            "(id,enabled,notify_on_failure,notify_on_success,"
            "notify_on_rust_fallback,include_llm_explanation,created_at,updated_at) "
            "VALUES (1,0,1,0,0,0,?,?)",
            ("2026-01-01 00:00:00.000000", "2026-01-01 00:00:00.000000"),
        )


def test_rust_config_update_is_visible_to_web_and_fresh_processing_worker(
    writer_parity_pair: WriterParityPair,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A committed Rust settings update reaches both Python process roles."""
    for key in _ENV_CONFIG_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PODLY_WRITER_BACKEND", "rust")
    monkeypatch.setenv("PODLY_IPC_AUTHKEY", AUTH_KEY)
    monkeypatch.setattr(writer_client_module, "RUST_WRITER_HOST", "127.0.0.1")

    backend = writer_parity_pair.rust
    _seed_missing_notification_settings(backend.db_path)
    server = RustWriterParityServer(backend.db_path, tmp_path)
    monkeypatch.setattr(writer_client_module, "RUST_WRITER_PORT", server.port)
    monkeypatch.setattr(
        writer_client,
        "_local_execute",
        lambda _command: pytest.fail("Python local writer fallback was invoked"),
    )
    monkeypatch.setattr(
        writer_client,
        "_local_execute_action",
        lambda _command: pytest.fail("Python action executor was invoked"),
    )

    app = _make_config_web_app(backend.instance_dir)
    from app.processor import ProcessorSingleton
    from app.routes import config_routes
    from app.runtime_config import config as web_config

    # Keep the real settings endpoint and adapter path while using only synthetic
    # authorization; no session or account state participates in this contract.
    monkeypatch.setattr(config_routes, "require_admin", lambda: (None, None))
    stale_processor = object()
    monkeypatch.setattr(ProcessorSingleton, "_instance", stale_processor)
    payload = {
        "llm": {"llm_model": "rust-config-visibility-model"},
        "whisper": {"whisper_type": "test"},
        "processing": {"num_segments_to_input_to_prompt": 47},
        "output": {"fade_ms": 987, "min_confidence": 0.73},
        "app": {"autoprocess_on_download": True},
        "notifications": {"enabled": True, "apprise_urls": []},
    }

    try:
        with app.test_client() as client:
            put_response = client.put("/api/config", json=payload)
            assert put_response.status_code == 200, put_response.get_data(as_text=True)
            saved = put_response.get_json()
            assert saved["llm"]["llm_model"] == "rust-config-visibility-model"
            assert saved["processing"]["num_segments_to_input_to_prompt"] == 47
            assert saved["output"]["fade_ms"] == 987
            assert saved["notifications"]["enabled"] is True
            assert web_config.llm_model == "rust-config-visibility-model"
            assert web_config.processing.num_segments_to_input_to_prompt == 47
            assert web_config.output.fade_ms == 987
            assert web_config.autoprocess_on_download is True
            assert web_config.notifications.enabled is True
            assert ProcessorSingleton._instance is None

            get_response = client.get("/api/config")
            assert get_response.status_code == 200, get_response.get_data(as_text=True)
            current = get_response.get_json()["config"]
            assert current["llm"]["llm_model"] == "rust-config-visibility-model"
            assert current["whisper"]["whisper_type"] == "test"
            assert current["processing"]["num_segments_to_input_to_prompt"] == 47
            assert current["output"]["fade_ms"] == 987
            assert current["app"]["autoprocess_on_download"] is True
            assert current["notifications"]["enabled"] is True

        # The worker is a fresh Python process. Its real processing-app startup
        # hydrates from the shared fixture DB; it cannot inherit the web process's
        # in-memory config object.
        context = multiprocessing.get_context("spawn")
        result_queue = context.Queue()
        worker = context.Process(
            target=_worker_config_probe,
            args=(str(backend.instance_dir), result_queue),
        )
        worker.start()
        worker.join(timeout=45)
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=10)
            pytest.fail("processing worker configuration probe timed out")
        assert worker.exitcode == 0
        worker_config = result_queue.get(timeout=5)
        result_queue.close()
        result_queue.join_thread()

        assert worker_config["llm_model"] == "rust-config-visibility-model"
        assert worker_config["processing_segments"] == 47
        assert worker_config["fade_ms"] == 987
        assert worker_config["autoprocess_on_download"] is True
        assert worker_config["notifications_enabled"] is True
        assert worker_config["whisper_type"] == "test"
        assert worker_config["pid"] != os.getpid()

        ready = server._request("/v1/ready")
        assert ready["backend"] == "rust"
        assert ready["ready"] is True
    finally:
        with app.app_context():
            db.session.remove()
            db.engine.dispose()
        server.close()
