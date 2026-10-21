"""One-shot bootstrap must not start a competing runtime writer or web app."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import flask_migrate
import pytest

import app as app_module
from app.auth.bootstrap import writer_client as bootstrap_writer_client
from app.auth.settings import AuthSettings


def _bootstrap_env(tmp_path: Path) -> dict[str, str]:
    """Build a subprocess environment without inheriting host secrets/config."""
    instance_dir = Path(tmp_path) / "instance"
    instance_dir.mkdir()
    env = os.environ.copy()
    for name in (
        "REQUIRE_AUTH",
        "PODLY_ADMIN_USERNAME",
        "PODLY_ADMIN_PASSWORD",
        "PODLY_SECRET_KEY",
        "LLM_API_KEY",
        "OPENAI_API_KEY",
        "GROQ_API_KEY",
        "LLM_MODEL",
        "WHISPER_TYPE",
        "WHISPER_REMOTE_API_KEY",
        "WHISPER_REMOTE_BASE_URL",
        "PODLY_DISABLE_SCHEDULER",
    ):
        env.pop(name, None)
    env.update(
        {
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
            "PODLY_INSTANCE_DIR": str(instance_dir),
            "PODLY_PODCAST_DATA_DIR": str(instance_dir / "data"),
            "REQUIRE_AUTH": "true",
            "PODLY_ADMIN_USERNAME": "bootstrap.admin",
            "PODLY_ADMIN_PASSWORD": "synthetic-bootstrap-password",
            "PODLY_SECRET_KEY": "synthetic-bootstrap-session-key",
        }
    )
    return env


def _run_bootstrap(
    tmp_path: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "bootstrap"],
        cwd=Path(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_bootstrap_factory_runs_strict_startup_without_http_or_scheduler(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    instance_dir = tmp_path / "instance"
    instance_dir.mkdir()
    monkeypatch.setenv("PODLY_INSTANCE_DIR", str(instance_dir))
    startup_calls: list[bool] = []

    def startup(_settings: object, *, fail_on_settings_error: bool = False) -> None:
        startup_calls.append(fail_on_settings_error)

    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail("one-shot bootstrap must not start HTTP or the scheduler")

    monkeypatch.setattr(app_module, "_run_app_startup", startup)
    monkeypatch.setattr(app_module, "_start_scheduler_and_jobs", unexpected)
    monkeypatch.setattr(app_module.scheduler, "start", unexpected)
    monkeypatch.setattr(app_module, "_register_routes_and_middleware", unexpected)
    monkeypatch.setattr(bootstrap_writer_client, "connect", unexpected)
    monkeypatch.setattr(
        app_module, "load_discord_settings", lambda: SimpleNamespace(enabled=False)
    )

    app = app_module.create_bootstrap_app()

    assert app.config["PODLY_APP_ROLE"] == "bootstrap"
    assert startup_calls == [True]
    assert "migrate" in app.extensions
    assert all(rule.endpoint == "static" for rule in app.url_map.iter_rules())
    assert app.instance_path == str(instance_dir)


def test_strict_bootstrap_propagates_settings_initialization_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(flask_migrate, "upgrade", lambda: None)
    monkeypatch.setattr(app_module, "bootstrap_admin_user", lambda _settings: None)

    def fail_defaults() -> None:
        raise RuntimeError("synthetic settings failure")

    monkeypatch.setattr(app_module, "ensure_defaults_and_hydrate", fail_defaults)

    with pytest.raises(RuntimeError, match="synthetic settings failure"):
        app_module._run_app_startup(
            cast(AuthSettings, object()), fail_on_settings_error=True
        )


def test_bootstrap_entrypoint_returns_when_initialization_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bootstrap

    calls: list[str] = []
    monkeypatch.setattr(bootstrap, "create_bootstrap_app", lambda: calls.append("done"))

    bootstrap.main()

    assert calls == ["done"]


def test_bootstrap_cli_is_sequentially_idempotent_and_preserves_settings(
    tmp_path: Path,
) -> None:
    import sqlite3

    env = _bootstrap_env(tmp_path)
    database = Path(env["PODLY_INSTANCE_DIR"]) / "sqlite3.db"

    first = _run_bootstrap(tmp_path, env)
    assert first.returncode == 0, first.stderr
    assert database.is_file()

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE llm_settings SET llm_model=? WHERE id=1", ("fixture-model",)
        )
        connection.execute(
            "UPDATE app_settings SET background_update_interval_minute=? WHERE id=1",
            (1337,),
        )

    second = _run_bootstrap(tmp_path, env)
    assert second.returncode == 0, second.stderr

    with sqlite3.connect(database) as connection:
        user_rows = connection.execute(
            "SELECT username,role FROM users ORDER BY id"
        ).fetchall()
        setting_counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "llm_settings",
                "whisper_settings",
                "processing_settings",
                "output_settings",
                "app_settings",
                "notification_settings",
            )
        }
        preserved = connection.execute(
            "SELECT llm_model FROM llm_settings WHERE id=1"
        ).fetchone()[0]
        interval = connection.execute(
            "SELECT background_update_interval_minute FROM app_settings WHERE id=1"
        ).fetchone()[0]

    assert user_rows == [("bootstrap.admin", "admin")]
    assert setting_counts == {table: 1 for table in setting_counts}
    assert preserved == "fixture-model"
    assert interval == 1337


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"PODLY_ADMIN_PASSWORD": ""}, "PODLY_ADMIN_PASSWORD"),
        ({"PODLY_ADMIN_USERNAME": "   "}, "PODLY_ADMIN_USERNAME"),
    ],
)
def test_bootstrap_cli_rejects_missing_or_invalid_auth_configuration(
    tmp_path: Path,
    overrides: dict[str, str | None],
    message: str,
) -> None:
    env = _bootstrap_env(tmp_path)
    for name, value in overrides.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value

    result = _run_bootstrap(tmp_path, env)

    assert result.returncode != 0
    assert message in result.stderr


def test_missing_auth_password_is_rejected_before_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.auth.settings import load_auth_settings

    monkeypatch.setenv("REQUIRE_AUTH", "true")
    monkeypatch.setenv("PODLY_ADMIN_USERNAME", "bootstrap.admin")
    monkeypatch.delenv("PODLY_ADMIN_PASSWORD", raising=False)

    with pytest.raises(RuntimeError, match="PODLY_ADMIN_PASSWORD must be provided"):
        load_auth_settings()


def test_bootstrap_cli_rejects_database_path_that_is_a_directory(
    tmp_path: Path,
) -> None:
    env = _bootstrap_env(tmp_path)
    database = Path(env["PODLY_INSTANCE_DIR"]) / "sqlite3.db"
    database.mkdir()

    result = _run_bootstrap(tmp_path, env)

    assert result.returncode != 0
    assert any(
        detail in result.stderr.lower()
        for detail in ("unable to open database file", "operationalerror")
    )


def test_bootstrap_cli_rejects_unknown_schema_revision(
    tmp_path: Path,
) -> None:
    import sqlite3

    env = _bootstrap_env(tmp_path)
    with sqlite3.connect(Path(env["PODLY_INSTANCE_DIR"]) / "sqlite3.db") as connection:
        connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32))")
        connection.execute(
            "INSERT INTO alembic_version(version_num) VALUES (?)",
            ("not-a-real-revision",),
        )

    result = _run_bootstrap(tmp_path, env)

    assert result.returncode != 0
    assert "not-a-real-revision" in result.stderr


@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="root bypasses directory mode bits, so permission denial is not observable",
)
def test_bootstrap_cli_reports_database_permission_failure(
    tmp_path: Path,
) -> None:
    env = _bootstrap_env(tmp_path)
    instance_dir = Path(env["PODLY_INSTANCE_DIR"])
    instance_dir.chmod(0o500)
    try:
        result = _run_bootstrap(tmp_path, env)
    finally:
        instance_dir.chmod(0o700)

    assert result.returncode != 0
    assert any(
        detail in result.stderr.lower()
        for detail in ("unable to open database file", "permission denied")
    )
