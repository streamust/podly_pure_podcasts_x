#!/bin/bash
set -Eeuo pipefail

# Parse command line arguments
RUN_INTEGRATION=false
RUN_WRITER_CONTAINER=false
RUN_WRITER_ROLLBACK=false
RUN_WRITER_BENCHMARK=false
RUN_WRITER_BASELINE=false
for arg in "$@"; do
    if [ "$arg" = "--int" ]; then
        RUN_INTEGRATION=true
    elif [ "$arg" = "--writer-container" ]; then
        RUN_WRITER_CONTAINER=true
    elif [ "$arg" = "--writer-rollback" ]; then
        RUN_WRITER_ROLLBACK=true
    elif [ "$arg" = "--writer-benchmark" ]; then
        RUN_WRITER_BENCHMARK=true
    elif [ "$arg" = "--writer-baseline" ]; then
        RUN_WRITER_BASELINE=true
    fi
done

if [ "$RUN_WRITER_BENCHMARK" = true ] && [ "$RUN_WRITER_BASELINE" = true ]; then
    echo "Choose either --writer-baseline or --writer-benchmark, not both" >&2
    exit 2
fi

# ensure dependencies are installed and are always up to date
echo '============================================================='
echo "Running 'uv sync --extra dev'"
echo '============================================================='
uv sync --extra dev
echo '============================================================='
echo "Running 'uv run ruff format .'"
echo '============================================================='
uv run ruff format .
echo '============================================================='
echo "Running 'uv run ruff check --fix .'"
echo '============================================================='
uv run ruff check --fix .

# type check
echo '============================================================='
echo "Running 'uv run ty check'"
echo '============================================================='
uv run ty check

echo '============================================================='
echo "Checking startup and health shell syntax"
echo '============================================================='
bash -n scripts/start_services.sh scripts/healthcheck.sh scripts/test_rust_writer_container.sh scripts/test_rust_writer_rollback.sh

# Build the Rust executables used by writer differential tests before pytest.
# Reuse only these binaries during tests so the Python and Rust paths execute
# against the same current source revision.
if [ -f rust/Cargo.toml ]; then
    echo '============================================================='
    echo "Building Rust binaries required by parity tests"
    echo '============================================================='
    mise exec -- cargo build \
        --manifest-path rust/Cargo.toml \
        --bin podly_writer \
        --bin podly_tools
    export PODLY_RUST_WRITER_BIN="${PWD}/rust/target/debug/podly_writer"
    export PODLY_RUST_TOOLS_BIN="${PWD}/rust/target/debug/podly_tools"
fi

# run tests
echo '============================================================='
echo "Running 'uv run pytest --disable-warnings'"
echo '============================================================='
uv run pytest --disable-warnings

if [ -f rust/Cargo.toml ]; then
    echo '============================================================='
    echo "Running Rust checks"
    echo '============================================================='
    (
        cd rust || exit 1
        cargo fmt --check
        cargo clippy -- -D warnings
        cargo test
    )
fi

# The live Python writer API and Rust implementation must remain parity-complete.
echo '============================================================='
echo "Running 'uv run python scripts/check_writer_registry.py'"
echo '============================================================='
uv run python scripts/check_writer_registry.py

# Schema changes must be reviewed against the Rust writer's hand-written SQL.
echo '============================================================='
echo "Running 'uv run python scripts/check_writer_schema.py'"
echo '============================================================='
uv run python scripts/check_writer_schema.py

# Run integration tests only if --int flag is provided
if [ "$RUN_INTEGRATION" = true ]; then
    echo '============================================================='
    echo "Running integration workflow checks..."
    echo '============================================================='
    uv run python scripts/check_integration_workflow.py
fi

# This mode builds a uniquely tagged image and mounts only fresh named volumes.
# It never targets the running localhost integration service used by --int.
if [ "$RUN_WRITER_CONTAINER" = true ]; then
    echo '============================================================='
    echo "Running isolated Rust writer container lifecycle checks"
    echo '============================================================='
    ./scripts/test_rust_writer_container.sh
fi

# This mode rehearses writer rollback in a fresh, isolated named Docker volume.
if [ "$RUN_WRITER_ROLLBACK" = true ]; then
    echo '============================================================='
    echo "Running isolated Rust writer rollback rehearsal"
    echo '============================================================='
    ./scripts/test_rust_writer_rollback.sh
fi

# This mode runs the P0-paired concurrent writer acceptance benchmark after
# the normal CI gates. Supply an already-built image and keep all output under
# /tmp (or another explicitly chosen isolated artifact directory).
if [ "$RUN_WRITER_BENCHMARK" = true ] || [ "$RUN_WRITER_BASELINE" = true ]; then
    if [ -z "${PODLY_WRITER_BENCH_IMAGE:-}" ]; then
        echo "PODLY_WRITER_BENCH_IMAGE must name the isolated image for writer benchmark/baseline" >&2
        exit 2
    fi
    if [ -n "${PODLY_WRITER_BENCH_OUTPUT:-}" ]; then
        BENCH_OUTPUT="$PODLY_WRITER_BENCH_OUTPUT"
    elif [ "$RUN_WRITER_BASELINE" = true ]; then
        BENCH_OUTPUT="$(mktemp -d /tmp/podly-rust-writer-p0-replacement.XXXXXX)"
    else
        BENCH_OUTPUT="$(mktemp -d /tmp/podly-rust-writer-p4-benchmark.XXXXXX)"
    fi
    if [ "$RUN_WRITER_BASELINE" = true ]; then
        BASELINE_ARGS=()
        if [ -n "${PODLY_WRITER_BENCH_BASELINE:-}" ]; then
            BASELINE_ARGS+=(--baseline-report "$PODLY_WRITER_BENCH_BASELINE")
        fi
        if [ -n "${PODLY_WRITER_BENCH_REPETITIONS:-}" ]; then
            BASELINE_ARGS+=(--repetitions "$PODLY_WRITER_BENCH_REPETITIONS")
        fi
        uv run python scripts/bench_service_migration.py \
            --image "$PODLY_WRITER_BENCH_IMAGE" \
            --output "$BENCH_OUTPUT" \
            --writer-backend python \
            --writer-baseline \
            "${BASELINE_ARGS[@]}"
    else
        BENCH_BASELINE="${PODLY_WRITER_BENCH_BASELINE:-}"
        if [ -z "$BENCH_BASELINE" ]; then
            echo "PODLY_WRITER_BENCH_BASELINE must name the frozen replacement Python report" >&2
            exit 2
        fi
        if [ ! -f "$BENCH_BASELINE" ]; then
            echo "paired Python baseline report not found: $BENCH_BASELINE" >&2
            exit 2
        fi
        BENCH_THRESHOLDS="${PODLY_WRITER_BENCH_THRESHOLD_REPORT:-/tmp/podly-rust-writer-baseline/report.json}"
        if [ ! -f "$BENCH_THRESHOLDS" ]; then
            echo "historical P0 threshold report not found: $BENCH_THRESHOLDS" >&2
            exit 2
        fi
        BENCH_ARGS=()
        if [ -n "${PODLY_WRITER_BENCH_REPETITIONS:-}" ]; then
            BENCH_ARGS+=(--repetitions "$PODLY_WRITER_BENCH_REPETITIONS")
        fi
        uv run python scripts/bench_service_migration.py \
            --image "$PODLY_WRITER_BENCH_IMAGE" \
            --output "$BENCH_OUTPUT" \
            --baseline-report "$BENCH_BASELINE" \
            --threshold-report "$BENCH_THRESHOLDS" \
            --writer-backend rust \
            "${BENCH_ARGS[@]}"
    fi
fi
