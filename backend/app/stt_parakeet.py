"""NVIDIA Parakeet TDT via transformers — the ``STT_BACKEND=parakeet`` engine.

Parakeet is a FastConformer encoder with a Token-and-Duration Transducer
(TDT) decoder. Two properties make it a better fit than Whisper for this
pipeline:

- **No 30 s padding.** Whisper pads every input to 30 s, so a 4 s window
  pays for 30 s of encoder. Parakeet encodes only the audio it is given,
  which is what makes variable-length VAD utterances cheap
  (``STT_SEGMENTATION=vad``, the default for this backend).
- **Accuracy.** nvidia/parakeet-tdt-0.6b-v3 scores ~6.8 average WER on the
  Open ASR Leaderboard's public sets vs ~9.2 for whisper-small.en, and ~11.4
  vs ~17.9 on AMI (noisy meetings, crosstalk).

It plugs into :class:`app.transcriber.BaseTranscriber` like the Whisper
engines, which means this module must produce the two load-bearing scores
the shared filter stack thresholds on:

- ``avg_logprob``: transformers' Parakeet ``generate`` discards per-step
  scores, so :class:`TokenConfidenceRecorder` rides along as a logits
  processor and keeps the log-probability of every greedy choice. A
  segment's score is the mean over its emitted (non-blank) tokens. If the
  recorder ever disagrees with the returned sequence (transformers drift),
  the score fails OPEN at 0.0 and a warning is logged once — never a silent
  drop of real speech.
- ``no_speech_prob``: 1 - Silero VAD coverage of the segment, exactly as the
  torch Whisper backend computes it.

Segments are split from TDT token timestamps (sentence punctuation or a
pause), so the window-mode overlap trim still has segment boundaries to work
with. The device/dtype resolution, the accelerator-fault contract (sync after
``generate`` so a fault surfaces in the window that caused it; the STT
supervisor reloads on CPU) and the log quieting follow
:mod:`app.stt_torch`, whose helpers are reused rather than duplicated.

``torch``/``transformers``/``librosa`` are imported inside :meth:`load`, so
the default install never needs them.
"""

import importlib.util
import logging
import math
import time
import warnings
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from app.segmenter import speech_coverage
from app.stt_torch import (
    SAMPLE_RATE,
    _cuda_available,
    _xpu_available,
    resolve_device,
    resolve_dtype,
)
from app.transcriber import BaseTranscriber, RawSegment, SimpleRawSegment

logger = logging.getLogger(__name__)

DEFAULT_PARAKEET_MODEL = "nvidia/parakeet-tdt-0.6b-v3"

#: SentencePiece word-boundary marker (Metaspace pre-tokenizer).
WORD_BOUNDARY = "▁"


@dataclass(frozen=True)
class TokenTiming:
    """One emitted (non-blank, non-special) token with its TDT timing."""

    token_id: int
    start_s: float
    end_s: float
    #: Log-probability of the greedy choice, or ``None`` when unscored.
    logprob: float | None


class TokenConfidenceRecorder:
    """Logits processor that records each step's greedy log-probability.

    transformers calls it once per decoding step with that step's scores:
    ``vocab_size`` token logits (blank included) followed by the TDT
    duration logits. It keeps ``max(log_softmax(token part))`` — the
    log-probability of the token greedy decoding is about to pick — on the
    device (no per-step host sync), and returns the same tensor with the
    duration columns masked to ``-inf``. The mask is a guard, not a fix: the
    shipped generation config already suppresses those ids, and without
    either one an argmax over the full row could pick a duration column as a
    token id past the decoder's embedding table — on an accelerator that is
    an asynchronous out-of-bounds fault.
    """

    def __init__(self, vocab_size: int) -> None:
        self._vocab_size = vocab_size
        self._steps: list[Any] = []

    def __call__(self, input_ids: Any, scores: Any) -> Any:
        token_scores = scores[:, : self._vocab_size].float()
        self._steps.append(token_scores.log_softmax(dim=-1).max(dim=-1).values)
        if scores.shape[-1] > self._vocab_size:
            scores[:, self._vocab_size :] = -math.inf
        return scores

    @property
    def step_count(self) -> int:
        return len(self._steps)

    def logprobs_for(self, batch_index: int) -> list[float] | None:
        """Per-step log-probabilities for one batch row, or ``None`` if empty."""
        if not self._steps:
            return None
        import torch

        stacked = torch.stack(self._steps, dim=1)[batch_index]
        return [float(value) for value in stacked.cpu().tolist()]


