import logging
import os
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from flask import Flask, current_app, g, has_app_context, request, request_started


def _load_dotenv_files() -> None:
    """Load .env.local and .env from the repo root.

    Both the web server (waitress / flask CLI) and subprocesses (writer,
    processing_worker) import this package, so loading here keeps env parity
    across processes. Without this, env-based overrides (e.g.
    WHISPER_REMOTE_MODEL) can apply in one process but not another and the
    runtime config silently diverges from what was used to write the DB.
    Existing process env wins (override=False) so docker-compose env_file and
    real shell exports still take precedence. Skipped under pytest to avoid
    polluting tests that assert specific env states.
    """
    if "pytest" in sys.modules:
        return
    repo_root = Path(__file__).resolve().parents[2]
    for filename in (".env.local", ".env"):
        path = repo_root / filename
        if path.is_file():
            load_dotenv(path, override=False)


# Dotenv must run before any app-namespace imports so submodules see the
# environment when they read it at import time. The E402 suppressions below
# encode that ordering requirement intentionally.
_load_dotenv_files()
from flask_cors import CORS  # noqa: E402
from sqlalchemy import event  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402

from app import models as models  # noqa: E402
from app.auth import AuthSettings, load_auth_settings  # noqa: E402
from app.auth.bootstrap import bootstrap_admin_user  # noqa: E402
from app.auth.discord_settings import load_discord_settings  # noqa: E402
from app.auth.middleware import init_auth_middleware  # noqa: E402
from app.config_store import (  # noqa: E402
    ensure_defaults_and_hydrate,
    hydrate_runtime_config_inplace,
)
from app.extensions import db, migrate, scheduler  # noqa: E402
from app.litellm_silencer import install_litellm_log_filter  # noqa: E402
from app.logger import setup_logger  # noqa: E402
from app.runtime_config import config, is_test  # noqa: E402
from shared import defaults as DEFAULTS  # noqa: E402
from shared.processing_paths import get_in_root, get_srv_root  # noqa: E402

if is_test:
    # Don't attach the production app.log file handler under pytest -- tests
    # deliberately raise mocked errors (e.g. "boom" in test_processing_worker,
    # mocked litellm.UnsupportedParamsError in test_ad_classifier) that would
    # otherwise be written into src/instance/logs/app.log and look like real
    # runtime errors. Tests still get console output via caplog / pytest's own
    # log capture. test_logger_rotation.py exercises the file-handler path
    # directly against tmp_path so this gate doesn't affect its coverage.
    _global_logger = logging.getLogger("global_logger")
    _global_logger.setLevel(logging.DEBUG)
    _global_logger.propagate = False
else:
    setup_logger("global_logger", "src/instance/logs/app.log")
# Install the litellm log-noise filter before anything imports litellm so the
# import-time "Bedrock/SageMaker: No module named botocore" warnings get
# dropped. The matching `suppress_debug_info` flip is deferred to the call
# sites that actually import litellm (apply_litellm_suppress_debug_info)
# because importing litellm here would pull ~160 MiB of openai submodules
# into every long-lived process at startup, even on requests that never
# touch the LLM.
install_litellm_log_filter()
app_logger: logging.Logger = logging.getLogger("global_logger")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _get_sqlite_busy_timeout_ms() -> int:
    # Longer timeout to allow large batch deletes/updates to finish before giving up
    return 90000


def setup_dirs() -> None:
    """Create data directories. Logs a warning and continues if paths are not writable."""
    in_root = get_in_root()
    srv_root = get_srv_root()
    try:
        os.makedirs(in_root, exist_ok=True)
        os.makedirs(srv_root, exist_ok=True)
    except OSError as exc:
        # During CLI commands like migrations, the /app path may not exist
        app_logger.warning(
            "Could not create data directories (%s, %s): %s. "
            "This is expected during migrations on local dev.",
            in_root,
            srv_root,
            exc,
        )


class SchedulerConfig:
    SCHEDULER_JOBSTORES = {
        "default": {
            "type": "sqlalchemy",
            "url": "sqlite:////tmp/jobs.sqlite",
        }
    }
    SCHEDULER_EXECUTORS = {"default": {"type": "threadpool", "max_workers": 1}}
    SCHEDULER_JOB_DEFAULTS = {"coalesce": False, "max_instances": 1}


