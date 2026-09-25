"""Parakeet TDT backend: token timing, segment building, confidence, wiring.

Everything except the ``slow`` class runs without downloading a model: the
transformers processor/model/tokenizer are small fakes that follow the real
call surface (``generate`` drives the logits processors step by step and
returns ``sequences`` + ``durations``, like ``ParakeetTDTGenerationMixin``).
"""

import logging
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app.config import Settings
from app.stt_parakeet import (
    WORD_BOUNDARY,
    ParakeetTranscriber,
    TokenTiming,
    build_segments,
    collect_token_timings,
)
from app.transcriber import SessionTextState, create_transcriber

FRAME_S = 0.08
BLANK, PAD = 5, 4
PIECES = {
    0: f"{WORD_BOUNDARY}The",
    1: f"{WORD_BOUNDARY}tower",
    2: ".",
    3: f"{WORD_BOUNDARY}It",
    PAD: "<pad>",
    BLANK: "<blank>",
    6: f"{WORD_BOUNDARY}3",
    7: "5",
}
VOCAB = 8  # token ids 0..7 (blank included); 5 duration logits follow.


def decode(ids: list[int]) -> str:
    return "".join(PIECES[i] for i in ids if i not in (PAD, BLANK)).replace(
        WORD_BOUNDARY, " "
    )


def timing(token_id: int, start: float, end: float, logprob=-0.1) -> TokenTiming:
    return TokenTiming(token_id=token_id, start_s=start, end_s=end, logprob=logprob)


def segments(tokens, window_s: float = 5.0, pause_split_s: float = 0.8):
    return build_segments(
        tokens,
        piece_of=PIECES.__getitem__,
        decode=decode,
        window_s=window_s,
        frame_s=FRAME_S,
        pause_split_s=pause_split_s,
    )


class TestCollectTokenTimings:
    def test_frames_from_cumulative_durations(self) -> None:
        # start token, The(2), tower(3), blank(4), It(2)
        sequence = [BLANK, 0, 1, BLANK, 3]
        durations = [0, 2, 3, 4, 2]
        logprobs = [-0.1, -0.2, -0.0, -0.3]
        tokens = collect_token_timings(
            sequence, durations, logprobs, {BLANK, PAD}, FRAME_S
        )
        assert [t.token_id for t in tokens] == [0, 1, 3]
        assert [(t.start_s, t.end_s) for t in tokens] == pytest.approx(
            [(0.0, 0.16), (0.16, 0.40), (0.72, 0.88)]
        )
        # logprobs[k - 1] scores step k.
        assert [t.logprob for t in tokens] == [-0.1, -0.2, -0.3]

    def test_unscored_and_out_of_vocabulary(self) -> None:
        tokens = collect_token_timings(
            [BLANK, 0, 99], [0, 1, 1], None, {BLANK}, FRAME_S, vocab_size=VOCAB
        )
        assert [t.token_id for t in tokens] == [0]
        assert tokens[0].logprob is None


class TestBuildSegments:
    def test_sentence_end_splits_before_a_new_word(self) -> None:
        drafts = segments(
            [
                timing(0, 0.0, 0.16),
                timing(1, 0.16, 0.40),
                timing(2, 0.40, 0.40),
                timing(3, 0.48, 0.64),
            ]
        )
        assert [d.text for d in drafts] == ["The tower.", "It"]
        assert (drafts[0].start, drafts[0].end) == pytest.approx((0.0, 0.40))

    def test_decimal_point_does_not_split(self) -> None:
        drafts = segments(
            [timing(6, 0.0, 0.08), timing(2, 0.08, 0.08), timing(7, 0.08, 0.16)]
        )
        assert [d.text for d in drafts] == ["3.5"]

    def test_pause_before_a_word_splits(self) -> None:
        drafts = segments([timing(0, 0.0, 0.16), timing(1, 1.2, 1.4)])
        assert [d.text for d in drafts] == ["The", "tower"]
        drafts = segments([timing(0, 0.0, 0.16), timing(1, 0.5, 0.7)])
        assert [d.text for d in drafts] == ["The tower"]

    def test_mean_logprob_and_fail_open(self) -> None:
        drafts = segments(
            [timing(0, 0.0, 0.1, logprob=-0.2), timing(1, 0.1, 0.2, logprob=-0.4)]
        )
        assert drafts[0].avg_logprob == pytest.approx(-0.3)
        unscored = segments([timing(0, 0.0, 0.1, logprob=None)])
        assert unscored[0].avg_logprob == 0.0

    def test_times_clamped_and_at_least_one_frame(self) -> None:
        drafts = segments([timing(0, 0.9, 0.9), timing(1, 0.95, 1.4)], window_s=1.0)
        (draft,) = drafts
        assert draft.start == pytest.approx(0.9)
        assert draft.end == pytest.approx(1.0)
        zero = segments([timing(0, 0.5, 0.5)])[0]
        assert zero.end == pytest.approx(0.5 + FRAME_S)


