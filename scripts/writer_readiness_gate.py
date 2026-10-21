#!/usr/bin/env python3
"""Test-only protocol gate for the isolated writer lifecycle container check."""

from __future__ import annotations

import errno
import http.client
import os
import signal
import socket
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROXY_PORT = 50001
REAL_PORT = 50002
REAL_WRITER = Path(__file__).with_name("podly_writer.real")
MARKER = Path(os.environ.get("PODLY_READINESS_GATE_MARKER", "/tmp/podly-ready"))
STARTED = Path(f"{MARKER}.started")


class ReadinessGateHandler(BaseHTTPRequestHandler):
    """Return unready until opened, then proxy requests to the real writer."""

    def do_GET(self) -> None:
        self._forward()

    def do_POST(self) -> None:
        self._forward()

    def do_PUT(self) -> None:
        self._forward()

    def do_DELETE(self) -> None:
        self._forward()

    def do_HEAD(self) -> None:
        self._forward()

    def _forward(self) -> None:
        if not MARKER.exists():
            body = b'{"ready":false,"reason":"test readiness gate is closed"}'
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return

        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else None
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in {"connection", "host", "transfer-encoding"}
        }
        connection = http.client.HTTPConnection("127.0.0.1", REAL_PORT, timeout=10)
        try:
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read()
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() not in {
                    "connection",
                    "content-length",
                    "date",
                    "server",
                    "transfer-encoding",
                }:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
        except OSError:
            self.send_error(502, "real Rust writer is unavailable")
        finally:
            connection.close()

    def log_message(self, _format: str, *_args: object) -> None:
        return


def probe() -> int:
    """Use the unmodified binary probe against the launcher's normal port."""
    return subprocess.run([str(REAL_WRITER), "--probe"], check=False).returncode


def assert_real_ready() -> None:
    """Waiter used by the harness to establish readiness behind the gate."""
    result = subprocess.run(
        [str(REAL_WRITER), "--probe", "--port", str(REAL_PORT)],
        check=False,
        timeout=3,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"real Rust writer probe failed with status {result.returncode}"
        )


def assert_web_absent() -> None:
    """Require both no web command and a refused web listener connection."""
    for process in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            command = process.read_bytes().replace(b"\0", b" ")
        except OSError:
            continue
        if b"src/main.py" in command:
            raise SystemExit(f"web process started before writer readiness: {process}")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(1)
        result = client.connect_ex(("127.0.0.1", 5001))
    if result != errno.ECONNREFUSED:
        raise SystemExit(
            f"web listener is not refused before readiness (connect_ex={result})"
        )


def run_writer() -> int:
    """Start the real writer behind the local proxy and reap it on shutdown."""
    running = True

    def stop(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    writer = subprocess.Popen(
        [str(REAL_WRITER), "--port", str(REAL_PORT)],
        close_fds=True,
    )
    server: ThreadingHTTPServer | None = None
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PROXY_PORT), ReadinessGateHandler)
        server.timeout = 0.25
        STARTED.touch()
        while running and writer.poll() is None:
            server.handle_request()
    finally:
        if server is not None:
            server.server_close()
        if writer.poll() is None:
            writer.terminate()
        try:
            writer.wait(timeout=5)
        except subprocess.TimeoutExpired:
            writer.kill()
            writer.wait()
    return writer.returncode or 0


def main(arguments: list[str]) -> int:
    if arguments == ["--probe"]:
        return probe()
    if arguments == ["assert-real-ready"]:
        assert_real_ready()
        return 0
    if arguments == ["assert-web-absent"]:
        assert_web_absent()
        return 0
    if arguments:
        raise SystemExit(f"unexpected test readiness gate arguments: {arguments!r}")
    return run_writer()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