@event.listens_for(Engine, "connect", once=False)
def _set_sqlite_pragmas(dbapi_connection: Any, connection_record: Any) -> None:
    module = getattr(dbapi_connection.__class__, "__module__", "")
    if not module.startswith(("sqlite3", "pysqlite2")):
        return

    cursor = dbapi_connection.cursor()
    busy_timeout_ms = _get_sqlite_busy_timeout_ms()
    try:
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute(f"PRAGMA busy_timeout={busy_timeout_ms};")
        # Limit WAL file size to prevent checkpoint starvation
        cursor.execute("PRAGMA wal_autocheckpoint=1000;")
        # Cap WAL file size so reader-pinned growth gets truncated on checkpoint
        cursor.execute("PRAGMA journal_size_limit=67108864;")
    finally:
        cursor.close()


def setup_scheduler(app: Flask) -> None:
    """Initialize and start the scheduler."""
    if not is_test:
        scheduler.init_app(app)
        scheduler.start()


def create_app() -> Flask:
    disable_scheduler = _env_bool("PODLY_DISABLE_SCHEDULER", default=False)
    run_startup = _env_bool("PODLY_RUN_STARTUP", default=True)
    return _create_configured_app(
        app_role="web",
        run_startup=run_startup,
        start_scheduler=not disable_scheduler,
    )


def create_web_app() -> Flask:
    """Create the web (read-mostly) Flask app.

    This app should not run startup migrations/bootstrapping; DB writes are
    delegated to the writer service. Scheduler runs here so background processing
    happens in the web process.
    """
    return _create_configured_app(
        app_role="web",
        run_startup=False,
        start_scheduler=True,
        initialize_migrations=False,
    )


def create_writer_app() -> Flask:
    """Create the writer Flask app.

    This app owns startup migrations/bootstrapping.
    """
    return _create_configured_app(
        app_role="writer",
        run_startup=True,
        start_scheduler=False,
        register_http=False,
    )


def create_bootstrap_app() -> Flask:
    """Run schema, admin, and settings initialization once before a runtime writer.

    Unlike the legacy writer app, bootstrap must fail closed if defaults cannot
    be initialized. It does not start HTTP, a scheduler, or writer IPC.
    """
    return _create_configured_app(
        app_role="bootstrap",
        run_startup=True,
        start_scheduler=False,
        register_http=False,
        fail_on_startup_error=True,
    )


def create_processing_app() -> Flask:
    """Create the per-job processing Flask app.

    Processing workers need app config and database reads, but they do not own
    HTTP routes, startup migrations, or the scheduler.
    """
    return _create_configured_app(
        app_role="processing",
        run_startup=False,
        start_scheduler=False,
        register_http=False,
        initialize_migrations=False,
    )


def _create_configured_app(
    *,
    app_role: str,
    run_startup: bool,
    start_scheduler: bool,
    register_http: bool = True,
    initialize_migrations: bool = True,
    fail_on_startup_error: bool = False,
) -> Flask:
    # Setup directories early but only when actually creating the app (not during migrations)
    if not is_test:
        setup_dirs()

    app = _create_flask_app()
    app.config["PODLY_APP_ROLE"] = app_role
    auth_settings = _load_auth_settings()
    _apply_auth_settings(app, auth_settings)
    if register_http:
        _configure_session(app, auth_settings)
        _configure_cors(app)
    _configure_scheduler(app)
    _configure_database(app)
    _configure_external_loggers()
    _initialize_extensions(app, initialize_migrations=initialize_migrations)
    if register_http:
        _register_routes_and_middleware(app)

    app.config["developer_mode"] = config.developer_mode

    with app.app_context():
        if run_startup:
            if fail_on_startup_error:
                _run_app_startup(auth_settings, fail_on_settings_error=True)
            else:
                _run_app_startup(auth_settings)
        else:
            _hydrate_web_config()

        discord_settings = load_discord_settings()
        app.config["DISCORD_SETTINGS"] = discord_settings

    app.config["AUTH_SETTINGS"] = auth_settings.without_password()

    if app.config["DISCORD_SETTINGS"].enabled:
        app_logger.info(
            "Discord SSO enabled (guild restriction: %s)",
            "yes" if app.config["DISCORD_SETTINGS"].guild_ids else "no",
        )

    _validate_env_key_conflicts()
    if start_scheduler:
        _start_scheduler_and_jobs(app)
    return app


