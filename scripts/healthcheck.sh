#!/bin/bash
set -euo pipefail

export PYTHONPATH="/app/src${PYTHONPATH:+:$PYTHONPATH}"
backend="$(python3 -m app.writer.backend)"
if [ "$backend" = rust ]; then
    /app/bin/podly_writer --probe
else
    # Legacy rollback mode: authenticate with the actual Python IPC manager.
    python3 -c 'from app.ipc import make_client_manager; make_client_manager().get_command_queue()'
fi
# Verify the dependent web process too. This is a one-shot health command,
# not a persistent Python polling daemon.
python3 -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:5001/", timeout=2).close()'
