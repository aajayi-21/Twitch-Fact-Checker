"""AudioRingBuffer watermark math + Transcriber filter stack (offline).

The filter tests inject a fake Whisper model (scripted raw segments) so no
model download or real inference happens. The one test that runs real Whisper
is marked ``slow`` and excluded by default.
"""

from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from app.transcriber import AudioRingBuffer, SessionTextState, Transcriber
from tests.conftest import pcm_ramp, pcm_silence

SAMPLE_RATE = 16000


# --------------------------------------------------------------------------- #
# AudioRingBuffer
# --------------------------------------------------------------------------- #


class TestRingBufferConstruction:
    def test_rejects_bad_sample_rate(self) -> None:
        with pytest.raises(ValueError, match="sample_rate"):
            AudioRingBuffer(sample_rate=0)

    @pytest.mark.parametrize(
        ("low", "high", "maximum"),
        [
            (8.0, 8.0, 30.0),  # low == high
            (12.0, 8.0, 30.0),  # low > high
            (8.0, 12.0, 10.0),  # high > max
            (0.0, 12.0, 30.0),  # low == 0
        ],
    )
    def test_rejects_bad_watermarks(
        self, low: float, high: float, maximum: float
    ) -> None:
        with pytest.raises(ValueError, match="watermarks"):
            AudioRingBuffer(
                sample_rate=SAMPLE_RATE,
                max_seconds=maximum,
                high_wm_s=high,
                low_wm_s=low,
            )


class TestRingBufferAppend:
    def test_odd_byte_count_rejected(self) -> None:
        ring = AudioRingBuffer(sample_rate=SAMPLE_RATE)
        with pytest.raises(ValueError, match="even number of bytes"):
            ring.append(b"\x00\x00\x00")

    def test_empty_payload_is_noop(self) -> None:
        ring = AudioRingBuffer(sample_rate=SAMPLE_RATE)
        assert ring.append(b"") == 0.0
        assert ring.pending_seconds == 0.0

    def test_pending_seconds_tracks_appends(self) -> None:
        ring = AudioRingBuffer(sample_rate=SAMPLE_RATE)
        ring.append(pcm_silence(0.25))
        ring.append(pcm_silence(0.25))
        assert ring.pending_seconds == pytest.approx(0.5)


class TestRingBufferWatermarks:
    def test_no_drop_at_exactly_high_watermark(self) -> None:
        ring = AudioRingBuffer(
            sample_rate=SAMPLE_RATE, max_seconds=30.0, high_wm_s=12.0, low_wm_s=8.0
        )
        dropped = ring.append(pcm_silence(12.0))
        assert dropped == 0.0
        assert ring.pending_seconds == pytest.approx(12.0)

    def test_overflow_drops_oldest_down_to_low_watermark(self) -> None:
        # 13 s pending -> drop 5 s -> 8 s pending.
        ring = AudioRingBuffer(
            sample_rate=SAMPLE_RATE, max_seconds=30.0, high_wm_s=12.0, low_wm_s=8.0
        )
        assert ring.append(pcm_silence(12.0)) == 0.0
        dropped = ring.append(pcm_silence(1.0))
        assert dropped == pytest.approx(5.0)
        assert ring.pending_seconds == pytest.approx(8.0)

    def test_drop_advances_stream_time(self) -> None:
        ring = AudioRingBuffer(
            sample_rate=SAMPLE_RATE, max_seconds=30.0, high_wm_s=12.0, low_wm_s=8.0
        )
        ring.append(pcm_silence(13.0))  # one oversized append -> immediate drop
        _audio, stream_time = ring.read_window(4.0)
        assert stream_time == pytest.approx(5.0)

    def test_drop_keeps_the_newest_audio(self) -> None:
        ring = AudioRingBuffer(
            sample_rate=SAMPLE_RATE, max_seconds=30.0, high_wm_s=12.0, low_wm_s=8.0
        )
        ring.append(pcm_ramp(13.0))
        audio, _ = ring.read_window(8.0)
        # First surviving sample is the one 5 s into the ramp.
        expected_raw = (5 * SAMPLE_RATE) % 32000
        assert audio[0] == pytest.approx(expected_raw / 32768.0)


