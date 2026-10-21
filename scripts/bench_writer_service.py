#!/usr/bin/env python3
"""Generate correlated writer workloads from inside an isolated container."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.writer.client import writer_client
from app.writer.protocol import WriteResult


@dataclass(frozen=True)
class Sample:
    latency_ms: float
    success: bool
    error: str | None


def _parse_cgroup_cpu_seconds(v2_stat: str, v1_usage: str | None = None) -> float:
    """Parse cgroup v2 cpu.stat, or v1 cpuacct.usage when supplied."""
    if v1_usage is not None:
        value = v1_usage.strip()
        if not value.isdigit():
            raise ValueError("cgroup v1 CPU usage counter is invalid")
        return int(value) / 1_000_000_000

    for line in v2_stat.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] == "usage_usec":
            if not fields[1].isdigit():
                raise ValueError("cgroup v2 CPU usage counter is invalid")
            return int(fields[1]) / 1_000_000
    raise ValueError("cgroup v2 CPU usage counter is missing")


def _read_cgroup_cpu_seconds(
    v2_path: Path = Path("/sys/fs/cgroup/cpu.stat"),
    v1_path: Path = Path("/sys/fs/cgroup/cpuacct/cpuacct.usage"),
) -> float:
    try:
        return _parse_cgroup_cpu_seconds(v2_path.read_text())
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise RuntimeError(f"cannot read cgroup v2 CPU accounting: {exc}") from exc

    try:
        return _parse_cgroup_cpu_seconds("", v1_path.read_text())
    except OSError as exc:
        raise RuntimeError(f"cannot read cgroup v1 CPU accounting: {exc}") from exc


def _cpu_window_measurement(
    before: float, after: float, elapsed: float
) -> dict[str, float]:
    delta = after - before
    if not all(math.isfinite(value) for value in (before, after, elapsed)) or (
        before < 0 or delta <= 0 or elapsed <= 0
    ):
        raise ValueError("writer CPU accounting must increase over a positive duration")
    return {
        "cpu_seconds_total": delta,
        "cpu_window_elapsed_seconds": elapsed,
    }


def _large_payload(
    segment_count: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    segments = [
        {
            "sequence_num": index,
            "start_time": index * 1.25,
            "end_time": (index + 1) * 1.25,
            "text": f"Synthetic segment {index:05d} " + ("x" * 180),
            "speaker_label": "A" if index % 2 == 0 else "B",
        }
        for index in range(segment_count)
    ]
    words = [
        {
            "sequence_num": index,
            "words": [
                {
                    "word": f"synthetic-{index:05d}-" + ("w" * 220),
                    "start": index * 1.25,
                    "end": index * 1.25 + 0.75,
                    "score": 0.99,
                }
            ],
        }
        for index in range(segment_count)
    ]
    return segments, words


def _quantiles(values: list[float]) -> dict[str, float]:
    percentiles = statistics.quantiles(values, n=100, method="inclusive")
    return {
        "p50": round(statistics.median(values), 3),
        "p95": round(percentiles[94], 3),
        "p99": round(percentiles[98], 3),
        "max": round(max(values), 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workload", choices=("small", "large", "mixed"), required=True
    )
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--user-id", type=int, default=101)
    parser.add_argument("--post-id", type=int, default=302)
    parser.add_argument("--segment-count", type=int, default=2000)
    parser.add_argument("--measure-cgroup-cpu", action="store_true")
    args = parser.parse_args()
    if args.count < 1 or args.concurrency < 1 or args.segment_count < 1:
        parser.error("count, concurrency, and segment-count must be positive")

    writer_client.connect()
    large_segments, large_words = _large_payload(args.segment_count)

    def run_one(index: int) -> Sample:
        is_large = args.workload == "large" or (
            args.workload == "mixed" and index % 10 == 0
        )
        started = time.perf_counter()
        if is_large:
            result = writer_client.action(
                "replace_transcription",
                {
                    "post_id": args.post_id,
                    "segments": large_segments,
                    "transcript_word_timestamps": large_words,
                },
                wait=True,
            )
        else:
            result = writer_client.action(
                "update_user_last_active", {"user_id": args.user_id}, wait=True
            )
        latency_ms = (time.perf_counter() - started) * 1000
        if not isinstance(result, WriteResult):
            return Sample(latency_ms, False, "missing writer result")
        return Sample(latency_ms, result.success, result.error)

    started = time.perf_counter()
    cpu_before = _read_cgroup_cpu_seconds() if args.measure_cgroup_cpu else None
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=args.concurrency
    ) as executor:
        samples = list(executor.map(run_one, range(args.count)))
    cpu_after = _read_cgroup_cpu_seconds() if args.measure_cgroup_cpu else None
    elapsed = time.perf_counter() - started
    latencies = [sample.latency_ms for sample in samples]
    failures = [asdict(sample) for sample in samples if not sample.success]
    output = {
        "workload": args.workload,
        "count": args.count,
        "concurrency": args.concurrency,
        "segment_count": args.segment_count if args.workload != "small" else 0,
        "elapsed_seconds": round(elapsed, 6),
        "throughput_per_second": round(args.count / elapsed, 3),
        "latency_ms": _quantiles(latencies),
        "successes": args.count - len(failures),
        "failures": failures[:20],
    }
    if args.measure_cgroup_cpu:
        assert cpu_before is not None and cpu_after is not None
        output.update(_cpu_window_measurement(cpu_before, cpu_after, elapsed))
    print(json.dumps(output, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
