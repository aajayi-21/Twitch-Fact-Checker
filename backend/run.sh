#!/usr/bin/env bash
# One-command backend bootstrap: creates the venv, installs deps, starts the server.
# Idempotent — safe to run every time.
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d .venv ]; then
    echo "Creating Python virtual environment (.venv)…"
    python3 -m venv .venv
fi

if ! .venv/bin/python -c "import fastapi" >/dev/null 2>&1; then
    echo "Installing backend dependencies (one-time)…"
    .venv/bin/pip install -r requirements.txt
    echo "First run: downloading speech model (~170 MB) on startup — this can take a few minutes…"
fi

echo "Starting Live Stream Fact-Checker backend on http://127.0.0.1:8710 …"
echo "No API key yet? That's fine — add one from the extension's options page."
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8710