class TestRingBufferReadConsume:
    def test_read_window_returns_audio_and_stream_time(self) -> None:
        ring = AudioRingBuffer(sample_rate=SAMPLE_RATE)
        ring.append(pcm_ramp(2.0))
        audio, stream_time = ring.read_window(1.0)
        assert len(audio) == SAMPLE_RATE
        assert stream_time == 0.0
        assert audio.dtype == np.float32

    def test_read_window_does_not_consume(self) -> None:
        ring = AudioRingBuffer(sample_rate=SAMPLE_RATE)
        ring.append(pcm_ramp(1.0))
        ring.read_window(0.5)
        assert ring.pending_seconds == pytest.approx(1.0)

    def test_read_window_returns_a_copy(self) -> None:
        ring = AudioRingBuffer(sample_rate=SAMPLE_RATE)
        ring.append(pcm_ramp(1.0))
        audio, _ = ring.read_window(0.5)
        original_first = audio[0]
        audio[0] = 0.999
        reread, _ = ring.read_window(0.5)
        assert reread[0] == original_first

    def test_consume_advances_stream_time_and_content(self) -> None:
        ring = AudioRingBuffer(sample_rate=SAMPLE_RATE)
        ring.append(pcm_ramp(2.0))
        ring.consume(0.5)
        audio, stream_time = ring.read_window(1.0)
        assert stream_time == pytest.approx(0.5)
        expected_raw = (int(0.5 * SAMPLE_RATE)) % 32000
        assert audio[0] == pytest.approx(expected_raw / 32768.0)
        assert ring.pending_seconds == pytest.approx(1.5)

    def test_read_window_clamps_to_available(self) -> None:
        ring = AudioRingBuffer(sample_rate=SAMPLE_RATE)
        ring.append(pcm_silence(0.5))
        audio, _ = ring.read_window(4.0)
        assert len(audio) == int(0.5 * SAMPLE_RATE)

    def test_consume_beyond_available_is_clamped(self) -> None:
        ring = AudioRingBuffer(sample_rate=SAMPLE_RATE)
        ring.append(pcm_silence(0.5))
        ring.consume(4.0)
        assert ring.pending_seconds == 0.0
        _audio, stream_time = ring.read_window(1.0)
        assert stream_time == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# Transcriber filter stack (fake Whisper model)
# --------------------------------------------------------------------------- #


def raw_segment(
    text: str,
    start: float = 0.0,
    end: float = 1.0,
    avg_logprob: float = -0.3,
    no_speech_prob: float = 0.05,
) -> SimpleNamespace:
    """One faster-whisper-shaped raw segment."""
    return SimpleNamespace(
        text=text,
        start=start,
        end=end,
        avg_logprob=avg_logprob,
        no_speech_prob=no_speech_prob,
    )


class FakeWhisperModel:
    """Scripted stand-in for ``faster_whisper.WhisperModel``."""

    def __init__(self) -> None:
        self.script: list[list[SimpleNamespace]] = []
        self.calls: list[dict[str, Any]] = []

    def transcribe(self, audio: np.ndarray, **kwargs: Any) -> tuple[Any, Any]:
        self.calls.append(kwargs)
        segments = self.script.pop(0) if self.script else []
        return iter(segments), None


def make_transcriber(
    model_name: str = "distil-small.en",
) -> tuple[Transcriber, FakeWhisperModel]:
    transcriber = Transcriber(model_name)
    fake_model = FakeWhisperModel()
    # Inject the fake in place of the (never-downloaded) Whisper model.
    transcriber._model = fake_model
    return transcriber, fake_model


AUDIO = np.zeros(SAMPLE_RATE, dtype=np.float32)


class TestTranscriberSetup:
    def test_transcribe_before_load_raises(self) -> None:
        transcriber = Transcriber("distil-small.en")
        with pytest.raises(RuntimeError, match="load"):
            transcriber.transcribe_window(AUDIO, 0.0, 0.0, SessionTextState())

    def test_english_model_pins_language(self) -> None:
        transcriber, fake_model = make_transcriber("distil-small.en")
        transcriber.transcribe_window(AUDIO, 0.0, 0.0, SessionTextState())
        assert fake_model.calls[0]["language"] == "en"

    def test_multilingual_model_leaves_language_unset(self) -> None:
        transcriber, fake_model = make_transcriber("base")
        transcriber.transcribe_window(AUDIO, 0.0, 0.0, SessionTextState())
        assert fake_model.calls[0]["language"] is None

    def test_model_call_options(self) -> None:
        transcriber, fake_model = make_transcriber()
        transcriber.transcribe_window(AUDIO, 0.0, 0.0, SessionTextState())
        kwargs = fake_model.calls[0]
        assert kwargs["beam_size"] == 1
        assert kwargs["vad_filter"] is True
        assert kwargs["condition_on_previous_text"] is False


