"""Replay a WAV through one STT engine exactly as a live session would. Offline.

Builds the engine from the app's own settings and factory
(``create_transcriber``), loads and warms it up, then feeds the WAV in 0.25 s
chunks through the same cutting logic the session pipeline uses — fixed
windows with a hop, or the VAD segmenter — and the same filter stack. Prints
the transcript, the per-call latency distribution, the real-time factor, the
filter drop counts and, with a reference transcript, the word error rate.

Run one engine per process (accelerator memory), e.g. from ``backend/``::

    uv run --no-sync python scripts/compare_stt.py --backend parakeet --device auto
    uv run --no-sync python scripts/compare_stt.py --backend torch \\
        --model openai/whisper-small.en --device auto --segmentation window
    uv run --no-sync python scripts/compare_stt.py --backend faster-whisper \\
        --model distil-small.en --device cpu --compute-type int8

The default WAV is ``tests/fixtures/claims_16k.wav`` (``make_fixture_wav.py``),
scored against the script it was synthesized from. For real stream audio,
record a 16 kHz mono clip, write a corrected transcript, and pass both with
``--wav`` / ``--reference``.
"""

import argparse
import json
import re
import statistics
import sys
import time
import wave
from pathlib import Path
from typing import Any

import numpy as np

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(BACKEND_DIR / "scripts"))

from app.config import VAD_SPEECH_PAD_MS, Settings  # noqa: E402
from app.segmenter import (  # noqa: E402
    VadSegmenter,
    VadSegmenterConfig,
    make_silero_span_fn,
)
from app.transcriber import SessionTextState, create_transcriber  # noqa: E402

SAMPLE_RATE = 16000
CHUNK_S = 0.25
DEFAULT_WAV = BACKEND_DIR / "tests" / "fixtures" / "claims_16k.wav"

_ONES = (
    "zero one two three four five six seven eight nine ten eleven twelve "
    "thirteen fourteen fifteen sixteen seventeen eighteen nineteen"
).split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
_SCALES = ((10**9, "billion"), (10**6, "million"), (1000, "thousand"))
_NUMBER_WORDS = set(_ONES + _TENS[2:] + ["hundred"] + [s for _, s in _SCALES])


def number_to_words(value: int) -> list[str]:
    """0 <= value < 10**12 in words, without "and" (both sides get the same)."""
    if value < 20:
        return [_ONES[value]]
    if value < 100:
        tens, ones = divmod(value, 10)
        return [_TENS[tens]] + ([_ONES[ones]] if ones else [])
    if value < 1000:
        hundreds, rest = divmod(value, 100)
        return [_ONES[hundreds], "hundred"] + (number_to_words(rest) if rest else [])
    for scale, name in _SCALES:
        if value >= scale:
            head, rest = divmod(value, scale)
            return (
                number_to_words(head) + [name] + (number_to_words(rest) if rest else [])
            )
    raise ValueError(value)


def normalize_words(text: str) -> list[str]:
    """Lowercase words; digits spelled out; "and" inside numbers dropped."""
    text = text.lower().replace("%", " percent ")
    text = re.sub(r"(?<=\d),(?=\d{3})", "", text)
    words: list[str] = []
    for token in re.findall(r"[a-z0-9']+", text):
        if token.isdigit() and len(token) <= 12:
            words.extend(number_to_words(int(token)))
        else:
            words.append(token.strip("'"))
    return [
        word
        for index, word in enumerate(words)
        if word
        and not (
            word == "and"
            and 0 < index < len(words) - 1
            and words[index - 1] in _NUMBER_WORDS
            and words[index + 1] in _NUMBER_WORDS
        )
    ]


def word_error_rate(reference: str, hypothesis: str) -> float:
    from rapidfuzz.distance import Levenshtein

    ref, hyp = normalize_words(reference), normalize_words(hypothesis)
    return Levenshtein.distance(ref, hyp) / max(1, len(ref))


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as handle:
        if handle.getframerate() != SAMPLE_RATE or handle.getnchannels() != 1:
            raise SystemExit(f"{path}: need 16 kHz mono, got {handle.getparams()}")
        frames = handle.readframes(handle.getnframes())
    return np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0


