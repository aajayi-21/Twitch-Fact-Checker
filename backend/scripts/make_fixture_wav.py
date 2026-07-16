#!/usr/bin/env python3
"""Build ``tests/fixtures/claims_16k.wav`` from a scripted transcript.

The script mixes real, checkable claims with opinions, predictions, and
gaming jargon so a full pipeline run (``scripts/stream_wav.py``) exercises
both sides of the claim gate. Text-to-speech is done with ``espeak-ng``
(subprocess), then resampled to 16 kHz mono s16le — the exact format the
``/ws/audio`` protocol expects.

The pytest suite does NOT depend on this fixture existing; it is only used
for manual end-to-end runs.

Usage (from ``backend/``)::

    python scripts/make_fixture_wav.py
    python scripts/make_fixture_wav.py --output /tmp/claims.wav --wpm 150
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

TARGET_SAMPLE_RATE = 16000

DEFAULT_OUTPUT = (
    Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "claims_16k.wav"
)

# Interleaved: checkable claims (true and false), opinions, predictions, and
# gaming jargon — the gate should extract only the real-world claims.
FIXTURE_SCRIPT = (
    "Welcome back to the stream everyone, we are live. "
    "Did you know the Eiffel Tower is four hundred and fifty meters tall? "
    "Look it up, chat. "
    "Honestly this patch is terrible, the developers have no idea what they "
    "are doing. "
    "I'm going to push the left lane and take the tower before their jungler "
    "respawns. "
    "Someone said the Great Wall of China is visible from space with the "
    "naked eye, and that's just facts. "
    "Messi has won eight Ballon d'Or awards, no other player is close. "
    "Trust me, our team is winning the whole tournament this year, easy. "
    "By the way, the speed of light is about three hundred thousand "
    "kilometers per second. "
    "Anyway, remember to hit that follow button and use code STREAM for ten "
    "percent off."
)

INSTALL_HINT = (
    "espeak-ng not found — install it first, e.g.: sudo apt-get install espeak-ng"
)


def synthesize_with_espeak(text: str, wav_path: Path, voice: str, wpm: int) -> None:
    """Run espeak-ng to synthesize ``text`` into ``wav_path`` (espeak's rate)."""
    command = [
        "espeak-ng",
        "-v",
        voice,
        "-s",
        str(wpm),
        "-w",
        str(wav_path),
        text,
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        print(
            f"error: espeak-ng failed (exit {exc.returncode}): {exc.stderr.strip()}",
            file=sys.stderr,
        )
        sys.exit(1)


def read_wav_as_float(wav_path: Path) -> tuple[np.ndarray, int]:
    """Read a 16-bit PCM WAV as mono float32 in [-1, 1] plus its sample rate."""
    with wave.open(str(wav_path), "rb") as reader:
        sample_width = reader.getsampwidth()
        if sample_width != 2:
            print(
                f"error: expected 16-bit PCM from espeak-ng, got "
                f"{sample_width * 8}-bit",
                file=sys.stderr,
            )
            sys.exit(1)
        channels = reader.getnchannels()
        sample_rate = reader.getframerate()
        raw = reader.readframes(reader.getnframes())
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, sample_rate


def resample_linear(samples: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
    """Linear-interpolation resample — ample fidelity for a speech fixture."""
    if from_rate == to_rate:
        return samples
    duration_s = len(samples) / from_rate
    target_count = int(round(duration_s * to_rate))
    source_times = np.arange(len(samples), dtype=np.float64) / from_rate
    target_times = np.arange(target_count, dtype=np.float64) / to_rate
    return np.interp(target_times, source_times, samples).astype(np.float32)


def write_wav_s16le_mono(samples: np.ndarray, sample_rate: int, path: Path) -> None:
    """Write float32 samples as a 16 kHz-style mono s16le WAV file."""
    clipped = np.clip(samples, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(pcm.tobytes())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="TTS the scripted claims/opinions/jargon transcript into "
        "a 16 kHz mono s16le WAV fixture."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"output WAV path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--voice", default="en-us", help="espeak-ng voice (default: en-us)"
    )
    parser.add_argument(
        "--wpm", type=int, default=165, help="speech rate in wpm (default: 165)"
    )
    args = parser.parse_args()

    if shutil.which("espeak-ng") is None:
        print(INSTALL_HINT, file=sys.stderr)
        sys.exit(1)

    with tempfile.TemporaryDirectory(prefix="fixture_tts_") as tmp_dir:
        raw_wav = Path(tmp_dir) / "espeak_raw.wav"
        synthesize_with_espeak(FIXTURE_SCRIPT, raw_wav, args.voice, args.wpm)
        samples, source_rate = read_wav_as_float(raw_wav)

    resampled = resample_linear(samples, source_rate, TARGET_SAMPLE_RATE)
    write_wav_s16le_mono(resampled, TARGET_SAMPLE_RATE, args.output)

    duration_s = len(resampled) / TARGET_SAMPLE_RATE
    print(
        f"wrote {args.output} ({duration_s:.1f}s, {TARGET_SAMPLE_RATE} Hz mono "
        f"s16le, resampled from {source_rate} Hz)"
    )


if __name__ == "__main__":
    main()
