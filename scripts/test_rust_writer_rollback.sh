#!/bin/bash
# Isolated P4.5 Rust-to-Python writer rollback rehearsal.
# Uses only a freshly-created named Docker volume; never mounts instance data.
set -Eeuo pipefail

image_tag="podly-rust-rollback-ci:${BASHPID}-${RANDOM}"
rust_container="podly-rust-rollback-rust-${BASHPID}-${RANDOM}"
python_container="${rust_container}-python"
instance_volume="${rust_container}-instance"

cleanup() {
    docker rm -f "$rust_container" "$python_container" >/dev/null 2>&1 || true
    docker volume rm "$instance_volume" >/dev/null 2>&1 || true
    docker image rm "$image_tag" >/dev/null 2>&1 || true
}

finish() {
    local status=$?
    if [ "$status" -ne 0 ]; then
        echo "Rollback rehearsal failed with status $status; collecting container state before cleanup." >&2
        for name in "$rust_container" "$python_container"; do
            if docker inspect "$name" >/dev/null 2>&1; then
                docker inspect -f \
                    '{{.Name}} running={{.State.Running}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}} error={{.State.Error}}' \
                    "$name" >&2 || true
                docker logs "$name" >&2 || true
                docker top "$name" -eo pid,args >&2 || true
            fi
        done
    fi
    cleanup
    exit "$status"
}
trap finish EXIT

docker volume create "$instance_volume" >/dev/null
echo "Building isolated rollback rehearsal image $image_tag..."
docker build --quiet --tag "$image_tag" . >/dev/null

bootstrap_isolated_volume() {
    docker run --rm \
        --network none \
        --volume "$instance_volume:/app/src/instance" \
        --env PODLY_WRITER_BACKEND=rust \
        --env PYTHONPATH=/app/src \
        --env PODLY_INSTANCE_DIR=/app/src/instance \
        --env PODLY_IPC_AUTHKEY=synthetic-rollback-auth-key \
        --env REQUIRE_AUTH=true \
        --env PODLY_ADMIN_USERNAME=rollback_admin \
        --env PODLY_ADMIN_PASSWORD=synthetic-rollback-password \
        --env PODLY_SECRET_KEY=synthetic-rollback-secret \
        --entrypoint python3 \
        "$image_tag" -u src/bootstrap.py
}

run_rust_writer() {
    docker run -d \
        --name "$rust_container" \
        --network none \
        --volume "$instance_volume:/app/src/instance" \
        --env PODLY_WRITER_BACKEND=rust \
        --env PYTHONPATH=/app/src \
        --env PODLY_INSTANCE_DIR=/app/src/instance \
        --env PODLY_IPC_AUTHKEY=synthetic-rollback-auth-key \
        --entrypoint /app/bin/podly_writer \
        "$image_tag" >/dev/null
}

await_rust_ready() {
    for _ in {1..120}; do
        if docker exec "$rust_container" /app/bin/podly_writer --probe >/dev/null 2>&1; then
            return 0
        fi
        if [ "$(docker inspect -f '{{.State.Running}}' "$rust_container")" != true ]; then
            docker logs "$rust_container" >&2
            return 1
        fi
        sleep 1
    done
    docker logs "$rust_container" >&2
    return 1
}

assert_only_rust_writer() {
    local process_list
    process_list="$(docker top "$rust_container" -eo pid,args)"
    local writer_count
    writer_count="$(grep -c '/app/bin/podly_writer' <<<"$process_list" || true)"
    if [ "$writer_count" != 1 ] || grep -Eq 'src/main.py|app\.writer' <<<"$process_list"; then
        echo "Rollback fixture setup must have exactly one Rust writer and no Python web/writer:" >&2
        echo "$process_list" >&2
        return 1
    fi
}

echo "Bootstrapping the isolated volume once, then starting the sole Rust writer..."
bootstrap_isolated_volume
run_rust_writer
await_rust_ready
assert_only_rust_writer

# Submit through the selected production WriterClient over real Rust RPC. The
# post GUID is synthetic; the writer's production schema intentionally allows
# jobs whose post was removed. A pending queue item must remain claimable after
# switching backend.
docker exec "$rust_container" python3 -c '
from app.writer.client import writer_client

for job_id, state in (("rollback-pending-01", "pending"), ("rollback-running-01", "running")):
    result = writer_client.action("create_job", {"job_data": {
        "id": job_id,
        "post_guid": "synthetic-rollback-post-" + job_id,
        "status": state,
        "current_step": 0 if state == "pending" else 2,
        "step_name": "Queued" if state == "pending" else "Processing",
        "total_steps": 4,
        "progress_percentage": 0.0 if state == "pending" else 50.0,
    }}, wait=True)
    if result is None or not result.success:
        raise SystemExit("Rust writer did not persist rollback fixture")
