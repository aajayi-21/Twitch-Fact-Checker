#!/usr/bin/env python3
"""Hardware spike for the Parakeet TDT backend: does it load, how fast, how sure?

Raw transformers — deliberately NOT the app's transcriber — so a failure here
isolates the model/accelerator from our filter stack. Loads the model on the
requested device/dtype, splits a WAV into utterances with Silero VAD (the same
options the VAD segmenter uses), and for each utterance prints latency, the
per-token confidence the backend will use as ``avg_logprob``, and the text.
Finishes with pure noise and a tone to show what non-speech scores look like
(the numbers ``ParakeetTranscriber.MIN_AVG_LOGPROB`` is calibrated from).

Usage (from ``backend/``, after ./scripts/install_stt_gpu.sh)::

    uv run --no-sync python scripts/spike_parakeet.py
    uv run --no-sync python scripts/spike_parakeet.py --device cpu --dtype fp32
    uv run --no-sync python scripts/spike_parakeet.py --wav some_16k_mono.wav
"""

import argparse
import sys
import time
import wave
from pathlib import Path

import numpy as np

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.stt_parakeet import (  # noqa: E402
    DEFAULT_PARAKEET_MODEL,
    TokenConfidenceRecorder,
    collect_token_timings,
)
from app.stt_torch import resolve_device, resolve_dtype  # noqa: E402

SAMPLE_RATE = 16000
DEFAULT_WAV = BACKEND_DIR / "tests" / "fixtures" / "claims_16k.wav"
DTYPES = {"fp16": "float16", "fp32": "float32", "bf16": "bfloat16"}


def read_wav(path: Path) -> np.ndarray:
    """16 kHz mono s16le WAV -> float32 in [-1, 1]."""
    with wave.open(str(path), "rb") as handle:
        if handle.getframerate() != SAMPLE_RATE or handle.getnchannels() != 1:
            raise SystemExit(f"{path}: need 16 kHz mono, got {handle.getparams()}")
        frames = handle.readframes(handle.getnframes())
    return np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0


def speech_spans(audio: np.ndarray) -> list[tuple[int, int]]:
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    options = VadOptions(
        min_silence_duration_ms=500,
        speech_pad_ms=200,
        min_speech_duration_ms=250,
        max_speech_duration_s=10.0,
    )
    return [
        (span["start"], span["end"])
        for span in get_speech_timestamps(audio, vad_options=options)
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--model", default=DEFAULT_PARAKEET_MODEL)
    parser.add_argument("--device", default="auto")
    parser.add_argument("--dtype", choices=sorted(DTYPES), default=None)
    parser.add_argument("--wav", type=Path, default=DEFAULT_WAV)
    args = parser.parse_args()

    import torch
    import transformers
    from transformers import AutoProcessor, LogitsProcessorList, ParakeetForTDT

    device = resolve_device(args.device, torch)
    dtype = resolve_dtype(DTYPES.get(args.dtype, "auto"), device, torch)
    print(f"torch {torch.__version__}  transformers {transformers.__version__}")
    print(f"device {device}  dtype {dtype}")
    if device == "xpu":
        print(f"xpu    {torch.xpu.get_device_name(0)}")

    started = time.monotonic()
    processor = AutoProcessor.from_pretrained(args.model)
    model = ParakeetForTDT.from_pretrained(args.model, dtype=dtype).to(device).eval()
    print(f"loaded {args.model} in {time.monotonic() - started:.1f}s")
    vocab_size = model.config.vocab_size
    blank_id = model.config.blank_token_id
    pad_id = model.config.pad_token_id
    feature_extractor = processor.feature_extractor
    frame_s = (
        feature_extractor.hop_length
        / feature_extractor.sampling_rate
        * model.config.encoder_config.subsampling_factor
    )
    print(
        f"vocab {vocab_size}  blank {blank_id}  pad {pad_id}  frame {frame_s:.3f}s  "
        f"suppress {model.generation_config.suppress_tokens}"
    )

    def transcribe(audio: np.ndarray) -> tuple[float, str, list[float], int]:
        inputs = processor(audio, sampling_rate=SAMPLE_RATE, return_tensors="pt")
        features = inputs["input_features"].to(device=device, dtype=dtype)
        mask = inputs["attention_mask"].to(device)
        recorder = TokenConfidenceRecorder(vocab_size)
        began = time.monotonic()
        with torch.inference_mode():
            output = model.generate(
                input_features=features,
                attention_mask=mask,
                logits_processor=LogitsProcessorList([recorder]),
            )
        if device == "xpu":
            torch.xpu.synchronize()
        elapsed = time.monotonic() - began
        sequences = output.sequences[0].cpu().tolist()
        durations = output.durations[0].cpu().tolist()
        logprobs = recorder.logprobs_for(0)
        if logprobs is not None and len(logprobs) != len(sequences) - 1:
            print(
                f"  !! recorder steps {len(logprobs)} != sequence steps "
                f"{len(sequences) - 1}"
            )
            logprobs = None
        tokens = collect_token_timings(
            sequences, durations, logprobs, {blank_id, pad_id}, frame_s
        )
        text = processor.batch_decode(output.sequences, skip_special_tokens=True)[0]
        return elapsed, text, [token.logprob for token in tokens], len(tokens)

    audio = read_wav(args.wav)
    spans = speech_spans(audio)
    print(f"\n{args.wav.name}: {len(audio) / SAMPLE_RATE:.1f}s, {len(spans)} spans")
    for pass_number in (1, 2):
        total_audio = total_time = 0.0
        print(f"\n-- pass {pass_number} --")
        for start, end in spans:
            clip = audio[start:end]
            elapsed, text, logprobs, n_tokens = transcribe(clip)
            total_audio += len(clip) / SAMPLE_RATE
            total_time += elapsed
            mean = float(np.mean(logprobs)) if logprobs else float("nan")
            low = float(np.min(logprobs)) if logprobs else float("nan")
            print(
                f"[{start / SAMPLE_RATE:6.2f}-{end / SAMPLE_RATE:6.2f}] "
                f"{elapsed * 1000:6.0f}ms  tokens {n_tokens:3d}  "
                f"mean_lp {mean:6.3f}  min_lp {low:6.3f}  {text!r}"
            )
        print(
            f"pass {pass_number}: {total_audio:.1f}s audio in {total_time:.2f}s "
            f"-> {total_audio / max(total_time, 1e-9):.1f}x realtime"
        )

    rng = np.random.default_rng(0)
    seconds = np.arange(4 * SAMPLE_RATE) / SAMPLE_RATE
    probes = {
        "white noise": (rng.standard_normal(4 * SAMPLE_RATE) * 0.1).astype(np.float32),
        "440 Hz tone": (0.3 * np.sin(2 * np.pi * 440 * seconds)).astype(np.float32),
        "silence": np.zeros(4 * SAMPLE_RATE, dtype=np.float32),
    }
    print("\n-- non-speech probes --")
    for name, clip in probes.items():
        elapsed, text, logprobs, n_tokens = transcribe(clip)
        mean = float(np.mean(logprobs)) if logprobs else float("nan")
        print(
            f"{name:12s} {elapsed * 1000:6.0f}ms  tokens {n_tokens:3d}  "
            f"mean_lp {mean:6.3f}  {text!r}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
