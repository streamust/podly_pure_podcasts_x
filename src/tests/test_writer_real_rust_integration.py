"""Real Flask and processing-client writes through the selected Rust writer."""

from __future__ import annotations

import http.client
import json
import os
import sqlite3
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from flask import Flask

from app.extensions import db
from app.writer import client as writer_client_module
from app.writer.client import WriterOutcomeUnknownError, writer_client
from tests.test_writer_differential_parity import RustWriterParityServer
from tests.writer_parity_fixtures import WriterParityPair


def _make_web_app(database_uri: str) -> Flask:
    app = Flask("writer-rust-real-client-integration")
    app.config.update(
        SQLALCHEMY_DATABASE_URI=database_uri,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        TESTING=True,
    )
    db.init_app(app)

    from app.routes.feed_routes import feed_bp

    app.register_blueprint(feed_bp)
    return app


def _read_sqlite(path: Path, query: str, params: tuple[object, ...]) -> tuple[Any, ...]:
    with sqlite3.connect(path) as connection:
        row = connection.execute(query, params).fetchone()
    assert row is not None
    return tuple(row)


class _CommitThenDropProxy(ThreadingHTTPServer):
    """Forward RPCs to Rust, but lose the first response after it commits."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, writer_port: int) -> None:
        self.writer_port = writer_port
        self.requests: list[dict[str, Any]] = []
        self.responses_received: list[tuple[int, bytes]] = []
        self.first_response_received = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                proxy = self.server
                assert isinstance(proxy, _CommitThenDropProxy)
                body = self.rfile.read(int(self.headers["Content-Length"]))
                request = json.loads(body)
                proxy.requests.append(request)

                upstream = http.client.HTTPConnection(
                    "127.0.0.1", proxy.writer_port, timeout=10
                )
                try:
                    upstream.request(
                        "POST",
                        self.path,
                        body=body,
                        headers={
                            "Authorization": self.headers["Authorization"],
                            "Content-Type": self.headers.get(
                                "Content-Type", "application/json"
                            ),
                            "Content-Length": str(len(body)),
                            "Connection": "close",
                        },
                    )
                    upstream_response = upstream.getresponse()
                    response_body = upstream_response.read()
                    response = (upstream_response.status, response_body)
                    proxy.responses_received.append(response)
                finally:
                    upstream.close()

                if len(proxy.requests) == 1:
                    # Reading the full successful Rust response proves the
                    # executor committed before the client loses its response.
                    proxy.first_response_received.set()
                    return

                status, response_body = response
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                self.wfile.write(response_body)

            def log_message(self, format: str, *args: Any) -> None:
                _ = format, args

        super().__init__(("127.0.0.1", 0), Handler)


def test_real_flask_and_processing_writes_use_rust_writer(
    writer_parity_pair: WriterParityPair,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise actual application code and the real isolated Rust RPC server."""
    monkeypatch.setenv("PODLY_WRITER_BACKEND", "rust")
    monkeypatch.setenv("PODLY_IPC_AUTHKEY", "synthetic-writer-parity-auth-key")

    server = RustWriterParityServer(writer_parity_pair.rust.db_path, tmp_path)
    monkeypatch.setattr(writer_client_module, "RUST_WRITER_HOST", "127.0.0.1")
    monkeypatch.setattr(writer_client_module, "RUST_WRITER_PORT", server.port)

    # A successful result and database mutation must come from RPC. Raising here
    # makes every legacy local execution path fail the test immediately.
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

    ready = server._request("/v1/ready")
    assert ready["backend"] == "rust"
    assert ready["ready"] is True
    assert server.process.poll() is None
    assert server.process.pid != os.getpid()

    app = _make_web_app(writer_parity_pair.rust.database_uri)
    from app.routes import feed_routes

    # Preserve the actual route and writer path while satisfying its admin
    # guard with a synthetic authorized test request.
    monkeypatch.setattr(feed_routes, "require_admin", lambda _message: (None, None))
    feed_id = writer_parity_pair.manifest.feed_ids[0]
    job_id = writer_parity_pair.manifest.job_ids[1]
    synthetic_job_id = "00000000-0000-0000-0000-000000009938"

    try:
        with app.test_client() as client:
            response = client.patch(
                f"/api/feeds/{feed_id}/settings",
                json={"auto_whitelist_new_episodes_override": True},
            )
            assert response.status_code == 200, response.get_data(as_text=True)
            assert response.get_json()["auto_whitelist_new_episodes_override"] is True

        # This is the same singleton client used by production processing
        # helpers. Create and advance a synthetic job through its lifecycle.
        created = writer_client.action(
            "create_job",
            {
                "job_data": {
                    "id": synthetic_job_id,
                    "post_guid": writer_parity_pair.manifest.post_guids[1],
                    "status": "pending",
                    "current_step": 0,
                    "step_name": "Queued",
                    "total_steps": 5,
                    "progress_percentage": 0.0,
                    "created_at": datetime.now(UTC).isoformat(),
                }
            },
            wait=True,
        )
        assert created is not None and created.success, created
        assert created.data == {"job_id": synthetic_job_id}

        result = writer_client.action(
            "update_job_status",
            {
                "job_id": synthetic_job_id,
                "status": "running",
                "step": 3,
                "step_name": "Synthetic Rust writer integration",
                "progress": 55.5,
                "total_steps": 5,
            },
            wait=True,
        )
        assert result is not None and result.success, result
        assert result.data == {"job_id": synthetic_job_id, "status": "running"}

        completed = writer_client.action(
            "update_job_status",
            {
                "job_id": synthetic_job_id,
                "status": "completed",
                "step": 5,
                "step_name": "Complete",
                "progress": 100.0,
                "total_steps": 5,
            },
            wait=True,
        )
        assert completed is not None and completed.success, completed
        assert completed.data == {"job_id": synthetic_job_id, "status": "completed"}

        # Inspect with an independent SQLite connection after the Rust RPCs
        # completed; Flask's ORM identity map cannot serve as write evidence.
        assert _read_sqlite(
            writer_parity_pair.rust.db_path,
            "SELECT auto_whitelist_new_episodes_override FROM feed WHERE id=?",
            (feed_id,),
        ) == (1,)
        assert _read_sqlite(
            writer_parity_pair.rust.db_path,
            "SELECT status,current_step,step_name,progress_percentage,total_steps "
            "FROM processing_job WHERE id=?",
            (synthetic_job_id,),
        ) == ("completed", 5, "Complete", 100.0, 5)

        # Keep Python's independent fixture clone untouched as an isolation
        # assertion alongside the explicit local-fallback traps.
        assert _read_sqlite(
            writer_parity_pair.python.db_path,
            "SELECT auto_whitelist_new_episodes_override FROM feed WHERE id=?",
            (feed_id,),
        ) == (None,)
        assert _read_sqlite(
            writer_parity_pair.python.db_path,
            "SELECT current_step,step_name,progress_percentage,total_steps "
            "FROM processing_job WHERE id=?",
            (job_id,),
        ) == (2, "Transcribing", 42.5, 4)
        assert _read_sqlite(
            writer_parity_pair.python.db_path,
            "SELECT COUNT(*) FROM processing_job WHERE id=?",
            (synthetic_job_id,),
        ) == (0,)

        ready_after = server._request("/v1/ready")
        assert ready_after["backend"] == "rust"
        assert ready_after["ready"] is True
    finally:
        with app.app_context():
            db.session.remove()
            db.engine.dispose()
        server.close()


