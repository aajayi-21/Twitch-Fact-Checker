#!/usr/bin/env python3
"""Stream a WAV file to the backend over the exact /ws/audio wire protocol.

Speaks §2.1 verbatim: JSON hello first, then raw Int16LE mono 16 kHz PCM in
4000-sample (250 ms) binary frames paced at real time, a ``{"type":"stop"}``
frame at EOF, and finally waits for the server to flush and close. Every
server frame is printed to stdout as pretty JSON; progress notes go to
stderr so stdout stays parseable.

Usage (from ``backend/``, with the server running)::

    python scripts/stream_wav.py tests/fixtures/claims_16k.wav
    python scripts/stream_wav.py tests/fixtures/claims_16k.wav --speed 3
"""

import argparse
import asyncio
import json
import sys
import time
import wave
from pathlib import Path

import numpy as np
import websockets

SAMPLE_RATE = 16000
FRAME_SAMPLES = 4000
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE  # 0.25 s

DEFAULT_URL = "ws://127.0.0.1:8710/ws/audio"


def load_pcm_16k_mono(wav_path: Path) -> np.ndarray:
    """Load a WAV as int16 mono 16 kHz samples, converting when necessary."""
    with wave.open(str(wav_path), "rb") as reader:
        sample_width = reader.getsampwidth()
        if sample_width != 2:
            raise SystemExit(
                f"error: {wav_path} is {sample_width * 8}-bit; only 16-bit PCM "
                "WAV is supported (regenerate with scripts/make_fixture_wav.py)"
            )
        channels = reader.getnchannels()
        source_rate = reader.getframerate()
        raw = reader.readframes(reader.getnframes())

    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        print(f"note: downmixing {channels} channels to mono", file=sys.stderr)
        samples = samples.reshape(-1, channels).mean(axis=1)
    if source_rate != SAMPLE_RATE:
        print(f"note: resampling {source_rate} Hz -> {SAMPLE_RATE} Hz", file=sys.stderr)
        duration_s = len(samples) / source_rate
        target_count = int(round(duration_s * SAMPLE_RATE))
        source_times = np.arange(len(samples), dtype=np.float64) / source_rate
        target_times = np.arange(target_count, dtype=np.float64) / SAMPLE_RATE
        samples = np.interp(target_times, source_times, samples)
    return (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")


def build_hello(sensitivity: str, send_transcripts: bool) -> dict:
    """The §2.1 hello frame."""
    return {
        "type": "hello",
        "version": 1,
        "format": "pcm_s16le",
        "sample_rate": SAMPLE_RATE,
        "channels": 1,
        "sensitivity": sensitivity,
        "send_transcripts": send_transcripts,
    }


async def print_server_frames(websocket: websockets.ClientConnection) -> None:
    """Print every server text frame as pretty JSON until the socket closes."""
    async for message in websocket:
        if isinstance(message, bytes):
            print(
                f"note: unexpected {len(message)}-byte binary frame from server",
                file=sys.stderr,
            )
            continue
        try:
            frame = json.loads(message)
        except json.JSONDecodeError:
            print(f"note: non-JSON server frame: {message!r}", file=sys.stderr)
            continue
        print(json.dumps(frame, indent=2, ensure_ascii=False), flush=True)


async def send_audio(
    websocket: websockets.ClientConnection,
    samples: np.ndarray,
    speed: float,
) -> None:
    """Pace 4000-sample frames against the wall clock (250 ms / speed each)."""
    started = time.monotonic()
    frames_sent = 0
    for offset in range(0, len(samples), FRAME_SAMPLES):
        await websocket.send(samples[offset : offset + FRAME_SAMPLES].tobytes())
        frames_sent += 1
        target = started + frames_sent * (FRAME_SECONDS / speed)
        delay = target - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
    await websocket.send(json.dumps({"type": "stop"}))
    print(
        f"note: sent {frames_sent} frames "
        f"({frames_sent * FRAME_SECONDS:.1f}s of audio) + stop; waiting for "
        "the server to flush…",
        file=sys.stderr,
    )


async def stream(args: argparse.Namespace) -> None:
    samples = load_pcm_16k_mono(args.wav_path)
    duration_s = len(samples) / SAMPLE_RATE
    print(
        f"note: streaming {args.wav_path} ({duration_s:.1f}s) to {args.url} "
        f"at {args.speed}x",
        file=sys.stderr,
    )
    async with websockets.connect(args.url, max_size=None) as websocket:
        await websocket.send(json.dumps(build_hello(args.sensitivity, True)))
        printer = asyncio.create_task(print_server_frames(websocket))
        try:
            await send_audio(websocket, samples, args.speed)
            await printer  # runs until the server closes the socket
        except websockets.ConnectionClosed:
            await printer
        finally:
            if not printer.done():
                printer.cancel()
        close_code = websocket.close_code
        print(f"note: connection closed (code={close_code})", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stream a WAV file to /ws/audio using the real wire "
        "protocol and print every server frame as pretty JSON."
    )
    parser.add_argument("wav_path", type=Path, help="input WAV file")
    parser.add_argument(
        "--url", default=DEFAULT_URL, help=f"WebSocket URL (default: {DEFAULT_URL})"
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="pacing multiplier; >1 streams faster than real time to "
        "exercise backpressure (default: 1.0)",
    )
    parser.add_argument(
        "--sensitivity",
        choices=["low", "medium", "high"],
        default="medium",
        help="claim-gate sensitivity for the hello frame (default: medium)",
    )
    args = parser.parse_args()
    if args.speed <= 0:
        parser.error("--speed must be > 0")
    if not args.wav_path.exists():
        parser.error(f"no such file: {args.wav_path}")

    try:
        asyncio.run(stream(args))
    except (OSError, websockets.WebSocketException) as exc:
        print(
            f"error: could not stream to {args.url}: {exc} — is the backend "
            "running? (uvicorn app.main:app from backend/)",
            file=sys.stderr,
        )
        sys.exit(1)
    except KeyboardInterrupt:
        print("note: interrupted", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