class FakeTokenizer:
    all_special_ids = [PAD]

    def convert_ids_to_tokens(self, token_id: int) -> str:
        return PIECES[token_id]

    def decode(self, ids, skip_special_tokens=True) -> str:
        return decode(list(ids))


class FakeProcessor:
    def __init__(self, torch) -> None:
        self._torch = torch
        self.tokenizer = FakeTokenizer()
        self.calls: list[dict] = []

    def __call__(self, audio, sampling_rate, return_tensors, **kwargs):
        self.calls.append({"samples": len(audio), **kwargs})
        frames = 10
        return {
            "input_features": self._torch.zeros(1, frames, 128),
            "attention_mask": self._torch.ones(1, frames, dtype=self._torch.long),
        }


class FakeModel:
    """Greedy TDT decode over scripted (token, duration, confidence) steps."""

    def __init__(self, torch, steps, *, call_processors_for: int | None = None):
        self._torch = torch
        self._steps = steps
        self._call_for = (
            len(steps) if call_processors_for is None else call_processors_for
        )

    def generate(self, *, input_features, attention_mask=None, logits_processor):
        torch = self._torch
        for index, (token, _duration, logit) in enumerate(self._steps):
            if index >= self._call_for:
                break
            scores = torch.full((1, VOCAB + 5), -5.0)
            scores[0, token] = logit
            scores[0, VOCAB] = 50.0  # a duration logit that must never win
            returned = logits_processor(None, scores)
            assert returned[0, VOCAB] == float("-inf")
        return SimpleNamespace(
            sequences=torch.tensor([[BLANK] + [t for t, _, _ in self._steps]]),
            durations=torch.tensor([[0] + [d for _, d, _ in self._steps]]),
        )


def loaded_transcriber(torch, steps, **model_kwargs) -> ParakeetTranscriber:
    """A transcriber in the state load() leaves it in, around fakes."""
    from transformers import LogitsProcessorList

    transcriber = ParakeetTranscriber(model_name="fake/parakeet", device="cpu")
    transcriber._torch = torch
    transcriber._processor = FakeProcessor(torch)
    transcriber._model = FakeModel(torch, steps, **model_kwargs)
    transcriber._logits_processor_list = LogitsProcessorList
    transcriber._torch_device = "cpu"
    transcriber._dtype = torch.float32
    transcriber._vocab_size = VOCAB
    transcriber._skip_ids = {BLANK, PAD}
    transcriber._frame_s = FRAME_S
    return transcriber


STEPS = [  # (token, duration in frames, logit)
    (0, 2, 5.0),
    (1, 3, 3.0),
    (2, 1, 4.0),
    (BLANK, 4, 6.0),
    (BLANK, 4, 6.0),
    (3, 2, 1.0),
]


