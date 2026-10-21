#!/bin/bash
# Isolated P4 writer lifecycle smoke; never mounts the checkout's instance data.
set -euo pipefail

image_tag="podly-rust-writer-ci:${BASHPID}-${RANDOM}"
container_name="podly-rust-writer-ci-${BASHPID}-${RANDOM}"
failure_name="${container_name}-bootstrap-failure"
upgrade_name="${container_name}-older-schema"
interrupted_name="${container_name}-interrupted-command"
instance_volume="${container_name}-instance"
failure_volume="${container_name}-failed-instance"
upgrade_volume="${container_name}-older-schema-instance"
interrupted_volume="${container_name}-interrupted-instance"
readiness_name="${container_name}-delayed-readiness"
readiness_volume="${container_name}-delayed-readiness-instance"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readiness_tmpdir="$(mktemp -d /tmp/podly-writer-readiness.XXXXXX)"
readiness_marker="/tmp/${readiness_name}-ready"
readiness_started="${readiness_marker}.started"

cleanup() {
    docker rm -f \
        "$container_name" "$failure_name" "$upgrade_name" "$interrupted_name" \
        "$readiness_name" "${container_name}-prepare-upgrade" "${container_name}-prepare-interruption" \
        >/dev/null 2>&1 || true
    docker volume rm "$instance_volume" "$failure_volume" "$upgrade_volume" "$interrupted_volume" "$readiness_volume" >/dev/null 2>&1 || true
    docker image rm "$image_tag" >/dev/null 2>&1 || true
    if [[ "$readiness_tmpdir" == /tmp/podly-writer-readiness.?????? ]]; then
        rm -rf -- "$readiness_tmpdir"
    fi
}
trap cleanup EXIT

docker volume create "$instance_volume" >/dev/null
docker volume create "$failure_volume" >/dev/null
docker volume create "$upgrade_volume" >/dev/null
docker volume create "$interrupted_volume" >/dev/null
docker volume create "$readiness_volume" >/dev/null
echo "Building isolated Rust writer test image $image_tag..."
docker build --quiet --tag "$image_tag" . >/dev/null

run_writer_container() {
    local target_name="${1:-$container_name}"
    local target_volume="${2:-$instance_volume}"
    docker run -d \
        --name "$target_name" \
        --network none \
        --volume "$target_volume:/app/src/instance" \
        --env PODLY_WRITER_BACKEND=rust \
        --env PODLY_INSTANCE_DIR=/app/src/instance \
        --env PODLY_IPC_AUTHKEY=synthetic-container-test-key \
        --env REQUIRE_AUTH=true \
        --env PODLY_ADMIN_USERNAME=synthetic_admin \
        --env PODLY_ADMIN_PASSWORD=synthetic-container-password \
        --env PODLY_SECRET_KEY=synthetic-container-secret-not-for-production \
        --env PUID=12001 \
        --env PGID=12001 \
        "$image_tag" >/dev/null
}

run_delayed_readiness_container() {
    docker create \
        --name "$readiness_name" \
        --network none \
        --volume "$readiness_volume:/app/src/instance" \
        --env PODLY_WRITER_BACKEND=rust \
        --env PODLY_INSTANCE_DIR=/app/src/instance \
        --env PODLY_IPC_AUTHKEY=synthetic-container-test-key \
        --env REQUIRE_AUTH=true \
        --env PODLY_ADMIN_USERNAME=synthetic_admin \
        --env PODLY_ADMIN_PASSWORD=synthetic-container-password \
        --env PODLY_SECRET_KEY=synthetic-container-secret-not-for-production \
        --env PUID=12001 \
        --env PGID=12001 \
        --env PODLY_READINESS_GATE_MARKER="$readiness_marker" \
        "$image_tag" >/dev/null
    docker cp "$readiness_name:/app/bin/podly_writer" "$readiness_tmpdir/podly_writer.real"
    docker cp "$readiness_tmpdir/podly_writer.real" "$readiness_name:/app/bin/podly_writer.real"
    docker cp "$script_dir/writer_readiness_gate.py" "$readiness_name:/app/bin/podly_writer"
    docker start "$readiness_name" >/dev/null
}