print("Rust RPC persisted one pending job and one interrupted running job")
'

rust_fixture_rows="$(docker exec "$rust_container" sqlite3 \
    /app/src/instance/sqlite3.db \
    "SELECT id || ':' || status FROM processing_job WHERE id LIKE 'rollback-%' ORDER BY id;")"
printf '%s\n' "$rust_fixture_rows"
expected_rust_fixture_rows=$'rollback-pending-01:pending\nrollback-running-01:running'
if [ "$rust_fixture_rows" != "$expected_rust_fixture_rows" ]; then
    echo "Rust RPC success did not persist fixtures in the mounted rollback database" >&2
    exit 1
fi

# Quiesce every Rust-side client and terminate the Rust writer before the
# Python writer starts. SQLite's backup API is used only after quiescence, so
# the copy is consistent even if WAL mode is active.
echo "Stopping all Rust-side clients and the Rust writer before rollback..."
docker stop --time 45 "$rust_container" >/dev/null
docker rm "$rust_container" >/dev/null

docker run --rm \
    --network none \
    --volume "$instance_volume:/app/src/instance" \
    --entrypoint sqlite3 \
    "$image_tag" /app/src/instance/sqlite3.db \
    ".backup '/app/src/instance/rollback-backup.sqlite3'"

# Confirm the current migration revision and the Python ORM contract before
# selecting Python. If this gate fails, the backup is retained in the test
# volume and no replacement writer is started.
docker run --rm \
    --network none \
    --volume "$instance_volume:/app/src/instance" \
    --env PYTHONPATH=/app/src \
    --env PODLY_INSTANCE_DIR=/app/src/instance \
    --entrypoint python3 \
    "$image_tag" -c '
import sqlite3
import app.models
from app.extensions import db

path = "/app/src/instance/sqlite3.db"
conn = sqlite3.connect(path)
revision = conn.execute("SELECT version_num FROM alembic_version").fetchone()
assert revision and revision[0], "database has no Alembic revision"
assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
backup = sqlite3.connect("/app/src/instance/rollback-backup.sqlite3")
assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
backup_revision = backup.execute(
    "SELECT version_num FROM alembic_version"
).fetchone()
assert backup_revision == revision, "backup and source schema revisions differ"
before = dict(backup.execute(
    "SELECT id, status FROM processing_job "
    "WHERE id IN (?,?)",
    ("rollback-pending-01", "rollback-running-01"),
))
assert before == {
    "rollback-pending-01": "pending",
    "rollback-running-01": "running",
}, f"quiesced backup does not contain expected pre-rollback jobs: {before}"
tables = {
    row[0]
    for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type=?", ("table",)
    )
}
for table in db.metadata.sorted_tables:
    assert table.name in tables, f"Python ORM table missing from rollback schema: {table.name}"
    actual = {row[1] for row in conn.execute(f"PRAGMA table_info({table.name})")}
    missing = sorted(column.name for column in table.columns if column.name not in actual)
    assert not missing, f"Python ORM columns missing from {table.name}: {missing}"
print(f"Verified Python rollback schema revision {revision[0]} and all ORM columns")
backup.close()
conn.close()
'

echo "Starting exactly one Python writer against the same verified volume..."
docker run -d \
    --name "$python_container" \
    --network none \
    --volume "$instance_volume:/app/src/instance" \
    --env PODLY_WRITER_BACKEND=python \
    --env PYTHONPATH=/app/src \
    --env PODLY_INSTANCE_DIR=/app/src/instance \
    --env PODLY_IPC_AUTHKEY=synthetic-rollback-auth-key \
    --env REQUIRE_AUTH=true \
    --env PODLY_ADMIN_USERNAME=rollback_admin \
    --env PODLY_ADMIN_PASSWORD=synthetic-rollback-password \
    --env PODLY_SECRET_KEY=synthetic-rollback-secret \
    --entrypoint python3 \
    "$image_tag" -u -m app.writer >/dev/null

# A dequeue through the Python WriterClient proves the retained pending work is
# visible and recoverable after rollback. The running fixture represents work
# interrupted by cutover; it must not be blindly replayed as pending.
docker exec "$python_container" python3 -c '
import time
from app.ipc import make_client_manager
from app.writer.client import writer_client

manager = None
for _ in range(120):
    try:
        manager = make_client_manager()
        break
    except (ConnectionError, OSError, EOFError):
        time.sleep(0.25)
if manager is None:
    raise SystemExit("Python writer IPC did not become ready")
