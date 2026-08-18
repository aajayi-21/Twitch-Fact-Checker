"""Torch STT backend: device/dtype resolution, VAD scoring, factory dispatch.

Everything here runs WITHOUT torch installed: ``resolve_device`` and
``resolve_dtype`` take the torch module as an argument precisely so a fake
can stand in for hardware the CI machine does not have. The one test that
needs a real model is marked ``slow``.
"""

from types import SimpleNamespace
from typing import Any

import pytest

from app.config import Settings
from app.stt_torch import (
    TorchWhisperTranscriber,
    resolve_device,
    resolve_dtype,
)
from app.transcriber import (
    BaseTranscriber,
    FasterWhisperTranscriber,
    create_transcriber,
)


def fake_torch(
    *, cuda: bool = False, xpu: bool = False, hip: str | None = None
) -> SimpleNamespace:
    """A stand-in for the torch module with the accelerators we choose."""
    return SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: cuda),
        xpu=SimpleNamespace(is_available=lambda: xpu),
        version=SimpleNamespace(hip=hip),
        float32="torch.float32",
        float16="torch.float16",
        bfloat16="torch.bfloat16",
    )


class TestResolveDevice:
    def test_auto_prefers_cuda(self) -> None:
        assert resolve_device("auto", fake_torch(cuda=True, xpu=True)) == "cuda"

    def test_auto_falls_back_to_xpu(self) -> None:
        assert resolve_device("auto", fake_torch(cuda=False, xpu=True)) == "xpu"

    def test_auto_falls_back_to_cpu(self) -> None:
        assert resolve_device("auto", fake_torch()) == "cpu"

    def test_cpu_is_always_available(self) -> None:
        assert resolve_device("cpu", fake_torch()) == "cpu"

    def test_rocm_maps_to_the_cuda_device_string(self) -> None:
        """PyTorch's HIP build reuses the CUDA API surface."""
        torch = fake_torch(cuda=True, hip="6.4.0")
        assert resolve_device("rocm", torch) == "cuda"

    def test_rocm_on_a_cuda_build_is_rejected(self) -> None:
        """Otherwise an AMD user silently runs on the wrong stack."""
        with pytest.raises(RuntimeError, match="CUDA build"):
            resolve_device("rocm", fake_torch(cuda=True, hip=None))

    def test_cuda_on_a_rocm_build_warns_but_works(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING", logger="app.stt_torch"):
            assert resolve_device("cuda", fake_torch(cuda=True, hip="6.4.0")) == "cuda"
        assert "ROCm build" in caplog.text

    @pytest.mark.parametrize("device", ["cuda", "rocm", "xpu"])
    def test_missing_accelerator_fails_loudly(self, device: str) -> None:
        """A silent CPU fallback reads as 'the GPU is just slow' forever."""
        with pytest.raises(RuntimeError, match="install_stt_gpu|no XPU|no CUDA"):
            resolve_device(device, fake_torch())

    def test_unknown_device_is_rejected(self) -> None:
        with pytest.raises(RuntimeError, match="unknown WHISPER_DEVICE"):
            resolve_device("tpu", fake_torch())


class TestResolveDtype:
    def test_int8_means_float32_on_cpu(self) -> None:
        """ctranslate2's int8 has no torch analogue; CPU fp16 is slower."""
        assert resolve_dtype("int8", "cpu", fake_torch()) == "torch.float32"

    @pytest.mark.parametrize("device", ["cuda", "xpu"])
    def test_int8_means_float16_on_gpu(self, device: str) -> None:
        assert resolve_dtype("int8", device, fake_torch()) == "torch.float16"

    @pytest.mark.parametrize(
        ("compute_type", "expected"),
        [
            ("float16", "torch.float16"),
            ("fp16", "torch.float16"),
            ("bfloat16", "torch.bfloat16"),
            ("float32", "torch.float32"),
        ],
    )
    def test_explicit_types_win(self, compute_type: str, expected: str) -> None:
        assert resolve_dtype(compute_type, "cuda", fake_torch()) == expected


class TestEnglishOnlyDetection:
    @pytest.mark.parametrize(
        "model_name",
        [
            "openai/whisper-tiny.en",
            "distil-whisper/distil-small.en",
            "whisper-small.en",
        ],
    )
    def test_english_only_names(self, model_name: str) -> None:
        assert TorchWhisperTranscriber._looks_english_only(model_name) is True

    @pytest.mark.parametrize(
        "model_name",
        ["openai/whisper-large-v3-turbo", "openai/whisper-small", "distil-large-v3"],
    )
    def test_multilingual_names(self, model_name: str) -> None:
        assert TorchWhisperTranscriber._looks_english_only(model_name) is False

    def test_explicit_language_overrides_the_heuristic(self) -> None:
        transcriber = TorchWhisperTranscriber("openai/whisper-large-v3", language="de")
        assert transcriber._language == "de"


class TestNoSpeechProb:
    """VAD coverage is what keeps filter #2 alive on this backend."""

    @pytest.fixture()
    def transcriber(self) -> TorchWhisperTranscriber:
        return TorchWhisperTranscriber("openai/whisper-tiny.en")

    def test_fully_covered_window_is_speech(
        self, transcriber: TorchWhisperTranscriber
    ) -> None:
        assert transcriber._no_speech_prob([(0, 64000)], 0.0, 4.0, 64000) == 0.0

    def test_empty_vad_is_no_speech(self, transcriber: TorchWhisperTranscriber) -> None:
        assert transcriber._no_speech_prob([], 0.0, 4.0, 64000) == 1.0

    def test_partial_coverage_is_proportional(
        self, transcriber: TorchWhisperTranscriber
    ) -> None:
        value = transcriber._no_speech_prob([(0, 32000)], 0.0, 4.0, 64000)
        assert value == pytest.approx(0.5, abs=0.01)

    def test_coverage_is_measured_per_segment_range(
        self, transcriber: TorchWhisperTranscriber
    ) -> None:
        """Speech in the first half must not vouch for the second half."""
        spans = [(0, 32000)]
        assert transcriber._no_speech_prob(spans, 0.0, 2.0, 64000) == pytest.approx(0.0)
        assert transcriber._no_speech_prob(spans, 2.0, 4.0, 64000) == pytest.approx(1.0)

    def test_missing_vad_fails_open_and_warns_once(
        self, transcriber: TorchWhisperTranscriber, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING", logger="app.stt_torch"):
            first = transcriber._no_speech_prob(None, 0.0, 4.0, 64000)
            second = transcriber._no_speech_prob(None, 0.0, 4.0, 64000)
        assert first == second == 0.0  # fails OPEN, never drops good speech
        assert caplog.text.count("no VAD available") == 1


class TestCreateTranscriber:
    def test_default_backend_is_faster_whisper(self) -> None:
        transcriber = create_transcriber(Settings(_env_file=None))
        assert isinstance(transcriber, FasterWhisperTranscriber)
        assert transcriber.backend_name == "faster-whisper"

    def test_torch_backend_is_built_lazily(self) -> None:
        settings = Settings(
            stt_backend="torch",
            whisper_model="openai/whisper-tiny.en",
            whisper_device="auto",
            _env_file=None,
        )
        transcriber = create_transcriber(settings)
        assert isinstance(transcriber, TorchWhisperTranscriber)
        # Constructed but NOT loaded: torch is only imported by load().
        assert transcriber.is_loaded is False

    def test_language_setting_reaches_the_backend(self) -> None:
        settings = Settings(
            stt_backend="torch",
            whisper_model="openai/whisper-large-v3",
            whisper_language="fr",
            _env_file=None,
        )
        assert create_transcriber(settings)._language == "fr"

    def test_unknown_backend_is_rejected(self) -> None:
        settings = Settings(_env_file=None)
        object.__setattr__(settings, "stt_backend", "vosk")
        with pytest.raises(ValueError, match="unknown STT_BACKEND"):
            create_transcriber(settings)


class TestFasterWhisperDeviceGuard:
    """ctranslate2 cannot reach XPU/ROCm — say so instead of failing deep."""

    @pytest.mark.parametrize("device", ["xpu", "rocm"])
    def test_gpu_only_devices_are_rejected_with_a_pointer(self, device: str) -> None:
        transcriber = FasterWhisperTranscriber("distil-small.en", device=device)
        with pytest.raises(RuntimeError, match="STT_BACKEND=torch"):
            transcriber.load()


class TestInterfaceParity:
    """Both backends must satisfy what the pipeline calls positionally."""

    @pytest.mark.parametrize(
        "transcriber",
        [
            FasterWhisperTranscriber("distil-small.en"),
            TorchWhisperTranscriber("openai/whisper-tiny.en"),
        ],
        ids=["faster-whisper", "torch"],
    )
    def test_shared_surface(self, transcriber: BaseTranscriber) -> None:
        assert isinstance(transcriber, BaseTranscriber)
        assert transcriber.is_loaded is False
        assert isinstance(transcriber.describe(), str)
        assert transcriber.backend_name
        transcriber.unload()  # safe before load

    @pytest.mark.parametrize(
        "transcriber",
        [
            FasterWhisperTranscriber("distil-small.en"),
            TorchWhisperTranscriber("openai/whisper-tiny.en"),
        ],
        ids=["faster-whisper", "torch"],
    )
    def test_transcribe_before_load_raises(self, transcriber: BaseTranscriber) -> None:
        import numpy as np

        from app.transcriber import SessionTextState

        with pytest.raises(RuntimeError, match="load"):
            transcriber.transcribe_window(
                np.zeros(16000, dtype=np.float32), 0.0, 0.0, SessionTextState()
            )


@pytest.mark.slow
class TestRealTorchModel:
    """End-to-end against a real checkpoint (downloads ~150 MB)."""

    def test_silence_is_skipped_and_scores_are_real(self) -> None:
        import numpy as np

        from app.transcriber import SessionTextState

        transcriber = TorchWhisperTranscriber(
            "openai/whisper-tiny.en", device="cpu", compute_type="int8"
        )
        transcriber.load()
        try:
            # VAD short-circuits silence before the encoder ever runs.
            silence = np.zeros(16000 * 4, dtype=np.float32)
            assert (
                transcriber.transcribe_window(silence, 0.0, 0.0, SessionTextState())
                == []
            )

            # A synthetic tone makes Whisper hallucinate; avg_logprob must be
            # a REAL number low enough for the confidence filter to catch it.
            samples = np.arange(16000 * 4) / 16000
            tone = (0.3 * np.sin(2 * np.pi * 140 * samples)).astype(np.float32)
            transcriber._vad_options = None  # force the encoder path
            raw = list(transcriber._run_model(tone))
            assert raw, "expected the model to emit something for a tone"
            assert all(segment.avg_logprob < 0.0 for segment in raw)
            assert all(segment.avg_logprob != 0.0 for segment in raw)
            state = SessionTextState()
            assert transcriber.transcribe_window(tone, 0.0, 0.0, state) == []
            assert state.drop_counts.get("low_confidence", 0) > 0
        finally:
            transcriber.unload()

    def test_generate_strips_the_forced_prefix_and_scoring_restores_it(self) -> None:
        """Pin the transformers behaviour the scoring pass compensates for.

        Asserting only "a hallucination is dropped" (above) cannot catch a
        systematically too-low avg_logprob, because that bug drops MORE. This
        checks the prefix directly: that ``sequences`` really does come back
        without the start token, and that supplying it changes the score by
        the double-digit margin that decides whether real speech survives.
        """
        import numpy as np
        import torch

        transcriber = TorchWhisperTranscriber(
            "openai/whisper-tiny.en", device="cpu", compute_type="float32"
        )
        transcriber.load()
        try:
            start_id = transcriber._model.generation_config.decoder_start_token_id
            assert transcriber._prefix_token_ids == [start_id]

            samples = np.arange(16000 * 4) / 16000
            tone = (0.3 * np.sin(2 * np.pi * 140 * samples)).astype(np.float32)
            features = transcriber._processor(
                tone, sampling_rate=16000, return_tensors="pt"
            ).input_features.to(transcriber._dtype)
            encoder_outputs = transcriber._model.model.encoder(features)
            sequence = transcriber._model.generate(
                encoder_outputs=encoder_outputs,
                return_timestamps=True,
                return_dict_in_generate=True,
            )["sequences"][0]

            # The behaviour that caused the bug: no <|startoftranscript|>.
            assert int(sequence[0]) != start_id

            corrected = transcriber._avg_logprob(encoder_outputs, sequence)
            # Force the old, unprefixed path by clearing the prefix.
            transcriber._prefix_token_ids = []
            unprefixed = transcriber._avg_logprob(encoder_outputs, sequence)
            assert corrected - unprefixed > 5.0, (
                f"prefix correction is not being applied: {corrected} vs "
                f"{unprefixed}"
            )
            assert torch.isfinite(torch.tensor(corrected))
        finally:
            transcriber.unload()


class TestForcedDecoderPrefix:
    """The scoring pass must rebuild the prefix ``generate`` strips.

    Regression cover for a bug that dropped 100% of real speech:
    transformers rebuilds ``sequences`` from the per-segment ``tokens``, which
    excludes the forced ``<|startoftranscript|>`` prefix the decoder actually
    saw. Teacher-forcing the bare sequence starts the decoder mid-utterance
    and costs ~17 nats — far below ``MIN_AVG_LOGPROB = -1.0``, so every
    correctly transcribed segment was filtered out as "low confidence".
    """

    @staticmethod
    def _transcriber(prefix: list[int], multilingual: bool = False) -> Any:
        transcriber = TorchWhisperTranscriber(
            "openai/whisper-tiny.en", device="cpu", compute_type="float32"
        )
        transcriber._prefix_token_ids = prefix
        transcriber._is_multilingual = multilingual
        return transcriber

    def test_english_only_prefix_is_the_start_token(self) -> None:
        transcriber = self._transcriber([])
        transcriber._model = SimpleNamespace(
            generation_config=SimpleNamespace(decoder_start_token_id=50257)
        )
        assert transcriber._build_prefix_token_ids() == [50257]

    def test_multilingual_prefix_adds_language_and_task(self) -> None:
        transcriber = self._transcriber([], multilingual=True)
        transcriber._language = "es"
        transcriber._model = SimpleNamespace(
            generation_config=SimpleNamespace(
                decoder_start_token_id=50258,
                lang_to_id={"<|es|>": 50262, "<|en|>": 50259},
                task_to_id={"transcribe": 50360, "translate": 50359},
            )
        )
        assert transcriber._build_prefix_token_ids() == [50258, 50262, 50360]

    def test_prefix_is_prepended_before_scoring(self) -> None:
        """The decoder must start on <|startoftranscript|>, not a timestamp."""
        torch = pytest.importorskip("torch")
        transcriber = self._transcriber([50257])
        transcriber._torch = torch
        seen: dict[str, Any] = {}

        def stub(encoder_outputs: Any = None, decoder_input_ids: Any = None) -> Any:
            seen["input"] = decoder_input_ids
            length = int(decoder_input_ids.shape[1])
            return SimpleNamespace(logits=torch.zeros(1, length, 6))

        transcriber._model = stub
        transcriber._avg_logprob(None, torch.tensor([1, 2, 3]))
        assert seen["input"].tolist() == [[50257, 1, 2]]

    def test_prefix_is_not_doubled_when_already_present(self) -> None:
        """Future transformers may keep the prefix; tolerate both shapes."""
        torch = pytest.importorskip("torch")
        transcriber = self._transcriber([50257])
        transcriber._torch = torch
        seen: dict[str, Any] = {}

        def stub(encoder_outputs: Any = None, decoder_input_ids: Any = None) -> Any:
            seen["input"] = decoder_input_ids
            length = int(decoder_input_ids.shape[1])
            return SimpleNamespace(logits=torch.zeros(1, length, 6))

        transcriber._model = stub
        transcriber._avg_logprob(None, torch.tensor([50257, 1, 2, 3]))
        assert seen["input"].tolist() == [[50257, 1, 2]]

    def test_forced_prefix_tokens_are_excluded_from_the_mean(self) -> None:
        """Averaging the forced tokens too would inflate every score.

        Logits are built so the two prefix-derived targets score ~0.0 and the
        three real tokens score exactly -3.0 each. The correct mean is -3.0;
        averaging all five would give -1.8.
        """
        torch = pytest.importorskip("torch")
        transcriber = self._transcriber([0, 1, 2])
        transcriber._torch = torch
        # -1.3398 = the logit that makes log_softmax(target) == -3.0 over a
        # 6-way distribution whose other 5 logits are 0.
        logits = torch.zeros(1, 5, 6)
        for position, target in enumerate([1, 2, 3, 4, 5]):
            logits[0, position, target] = 20.0 if position < 2 else -1.3398

        def stub(encoder_outputs: Any = None, decoder_input_ids: Any = None) -> Any:
            return SimpleNamespace(logits=logits)

        transcriber._model = stub
        result = transcriber._avg_logprob(None, torch.tensor([3, 4, 5]))
        assert result == pytest.approx(-3.0, abs=0.01)


class TestLanguageReachesBothBackends:
    """WHISPER_LANGUAGE must not be a no-op on the default backend.

    The setting was only threaded into the torch branch of the factory, so
    the faster-whisper path silently kept inferring language from the model
    NAME — a config knob that appeared to work and did nothing.
    """

    @pytest.mark.parametrize("backend", ["faster-whisper", "torch"])
    def test_explicit_language_is_honoured(self, backend: str) -> None:
        settings = Settings(
            stt_backend=backend,
            whisper_model="openai/whisper-small",
            whisper_language="es",
            _env_file=None,
        )
        assert create_transcriber(settings)._language == "es"

    @pytest.mark.parametrize("backend", ["faster-whisper", "torch"])
    def test_blank_language_still_infers_from_the_checkpoint(
        self, backend: str
    ) -> None:
        settings = Settings(
            stt_backend=backend,
            whisper_model="openai/whisper-small.en",
            whisper_language="",
            _env_file=None,
        )
        assert create_transcriber(settings)._language == "en"

    @pytest.mark.parametrize("backend", ["faster-whisper", "torch"])
    def test_multilingual_checkpoint_defaults_to_autodetect(self, backend: str) -> None:
        settings = Settings(
            stt_backend=backend,
            whisper_model="openai/whisper-small",
            whisper_language="",
            _env_file=None,
        )
        assert create_transcriber(settings)._language is None

    def test_repo_id_suffix_is_recognised_on_the_default_backend(self) -> None:
        """ "openai/whisper-small.en" — the ".en" is on the last path segment."""
        transcriber = FasterWhisperTranscriber("openai/whisper-small.en")
        assert transcriber._language == "en"
