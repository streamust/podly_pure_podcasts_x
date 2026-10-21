import base64
import http.client
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from queue import Empty, Queue
from typing import Any, cast

from flask import current_app

from app.writer.backend import WriterBackend, selected_writer_backend
from app.writer.protocol import WriteCommand, WriteCommandType, WriteResult

logger = logging.getLogger("global_logger")

DEFAULT_SUBMIT_TIMEOUT_SECONDS = 30
RUST_WRITER_HOST = "127.0.0.1"
RUST_WRITER_PORT = 50_001
RUST_WRITER_MAX_CONNECTIONS = 16
RUST_WRITER_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
_RUST_CONNECTIONS = threading.BoundedSemaphore(RUST_WRITER_MAX_CONNECTIONS)


class WriterTransportError(ConnectionError):
    """The Rust writer could not be reached or rejected the RPC envelope."""


class WriterOutcomeUnknownError(TimeoutError):
    """A write may have been admitted, but its final outcome was not received."""

    def __init__(self, command_id: str, message: str) -> None:
        super().__init__(f"writer outcome unknown for command {command_id}: {message}")
        self.command_id = command_id


def _default_submit_timeout() -> int:
    """Seconds to wait for a writer reply, overridable per deployment.

    The writer executes one command at a time, so this bounds how long a
    caller can be blocked behind somebody else's slow command as well as by
    its own. Raise it if a deployment has an unavoidably slow action; the
    right fix for a slow action is normally to make it fast.
    """
    raw = os.environ.get("PODLY_WRITER_TIMEOUT_SECONDS")
    if not raw:
        return DEFAULT_SUBMIT_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Ignoring non-integer PODLY_WRITER_TIMEOUT_SECONDS=%r; using %s",
            raw,
            DEFAULT_SUBMIT_TIMEOUT_SECONDS,
        )
        return DEFAULT_SUBMIT_TIMEOUT_SECONDS
    if value <= 0:
        logger.warning(
            "Ignoring non-positive PODLY_WRITER_TIMEOUT_SECONDS=%r; using %s",
            raw,
            DEFAULT_SUBMIT_TIMEOUT_SECONDS,
        )
        return DEFAULT_SUBMIT_TIMEOUT_SECONDS
    return value


def _model_column_values(data: dict[str, Any]) -> dict[str, Any]:
    """Encode datetimes the way SQLAlchemy stores them in SQLite.

    The Rust writer stores DateTime columns as the text it receives, and
    SQLAlchemy only parses back "YYYY-MM-DD HH:MM:SS.ffffff".
    """
    return {
        key: (
            (
                value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value
            ).strftime("%Y-%m-%d %H:%M:%S.%f")
            if isinstance(value, datetime)
            else value
        )
        for key, value in data.items()
    }