await_ready() {
    local target_name="${1:-$container_name}"
    for _ in {1..120}; do
        if docker exec "$target_name" /app/scripts/healthcheck.sh >/dev/null 2>&1; then
            return 0
        fi
        if [ "$(docker inspect -f '{{.State.Running}}' "$target_name")" != true ]; then
            docker logs "$target_name" >&2
            return 1
        fi
        sleep 1
    done
    docker logs "$target_name" >&2
    return 1
}

assert_retired_python_writer() {
    local target_name="${1:-$container_name}"
    local process_list
    process_list="$(docker top "$target_name" -eo pid,args)"
    if ! grep -q '/app/bin/podly_writer' <<<"$process_list"; then
        echo "Rust writer is absent from isolated container" >&2
        return 1
    fi
    if ! grep -q 'src/main.py' <<<"$process_list"; then
        echo "Python web is absent from isolated container" >&2
        return 1
    fi
    if grep -Eq 'python3 .* -m app.writer|src/bootstrap.py' <<<"$process_list"; then
        echo "Python writer or bootstrap persisted after readiness" >&2
        return 1
    fi
}

echo "Checking fresh bootstrap, readiness, process retirement, and non-default UID..."
run_writer_container
await_ready
assert_retired_python_writer
admin_count="$(docker exec "$container_name" sqlite3 /app/src/instance/sqlite3.db 'SELECT COUNT(*) FROM users;')"
test "$admin_count" = 1
test "$(docker exec "$container_name" stat -c %u /proc/1)" = 12001

echo "Checking web startup remains gated while protocol readiness is withheld..."
run_delayed_readiness_container
gate_started=0
for _ in {1..120}; do
    if docker exec "$readiness_name" test -f "$readiness_started"; then
        gate_started=1
        break
    fi
    if [ "$(docker inspect -f '{{.State.Running}}' "$readiness_name")" != true ]; then
        docker logs "$readiness_name" >&2
        exit 1
    fi
    sleep 0.25
done
test "$gate_started" = 1

# Wait until the real Rust service is ready behind the test-only gate. Its
# launcher-facing readiness probe must still fail until the marker is opened.
real_writer_ready=0
for _ in {1..80}; do
    if docker exec "$readiness_name" python3 /app/bin/podly_writer assert-real-ready; then
        real_writer_ready=1
        break
    fi
    sleep 0.25
done
test "$real_writer_ready" = 1
for _ in {1..8}; do
    if docker exec "$readiness_name" /app/bin/podly_writer --probe >/dev/null 2>&1; then
        echo "Rust readiness probe passed while the test gate was closed" >&2
        exit 1
    fi
    if docker exec "$readiness_name" /app/scripts/healthcheck.sh >/dev/null 2>&1; then
        echo "Container health passed while Rust readiness was withheld" >&2
        exit 1
    fi
    docker exec "$readiness_name" python3 /app/bin/podly_writer assert-web-absent
    sleep 0.25
done

docker exec "$readiness_name" touch "$readiness_marker"
await_ready "$readiness_name"
assert_retired_python_writer "$readiness_name"
readiness_processes="$(docker top "$readiness_name" -eo pid,args)"
if ! grep -Eq '/app/bin/podly_writer([[:space:]]|$)' <<<"$readiness_processes"; then
    echo "Test-only readiness proxy is absent after writer readiness" >&2
    exit 1
fi
if ! grep -q '/app/bin/podly_writer.real' <<<"$readiness_processes"; then
    echo "Real Rust writer is absent behind the delayed-readiness proxy" >&2
    exit 1
fi
docker stop --time 45 "$readiness_name" >/dev/null
docker rm "$readiness_name" >/dev/null

