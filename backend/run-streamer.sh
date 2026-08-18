#!/usr/bin/env bash
# One-command STREAMER backend bootstrap — the side-by-side sibling of
# run.sh (the viewer backend). Own port (8711), own database (streamer.db).
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
    echo "uv is not installed. Install it with:" >&2
    echo "    curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    echo "…then re-run this script." >&2
    exit 1
fi

# --inexact / --no-sync: same reasoning as run.sh — never prune or downgrade
# a hand-picked accelerator torch wheel.
echo "Syncing Python environment with uv…"
uv sync --inexact

echo "Starting Live Stream Fact-Checker STREAMER backend on http://127.0.0.1:8711 …"
echo "Control panel: http://127.0.0.1:8711/control"
echo "OBS overlay:   http://127.0.0.1:8711/overlay"
exec uv run --no-sync uvicorn streamer.main:app \
    --host 127.0.0.1 --port 8711 --no-access-log
