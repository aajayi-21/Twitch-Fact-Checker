"""The headless ingest CLI: stream audio into ``/ws/audio`` without a browser.

``fact-checker-ingest`` is the streamer product's front half — the browser
extension's tab capture, replaced by one command run before each stream:

    uv run fact-checker-ingest twitch.tv/<you>            # via streamlink
    uv run fact-checker-ingest --source device --device pulse_monitor
    uv run fact-checker-ingest --source wav tests/fixtures/claims_16k.wav

Three sources behind one 250 ms-PCM-frames interface (``sources.py``), one
reconnecting protocol client (``client.py``), and a thin pump between them
(``cli.py``). Import discipline: this package may import ``app.models`` (the
wire vocabulary) and nothing heavier — pulling in the pipeline would make a
200 ms CLI take seconds to print ``--help``.
"""

SAMPLE_RATE = 16000
FRAME_SAMPLES = 4000  # 250 ms
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE
FRAME_BYTES = FRAME_SAMPLES * 2  # Int16LE mono

DEFAULT_URL = "ws://127.0.0.1:8711/ws/audio"
