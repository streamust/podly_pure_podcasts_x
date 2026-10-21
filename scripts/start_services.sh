#!/bin/bash
set -euo pipefail

export PYTHONPATH="/app/src${PYTHONPATH:+:$PYTHONPATH}"
WRITER_PID=""
APP_PID=""

stop_children() {
    if [ -n "$APP_PID" ] && kill -0 "$APP_PID" 2>/dev/null; then
        kill -TERM "$APP_PID" 2>/dev/null || true
    fi
    if [ -n "$WRITER_PID" ] && kill -0 "$WRITER_PID" 2>/dev/null; then
        kill -TERM "$WRITER_PID" 2>/dev/null || true
    fi
    if [ -n "$APP_PID" ]; then
        wait "$APP_PID" 2>/dev/null || true
    fi
    if [ -n "$WRITER_PID" ]; then
        wait "$WRITER_PID" 2>/dev/null || true
    fi
}
trap stop_children EXIT
trap 'exit 143' TERM INT

# The client and launcher use the same parser; a typo cannot select different
# writers in different processes. This one-shot selector exits immediately.
WRITER_BACKEND="$(python3 -m app.writer.backend)"
case "$WRITER_BACKEND" in
    python)
        echo "Starting Python writer..."
        python3 -u -m app.writer &
        WRITER_PID=$!
        ;;
    rust)
        echo "Running exclusive one-shot database bootstrap..."
        python3 -u src/bootstrap.py
        echo "Starting Rust writer..."
        /app/bin/podly_writer &
        WRITER_PID=$!
        ;;
    *)
        echo "Unsupported writer backend: $WRITER_BACKEND" >&2
        exit 2
        ;;
esac

READY=0
for _ in {1..120}; do
    if ! kill -0 "$WRITER_PID" 2>/dev/null; then
        echo "$WRITER_BACKEND writer exited before readiness" >&2
        exit 1
    fi
    if [ "$WRITER_BACKEND" = rust ]; then
        if /app/bin/podly_writer --probe >/dev/null 2>&1; then
            READY=1
            break
        fi
    elif python3 -c 'from app.ipc import make_client_manager; make_client_manager().get_command_queue()' >/dev/null 2>&1; then
        READY=1
        break
    fi
    sleep 0.25
done
if [ "$READY" -ne 1 ]; then
    echo "$WRITER_BACKEND writer did not become ready" >&2
    if [ "$WRITER_BACKEND" = rust ]; then
        /app/bin/podly_writer --probe || true
    fi
    exit 1
fi

echo "Starting Python web after $WRITER_BACKEND writer readiness..."
python3 -u src/main.py &
APP_PID=$!

EXITED=""
if wait -n -p EXITED "$WRITER_PID" "$APP_PID"; then
    STATUS=0
else
    STATUS=$?
fi
echo "Supervised process ${EXITED:-unknown} exited with status $STATUS" >&2
# Both children are intended to be persistent. Even a zero exit means the
# dependent process must stop and container supervision must investigate.
if [ "$STATUS" -eq 0 ]; then
    STATUS=1
fi
exit "$STATUS"