writer_client.manager = manager
writer_client.queue = manager.get_command_queue()
reconcile = writer_client.action("update_job_status", {
    "job_id": "rollback-running-01",
    "status": "failed",
    "step": 2,
    "step_name": "Interrupted by isolated rollback rehearsal",
    "progress": 50.0,
    "error_message": "synthetic worker interrupted during rollback",
}, wait=True)
print(
    "Interrupted-job reconciliation:",
    "none" if reconcile is None else f"success={reconcile.success} data_present={bool(reconcile.data)} error_present={bool(reconcile.error)}",
)
if reconcile is None or not reconcile.success:
    raise SystemExit("Python writer could not reconcile the interrupted synthetic job")

result = writer_client.action("dequeue_job", {}, wait=True)
print(
    "Pending-job recovery:",
    "none" if result is None else f"success={result.success} data_present={bool(result.data)} error_present={bool(result.error)}",
)
if result is None or not result.success or not result.data:
    raise SystemExit("Python writer could not recover pending work")
if result.data.get("job_id") != "rollback-pending-01":
    raise SystemExit("Python rollback dequeued an unexpected job")
print("Python writer recovered the pending job through its normal dequeue action")
'

# The P3.3 real-response-loss test proves how a Rust command reaches an
# unknown outcome. During rollback no RPC or client queue is carried forward,
# so that command is intentionally not resubmitted. The assertions below check
# state changes only from the one pending-job dequeue performed by Python.
echo "No Rust command was resubmitted during backend selection; unknown outcomes remain unreplayed."

if [ "$(docker inspect -f '{{.State.Running}}' "$rust_container" 2>/dev/null || true)" = true ]; then
    echo "Rust writer container unexpectedly remains active" >&2
    exit 1
fi
if [ "$(docker inspect -f '{{.State.Running}}' "$python_container")" != true ]; then
    echo "Python writer is not the sole active writer after rollback" >&2
    exit 1
fi

echo "Stopping Python writer before checking isolated rollback results..."
docker stop --time 15 "$python_container" >/dev/null
docker rm "$python_container" >/dev/null

docker run --rm \
    --network none \
    --volume "$instance_volume:/app/src/instance" \
    --env PYTHONPATH=/app/src \
    --entrypoint python3 \
    "$image_tag" -c '
import sqlite3

path = "/app/src/instance/sqlite3.db"
current = sqlite3.connect(path)
backup = sqlite3.connect("/app/src/instance/rollback-backup.sqlite3")
for table, in current.execute(
    "SELECT name FROM sqlite_master WHERE type=?", ("table",)
):
    if table.startswith("sqlite_"):
        continue
    current_count = current.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    backup_count = backup.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    assert current_count == backup_count, (
        f"rollback lost or added rows in {table}: {backup_count} -> {current_count}"
    )

job_columns = [row[1] for row in current.execute("PRAGMA table_info(processing_job)")]
column_sql = ",".join(job_columns)
job_ids = ("rollback-pending-01", "rollback-running-01")


def read_jobs(connection):
    rows = connection.execute(
        f"SELECT {column_sql} FROM processing_job WHERE id IN (?,?)", job_ids
    )
    return {row[0]: dict(zip(job_columns, row, strict=True)) for row in rows}


before = read_jobs(backup)
after = read_jobs(current)
assert before.keys() == after.keys() == set(job_ids), "rollback job rows changed or disappeared"

pending_before = before["rollback-pending-01"]
pending = after["rollback-pending-01"]
pending_changed = {
    column for column in job_columns if pending_before[column] != pending[column]
}
assert pending_changed <= {"status", "started_at"}, (
    f"pending recovery changed unexpected fields: {sorted(pending_changed)}"
)
assert pending["status"] == "running" and pending["started_at"], (
    "pending work was not claimed exactly once"
)

interrupted_before = before["rollback-running-01"]
interrupted = after["rollback-running-01"]
interrupted_changed = {
    column
    for column in job_columns
    if interrupted_before[column] != interrupted[column]
}
assert interrupted_changed <= {
    "status",
    "step_name",
    "completed_at",
    "error_message",
}, (
    f"interrupted-job reconciliation changed unexpected fields: {sorted(interrupted_changed)}"
)
assert interrupted["status"] == "failed", (
    "interrupted job was not explicitly reconciled before pending recovery"
)
assert interrupted["step_name"] == "Interrupted by isolated rollback rehearsal", (
    "interrupted job step name does not match explicit reconciliation"
)
assert interrupted["started_at"] == interrupted_before["started_at"], (
    "reconciliation changed the original start time"
)
assert interrupted["completed_at"], "interrupted job has no completion timestamp"
assert interrupted["current_step"] == interrupted_before["current_step"]
assert interrupted["progress_percentage"] == interrupted_before["progress_percentage"]
assert interrupted["error_message"] == "synthetic worker interrupted during rollback"
print("Backup comparison: no table row loss; only explicit job reconciliation and pending claim changed")
current.close()
backup.close()
'

echo "Isolated rollback rehearsal passed: schema, backup, exclusivity, pending recovery, and no replay."