def _clear_scheduler_jobstore() -> None:
    """Remove persisted APScheduler jobs so startup adds a clean schedule."""
    jobstore_config = SchedulerConfig.SCHEDULER_JOBSTORES.get("default")
    if not isinstance(jobstore_config, dict):
        return

    url = jobstore_config.get("url")
    if not isinstance(url, str):
        return

    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return

    relative_path = url[len(prefix) :]
    project_root = Path(__file__).resolve().parents[2]
    jobstore_path = (project_root / Path(relative_path)).resolve()
    jobstore_path.parent.mkdir(parents=True, exist_ok=True)

    sidecars = [
        jobstore_path,
        jobstore_path.with_name(jobstore_path.name + "-wal"),
        jobstore_path.with_name(jobstore_path.name + "-shm"),
    ]

    try:
        cleared_any = False
        for path in sidecars:
            if path.exists():
                path.unlink()
                cleared_any = True

        if cleared_any:
            app_logger.info(
                "Startup: cleared persisted APScheduler jobs at %s", jobstore_path
            )
    except OSError as exc:
        app_logger.warning(
            "Startup: failed to clear APScheduler jobs at %s: %s", jobstore_path, exc
        )


def _validate_env_key_conflicts() -> None:
    """Validate that environment API key variables are not conflicting.

    Rules:
    - If Groq is being used as the LLM and both LLM_API_KEY and GROQ_API_KEY are
      set but differ -> error
    """
    llm_key = os.environ.get("LLM_API_KEY")
    groq_key = os.environ.get("GROQ_API_KEY")
    llm_model = os.environ.get("LLM_MODEL") or DEFAULTS.LLM_DEFAULT_MODEL
    llm_model_norm = llm_model.strip().lower() if isinstance(llm_model, str) else ""
    groq_llm_selected = llm_model_norm.startswith("groq/")

    conflicts: list[str] = []
    if groq_llm_selected and llm_key and groq_key and llm_key != groq_key:
        conflicts.append(
            "LLM_API_KEY and GROQ_API_KEY are both set but have different values"
        )

    if conflicts:
        details = "; ".join(conflicts)
        message = (
            "Configuration error: Conflicting environment API keys detected. "
            f"{details}. To use Groq, prefer setting GROQ_API_KEY only; "
            "alternatively, set the variables to the same value."
        )
        # Crash the process so Docker start fails clearly
        raise SystemExit(message)


def _create_flask_app() -> Flask:
    static_folder = os.path.abspath(os.path.join(os.path.dirname(__file__), "static"))
    instance_path = os.environ.get("PODLY_INSTANCE_DIR")
    if instance_path:
        return Flask(
            __name__,
            static_folder=static_folder,
            instance_path=os.path.abspath(instance_path),
        )
    return Flask(__name__, static_folder=static_folder)


def _load_auth_settings() -> AuthSettings:
    try:
        return load_auth_settings()
    except RuntimeError as exc:
        app_logger.critical("Authentication configuration error: %s", exc)
        raise


def _apply_auth_settings(app: Flask, auth_settings: AuthSettings) -> None:
    app.config["AUTH_SETTINGS"] = auth_settings
    app.config["REQUIRE_AUTH"] = auth_settings.require_auth
    app.config["AUTH_ADMIN_USERNAME"] = auth_settings.admin_username


def _configure_session(app: Flask, auth_settings: AuthSettings) -> None:
    secret_key = os.environ.get("PODLY_SECRET_KEY")
    if not secret_key:
        try:
            secret_key = secrets.token_urlsafe(64)
        except Exception as exc:
            raise RuntimeError("Failed to generate session secret key.") from exc
        if auth_settings.require_auth:
            app_logger.warning(
                "Generated ephemeral session secret key because PODLY_SECRET_KEY is not set; "
                "all sessions will be invalidated on restart."
            )

    app.config["SECRET_KEY"] = secret_key
    app.config["SESSION_COOKIE_NAME"] = os.environ.get(
        "PODLY_SESSION_COOKIE_NAME", "podly_session"
    )
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    # We always allow HTTP cookies so self-hosted installs work behind simple HTTP reverse proxies.
    app.config["SESSION_COOKIE_SECURE"] = False