class Replay:
    """Feeds audio chunk by chunk and cuts it like the session pipeline."""

    def __init__(self, transcriber: Any, settings: Settings) -> None:
        self._transcriber = transcriber
        self._settings = settings
        self.state = SessionTextState(
            overlapping=settings.resolved_stt_segmentation == "window"
        )
        self.segments: list[Any] = []
        self.latencies: list[float] = []
        self._last_emitted_end = 0.0

    def _transcribe(self, audio: np.ndarray, start_s: float) -> None:
        started = time.perf_counter()
        emitted = self._transcriber.transcribe_window(
            audio, start_s, self._last_emitted_end, self.state
        )
        self.latencies.append(time.perf_counter() - started)
        for segment in emitted:
            self.segments.append(segment)
            self._last_emitted_end = max(self._last_emitted_end, segment.end)

    def run_windows(self, audio: np.ndarray) -> None:
        window = int(self._settings.stt_window_s * SAMPLE_RATE)
        hop = int(self._settings.stt_hop_s * SAMPLE_RATE)
        chunk = int(CHUNK_S * SAMPLE_RATE)
        base = 0
        for fed in range(chunk, len(audio) + chunk, chunk):
            fed = min(fed, len(audio))
            while fed - base >= window:
                self._transcribe(audio[base : base + window], base / SAMPLE_RATE)
                base += hop
        while len(audio) - base >= SAMPLE_RATE // 2:  # the stop flush
            self._transcribe(audio[base : base + window], base / SAMPLE_RATE)
            base += window

    def run_vad(self, audio: np.ndarray) -> None:
        config = VadSegmenterConfig(
            sample_rate=SAMPLE_RATE,
            min_silence_s=self._settings.stt_vad_min_silence_ms / 1000,
            speech_pad_s=VAD_SPEECH_PAD_MS / 1000,
            max_segment_s=self._settings.stt_vad_max_segment_s,
        )
        segmenter = VadSegmenter(make_silero_span_fn(config), config)
        chunk = int(CHUNK_S * SAMPLE_RATE)
        base = 0

        def act(fed: int, final: bool) -> bool:
            nonlocal base
            plan = segmenter.plan(audio[base:fed], final=final)
            if plan is None or plan.consume_to <= 0:
                return False
            if plan.speech is not None:
                start, end = plan.speech
                self._transcribe(
                    audio[base + start : base + end], (base + start) / SAMPLE_RATE
                )
            base += plan.consume_to
            return True

        for fed in range(chunk, len(audio) + chunk, chunk):
            fed = min(fed, len(audio))
            while act(fed, final=False):
                pass
        while len(audio) - base >= SAMPLE_RATE // 2 and act(len(audio), final=True):
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--backend", choices=["parakeet", "torch", "faster-whisper"], required=True
    )
    parser.add_argument("--model", help="model id (default: the backend's default)")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--compute-type", default="auto")
    parser.add_argument("--segmentation", choices=["auto", "window", "vad"])
    parser.add_argument("--wav", type=Path, default=DEFAULT_WAV)
    parser.add_argument("--reference", type=Path, help="reference transcript file")
    parser.add_argument("--show-segments", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    overrides: dict[str, Any] = {
        "stt_backend": args.backend,
        "whisper_device": args.device,
        "whisper_compute_type": args.compute_type,
    }
    if args.segmentation:
        overrides["stt_segmentation"] = args.segmentation
    if args.model:
        key = "parakeet_model" if args.backend == "parakeet" else "whisper_model"
        overrides[key] = args.model
    settings = Settings(_env_file=None, **overrides)

    reference: str | None = None
    if args.reference:
        reference = args.reference.read_text()
    elif args.wav.resolve() == DEFAULT_WAV.resolve():
        from make_fixture_wav import FIXTURE_SCRIPT

        reference = FIXTURE_SCRIPT

    audio = read_wav(args.wav)
    transcriber = create_transcriber(settings)
    load_started = time.perf_counter()
    transcriber.load()
    load_s = time.perf_counter() - load_started
    warm_started = time.perf_counter()
    transcriber.warm_up(settings.stt_warm_up_budget_s)
    warm_s = time.perf_counter() - warm_started

    replay = Replay(transcriber, settings)
    if settings.resolved_stt_segmentation == "vad":
        replay.run_vad(audio)
    else:
        replay.run_windows(audio)

    audio_s = len(audio) / SAMPLE_RATE
    compute_s = sum(replay.latencies)
    hypothesis = " ".join(segment.text for segment in replay.segments)
    latencies_ms = sorted(latency * 1000 for latency in replay.latencies)
    result = {
        "engine": transcriber.describe(),
        "segmentation": settings.resolved_stt_segmentation,
        "wav": str(args.wav),
        "audio_s": round(audio_s, 2),
        "load_s": round(load_s, 1),
        "warm_up_s": round(warm_s, 1),
        "calls": len(latencies_ms),
        "latency_ms_p50": (
            round(statistics.median(latencies_ms)) if latencies_ms else None
        ),
        "latency_ms_p95": (
            round(latencies_ms[int(0.95 * (len(latencies_ms) - 1))])
            if latencies_ms
            else None
        ),
        "compute_s": round(compute_s, 2),
        "realtime_factor": round(compute_s / audio_s, 3) if audio_s else None,
        "drop_counts": replay.state.drop_counts,
        "wer": (
            round(word_error_rate(reference, hypothesis), 4)
            if reference is not None
            else None
        ),
        "transcript": hypothesis,
    }
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    if args.show_segments:
        for segment in replay.segments:
            print(
                f"[{segment.start:7.2f}-{segment.end:7.2f}] "
                f"lp={segment.avg_logprob:6.3f} ns={segment.no_speech_prob:.2f} "
                f"{segment.text}"
            )
        print()
    for key, value in result.items():
        if key != "transcript":
            print(f"{key:16s} {value}")
    print(f"\n{hypothesis}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
