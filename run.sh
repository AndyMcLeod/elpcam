#!/usr/bin/env sh
# ELPCam launcher for Linux / macOS: creates .venv on first run, then starts the browser GUI.
# Extra arguments are passed through, e.g.  ./run.sh --simulate   or   ./run.sh serve --host 0.0.0.0
set -e
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
if [ ! -x .venv/bin/python ]; then
    echo "Creating virtual environment..."
    "$PY" -m venv .venv
    .venv/bin/python -m pip install --upgrade pip
    .venv/bin/python -m pip install -r requirements.txt
fi
if [ "$(uname)" = "Linux" ] && ! command -v v4l2-ctl >/dev/null 2>&1; then
    echo "Tip: install v4l-utils (sudo apt install v4l-utils) for full camera control on Linux."
fi
exec .venv/bin/python -m elpcam "$@"
