from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from scripts.bench_service_migration import (
    EXPECTED_SCHEMA_REVISION,
    P0_ACCEPTANCE,
    P0_PROFILE,
    RESOURCE_SAMPLING_POLICY,
    WRITER_CPU_WINDOW_POLICY,
    Sampler,
    _build_protocol,
    _check_resource_samples,
    _collect_run_errors,
    _container_cpu_seconds,
    _cpu_measurement,
    _environment_fingerprint,
    _fixture_hash,
    _instance_tree_hash,
    _python_fallback_lines,
    _resources_with_cpu,
    _select_profile,
    _verify_fixture_hash,
    _verify_fixture_tree,
    _warm_idle_window,
    _with_cpu_measurement,
    _writer_cpu_measurement,
    _writer_memory_trim_evidence,
    _writer_workload_with_cpu,
    clone_instance,
    compare_to_p0,
    paired_profile,
    parse_size_bytes,
    parse_writer_timing,
    quantiles,
    rust_writer_readiness_probe,
    summarize_resources,
)
from scripts.bench_service_migration import (
    main as benchmark_main,
)
from scripts.bench_writer_service import (
    _cpu_window_measurement,
    _parse_cgroup_cpu_seconds,
    _read_cgroup_cpu_seconds,
)


@pytest.mark.parametrize("v1", [False, True])
def test_container_cpu_counter_reads_cumulative_usage(
    monkeypatch: pytest.MonkeyPatch, v1: bool
) -> None:
    replies = iter(
        [
            SimpleNamespace(returncode=1, stdout="", stderr="No such file"),
            SimpleNamespace(returncode=0, stdout="2500000000\n", stderr=""),
        ]
        if v1
        else [SimpleNamespace(returncode=0, stdout="usage_usec 2500000\n", stderr="")]
    )
    monkeypatch.setattr(
        "scripts.bench_service_migration._run", lambda _args, **_kwargs: next(replies)
    )
    assert _container_cpu_seconds("isolated-test") == 2.5


def test_container_cpu_counter_does_not_hide_permission_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def denied(args: list[str], **_kwargs: Any) -> SimpleNamespace:
        calls.append(args)
        return SimpleNamespace(returncode=1, stdout="", stderr="Permission denied")

    monkeypatch.setattr("scripts.bench_service_migration._run", denied)
    with pytest.raises(ValueError, match="cannot read"):
        _container_cpu_seconds("isolated-test")
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("v2_stat", "v1_usage", "expected"),
    [
        ("usage_usec 2500000\nuser_usec 500000", None, 2.5),
        ("", "2500000000\n", 2.5),
    ],
)
def test_writer_helper_parses_cgroup_v1_and_v2_cpu_counters(
    v2_stat: str, v1_usage: str | None, expected: float
) -> None:
    assert _parse_cgroup_cpu_seconds(v2_stat, v1_usage) == expected


@pytest.mark.parametrize(
    ("v2_stat", "v1_usage"),
    [
        ("user_usec 1\n", None),
        ("usage_usec invalid\n", None),
        ("", "invalid\n"),
    ],
)
def test_writer_helper_rejects_missing_or_invalid_cgroup_counters(
    v2_stat: str, v1_usage: str | None
) -> None:
    with pytest.raises(ValueError, match="cgroup"):
        _parse_cgroup_cpu_seconds(v2_stat, v1_usage)


def test_writer_helper_reads_v1_only_when_v2_counter_file_is_missing(
    tmp_path: Path,
) -> None:
    v2_path = tmp_path / "missing-cpu.stat"
    v1_path = tmp_path / "cpuacct.usage"
    v1_path.write_text("2500000000\n")

    assert _read_cgroup_cpu_seconds(v2_path, v1_path) == 2.5

    v2_path.write_text("user_usec 1\n")
    with pytest.raises(ValueError, match="cgroup v2 CPU usage counter"):
        _read_cgroup_cpu_seconds(v2_path, v1_path)


def test_writer_helper_fails_when_both_cgroup_counter_files_are_missing(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="cannot read cgroup v1 CPU accounting"):
        _read_cgroup_cpu_seconds(tmp_path / "missing-v2", tmp_path / "missing-v1")


@pytest.mark.parametrize(
    ("before", "after", "elapsed"),
    [(1.0, 1.0, 1.0), (2.0, 1.0, 1.0), (1.0, 2.0, 0.0), (1.0, float("inf"), 1.0)],
)
def test_writer_helper_rejects_invalid_cpu_windows(
    before: float, after: float, elapsed: float
) -> None:
    with pytest.raises(ValueError, match="writer CPU accounting"):
        _cpu_window_measurement(before, after, elapsed)


