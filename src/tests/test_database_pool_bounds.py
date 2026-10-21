import os
from contextlib import ExitStack, suppress
from pathlib import Path
from typing import Any

import pytest
from flask import Flask, request
from sqlalchemy import create_engine
from sqlalchemy.pool import QueuePool

from app import _configure_database
from app.memory_pressure import RequestBurstTrim


def test_database_pool_releases_overflow_without_reducing_concurrency(
    tmp_path: Path,
) -> None:
    app = Flask("database-pool-bounds", instance_path=str(tmp_path))
    _configure_database(app)
    options = app.config["SQLALCHEMY_ENGINE_OPTIONS"]
    assert options["pool_size"] + options["max_overflow"] == 10
    assert options["connect_args"]["timeout"] == 60

    engine = create_engine(f"sqlite:///{tmp_path / 'isolated-pool.db'}", **options)
    try:
        assert isinstance(engine.pool, QueuePool)
        with ExitStack() as connections:
            for _ in range(10):
                connections.enter_context(engine.connect())
            assert engine.pool.checkedout() == 10
        assert engine.pool.checkedout() == 0
        assert engine.pool.checkedin() == 3
        assert engine.pool.size() == 3
    finally:
        engine.dispose()


def _open_descriptors_for(path: Path) -> int:
    fd_dir = Path("/proc/self/fd")
    count = 0
    for entry in fd_dir.iterdir():
        with suppress(OSError):
            if Path(os.readlink(entry)) == path:
                count += 1
    return count


@pytest.mark.skipif(not Path("/proc/self/fd").exists(), reason="needs procfs")
def test_request_burst_trim_releases_pinned_sqlite_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app as app_module

    db_path = tmp_path / "burst.db"
    flask_app = Flask("burst-trim", instance_path=str(tmp_path))
    _configure_database(flask_app)
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
    app_module.db.init_app(flask_app)
    app_module._register_memory_cleanup(flask_app)
    trims: list[str] = []
    monkeypatch.setattr(
        "app.memory_pressure.release_memory_to_os",
        lambda context, *_args: trims.append(context),
    )

    with flask_app.app_context():
        engine = app_module.db.engine
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            connection.exec_driver_sql("CREATE TABLE t (x)")
            connection.commit()
        engine.dispose()
        with ExitStack() as connections:
            for _ in range(8):
                connection = connections.enter_context(engine.connect())
                connection.exec_driver_sql("SELECT count(*) FROM t").scalar()
        # WAL read locks held by retained pool connections pin every closed
        # overflow connection's descriptor.
        assert _open_descriptors_for(db_path) == 8

    burst_trim = flask_app.extensions["podly_request_burst_trim"]
    burst_trim.started()
    burst_trim.finished()
    burst_trim.quiet_seconds = 0
    app_module._trim_after_request_burst(flask_app)

    assert _open_descriptors_for(db_path) == 0
    assert trims == ["web request burst"]
    app_module._trim_after_request_burst(flask_app)
    assert trims == ["web request burst"]


def test_request_burst_trim_waits_for_quiet_and_idle() -> None:
    trim = RequestBurstTrim(quiet_seconds=1.0)
    assert not trim.claim()

    trim.started()
    assert not trim.claim(now=10**9)
    trim.finished()
    assert not trim.claim()
    assert trim.claim(now=10**9)
    assert not trim.claim(now=10**9)


def test_request_counting_balances_short_circuit_and_errors(tmp_path: Path) -> None:
    import app as app_module

    flask_app = Flask("burst-count", instance_path=str(tmp_path))
    app_module._register_memory_cleanup(flask_app)

    @flask_app.before_request
    def _deny() -> Any:
        if request.path == "/denied":
            return "no", 401
        return None

    @flask_app.route("/boom")
    def _boom() -> str:
        raise RuntimeError("boom")

    client = flask_app.test_client()
    assert client.get("/denied").status_code == 401
    assert client.get("/boom").status_code == 500

    burst_trim = flask_app.extensions["podly_request_burst_trim"]
    assert burst_trim._active == 0
    assert burst_trim.claim(now=10**9)