class TestTranscriberFilters:
    def test_clean_segment_passes_with_absolute_times(self) -> None:
        transcriber, fake_model = make_transcriber()
        fake_model.script = [[raw_segment("The tower is tall", start=0.5, end=1.5)]]
        segments = transcriber.transcribe_window(AUDIO, 10.0, 0.0, SessionTextState())
        assert len(segments) == 1
        assert segments[0].text == "The tower is tall"
        assert segments[0].start == pytest.approx(10.5)
        assert segments[0].end == pytest.approx(11.5)

    def test_overlap_trim_drops_already_emitted_audio(self) -> None:
        transcriber, fake_model = make_transcriber()
        fake_model.script = [
            [
                raw_segment("old words", start=0.0, end=0.6),  # 3.6 <= 3.5 + 0.15
                raw_segment("new words here", start=0.7, end=1.0),  # 4.0 > 3.65
            ]
        ]
        segments = transcriber.transcribe_window(AUDIO, 3.0, 3.5, SessionTextState())
        assert [s.text for s in segments] == ["new words here"]

    def test_no_speech_prob_filter(self) -> None:
        transcriber, fake_model = make_transcriber()
        fake_model.script = [[raw_segment("ghost words", no_speech_prob=0.9)]]
        assert transcriber.transcribe_window(AUDIO, 0.0, 0.0, SessionTextState()) == []

    def test_avg_logprob_filter(self) -> None:
        transcriber, fake_model = make_transcriber()
        fake_model.script = [[raw_segment("garbled nonsense", avg_logprob=-1.5)]]
        assert transcriber.transcribe_window(AUDIO, 0.0, 0.0, SessionTextState()) == []

    @pytest.mark.parametrize(
        "text",
        ["Thanks for watching!", "Please subscribe.", "THANK YOU FOR WATCHING"],
    )
    def test_hallucination_blacklist_is_normalized(self, text: str) -> None:
        transcriber, fake_model = make_transcriber()
        fake_model.script = [[raw_segment(text)]]
        assert transcriber.transcribe_window(AUDIO, 0.0, 0.0, SessionTextState()) == []

    def test_empty_text_dropped(self) -> None:
        transcriber, fake_model = make_transcriber()
        fake_model.script = [[raw_segment("   ")]]
        assert transcriber.transcribe_window(AUDIO, 0.0, 0.0, SessionTextState()) == []

    def test_loop_artifact_dropped_across_windows(self) -> None:
        transcriber, fake_model = make_transcriber()
        fake_model.script = [
            [raw_segment("we keep saying this", start=0.0, end=1.0)],
            [raw_segment("We keep saying this.", start=0.0, end=1.0)],
        ]
        state = SessionTextState()
        first = transcriber.transcribe_window(AUDIO, 0.0, 0.0, state)
        second = transcriber.transcribe_window(AUDIO, 3.5, 1.0, state)
        assert len(first) == 1
        assert second == []

    def test_suffix_match_drops_retranscribed_overlap(self) -> None:
        transcriber, fake_model = make_transcriber()
        fake_model.script = [
            [raw_segment("the quick brown fox jumps over the lazy dog", end=3.0)],
            # Same trailing words, but timestamps drifted past the time trim.
            [raw_segment("over the lazy dog", start=0.5, end=1.5)],
        ]
        state = SessionTextState()
        first = transcriber.transcribe_window(AUDIO, 0.0, 0.0, state)
        second = transcriber.transcribe_window(AUDIO, 3.5, 3.0, state)
        assert len(first) == 1
        assert second == []

    def test_short_suffix_is_not_dropped(self) -> None:
        transcriber, fake_model = make_transcriber()
        fake_model.script = [
            [raw_segment("the quick brown fox jumps over the lazy dog", end=3.0)],
            [raw_segment("the lazy dog", start=0.5, end=1.5)],  # 3 words < 4
        ]
        state = SessionTextState()
        transcriber.transcribe_window(AUDIO, 0.0, 0.0, state)
        second = transcriber.transcribe_window(AUDIO, 3.5, 3.0, state)
        assert [s.text for s in second] == ["the lazy dog"]

    def test_whole_segment_must_match_not_a_prefix(self) -> None:
        transcriber, fake_model = make_transcriber()
        fake_model.script = [
            [raw_segment("the quick brown fox jumps over the lazy dog", end=3.0)],
            # Contains the tail but adds genuinely new speech -> keep it.
            [raw_segment("over the lazy dog and then some", start=0.5, end=2.0)],
        ]
        state = SessionTextState()
        transcriber.transcribe_window(AUDIO, 0.0, 0.0, state)
        second = transcriber.transcribe_window(AUDIO, 3.5, 3.0, state)
        assert len(second) == 1

    def test_fresh_session_state_has_no_cross_window_memory(self) -> None:
        transcriber, fake_model = make_transcriber()
        fake_model.script = [
            [raw_segment("the quick brown fox jumps over the lazy dog", end=3.0)],
            [raw_segment("over the lazy dog", start=0.5, end=1.5)],
        ]
        transcriber.transcribe_window(AUDIO, 0.0, 0.0, SessionTextState())
        second = transcriber.transcribe_window(AUDIO, 3.5, 3.0, SessionTextState())
        assert [s.text for s in second] == ["over the lazy dog"]

    def test_late_job_from_old_session_cannot_pollute_new_session(self) -> None:
        """Regression: a preempted session's executor job that finishes AFTER
        a new session has started must not seed the new session's dedupe
        filters — the tail state is per-session, so the new session's first
        (identical) segment still passes."""
        transcriber, fake_model = make_transcriber()
        fake_model.script = [
            [raw_segment("the quick brown fox jumps over the lazy dog", end=3.0)],
            [raw_segment("the quick brown fox jumps over the lazy dog", end=3.0)],
        ]
        old_session = SessionTextState()
        new_session = SessionTextState()
        # The OLD session's in-flight window lands after the NEW session's
        # state was created (the un-cancellable run_in_executor case).
        transcriber.transcribe_window(AUDIO, 0.0, 0.0, old_session)
        segments = transcriber.transcribe_window(AUDIO, 0.0, 0.0, new_session)
        assert [s.text for s in segments] == [
            "the quick brown fox jumps over the lazy dog"
        ]