echo "Checking upgrade from an existing database at the prior Alembic revision..."
docker run --rm --name "${container_name}-prepare-upgrade" \
    --network none \
    --volume "$upgrade_volume:/app/src/instance" \
    --env PODLY_INSTANCE_DIR=/app/src/instance \
    --env PUID=12001 --env PGID=12001 \
    "$image_tag" /bin/bash -c \
    'export PYTHONPATH=/app/src; exec python3 /app/scripts/writer_container_test_helpers.py prepare-older-schema'
older_revision="$(docker run --rm --network none \
    --volume "$upgrade_volume:/app/src/instance" \
    --entrypoint sqlite3 "$image_tag" \
    /app/src/instance/sqlite3.db 'SELECT version_num FROM alembic_version;')"
test "$older_revision" = 88710a0fe69c
run_writer_container "$upgrade_name" "$upgrade_volume"
await_ready "$upgrade_name"
assert_retired_python_writer "$upgrade_name"
upgraded_revision="$(docker exec "$upgrade_name" sqlite3 /app/src/instance/sqlite3.db \
    'SELECT version_num FROM alembic_version;')"
test "$upgraded_revision" = 080b5181e23a
notification_table_count="$(docker exec "$upgrade_name" sqlite3 /app/src/instance/sqlite3.db \
    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='notification_settings';")"
test "$notification_table_count" = 1
test "$(docker exec "$upgrade_name" sqlite3 /app/src/instance/sqlite3.db 'SELECT COUNT(*) FROM users;')" = 1
docker stop --time 45 "$upgrade_name" >/dev/null
docker rm "$upgrade_name" >/dev/null

echo "Checking restart on the same isolated persistent volume..."
docker stop --time 45 "$container_name" >/dev/null
docker rm "$container_name" >/dev/null
run_writer_container
await_ready
assert_retired_python_writer
test "$(docker exec "$container_name" sqlite3 /app/src/instance/sqlite3.db 'SELECT COUNT(*) FROM users;')" = 1

echo "Checking in-flight transaction rollback across an interrupted writer..."
docker run --rm --name "${container_name}-prepare-interruption" \
    --network none \
    --volume "$interrupted_volume:/app/src/instance" \
    --env PODLY_INSTANCE_DIR=/app/src/instance \
    --env REQUIRE_AUTH=true \
    --env PODLY_ADMIN_USERNAME=synthetic_admin \
    --env PODLY_ADMIN_PASSWORD=synthetic-container-password \
    --env PODLY_SECRET_KEY=synthetic-container-secret-not-for-production \
    --env PUID=12001 --env PGID=12001 \
    "$image_tag" /bin/bash -c \
    'export PYTHONPATH=/app/src; exec python3 /app/src/bootstrap.py'
docker run --rm --network none \
    --volume "$interrupted_volume:/app/src/instance" \
    --user 12001:12001 --entrypoint sqlite3 "$image_tag" \
    /app/src/instance/sqlite3.db \
    'CREATE TABLE writer_test_events(value TEXT NOT NULL);'
docker run -d --name "$interrupted_name" \
    --network none \
    --volume "$interrupted_volume:/app/src/instance" \
    --user 12001:12001 \
    --env PODLY_IPC_AUTHKEY=synthetic-container-test-key \
    --env PODLY_INSTANCE_DIR=/app/src/instance \
    --entrypoint /app/bin/podly_writer "$image_tag" \
    --db /app/src/instance/sqlite3.db --port 50001 --enable-test-actions >/dev/null
writer_ready=0
for _ in {1..120}; do
    if docker exec "$interrupted_name" /app/bin/podly_writer --probe >/dev/null 2>&1; then
        writer_ready=1
        break
    fi
    if [ "$(docker inspect -f '{{.State.Running}}' "$interrupted_name")" != true ]; then
        docker logs "$interrupted_name" >&2
        exit 1
    fi
    sleep 0.25
