"""Local speech-to-text: PCM ring buffer + faster-whisper wrapper.

``AudioRingBuffer`` absorbs 250 ms PCM frames from the WebSocket receive loop
and hands fixed windows to the STT executor. When transcription falls behind
the live stream, the buffer drops its OLDEST audio (watermark policy) — stale
audio on a live stream is worthless — and reports how much was dropped so the
caller can log it and emit an ``stt_overload`` frame.

``Transcriber`` wraps one process-wide ``WhisperModel``. ``transcribe_window``
is synchronous by design: the pipeline runs it on a single-worker
``ThreadPoolExecutor`` (ctranslate2 releases the GIL, one worker keeps CPU use
predictable). Whisper hallucinates stock outro phrases on music and silence,
so every segment passes a layered filter stack (VAD is enabled in the model
call itself; then no-speech probability, average log-probability, a blacklist
of known hallucinations, loop-artifact and window-overlap dedupe) before it
reaches the claim gate.
"""

import logging
import string
from dataclasses import dataclass, field

import numpy as np
from faster_whisper import WhisperModel

from app.models import TranscriptSegment

logger = logging.getLogger(__name__)

_PUNCTUATION_TABLE = str.maketrans("", "", string.punctuation)


def _normalize_text(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace (filter compare key)."""
    return " ".join(text.lower().translate(_PUNCTUATION_TABLE).split())


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


class Transcriber:
    """faster-whisper wrapper with overlap trimming and hallucination filters.

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

    def __init__(
        self,
        model_name: str,
        device: str = "cpu",
        compute_type: str = "int8",
    ) -> None:
        self._model_name = model_name
        self._device = device
        self._compute_type = compute_type
        # English-only checkpoints reject a language kwarg mismatch; pin it.
        self._language: str | None = "en" if model_name.endswith(".en") else None
        self._model: WhisperModel | None = None

    def load(self) -> None:
        """Blocking model load; lifespan runs it via ``asyncio.to_thread``.

        Fails loudly: a download or initialization failure aborts server
        startup instead of surfacing mid-session.

        Raises:
            RuntimeError: with the underlying cause when the model cannot be
                downloaded or initialized.
        """
        logger.info(
            "loading whisper model %s (device=%s, compute_type=%s)…",
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
        logger.info("whisper model %s loaded", self._model_name)

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
        if self._model is None:
            raise RuntimeError(
                "Transcriber.load() has not been called; cannot transcribe"
            )
        raw_segments, _info = self._model.transcribe(
            audio,
            beam_size=1,
            vad_filter=True,
            condition_on_previous_text=False,
            language=self._language,
        )
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