@pytest.mark.parametrize(
    ("before", "after", "elapsed"),
    [(1.0, 1.0, 1.0), (2.0, 1.0, 1.0), (1.0, 2.0, 0.0), (1.0, float("nan"), 1.0)],
)
def test_cpu_accounting_rejects_zero_missing_or_invalid_usage(
    before: float, after: float, elapsed: float
) -> None:
    with pytest.raises(ValueError, match="CPU accounting"):
        _cpu_measurement(before, after, elapsed)


def test_short_workload_cpu_comes_from_counters_not_a_later_idle_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counters = iter([8.0, 10.0])
    monkeypatch.setattr(
        "scripts.bench_service_migration._container_cpu_seconds",
        lambda _name: next(counters),
    )
    workload = {"client": {"elapsed_seconds": 0.5}}
    result, measurement = _with_cpu_measurement("isolated-test", lambda: workload)
    assert result is workload
    assert measurement == {
        "cpu_seconds_total": 2.0,
        "cpu_window_elapsed_seconds": 0.5,
        "cpu_percent_mean": 400.0,
    }


def test_writer_cpu_uses_helper_window_and_does_not_read_host_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = {
        "client": {
            "elapsed_seconds": 0.5,
            "cpu_seconds_total": 1.0,
            "cpu_window_elapsed_seconds": 0.5,
        }
    }
    monkeypatch.setattr(
        "scripts.bench_service_migration.writer_workload",
        lambda *_args: result,
    )

    def host_counter_must_not_run(_name: str) -> float:
        raise AssertionError("writer phases must use the helper CPU window")

    monkeypatch.setattr(
        "scripts.bench_service_migration._container_cpu_seconds",
        host_counter_must_not_run,
    )
    measured_result, measurement = _writer_workload_with_cpu(
        "isolated-test", "small", 100, 8, "rust"
    )

    assert measured_result is result
    assert measurement == {
        "cpu_seconds_total": 1.0,
        "cpu_window_elapsed_seconds": 0.5,
        "cpu_percent_mean": 200.0,
    }


def test_writer_cpu_measurement_fails_on_missing_helper_fields() -> None:
    with pytest.raises(ValueError, match="writer helper CPU window"):
        _writer_cpu_measurement({"elapsed_seconds": 0.5})


def test_resources_use_actual_cpu_delta_and_keep_idle_probe_average_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "scripts.bench_service_migration.summarize_resources",
        lambda _samples: {"writer_large": {"cpu_percent_mean": 0.0}},
    )
    resources = _resources_with_cpu(
        [], {"writer_large": {"cpu_seconds_total": 0.5, "cpu_percent_mean": 250.0}}
    )
    assert resources["writer_large"]["cpu_percent_mean"] == 250.0
    assert resources["writer_large"]["cpu_seconds_total"] == 0.5
    assert resources["writer_large"]["cpu_percent_periodic_mean"] == 0.0


def test_sampler_records_a_phase_shorter_than_its_periodic_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "scripts.bench_service_migration.resource_sample",
        lambda _name, phase: {"phase": phase},
    )
    sampler = Sampler("isolated-test", 60)
    # No periodic tick occurs during this simulated short burst.
    sampler._started = True
    sampler.phase = "writer_large"
    sampler.phase = "cooldown_writer_large"
    assert [sample["phase"] for sample in sampler.samples] == ["idle", "writer_large"]


def test_warmed_idle_window_runs_after_endpoint_warmups_and_captures_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, Any]] = []

    class FakeSampler:
        @property
        def phase(self) -> str | None:
            return getattr(self, "_phase", None)

        @phase.setter
        def phase(self, value: str) -> None:
            self._phase = value
            events.append(("phase", value))

    monkeypatch.setattr(
        "scripts.bench_service_migration._request",
        lambda url, _cookie: events.append(("request", url)),
    )
    monkeypatch.setattr(
        "scripts.bench_service_migration.time.sleep",
        lambda seconds: events.append(("sleep", seconds)),
    )

    def process_snapshot(name: str) -> list[dict[str, Any]]:
        events.append(("snapshot", name))
        return [{"rss_bytes": 123}]

    monkeypatch.setattr(
        "scripts.bench_service_migration._process_snapshot", process_snapshot
    )

    processes = _warm_idle_window(
        "isolated-test",
        cast(Sampler, FakeSampler()),
        "http://example.invalid",
        "cookie",
        30.0,
    )

    assert len([event for event in events if event[0] == "request"]) == 20
    assert events[0] == ("phase", "warmup")
    assert events[21:] == [
        ("sleep", 2),
        ("phase", "warmed_idle"),
        ("sleep", 30.0),
        ("snapshot", "isolated-test"),
    ]
    assert processes == [{"rss_bytes": 123}]


