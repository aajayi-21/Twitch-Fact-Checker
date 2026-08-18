#!/usr/bin/env python3
"""Stream a WAV file to the backend over the exact /ws/audio wire protocol.

Kept as the README's documented test recipe; the implementation moved into
the ingest CLI (``streamer/ingest/``), of which ``--source wav`` is a strict
superset — so the WAV path has ONE implementation, exercised by both this
recipe and the product CLI.

Usage (from ``backend/``, with a server running)::

    python scripts/stream_wav.py tests/fixtures/claims_16k.wav
    python scripts/stream_wav.py tests/fixtures/claims_16k.wav --speed 3

Note the default URL targets the VIEWER backend (8710), matching this
script's historical behaviour; the ingest CLI itself defaults to the
streamer backend (8711).
"""

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    # Same shim as tests/conftest.py: makes `python scripts/stream_wav.py`
    # work outside `uv run` too.
    sys.path.insert(0, str(BACKEND_DIR))

from streamer.ingest.cli import main  # noqa: E402

if __name__ == "__main__":
    # Re-shape the historical argv (wav_path [--url] [--speed] [--sensitivity])
    # into the CLI's surface; unknown flags fall through to its parser.
    argv = sys.argv[1:]
    if "--url" not in " ".join(argv):
        argv += ["--url", "ws://127.0.0.1:8710/ws/audio"]
    sys.argv = [sys.argv[0], "--source", "wav", "--transcripts", *argv]
    main()