def _configure_cors(app: Flask) -> None:
    default_cors = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]
    cors_origins_env = os.environ.get("CORS_ORIGINS")
    if cors_origins_env:
        cors_origins = [
            origin.strip() for origin in cors_origins_env.split(",") if origin.strip()
        ]
    else:
        cors_origins = default_cors
    CORS(
        app,
        resources={r"/*": {"origins": cors_origins}},
        allow_headers=["Content-Type", "Authorization", "Range"],
        methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        supports_credentials=True,
    )


def _configure_scheduler(app: Flask) -> None:
    app.config.from_object(SchedulerConfig())


def _configure_database(app: Flask) -> None:
    def _get_sqlite_connect_timeout() -> int:
        return 60

    uri_scheme = "sqlite"
    connect_timeout = _get_sqlite_connect_timeout()
    app.config["SQLALCHEMY_DATABASE_URI"] = (
        f"{uri_scheme}:///sqlite3.db?timeout={connect_timeout}"
    )
    engine_options: dict[str, Any] = {
        "connect_args": {
            "timeout": connect_timeout,
        },
        # Retain fewer idle read connections and their SQLite descriptors.
        # Preserve the existing ten-connection concurrency ceiling; runtime
        # writes still belong exclusively to the writer service.
        "pool_size": 3,
        "max_overflow": 7,
    }

    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = engine_options
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False


def _configure_external_loggers() -> None:
    groq_logger = logging.getLogger("groq")
    groq_logger.setLevel(logging.INFO)


def _configure_readonly_sessions(app: Flask) -> None:
    """
    Configure SQLAlchemy sessions to be read-only for the web/API app.
    This prevents Flask from acquiring write locks on the database, which
    can cause deadlocks with the writer service.

    Only the writer service should perform database writes.
    """
    from sqlalchemy.orm import Session

    @event.listens_for(Session, "after_begin", once=False)
    def receive_after_begin(
        session: Session, transaction: Any, connection: Any
    ) -> None:
        """Set new transactions to read-only by default."""
        # Only apply to sessions created within this app context
        try:
            if not has_app_context():
                return
            if current_app.config.get("PODLY_APP_ROLE") not in {"web", "processing"}:
                return
        except Exception:  # noqa: BLE001
            return

        # Set isolation level to prevent write locks
        # For SQLite, this prevents RESERVED/EXCLUSIVE locks
        connection.connection.isolation_level = "DEFERRED"

        # Disable autoflush to prevent accidental writes
        session.autoflush = False

        # Mark session as read-only to prevent any writes
        session.info["readonly"] = True

    @event.listens_for(Session, "before_flush", once=False)
    def receive_before_flush(
        session: Session, flush_context: Any, instances: Any
    ) -> None:
        """Prevent accidental writes in read-only sessions."""
        try:
            if not has_app_context():
                return
            if current_app.config.get("PODLY_APP_ROLE") not in {"web", "processing"}:
                return
        except Exception:  # noqa: BLE001
            return

        if session.info.get("readonly"):
            raise RuntimeError(
                "Attempted to flush changes in read-only session. "
                "All database writes must go through the writer service."
            )


def _initialize_extensions(app: Flask, *, initialize_migrations: bool = True) -> None:
    db.init_app(app)
    # Production readers do not run migrations. Keep Alembic and its CLI
    # dependencies out of their resident imports; create_app still registers
    # the migration CLI even when startup is disabled.
    if initialize_migrations:
        migrate.init_app(app, db)

    # Configure read-only mode for web/API and processing Flask apps to prevent database locks
    # Only the writer service should acquire write locks
    if app.config.get("PODLY_APP_ROLE") in {"web", "processing"}:
        _configure_readonly_sessions(app)


def _register_routes_and_middleware(app: Flask) -> None:
    from app.routes import register_routes

    register_routes(app)
    init_auth_middleware(app)

    _register_api_logging(app)
    _register_memory_cleanup(app)