@pytest.mark.parametrize("baseline_mode", [True, False])
def test_only_python_baseline_refresh_can_reuse_a_fixture_from_an_older_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, baseline_mode: bool
) -> None:
    (tmp_path / "sqlite3.db").write_bytes(b"isolated-test-fixture")
    fixture_hash = _fixture_hash(tmp_path)
    baseline = {"protocol": {"fixture_sha256": fixture_hash, "image_id": "old-image"}}
    monkeypatch.setattr(
        "scripts.bench_service_migration._run",
        lambda _args: SimpleNamespace(stdout=json.dumps([{"Id": "new-image"}])),
    )
    args = argparse.Namespace(
        image="new-isolated-image",
        writer_baseline=baseline_mode,
        writer_backend="python" if baseline_mode else "rust",
        baseline_report=None,
        threshold_report=None,
    )
    parser = argparse.ArgumentParser()
    if baseline_mode:
        protocol = _build_protocol(args, P0_PROFILE, tmp_path, baseline, parser)
        assert protocol["image_id"] == "new-image"
        assert protocol["fixture_sha256"] == fixture_hash
        assert protocol["resource_sampling"] == RESOURCE_SAMPLING_POLICY
        assert len(protocol["runner_sha256"]) == 64
    else:
        with pytest.raises(SystemExit):
            _build_protocol(args, P0_PROFILE, tmp_path, baseline, parser)


def test_sampler_phase_transition_waits_for_an_inflight_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def sample(_name: str, phase: str) -> dict[str, str]:
        entered.set()
        assert release.wait(timeout=2)
        return {"phase": phase}

    monkeypatch.setattr("scripts.bench_service_migration.resource_sample", sample)
    sampler = Sampler("isolated-test", 60)
    sampler.start()
    transition = threading.Thread(
        target=lambda: setattr(sampler, "phase", "writer_large"),
    )
    try:
        assert entered.wait(timeout=2)
        transition.start()
        assert sampler.phase == "idle"
        release.set()
        transition.join(timeout=2)
        assert not transition.is_alive()
        assert sampler.phase == "writer_large"
        assert all(item["phase"] == "idle" for item in sampler.samples)
    finally:
        release.set()
        sampler.close()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1KiB", 1024), ("1.5MiB", 1_572_864), ("2 MB", 2_000_000)],
)
def test_parse_size_bytes(raw: str, expected: int) -> None:
    assert parse_size_bytes(raw) == expected


def test_quantiles_report_expected_keys() -> None:
    result = quantiles([float(value) for value in range(1, 101)])
    assert result == {"p50": 50.5, "p95": 95.05, "p99": 99.01, "max": 100.0}


def test_parse_writer_timing_filters_actions_and_counts_failures() -> None:
    good = {
        "action": "replace_transcription",
        "queue_ms": 2.0,
        "execution_ms": 8.0,
        "total_ms": 10.0,
        "success": True,
    }
    failed = {**good, "queue_ms": 4.0, "success": False}
    ignored = {**good, "action": "dequeue_job"}
    logs = "\n".join(
        f'INFO [WRITER_TIMING] {json.dumps(row)} | extra={{"taskName":null}}'
        for row in (good, failed, ignored)
    )

    result = parse_writer_timing(logs, {"replace_transcription"})

    assert result["count"] == 2
    assert result["failures"] == 1
    assert result["queue_ms"]["p50"] == 3.0


def test_python_fallback_scan_catches_writer_and_read_path_fallbacks() -> None:
    logs = "\n".join(
        (
            "INFO Rust feed posts failed; falling back to Python behavior",
            "ERROR Rust writer unavailable; falling back to Python behavior",
            "INFO request completed",
        )
    )

    assert len(_python_fallback_lines(logs)) == 2