def collect_token_timings(
    sequence: Sequence[int],
    durations: Sequence[int],
    logprobs: Sequence[float] | None,
    skip_ids: set[int],
    frame_s: float,
    vocab_size: int | None = None,
) -> list[TokenTiming]:
    """Emitted tokens with start/end seconds, from a TDT ``generate`` output.

    ``sequence[0]`` is the decoder start token (blank) and ``durations[0]``
    the zero transformers prepends for it. Step ``k``'s token was emitted at
    encoder frame ``sum(durations[:k])`` and spans ``durations[k]`` frames —
    the same arithmetic as ``ParakeetProcessor.decode(durations=...)``.
    ``logprobs[k - 1]`` scored step ``k`` (the recorder never sees the start
    token); pass ``None`` to leave every token unscored.

    Blank, padding and special tokens are skipped, as is any id at or past
    ``vocab_size`` when it is given.
    """
    timings: list[TokenTiming] = []
    frame = 0
    for step, (token_id, duration) in enumerate(zip(sequence, durations)):
        token_id = int(token_id)
        duration = int(duration)
        start_frame = frame
        frame += duration
        if step == 0 or token_id in skip_ids:
            continue
        if vocab_size is not None and not 0 <= token_id < vocab_size:
            continue
        logprob = None
        if logprobs is not None and step - 1 < len(logprobs):
            logprob = float(logprobs[step - 1])
        timings.append(
            TokenTiming(
                token_id=token_id,
                start_s=start_frame * frame_s,
                end_s=(start_frame + duration) * frame_s,
                logprob=logprob,
            )
        )
    return timings


@dataclass(frozen=True)
class SegmentDraft:
    """A transcript segment before VAD scoring (window-relative seconds)."""

    text: str
    start: float
    end: float
    avg_logprob: float


def build_segments(
    tokens: Sequence[TokenTiming],
    piece_of: Callable[[int], str],
    decode: Callable[[list[int]], str],
    window_s: float,
    frame_s: float,
    pause_split_s: float,
    sentence_end: tuple[str, ...] = (".", "?", "!"),
) -> list[SegmentDraft]:
    """Group tokens into segments at sentence ends and at pauses.

    A sentence-ending piece closes the segment only when the NEXT token
    starts a new word, so decimals ("3.5" is ``▁3`` ``.`` ``5``) and dotted
    abbreviations stay in one piece. A gap of at least ``pause_split_s``
    before a word-initial token also starts a new segment.

    Each segment's ``avg_logprob`` is the mean over its scored tokens, or
    0.0 (fail open) when none were scored. Times are clamped to the window
    and every segment spans at least one encoder frame.
    """
    groups: list[list[TokenTiming]] = []
    current: list[TokenTiming] = []
    for index, token in enumerate(tokens):
        piece = piece_of(token.token_id)
        starts_word = piece.startswith(WORD_BOUNDARY)
        if (
            current
            and starts_word
            and token.start_s - current[-1].end_s >= pause_split_s
        ):
            groups.append(current)
            current = []
        current.append(token)
        next_starts_word = index + 1 >= len(tokens) or piece_of(
            tokens[index + 1].token_id
        ).startswith(WORD_BOUNDARY)
        if piece.strip(WORD_BOUNDARY).endswith(sentence_end) and next_starts_word:
            groups.append(current)
            current = []
    if current:
        groups.append(current)

    drafts: list[SegmentDraft] = []
    for group in groups:
        text = decode([token.token_id for token in group]).strip()
        if not text:
            continue
        scored = [token.logprob for token in group if token.logprob is not None]
        start = max(0.0, min(group[0].start_s, window_s))
        end = max(group[-1].end_s, group[0].start_s + frame_s)
        end = max(start, min(end, window_s))
        drafts.append(
            SegmentDraft(
                text=text,
                start=start,
                end=end,
                avg_logprob=float(np.mean(scored)) if scored else 0.0,
            )
        )
    return drafts


