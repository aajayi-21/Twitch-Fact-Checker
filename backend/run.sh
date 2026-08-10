#!/usr/bin/env bash
# One-command backend bootstrap: syncs the uv environment, starts the server.
# Idempotent — safe to run every time.
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
    echo "uv is not installed. Install it with:" >&2
    echo "    curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    echo "…then re-run this script." >&2
    exit 1
fi

# --inexact: never uninstall packages that are not in uv.lock. The GPU speech
# backend is installed with an accelerator-specific PyTorch wheel
# (scripts/install_stt_gpu.sh), and a plain `uv sync` would prune or
# downgrade it on every start.
echo "Syncing Python environment with uv…"
uv sync --inexact

echo "Starting Live Stream Fact-Checker backend on http://127.0.0.1:8710 …"
echo "No API key yet? That's fine — add one from the extension's options page."
# --no-sync: the sync above already ran. Without it `uv run` re-syncs and
# would replace a hand-picked +rocm/+xpu torch wheel with the locked one.
# --no-access-log: the extension polls /healthz every 3 s, which would
# otherwise bury the session log in ~20 access lines a minute.
exec uv run --no-sync uvicorn app.main:app \
    --host 127.0.0.1 --port 8710 --no-access-log