def test_writer_memory_trim_evidence_is_allowlisted_and_redacts_unrelated_logs() -> (
    None
):
    evidence = _writer_memory_trim_evidence(
        "\n".join(
            (
                "[WRITER_MEMORY_TRIM] jemalloc_mallctl=available",
                "[WRITER_MEMORY_TRIM] arena_purge=all rc=0",
                "INFO request payload contains secret-value",
                "[WRITER_MEMORY_TRIM] arena_purge=all rc=0 extra=secret-value",
            )
        ),
        "rust",
    )

    assert evidence == {
        "status": "success",
        "records": [
            {"kind": "jemalloc_mallctl", "status": "available"},
            {"kind": "arena_purge", "status": "success", "rc": 0},
        ],
    }
    assert "secret-value" not in json.dumps(evidence)


def test_writer_memory_trim_evidence_records_failure_missing_and_python_optional() -> (
    None
):
    failure = _writer_memory_trim_evidence(
        "[WRITER_MEMORY_TRIM] arena_purge=failed rc=5", "rust"
    )
    missing = _writer_memory_trim_evidence("unrelated log", "rust")
    python = _writer_memory_trim_evidence("unrelated log", "python")

    assert failure == {
        "status": "failure",
        "records": [{"kind": "arena_purge", "status": "failure", "rc": 5}],
    }
    assert missing == {"status": "missing", "records": []}
    assert python == {"status": "not_required", "records": []}


def _write_baseline_report(
    directory: Path,
    *,
    runs: object = None,
    revision: str = EXPECTED_SCHEMA_REVISION,
    repetitions: int = 3,
) -> Path:
    source = directory / "source" / "instance"
    source.mkdir(parents=True)
    connection = sqlite3.connect(source / "sqlite3.db")
    try:
        connection.execute("CREATE TABLE alembic_version(version_num TEXT NOT NULL)")
        connection.execute("INSERT INTO alembic_version VALUES (?)", (revision,))
        connection.commit()
    finally:
        connection.close()

    report = directory / "report.json"
    report.write_text(
        json.dumps(
            {
                "protocol": {
                    **{**P0_PROFILE, "repetitions": repetitions},
                    "fixture_sha256": hashlib.sha256(
                        (source / "sqlite3.db").read_bytes()
                    ).hexdigest(),
                    "fixture_tree_sha256": _instance_tree_hash(source),
                    "image_id": "sha256:synthetic-test-image",
                },
                "runs": [{"run": index} for index in range(1, 4)]
                if runs is None
                else runs,
            }
        ),
        encoding="utf-8",
    )
    return report


def test_paired_profile_uses_the_exact_baseline_profile(tmp_path: Path) -> None:
    report_path = _write_baseline_report(tmp_path)

    profile, source_instance, report = paired_profile(report_path, {})

    assert profile == P0_PROFILE
    assert source_instance == tmp_path / "source" / "instance"
    assert report is not None and len(report["runs"]) == 3


def test_python_baseline_can_reuse_v2_report_as_frozen_fixture_source(
    tmp_path: Path,
) -> None:
    report_path = _write_baseline_report(tmp_path)
    old_report = json.loads(report_path.read_text(encoding="utf-8"))
    old_report["protocol"]["resource_sampling"] = "periodic-phase-end-cumulative-cpu-v2"
    report_path.write_text(json.dumps(old_report), encoding="utf-8")
    args = argparse.Namespace(
        writer_baseline=True,
        writer_backend="python",
        repetitions=None,
        duration_seconds=None,
        idle_seconds=None,
        cooldown_seconds=None,
        sample_interval_seconds=None,
        concurrency=None,
        small_writes=None,
        large_writes=None,
        mixed_writes=None,
        baseline_report=report_path,
    )

    profile, source_instance, report = _select_profile(args)

    assert profile == P0_PROFILE
    assert source_instance == tmp_path / "source" / "instance"
    assert report is not None


def test_paired_profile_rejects_schema_revision_mismatch(tmp_path: Path) -> None:
    report_path = _write_baseline_report(tmp_path, revision="wrong-revision")

    with pytest.raises(ValueError, match="Alembic revision mismatch"):
        paired_profile(report_path, {})


