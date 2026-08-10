"""Local speech-to-text: PCM ring buffer + pluggable Whisper backends.

``AudioRingBuffer`` absorbs 250 ms PCM frames from the WebSocket receive loop
and hands fixed windows to the STT executor. When transcription falls behind
the live stream, the buffer drops its OLDEST audio (watermark policy) — stale
audio on a live stream is worthless — and reports how much was dropped so the
caller can log it and emit an ``stt_overload`` frame.

**Two engines, one filter stack.** :class:`BaseTranscriber` owns everything
that is engine-agnostic — the whole hallucination filter stack and its
per-session dedupe state — and leaves exactly one thing abstract:
:meth:`BaseTranscriber._run_model`, "turn this window of audio into raw
segments". The concrete backends are:

- :class:`FasterWhisperTranscriber` (default) — ctranslate2, CPU and CUDA
  only, with Silero VAD built into the model call.
- :class:`app.stt_torch.TorchWhisperTranscriber` — transformers + PyTorch,
  which is the only way to reach Intel **XPU** and AMD **ROCm**.

Pick one with ``STT_BACKEND``; :func:`create_transcriber` builds it. The
pipeline never learns which is running: ``transcribe_window`` keeps its exact
signature and is invoked positionally through the STT executor.

``transcribe_window`` is synchronous by design — the pipeline runs it on a
single-worker ``ThreadPoolExecutor`` (ctranslate2 releases the GIL; one worker
also keeps a single GPU-resident model serialized, which the per-session
``SessionTextState`` contract relies on). Whisper hallucinates stock outro
phrases on music and silence, so every segment passes a layered filter stack
(VAD; then no-speech probability, average log-probability, a blacklist of
known hallucinations, loop-artifact and window-overlap dedupe) before it
reaches the claim gate.

Heavy engine imports (``faster_whisper``, ``torch``) are deliberately made
inside ``load()``, never at module scope: importing this module must stay
cheap, because the test suite imports :class:`SessionTextState` from here and
must not pay for — or require — either engine.
"""

import logging
import string
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np

from app.models import TranscriptSegment

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.config import Settings

logger = logging.getLogger(__name__)

_PUNCTUATION_TABLE = str.maketrans("", "", string.punctuation)


