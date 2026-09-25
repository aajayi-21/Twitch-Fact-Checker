"""VadSegmenter: utterance planning over the pending buffer (scripted spans)."""

import numpy as np
import pytest

from app.segmenter import (
    SegmentPlan,
    VadSegmenter,
    VadSegmenterConfig,
    make_silero_span_fn,
    speech_coverage,
)

RATE = 16000
CONFIG = VadSegmenterConfig(
    sample_rate=RATE,
    min_silence_s=0.5,
    speech_pad_s=0.2,
    max_segment_s=10.0,
    silence_keep_tail_s=0.5,
    coalesce_max_gap_s=2.0,
)


def s(seconds: float) -> int:
    return int(seconds * RATE)


def audio(seconds: float) -> np.ndarray:
    return np.zeros(s(seconds), dtype=np.float32)


def segmenter(*spans_in_seconds: tuple[float, float]) -> VadSegmenter:
    spans = [(s(start), s(end)) for start, end in spans_in_seconds]
    return VadSegmenter(lambda _audio: list(spans), CONFIG)


class TestSilence:
    def test_empty_buffer_waits(self) -> None:
        assert segmenter().plan(audio(0)) is None

    def test_short_silence_waits(self) -> None:
        assert segmenter().plan(audio(0.9)) is None

    def test_long_silence_is_released_but_the_tail_kept(self) -> None:
        assert segmenter().plan(audio(3.0)) == SegmentPlan(s(2.5), None)

    def test_final_releases_everything(self) -> None:
        assert segmenter().plan(audio(0.9), final=True) == SegmentPlan(s(0.9), None)


class TestOpenSpeech:
    def test_speech_still_going_waits(self) -> None:
        # Open span: it ends exactly at the end of the buffer.
        assert segmenter((0.2, 3.0)).plan(audio(3.0)) is None

    def test_leading_non_speech_is_released_up_to_the_span(self) -> None:
        plan = segmenter((1.5, 3.0)).plan(audio(3.0))
        assert plan == SegmentPlan(s(1.5), None)

    def test_forced_cut_at_the_maximum_length(self) -> None:
        plan = segmenter((0.0, 12.0)).plan(audio(12.0))
        assert plan == SegmentPlan(s(10.0), (0, s(10.0)))

    def test_forced_cut_seeks_the_longest_pause_before_the_cap(self) -> None:
        rng = np.random.default_rng(0)
        speech = (rng.standard_normal(s(12.0)) * 0.2).astype(np.float32)
        speech[s(7.0) : s(7.3)] = 0.0  # a sentence pause
        speech[s(9.0) : s(9.06)] = 0.0  # a shorter gap between words
        plan = segmenter((0.0, 12.0)).plan(speech)
        assert plan.speech == (0, plan.consume_to)
        assert s(7.0) <= plan.consume_to <= s(7.3)

    def test_forced_cut_never_before_half_the_cap(self) -> None:
        rng = np.random.default_rng(1)
        speech = (rng.standard_normal(s(12.0)) * 0.2).astype(np.float32)
        speech[s(1.0) : s(2.0)] = 0.0  # long pause, but too early to use
        plan = segmenter((0.0, 12.0)).plan(speech)
        assert s(5.0) <= plan.consume_to <= s(10.0)

    def test_final_emits_the_open_span(self) -> None:
        plan = segmenter((0.2, 3.0)).plan(audio(3.0), final=True)
        assert plan == SegmentPlan(s(3.0), (s(0.2), s(3.0)))


class TestCompletion:
    def test_trailing_silence_just_short_of_the_gap_waits(self) -> None:
        # min_silence - pad = 0.3 s of trailing audio marks completion.
        assert segmenter((0.0, 2.0)).plan(audio(2.29)) is None

    def test_trailing_silence_at_the_gap_completes(self) -> None:
        plan = segmenter((0.0, 2.0)).plan(audio(2.3))
        assert plan == SegmentPlan(s(2.0), (0, s(2.0)))

    def test_a_span_followed_by_another_is_complete(self) -> None:
        plan = segmenter((0.0, 2.0), (4.5, 6.0)).plan(audio(6.0))
        # The second span is open, so only the first is transcribed.
        assert plan == SegmentPlan(s(2.0), (0, s(2.0)))

    def test_close_complete_spans_share_a_clip(self) -> None:
        plan = segmenter((0.0, 2.0), (3.0, 5.0)).plan(audio(6.0))
        assert plan == SegmentPlan(s(5.0), (0, s(5.0)))

    def test_distant_spans_do_not_coalesce(self) -> None:
        plan = segmenter((0.0, 2.0), (4.5, 5.0)).plan(audio(6.0))
        assert plan == SegmentPlan(s(2.0), (0, s(2.0)))

    def test_coalescing_stops_at_the_maximum_length(self) -> None:
        plan = segmenter((0.0, 6.0), (7.0, 12.0)).plan(audio(13.0))
        assert plan == SegmentPlan(s(6.0), (0, s(6.0)))

    def test_an_overlong_complete_span_is_clamped(self) -> None:
        plan = segmenter((0.0, 11.0)).plan(audio(12.0))
        assert plan == SegmentPlan(s(10.0), (0, s(10.0)))

    def test_leading_silence_is_released_with_the_clip(self) -> None:
        plan = segmenter((3.0, 4.0)).plan(audio(5.0))
        assert plan == SegmentPlan(s(4.0), (s(3.0), s(4.0)))


class TestSpanHygiene:
    def test_dirty_spans_are_sorted_clamped_merged(self) -> None:
        dirty = VadSegmenter(
            lambda _audio: [(s(3.0), s(4.0)), (-50, s(1.0)), (s(0.5), s(1.5)), (9, 9)],
            CONFIG,
        )
        plan = dirty.plan(audio(5.0))
        # (0, 1.5) and (3, 4) are complete and 1.5 s apart -> one clip.
        assert plan == SegmentPlan(s(4.0), (0, s(4.0)))

    def test_min_silence_must_exceed_the_pad(self) -> None:
        with pytest.raises(ValueError):
            VadSegmenter(
                lambda _audio: [],
                VadSegmenterConfig(min_silence_s=0.2, speech_pad_s=0.2),
            )


class TestSilero:
    def test_span_fn_passes_the_segmenter_timing(self, monkeypatch) -> None:
        import faster_whisper.vad as vad

        seen = {}

        def fake_timestamps(samples, vad_options, sampling_rate):
            seen["options"] = vad_options
            seen["rate"] = sampling_rate
            return [{"start": 100, "end": 900}]

        monkeypatch.setattr(vad, "get_speech_timestamps", fake_timestamps)
        span_fn = make_silero_span_fn(CONFIG)
        assert span_fn(audio(1.0)) == [(100, 900)]
        options = seen["options"]
        assert options.min_silence_duration_ms == 500
        assert options.speech_pad_ms == 200
        assert options.min_speech_duration_ms == 250
        # Silero never splits long speech itself; the segmenter does.
        assert options.max_speech_duration_s == float("inf")
        assert seen["rate"] == RATE

    def test_real_silero_finds_no_speech_in_silence(self) -> None:
        assert make_silero_span_fn(CONFIG)(audio(2.0)) == []


class TestSpeechCoverage:
    def test_partial_coverage(self) -> None:
        spans = [(s(1.0), s(2.0))]
        assert speech_coverage(spans, 0.0, 2.0, s(4.0), RATE) == pytest.approx(0.5)

    def test_no_spans(self) -> None:
        assert speech_coverage([], 0.0, 1.0, s(1.0), RATE) == 0.0