def test_fixture_clones_are_byte_identical_and_hash_guard_rejects_mutation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "canonical" / "instance"
    source.mkdir(parents=True)
    database = source / "sqlite3.db"
    import sqlite3

    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE fixture (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO fixture(value) VALUES ('synthetic')")
        connection.commit()
    finally:
        connection.close()
    expected = _fixture_hash(source)

    first = tmp_path / "run-1" / "instance"
    second = tmp_path / "run-2" / "instance"
    clone_instance(source, first)
    clone_instance(source, second)

    assert (first / "sqlite3.db").resolve() != (second / "sqlite3.db").resolve()
    assert (first / "sqlite3.db").read_bytes() == database.read_bytes()
    assert (second / "sqlite3.db").read_bytes() == database.read_bytes()
    assert first != second
    assert _instance_tree_hash(first) == _instance_tree_hash(second)
    _verify_fixture_hash(source, expected, "after clone")

    database.write_bytes(database.read_bytes() + b"mutated")
    with pytest.raises(
        ValueError, match="immutable benchmark fixture changed after run"
    ):
        _verify_fixture_tree(source, expected, _instance_tree_hash(first), "after run")


def test_writer_baseline_rejects_rust_backend_before_running(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bench_service_migration.py",
            "--image",
            "unused",
            "--output",
            "/tmp/unused",
            "--writer-baseline",
            "--writer-backend",
            "rust",
        ],
    )

    with pytest.raises(SystemExit) as error:
        benchmark_main()

    assert error.value.code == 2


def test_paired_profile_rejects_a_profile_override(tmp_path: Path) -> None:
    report_path = _write_baseline_report(tmp_path)

    with pytest.raises(ValueError, match="differs from P0 baseline"):
        paired_profile(report_path, {"concurrency": 16})


def test_paired_profile_accepts_exact_five_run_expansion(tmp_path: Path) -> None:
    report_path = _write_baseline_report(
        tmp_path,
        runs=[{"run": index} for index in range(1, 6)],
        repetitions=5,
    )

    profile, _source, report = paired_profile(report_path, {})

    assert profile["repetitions"] == 5
    assert report is not None and len(report["runs"]) == 5


def test_paired_profile_rejects_a_baseline_that_changes_the_fixed_p0_profile(
    tmp_path: Path,
) -> None:
    report_path = _write_baseline_report(tmp_path)
    baseline = json.loads(report_path.read_text(encoding="utf-8"))
    baseline["protocol"]["duration_seconds"] = 60
    report_path.write_text(json.dumps(baseline), encoding="utf-8")

    with pytest.raises(ValueError, match="fixed 3/5-run P0 profile"):
        paired_profile(report_path, {})


@pytest.mark.parametrize("runs", [None, [], [{"run": 1}, "not-an-object", {}]])
def test_paired_profile_rejects_incomplete_baseline_runs(
    tmp_path: Path, runs: object
) -> None:
    report_path = _write_baseline_report(
        tmp_path,
        runs=[] if runs is None else runs,
    )

    with pytest.raises(ValueError, match=r"3/5-run profile|JSON objects"):
        paired_profile(report_path, {})


def test_summarize_resources_rejects_an_empty_sample_set() -> None:
    with pytest.raises(ValueError, match="zero samples"):
        summarize_resources([])


def _complete_run() -> dict[str, Any]:
    resources = {
        phase: {"samples": 1}
        for phase in (
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
        )
    }
    writer_workload = {
        "client": {"failures": 0, "successes": 1},
        "writer_timing": {"failures": 0, "count": 1},
    }
    return {
        "fallback_lines": [],
        "resources": resources,
        "http": {
            "feed_posts": {"count": 1, "status_codes": {200: 1}},
            "rss": {"count": 1, "status_codes": {200: 1}},
        },
        "writer": {
            "small": writer_workload,
            "large": writer_workload,
            "mixed": writer_workload,
        },
        "writer_backend_requested": "rust",
        "writer_backend_environment": "rust",
        "writer_backend_process_identity": "rust",
        "writer_memory_trim": {
            "status": "success",
            "records": [{"kind": "arena_purge", "status": "success", "rc": 0}],
        },
        "writer_readiness_probe": True,
        "rust_probe": {"feed_posts_bytes": 10, "feed_xml_bytes": 10},
    }


def test_rust_run_requires_backend_selection_probe_and_complete_samples() -> None:
    run = _complete_run()

    assert _collect_run_errors([run], "rust") == []

    incomplete = _complete_run()
    incomplete["writer_readiness_probe"] = False
    incomplete["resources"] = {"idle": {"samples": 0}}
    incomplete["rust_probe"] = {"feed_posts_bytes": 0, "feed_xml_bytes": 0}
    errors = _collect_run_errors([incomplete], "rust")
    assert "Rust writer protocol/schema/executor readiness probe failed" in errors
    assert any("missing resource samples" in error for error in errors)
    assert "Rust feed/RSS paths did not return nonempty bytes" in errors