class ParakeetTranscriber(BaseTranscriber):
    """Parakeet TDT via transformers, on CPU / CUDA / ROCm / XPU."""

    BACKEND_NAME = "parakeet"

    #: Silero speech coverage below this fraction of the input skips the
    #: model entirely (same gate as the torch Whisper backend).
    MIN_SPEECH_RATIO = 0.02
    #: Low-confidence floor for a segment's mean token log-probability.
    #: Whisper's -1.0 does not transfer: TDT greedy choices score close to 0.
    #: Measured on v3 (fp16, Arc iGPU) with the fixture speech under white
    #: noise: clean -0.01..-0.07, 0 dB SNR (a few word errors) -0.16, -5 dB
    #: (mostly wrong words) -0.49, -10 dB (unrelated words) -0.74. Pure
    #: noise, tones and silence emit no tokens at all. -0.6 drops the
    #: garbage and keeps noisy-but-usable speech; the claim gate's own
    #: "garbled text" exclusion handles the band in between.
    MIN_AVG_LOGPROB = -0.6
    #: Inputs are padded (attention-masked, so transcripts are unchanged) up
    #: to a multiple of this many seconds. Accelerator kernels are compiled
    #: per input shape, and VAD clips are all different lengths: measured on
    #: an Arc iGPU, a previously seen shape runs in ~0.1-0.3 s against
    #: ~0.3-0.5 s for a new one. 0 disables bucketing.
    BUCKET_S = 1.0
    #: A silence of at least this long before a word starts a new segment.
    PAUSE_SPLIT_S = 0.8
    #: Accelerators whose kernels compile lazily and fault asynchronously.
    ACCELERATOR_DEVICES: frozenset[str] = frozenset({"cuda", "xpu"})
    #: transformers loggers that emit per-call notices during generate().
    NOISY_TRANSFORMERS_LOGGERS: tuple[str, ...] = (
        "transformers.generation",
        "transformers.models.parakeet",
        "transformers.tokenization_utils_base",
    )

    def __init__(
        self,
        model_name: str = DEFAULT_PARAKEET_MODEL,
        device: str = "auto",
        compute_type: str = "auto",
        language: str | None = None,
        warm_up_seconds: float = 4.0,
    ) -> None:
        super().__init__(
            model_name=model_name,
            device=device,
            compute_type=compute_type,
            language=language,
        )
        self._warm_up_seconds = warm_up_seconds
        self._torch: Any | None = None
        self._processor: Any | None = None
        self._logits_processor_list: Any | None = None
        self._torch_device: str = "cpu"
        self._dtype: Any | None = None
        self._vad_options: Any | None = None
        self._vocab_size = 0
        self._skip_ids: set[int] = set()
        self._frame_s = 0.08
        self._warned_missing_vad = False
        self._warned_missing_scores = False

    def describe(self) -> str:
        return (
            f"{self.BACKEND_NAME}:{self._model_name} "
            f"(device={self._torch_device}, dtype={self._dtype_name()})"
            f"{self._degraded_suffix()}"
        )

    @property
    def effective_device(self) -> str:
        """The resolved torch device (``cpu``/``cuda``/``xpu``), not the request."""
        return self._torch_device

    def _dtype_name(self) -> str:
        return str(self._dtype).removeprefix("torch.") if self._dtype else "?"

    def load(self) -> None:
        """Import torch, resolve the device, and load the model.

        Raises:
            RuntimeError: if torch/transformers/librosa are missing, the
                requested accelerator is unavailable, or the model fails to
                load.
        """
        try:
            import torch
            from transformers import (
                AutoProcessor,
                LogitsProcessorList,
                ParakeetForTDT,
            )
        except ImportError as exc:
            raise RuntimeError(
                "STT_BACKEND=parakeet needs PyTorch and transformers, which are "
                "not installed. Run ./scripts/install_stt_gpu.sh (it picks the "
                f"right wheel for your GPU). Original error: {exc}"
            ) from exc
        if importlib.util.find_spec("librosa") is None:
            raise RuntimeError(
                "STT_BACKEND=parakeet needs librosa (transformers builds "
                "Parakeet's mel filterbank with it). Re-run "
                "./scripts/install_stt_gpu.sh, which installs it."
            )

        # Same quieting as the torch Whisper backend (see stt_torch.load):
        # the weights progress bar, the doubled hub notice, and generate()'s
        # per-call notices, all kept at DEBUG.
        if not logger.isEnabledFor(logging.DEBUG):
            try:
                from transformers.utils import logging as hf_logging

                hf_logging.disable_progress_bar()
                logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
            except Exception as exc:  # pragma: no cover - transformers drift
                logger.debug("could not quiet the Hugging Face loggers: %s", exc)
            for name in self.NOISY_TRANSFORMERS_LOGGERS:
                logging.getLogger(name).setLevel(logging.ERROR)
        # Parakeet's generate() sizes its own output buffer from the encoder
        # length (ParakeetRNNTGenerationMixin._prepare_generated_length), so
        # the generic "model-agnostic default max_length" UserWarning is
        # benign — and, since the length is in the message, it re-fires for
        # every new input size (i.e. every VAD bucket).
        warnings.filterwarnings(
            "ignore",
            message="Using the model-agnostic default `max_length`",
            category=UserWarning,
        )

        self._torch = torch
        self._logits_processor_list = LogitsProcessorList
        self._torch_device = resolve_device(self._device, torch)
        self._dtype = resolve_dtype(self._compute_type, self._torch_device, torch)
        if self._language not in (None, "en"):
            logger.warning(
                "WHISPER_LANGUAGE=%s ignored: %s detects the language itself",
                self._language,
                self._model_name,
            )

        logger.info(
            "loading parakeet model %s (device=%s, dtype=%s)…",
            self._model_name,
            self._torch_device,
            self._dtype_name(),
        )
        try:
            self._processor = AutoProcessor.from_pretrained(self._model_name)
            model = ParakeetForTDT.from_pretrained(self._model_name, dtype=self._dtype)
            model.to(self._torch_device)
            model.eval()
            config = model.config
            tokenizer = self._processor.tokenizer
            feature_extractor = self._processor.feature_extractor
            self._vocab_size = int(config.vocab_size)
            self._skip_ids = {
                int(config.blank_token_id),
                int(config.pad_token_id),
                *(int(token_id) for token_id in tokenizer.all_special_ids),
            }
            self._frame_s = (
                feature_extractor.hop_length
                / feature_extractor.sampling_rate
                * config.encoder_config.subsampling_factor
            )
            self._model = model
        except Exception as exc:
            raise RuntimeError(
                f"failed to load Parakeet model {self._model_name!r} "
                f"(device={self._torch_device}, dtype={self._dtype_name()}): "
                f"{exc}"
            ) from exc

        try:
            from faster_whisper.vad import VadOptions

            self._vad_options = VadOptions()
        except Exception as exc:  # pragma: no cover - optional hardening
            logger.warning(
                "Silero VAD unavailable (%s); parakeet will run on every input "
                "and lean on the text filters alone",
                exc,
            )
            self._vad_options = None

        logger.info(
            "parakeet model %s ready on %s", self._model_name, self._torch_device
        )

    def unload(self) -> None:
        """Drop the model and free accelerator memory."""
        self._model = None
        self._processor = None
        torch = self._torch
        if torch is None:
            return
        try:
            if self._torch_device == "cuda" and _cuda_available(torch):
                torch.cuda.empty_cache()
            elif self._torch_device == "xpu" and _xpu_available(torch):
                torch.xpu.empty_cache()
        except Exception as exc:  # pragma: no cover - best effort
            logger.debug("emptying %s cache failed: %s", self._torch_device, exc)

    def fall_back_to_cpu(self) -> None:
        """CPU reload in fp32 (fp16 is emulated, and slower, on CPUs)."""
        self._compute_type = "float32"
        super().fall_back_to_cpu()

    # ------------------------------------------------------------------ #
    # Engine
    # ------------------------------------------------------------------ #

    def _run_model(self, audio: np.ndarray) -> Iterable[RawSegment]:
        speech_spans = self._speech_spans(audio)
        if speech_spans is not None:
            covered = sum(end - start for start, end in speech_spans)
            if covered / max(1, len(audio)) < self.MIN_SPEECH_RATIO:
                return []
        return self._infer(audio, speech_spans)

    def _infer(
        self, audio: np.ndarray, speech_spans: list[tuple[int, int]] | None
    ) -> list[RawSegment]:
        """Features -> generate (+ confidence recorder) -> sync -> segments.

        No VAD gate here, so :meth:`warm_up` can push synthetic audio through
        the whole path. Device faults (``RuntimeError``) propagate: the STT
        supervisor owns recovery.
        """
        torch = self._torch
        processor_kwargs: dict[str, Any] = {}
        bucketed_length = self._bucketed_length(len(audio))
        if bucketed_length > len(audio):
            processor_kwargs = {"padding": "max_length", "max_length": bucketed_length}
        inputs = self._processor(
            audio, sampling_rate=SAMPLE_RATE, return_tensors="pt", **processor_kwargs
        )
        generate_kwargs: dict[str, Any] = {
            "input_features": inputs["input_features"].to(
                self._torch_device, dtype=self._dtype
            )
        }
        attention_mask = inputs.get("attention_mask")
        if attention_mask is not None:
            generate_kwargs["attention_mask"] = attention_mask.to(self._torch_device)
        recorder = TokenConfidenceRecorder(self._vocab_size)
        with torch.inference_mode():
            output = self._model.generate(
                **generate_kwargs,
                logits_processor=self._logits_processor_list([recorder]),
            )
            # Surface an asynchronous accelerator fault in THIS window.
            self._sync_device()
        sequence = [int(token) for token in output.sequences[0].cpu().tolist()]
        durations = [int(frames) for frames in output.durations[0].cpu().tolist()]
        logprobs = recorder.logprobs_for(0)
        if logprobs is None or len(logprobs) != len(sequence) - 1:
            self._warn_missing_scores(recorder.step_count, max(0, len(sequence) - 1))
            logprobs = None
        tokens = collect_token_timings(
            sequence,
            durations,
            logprobs,
            self._skip_ids,
            self._frame_s,
            self._vocab_size,
        )
        return self._to_raw_segments(tokens, audio, speech_spans)

    def _bucketed_length(self, sample_count: int) -> int:
        """``sample_count`` rounded up to the next :attr:`BUCKET_S` multiple."""
        bucket = int(self.BUCKET_S * SAMPLE_RATE)
        if bucket <= 0 or sample_count <= 0:
            return sample_count
        return -(-sample_count // bucket) * bucket

    def _to_raw_segments(
        self,
        tokens: Sequence[TokenTiming],
        audio: np.ndarray,
        speech_spans: list[tuple[int, int]] | None,
    ) -> list[RawSegment]:
        tokenizer = self._processor.tokenizer
        drafts = build_segments(
            tokens,
            piece_of=tokenizer.convert_ids_to_tokens,
            decode=lambda ids: tokenizer.decode(ids, skip_special_tokens=True),
            window_s=len(audio) / SAMPLE_RATE,
            frame_s=self._frame_s,
            pause_split_s=self.PAUSE_SPLIT_S,
        )
        return [
            SimpleRawSegment(
                text=draft.text,
                start=draft.start,
                end=draft.end,
                avg_logprob=draft.avg_logprob,
                no_speech_prob=self._no_speech_prob(
                    speech_spans, draft.start, draft.end, len(audio)
                ),
            )
            for draft in drafts
        ]

    def _sync_device(self) -> None:
        """Block until queued accelerator kernels finish (no-op on CPU)."""
        torch = self._torch
        if torch is None or self._torch_device not in self.ACCELERATOR_DEVICES:
            return
        backend = getattr(torch, self._torch_device, None)
        if backend is not None:
            backend.synchronize()

    def warm_up(self, budget_s: float | None = None) -> None:
        """Compile the inference path for every input shape it will see.

        Pass 1 at the longest input (``warm_up_seconds``: the VAD max segment
        or the STT window) pays the accelerator's lazy kernel compilation;
        pass 2 is the steady-state number, compared against ``budget_s`` so a
        too-slow configuration is called out at startup. On an accelerator a
        sweep then visits each shorter :attr:`BUCKET_S` length once, so a
        live session never pays a first-shape compile. Bypasses the Silero
        gate by declaring the input as speech.

        Raises:
            RuntimeError: if the model is not loaded, or on a device fault.
        """
        if not self.is_loaded:
            raise RuntimeError("warm_up() called before load()")
        rng = np.random.default_rng(0)
        sample_count = int(self._warm_up_seconds * SAMPLE_RATE)
        audio = (rng.standard_normal(sample_count) * 0.01).astype(np.float32)
        self._speech_spans(audio)
        timings: list[float] = []
        for _ in range(2):
            started = time.perf_counter()
            self._infer(audio, [(0, sample_count)])
            timings.append(time.perf_counter() - started)
        sweep_started = time.perf_counter()
        shorter_buckets = self._warm_up_buckets(sample_count)
        for length in shorter_buckets:
            self._infer(audio[:length], [(0, length)])
        logger.info(
            "parakeet STT warm-up on %s: pass 1 %.1fs, pass 2 %.1fs (%.0fs input), "
            "%d more input shapes in %.1fs",
            self._torch_device,
            timings[0],
            timings[1],
            self._warm_up_seconds,
            len(shorter_buckets),
            time.perf_counter() - sweep_started,
        )
        if budget_s is not None and timings[1] > budget_s:
            logger.warning(
                "steady-state STT takes %.1fs per %.0fs input on %s, more than "
                "the %.1fs budget: transcription will fall behind live audio "
                "(try a faster WHISPER_DEVICE)",
                timings[1],
                self._warm_up_seconds,
                self._torch_device,
                budget_s,
            )

    def _warm_up_buckets(self, longest: int) -> list[int]:
        """Bucket lengths below ``longest`` worth pre-compiling (accelerators)."""
        bucket = int(self.BUCKET_S * SAMPLE_RATE)
        if bucket <= 0 or self._torch_device not in self.ACCELERATOR_DEVICES:
            return []
        top = self._bucketed_length(longest)
        return list(range(bucket, top, bucket))

    def _speech_spans(self, audio: np.ndarray) -> list[tuple[int, int]] | None:
        """Silero speech ranges as ``(start_sample, end_sample)``, or ``None``."""
        if self._vad_options is None:
            return None
        try:
            from faster_whisper.vad import get_speech_timestamps

            spans = get_speech_timestamps(audio, self._vad_options)
        except Exception as exc:  # pragma: no cover - version-dependent
            logger.warning("Silero VAD failed (%s); continuing without it", exc)
            self._vad_options = None
            return None
        return [(int(span["start"]), int(span["end"])) for span in spans]

    def _no_speech_prob(
        self,
        speech_spans: list[tuple[int, int]] | None,
        start_s: float,
        end_s: float,
        total_samples: int,
    ) -> float:
        """1 - VAD speech coverage of the segment; 0.0 (warned once) without VAD."""
        if speech_spans is None:
            if not self._warned_missing_vad:
                self._warned_missing_vad = True
                logger.warning(
                    "no VAD available: no_speech_prob is pinned to 0.0, so the "
                    "no-speech filter is inactive on this backend"
                )
            return 0.0
        coverage = speech_coverage(
            speech_spans, start_s, end_s, total_samples, SAMPLE_RATE
        )
        return float(min(1.0, max(0.0, 1.0 - coverage)))

    def _warn_missing_scores(self, recorded: int, expected: int) -> None:
        if not self._warned_missing_scores:
            self._warned_missing_scores = True
            logger.warning(
                "parakeet confidence recorder saw %d steps for %d generated "
                "tokens: avg_logprob is pinned to 0.0, so the low-confidence "
                "filter is inactive on this backend",
                recorded,
                expected,
            )