class TestInference:
    @pytest.fixture()
    def torch(self):
        return pytest.importorskip("torch")

    def test_segments_times_and_recorded_confidence(self, torch) -> None:
        transcriber = loaded_transcriber(torch, STEPS)
        audio = np.zeros(32000, dtype=np.float32)
        raw = transcriber._infer(audio, [(0, 32000)])
        assert [r.text for r in raw] == ["The tower.", "It"]
        assert (raw[0].start, raw[0].end) == pytest.approx((0.0, 0.48))
        assert (raw[1].start, raw[1].end) == pytest.approx((1.12, 1.28))

        def logprob(logit: float) -> float:
            row = torch.full((VOCAB,), -5.0)
            row[0] = logit
            return float(torch.log_softmax(row, dim=-1)[0])

        expected = np.mean([logprob(5.0), logprob(3.0), logprob(4.0)])
        assert raw[0].avg_logprob == pytest.approx(expected, abs=1e-5)
        assert raw[1].avg_logprob == pytest.approx(logprob(1.0), abs=1e-5)
        assert raw[0].no_speech_prob == 0.0  # fully covered by the VAD span

    def test_inputs_are_padded_to_a_whole_bucket(self, torch) -> None:
        transcriber = loaded_transcriber(torch, STEPS)
        transcriber._infer(np.zeros(20000, dtype=np.float32), [(0, 20000)])
        transcriber._infer(np.zeros(32000, dtype=np.float32), [(0, 32000)])
        calls = transcriber._processor.calls
        assert calls[0] == {
            "samples": 20000,
            "padding": "max_length",
            "max_length": 32000,
        }
        assert calls[1] == {"samples": 32000}  # already a whole bucket

    def test_misaligned_recorder_fails_open_and_warns_once(self, torch, caplog) -> None:
        transcriber = loaded_transcriber(torch, STEPS, call_processors_for=3)
        audio = np.zeros(32000, dtype=np.float32)
        with caplog.at_level(logging.WARNING, logger="app.stt_parakeet"):
            first = transcriber._infer(audio, [(0, 32000)])
            transcriber._infer(audio, [(0, 32000)])
        assert all(r.avg_logprob == 0.0 for r in first)
        warnings = [r for r in caplog.records if "confidence recorder" in r.message]
        assert len(warnings) == 1

    def test_transcribe_window_applies_the_shared_filters(self, torch) -> None:
        transcriber = loaded_transcriber(torch, STEPS)
        transcriber._vad_options = None  # no VAD gate in this fake setup
        state = SessionTextState(overlapping=False)
        emitted = transcriber.transcribe_window(
            np.zeros(32000, dtype=np.float32), 10.0, 0.0, state
        )
        assert [s.text for s in emitted] == ["The tower.", "It"]
        assert emitted[0].start == pytest.approx(10.0)


class TestRecorder:
    def test_records_greedy_logprob_and_masks_durations(self) -> None:
        torch = pytest.importorskip("torch")
        from app.stt_parakeet import TokenConfidenceRecorder

        recorder = TokenConfidenceRecorder(vocab_size=3)
        scores = torch.tensor([[2.0, 0.0, 0.0, 9.0, 9.0]])
        returned = recorder(None, scores)
        assert returned is scores
        assert torch.isinf(scores[0, 3:]).all()
        expected = float(torch.log_softmax(torch.tensor([2.0, 0.0, 0.0]), dim=-1)[0])
        assert recorder.logprobs_for(0) == pytest.approx([expected])
        assert recorder.step_count == 1

    def test_no_steps(self) -> None:
        from app.stt_parakeet import TokenConfidenceRecorder

        assert TokenConfidenceRecorder(3).logprobs_for(0) is None