def test_rust_run_requires_warmed_idle_resource_samples() -> None:
    run = _complete_run()
    run["resources"].pop("warmed_idle")

    errors = _check_resource_samples(run)

    assert any("warmed_idle" in error for error in errors)


def test_trim_success_is_required_only_for_rust_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "_check_resource_samples",
        "_check_http_measurements",
        "_check_writer_measurements",
        "_check_writer_identity",
    ):
        monkeypatch.setattr(
            f"scripts.bench_service_migration.{name}", lambda _run, *_args: []
        )
    run = {"writer_memory_trim": {"status": "missing", "records": []}}

    rust_errors = _collect_run_errors([run], "rust")

    assert "Rust writer memory trim did not report arena_purge=all rc=0" in rust_errors
    assert _collect_run_errors([run], "python") == []


def test_rust_writer_readiness_probe_reports_command_success(monkeypatch) -> None:
    seen: list[list[str]] = []

    def fake_run(args: list[str], *, check: bool = True):
        seen.append(args)
        assert check is False
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("scripts.bench_service_migration._run", fake_run)

    assert rust_writer_readiness_probe("isolated-benchmark") is True
    assert seen == [
        ["docker", "exec", "isolated-benchmark", "/app/bin/podly_writer", "--probe"]
    ]


def test_compare_to_p0_turns_missing_measurements_into_failed_gate() -> None:
    comparison = compare_to_p0({}, {})

    assert comparison["passed"] is False
    assert comparison["gates"]["complete_measurements"]["passed"] is False


def _comparison_report(
    *,
    backend: str,
    idle_memory_bytes: int,
    writer_rss_bytes: int,
    warmed_idle_memory_bytes: int | None = None,
) -> dict[str, Any]:
    runs = []
    phases = (
        "http_feed_posts",
        "http_rss",
        "writer_small",
        "writer_large",
        "writer_mixed",
    )
    cooldowns = (
        "cooldown_http_feed_posts",
        "cooldown_http_rss",
        "cooldown_writer_small",
        "cooldown_writer_large",
        "cooldown_writer_mixed",
    )
    for index in range(1, 4):
        resources: dict[str, object] = {
            "warmup": {"samples": 1},
            "idle": {
                "memory_bytes_median": idle_memory_bytes,
                "threads_max": 45,
                "fds_max": 60,
            },
            "warmed_idle": {
                "samples": 1,
                "memory_bytes_median": (
                    idle_memory_bytes
                    if warmed_idle_memory_bytes is None
                    else warmed_idle_memory_bytes
                ),
            },
        }
        resources.update({phase: {"cpu_percent_mean": 10.0} for phase in phases})
        resources.update(
            {
                phase: {"memory_bytes_max": idle_memory_bytes + 1024}
                for phase in cooldowns
            }
        )
        http = {
            name: {
                "latency_ms": {"p95": 10.0, "p99": 12.0},
                "throughput_per_second": 400.0 if name == "feed_posts" else 20.0,
            }
            for name in ("feed_posts", "rss")
        }
        writer = {}
        for name in ("small", "large", "mixed"):
            writer[name] = {
                "client": {
                    "latency_ms": {"p95": 10.0, "p99": 12.0},
                    "throughput_per_second": 100.0,
                },
                "writer_timing": {"queue_ms": {"p95": 5.0, "p99": 8.0}},
            }
        writer_command = (
            "podly_writer" if backend == "rust" else "python3 -m app.writer"
        )
        runs.append(
            {
                "run": index,
                "resources": resources,
                "http": http,
                "writer": writer,
                "processes_after_cooldown": [
                    {
                        "command": writer_command,
                        "rss_bytes": writer_rss_bytes,
                        "threads": 6,
                        "fds": 14,
                    },
                    {
                        "command": "python3 /app/src/main.py",
                        "rss_bytes": 50_000_000,
                        "threads": 35,
                        "fds": 28,
                    },
                ],
                "writer_backend_process_identity": backend,
                "fallback_lines": [],
            }
        )
    return {
        "protocol": {
            **P0_PROFILE,
            "writer_backend": backend,
            "image_id": "sha256:synthetic-test-image",
            "fixture_sha256": "fixture-sha",
            "fixture_tree_sha256": "tree-sha",
            "environment_fingerprint": _environment_fingerprint(),
            "runner_sha256": "synthetic-test-runner-sha",
            "resource_sampling": RESOURCE_SAMPLING_POLICY,
            "writer_cpu_window": WRITER_CPU_WINDOW_POLICY,
            "warmed_idle_seconds": 30.0,
        },
        "runs": runs,
    }


