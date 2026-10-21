#!/usr/bin/env python3
"""Benchmark a Podly image in fresh synthetic containers for migration gates."""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import datetime as dt
import hashlib
import http.cookiejar
import json
import math
import os
import re
import shutil
import sqlite3
import stat
import statistics
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

from tests.writer_parity_fixtures import export_writer_parity_instance

RUST_FLAGS = (
    "PODLY_RUST_AD_MERGE_ENABLED",
    "PODLY_RUST_AUDIO_ENABLED",
    "PODLY_RUST_CHAPTERS_ENABLED",
    "PODLY_RUST_CHAPTER_FALLBACK_ENABLED",
    "PODLY_RUST_COSTS_ENABLED",
    "PODLY_RUST_FEED_POSTS_ENABLED",
    "PODLY_RUST_FEED_REFRESH_ENABLED",
    "PODLY_RUST_FEED_XML_ENABLED",
    "PODLY_RUST_JOBS_ENABLED",
    "PODLY_RUST_PROFANITY_ENABLED",
    "PODLY_RUST_STATS_ENABLED",
    "PODLY_RUST_TRANSCRIPT_ENABLED",
    "PODLY_RUST_WORD_BOUNDARY_ENABLED",
)

DEFAULT_PROFILE: dict[str, int | float] = {
    "repetitions": 3,
    "duration_seconds": 60,
    "idle_seconds": 60,
    "cooldown_seconds": 60,
    "sample_interval_seconds": 1,
    "concurrency": 8,
    "small_writes": 1000,
    "large_writes": 12,
    "mixed_writes": 100,
}
P0_PROFILE: dict[str, int | float] = {
    "repetitions": 3,
    "duration_seconds": 30.0,
    "idle_seconds": 30.0,
    "cooldown_seconds": 30.0,
    "sample_interval_seconds": 1.0,
    "concurrency": 8,
    "small_writes": 1000,
    "large_writes": 12,
    "mixed_writes": 100,
}
# Must match rust/src/writer/database.rs::EXPECTED_SCHEMA_REVISION.
EXPECTED_SCHEMA_REVISION = "080b5181e23a"
RESOURCE_SAMPLING_POLICY = "periodic-phase-end-helper-window-warmed-idle-v4"
WRITER_CPU_WINDOW_POLICY = "helper-threadpool-cgroup-cpu-v1"
PROFILE_KEYS = tuple(DEFAULT_PROFILE)
P0_ACCEPTANCE = {
    "idle_memory_bytes_max": 130_652_570,
    "writer_rss_bytes_max": 49_753_293,
    "feed_posts_p95_ms_max": 49.210,
    "feed_posts_p99_ms_max": 81.428,
    "feed_posts_throughput_min": 320.073,
    "rss_p95_ms_max": 933.051,
    "rss_p99_ms_max": 1110.410,
    "rss_throughput_min": 11.136,
    "writer_small_p95_ms_max": 22.313,
    "writer_small_p99_ms_max": 39.341,
    "writer_large_p95_ms_max": 2392.645,
    "writer_large_p99_ms_max": 2581.554,
    "writer_mixed_p95_ms_max": 534.044,
    "writer_mixed_p99_ms_max": 598.306,
    "queue_small_p95_ms_max": 20.440,
    "queue_small_p99_ms_max": 31.947,
    "queue_large_p95_ms_max": 2026.144,
    "queue_large_p99_ms_max": 2122.885,
    "queue_mixed_p95_ms_max": 321.793,
    "queue_mixed_p99_ms_max": 593.488,
    "cpu_efficiency_ratio_max": 1.15,
    "recovery_memory_headroom_bytes": 10 * 1024 * 1024,
    "recovery_threads_headroom": 2,
    "recovery_fds_headroom": 8,
}


@dataclass(frozen=True)
class HttpSample:
    latency_ms: float
    status: int
    body_bytes: int


def _run(
    args: list[str], *, timeout: float = 120, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, capture_output=True, text=True, check=check, timeout=timeout
    )


def parse_size_bytes(value: str) -> int:
    units = {
        "B": 1,
        "kB": 1000,
        "KB": 1000,
        "KiB": 1024,
        "MB": 1000**2,
        "MiB": 1024**2,
        "GB": 1000**3,
        "GiB": 1024**3,
    }
    stripped = value.strip()
    for unit in sorted(units, key=len, reverse=True):
        if stripped.endswith(unit):
            return round(float(stripped[: -len(unit)].strip()) * units[unit])
    raise ValueError(f"unsupported size: {value}")


def quantiles(values: list[float]) -> dict[str, float]:
    if not values:
        return {key: math.nan for key in ("p50", "p95", "p99", "max")}
    if len(values) == 1:
        only = round(values[0], 3)
        return {key: only for key in ("p50", "p95", "p99", "max")}
    percentile = statistics.quantiles(values, n=100, method="inclusive")
    return {
        "p50": round(statistics.median(values), 3),
        "p95": round(percentile[94], 3),
        "p99": round(percentile[98], 3),
        "max": round(max(values), 3),
    }


