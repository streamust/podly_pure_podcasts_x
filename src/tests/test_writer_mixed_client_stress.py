"""Concurrent production-action traffic against one isolated Rust writer."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from tests.test_writer_differential_parity import (
    AUTH_KEY,
    REPO_ROOT,
    RustWriterParityServer,
    _free_port,
    _rust_writer_command,
)
from tests.writer_parity_fixtures import WriterParityPair

pytest_plugins = ("tests.writer_parity_fixtures",)


def _scalar(db_path: Path, sql: str, parameter: int) -> int:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(sql, (parameter,)).fetchone()
    assert row is not None
    return int(row[0])


class ConfiguredStressServer:
    """Isolated writer with deliberately small capacity/deadline test limits."""

    def __init__(
        self,
        db_path: Path,
        tmp_path: Path,
        *,
        queue_entries: int = 128,
        request_deadline_ms: int = 30_000,
    ) -> None:
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.log_path = tmp_path / f"rust-writer-stress-{self.port}.log"
        env = os.environ.copy()
        env["PODLY_IPC_AUTHKEY"] = AUTH_KEY
        self._log = self.log_path.open("wb")
        self.process = subprocess.Popen(
            [
                *_rust_writer_command(),
                "--db",
                str(db_path),
                "--port",
                str(self.port),
                "--enable-test-actions",
                "--test-queue-entries",
                str(queue_entries),
                "--test-request-deadline-ms",
                str(request_deadline_ms),
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=self._log,
        )
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise AssertionError(
                        f"writer exited during startup: {self.log_path.read_text(errors='replace')}"
                    )
                try:
                    readiness = self.request("/v1/ready")
                    if readiness.get("ready") is True:
                        return
                except OSError, urllib.error.URLError, json.JSONDecodeError:
                    time.sleep(0.02)
            raise AssertionError("writer readiness timed out")
        except BaseException:
            self.close()
            raise

    def request(
        self,
        endpoint: str,
        payload: dict | None = None,
        *,
        timeout: float = 10,
    ) -> dict:
        headers = {
            "Authorization": "PodlyWriter "
            + base64.urlsafe_b64encode(AUTH_KEY.encode()).decode().rstrip("="),
        }
        data = None
        method = "GET"
        if payload is not None:
            method = "POST"
            data = json.dumps(payload, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + endpoint, data=data, headers=headers, method=method
        )
        try:
            response = urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return json.loads(response.read())

    def execute(self, operation: dict, *, command_id: str | None = None) -> dict:
        return self.request(
            "/v1/commands",
            {
                "version": 1,
                "command_id": command_id or f"stress-{time.time_ns()}",
                "operation": operation["operation"],
                "wait": True,
                **{
                    key: value for key, value in operation.items() if key != "operation"
                },
            },
        )

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if not self._log.closed:
            self._log.close()


def _rss_bytes(pid: int) -> int:
    status = Path(f"/proc/{pid}/status").read_text()
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) * 1024
    raise AssertionError("writer process has no VmRSS measurement")


def _download_count_operation(post_id: int) -> dict:
    return {
        "operation": "action",
        "action": "increment_download_count",
        "params": {"post_id": post_id},
    }


def _segments_operation(post_id: int, base: int, count: int = 1) -> dict:
    return {
        "operation": "action",
        "action": "insert_transcript_segments",
        "params": {
            "post_id": post_id,
            "segments": [
                {
                    "sequence_num": base + offset,
                    "start_time": float(base + offset),
                    "end_time": float(base + offset) + 0.5,
                    "text": f"synthetic stress transcript segment {base + offset} "
                    + ("x" * 512),
                }
                for offset in range(count)
            ],
        },
    }


def test_eight_mixed_clients_preserve_replies_and_database_effects(
    writer_parity_pair: WriterParityPair, tmp_path: Path
) -> None:
    pair = writer_parity_pair
    post_id = pair.manifest.post_ids[0]
    before_downloads = _scalar(
        pair.rust.db_path, "SELECT download_count FROM post WHERE id=?", post_id
    )
    before_segments = _scalar(
        pair.rust.db_path,
        "SELECT COUNT(*) FROM transcript_segment WHERE post_id=?",
        post_id,
    )
    server = RustWriterParityServer(pair.rust.db_path, tmp_path)
    try:

        def submit(index: int) -> dict:
            if index % 2 == 0:
                operation = {
                    "operation": "action",
                    "action": "increment_download_count",
                    "params": {"post_id": post_id},
                }
            else:
                operation = {
                    "operation": "action",
                    "action": "insert_transcript_segments",
                    "params": {
                        "post_id": post_id,
                        "segments": [
                            {
                                "sequence_num": 1000 + index,
                                "start_time": float(index),
                                "end_time": float(index) + 0.5,
                                "text": f"synthetic concurrent segment {index}",
                            }
                        ],
                    },
                }
            return server.execute_outcome(operation)

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(submit, range(32)))
        assert len({outcome["command_id"] for outcome in outcomes}) == 32
        assert all(outcome["success"] is True for outcome in outcomes)
        assert (
            _scalar(
                pair.rust.db_path, "SELECT download_count FROM post WHERE id=?", post_id
            )
            == before_downloads + 16
        )
        assert (
            _scalar(
                pair.rust.db_path,
                "SELECT COUNT(*) FROM transcript_segment WHERE post_id=?",
                post_id,
            )
            == before_segments + 16
        )
    finally:
        server.close()


def test_mixed_processing_burst_keeps_writer_memory_bounded(
    writer_parity_pair: WriterParityPair, tmp_path: Path
) -> None:
    pair = writer_parity_pair
    post_id = pair.manifest.post_ids[0]
    before = _scalar(
        pair.rust.db_path,
        "SELECT COUNT(*) FROM transcript_segment WHERE post_id=?",
        post_id,
    )
    server = ConfiguredStressServer(pair.rust.db_path, tmp_path)
    try:
        baseline_rss = _rss_bytes(server.process.pid)

        def submit(batch: int) -> dict:
            return server.execute(
                _segments_operation(post_id, 20_000 + batch * 128, 128)
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(submit, range(32)))
        assert all(
            response["state"] == "completed" and response["success"] is True
            for response in responses
        )
        assert len({response["command_id"] for response in responses}) == 32
        assert _scalar(
            pair.rust.db_path,
            "SELECT COUNT(*) FROM transcript_segment WHERE post_id=?",
            post_id,
        ) == before + (32 * 128)
        # Responses and command bodies must be released after work completes.
        # Permit allocator retention while catching request/result accumulation.
        time.sleep(0.2)
        assert _rss_bytes(server.process.pid) - baseline_rss < 48 * 1024 * 1024
    finally:
        server.close()


def test_writer_capacity_rejects_concurrent_request_without_losing_active_reply(
    writer_parity_pair: WriterParityPair, tmp_path: Path
) -> None:
    server = ConfiguredStressServer(
        writer_parity_pair.rust.db_path,
        tmp_path,
        queue_entries=1,
        request_deadline_ms=5_000,
    )
    try:
        slow = {
            "operation": "action",
            "action": "__test_sleep",
            "params": {"milliseconds": 500},
        }
        with ThreadPoolExecutor(max_workers=2) as pool:
            active = pool.submit(server.execute, slow, command_id="active-slow-command")
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                readiness = server.request("/v1/ready")
                if readiness["queue_capacity"]["entries_available"] == 0:
                    break
                time.sleep(0.01)
            else:
                raise AssertionError("slow command was not admitted")

            saturated = server.execute(
                {"operation": "action", "action": "__test_noop", "params": {}},
                command_id="rejected-saturated-command",
            )
            accepted = active.result(timeout=3)

        assert saturated["state"] == "rejected"
        assert saturated["error"]["code"] == "capacity_exhausted"
        assert accepted["state"] == "completed"
        assert accepted["command_id"] == "active-slow-command"
        assert accepted["success"] is True
    finally:
        server.close()


def test_sqlite_lock_timeout_is_unknown_then_commits_once_after_unlock(
    writer_parity_pair: WriterParityPair, tmp_path: Path
) -> None:
    pair = writer_parity_pair
    post_id = pair.manifest.post_ids[0]
    before = _scalar(
        pair.rust.db_path, "SELECT download_count FROM post WHERE id=?", post_id
    )
    server = ConfiguredStressServer(
        pair.rust.db_path, tmp_path, request_deadline_ms=150
    )
    blocker = sqlite3.connect(pair.rust.db_path, timeout=2, isolation_level=None)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        with ThreadPoolExecutor(max_workers=1) as pool:
            response_future = pool.submit(
                server.execute,
                _download_count_operation(post_id),
                command_id="sqlite-lock-timeout",
            )
            timed_out = response_future.result(timeout=5)
        assert timed_out["state"] == "unknown"
        assert timed_out["command_id"] == "sqlite-lock-timeout"
        assert timed_out["error"]["outcome"] == "unknown"
        blocker.execute("ROLLBACK")

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = _scalar(
                pair.rust.db_path, "SELECT download_count FROM post WHERE id=?", post_id
            )
            if current == before + 1:
                break
            time.sleep(0.02)
        assert current == before + 1
        time.sleep(0.1)
        assert (
            _scalar(
                pair.rust.db_path,
                "SELECT download_count FROM post WHERE id=?",
                post_id,
            )
            == before + 1
        ), "the unknown-outcome command was replayed"
    finally:
        if blocker.in_transaction:
            blocker.execute("ROLLBACK")
        blocker.close()
        server.close()


def test_transaction_failure_rolls_back_prior_real_write_and_restart_accepts_new_work(
    writer_parity_pair: WriterParityPair, tmp_path: Path
) -> None:
    pair = writer_parity_pair
    post_id = pair.manifest.post_ids[0]
    before = _scalar(
        pair.rust.db_path, "SELECT download_count FROM post WHERE id=?", post_id
    )
    server = ConfiguredStressServer(pair.rust.db_path, tmp_path)
    try:
        failed = server.execute(
            {
                "operation": "transaction",
                "commands": [
                    {
                        "command_id": "increment-before-failure",
                        **_download_count_operation(post_id),
                    },
                    {
                        "command_id": "invalid-segment-after-increment",
                        "operation": "action",
                        "action": "insert_transcript_segments",
                        "params": {
                            "post_id": post_id,
                            "segments": [
                                {
                                    "sequence_num": 90_000,
                                    "start_time": {},
                                    "end_time": 90_000.5,
                                    "text": "synthetic rollback fixture",
                                }
                            ],
                        },
                    },
                ],
            },
            command_id="atomic-mixed-failure",
        )
        assert failed["state"] == "completed"
        assert failed["success"] is False
        assert (
            _scalar(
                pair.rust.db_path, "SELECT download_count FROM post WHERE id=?", post_id
            )
            == before
        )
    finally:
        server.close()

    restarted = ConfiguredStressServer(pair.rust.db_path, tmp_path)
    try:
        result = restarted.execute(
            _download_count_operation(post_id), command_id="post-restart-increment"
        )
        assert result["state"] == "completed" and result["success"] is True
        assert (
            _scalar(
                pair.rust.db_path, "SELECT download_count FROM post WHERE id=?", post_id
            )
            == before + 1
        )
    finally:
        restarted.close()