done
test "$writer_ready" = 1
docker exec -d "$interrupted_name" /bin/bash -c \
    'export PYTHONPATH=/app/src PODLY_WRITER_BACKEND=rust PODLY_IPC_AUTHKEY=synthetic-container-test-key; exec python3 /app/scripts/writer_container_test_helpers.py submit-interrupted-transaction'
command_started=0
for _ in {1..40}; do
    if docker exec "$interrupted_name" test -f /tmp/podly-interrupted-writer-command-started; then
        command_started=1
        break
    fi
    sleep 0.1
done
test "$command_started" = 1
write_lock_held=0
for _ in {1..80}; do
    if docker exec "$interrupted_name" /bin/bash -c \
        'export PYTHONPATH=/app/src PODLY_INSTANCE_DIR=/app/src/instance; exec python3 /app/scripts/writer_container_test_helpers.py assert-write-lock' \
        >/dev/null 2>&1; then
        write_lock_held=1
        break
    fi
    sleep 0.1
done
test "$write_lock_held" = 1
echo "Confirmed SQLite's write lock is held by the in-flight command."

docker kill --signal KILL "$interrupted_name" >/dev/null
test "$(docker inspect -f '{{.State.Running}}' "$interrupted_name")" = false
docker rm "$interrupted_name" >/dev/null
run_writer_container "$interrupted_name" "$interrupted_volume"
await_ready "$interrupted_name"
assert_retired_python_writer "$interrupted_name"
interrupted_rows="$(docker exec "$interrupted_name" sqlite3 /app/src/instance/sqlite3.db \
    "SELECT COUNT(*) FROM writer_test_events WHERE value='must-roll-back';")"
test "$interrupted_rows" = 0
test "$(docker exec "$interrupted_name" sqlite3 /app/src/instance/sqlite3.db 'SELECT COUNT(*) FROM users;')" = 1
docker stop --time 45 "$interrupted_name" >/dev/null
docker rm "$interrupted_name" >/dev/null

echo "Checking dependent shutdown after isolated writer death..."
docker exec "$container_name" /bin/bash -c '
    for file in /proc/[0-9]*/comm; do
        read -r name < "$file" || continue
        if [ "$name" = podly_writer ]; then
            pid="${file#/proc/}"
            pid="${pid%/comm}"
            kill -TERM "$pid"
            exit 0
        fi
    done
    exit 1
'
for _ in {1..30}; do
    if [ "$(docker inspect -f '{{.State.Running}}' "$container_name")" != true ]; then
        break
    fi
    sleep 1
done
test "$(docker inspect -f '{{.State.Running}}' "$container_name")" = false
test "$(docker inspect -f '{{.State.ExitCode}}' "$container_name")" != 0

echo "Checking bootstrap failure prevents writer/web startup..."
docker run -d \
    --name "$failure_name" \
    --network none \
    --volume "$failure_volume:/app/src/instance" \
    --env PODLY_WRITER_BACKEND=rust \
    --env PODLY_INSTANCE_DIR=/app/src/instance \
    --env PODLY_IPC_AUTHKEY=synthetic-container-test-key \
    --env REQUIRE_AUTH=true \
    --env PODLY_ADMIN_PASSWORD= \
    --env PODLY_SECRET_KEY=synthetic-container-secret-not-for-production \
    "$image_tag" >/dev/null
for _ in {1..30}; do
    if [ "$(docker inspect -f '{{.State.Running}}' "$failure_name")" != true ]; then
        break
    fi
    sleep 1
done
test "$(docker inspect -f '{{.State.Running}}' "$failure_name")" = false
test "$(docker inspect -f '{{.State.ExitCode}}' "$failure_name")" != 0
if docker logs "$failure_name" 2>&1 | grep -q 'Starting Rust writer'; then
    echo "Rust writer started after failed bootstrap" >&2
    exit 1
fi

echo "Isolated Rust writer container lifecycle checks passed."