# --------------------------------------------------------------------------- #
# Real-model smoke test (excluded by default; run with: pytest -m slow)
# --------------------------------------------------------------------------- #


@pytest.mark.slow
def test_real_whisper_emits_nothing_for_silence_and_tone() -> None:
    """VAD + hallucination filters must yield zero segments for non-speech."""
    transcriber = Transcriber("tiny.en")
    transcriber.load()

    state = SessionTextState()
    silence = np.zeros(SAMPLE_RATE * 4, dtype=np.float32)
    assert transcriber.transcribe_window(silence, 0.0, 0.0, state) == []

    t = np.arange(SAMPLE_RATE * 4, dtype=np.float32) / SAMPLE_RATE
    tone = (0.2 * np.sin(2.0 * np.pi * 440.0 * t)).astype(np.float32)
    assert transcriber.transcribe_window(tone, 4.0, 0.0, state) == []


class TestDropCounts:
    def test_filtered_segments_increment_coarse_reason_keys(self) -> None:
        """Per-session drop-reason instrumentation (report §1 Tier 3)."""
        transcriber, fake_model = make_transcriber()
        fake_model.script.append(
            [
                raw_segment("thanks for watching", 0.0, 1.0),  # blacklist
                raw_segment("garbled noise", 1.0, 2.0, no_speech_prob=0.9),
                raw_segment("a real sentence spoken here", 2.0, 3.0),
            ]
        )
        state = SessionTextState()
        segments = transcriber.transcribe_window(AUDIO, 0.0, 0.0, state)
        assert [segment.text for segment in segments] == ["a real sentence spoken here"]
        assert state.drop_counts == {"blacklist": 1, "no_speech": 1}