def parse_writer_timing(log_text: str, actions: set[str]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    marker = "[WRITER_TIMING] "
    decoder = json.JSONDecoder()
    for line in log_text.splitlines():
        if marker not in line:
            continue
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            payload, _end = decoder.raw_decode(line.split(marker, 1)[1])
            if payload.get("action") in actions:
                rows.append(payload)
    return {
        "count": len(rows),
        "failures": sum(not bool(row.get("success")) for row in rows),
        "queue_ms": quantiles(
            [float(row["queue_ms"]) for row in rows if row.get("queue_ms") is not None]
        ),
        "execution_ms": quantiles([float(row["execution_ms"]) for row in rows]),
        "total_ms": quantiles(
            [float(row["total_ms"]) for row in rows if row.get("total_ms") is not None]
        ),
    }


def _sqlite_backup(
    source: Path, destination: Path, *, immutable_source: bool = True
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    immutable = "&immutable=1" if immutable_source else ""
    source_uri = f"{source.resolve().as_uri()}?mode=ro{immutable}"
    source_conn = sqlite3.connect(source_uri, uri=True)
    destination_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(destination_conn)
    finally:
        destination_conn.close()
        source_conn.close()


def _fixture_hash(instance: Path) -> str:
    return hashlib.sha256((instance / "sqlite3.db").read_bytes()).hexdigest()


def _validate_fixture_schema(instance: Path) -> None:
    uri = f"{(instance / 'sqlite3.db').resolve().as_uri()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        revisions = connection.execute(
            "SELECT version_num FROM alembic_version ORDER BY version_num"
        ).fetchall()
        if revisions != [(EXPECTED_SCHEMA_REVISION,)]:
            raise ValueError(
                "benchmark fixture Alembic revision mismatch: "
                f"expected {EXPECTED_SCHEMA_REVISION}, got {revisions}"
            )
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ValueError("benchmark fixture integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("benchmark fixture has foreign-key violations")
    except sqlite3.Error as exc:
        raise ValueError(f"benchmark fixture schema validation failed: {exc}") from exc
    finally:
        connection.close()


def _instance_tree_hash(instance: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(instance.rglob("*")):
        relative = path.relative_to(instance)
        if path.is_symlink():
            raise ValueError(f"benchmark fixture contains a symlink: {relative}")
        if path.name.endswith(("-wal", "-shm", "-journal")):
            raise ValueError(f"benchmark fixture contains a SQLite sidecar: {relative}")
        digest.update(str(relative).encode())
        digest.update(b"\0")
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _environment_fingerprint() -> str:
    values = {}
    env_args = _docker_env("python")
    for argument in env_args:
        if argument.startswith("PODLY_WRITER_BACKEND="):
            continue
        key, separator, value = argument.partition("=")
        if separator:
            values[key] = value
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _make_instance_read_only(instance: Path) -> None:
    for path in sorted(instance.rglob("*"), reverse=True):
        path.chmod(path.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    instance.chmod(
        instance.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    )


def _make_instance_writable(instance: Path) -> None:
    for path in instance.rglob("*"):
        mode = path.stat().st_mode
        if path.is_dir():
            path.chmod(mode | stat.S_IWUSR | stat.S_IXUSR)
        else:
            path.chmod(mode | stat.S_IWUSR)
    instance.chmod(instance.stat().st_mode | stat.S_IWUSR | stat.S_IXUSR)


def _verify_fixture_hash(instance: Path, expected: str, stage: str) -> None:
    actual = _fixture_hash(instance)
    if actual != expected:
        raise ValueError(
            f"immutable benchmark fixture changed {stage}: expected {expected}, got {actual}"
        )


def _verify_fixture_tree(
    instance: Path, expected_db: str, expected_tree: str, stage: str
) -> None:
    _verify_fixture_hash(instance, expected_db, stage)
    if _instance_tree_hash(instance) != expected_tree:
        raise ValueError(f"immutable benchmark fixture tree changed {stage}")


def paired_profile(
    baseline_report: Path | None, requested: dict[str, int | float | None]
) -> tuple[dict[str, int | float], Path | None, dict[str, Any] | None]:
    if baseline_report is None:
        profile = {
            key: requested[key] if requested.get(key) is not None else default
            for key, default in DEFAULT_PROFILE.items()
        }
        return profile, None, None

    baseline = json.loads(baseline_report.read_text(encoding="utf-8"))
    baseline_protocol = baseline.get("protocol")
    if not isinstance(baseline_protocol, dict):
        raise ValueError("baseline report has no protocol object")
    missing = [key for key in PROFILE_KEYS if key not in baseline_protocol]
    if missing:
        raise ValueError(f"baseline protocol is missing profile fields: {missing}")
    profile = {key: baseline_protocol[key] for key in PROFILE_KEYS}
    if {
        **profile,
        "repetitions": P0_PROFILE["repetitions"],
    } != P0_PROFILE or profile["repetitions"] not in {3, 5}:
        raise ValueError(
            f"P0 baseline profile does not match the fixed 3/5-run P0 profile: {profile}"
        )
    mismatches = {
        key: {"baseline": profile[key], "requested": requested[key]}
        for key in PROFILE_KEYS
        if requested.get(key) is not None and requested[key] != profile[key]
    }
    if mismatches:
        raise ValueError(f"requested profile differs from P0 baseline: {mismatches}")
    source_instance = baseline_report.resolve().parent / "source" / "instance"
    if not (source_instance / "sqlite3.db").is_file():
        raise ValueError("P0 baseline source/instance/sqlite3.db is missing")
    actual_fixture = hashlib.sha256(
        (source_instance / "sqlite3.db").read_bytes()
    ).hexdigest()
    if actual_fixture != baseline_protocol.get("fixture_sha256"):
        raise ValueError(
            "P0 baseline fixture hash mismatch: "
            f"expected {baseline_protocol.get('fixture_sha256')}, got {actual_fixture}"
        )
    expected_tree = baseline_protocol.get("fixture_tree_sha256")
    if (
        not isinstance(expected_tree, str)
        or _instance_tree_hash(source_instance) != expected_tree
    ):
        raise ValueError(
            "P0 baseline immutable fixture tree hash is missing or mismatched"
        )
    _validate_fixture_schema(source_instance)
    baseline_runs = baseline.get("runs")
    if (
        not isinstance(baseline_runs, list)
        or len(baseline_runs) != profile["repetitions"]
    ):
        raise ValueError("P0 baseline run count must match its 3/5-run profile")
    if any(not isinstance(run, dict) for run in baseline_runs):
        raise ValueError("P0 baseline runs must be JSON objects")
    return profile, source_instance, baseline


def clone_instance(source: Path, destination: Path) -> None:
    expected_tree = _instance_tree_hash(source)
    destination.mkdir(parents=True, exist_ok=False)
    for child in source.iterdir():
        if child.name == "sqlite3.db":
            continue
        target = destination / child.name
        if child.is_dir():
            shutil.copytree(child, target)
        else:
            shutil.copy2(child, target)
    shutil.copy2(source / "sqlite3.db", destination / "sqlite3.db")
    _make_instance_writable(destination)
    if _instance_tree_hash(destination) != expected_tree:
        raise ValueError("benchmark run clone differs from its immutable source tree")


def _docker_env(writer_backend: str) -> list[str]:
    values = {
        "PUID": str(os.getuid()),
        "PGID": str(os.getgid()),
        "PODLY_INSTANCE_DIR": "/app/src/instance",
        "REQUIRE_AUTH": "true",
        "PODLY_ADMIN_USERNAME": "parity.user",
        "PODLY_ADMIN_PASSWORD": "parity-password-not-a-secret",
        "PODLY_SECRET_KEY": "synthetic-benchmark-session-key",
        "PODLY_IPC_AUTHKEY": "synthetic-benchmark-ipc-key",
        "PODLY_WRITER_TIMING_LOG": "true",
        "PODLY_WRITER_BACKEND": writer_backend,
        "PODLY_WRITER_IDLE_TRIM_INTERVAL_SEC": "900",
        "PODLY_MEMORY_TRIM_INTERVAL_MIN": "15",
        "PODLY_RUST_TOOLS_BIN": "/app/bin/podly_tools",
        "SERVER_THREADS": "8",
    }
    values.update({name: "true" for name in RUST_FLAGS})
    flattened: list[str] = []
    for key, value in values.items():
        flattened.extend(["-e", f"{key}={value}"])
    return flattened


def start_container(
    name: str, image: str, instance_dir: Path, writer_backend: str
) -> str:
    command = [
        "docker",
        "run",
        "-d",
        "--name",
        name,
        "--restart=no",
        "--label",
        "podly.benchmark=rust-writer-migration",
        "-p",
        "127.0.0.1:0:5001",
        "--mount",
        f"type=bind,src={instance_dir.resolve()},dst=/app/src/instance",
        "--add-host",
        "one.example.invalid:127.0.0.1",
        "--add-host",
        "two.example.invalid:127.0.0.1",
        *_docker_env(writer_backend),
        image,
    ]
    _run(command, timeout=120)
    port = _run(["docker", "port", name, "5001/tcp"]).stdout.strip()
    return f"http://{port}"


def stop_container(name: str) -> None:
    _run(["docker", "stop", "--time", "20", name], timeout=40, check=False)
    _run(["docker", "rm", name], timeout=30, check=False)


def _request(url: str, cookie: str | None = None) -> HttpSample:
    request = urllib.request.Request(url, method="GET")
    if cookie:
        request.add_header("Cookie", cookie)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read()
        status = exc.code
    return HttpSample((time.perf_counter() - started) * 1000, status, len(body))


def _login(base_url: str) -> str:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    request = urllib.request.Request(
        f"{base_url}/api/auth/login",
        data=json.dumps(
            {
                "username": "parity.user",
                "password": "parity-password-not-a-secret",
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener.open(request, timeout=30).read()
    return "; ".join(f"{cookie.name}={cookie.value}" for cookie in jar)


def wait_ready(
    name: str, base_url: str, writer_backend: str, timeout: float = 120
) -> str:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if _request(f"{base_url}/api/auth/status").status != 200:
                raise RuntimeError("auth status is not ready")
            if writer_backend == "rust":
                _run(["docker", "exec", name, "/app/bin/podly_writer", "--probe"])
            readiness_code = """
from app.writer.client import writer_client
r=writer_client.action('update_user_last_active',{'user_id':101},wait=True)
assert r is not None and r.success, r
"""
            if writer_backend == "python":
                _run(
                    [
                        "docker",
                        "exec",
                        "-e",
                        "PYTHONPATH=/app/src",
                        "-e",
                        "PODLY_WRITER_TIMEOUT_SECONDS=2",
                        name,
                        "python3",
                        "-c",
                        readiness_code,
                    ],
                    timeout=10,
                )
            cookie = _login(base_url)
            if _request(f"{base_url}/feeds", cookie).status == 200:
                return cookie
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        time.sleep(0.5)
    raise TimeoutError(f"isolated container did not become ready: {last_error}")


def direct_rust_probe(name: str) -> dict[str, int]:
    code = """
from pathlib import Path
from shared.rust_sidecar import try_render_aggregate_feed_xml, try_render_feed_posts
p=Path('/app/src/instance/sqlite3.db')
posts=try_render_feed_posts(db_path=p,feed_id=201,page=1,page_size=200,whitelisted_only=False)
xml=try_render_aggregate_feed_xml(db_path=p,user_id=101,base_url='http://benchmark.invalid',require_auth=True,limit_per_feed=3,feed_token=None,feed_secret=None)
assert isinstance(posts,bytes), type(posts)
assert isinstance(xml,bytes), type(xml)
import json; print(json.dumps({'feed_posts_bytes':len(posts),'feed_xml_bytes':len(xml)}))
"""
    output = _run(
        [
            "docker",
            "exec",
            "-e",
            "PYTHONPATH=/app/src",
            name,
            "python3",
            "-c",
            code,
        ],
        timeout=120,
    ).stdout
    return json.loads(output.strip().splitlines()[-1])


def _container_writer_backend(name: str) -> str | None:
    output = _run(
        [
            "docker",
            "inspect",
            "--format",
            "{{range .Config.Env}}{{println .}}{{end}}",
            name,
        ]
    ).stdout
    selected = [
        line.partition("=")[2]
        for line in output.splitlines()
        if line.startswith("PODLY_WRITER_BACKEND=")
    ]
    return selected[0] if len(selected) == 1 else None


def rust_writer_readiness_probe(name: str) -> bool:
    return (
        _run(
            ["docker", "exec", name, "/app/bin/podly_writer", "--probe"],
            check=False,
        ).returncode
        == 0
    )


def _python_fallback_lines(log_text: str) -> list[str]:
    return [
        line
        for line in log_text.splitlines()
        if "falling back to python" in line.lower()
    ]


def _writer_memory_trim_evidence(log_text: str, writer_backend: str) -> dict[str, Any]:
    if writer_backend != "rust":
        return {"status": "not_required", "records": []}

    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str | int | None]] = set()
    success = False
    failure = False
    for raw_line in log_text.splitlines():
        line = raw_line.strip()
        if line == "[WRITER_MEMORY_TRIM] jemalloc_mallctl=available":
            record = ("jemalloc_mallctl", "available")
            payload = {"kind": record[0], "status": record[1]}
        elif line == "[WRITER_MEMORY_TRIM] jemalloc_mallctl=unavailable purge=skipped":
            record = ("jemalloc_mallctl", "unavailable")
            payload = {"kind": record[0], "status": record[1], "purge": "skipped"}
        elif line == "[WRITER_MEMORY_TRIM] arena_purge=all rc=0":
            record = ("arena_purge", 0)
            payload = {"kind": record[0], "status": "success", "rc": 0}
            success = True
        else:
            match = re.fullmatch(
                r"\[WRITER_MEMORY_TRIM\] arena_purge=failed rc=(-?\d{1,5})", line
            )
            if match is None:
                continue
            rc = int(match.group(1))
            record = ("arena_purge_failed", rc)
            payload = {"kind": "arena_purge", "status": "failure", "rc": rc}
            failure = True
        if record not in seen:
            records.append(payload)
            seen.add(record)

    status = "failure" if failure else "success" if success else "missing"
    return {"status": status, "records": records}


def _process_snapshot(name: str) -> list[dict[str, Any]]:
    output = _run(
        ["docker", "top", name, "-eo", "pid,ppid,rss,nlwp,args"], check=False
    ).stdout
    rows: list[dict[str, Any]] = []
    for line in output.splitlines()[1:]:
        parts = line.split(None, 4)
        if len(parts) != 5 or not all(item.isdigit() for item in parts[:4]):
            continue
        pid = int(parts[0])
        try:
            fd_count = len(list(Path(f"/proc/{pid}/fd").iterdir()))
        except OSError:
            fd_count = -1
        rows.append(
            {
                "pid": pid,
                "ppid": int(parts[1]),
                "rss_bytes": int(parts[2]) * 1024,
                "threads": int(parts[3]),
                "fds": fd_count,
                "command": parts[4],
            }
        )
    return rows


def _application_log(name: str) -> str:
    result = _run(
        ["docker", "exec", name, "cat", "/app/src/instance/logs/app.log"],
        timeout=30,
        check=False,
    )
    return result.stdout + result.stderr


def _writer_role(processes: list[dict[str, Any]]) -> str | None:
    commands = "\n".join(str(process["command"]) for process in processes)
    if "podly_writer" in commands:
        return "rust"
    if "app.writer" in commands:
        return "python"
    return None


def _assert_writer_identity(name: str, writer_backend: str) -> list[dict[str, Any]]:
    processes = _process_snapshot(name)
    actual = _writer_role(processes)
    if actual != writer_backend:
        raise RuntimeError(
            f"requested {writer_backend} writer but process identity is {actual}: "
            f"{processes}"
        )
    if writer_backend == "rust" and any(
        "app.writer" in str(process["command"]) for process in processes
    ):
        raise RuntimeError("Rust benchmark contains a resident Python writer")
    return processes


def _container_cpu_seconds(name: str) -> float:
    result = _run(
        ["docker", "exec", name, "cat", "/sys/fs/cgroup/cpu.stat"], check=False
    )
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[0] == "usage_usec" and fields[1].isdigit():
                return int(fields[1]) / 1_000_000
        raise ValueError("container cgroup v2 CPU usage counter is missing or invalid")
    if "no such file" not in result.stderr.lower():
        raise ValueError("cannot read container CPU accounting")
    result = _run(
        ["docker", "exec", name, "cat", "/sys/fs/cgroup/cpuacct/cpuacct.usage"]
    )
    counter = result.stdout.strip()
    if not counter.isdigit():
        raise ValueError("container cgroup v1 CPU usage counter is invalid")
    return int(counter) / 1_000_000_000


def _cpu_measurement(before: float, after: float, elapsed: float) -> dict[str, float]:
    delta = after - before
    return _cpu_measurement_from_delta(delta, elapsed, before=before, after=after)


def _cpu_measurement_from_delta(
    cpu_seconds_total: float,
    elapsed: float,
    *,
    before: float | None = None,
    after: float | None = None,
) -> dict[str, float]:
    values = (
        (cpu_seconds_total, elapsed)
        + (() if before is None else (before,))
        + (() if after is None else (after,))
    )
    if not all(math.isfinite(value) for value in values) or (
        cpu_seconds_total <= 0 or elapsed <= 0 or (before is not None and before < 0)
    ):
        raise ValueError(
            "workload CPU accounting must increase over a positive duration"
        )
    return {
        "cpu_seconds_total": cpu_seconds_total,
        "cpu_window_elapsed_seconds": elapsed,
        "cpu_percent_mean": cpu_seconds_total * 100 / elapsed,
    }


def _writer_cpu_measurement(client: dict[str, Any]) -> dict[str, float]:
    try:
        cpu_seconds_total = float(client["cpu_seconds_total"])
        elapsed = float(client["cpu_window_elapsed_seconds"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            "writer helper CPU window measurement is missing or invalid"
        ) from exc
    return _cpu_measurement_from_delta(cpu_seconds_total, elapsed)


def _writer_workload_with_cpu(
    name: str, workload: str, count: int, concurrency: int, writer_backend: str
) -> tuple[dict[str, Any], dict[str, float]]:
    result = writer_workload(name, workload, count, concurrency, writer_backend)
    return result, _writer_cpu_measurement(result["client"])


def _with_cpu_measurement(
    name: str, workload: Callable[[], dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, float]]:
    before = _container_cpu_seconds(name)
    result = workload()
    after = _container_cpu_seconds(name)
    client = result.get("client", result)
    return result, _cpu_measurement(before, after, float(client["elapsed_seconds"]))


def _resources_with_cpu(
    samples: list[dict[str, Any]], measurements: dict[str, dict[str, float]]
) -> dict[str, Any]:
    resources = summarize_resources(samples)
    for phase, measurement in measurements.items():
        resources[phase]["cpu_percent_periodic_mean"] = resources[phase][
            "cpu_percent_mean"
        ]
        resources[phase].update(measurement)
    return resources


def resource_sample(name: str, phase: str) -> dict[str, Any]:
    raw = _run(
        ["docker", "stats", name, "--no-stream", "--format", "{{json .}}"],
        timeout=30,
    ).stdout.strip()
    stats = json.loads(raw)
    usage = stats["MemUsage"].split("/", 1)[0].strip()
    return {
        "timestamp": dt.datetime.now(dt.UTC).isoformat(),
        "phase": phase,
        "container_memory_bytes": parse_size_bytes(usage),
        "container_cpu_percent": float(stats["CPUPerc"].rstrip("%")),
        "container_pids": int(stats["PIDs"]),
        "processes": _process_snapshot(name),
    }


class Sampler:
    def __init__(self, name: str, interval: float) -> None:
        self.name = name
        self.interval = interval
        self._phase = "idle"
        self._sample_lock = threading.Lock()
        self.samples: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._started = False

    @property
    def phase(self) -> str:
        return self._phase

    @phase.setter
    def phase(self, phase: str) -> None:
        # Capture the outgoing phase even when its entire workload completed
        # between periodic samples. Keep an in-flight sample's label stable.
        # Workload latency/throughput timers have already stopped at this point.
        with self._sample_lock:
            if self._started and phase != self._phase:
                self.samples.append(resource_sample(self.name, self._phase))
            self._phase = phase

    def start(self) -> None:
        self._thread.start()
        self._started = True

    def close(self) -> None:
        if not self._started:
            return
        self._stop.set()
        self._thread.join(timeout=self.interval + 30)

    def _loop(self) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(Exception):
                with self._sample_lock:
                    self.samples.append(resource_sample(self.name, self._phase))
            self._stop.wait(self.interval)


def http_workload(
    url: str, cookie: str, duration: float, concurrency: int
) -> dict[str, Any]:
    deadline = time.monotonic() + duration

    def worker() -> list[HttpSample]:
        samples: list[HttpSample] = []
        while time.monotonic() < deadline:
            samples.append(_request(url, cookie))
        return samples

    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        batches = list(executor.map(lambda _index: worker(), range(concurrency)))
    elapsed = time.perf_counter() - started
    samples = [sample for batch in batches for sample in batch]
    statuses = {
        status: sum(item.status == status for item in samples)
        for status in {item.status for item in samples}
    }
    return {
        "url": url,
        "count": len(samples),
        "concurrency": concurrency,
        "elapsed_seconds": round(elapsed, 6),
        "throughput_per_second": round(len(samples) / elapsed, 3),
        "status_codes": statuses,
        "body_bytes_mean": round(statistics.fmean(item.body_bytes for item in samples)),
        "latency_ms": quantiles([item.latency_ms for item in samples]),
    }


def writer_workload(
    name: str, workload: str, count: int, concurrency: int, writer_backend: str
) -> dict[str, Any]:
    log_before = _application_log(name)
    workload_started_at = dt.datetime.now(dt.UTC).isoformat()
    result = _run(
        [
            "docker",
            "exec",
            "-e",
            "PYTHONPATH=/app/src",
            name,
            "python3",
            "/app/scripts/bench_writer_service.py",
            "--workload",
            workload,
            "--count",
            str(count),
            "--concurrency",
            str(concurrency),
            "--measure-cgroup-cpu",
        ],
        timeout=1800,
    )
    client = json.loads(result.stdout.strip().splitlines()[-1])
    if writer_backend == "rust":
        log_result = _run(
            ["docker", "logs", "--since", workload_started_at, name], check=False
        )
        logs = log_result.stdout + log_result.stderr
    else:
        log_after = _application_log(name)
        logs = (
            log_after[len(log_before) :]
            if log_after.startswith(log_before)
            else log_after
        )
    actions = (
        {"update_user_last_active"}
        if workload == "small"
        else {"replace_transcription"}
        if workload == "large"
        else {"update_user_last_active", "replace_transcription"}
    )
    return {
        "client": client,
        "writer_timing": parse_writer_timing(logs, actions),
    }


def summarize_resources(samples: list[dict[str, Any]]) -> dict[str, Any]:
    if not samples:
        raise ValueError("resource sampler collected zero samples")
    by_phase: dict[str, list[dict[str, Any]]] = {}
    for sample in samples:
        by_phase.setdefault(sample["phase"], []).append(sample)
    output: dict[str, Any] = {}
    for phase, rows in by_phase.items():
        memory = [row["container_memory_bytes"] for row in rows]
        cpu = [row["container_cpu_percent"] for row in rows]
        output[phase] = {
            "samples": len(rows),
            "memory_bytes_median": round(statistics.median(memory)),
            "memory_bytes_max": max(memory),
            "cpu_percent_mean": round(statistics.fmean(cpu), 3),
            "pids_max": max(row["container_pids"] for row in rows),
            "threads_max": max(
                sum(process["threads"] for process in row["processes"]) for row in rows
            ),
            "fds_max": max(
                sum(max(0, process["fds"]) for process in row["processes"])
                for row in rows
            ),
        }
    return output


def cooldown(sampler: Sampler, seconds: float, label: str) -> None:
    sampler.phase = f"cooldown_{label}"
    time.sleep(seconds)


def _warm_idle_window(
    name: str,
    sampler: Sampler,
    base_url: str,
    cookie: str,
    idle_seconds: float,
) -> list[dict[str, Any]]:
    sampler.phase = "warmup"
    for url in (
        f"{base_url}/api/feeds/201/posts?page=1&page_size=200",
        f"{base_url}/feed/user/101",
    ):
        for _ in range(10):
            _request(url, cookie)
    time.sleep(2)
    sampler.phase = "warmed_idle"
    time.sleep(idle_seconds)
    return _process_snapshot(name)


def _median_metric(report: dict[str, Any], selector: Any) -> float:
    values = [float(selector(run)) for run in report["runs"]]
    return float(statistics.median(values))


def _writer_rss(report: dict[str, Any], backend: str) -> float:
    marker = "podly_writer" if backend == "rust" else "app.writer"
    return _median_metric(
        report,
        lambda run: next(
            int(process["rss_bytes"])
            for process in run["processes_after_cooldown"]
            if marker in str(process["command"])
        ),
    )


def _validate_pair_environment(paired: dict[str, Any], current: dict[str, Any]) -> str:
    paired_protocol = paired["protocol"]
    current_protocol = current["protocol"]
    if not paired_protocol.get("runner_sha256") or paired_protocol.get(
        "runner_sha256"
    ) != current_protocol.get("runner_sha256"):
        raise ValueError("paired benchmark runner source hashes differ or are missing")
    if paired_protocol.get("resource_sampling") != RESOURCE_SAMPLING_POLICY or (
        paired_protocol.get("resource_sampling")
        != current_protocol.get("resource_sampling")
    ):
        raise ValueError("paired resource sampling methods differ or are unsupported")
    if (
        paired_protocol.get("writer_cpu_window") != WRITER_CPU_WINDOW_POLICY
        or current_protocol.get("writer_cpu_window") != WRITER_CPU_WINDOW_POLICY
    ):
        raise ValueError("paired writer CPU windows are missing or unsupported")
    paired_warmed_idle_seconds = paired_protocol.get("warmed_idle_seconds")
    current_warmed_idle_seconds = current_protocol.get("warmed_idle_seconds")
    if (
        paired_warmed_idle_seconds != 30.0
        or current_warmed_idle_seconds != 30.0
        or paired_warmed_idle_seconds != paired_protocol.get("idle_seconds")
        or current_warmed_idle_seconds != current_protocol.get("idle_seconds")
    ):
        raise ValueError(
            "paired warmed-idle windows must both match the 30s idle profile"
        )
    if paired_protocol.get("warmed_idle_seconds") != current_protocol.get(
        "warmed_idle_seconds"
    ):
        raise ValueError("paired warmed-idle durations differ")
    paired_environment = paired_protocol.get("environment_fingerprint")
    current_environment = current_protocol.get("environment_fingerprint")
    if not isinstance(paired_environment, str) or not paired_environment:
        raise ValueError("paired baseline lacks recorded environment fingerprint")
    if not isinstance(current_environment, str) or not current_environment:
        raise ValueError("P4 report lacks recorded environment fingerprint")
    if paired_environment != current_environment:
        raise ValueError("paired Python and Rust container environments differ")
    if current_environment != _environment_fingerprint():
        raise ValueError("current benchmark environment fingerprint is invalid")
    return "matching recorded environment fingerprints"


def _validate_pair_workload(
    historical: dict[str, Any], paired: dict[str, Any], current: dict[str, Any]
) -> None:
    paired_protocol = paired["protocol"]
    current_protocol = current["protocol"]
    for key in ("fixture_sha256", "fixture_tree_sha256"):
        if paired_protocol.get(key) != current_protocol.get(key):
            raise ValueError(f"paired reports use different {key} values")
    profile_keys = [key for key in PROFILE_KEYS if key != "repetitions"]
    if any(
        paired_protocol.get(key) != current_protocol.get(key) for key in profile_keys
    ):
        raise ValueError("paired Python and Rust workloads do not match")
    repetitions = paired_protocol.get("repetitions")
    if repetitions not in {3, 5} or current_protocol.get("repetitions") != repetitions:
        raise ValueError("paired reports must use the same 3- or 5-run count")
    if (
        len(paired.get("runs", [])) != repetitions
        or len(current.get("runs", [])) != repetitions
    ):
        raise ValueError("paired report run count does not match its protocol")
    historical_protocol = historical.get("protocol")
    if isinstance(historical_protocol, dict) and any(
        historical_protocol.get(key) != paired_protocol.get(key) for key in profile_keys
    ):
        raise ValueError(
            "historical P0 workload profile differs from the paired profile"
        )


def _validate_pair_protocols(
    historical: dict[str, Any], paired: dict[str, Any], current: dict[str, Any]
) -> str:
    paired_protocol = paired.get("protocol")
    current_protocol = current.get("protocol")
    if not isinstance(paired_protocol, dict) or not isinstance(current_protocol, dict):
        raise ValueError("paired reports must contain protocol objects")
    if not isinstance(historical.get("runs"), list) or len(historical["runs"]) < 3:
        raise ValueError("historical threshold report must contain at least three runs")
    if paired_protocol.get("writer_backend") != "python":
        raise ValueError("paired baseline must select the Python writer")
    if current_protocol.get("writer_backend") != "rust":
        raise ValueError("P4 report must select the Rust writer")
    if paired_protocol.get("image_id") != current_protocol.get("image_id"):
        raise ValueError("paired Python and Rust reports do not use the same image ID")
    _validate_pair_workload(historical, paired, current)
    return _validate_pair_environment(paired, current)


def _process_role(command: str) -> str | None:
    lowered = command.lower()
    if "podly_writer" in lowered or "app.writer" in lowered:
        return "writer"
    if "src/main.py" in lowered:
        return "web"
    if "start_services.sh" in lowered:
        return "supervisor"
    return None


def compare_to_p0(
    paired_baseline: dict[str, Any],
    current: dict[str, Any],
    historical_baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply original P0 caps and stricter same-image paired-baseline gates."""
    historical = historical_baseline or paired_baseline
    try:
        return _compare_to_p0(historical, paired_baseline, current)
    except (
        KeyError,
        TypeError,
        ValueError,
        StopIteration,
        statistics.StatisticsError,
    ) as exc:
        # Missing resource phases, incomplete process snapshots, or malformed
        # report data must fail acceptance explicitly rather than crash while
        # calculating a metric.
        return {
            "thresholds": {"original_p0": P0_ACCEPTANCE},
            "gates": {
                "complete_measurements": {
                    "passed": False,
                    "error": type(exc).__name__,
                }
            },
            "passed": False,
        }


def _compare_to_p0(
    historical: dict[str, Any],
    paired: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    environment_provenance = _validate_pair_protocols(historical, paired, current)
    gates: dict[str, dict[str, Any]] = {}
    paired_thresholds: dict[str, float] = {}
    spread_candidates: list[dict[str, Any]] = []

    def bounded(
        name: str, observed: float, limit: float, *, minimum: bool = False
    ) -> None:
        passed = observed >= limit if minimum else observed <= limit
        gates[name] = {
            "observed": round(observed, 3),
            "threshold": limit,
            "comparison": ">=" if minimum else "<=",
            "passed": passed,
        }

    def value(path: tuple[str, ...]) -> float:
        return _median_metric(
            current,
            lambda run: _deep_get(run, path),
        )

    def metric_median(report: dict[str, Any], path: tuple[str, ...]) -> float:
        return _median_metric(
            report,
            lambda run: _deep_get(run, path),
        )

    def relative_limit(key: str, path: tuple[str, ...], *, minimum: bool) -> float:
        old_limit = float(P0_ACCEPTANCE[key])
        old_median = metric_median(historical, path)
        paired_median = metric_median(paired, path)
        if minimum:
            limit = max(old_limit, paired_median - (old_median - old_limit))
        else:
            limit = min(old_limit, paired_median + (old_limit - old_median))
        paired_thresholds[key] = limit
        spread_candidates.append(
            {
                "name": key,
                "paired_selector": lambda run: _deep_get(run, path),
                "current_selector": lambda run: _deep_get(run, path),
                "observed": value(path),
                "kind": "minimum" if minimum else "maximum",
                "old_limit": old_limit,
                "old_median": old_median,
            }
        )
        return limit

    def cpu_efficiency(
        report: dict[str, Any], phase: str, section: tuple[str, ...]
    ) -> float:
        return _median_metric(
            report,
            lambda run: (
                float(_deep_get(run, ("resources", phase, "cpu_percent_mean")))
                / max(float(_deep_get(run, (*section, "throughput_per_second"))), 1e-9)
            ),
        )

    idle_memory = value(("resources", "idle", "memory_bytes_median"))
    paired_idle = metric_median(paired, ("resources", "idle", "memory_bytes_median"))
    idle_relative_limit = min(
        float(P0_ACCEPTANCE["idle_memory_bytes_max"]),
        paired_idle - 50 * 1024 * 1024,
        paired_idle * 0.75,
    )
    paired_thresholds["idle_container_memory"] = idle_relative_limit
    idle_path = ("resources", "idle", "memory_bytes_median")
    spread_candidates.append(
        {
            "name": "idle_container_memory",
            "paired_selector": lambda run: _deep_get(run, idle_path),
            "current_selector": lambda run: _deep_get(run, idle_path),
            "observed": idle_memory,
            "kind": "idle_memory",
            "old_limit": float(P0_ACCEPTANCE["idle_memory_bytes_max"]),
            "old_median": 0.0,
        }
    )
    bounded(
        "idle_container_memory", idle_memory, P0_ACCEPTANCE["idle_memory_bytes_max"]
    )
    bounded("paired_idle_container_memory", idle_memory, idle_relative_limit)

    warmed_idle_memory = value(("resources", "warmed_idle", "memory_bytes_median"))
    paired_warmed_idle = metric_median(
        paired, ("resources", "warmed_idle", "memory_bytes_median")
    )
    warmed_idle_relative_limit = min(
        float(P0_ACCEPTANCE["idle_memory_bytes_max"]),
        paired_warmed_idle - 50 * 1024 * 1024,
        paired_warmed_idle * 0.75,
    )
    paired_thresholds["paired_warmed_idle_container_memory"] = (
        warmed_idle_relative_limit
    )
    warmed_idle_path = ("resources", "warmed_idle", "memory_bytes_median")
    spread_candidates.append(
        {
            "name": "warmed_idle_container_memory",
            "paired_selector": lambda run: _deep_get(run, warmed_idle_path),
            "current_selector": lambda run: _deep_get(run, warmed_idle_path),
            "observed": warmed_idle_memory,
            "kind": "idle_memory",
            "old_limit": float(P0_ACCEPTANCE["idle_memory_bytes_max"]),
            "old_median": 0.0,
        }
    )
    bounded(
        "warmed_idle_container_memory",
        warmed_idle_memory,
        P0_ACCEPTANCE["idle_memory_bytes_max"],
    )
    bounded(
        "paired_warmed_idle_container_memory",
        warmed_idle_memory,
        warmed_idle_relative_limit,
    )

    writer_rss = _writer_rss(current, "rust")
    paired_writer_rss = _writer_rss(paired, "python")
    rss_relative_limit = min(
        float(P0_ACCEPTANCE["writer_rss_bytes_max"]), paired_writer_rss * 0.4
    )
    paired_thresholds["rust_writer_rss"] = rss_relative_limit
    spread_candidates.append(
        {
            "name": "rust_writer_rss",
            "paired_selector": lambda run: next(
                int(process["rss_bytes"])
                for process in run["processes_after_cooldown"]
                if "app.writer" in str(process["command"])
            ),
            "current_selector": lambda run: next(
                int(process["rss_bytes"])
                for process in run["processes_after_cooldown"]
                if "podly_writer" in str(process["command"])
            ),
            "observed": writer_rss,
            "kind": "writer_rss",
            "old_limit": float(P0_ACCEPTANCE["writer_rss_bytes_max"]),
            "old_median": 0.0,
        }
    )
    bounded("rust_writer_rss", writer_rss, P0_ACCEPTANCE["writer_rss_bytes_max"])
    bounded("paired_rust_writer_rss", writer_rss, rss_relative_limit)

    latency_and_throughput = (
        ("feed_posts_p95_ms_max", ("http", "feed_posts", "latency_ms", "p95")),
        ("feed_posts_p99_ms_max", ("http", "feed_posts", "latency_ms", "p99")),
        (
            "feed_posts_throughput_min",
            ("http", "feed_posts", "throughput_per_second"),
        ),
        ("rss_p95_ms_max", ("http", "rss", "latency_ms", "p95")),
        ("rss_p99_ms_max", ("http", "rss", "latency_ms", "p99")),
        ("rss_throughput_min", ("http", "rss", "throughput_per_second")),
    )
    for key, path in latency_and_throughput:
        minimum = key.endswith("throughput_min")
        bounded(
            key,
            value(path),
            P0_ACCEPTANCE[key],
            minimum=minimum,
        )
        bounded(
            f"paired_{key}",
            value(path),
            relative_limit(key, path, minimum=minimum),
            minimum=minimum,
        )

    for name in ("small", "large", "mixed"):
        for percentile in ("p95", "p99"):
            e2e_key = f"writer_{name}_{percentile}_ms_max"
            queue_key = f"queue_{name}_{percentile}_ms_max"
            bounded(
                e2e_key,
                value(("writer", name, "client", "latency_ms", percentile)),
                P0_ACCEPTANCE[e2e_key],
            )
            e2e_path = ("writer", name, "client", "latency_ms", percentile)
            bounded(
                f"paired_{e2e_key}",
                value(e2e_path),
                relative_limit(e2e_key, e2e_path, minimum=False),
            )
            bounded(
                queue_key,
                value(("writer", name, "writer_timing", "queue_ms", percentile)),
                P0_ACCEPTANCE[queue_key],
            )
            queue_path = ("writer", name, "writer_timing", "queue_ms", percentile)
            bounded(
                f"paired_{queue_key}",
                value(queue_path),
                relative_limit(queue_key, queue_path, minimum=False),
            )

    cpu_sections = (
        ("feed_posts", "http_feed_posts", ("http", "feed_posts")),
        ("rss", "http_rss", ("http", "rss")),
        ("small", "writer_small", ("writer", "small", "client")),
        ("large", "writer_large", ("writer", "large", "client")),
        ("mixed", "writer_mixed", ("writer", "mixed", "client")),
    )
    for name, phase, section in cpu_sections:
        current_efficiency = cpu_efficiency(current, phase, section)
        historical_efficiency = cpu_efficiency(historical, phase, section)
        paired_efficiency = cpu_efficiency(paired, phase, section)
        historical_ratio = (
            current_efficiency / historical_efficiency
            if historical_efficiency
            else math.inf
        )
        paired_ratio = (
            current_efficiency / paired_efficiency if paired_efficiency else math.inf
        )
        bounded(
            f"cpu_efficiency_{name}_historical",
            historical_ratio,
            P0_ACCEPTANCE["cpu_efficiency_ratio_max"],
        )
        bounded(
            f"cpu_efficiency_{name}_paired",
            paired_ratio,
            P0_ACCEPTANCE["cpu_efficiency_ratio_max"],
        )

    spread_results = []

    def candidate_threshold(candidate: dict[str, Any], paired_value: float) -> float:
        kind = candidate["kind"]
        if kind == "idle_memory":
            return min(
                float(P0_ACCEPTANCE["idle_memory_bytes_max"]),
                paired_value - 50 * 1024 * 1024,
                paired_value * 0.75,
            )
        if kind == "writer_rss":
            return min(float(P0_ACCEPTANCE["writer_rss_bytes_max"]), paired_value * 0.4)
        old_limit = float(candidate["old_limit"])
        old_median = float(candidate["old_median"])
        if kind == "minimum":
            return max(old_limit, paired_value - (old_median - old_limit))
        return min(old_limit, paired_value + (old_limit - old_median))

    for candidate in spread_candidates:
        paired_values = [
            float(candidate["paired_selector"](run)) for run in paired["runs"]
        ]
        current_values = [
            float(candidate["current_selector"](run)) for run in current["runs"]
        ]
        paired_median = float(statistics.median(paired_values))
        current_median = float(statistics.median(current_values))
        paired_spread = (max(paired_values) - min(paired_values)) / max(
            abs(paired_median), 1e-9
        )
        current_spread = (max(current_values) - min(current_values)) / max(
            abs(current_median), 1e-9
        )
        old_limit = float(candidate["old_limit"])
        median_limit = candidate_threshold(candidate, paired_median)
        extreme_limits = [
            candidate_threshold(candidate, min(paired_values)),
            candidate_threshold(candidate, max(paired_values)),
        ]
        is_minimum = candidate["kind"] == "minimum"

        def passes(
            observed_value: float,
            limit_value: float,
            *,
            minimum_gate: bool = is_minimum,
        ) -> bool:
            return (
                observed_value >= limit_value
                if minimum_gate
                else observed_value <= limit_value
            )

        baseline_can_reverse = (
            len(
                {
                    passes(current_median, limit)
                    for limit in [median_limit, *extreme_limits]
                }
            )
            > 1
        )
        current_limits = [old_limit, median_limit, *extreme_limits]
        current_can_reverse = any(
            min(current_values) <= limit <= max(current_values)
            for limit in current_limits
        )
        spread = max(paired_spread, current_spread)
        could_reverse = spread > 0.10 and (baseline_can_reverse or current_can_reverse)
        spread_results.append(
            {
                "metric": candidate["name"],
                "relative_range": round(spread, 4),
                "paired_threshold": round(median_limit, 3),
                "observed_current_median": round(current_median, 3),
                "could_reverse_gate": could_reverse,
            }
        )
    for name, phase, section in cpu_sections:
        paired_values = [
            float(_deep_get(run, ("resources", phase, "cpu_percent_mean")))
            / max(float(_deep_get(run, (*section, "throughput_per_second"))), 1e-9)
            for run in paired["runs"]
        ]
        current_values = [
            float(_deep_get(run, ("resources", phase, "cpu_percent_mean")))
            / max(float(_deep_get(run, (*section, "throughput_per_second"))), 1e-9)
            for run in current["runs"]
        ]
        paired_spread = (max(paired_values) - min(paired_values)) / max(
            abs(float(statistics.median(paired_values))), 1e-9
        )
        current_spread = (max(current_values) - min(current_values)) / max(
            abs(float(statistics.median(current_values))), 1e-9
        )
        paired_median = float(statistics.median(paired_values))
        current_median = float(statistics.median(current_values))
        ratio_limits = [
            current_value / baseline_value
            for baseline_value in (min(paired_values), max(paired_values))
            for current_value in (min(current_values), max(current_values))
            if baseline_value > 0
        ]
        historical_values = [
            float(_deep_get(run, ("resources", phase, "cpu_percent_mean")))
            / max(float(_deep_get(run, (*section, "throughput_per_second"))), 1e-9)
            for run in historical["runs"]
        ]
        historical_ratio_values = [
            current_value / baseline_value
            for current_value in current_values
            for baseline_value in historical_values
            if baseline_value > 0
        ]
        gate = float(P0_ACCEPTANCE["cpu_efficiency_ratio_max"])
        could_reverse = max(paired_spread, current_spread) > 0.10 and (
            min(ratio_limits) <= gate <= max(ratio_limits)
            or min(historical_ratio_values) <= gate <= max(historical_ratio_values)
        )
        spread_results.append(
            {
                "metric": f"cpu_efficiency_{name}_paired",
                "relative_range": round(max(paired_spread, current_spread), 4),
                "observed_current_median": round(current_median, 4),
                "could_reverse_gate": could_reverse,
            }
        )
    requires_five = [
        item["metric"] for item in spread_results if item["could_reverse_gate"]
    ]
    repetition_count = len(current["runs"])
    gates["paired_baseline_spread"] = {
        "metrics": spread_results,
        "required_repetitions": 5 if requires_five else repetition_count,
        "passed": not requires_five or repetition_count >= 5,
    }
    gates["paired_environment_equivalence"] = {
        "provenance": environment_provenance,
        "passed": True,
    }

    recovery_ok = True
    recovery_observed: dict[str, int] = {}
    for run in current["runs"]:
        idle = int(run["resources"]["idle"]["memory_bytes_median"])
        cooldown_rows = [
            row
            for phase, row in run["resources"].items()
            if phase.startswith("cooldown_")
        ]
        recovery_observed[run["run"]] = max(
            (int(row["memory_bytes_max"]) - idle for row in cooldown_rows),
            default=0,
        )
        recovery_ok = (
            recovery_ok
            and bool(cooldown_rows)
            and all(
                int(row["memory_bytes_max"])
                <= idle + P0_ACCEPTANCE["recovery_memory_headroom_bytes"]
                for row in cooldown_rows
            )
        )
    gates["post_burst_memory_recovery"] = {
        "observed_extra_bytes_by_run": recovery_observed,
        "threshold_extra_bytes": P0_ACCEPTANCE["recovery_memory_headroom_bytes"],
        "passed": recovery_ok,
    }

    resource_ok = True
    resource_observed: dict[str, dict[str, int]] = {}
    for run in current["runs"]:
        idle_resources = run["resources"]["idle"]
        final_processes = run["processes_after_cooldown"]
        final_threads = sum(int(process["threads"]) for process in final_processes)
        final_fds = sum(max(0, int(process["fds"])) for process in final_processes)
        thread_delta = final_threads - int(idle_resources["threads_max"])
        fd_delta = final_fds - int(idle_resources["fds_max"])
        resource_observed[str(run["run"])] = {
            "threads_delta": thread_delta,
            "fds_delta": fd_delta,
        }
        resource_ok = resource_ok and (
            thread_delta <= P0_ACCEPTANCE["recovery_threads_headroom"]
            and fd_delta <= P0_ACCEPTANCE["recovery_fds_headroom"]
        )
    gates["thread_fd_recovery"] = {
        "observed_deltas_by_run": resource_observed,
        "thread_headroom": P0_ACCEPTANCE["recovery_threads_headroom"],
        "fd_headroom": P0_ACCEPTANCE["recovery_fds_headroom"],
        "passed": resource_ok,
    }

    idle_series = [
        int(run["resources"]["idle"]["memory_bytes_median"]) for run in current["runs"]
    ]
    monotonic_growth = len(idle_series) > 1 and all(
        right > left for left, right in pairwise(idle_series)
    )
    gates["no_monotonic_idle_growth"] = {
        "idle_memory_bytes_by_run": idle_series,
        "passed": not monotonic_growth,
    }

    process_ok = True
    for run in current["runs"]:
        commands = [
            str(process["command"]) for process in run["processes_after_cooldown"]
        ]
        process_ok = process_ok and run["writer_backend_process_identity"] == "rust"
        process_ok = process_ok and any(
            "src/main.py" in command for command in commands
        )
        process_ok = process_ok and not any(
            "app.writer" in command or "bootstrap" in command for command in commands
        )
        process_ok = process_ok and not run["fallback_lines"]
    gates["rust_identity_and_fallback"] = {"passed": process_ok}
    return {
        "thresholds": {
            "original_p0": P0_ACCEPTANCE,
            "paired_derived": paired_thresholds,
        },
        "gates": gates,
        "passed": all(gate["passed"] for gate in gates.values()),
    }


def _deep_get(value: dict[str, Any], path: tuple[str, ...]) -> Any:
    current_value: Any = value
    for key in path:
        current_value = current_value[key]
    return current_value


def run_repetition(
    *,
    image: str,
    output: Path,
    index: int,
    duration: float,
    idle_seconds: float,
    cooldown_seconds: float,
    sample_interval: float,
    concurrency: int,
    small_writes: int,
    large_writes: int,
    mixed_writes: int,
    writer_backend: str,
    fixture_sha256: str,
    fixture_tree_sha256: str,
) -> dict[str, Any]:
    name = f"podly-writer-{writer_backend}-{os.getpid()}-{index}"
    instance_dir = output / f"run-{index}" / "instance"
    source_instance = output / "source" / "instance"
    _verify_fixture_tree(
        source_instance, fixture_sha256, fixture_tree_sha256, "before clone"
    )
    clone_instance(source_instance, instance_dir)
    if _fixture_hash(instance_dir) != fixture_sha256:
        raise ValueError(f"run {index} clone does not match the immutable fixture")
    sampler = Sampler(name, sample_interval)
    phase_cpu: dict[str, dict[str, float]] = {}
    started_at = dt.datetime.now(dt.UTC).isoformat()
    try:
        base_url = start_container(name, image, instance_dir, writer_backend)
        cookie = wait_ready(name, base_url, writer_backend)
        processes_ready = _assert_writer_identity(name, writer_backend)
        rust_probe = direct_rust_probe(name)
        writer_backend_environment = _container_writer_backend(name)
        writer_readiness_probe = (
            rust_writer_readiness_probe(name) if writer_backend == "rust" else None
        )
        sampler.start()
        sampler.phase = "idle"
        time.sleep(idle_seconds)

        processes_after_warmed_idle = _warm_idle_window(
            name, sampler, base_url, cookie, idle_seconds
        )

        sampler.phase = "http_feed_posts"
        feed_posts, phase_cpu["http_feed_posts"] = _with_cpu_measurement(
            name,
            lambda: http_workload(
                f"{base_url}/api/feeds/201/posts?page=1&page_size=200",
                cookie,
                duration,
                concurrency,
            ),
        )
        cooldown(sampler, cooldown_seconds, "http_feed_posts")

        sampler.phase = "http_rss"
        rss, phase_cpu["http_rss"] = _with_cpu_measurement(
            name,
            lambda: http_workload(
                f"{base_url}/feed/user/101", cookie, duration, concurrency
            ),
        )
        cooldown(sampler, cooldown_seconds, "http_rss")

        sampler.phase = "writer_small"
        small, phase_cpu["writer_small"] = _writer_workload_with_cpu(
            name, "small", small_writes, concurrency, writer_backend
        )
        cooldown(sampler, cooldown_seconds, "writer_small")

        sampler.phase = "writer_large"
        large, phase_cpu["writer_large"] = _writer_workload_with_cpu(
            name, "large", large_writes, concurrency, writer_backend
        )
        cooldown(sampler, cooldown_seconds, "writer_large")

        sampler.phase = "writer_mixed"
        mixed, phase_cpu["writer_mixed"] = _writer_workload_with_cpu(
            name, "mixed", mixed_writes, concurrency, writer_backend
        )
        cooldown(sampler, cooldown_seconds, "writer_mixed")
        sampler.close()

        log_result = _run(["docker", "logs", "--since", started_at, name], check=False)
        logs = log_result.stdout + log_result.stderr + _application_log(name)
        fallback_lines = _python_fallback_lines(logs)
        writer_memory_trim = _writer_memory_trim_evidence(logs, writer_backend)
        process_end = _process_snapshot(name)
        final_writer = _writer_role(process_end)
        return {
            "run": index,
            "container": name,
            "base_url": base_url,
            "started_at": started_at,
            "rust_probe": rust_probe,
            "writer_backend_requested": writer_backend,
            "writer_backend_environment": writer_backend_environment,
            "writer_readiness_probe": writer_readiness_probe,
            "writer_backend_process_identity": final_writer,
            "processes_after_ready": processes_ready,
            "processes_after_warmed_idle": processes_after_warmed_idle,
            "fallback_lines": fallback_lines,
            "writer_memory_trim": writer_memory_trim,
            "http": {"feed_posts": feed_posts, "rss": rss},
            "writer": {"small": small, "large": large, "mixed": mixed},
            "resources": _resources_with_cpu(sampler.samples, phase_cpu),
            "processes_after_cooldown": process_end,
        }
    finally:
        sampler.close()
        stop_container(name)
        _verify_fixture_tree(
            source_instance,
            fixture_sha256,
            fixture_tree_sha256,
            f"after run {index}",
        )


def _select_profile(
    args: argparse.Namespace,
) -> tuple[dict[str, Any], Path | None, dict[str, Any] | None]:
    requested = _requested_profile(args)
    if args.writer_baseline:
        if args.writer_backend != "python":
            raise ValueError("--writer-baseline requires --writer-backend python")
        if any(
            value is not None
            for key, value in requested.items()
            if key != "repetitions"
        ):
            raise ValueError("--writer-baseline uses the fixed P0 workload profile")
        if requested["repetitions"] not in {None, 3, 5}:
            raise ValueError("P0 baseline repetitions must be 3 or 5")
        profile = dict(P0_PROFILE)
        baseline_instance = None
        baseline = None
        if args.baseline_report:
            source_profile, baseline_instance, baseline = paired_profile(
                args.baseline_report, {}
            )
        profile["repetitions"] = requested["repetitions"] or (
            source_profile["repetitions"] if args.baseline_report else 3
        )
        return profile, baseline_instance, baseline
    if args.baseline_report is None:
        raise ValueError("Rust benchmark requires a fresh Python --baseline-report")
    return paired_profile(args.baseline_report, requested)


def _read_threshold_report(args: argparse.Namespace) -> dict[str, Any] | None:
    if args.writer_baseline:
        return None
    if args.threshold_report is None:
        raise ValueError(
            "Rust benchmark requires --threshold-report for original P0 gates"
        )
    return json.loads(args.threshold_report.read_text(encoding="utf-8"))


def main() -> int:
    parser, args = _parse_benchmark_args()
    try:
        profile, baseline_instance, baseline = _select_profile(args)
        threshold_baseline = _read_threshold_report(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    _validate_run_options(parser, args, profile)
    args.output.mkdir(parents=True, exist_ok=True)
    source_instance = args.output / "source" / "instance"
    _copy_benchmark_fixture(source_instance, baseline_instance)
    threshold_hash = (
        _file_hash(args.threshold_report) if args.threshold_report else None
    )
    protocol = _build_protocol(
        args, profile, source_instance, baseline, parser, threshold_hash
    )
    (args.output / "protocol.json").write_text(json.dumps(protocol, indent=2))
    runs = _execute_repetitions(
        args,
        profile,
        protocol["fixture_sha256"],
        protocol["fixture_tree_sha256"],
    )
    try:
        _verify_fixture_tree(
            source_instance,
            protocol["fixture_sha256"],
            protocol["fixture_tree_sha256"],
            "before report finalization",
        )
    except ValueError as exc:
        parser.error(str(exc))
    report = {"protocol": protocol, "runs": runs}
    (args.output / "report.json").write_text(json.dumps(report, indent=2))
    errors = _collect_run_errors(runs, args.writer_backend)
    if args.baseline_report and _file_hash(args.baseline_report) != protocol.get(
        "paired_baseline_report_sha256"
    ):
        parser.error("fixture-source baseline report changed during benchmark")
    if baseline is not None and not args.writer_baseline:
        if (
            args.threshold_report
            and _file_hash(args.threshold_report) != threshold_hash
        ):
            parser.error("historical P0 threshold report changed during benchmark")
        if args.baseline_report and _file_hash(args.baseline_report) != protocol.get(
            "paired_baseline_report_sha256"
        ):
            parser.error("paired Python baseline report changed during benchmark")
        comparison = compare_to_p0(baseline, report, threshold_baseline)
        (args.output / "comparison.json").write_text(json.dumps(comparison, indent=2))
        errors.extend(_comparison_errors(comparison))
    print(json.dumps({"report": str(args.output / "report.json"), "errors": errors}))
    return 0 if not errors else 1


def _parse_benchmark_args() -> tuple[argparse.ArgumentParser, argparse.Namespace]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline-report", type=Path)
    parser.add_argument("--threshold-report", type=Path)
    parser.add_argument("--writer-baseline", action="store_true")
    parser.add_argument(
        "--writer-backend", choices=("python", "rust"), default="python"
    )
    parser.add_argument("--repetitions", type=int)
    parser.add_argument("--duration-seconds", type=float)
    parser.add_argument("--idle-seconds", type=float)
    parser.add_argument("--cooldown-seconds", type=float)
    parser.add_argument("--sample-interval-seconds", type=float)
    parser.add_argument("--concurrency", type=int)
    parser.add_argument("--small-writes", type=int)
    parser.add_argument("--large-writes", type=int)
    parser.add_argument("--mixed-writes", type=int)
    return parser, parser.parse_args()


def _requested_profile(args: argparse.Namespace) -> dict[str, int | float | None]:
    return {
        "repetitions": args.repetitions,
        "duration_seconds": args.duration_seconds,
        "idle_seconds": args.idle_seconds,
        "cooldown_seconds": args.cooldown_seconds,
        "sample_interval_seconds": args.sample_interval_seconds,
        "concurrency": args.concurrency,
        "small_writes": args.small_writes,
        "large_writes": args.large_writes,
        "mixed_writes": args.mixed_writes,
    }


def _validate_run_options(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    profile: dict[str, int | float],
) -> None:
    if profile["repetitions"] < 3 or profile["concurrency"] < 8:
        parser.error("migration benchmarks require at least 3 repetitions and c=8")
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("--output must be absent or empty")


def _copy_benchmark_fixture(
    source_instance: Path, baseline_instance: Path | None
) -> None:
    if baseline_instance is None:
        source_instance.parent.mkdir(parents=True, exist_ok=True)
        source_instance.mkdir(parents=True, exist_ok=False)
        with tempfile.TemporaryDirectory(
            prefix="writer-baseline-export-", dir=source_instance.parent
        ) as staging_dir:
            staging_instance = Path(staging_dir) / "instance"
            export_writer_parity_instance(staging_instance, benchmark_post_count=200)
            for child in staging_instance.iterdir():
                if child.name == "sqlite3.db":
                    continue
                target = source_instance / child.name
                if child.is_dir():
                    shutil.copytree(child, target)
                else:
                    shutil.copy2(child, target)
            _sqlite_backup(
                staging_instance / "sqlite3.db",
                source_instance / "sqlite3.db",
                immutable_source=False,
            )
        # Canonical source is independent from exporter connections and has a
        # single self-contained database image before it is hashed.
        connection = sqlite3.connect(source_instance / "sqlite3.db")
        try:
            checkpoint = connection.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
            mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()
            if checkpoint is None or checkpoint[0] != 0:
                raise ValueError(f"benchmark source checkpoint was busy: {checkpoint}")
            if mode is None or mode[0].lower() != "delete":
                raise ValueError(f"benchmark source did not leave WAL mode: {mode}")
            if (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1"
                ).fetchone()
                is None
            ):
                raise ValueError("benchmark source contains no schema tables")
        finally:
            connection.close()
    else:
        shutil.copytree(baseline_instance, source_instance)
    _validate_fixture_schema(source_instance)
    _instance_tree_hash(source_instance)
    _make_instance_read_only(source_instance)


def _build_protocol(
    args: argparse.Namespace,
    profile: dict[str, int | float],
    source_instance: Path,
    baseline: dict[str, Any] | None,
    parser: argparse.ArgumentParser,
    threshold_report_sha256: str | None = None,
) -> dict[str, Any]:
    fixture_sha256 = _fixture_hash(source_instance)
    if (
        baseline is not None
        and fixture_sha256 != baseline["protocol"]["fixture_sha256"]
    ):
        parser.error("cloned P0 fixture does not match the recorded baseline hash")
    image_info = json.loads(_run(["docker", "image", "inspect", args.image]).stdout)[0]
    if (
        baseline is not None
        and not args.writer_baseline
        and baseline["protocol"].get("image_id") != image_info["Id"]
    ):
        parser.error("paired Python and Rust runs must use the exact same image ID")
    protocol = {
        "image": args.image,
        "image_id": image_info["Id"],
        "fixture_sha256": fixture_sha256,
        "fixture_tree_sha256": _instance_tree_hash(source_instance),
        "writer_backend": args.writer_backend,
        "baseline_mode": "replacement-python-p0" if args.writer_baseline else None,
        "environment_fingerprint": _environment_fingerprint(),
        "runner_sha256": _file_hash(Path(__file__)),
        "resource_sampling": RESOURCE_SAMPLING_POLICY,
        "writer_cpu_window": WRITER_CPU_WINDOW_POLICY,
        "warmed_idle_seconds": profile["idle_seconds"],
        **profile,
        "paired_baseline_report": (
            str(args.baseline_report.resolve()) if args.baseline_report else None
        ),
        "paired_baseline_report_sha256": (
            _file_hash(args.baseline_report) if args.baseline_report else None
        ),
        "historical_threshold_report": (
            str(args.threshold_report.resolve()) if args.threshold_report else None
        ),
        "historical_threshold_report_sha256": threshold_report_sha256,
        "environment_names": sorted(
            [
                "PODLY_INSTANCE_DIR",
                "REQUIRE_AUTH",
                "SERVER_THREADS",
                "PODLY_WRITER_BACKEND",
                "PODLY_WRITER_TIMING_LOG",
                "PODLY_WRITER_IDLE_TRIM_INTERVAL_SEC",
                *RUST_FLAGS,
            ]
        ),
    }
    return protocol


def _execute_repetitions(
    args: argparse.Namespace,
    profile: dict[str, int | float],
    fixture_sha256: str,
    fixture_tree_sha256: str,
) -> list[dict[str, Any]]:
    runs = []
    for index in range(1, int(profile["repetitions"]) + 1):
        result = run_repetition(
            image=args.image,
            output=args.output,
            index=index,
            duration=profile["duration_seconds"],
            idle_seconds=profile["idle_seconds"],
            cooldown_seconds=profile["cooldown_seconds"],
            sample_interval=profile["sample_interval_seconds"],
            concurrency=profile["concurrency"],
            small_writes=profile["small_writes"],
            large_writes=profile["large_writes"],
            mixed_writes=profile["mixed_writes"],
            writer_backend=args.writer_backend,
            fixture_sha256=fixture_sha256,
            fixture_tree_sha256=fixture_tree_sha256,
        )
        runs.append(result)
        (args.output / f"run-{index}.json").write_text(json.dumps(result, indent=2))
    return runs


REQUIRED_RESOURCE_PHASES = {
    "idle",
    "warmup",
    "warmed_idle",
    "http_feed_posts",
    "http_rss",
    "writer_small",
    "writer_large",
    "writer_mixed",
    "cooldown_http_feed_posts",
    "cooldown_http_rss",
    "cooldown_writer_small",
    "cooldown_writer_large",
    "cooldown_writer_mixed",
}


def _check_resource_samples(run: dict[str, Any]) -> list[str]:
    resources = run.get("resources", {})
    if not isinstance(resources, dict):
        return ["benchmark resource measurements are malformed"]
    missing = sorted(
        phase
        for phase in REQUIRED_RESOURCE_PHASES
        if not isinstance(resources.get(phase), dict)
        or resources[phase].get("samples", 0) <= 0
    )
    return [f"missing resource samples for phases: {missing}"] if missing else []


def _check_http_measurements(run: dict[str, Any]) -> list[str]:
    workloads = run.get("http", {})
    if not isinstance(workloads, dict):
        return ["HTTP workload measurements are malformed"]
    errors = []
    if set(workloads) != {"feed_posts", "rss"}:
        errors.append("benchmark run is missing an HTTP workload")
    for workload in workloads.values():
        if not isinstance(workload, dict):
            errors.append("HTTP workload measurements are malformed")
        elif not workload.get("count") or set(workload.get("status_codes", {})) != {
            200
        }:
            errors.append(
                f"unexpected or empty HTTP measurements: {workload.get('status_codes', {})}"
            )
    return errors


def _check_writer_workload(workload: Any) -> list[str]:
    if not isinstance(workload, dict):
        return ["writer workload measurements are malformed"]
    client = workload.get("client", {})
    timing = workload.get("writer_timing", {})
    if not isinstance(client, dict) or not isinstance(timing, dict):
        return ["writer workload measurements are malformed"]
    errors = []
    if client.get("successes", 0) <= 0:
        errors.append("writer workload completed no commands")
    if client.get("failures") or timing.get("failures"):
        errors.append("writer workload failure")
    if timing.get("count", 0) != client.get("successes", -1):
        errors.append("writer timing record count does not match successful commands")
    return errors


def _check_writer_measurements(run: dict[str, Any]) -> list[str]:
    workloads = run.get("writer", {})
    if not isinstance(workloads, dict):
        return ["writer workload measurements are malformed"]
    errors = []
    if set(workloads) != {"small", "large", "mixed"}:
        errors.append("benchmark run is missing a writer workload")
    for workload in workloads.values():
        errors.extend(_check_writer_workload(workload))
    return errors


def _check_writer_identity(run: dict[str, Any], backend: str) -> list[str]:
    errors = []
    if run.get("writer_backend_requested") != backend:
        errors.append("writer benchmark request does not match selected backend")
    if run.get("writer_backend_environment") != backend:
        errors.append("container writer backend environment does not match selection")
    if run.get("writer_backend_process_identity") != backend:
        errors.append("writer process identity does not match selected backend")
    if backend == "rust":
        if run.get("writer_readiness_probe") is not True:
            errors.append("Rust writer protocol/schema/executor readiness probe failed")
        probe = run.get("rust_probe")
        if not isinstance(probe, dict) or any(
            not isinstance(probe.get(key), int) or probe[key] <= 0
            for key in ("feed_posts_bytes", "feed_xml_bytes")
        ):
            errors.append("Rust feed/RSS paths did not return nonempty bytes")
    return errors


def _collect_run_errors(runs: list[dict[str, Any]], writer_backend: str) -> list[str]:
    if not runs:
        return ["benchmark report contains no runs"]
    errors = []
    for run in runs:
        fallback_lines = run.get("fallback_lines", [])
        if isinstance(fallback_lines, list):
            errors.extend(fallback_lines)
        elif fallback_lines:
            errors.append("benchmark fallback log data is malformed")
        errors.extend(_check_resource_samples(run))
        errors.extend(_check_http_measurements(run))
        errors.extend(_check_writer_measurements(run))
        errors.extend(_check_writer_identity(run, writer_backend))
        if writer_backend == "rust":
            trim_evidence = run.get("writer_memory_trim")
            records = (
                trim_evidence.get("records")
                if isinstance(trim_evidence, dict)
                else None
            )
            if (
                not isinstance(trim_evidence, dict)
                or trim_evidence.get("status") != "success"
                or not isinstance(records, list)
                or not any(
                    isinstance(record, dict)
                    and record.get("kind") == "arena_purge"
                    and record.get("status") == "success"
                    and record.get("rc") == 0
                    for record in records
                )
            ):
                errors.append(
                    "Rust writer memory trim did not report arena_purge=all rc=0"
                )
    return errors


def _comparison_errors(comparison: dict[str, Any]) -> list[str]:
    return [
        f"P0 acceptance gate failed: {name}={gate.get('observed')}"
        for name, gate in comparison["gates"].items()
        if not gate["passed"]
    ]


if __name__ == "__main__":
    raise SystemExit(main())