def _normalize_text(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace (filter compare key)."""
    return " ".join(text.lower().translate(_PUNCTUATION_TABLE).split())


class RawSegment(Protocol):
    """What an engine must hand back per detected segment.

    faster-whisper's own ``Segment`` satisfies this structurally; the torch
    backend builds :class:`SimpleRawSegment`. ``avg_logprob`` and
    ``no_speech_prob`` are load-bearing, not decoration: two of the six
    filters below threshold on them, so an engine that cannot produce real
    values silently disables those filters.
    """

    text: str
    start: float
    end: float
    avg_logprob: float
    no_speech_prob: float


@dataclass
class SimpleRawSegment:
    """Concrete :class:`RawSegment` for engines that lack their own type."""

    text: str
    start: float
    end: float
    avg_logprob: float
    no_speech_prob: float


@dataclass
class SessionTextState:
    """Cross-window text state for ONE session's overlap/dedupe filters.

    Owned by the session pipeline and passed into every
    :meth:`Transcriber.transcribe_window` call. Keeping this state
    per-session (instead of on the process-wide ``Transcriber``) makes it
    structurally impossible for a preempted session's still-running executor
    job to pollute the next session's filters: each job writes only its own
    session's holder. No locking is needed because each session's STT calls
    are serialized by its own loop (one awaited executor job at a time, in
    both the live and flush phases).
    """

    emitted_tail_words: list[str] = field(default_factory=list)
    last_emitted_normalized: str = ""
    # Per-session count of filtered segments by coarse drop reason (report
    # §1 Tier-3 instrumentation): flushed into the analytics sessions row at
    # session end so "how much non-speech reached Whisper" is measurable.
    drop_counts: dict[str, int] = field(default_factory=dict)


class AudioRingBuffer:
    """Float32 mono PCM buffer with drop-oldest watermarks.

    All stream times derive from integer sample counts (no float drift):
    the absolute time of the buffer's first pending sample is the count of
    samples ever released (consumed or dropped) divided by the sample rate.

    ``read_window`` and ``consume`` are deliberately decoupled so the STT loop
    can read a 4.0 s window but consume only the 3.5 s hop, leaving 0.5 s of
    overlap for the next window (the transcriber trims the duplicated words).
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        max_seconds: float = 30.0,
        high_wm_s: float = 12.0,
        low_wm_s: float = 8.0,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError(f"sample_rate must be > 0, got {sample_rate}")
        if not 0.0 < low_wm_s < high_wm_s <= max_seconds:
            raise ValueError(
                "watermarks must satisfy 0 < low < high <= max, got "
                f"low={low_wm_s}, high={high_wm_s}, max={max_seconds}"
            )
        self._sample_rate = sample_rate
        self._high_wm_samples = int(high_wm_s * sample_rate)
        self._low_wm_samples = int(low_wm_s * sample_rate)
        self._samples: np.ndarray = np.empty(0, dtype=np.float32)
        # Samples ever released from the front (consumed + dropped); this is
        # the absolute stream position of self._samples[0].
        self._released_samples = 0

    def append(self, pcm_bytes: bytes) -> float:
        """Append Int16LE mono PCM; return seconds of OLD audio dropped.

        Normally returns 0.0. When pending audio exceeds the high watermark
        (STT cannot keep up with real time), the oldest audio is dropped down
        to the low watermark and the dropped duration is returned.

        Raises:
            ValueError: if the payload length is not a whole number of
                Int16 samples.
        """
        if len(pcm_bytes) % 2 != 0:
            raise ValueError(
                "PCM payload must be an even number of bytes (Int16LE), "
                f"got {len(pcm_bytes)}"
            )
        if not pcm_bytes:
            return 0.0
        new_samples = np.frombuffer(pcm_bytes, dtype="<i2").astype(np.float32)
        new_samples /= 32768.0
        self._samples = np.concatenate((self._samples, new_samples))
        dropped_samples = 0
        if len(self._samples) > self._high_wm_samples:
            dropped_samples = len(self._samples) - self._low_wm_samples
            self._release(dropped_samples)
        return dropped_samples / self._sample_rate

    def read_window(self, seconds: float) -> tuple[np.ndarray, float]:
        """Up to ``seconds`` of audio from the front, plus its stream time.

        Returns:
            ``(audio, stream_time)`` where ``stream_time`` is the absolute
            stream position (in seconds) of the first returned sample. The
            audio is a copy; the buffer is unchanged (use :meth:`consume`).
        """
        sample_count = min(len(self._samples), int(round(seconds * self._sample_rate)))
        stream_time = self._released_samples / self._sample_rate
        return self._samples[:sample_count].copy(), stream_time

    def consume(self, seconds: float) -> None:
        """Advance the read pointer past audio that has been transcribed."""
        self._release(min(len(self._samples), int(round(seconds * self._sample_rate))))

    @property
    def pending_seconds(self) -> float:
        """Seconds of audio waiting to be transcribed."""
        return len(self._samples) / self._sample_rate

    def _release(self, sample_count: int) -> None:
        self._samples = self._samples[sample_count:]
        self._released_samples += sample_count


class BaseTranscriber(ABC):
    """Engine-agnostic filter stack shared by every STT backend.

    Subclasses implement exactly two things — :meth:`load` (bring the engine
    up, loudly) and :meth:`_run_model` (audio in, raw segments out). Every
    hallucination filter, the overlap trimming, and the per-session dedupe
    memory live here so both backends behave identically on the parts that
    took tuning.

    Cross-window text state (the emitted tail used for suffix dedupe and the
    last emitted segment) lives in a per-session :class:`SessionTextState`
    passed into every :meth:`transcribe_window` call, so a preempted
    session's in-flight executor job can never leak tail text into the next
    session. The instance itself holds only the (stateless-per-call) model.
    """

    # Stock phrases Whisper invents on music, silence, and outro-like audio.
    # Compared against normalized text (lowercase, punctuation stripped).
    HALLUCINATION_BLACKLIST: frozenset[str] = frozenset(
        {
            "you",
            "thank you",
            "thank you for watching",
            "thanks for watching",
            "thanks for watching and see you in the next video",
            "see you in the next video",
            "see you in the next one",
            "see you next time",
            "please subscribe",
            "please subscribe to my channel",
            "please like and subscribe",
            "like and subscribe",
            "dont forget to like and subscribe",
            "dont forget to subscribe",
            "thanks for listening",
            "thank you for listening",
            "bye",
            "bye bye",
            "goodbye",
            "music",
            "applause",
            "laughter",
            "silence",
            "subtitles by the amaraorg community",
        }
    )

    OVERLAP_TRIM_TOLERANCE_S: float = 0.15
    MAX_NO_SPEECH_PROB: float = 0.6
    MIN_AVG_LOGPROB: float = -1.0
    SUFFIX_MATCH_MIN_WORDS: int = 4
    EMITTED_TAIL_MAX_WORDS: int = 40

    #: Short name used in logs and /healthz (overridden per backend).
    BACKEND_NAME: str = "base"

    def __init__(
        self,
        model_name: str,
        device: str = "cpu",
        compute_type: str = "int8",
        language: str | None = None,
    ) -> None:
        self._model_name = model_name
        self._device = device
        self._compute_type = compute_type
        # An explicit WHISPER_LANGUAGE wins; otherwise infer, because
        # English-only checkpoints reject a mismatched language kwarg.
        # ``None`` means "let Whisper detect it".
        self._language: str | None = (
            language
            if language is not None
            else ("en" if self._looks_english_only(model_name) else None)
        )
        self._model: Any | None = None

    @staticmethod
    def _looks_english_only(model_name: str) -> bool:
        """Whether a checkpoint name denotes an English-only Whisper model.

        Handles both bare sizes ("small.en") and Hugging Face repo ids, where
        the ".en" suffix sits on the last path segment
        ("openai/whisper-small.en") rather than on the whole string.
        """
        tail = model_name.rsplit("/", 1)[-1].lower()
        return tail.endswith(".en") or tail.endswith("-en")

    # ------------------------------------------------------------------ #
    # Engine hooks — the only things a backend must implement
    # ------------------------------------------------------------------ #

    @abstractmethod
    def load(self) -> None:
        """Blocking model load; lifespan runs it via ``asyncio.to_thread``.

        Must fail loudly: a download, device, or initialization failure
        aborts server startup (wrapped in :class:`RuntimeError`) instead of
        surfacing mid-session.
        """

    @abstractmethod
    def _run_model(self, audio: np.ndarray) -> Iterable[RawSegment]:
        """Transcribe one window of 16 kHz mono float32 audio.

        Times are WINDOW-RELATIVE seconds; the caller shifts them into
        absolute stream time. Implementations must supply real
        ``avg_logprob``/``no_speech_prob`` values — the filter stack depends
        on them.
        """

    def unload(self) -> None:
        """Release engine resources (GPU memory). Default: nothing to do."""
        self._model = None

    # ------------------------------------------------------------------ #
    # Shared surface
    # ------------------------------------------------------------------ #

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def describe(self) -> str:
        """One-line engine description for the startup banner and logs."""
        return (
            f"{self.BACKEND_NAME}:{self._model_name} "
            f"(device={self._device}, compute_type={self._compute_type})"
        )

    @property
    def backend_name(self) -> str:
        return self.BACKEND_NAME

    @property
    def device(self) -> str:
        return self._device

    @property
    def model_name(self) -> str:
        return self._model_name

    def transcribe_window(
        self,
        audio: np.ndarray,
        window_start_s: float,
        last_emitted_end: float,
        text_state: SessionTextState,
    ) -> list[TranscriptSegment]:
        """Transcribe one window; return filtered, absolute-time segments.

        SYNC and executor-run. Segment times are shifted by
        ``window_start_s`` into absolute stream seconds. ``text_state`` is
        the calling session's cross-window dedupe memory; it is read by the
        loop-artifact and suffix filters and updated in place with every
        emitted segment.

        Filter stack, in order:

        1. Overlap trim — drop segments with
           ``end <= last_emitted_end + 0.15`` (already emitted by the
           previous, overlapping window).
        2. ``no_speech_prob > 0.6`` — Whisper thinks it is not speech.
        3. ``avg_logprob < -1.0`` — low-confidence garble.
        4. Hallucination blacklist (normalized exact match).
        5. Identical to the immediately preceding emitted segment (Whisper
           loop artifact on repetitive audio).
        6. Belt-and-braces suffix match: a segment of >= 4 words whose text
           equals the trailing words of previously emitted text is a
           re-transcription of the overlap region, even if its timestamps
           drifted past the time-based trim.

        Raises:
            RuntimeError: if :meth:`load` has not been called.
        """
        if not self.is_loaded:
            raise RuntimeError(
                f"{type(self).__name__}.load() has not been called; "
                "cannot transcribe"
            )
        raw_segments = self._run_model(audio)
        emitted: list[TranscriptSegment] = []
        for raw in raw_segments:
            segment = TranscriptSegment(
                text=raw.text.strip(),
                start=window_start_s + raw.start,
                end=window_start_s + raw.end,
                avg_logprob=raw.avg_logprob,
                no_speech_prob=raw.no_speech_prob,
            )
            normalized = _normalize_text(segment.text)
            drop_reason = self._drop_reason(
                segment, normalized, last_emitted_end, text_state
            )
            if drop_reason is not None:
                coarse_key, detail = drop_reason
                text_state.drop_counts[coarse_key] = (
                    text_state.drop_counts.get(coarse_key, 0) + 1
                )
                logger.debug("dropping segment (%s): %r", detail, segment.text)
                continue
            self._register_emitted(normalized, text_state)
            emitted.append(segment)
        return emitted

    def _drop_reason(
        self,
        segment: TranscriptSegment,
        normalized: str,
        last_emitted_end: float,
        text_state: SessionTextState,
    ) -> tuple[str, str] | None:
        """``(coarse_key, detail)`` when the segment must be dropped, else None.

        The coarse key feeds the per-session ``drop_counts`` instrumentation;
        the detail keeps the precise numbers for debug logging.
        """
        if not normalized:
            return "empty", "empty text"
        if segment.end <= last_emitted_end + self.OVERLAP_TRIM_TOLERANCE_S:
            return "overlap", (
                f"overlap trim: end {segment.end:.2f} <= "
                f"{last_emitted_end:.2f} + {self.OVERLAP_TRIM_TOLERANCE_S}"
            )
        if segment.no_speech_prob > self.MAX_NO_SPEECH_PROB:
            return "no_speech", (
                f"no_speech_prob {segment.no_speech_prob:.2f} > "
                f"{self.MAX_NO_SPEECH_PROB}"
            )
        if segment.avg_logprob < self.MIN_AVG_LOGPROB:
            return "low_confidence", (
                f"avg_logprob {segment.avg_logprob:.2f} < {self.MIN_AVG_LOGPROB}"
            )
        if normalized in self.HALLUCINATION_BLACKLIST:
            return "blacklist", "hallucination blacklist"
        if normalized == text_state.last_emitted_normalized:
            return "loop", "identical to previous segment (loop artifact)"
        if self._matches_emitted_suffix(normalized.split(), text_state):
            return "suffix", "suffix of previously emitted text (window overlap)"
        return None

    def _matches_emitted_suffix(
        self, words: list[str], text_state: SessionTextState
    ) -> bool:
        """True when the whole segment (>= 4 words) is the tail of prior text."""
        if len(words) < self.SUFFIX_MATCH_MIN_WORDS:
            return False
        if len(text_state.emitted_tail_words) < len(words):
            return False
        return text_state.emitted_tail_words[-len(words) :] == words

    def _register_emitted(self, normalized: str, text_state: SessionTextState) -> None:
        text_state.last_emitted_normalized = normalized
        text_state.emitted_tail_words = (
            text_state.emitted_tail_words + normalized.split()
        )[-self.EMITTED_TAIL_MAX_WORDS :]


class FasterWhisperTranscriber(BaseTranscriber):
    """Default engine: faster-whisper / ctranslate2 (CPU and CUDA only).

    Silero VAD runs inside the model call (``vad_filter=True``), which is why
    the shared filter stack treats no-speech/low-confidence scores as a
    SECOND line of defense rather than the first.
    """

    BACKEND_NAME = "faster-whisper"

    #: ctranslate2 accepts only these; "rocm"/"xpu" need the torch backend.
    SUPPORTED_DEVICES: frozenset[str] = frozenset({"cpu", "cuda", "auto"})

    def load(self) -> None:
        """Instantiate the ctranslate2 model (downloads on first use).

        Raises:
            RuntimeError: on an unsupported device or any load failure.
        """
        if self._device not in self.SUPPORTED_DEVICES:
            raise RuntimeError(
                f"STT_BACKEND=faster-whisper cannot use WHISPER_DEVICE="
                f"{self._device!r} — ctranslate2 supports only "
                f"{sorted(self.SUPPORTED_DEVICES)}. For Intel XPU or AMD "
                "ROCm set STT_BACKEND=torch (see scripts/install_stt_gpu.sh)."
            )
        # Imported here, not at module scope: the test suite imports this
        # module for SessionTextState and must not pay for ctranslate2.
        from faster_whisper import WhisperModel

        logger.info(
            "loading faster-whisper model %s (device=%s, compute_type=%s)…",
            self._model_name,
            self._device,
            self._compute_type,
        )
        try:
            self._model = WhisperModel(
                self._model_name,
                device=self._device,
                compute_type=self._compute_type,
            )
        except Exception as exc:
            raise RuntimeError(
                f"failed to load Whisper model {self._model_name!r} "
                f"(device={self._device}, compute_type={self._compute_type}): {exc}"
            ) from exc
        logger.info("faster-whisper model %s loaded", self._model_name)

    def _run_model(self, audio: np.ndarray) -> Iterable[RawSegment]:
        raw_segments, _info = self._model.transcribe(
            audio,
            beam_size=1,
            vad_filter=True,
            condition_on_previous_text=False,
            language=self._language,
        )
        return raw_segments


# Backwards-compatible alias: this was the only engine before STT_BACKEND
# existed, and `Transcriber` still reads well as "the default one".
Transcriber = FasterWhisperTranscriber


#: STT_BACKEND value -> loader. Torch is imported lazily inside the factory
#: so the default install never needs it (mirrors llm_provider's registry).
_STT_BACKENDS: tuple[str, ...] = ("faster-whisper", "torch")


def create_transcriber(settings: "Settings") -> BaseTranscriber:
    """Build the transcriber selected by ``STT_BACKEND``.

    Raises:
        ValueError: on an unknown backend name.
    """
    backend = settings.stt_backend
    if backend == "faster-whisper":
        return FasterWhisperTranscriber(
            model_name=settings.whisper_model,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute_type,
            language=settings.whisper_language_or_none,
        )
    if backend == "torch":
        from app.stt_torch import TorchWhisperTranscriber

        return TorchWhisperTranscriber(
            model_name=settings.whisper_model,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute_type,
            language=settings.whisper_language_or_none,
        )
    raise ValueError(
        f"unknown STT_BACKEND {backend!r}; expected one of {list(_STT_BACKENDS)}"
    )