def _register_api_logging(app: Flask) -> None:
    @app.after_request
    def _log_api_request(response: Any) -> Any:
        try:
            path = request.path
        except Exception:  # pragma: no cover  # noqa: BLE001
            return response

        if not path.startswith("/api/"):
            return response

        method = request.method
        status = getattr(response, "status_code", None)

        user = getattr(g, "current_user", None)
        user_id = getattr(user, "id", None)

        app_logger.info(
            "[API] %s %s status=%s user_id=%s content_type=%s",
            method,
            path,
            status,
            user_id,
            getattr(response, "content_type", None),
        )

        return response


def _register_memory_cleanup(app: Flask) -> None:
    from app.memory_pressure import (
        RequestBurstTrim,
        consume_memory_trim_contexts,
        release_memory_to_os,
    )

    burst_trim = RequestBurstTrim()
    app.extensions["podly_request_burst_trim"] = burst_trim

    # request_started fires before any before_request hook can short-circuit;
    # the g marker keeps the count balanced if context setup fails earlier.
    def _note_request_started(_sender: Flask, **_extra: Any) -> None:
        g.podly_burst_tracked = True
        burst_trim.started()

    request_started.connect(_note_request_started, app, weak=False)

    @app.teardown_request
    def _note_request_finished(_exc: BaseException | None) -> None:
        if g.pop("podly_burst_tracked", False):
            burst_trim.finished()

    @app.teardown_appcontext
    def _release_memory_after_app_context(exc: BaseException | None) -> None:
        trim_contexts = consume_memory_trim_contexts()
        if not trim_contexts:
            return

        try:
            db.session.remove()
        except Exception as remove_exc:
            app_logger.debug(
                "Failed to remove DB session before memory trim: %s",
                remove_exc,
                exc_info=True,
            )

        context = ", ".join(trim_contexts[-3:])
        release_memory_to_os(f"app context teardown after {context}", app_logger)


def _trim_after_request_burst(app: Flask) -> None:
    from app.memory_pressure import release_memory_to_os

    burst_trim = app.extensions["podly_request_burst_trim"]
    if not burst_trim.claim():
        return
    with app.app_context():
        # Closing every idle pooled connection lets SQLite close the database
        # descriptors it defers while any connection holds the WAL read lock.
        db.engine.dispose()
    release_memory_to_os("web request burst", app_logger)


def _start_request_burst_trim(app: Flask) -> None:
    if not _env_bool("PODLY_MEMORY_TRIM_ENABLED", default=True):
        return

    def _loop() -> None:
        while True:
            time.sleep(0.25)
            try:
                _trim_after_request_burst(app)
            except Exception:
                app_logger.debug("request burst trim failed", exc_info=True)

    threading.Thread(target=_loop, name="web-burst-trim", daemon=True).start()


def _run_app_startup(
    auth_settings: AuthSettings, *, fail_on_settings_error: bool = False
) -> None:
    from flask_migrate import upgrade

    upgrade()
    bootstrap_admin_user(auth_settings)
    try:
        ensure_defaults_and_hydrate()

        _reset_processor_if_loaded()
    except Exception as exc:
        app_logger.error(f"Failed to initialize settings: {exc}")
        if fail_on_settings_error:
            raise


def _hydrate_web_config() -> None:
    """Hydrate runtime config for web app (read-only)."""
    hydrate_runtime_config_inplace()

    _reset_processor_if_loaded()


def _reset_processor_if_loaded() -> None:
    processor_module = sys.modules.get("app.processor")
    if processor_module is None:
        return
    processor_singleton = getattr(processor_module, "ProcessorSingleton", None)
    if processor_singleton is not None:
        processor_singleton.reset_instance()


def _start_scheduler_and_jobs(app: Flask) -> None:
    from app.background import (
        add_background_job,
        schedule_cleanup_job,
        schedule_memory_trim_job,
    )
    from app.jobs_manager import get_jobs_manager

    _clear_scheduler_jobstore()
    setup_scheduler(app)

    jobs_manager = get_jobs_manager()
    clear_result = jobs_manager.clear_active_jobs()
    if clear_result["status"] == "success":
        app_logger.info(f"Startup: {clear_result['message']}")
    else:
        app_logger.warning(f"Startup job clearing failed: {clear_result['message']}")

    add_background_job(
        10
        if config.background_update_interval_minute is None
        else int(config.background_update_interval_minute)
    )
    schedule_cleanup_job(getattr(config, "post_cleanup_retention_days", None))
    schedule_memory_trim_job()
    _start_request_burst_trim(app)