class WriterClient:
    def __init__(self) -> None:
        self.manager: Any = None
        self.queue: Queue[Any] | None = None
        # Per-thread reply queue. Reusing a single Manager().Queue() per worker
        # thread avoids leaking server-side Queue objects in the writer process
        # (each manager.Queue() call allocates a new Queue in the writer's
        # referent table that is only freed when the client-side proxy is
        # garbage-collected — a path that's fragile under load).
        self._tls = threading.local()

    def connect(self) -> None:
        if selected_writer_backend() is WriterBackend.RUST:
            self._check_rust_readiness(_default_submit_timeout())
            return
        if not self.manager:
            from app.ipc import make_client_manager

            manager = make_client_manager()
            self.manager = manager
            self.queue = manager.get_command_queue()

    @staticmethod
    def _check_rust_readiness(timeout: int) -> None:
        connection = http.client.HTTPConnection(
            RUST_WRITER_HOST, RUST_WRITER_PORT, timeout=timeout
        )
        try:
            connection.request("GET", "/v1/ready")
            response = connection.getresponse()
            payload = response.read(4096)
            if response.status != 200:
                raise WriterTransportError(
                    f"Rust writer readiness failed with HTTP {response.status}"
                )
            ready = json.loads(payload)
            if (
                ready.get("backend") != "rust"
                or ready.get("version") != 1
                or ready.get("ready") is not True
            ):
                raise WriterTransportError(
                    "writer endpoint is not ready for protocol version 1"
                )
        except WriterTransportError:
            raise
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise WriterTransportError("Rust writer readiness check failed") from exc
        finally:
            connection.close()

    def _should_use_local_fallback(self) -> bool:
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return True
        if os.environ.get("PODLY_WRITER_LOCAL_FALLBACK") == "1":
            return True
        try:
            return bool(getattr(current_app, "testing", False))
        except Exception:  # noqa: BLE001
            return False

    def _local_execute(self, cmd: WriteCommand) -> WriteResult:
        # Import locally to avoid cyclic dependencies
        from app import models
        from app.extensions import db

        model_map: dict[str, Any] = {}
        for name, obj in vars(models).items():
            if isinstance(obj, type) and issubclass(obj, db.Model) and obj != db.Model:
                model_map[name] = obj

        try:
            if cmd.type == WriteCommandType.TRANSACTION:
                return self._local_execute_transaction(cmd, model_map)

            result = self._local_execute_single(cmd, model_map)
            if result.success:
                db.session.commit()
            else:
                db.session.rollback()
            return result
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            return WriteResult(cmd.id, False, error=str(exc))

    def _local_execute_single(
        self, cmd: WriteCommand, model_map: dict[str, Any]
    ) -> WriteResult:
        if cmd.type == WriteCommandType.ACTION:
            return self._local_execute_action(cmd)
        return self._local_execute_model(cmd, model_map)

    def _local_execute_transaction(
        self, cmd: WriteCommand, model_map: dict[str, Any]
    ) -> WriteResult:
        # Import locally to avoid cyclic dependencies
        from app.extensions import db

        results = []
        for sub_cmd_data in cmd.data.get("commands", []):
            if isinstance(sub_cmd_data, dict):
                sub_cmd = WriteCommand(
                    id=sub_cmd_data.get("id", "sub"),
                    type=WriteCommandType(sub_cmd_data.get("type")),
                    model=sub_cmd_data.get("model"),
                    data=sub_cmd_data.get("data", {}),
                )
            else:
                sub_cmd = sub_cmd_data

            res = self._local_execute_single(sub_cmd, model_map)
            if not res.success:
                db.session.rollback()
                return WriteResult(
                    cmd.id,
                    False,
                    error=f"Transaction failed at {sub_cmd.id}: {res.error}",
                )
            results.append(res)

        db.session.commit()
        return WriteResult(cmd.id, True, data={"results": [r.data for r in results]})

    def _local_execute_action(self, cmd: WriteCommand) -> WriteResult:
        # Import locally to avoid cyclic dependencies
        from app.writer import actions as writer_actions

        action_name = cmd.data.get("action")
        func_name = f"{action_name}_action" if action_name else None
        func_obj = getattr(writer_actions, func_name, None) if func_name else None
        if func_obj is None or not callable(func_obj):
            return WriteResult(cmd.id, False, error=f"Unknown action: {action_name}")

        func = cast(Callable[[dict[str, Any]], Any], func_obj)
        result = func(cmd.data.get("params", {}))
        return WriteResult(cmd.id, True, data=result)

    def _local_execute_model(
        self, cmd: WriteCommand, model_map: dict[str, Any]
    ) -> WriteResult:
        # Import locally to avoid cyclic dependencies
        from app.extensions import db
        from app.writer.model_ops import execute_model_command

        if not cmd.model or cmd.model not in model_map:
            return WriteResult(cmd.id, False, error=f"Unknown model: {cmd.model}")

        model_cls = model_map[cmd.model]
        return execute_model_command(
            cmd=cmd, model_cls=model_cls, db_session=db.session
        )

    def _get_thread_reply_queue(self) -> Queue[Any]:
        if not self.manager:
            raise RuntimeError("Manager not connected")
        reply_q = cast("Queue[Any] | None", getattr(self._tls, "reply_q", None))
        if reply_q is None:
            reply_q = cast(Queue[Any], self.manager.Queue())
            self._tls.reply_q = reply_q
        return reply_q

    @staticmethod
    def _drain_queue(reply_q: Queue[Any]) -> int:
        drained = 0
        while True:
            try:
                reply_q.get_nowait()
            except Empty:
                break
            drained += 1
        return drained

    def submit(
        self, cmd: WriteCommand, wait: bool = False, timeout: int | None = None
    ) -> WriteResult | None:
        if timeout is None:
            timeout = _default_submit_timeout()
        if selected_writer_backend() is WriterBackend.RUST:
            return self._submit_rust(cmd, wait=wait, timeout=timeout)
        reply_q: Queue[Any] | None = None
        if not self.queue:
            try:
                self.connect()
            except Exception:
                if self._should_use_local_fallback():
                    result = self._local_execute(cmd)
                    return result if wait else None
                raise

        if wait:
            reply_q = self._get_thread_reply_queue()
            stale = self._drain_queue(reply_q)
            if stale:
                logger.warning(
                    "WriterClient: drained %s stale reply(s) before cmd id=%s",
                    stale,
                    cmd.id,
                )
            cmd.reply_queue = reply_q

        if self.queue:
            cmd.enqueued_monotonic_ns = time.monotonic_ns()
            self.queue.put(cmd)

        if wait:
            if reply_q is None:
                raise RuntimeError("Reply queue was not initialized")
            return self._await_reply(reply_q, cmd, timeout)
        return None

    def _submit_rust(
        self, cmd: WriteCommand, *, wait: bool, timeout: int
    ) -> WriteResult | None:
        if timeout <= 0:
            raise ValueError("writer timeout must be positive")
        deadline = time.monotonic() + timeout
        encoded = self._encode_rust_command(cmd, wait)
        auth_key = os.environ.get("PODLY_IPC_AUTHKEY", "").encode("utf-8")
        if not auth_key:
            raise WriterTransportError(
                "PODLY_IPC_AUTHKEY is required for Rust writer mode"
            )
        status, response_payload = self._post_rust_command(
            cmd.id, encoded, auth_key, deadline
        )
        return self._adapt_rust_response(cmd, wait, status, response_payload)

    @staticmethod
    def _encode_rust_command(cmd: WriteCommand, wait: bool) -> bytes:
        try:
            payload = WriterClient._rust_command_payload(cmd, wait)
            encoded = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        except (TypeError, ValueError, KeyError) as exc:
            raise ValueError(
                "writer command cannot be encoded as protocol JSON"
            ) from exc
        if len(encoded) > 16 * 1024 * 1024:
            raise WriterTransportError(
                "Rust writer command exceeds the request size limit"
            )
        return encoded

    @staticmethod
    def _post_rust_command(
        command_id: str, encoded: bytes, auth_key: bytes, deadline: float
    ) -> tuple[int, Any]:
        connection = WriterClient._open_rust_connection(deadline)
        token = base64.urlsafe_b64encode(auth_key).decode("ascii").rstrip("=")
        try:
            connection.request(
                "POST",
                "/v1/commands",
                body=encoded,
                headers={
                    "Authorization": f"PodlyWriter {token}",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(encoded)),
                    "Connection": "close",
                },
            )
            return WriterClient._read_rust_response(connection, command_id, deadline)
        except WriterTransportError, WriterOutcomeUnknownError, ValueError:
            raise
        except (TimeoutError, OSError, http.client.HTTPException) as exc:
            raise WriterOutcomeUnknownError(
                command_id, "connection ended after request send"
            ) from exc
        finally:
            connection.close()
            _RUST_CONNECTIONS.release()

    @staticmethod
    def _open_rust_connection(deadline: float) -> http.client.HTTPConnection:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not _RUST_CONNECTIONS.acquire(timeout=max(remaining, 0)):
            raise WriterTransportError("Rust writer connection capacity is exhausted")

        try:
            connection = http.client.HTTPConnection(
                RUST_WRITER_HOST,
                RUST_WRITER_PORT,
                timeout=max(deadline - time.monotonic(), 0.001),
            )
            # Connecting is a definite pre-admission operation. Any failure once
            # request bytes may have been sent has an unknown write outcome.
            connection.connect()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WriterTransportError(
                    "Rust writer request deadline expired before send"
                )
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            return connection
        except (OSError, http.client.HTTPException) as exc:
            if "connection" in locals():
                connection.close()
            _RUST_CONNECTIONS.release()
            raise WriterTransportError("Rust writer connection failed") from exc
        except BaseException:
            if "connection" in locals():
                connection.close()
            _RUST_CONNECTIONS.release()
            raise

    @staticmethod
    def _read_rust_response(
        connection: http.client.HTTPConnection, command_id: str, deadline: float
    ) -> tuple[int, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WriterOutcomeUnknownError(
                command_id, "request deadline expired after send"
            )
        if connection.sock is not None:
            connection.sock.settimeout(remaining)
        response = connection.getresponse()
        response_bytes = response.read(RUST_WRITER_MAX_RESPONSE_BYTES + 1)
        if len(response_bytes) > RUST_WRITER_MAX_RESPONSE_BYTES:
            raise WriterOutcomeUnknownError(
                command_id, "writer response exceeded size limit"
            )
        try:
            response_payload = json.loads(response_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WriterOutcomeUnknownError(
                command_id, "writer returned invalid JSON"
            ) from exc
        return response.status, response_payload

    @staticmethod
    def _rust_command_payload(cmd: WriteCommand, wait: bool) -> dict[str, Any]:
        common = {"version": 1, "command_id": cmd.id, "wait": wait}
        if cmd.type is WriteCommandType.ACTION:
            return {
                **common,
                "operation": "action",
                "action": cmd.data["action"],
                "params": cmd.data.get("params", {}),
            }
        if cmd.type is WriteCommandType.CREATE:
            return {
                **common,
                "operation": "create",
                "model": cmd.model,
                "data": _model_column_values(cmd.data),
            }
        if cmd.type is WriteCommandType.UPDATE:
            values = _model_column_values(cmd.data)
            record_id = values.pop("id", None)
            return {
                **common,
                "operation": "update",
                "model": cmd.model,
                "id": record_id,
                "data": values,
            }
        if cmd.type is WriteCommandType.DELETE:
            return {
                **common,
                "operation": "delete",
                "model": cmd.model,
                "id": cmd.data.get("id"),
            }
        if cmd.type is WriteCommandType.TRANSACTION:
            commands = cmd.data.get("commands", [])
            return {
                **common,
                "operation": "transaction",
                "commands": [
                    WriterClient._rust_transaction_command(item) for item in commands
                ],
            }
        raise ValueError(f"unsupported writer command type: {cmd.type}")

    @staticmethod
    def _rust_transaction_command(command: Any) -> dict[str, Any]:
        if isinstance(command, WriteCommand):
            payload = WriterClient._rust_command_payload(command, wait=False)
            return {
                key: value
                for key, value in payload.items()
                if key not in {"version", "wait"}
            }
        if not isinstance(command, dict):
            raise ValueError(
                "transaction commands must be writer commands or dictionaries"
            )
        command_type = WriteCommandType(command["type"])
        nested = WriteCommand(
            id=str(command.get("id", "sub")),
            type=command_type,
            model=command.get("model"),
            data=command.get("data", {}),
        )
        payload = WriterClient._rust_command_payload(nested, wait=False)
        return {
            key: value
            for key, value in payload.items()
            if key not in {"version", "wait"}
        }

    @staticmethod
    def _adapt_rust_response(
        cmd: WriteCommand, wait: bool, status: int, payload: Any
    ) -> WriteResult | None:
        WriterClient._validate_response_envelope(cmd.id, payload)
        response_id = payload.get("command_id")
        state = payload.get("state")
        if state == "rejected":
            return WriterClient._adapt_rejection(cmd, wait, response_id, payload)
        if state == "unknown":
            raise WriterOutcomeUnknownError(
                cmd.id, "writer reported an unknown outcome"
            )
        if state == "accepted":
            return WriterClient._adapt_admission(
                cmd, wait, status, response_id, payload
            )
        if state != "completed":
            raise WriterOutcomeUnknownError(cmd.id, "writer response state is invalid")
        return WriterClient._adapt_completion(cmd, wait, status, response_id, payload)

    @staticmethod
    def _validate_response_envelope(command_id: str, payload: Any) -> None:
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise WriterOutcomeUnknownError(
                command_id, "writer response protocol mismatch"
            )

    @staticmethod
    def _adapt_rejection(
        cmd: WriteCommand, wait: bool, response_id: Any, payload: dict[str, Any]
    ) -> WriteResult | None:
        if response_id not in (None, cmd.id) or payload.get("admitted") is not False:
            raise WriterOutcomeUnknownError(cmd.id, "invalid rejection response")
        error = payload.get("error") or {}
        code = error.get("code", "writer_rejected")
        message = error.get("message", "writer rejected command")
        if not wait:
            raise WriterTransportError(
                f"Rust writer rejected command: {code}: {message}"
            )
        return WriteResult(cmd.id, False, error=f"{code}: {message}")

    @staticmethod
    def _adapt_admission(
        cmd: WriteCommand,
        wait: bool,
        status: int,
        response_id: Any,
        payload: dict[str, Any],
    ) -> None:
        if (
            wait
            or response_id != cmd.id
            or payload.get("admitted") is not True
            or status != 202
        ):
            raise WriterOutcomeUnknownError(cmd.id, "invalid admission response")

    @staticmethod
    def _adapt_completion(
        cmd: WriteCommand,
        wait: bool,
        status: int,
        response_id: Any,
        payload: dict[str, Any],
    ) -> WriteResult | None:
        if response_id != cmd.id:
            raise WriterOutcomeUnknownError(
                cmd.id, "writer response command ID mismatch"
            )
        if status != 200:
            raise WriterOutcomeUnknownError(cmd.id, "unexpected completion HTTP status")
        if not wait:
            raise WriterOutcomeUnknownError(
                cmd.id, "unexpected completion for asynchronous command"
            )
        if not payload.get("success", False):
            return WriterClient._adapt_failed_completion(cmd, payload)

        result = payload.get("result")
        if cmd.type is WriteCommandType.TRANSACTION:
            if not isinstance(result, dict) or not isinstance(
                result.get("results"), list
            ):
                raise WriterOutcomeUnknownError(cmd.id, "invalid transaction response")
            transaction_results = []
            for index, item in enumerate(result["results"]):
                if not isinstance(item, dict):
                    raise WriterOutcomeUnknownError(
                        cmd.id, "invalid transaction result"
                    )
                entry = cast(dict[str, Any], item)
                if entry.get("success") is not True:
                    raise WriterOutcomeUnknownError(
                        cmd.id, "invalid transaction result"
                    )
                transaction_results.append(
                    {
                        "command_id": entry.get("command_id"),
                        "success": True,
                        "data": WriterClient._legacy_result_data(
                            entry.get("data"),
                            WriterClient._transaction_operation_type(cmd, index),
                        ),
                        "error": None,
                    }
                )
            data = {"results": transaction_results}
        else:
            data = WriterClient._legacy_result_data(result, cmd.type)
        return WriteResult(cmd.id, True, data=data)

    @staticmethod
    def _adapt_failed_completion(
        cmd: WriteCommand, payload: dict[str, Any]
    ) -> WriteResult:
        error = payload.get("error") or {}
        if error.get("outcome") == "unknown":
            raise WriterOutcomeUnknownError(
                cmd.id, error.get("message", "unknown outcome")
            )
        code = error.get("code", "writer_failed")
        message = error.get("message", "writer command failed")
        return WriteResult(cmd.id, False, error=f"{code}: {message}")

    @staticmethod
    def _transaction_operation_type(
        cmd: WriteCommand, index: int
    ) -> WriteCommandType | None:
        commands = cmd.data.get("commands", [])
        if index >= len(commands):
            return None
        item = commands[index]
        if isinstance(item, WriteCommand):
            return item.type
        if isinstance(item, dict):
            try:
                return WriteCommandType(item.get("type"))
            except ValueError:
                return None
        return None

    @staticmethod
    def _legacy_result_data(value: Any, command_type: WriteCommandType | None) -> Any:
        # The Python writer service returns action values unwrapped: callers
        # test `result.data` for truthiness (idle dequeue is None) and read
        # scalar counts directly.
        if command_type is WriteCommandType.CREATE:
            return value if isinstance(value, dict) else None
        if command_type in (WriteCommandType.UPDATE, WriteCommandType.DELETE):
            return None
        return value

    @staticmethod
    def _await_reply(
        reply_q: Queue[Any], cmd: WriteCommand, timeout: int
    ) -> WriteResult:
        """Wait for this command's reply, discarding replies to abandoned ones.

        The reply queue is thread-local and this thread's submits are
        serialized, so any reply that is not ours belongs to an earlier
        command this thread already gave up on -- the writer answered it after
        we stopped waiting. Those must be *dropped*, not returned and not
        counted against our own wait: returning one hands the caller another
        command's result, and consuming our slot for it leaves the queue
        permanently one reply behind, so a single slow command used to poison
        every subsequent write on the thread.
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Writer service did not respond")
            try:
                result = cast(WriteResult, reply_q.get(timeout=remaining))
            except Empty as exc:
                raise TimeoutError("Writer service did not respond") from exc

            result_cmd_id = getattr(result, "command_id", None)
            if result_cmd_id == cmd.id:
                return result

            logger.warning(
                "WriterClient: discarding late reply for abandoned command "
                "id=%s while waiting for id=%s",
                result_cmd_id,
                cmd.id,
            )

    def create(
        self, model: str, data: dict[str, Any], wait: bool = True
    ) -> WriteResult | None:
        cmd = WriteCommand(
            id=str(uuid.uuid4()), type=WriteCommandType.CREATE, model=model, data=data
        )
        return self.submit(cmd, wait=wait)

    def update(
        self, model: str, pk: Any, data: dict[str, Any], wait: bool = True
    ) -> WriteResult | None:
        data["id"] = pk
        cmd = WriteCommand(
            id=str(uuid.uuid4()), type=WriteCommandType.UPDATE, model=model, data=data
        )
        return self.submit(cmd, wait=wait)

    def delete(self, model: str, pk: Any, wait: bool = True) -> WriteResult | None:
        cmd = WriteCommand(
            id=str(uuid.uuid4()),
            type=WriteCommandType.DELETE,
            model=model,
            data={"id": pk},
        )
        return self.submit(cmd, wait=wait)

    def action(
        self, action_name: str, params: dict[str, Any], wait: bool = True
    ) -> WriteResult | None:
        cmd = WriteCommand(
            id=str(uuid.uuid4()),
            type=WriteCommandType.ACTION,
            model=None,
            data={"action": action_name, "params": params},
        )
        return self.submit(cmd, wait=wait)


# Singleton instance
writer_client = WriterClient()
