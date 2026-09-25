"""VAD utterance segmentation (``STT_SEGMENTATION=vad``) — report §1 Tier 2.

The windowed path transcribes a fixed 4.0 s window every 3.5 s hop,
whatever is in it: sentences get chopped mid-clause, the 0.5 s overlap has to
be trimmed and deduplicated afterwards, and music or silence still costs one
engine call per hop. This module cuts the audio at **utterance boundaries**
instead: Silero VAD finds speech, and a clip is handed to the engine only
once its speech has ended (followed by enough silence) or has run to
``max_segment_s``. Clips never overlap, so the overlap trim and suffix dedupe
are switched off for the session (``SessionTextState.overlapping``).

:class:`VadSegmenter` is pure: it looks at the ring buffer's pending audio
(through an injectable span function, so tests can script "speech") and
returns a :class:`SegmentPlan` saying how much audio to release and which
range, if any, to transcribe. The pipeline owns the ring and the engine.

Why the "complete span" rule is exact: Silero (``faster_whisper.vad``)
closes a speech span only after ``min_silence`` of silence and then pads it
by ``speech_pad``, so a closed span ends at least ``min_silence - pad``
before the end of the buffer; a span still in progress ends exactly AT the
end of the buffer. Anything ending ``min_silence - pad`` or more before the
end is therefore complete, and nothing still being spoken can be mistaken
for complete.
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger(__name__)

#: ``audio -> [(start_sample, end_sample), ...]``, buffer-relative.
SpanFn = Callable[[np.ndarray], list[tuple[int, int]]]


@dataclass(frozen=True)
class VadSegmenterConfig:
    """Segmentation timing. The Silero options are derived from these."""

    sample_rate: int = 16000
    #: Silence that ends an utterance (Silero ``min_silence_duration_ms``).
    min_silence_s: float = 0.5
    #: Padding Silero adds around speech (``speech_pad_ms``) — pre-roll, so
    #: word onsets are not clipped.
    speech_pad_s: float = 0.2
    #: Blips shorter than this are not speech (``min_speech_duration_ms``).
    min_speech_s: float = 0.25
    #: Hard cap on one clip; also Silero's ``max_speech_duration_s``, which
    #: splits long speech at its last short pause before this.
    max_segment_s: float = 10.0
    #: Non-speech audio kept at the end of the buffer while waiting: covers
    #: the pad plus Silero's onset latency, so a word just starting survives.
    silence_keep_tail_s: float = 0.5
    #: Complete spans separated by at most this much silence share a clip.
    coalesce_max_gap_s: float = 2.0


@dataclass(frozen=True)
class SegmentPlan:
    """What to do with the buffer's pending audio.

    ``consume_to``: release ``audio[:consume_to]`` from the ring.
    ``speech``: transcribe ``audio[start:end]`` (within the released range),
    or ``None`` when the released audio is non-speech.
    """

    consume_to: int
    speech: tuple[int, int] | None


def speech_coverage(
    spans: Sequence[tuple[int, int]],
    start_s: float,
    end_s: float,
    total_samples: int,
    sample_rate: int,
) -> float:
    """Fraction of ``[start_s, end_s)`` covered by speech ``spans`` (0..1)."""
    segment_start = max(0, int(start_s * sample_rate))
    segment_end = min(total_samples, max(segment_start + 1, int(end_s * sample_rate)))
    overlap = 0
    for span_start, span_end in spans:
        overlap += max(0, min(segment_end, span_end) - max(segment_start, span_start))
    return min(1.0, overlap / max(1, segment_end - segment_start))


def make_silero_span_fn(config: VadSegmenterConfig, threshold: float = 0.5) -> SpanFn:
    """Silero VAD (``faster_whisper.vad``) as a :data:`SpanFn`.

    Imported lazily; the model is an ``lru_cache``'d ONNX session shared
    across calls, and ``get_speech_timestamps`` is stateless per call, so the
    whole pending buffer is re-analyzed each time (a few ms for 10 s).
    """
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    options = VadOptions(
        threshold=threshold,
        min_speech_duration_ms=int(config.min_speech_s * 1000),
        max_speech_duration_s=config.max_segment_s,
        min_silence_duration_ms=int(config.min_silence_s * 1000),
        speech_pad_ms=int(config.speech_pad_s * 1000),
    )

    def silero_spans(audio: np.ndarray) -> list[tuple[int, int]]:
        spans = get_speech_timestamps(
            audio, vad_options=options, sampling_rate=config.sample_rate
        )
        return [(int(span["start"]), int(span["end"])) for span in spans]

    return silero_spans


class VadSegmenter:
    """Decides where utterances start and end in the pending audio."""

    def __init__(self, span_fn: SpanFn, config: VadSegmenterConfig) -> None:
        if config.min_silence_s <= config.speech_pad_s:
            raise ValueError("min_silence_s must exceed speech_pad_s")
        self._span_fn = span_fn
        self._config = config
        rate = config.sample_rate
        self._max_segment = int(config.max_segment_s * rate)
        self._keep_tail = int(config.silence_keep_tail_s * rate)
        self._release_slack = int(0.5 * rate)
        self._complete_gap = int((config.min_silence_s - config.speech_pad_s) * rate)
        self._coalesce_gap = int(config.coalesce_max_gap_s * rate)

    @property
    def config(self) -> VadSegmenterConfig:
        return self._config

    def plan(self, audio: np.ndarray, *, final: bool = False) -> SegmentPlan | None:
        """Plan the next action for ``audio`` (the ring's pending samples).

        Returns ``None`` when the right move is to wait for more audio.
        ``final=True`` (the stop flush) treats every span as complete and
        releases trailing silence entirely.
        """
        total = len(audio)
        if total == 0:
            return None
        spans = self._clean_spans(self._span_fn(audio), total)

        if not spans:
            if final:
                return SegmentPlan(total, None)
            if total > self._keep_tail + self._release_slack:
                return SegmentPlan(total - self._keep_tail, None)
            return None

        complete = [
            (start, end)
            for index, (start, end) in enumerate(spans)
            if final or index + 1 < len(spans) or total - end >= self._complete_gap
        ]
        if not complete:
            open_start = spans[0][0]
            if total - open_start >= self._max_segment:
                cut = open_start + self._max_segment
                return SegmentPlan(cut, (open_start, cut))
            if open_start >= self._release_slack:
                # Release leading non-speech; the span start already
                # includes Silero's pad as pre-roll.
                return SegmentPlan(open_start, None)
            return None

        clip_start, clip_end = complete[0]
        for start, end in complete[1:]:
            if start - clip_end > self._coalesce_gap:
                break
            if end - clip_start > self._max_segment:
                break
            clip_end = end
        clip_end = min(clip_end, clip_start + self._max_segment)
        return SegmentPlan(clip_end, (clip_start, clip_end))

    @staticmethod
    def _clean_spans(
        spans: Sequence[tuple[int, int]], total: int
    ) -> list[tuple[int, int]]:
        """Sorted, clamped to ``[0, total]``, non-empty, overlaps merged."""
        cleaned: list[tuple[int, int]] = []
        for start, end in sorted(
            (max(0, int(start)), min(total, int(end))) for start, end in spans
        ):
            if end <= start:
                continue
            if cleaned and start <= cleaned[-1][1]:
                cleaned[-1] = (cleaned[-1][0], max(cleaned[-1][1], end))
            else:
                cleaned.append((start, end))
        return cleaned
