from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

import pytest

from app.writer import client as writer_client_module
from app.writer.backend import WriterBackend, selected_writer_backend
from app.writer.client import (
    WriterClient,
    WriterOutcomeUnknownError,
    WriterTransportError,
)
from app.writer.protocol import WriteCommand, WriteCommandType


class _RpcServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        callback: Callable[[dict[str, Any]], tuple[int, bytes]],
        address: tuple[str, int] = ("127.0.0.1", 0),
    ) -> None:
        self.callback = callback

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                payload = json.loads(self.rfile.read(length))
                server = cast(_RpcServer, self.server)
                server.requests.append(payload)
                server.authorization_headers.append(self.headers.get("Authorization"))
                status, body = server.callback(payload)
                self.send_response(200 if status == 598 else status)
                self.send_header("Content-Type", "application/json")
                self.send_header(
                    "Content-Length",
                    str(len(body) + 32 if status == 598 else len(body)),
                )
                self.end_headers()
                try:
                    self.wfile.write(body)
                    self.wfile.flush()
                except OSError:
                    pass

            def log_message(self, format: str, *args: Any) -> None:
                _ = format, args

        super().__init__(address, Handler)
        self.requests: list[dict[str, Any]] = []
        self.authorization_headers: list[str | None] = []


class _RunningServer:
    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        callback: Callable[[dict[str, Any]], tuple[int, bytes]],
    ) -> None:
        self.server = _RpcServer(callback)
        monkeypatch.setattr(writer_client_module, "RUST_WRITER_HOST", "127.0.0.1")
        monkeypatch.setattr(
            writer_client_module, "RUST_WRITER_PORT", self.server.server_port
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def requests(self) -> list[dict[str, Any]]:
        return self.server.requests

    @property
    def authorization_headers(self) -> list[str | None]:
        return self.server.authorization_headers

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def _reply(
    request: dict[str, Any],
    *,
    state: str = "completed",
    status: int = 200,
    success: bool = True,
    result: Any = None,
    error: dict[str, Any] | None = None,
) -> tuple[int, bytes]:
    body: dict[str, Any] = {
        "version": 1,
        "state": state,
        "command_id": request["command_id"],
    }
    if state == "accepted":
        body["admitted"] = True
    elif state == "rejected":
        body["admitted"] = False
        body["error"] = error or {
            "code": "capacity_exhausted",
            "message": "writer capacity exhausted",
            "retryable": True,
            "outcome": "not_admitted",
        }
    elif state == "unknown":
        body["admitted"] = True
        body["error"] = error or {
            "code": "deadline_exceeded",
            "message": "deadline exceeded after admission",
            "retryable": False,
            "outcome": "unknown",
        }
    else:
        body["success"] = success
        if success:
            body["result"] = result
        else:
            body["error"] = error or {
                "code": "invalid_params",
                "message": "invalid parameters",
                "retryable": False,
                "outcome": "rolled_back",
            }
    return status, json.dumps(body).encode()


@pytest.fixture(autouse=True)
def _rust_writer_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PODLY_WRITER_BACKEND", "rust")
    monkeypatch.setenv("PODLY_IPC_AUTHKEY", "test-only-key")


def test_backend_selector_defaults_to_rust_and_rejects_unknown_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PODLY_WRITER_BACKEND", raising=False)
    assert selected_writer_backend() is WriterBackend.RUST
    monkeypatch.setenv("PODLY_WRITER_BACKEND", "python")
    assert selected_writer_backend() is WriterBackend.PYTHON
    monkeypatch.setenv("PODLY_WRITER_BACKEND", "pythonish")
    with pytest.raises(RuntimeError, match="must be either"):
        selected_writer_backend()


def test_rust_writer_action_preserves_result_and_wait_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _RunningServer(
        monkeypatch,
        lambda request: _reply(
            request,
            state="accepted" if not request["wait"] else "completed",
            status=202 if not request["wait"] else 200,
            result={"count": 3},
        ),
    )
    try:
        client = WriterClient()
        result = client.action("increment_download_count", {"post_id": 9}, wait=True)
        assert result is not None and result.success
        assert result.data == {"count": 3}
        assert (
            client.action("touch_feed_access_token", {"token_id": 2}, wait=False)
            is None
        )
        assert server.requests[0]["operation"] == "action"
        assert server.requests[0]["action"] == "increment_download_count"
        assert server.requests[1]["wait"] is False
        assert server.authorization_headers == [
            "PodlyWriter dGVzdC1vbmx5LWtleQ",
            "PodlyWriter dGVzdC1vbmx5LWtleQ",
        ]
    finally:
        server.close()


def test_known_fire_and_forget_writer_call_shapes_only_wait_for_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _RunningServer(
        monkeypatch,
        lambda request: _reply(request, state="accepted", status=202),
    )
    try:
        client = WriterClient()
        results = [
            client.action("touch_feed_access_token", {"token_id": 2}, wait=False),
            client.action("increment_download_count", {"post_id": 9}, wait=False),
            client.action("update_user_last_active", {"user_id": 4}, wait=False),
            client.update("ModelCall", 17, {"estimated_cost_usd": 0.25}, wait=False),
            client.action("delete_feed_cascade", {"feed_id": 8}, wait=False),
        ]
        assert results == [None] * 5
        assert len(server.requests) == 5
        assert all(request["wait"] is False for request in server.requests)
        assert [request["operation"] for request in server.requests] == [
            "action",
            "action",
            "action",
            "update",
            "action",
        ]
    finally:
        server.close()


def test_rust_writer_maps_scalar_action_result_and_domain_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def callback(request: dict[str, Any]) -> tuple[int, bytes]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _reply(request, result=7)
        return _reply(request, success=False)

    server = _RunningServer(monkeypatch, callback)
    try:
        client = WriterClient()
        scalar = client.action("clear_all_jobs", {}, wait=True)
        failed = client.action("create_job", {}, wait=True)
        assert scalar is not None and scalar.data == 7
        assert failed is not None and not failed.success
        assert failed.error == "invalid_params: invalid parameters"
    finally:
        server.close()


def test_rust_wait_false_admission_rejection_is_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _RunningServer(
        monkeypatch,
        lambda request: _reply(request, state="rejected", status=429),
    )
    try:
        with pytest.raises(WriterTransportError, match="capacity_exhausted"):
            WriterClient().action("touch_feed_access_token", {}, wait=False)
    finally:
        server.close()


def test_rust_mode_never_uses_python_fallback_even_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An ephemeral port that has been released gives a deterministic refusal.
    probe = _RpcServer(_reply)
    port = probe.server_port
    probe.server_close()
    monkeypatch.setattr(writer_client_module, "RUST_WRITER_HOST", "127.0.0.1")
    monkeypatch.setattr(writer_client_module, "RUST_WRITER_PORT", port)
    monkeypatch.setenv("PODLY_WRITER_LOCAL_FALLBACK", "1")
    client = WriterClient()
    monkeypatch.setattr(
        client, "_local_execute", lambda _cmd: pytest.fail("Python fallback ran")
    )
    with pytest.raises(WriterTransportError, match="connection failed"):
        client.action("touch_feed_access_token", {}, wait=True)


def test_timeout_after_admission_is_unknown_and_not_replayed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    committed = threading.Event()
    committed_commands: list[str] = []
    calls = 0

    def callback(request: dict[str, Any]) -> tuple[int, bytes]:
        nonlocal calls
        calls += 1
        if calls == 1:
            # Model a write whose durable commit has completed but whose reply
            # is delayed beyond the client's deadline.
            committed_commands.append(request["command_id"])
            committed.set()
            time.sleep(1.2)
        return _reply(request, result={"committed": True})

    server = _RunningServer(monkeypatch, callback)
    try:
        client = WriterClient()
        command = WriteCommand(
            id="timeout-after-commit",
            type=WriteCommandType.ACTION,
            model=None,
            data={"action": "__test_slow", "params": {}},
        )
        with pytest.raises(WriterOutcomeUnknownError) as raised:
            client.submit(command, wait=True, timeout=1)
        assert raised.value.command_id == command.id
        assert committed.wait(timeout=1)
        followup = client.action("increment_download_count", {"post_id": 11}, wait=True)
        assert followup is not None and followup.success
        assert committed_commands == [command.id]
        assert calls == 2
    finally:
        server.close()


def test_partial_response_and_mismatched_command_id_are_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _RunningServer(
        monkeypatch,
        lambda request: (
            200,
            json.dumps(
                {
                    "version": 1,
                    "state": "completed",
                    "command_id": "some-other-command",
                    "success": True,
                    "result": None,
                }
            ).encode(),
        ),
    )
    try:
        with pytest.raises(WriterOutcomeUnknownError, match="command ID"):
            WriterClient().action("increment_download_count", {}, wait=True)
    finally:
        server.close()

    truncated = _RunningServer(
        monkeypatch,
        lambda request: (598, json.dumps(_reply(request)[1]).encode()),
    )
    try:
        with pytest.raises(WriterOutcomeUnknownError, match="connection ended"):
            WriterClient().action("increment_download_count", {}, wait=True)
    finally:
        truncated.close()


def test_connection_failure_then_writer_restart_reconnects_without_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _RpcServer(lambda request: _reply(request, result={"first": True}))
    port = first.server_port
    monkeypatch.setattr(writer_client_module, "RUST_WRITER_HOST", "127.0.0.1")
    monkeypatch.setattr(writer_client_module, "RUST_WRITER_PORT", port)
    first_thread = threading.Thread(target=first.serve_forever, daemon=True)
    first_thread.start()
    client = WriterClient()
    try:
        first_result = client.action("increment_download_count", {"post_id": 1})
        assert first_result is not None and first_result.success
        first.shutdown()
        first.server_close()
        first_thread.join(timeout=2)

        with pytest.raises(WriterTransportError):
            client.action("increment_download_count", {"post_id": 2})

        restarted = _RpcServer(
            lambda request: _reply(request, result={"restarted": True}),
            ("127.0.0.1", port),
        )
        restarted_thread = threading.Thread(target=restarted.serve_forever, daemon=True)
        restarted_thread.start()
        try:
            result = client.action("increment_download_count", {"post_id": 3})
            assert result is not None and result.success
            assert result.data == {"restarted": True}
            assert [request["params"]["post_id"] for request in restarted.requests] == [
                3
            ]
        finally:
            restarted.shutdown()
            restarted.server_close()
            restarted_thread.join(timeout=2)
    finally:
        if first_thread.is_alive():
            first.shutdown()
            first.server_close()
            first_thread.join(timeout=2)


def test_mismatched_writer_protocol_version_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _RunningServer(
        monkeypatch,
        lambda request: (
            200,
            json.dumps(
                {
                    "version": 2,
                    "state": "completed",
                    "command_id": request["command_id"],
                    "success": True,
                    "result": None,
                }
            ).encode(),
        ),
    )
    try:
        with pytest.raises(WriterOutcomeUnknownError, match="protocol mismatch"):
            WriterClient().action("increment_download_count", {}, wait=True)
        assert len(server.requests) == 1
    finally:
        server.close()


def test_pre_admission_rejection_then_success_uses_new_correlated_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def callback(request: dict[str, Any]) -> tuple[int, bytes]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _reply(request, state="rejected", status=429)
        return _reply(request, result={"accepted": True})

    server = _RunningServer(monkeypatch, callback)
    try:
        client = WriterClient()
        rejected = client.action("increment_download_count", {"post_id": 1})
        accepted = client.action("increment_download_count", {"post_id": 1})
        assert rejected is not None and not rejected.success
        assert accepted is not None and accepted.success
        assert server.requests[0]["command_id"] != server.requests[1]["command_id"]
        assert calls == 2
    finally:
        server.close()


def test_idle_dequeue_is_falsy_so_job_loop_stays_quiet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _RunningServer(monkeypatch, lambda request: _reply(request, result=None))
    try:
        result = WriterClient().action("dequeue_job", {"run_id": "run"}, wait=True)
        assert result is not None and result.success
        assert result.data is None
    finally:
        server.close()


def test_concurrent_requests_keep_reply_correlation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _RunningServer(
        monkeypatch, lambda request: _reply(request, result=request["command_id"])
    )
    try:

        def send(index: int) -> str:
            result = WriterClient().action("increment_download_count", {"index": index})
            assert result is not None and result.success
            assert result.data == result.command_id
            return result.command_id

        with ThreadPoolExecutor(max_workers=12) as pool:
            ids = list(pool.map(send, range(24)))
        assert len(set(ids)) == 24
        assert {request["command_id"] for request in server.requests} == set(ids)
    finally:
        server.close()


def test_rust_transaction_serialization_and_response_adaptation() -> None:
    command = WriteCommand(
        id="outer",
        type=WriteCommandType.TRANSACTION,
        model=None,
        data={
            "commands": [
                {
                    "id": "action-1",
                    "type": "action",
                    "data": {"action": "clear_all_jobs", "params": {}},
                },
                {
                    "id": "update-1",
                    "type": "update",
                    "model": "Feed",
                    "data": {"id": 4, "chapter_filter_strings": "changed"},
                },
            ]
        },
    )
    wire = WriterClient._rust_command_payload(command, wait=True)
    assert wire["commands"] == [
        {
            "command_id": "action-1",
            "operation": "action",
            "action": "clear_all_jobs",
            "params": {},
        },
        {
            "command_id": "update-1",
            "operation": "update",
            "model": "Feed",
            "id": 4,
            "data": {"chapter_filter_strings": "changed"},
        },
    ]
    result = WriterClient._adapt_rust_response(
        command,
        True,
        200,
        {
            "version": 1,
            "state": "completed",
            "command_id": "outer",
            "success": True,
            "result": {
                "results": [
                    {
                        "command_id": "action-1",
                        "success": True,
                        "data": 3,
                        "error": None,
                    },
                    {
                        "command_id": "update-1",
                        "success": True,
                        "data": None,
                        "error": None,
                    },
                ]
            },
        },
    )
    assert result is not None
    assert result.data == {
        "results": [
            {
                "command_id": "action-1",
                "success": True,
                "data": 3,
                "error": None,
            },
            {"command_id": "update-1", "success": True, "data": None, "error": None},
        ]
    }


def test_model_datetimes_use_sqlalchemy_sqlite_storage_format() -> None:
    from datetime import UTC, datetime, timedelta, timezone

    from app.writer.protocol import WriteCommand, WriteCommandType

    update = WriteCommand(
        id="update-1",
        type=WriteCommandType.UPDATE,
        model="Post",
        data={
            "id": 7,
            "refined_ad_boundaries": [{"refined_start": 1.5}],
            "refined_ad_boundaries_updated_at": datetime(2026, 9, 30, 7, 44, 58),
            "other_at": datetime(
                2026, 9, 30, 2, 0, 0, 5, tzinfo=timezone(timedelta(hours=-5))
            ),
        },
    )
    payload = WriterClient._rust_command_payload(update, wait=True)
    assert payload["id"] == 7
    assert payload["data"] == {
        "refined_ad_boundaries": [{"refined_start": 1.5}],
        "refined_ad_boundaries_updated_at": "2026-09-30 07:44:58.000000",
        "other_at": "2026-09-30 07:00:00.000005",
    }
    WriterClient._encode_rust_command(update, wait=True)

    transaction = WriteCommand(
        id="tx-1",
        type=WriteCommandType.TRANSACTION,
        model=None,
        data={
            "commands": [
                {
                    "type": "update",
                    "model": "Post",
                    "data": {"id": 7, "updated_at": datetime(2026, 1, 2, tzinfo=UTC)},
                }
            ]
        },
    )
    nested = WriterClient._rust_command_payload(transaction, wait=True)
    assert nested["commands"][0]["data"] == {"updated_at": "2026-01-02 00:00:00.000000"}