def test_committed_rust_write_response_loss_is_unknown_and_never_replayed(
    writer_parity_pair: WriterParityPair,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost post-commit reply is not retried; the next RPC still succeeds."""
    monkeypatch.setenv("PODLY_WRITER_BACKEND", "rust")
    monkeypatch.setenv("PODLY_IPC_AUTHKEY", "synthetic-writer-parity-auth-key")
    server = RustWriterParityServer(writer_parity_pair.rust.db_path, tmp_path)
    proxy = _CommitThenDropProxy(server.port)
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    monkeypatch.setattr(writer_client_module, "RUST_WRITER_HOST", "127.0.0.1")
    monkeypatch.setattr(writer_client_module, "RUST_WRITER_PORT", proxy.server_port)
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

    post_id = writer_parity_pair.manifest.post_ids[0]
    initial_count = _read_sqlite(
        writer_parity_pair.rust.db_path,
        "SELECT download_count FROM post WHERE id=?",
        (post_id,),
    )[0]
    try:
        with pytest.raises(WriterOutcomeUnknownError) as unknown:
            writer_client.action(
                "increment_download_count", {"post_id": post_id}, wait=True
            )

        assert proxy.first_response_received.wait(timeout=5), (
            "proxy did not receive the successful Rust response before dropping it"
        )
        assert len(proxy.requests) == 1
        lost_command_id = proxy.requests[0]["command_id"]
        assert unknown.value.command_id == lost_command_id
        assert json.loads(proxy.responses_received[0][1])["success"] is True
        assert _read_sqlite(
            writer_parity_pair.rust.db_path,
            "SELECT download_count FROM post WHERE id=?",
            (post_id,),
        ) == (initial_count + 1,)

        recovered = writer_client.action(
            "increment_download_count", {"post_id": post_id}, wait=True
        )
        assert recovered is not None and recovered.success, recovered
        assert recovered.data == {"post_id": post_id, "updated": 1}
        assert len(proxy.requests) == 2
        command_ids = [request["command_id"] for request in proxy.requests]
        assert command_ids[0] == lost_command_id
        assert len(set(command_ids)) == 2
        assert _read_sqlite(
            writer_parity_pair.rust.db_path,
            "SELECT download_count FROM post WHERE id=?",
            (post_id,),
        ) == (initial_count + 2,)
    finally:
        proxy.shutdown()
        proxy.server_close()
        proxy_thread.join(timeout=2)
        server.close()


def test_post_datetime_update_round_trips_through_real_rust_writer(
    writer_parity_pair: WriterParityPair,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ad classifier's refined-boundary update, sent to the real Rust writer."""
    from sqlalchemy.dialects import sqlite as sqlite_dialect

    monkeypatch.setenv("PODLY_WRITER_BACKEND", "rust")
    monkeypatch.setenv("PODLY_IPC_AUTHKEY", "synthetic-writer-parity-auth-key")
    server = RustWriterParityServer(writer_parity_pair.rust.db_path, tmp_path)
    monkeypatch.setattr(writer_client_module, "RUST_WRITER_HOST", "127.0.0.1")
    monkeypatch.setattr(writer_client_module, "RUST_WRITER_PORT", server.port)
    monkeypatch.setattr(
        writer_client,
        "_local_execute",
        lambda _command: pytest.fail("Python local writer fallback was invoked"),
    )

    post_id = _read_sqlite(
        writer_parity_pair.rust.db_path,
        "SELECT id FROM post WHERE guid = ?",
        (writer_parity_pair.manifest.post_guids[0],),
    )[0]
    updated_at = datetime(2026, 9, 30, 7, 44, 58, 991234)
    boundaries = [{"orig_start": 10.0, "refined_start": 10.25, "confidence": 0.9}]

    try:
        result = writer_client.update(
            "Post",
            post_id,
            {
                "refined_ad_boundaries": boundaries,
                "refined_ad_boundaries_updated_at": updated_at,
            },
            wait=True,
        )
        assert result is not None and result.success, result
    finally:
        server.close()

    raw_boundaries, raw_updated_at = _read_sqlite(
        writer_parity_pair.rust.db_path,
        "SELECT refined_ad_boundaries, refined_ad_boundaries_updated_at "
        "FROM post WHERE id = ?",
        (post_id,),
    )
    assert json.loads(raw_boundaries) == boundaries
    assert raw_updated_at == "2026-09-30 07:44:58.991234"
    parse = sqlite_dialect.DATETIME().result_processor(sqlite_dialect.dialect(), None)
    assert parse is not None and parse(raw_updated_at) == updated_at