class TestLifecycle:
    def test_bucketing_and_warm_up_shapes(self) -> None:
        transcriber = ParakeetTranscriber(device="cpu")
        assert transcriber._bucketed_length(1) == 16000
        assert transcriber._bucketed_length(16000) == 16000
        assert transcriber._bucketed_length(16001) == 32000
        transcriber._torch_device = "xpu"
        assert transcriber._warm_up_buckets(160000) == [16000 * n for n in range(1, 10)]
        transcriber._torch_device = "cpu"
        assert transcriber._warm_up_buckets(160000) == []

    def test_missing_librosa_names_the_installer(self, monkeypatch) -> None:
        pytest.importorskip("torch")
        pytest.importorskip("transformers")
        import importlib.util

        real_find_spec = importlib.util.find_spec
        monkeypatch.setattr(
            importlib.util,
            "find_spec",
            lambda name, *a: None if name == "librosa" else real_find_spec(name, *a),
        )
        with pytest.raises(RuntimeError, match="install_stt_gpu.sh"):
            ParakeetTranscriber(device="cpu").load()

    def test_cpu_fallback_reloads_in_fp32(self, monkeypatch) -> None:
        transcriber = ParakeetTranscriber(device="xpu", compute_type="auto")
        loads = []
        monkeypatch.setattr(
            transcriber, "load", lambda: loads.append(transcriber._compute_type)
        )
        transcriber.fall_back_to_cpu()
        assert loads == ["float32"]
        assert transcriber.device == "cpu"
        assert transcriber.degraded_from == "xpu"

    def test_warm_up_requires_a_loaded_model(self) -> None:
        with pytest.raises(RuntimeError, match="before load"):
            ParakeetTranscriber(device="cpu").warm_up(1.0)


class TestWiring:
    def test_factory_builds_parakeet_lazily_with_the_vad_input_size(self) -> None:
        settings = Settings(
            _env_file=None,
            stt_backend="parakeet",
            whisper_device="cpu",
            stt_vad_max_segment_s=8.0,
        )
        transcriber = create_transcriber(settings)
        assert isinstance(transcriber, ParakeetTranscriber)
        assert transcriber.model_name == "nvidia/parakeet-tdt-0.6b-v3"
        assert transcriber._warm_up_seconds == 8.0
        assert not transcriber.is_loaded

    def test_settings_resolve_vad_and_report_the_parakeet_model(self) -> None:
        settings = Settings(_env_file=None, stt_backend="parakeet")
        assert settings.resolved_stt_segmentation == "vad"
        assert settings.stt_model_name == "nvidia/parakeet-tdt-0.6b-v3"
        window = Settings(
            _env_file=None, stt_backend="parakeet", stt_segmentation="window"
        )
        assert create_transcriber(window)._warm_up_seconds == window.stt_window_s


FIXTURE_WAV = Path(__file__).parent / "fixtures" / "claims_16k.wav"


@pytest.mark.slow
class TestRealParakeet:
    """Downloads nvidia/parakeet-tdt-0.6b-v3 (~2.5 GB) and runs it on CPU.

    Set PARAKEET_TEST_DEVICE=xpu (or cuda) to exercise an accelerator.
    """

    @pytest.fixture(scope="class")
    def transcriber(self):
        import os

        pytest.importorskip("torch")
        pytest.importorskip("librosa")
        engine = ParakeetTranscriber(
            device=os.environ.get("PARAKEET_TEST_DEVICE", "cpu")
        )
        engine.load()
        return engine

    def test_silence_produces_nothing(self, transcriber) -> None:
        state = SessionTextState(overlapping=False)
        audio = np.zeros(32000, dtype=np.float32)
        assert transcriber.transcribe_window(audio, 0.0, 0.0, state) == []

    def test_fixture_speech(self, transcriber) -> None:
        if not FIXTURE_WAV.exists():
            pytest.skip("run scripts/make_fixture_wav.py to create the fixture")
        with wave.open(str(FIXTURE_WAV)) as handle:
            pcm = handle.readframes(handle.getnframes())
        audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        state = SessionTextState(overlapping=False)
        segments = transcriber.transcribe_window(audio[: 9 * 16000], 0.0, 0.0, state)
        text = " ".join(segment.text for segment in segments).lower()
        assert "eiffel" in text
        assert all(np.isfinite(segment.avg_logprob) for segment in segments)
        starts = [segment.start for segment in segments]
        assert starts == sorted(starts)