def test_compare_to_p0_passes_all_writer_acceptance_thresholds() -> None:
    baseline = _comparison_report(
        backend="python", idle_memory_bytes=200_000_000, writer_rss_bytes=100_000_000
    )
    current = _comparison_report(
        backend="rust", idle_memory_bytes=100_000_000, writer_rss_bytes=40_000_000
    )

    comparison = compare_to_p0(baseline, current)

    assert comparison["passed"] is True
    assert all(gate["passed"] for gate in comparison["gates"].values())


def test_compare_to_p0_fails_threshold_regressions() -> None:
    baseline = _comparison_report(
        backend="python", idle_memory_bytes=200_000_000, writer_rss_bytes=100_000_000
    )
    current = _comparison_report(
        backend="rust", idle_memory_bytes=130_652_571, writer_rss_bytes=50_000_000
    )

    comparison = compare_to_p0(baseline, current)

    assert comparison["passed"] is False
    assert comparison["gates"]["idle_container_memory"]["passed"] is False
    assert comparison["gates"]["rust_writer_rss"]["passed"] is False


def test_warmed_idle_gates_apply_original_cap_and_paired_reduction() -> None:
    paired = _comparison_report(
        backend="python",
        idle_memory_bytes=200_000_000,
        writer_rss_bytes=100_000_000,
        warmed_idle_memory_bytes=150_000_000,
    )
    current = _comparison_report(
        backend="rust",
        idle_memory_bytes=100_000_000,
        writer_rss_bytes=40_000_000,
        warmed_idle_memory_bytes=131_000_000,
    )

    comparison = compare_to_p0(paired, current)

    assert comparison["gates"]["idle_container_memory"]["passed"] is True
    assert comparison["gates"]["warmed_idle_container_memory"]["passed"] is False
    assert comparison["gates"]["paired_warmed_idle_container_memory"]["passed"] is False


def test_warmed_idle_paired_reduction_can_fail_when_absolute_cap_passes() -> None:
    paired = _comparison_report(
        backend="python",
        idle_memory_bytes=200_000_000,
        writer_rss_bytes=100_000_000,
        warmed_idle_memory_bytes=150_000_000,
    )
    current = _comparison_report(
        backend="rust",
        idle_memory_bytes=100_000_000,
        writer_rss_bytes=40_000_000,
        warmed_idle_memory_bytes=100_000_000,
    )

    comparison = compare_to_p0(paired, current)

    assert comparison["gates"]["warmed_idle_container_memory"]["passed"] is True
    assert comparison["gates"]["paired_warmed_idle_container_memory"]["passed"] is False


def test_cold_idle_recovery_gate_is_unchanged_when_warmed_idle_is_higher() -> None:
    paired = _comparison_report(
        backend="python", idle_memory_bytes=200_000_000, writer_rss_bytes=100_000_000
    )
    current = _comparison_report(
        backend="rust",
        idle_memory_bytes=100_000_000,
        writer_rss_bytes=40_000_000,
        warmed_idle_memory_bytes=125_000_000,
    )
    cold_recovery_limit = 100_000_000 + int(
        P0_ACCEPTANCE["recovery_memory_headroom_bytes"]
    )
    for run in current["runs"]:
        for phase, resources in run["resources"].items():
            if phase.startswith("cooldown_"):
                resources["memory_bytes_max"] = cold_recovery_limit + 1

    comparison = compare_to_p0(paired, current)

    assert comparison["gates"]["warmed_idle_container_memory"]["passed"] is True
    assert comparison["gates"]["post_burst_memory_recovery"]["passed"] is False


def test_compare_to_p0_requires_original_and_stricter_paired_cpu_gates() -> None:
    historical = _comparison_report(
        backend="python", idle_memory_bytes=200_000_000, writer_rss_bytes=100_000_000
    )
    paired = _comparison_report(
        backend="python", idle_memory_bytes=200_000_000, writer_rss_bytes=100_000_000
    )
    current = _comparison_report(
        backend="rust", idle_memory_bytes=100_000_000, writer_rss_bytes=40_000_000
    )
    for run in historical["runs"]:
        run["resources"]["http_feed_posts"]["cpu_percent_mean"] = 1.0

    comparison = compare_to_p0(paired, current, historical)

    assert (
        comparison["gates"]["cpu_efficiency_feed_posts_historical"]["passed"] is False
    )
    assert comparison["gates"]["cpu_efficiency_feed_posts_paired"]["passed"] is True
    assert comparison["passed"] is False


def test_paired_memory_gate_can_be_stricter_than_original_limit() -> None:
    paired = _comparison_report(
        backend="python", idle_memory_bytes=150_000_000, writer_rss_bytes=100_000_000
    )
    current = _comparison_report(
        backend="rust", idle_memory_bytes=120_000_000, writer_rss_bytes=40_000_000
    )

    comparison = compare_to_p0(paired, current)

    assert comparison["gates"]["idle_container_memory"]["passed"] is True
    assert comparison["gates"]["paired_idle_container_memory"]["passed"] is False


def test_three_run_spread_that_can_reverse_gate_requires_five_repetitions() -> None:
    historical = _comparison_report(
        backend="python", idle_memory_bytes=200_000_000, writer_rss_bytes=100_000_000
    )
    paired = _comparison_report(
        backend="python", idle_memory_bytes=200_000_000, writer_rss_bytes=100_000_000
    )
    current = _comparison_report(
        backend="rust", idle_memory_bytes=100_000_000, writer_rss_bytes=40_000_000
    )
    for report, observations in (
        (historical, [10.0, 10.0, 10.0]),
        (paired, [1.0, 10.0, 19.0]),
        (current, [45.0, 45.0, 45.0]),
    ):
        for run, observation in zip(report["runs"], observations, strict=True):
            run["http"]["feed_posts"]["latency_ms"]["p95"] = observation

    comparison = compare_to_p0(paired, current, historical)

    spread = comparison["gates"]["paired_baseline_spread"]
    assert spread["required_repetitions"] == 5
    assert spread["passed"] is False
    assert any(
        item["metric"] == "feed_posts_p95_ms_max" and item["could_reverse_gate"]
        for item in spread["metrics"]
    )


def test_paired_environment_rejects_missing_fingerprint() -> None:
    paired = _comparison_report(
        backend="python", idle_memory_bytes=200_000_000, writer_rss_bytes=100_000_000
    )
    current = _comparison_report(
        backend="rust", idle_memory_bytes=100_000_000, writer_rss_bytes=40_000_000
    )
    paired["protocol"].pop("environment_fingerprint")
    comparison = compare_to_p0(paired, current)
    assert comparison["passed"] is False
    assert comparison["gates"]["complete_measurements"]["passed"] is False


def test_paired_environment_rejects_old_writer_cpu_window_policy() -> None:
    paired = _comparison_report(
        backend="python", idle_memory_bytes=200_000_000, writer_rss_bytes=100_000_000
    )
    current = _comparison_report(
        backend="rust", idle_memory_bytes=100_000_000, writer_rss_bytes=40_000_000
    )
    paired["protocol"].pop("writer_cpu_window")

    comparison = compare_to_p0(paired, current)

    assert comparison["passed"] is False
    assert comparison["gates"]["complete_measurements"]["passed"] is False


def test_paired_environment_rejects_missing_or_mismatched_warmed_idle_window() -> None:
    paired = _comparison_report(
        backend="python", idle_memory_bytes=200_000_000, writer_rss_bytes=100_000_000
    )
    current = _comparison_report(
        backend="rust", idle_memory_bytes=100_000_000, writer_rss_bytes=40_000_000
    )
    paired["protocol"].pop("warmed_idle_seconds")

    comparison = compare_to_p0(paired, current)

    assert comparison["passed"] is False
    assert comparison["gates"]["complete_measurements"]["passed"] is False

    paired["protocol"]["warmed_idle_seconds"] = 29.0
    comparison = compare_to_p0(paired, current)

    assert comparison["passed"] is False
    assert comparison["gates"]["complete_measurements"]["passed"] is False


def test_paired_acceptance_rejects_v2_resource_sampling_policy() -> None:
    paired = _comparison_report(
        backend="python", idle_memory_bytes=200_000_000, writer_rss_bytes=100_000_000
    )
    current = _comparison_report(
        backend="rust", idle_memory_bytes=100_000_000, writer_rss_bytes=40_000_000
    )
    paired["protocol"]["resource_sampling"] = "periodic-phase-end-cumulative-cpu-v2"

    comparison = compare_to_p0(paired, current)

    assert comparison["passed"] is False
    assert comparison["gates"]["complete_measurements"]["passed"] is False
