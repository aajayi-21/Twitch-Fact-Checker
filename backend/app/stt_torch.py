"""PyTorch/transformers speech-to-text — the XPU / CUDA / ROCm backend.

The default engine (ctranslate2 via faster-whisper) reaches only CPU and
CUDA. This backend exists so Intel **XPU** and AMD **ROCm** GPUs work too,
and it plugs into the same :class:`app.transcriber.BaseTranscriber` filter
stack — only :meth:`_run_model` differs.

Three things this module has to get right, because the shared filter stack
depends on them:

1. **Devices.** PyTorch spells AMD ROCm as ``"cuda"`` (HIP reuses the CUDA
   API surface) and Intel as ``"xpu"``. ``WHISPER_DEVICE=rocm`` therefore
   maps to torch device ``cuda`` *and* asserts ``torch.version.hip`` so a
   ROCm typo cannot silently run on an NVIDIA card — or, worse, fall back to
   CPU and quietly halve the frame rate. ``auto`` probes cuda → xpu → cpu.
2. **Confidence scores.** ``avg_logprob`` and ``no_speech_prob`` drive two of
   the six hallucination filters, and transformers hands over NEITHER:
   Whisper's long-form ``generate`` ignores ``output_scores`` entirely
   (``scores`` comes back ``None``, and the per-segment ``result`` holds only
   ``sequences``). So this backend computes them itself —
   ``avg_logprob`` from one teacher-forced decoder pass over the generated
   tokens, ``no_speech_prob`` from Silero VAD coverage. Filling zeros instead
   would disable those filters silently: no error, just a worse
   hallucination rate in production.
3. **VAD.** faster-whisper runs Silero inside its own call
   (``vad_filter=True``); transformers has nothing equivalent. This backend
   reuses ``faster_whisper.vad`` (already a base dependency) BEFORE the
   encoder, which both restores the first line of defense and skips the
   expensive forward pass entirely on music/silence — Whisper pads every
   input to 30 s, so a skipped 4 s window is a large saving.

``torch`` and ``transformers`` are imported inside :meth:`load` so the
default install never needs them.
"""

import logging
from collections.abc import Iterable
from typing import Any

import numpy as np

from app.transcriber import BaseTranscriber, RawSegment, SimpleRawSegment

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000

#: WHISPER_DEVICE -> torch device string. "rocm" is not a torch device; it is
#: a CUDA-API build, disambiguated by torch.version.hip at load time.
_DEVICE_ALIASES: dict[str, str] = {
    "cpu": "cpu",
    "cuda": "cuda",
    "rocm": "cuda",
    "xpu": "xpu",
}

#: WHISPER_COMPUTE_TYPE -> torch dtype name. ctranslate2's "int8" has no
#: PyTorch analogue here, so it means "the sensible default for this device".
_DTYPE_ALIASES: dict[str, str] = {
    "int8": "auto",
    "auto": "auto",
    "float16": "float16",
    "fp16": "float16",
    "half": "float16",
    "bfloat16": "bfloat16",
    "bf16": "bfloat16",
    "float32": "float32",
    "fp32": "float32",
}


def resolve_device(requested: str, torch_module: Any) -> str:
    """Map a ``WHISPER_DEVICE`` value onto a concrete torch device string.

    Args:
        requested: cpu | cuda | rocm | xpu | auto (case-insensitive).
        torch_module: the imported ``torch`` (injected so this is testable
            without a real GPU, and without importing torch at module scope).

    Returns:
        A torch device string — ``"cpu"``, ``"cuda"`` or ``"xpu"``.

    Raises:
        RuntimeError: when the requested accelerator is unavailable. Failing
            loudly at startup is deliberate: a silent CPU fallback looks like
            "the GPU is just slow" and is very hard to notice later.
    """
    wanted = (requested or "auto").strip().lower()
    if wanted == "auto":
        if _cuda_available(torch_module):
            flavour = "rocm" if _is_rocm(torch_module) else "cuda"
            logger.info("WHISPER_DEVICE=auto resolved to %s", flavour)
            return "cuda"
        if _xpu_available(torch_module):
            logger.info("WHISPER_DEVICE=auto resolved to xpu")
            return "xpu"
        logger.info("WHISPER_DEVICE=auto found no GPU; using cpu")
        return "cpu"

    if wanted not in _DEVICE_ALIASES:
        raise RuntimeError(
            f"unknown WHISPER_DEVICE {requested!r}; expected one of "
            f"{sorted({*_DEVICE_ALIASES, 'auto'})}"
        )
    device = _DEVICE_ALIASES[wanted]
    if device == "cpu":
        return "cpu"

    if device == "cuda":
        if not _cuda_available(torch_module):
            raise RuntimeError(
                f"WHISPER_DEVICE={wanted!r} but torch reports no CUDA/ROCm "
                "device. Install the matching PyTorch build with "
                "./scripts/install_stt_gpu.sh (auto|cu128|rocm6.4)."
            )
        is_rocm = _is_rocm(torch_module)
        if wanted == "rocm" and not is_rocm:
            raise RuntimeError(
                "WHISPER_DEVICE=rocm but this PyTorch is a CUDA build "
                "(torch.version.hip is unset). Reinstall with "
                "./scripts/install_stt_gpu.sh rocm6.4, or set "
                "WHISPER_DEVICE=cuda if this really is an NVIDIA GPU."
            )
        if wanted == "cuda" and is_rocm:
            logger.warning(
                "WHISPER_DEVICE=cuda on a ROCm build of PyTorch; running on "
                "HIP (this is normal for AMD GPUs — WHISPER_DEVICE=rocm is "
                "the clearer spelling)"
            )
        return "cuda"

    if not _xpu_available(torch_module):
        raise RuntimeError(
            "WHISPER_DEVICE=xpu but torch reports no XPU device. Install the "
            "Intel build with ./scripts/install_stt_gpu.sh xpu (and check "
            "that the Intel GPU drivers are present)."
        )
    return "xpu"


def _cuda_available(torch_module: Any) -> bool:
    try:
        return bool(torch_module.cuda.is_available())
    except Exception:  # pragma: no cover - defensive, driver-dependent
        return False


def _xpu_available(torch_module: Any) -> bool:
    xpu = getattr(torch_module, "xpu", None)
    if xpu is None:
        return False
    try:
        return bool(xpu.is_available())
    except Exception:  # pragma: no cover - defensive, driver-dependent
        return False


def _is_rocm(torch_module: Any) -> bool:
    """True when this PyTorch is a ROCm/HIP build masquerading as CUDA."""
    return bool(getattr(getattr(torch_module, "version", None), "hip", None))


def resolve_dtype(compute_type: str, device: str, torch_module: Any) -> Any:
    """Map ``WHISPER_COMPUTE_TYPE`` onto a torch dtype for ``device``."""
    wanted = _DTYPE_ALIASES.get((compute_type or "auto").strip().lower(), "auto")
    if wanted == "auto":
        # fp16 is a large win on every accelerator; CPU fp16 is emulated and
        # slower than fp32, so CPU stays fp32.
        wanted = "float32" if device == "cpu" else "float16"
    return getattr(torch_module, wanted)


class TorchWhisperTranscriber(BaseTranscriber):
    """Whisper via transformers, on CPU / CUDA / ROCm / XPU."""

    BACKEND_NAME = "torch"

    #: Whisper's own decoder cap for a 30 s window; our windows are ~4 s, so
    #: this only ever bounds a runaway repetition loop.
    MAX_NEW_TOKENS = 128
    #: Silero speech coverage below this fraction of the window is treated as
    #: "no speech" and skips the encoder entirely.
    MIN_SPEECH_RATIO = 0.02

    def __init__(
        self,
        model_name: str,
        device: str = "auto",
        compute_type: str = "auto",
        language: str | None = None,
    ) -> None:
        super().__init__(
            model_name=model_name,
            device=device,
            compute_type=compute_type,
            language=language,
        )
        self._torch: Any | None = None
        self._processor: Any | None = None
        self._torch_device: str = "cpu"
        self._dtype: Any | None = None
        self._vad_options: Any | None = None
        self._warned_missing_scores = False
        # Whisper's forced decoder prefix; rebuilt at load() because
        # generate() strips it from the sequences it returns.
        self._prefix_token_ids: list[int] = []
        # Authoritative after load(): English-only checkpoints REJECT the
        # language/task kwargs outright, so the name heuristic above is only
        # a pre-load guess.
        self._is_multilingual = False

    def describe(self) -> str:
        return (
            f"{self.BACKEND_NAME}:{self._model_name} "
            f"(device={self._torch_device}, dtype={self._dtype_name()}, "
            f"language={self._language or 'auto'})"
        )

    def _dtype_name(self) -> str:
        return str(self._dtype).removeprefix("torch.") if self._dtype else "?"

    def load(self) -> None:
        """Import torch, resolve the device, and load the model.

        Raises:
            RuntimeError: if torch/transformers are missing, the requested
                accelerator is unavailable, or the model fails to load.
        """
        try:
            import torch
            from transformers import WhisperForConditionalGeneration, WhisperProcessor
        except ImportError as exc:
            raise RuntimeError(
                "STT_BACKEND=torch needs PyTorch and transformers, which are "
                "not installed. Run ./scripts/install_stt_gpu.sh (it picks "
                f"the right wheel for your GPU). Original error: {exc}"
            ) from exc

        # Quiet the Hugging Face stack, which is imported here — too late for
        # configure_logging to have reached it, and it re-sets its own logger
        # level on import anyway. Two separate annoyances: transformers draws
        # a several-hundred-step "Loading weights" bar straight to stderr,
        # and huggingface_hub emits its "unauthenticated request" notice
        # through BOTH its own handler and ours, so it lands twice. Both are
        # kept at DEBUG, where a slow load is worth watching.
        if not logger.isEnabledFor(logging.DEBUG):
            try:
                from transformers.utils import logging as hf_logging

                hf_logging.disable_progress_bar()
                logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
            except Exception as exc:  # pragma: no cover - transformers drift
                logger.debug("could not quiet the Hugging Face loggers: %s", exc)

        self._torch = torch
        self._torch_device = resolve_device(self._device, torch)
        self._dtype = resolve_dtype(self._compute_type, self._torch_device, torch)

        logger.info(
            "loading torch whisper model %s (device=%s, dtype=%s, language=%s)…",
            self._model_name,
            self._torch_device,
            self._dtype_name(),
            self._language or "auto",
        )
        try:
            self._processor = WhisperProcessor.from_pretrained(self._model_name)
            model = WhisperForConditionalGeneration.from_pretrained(
                self._model_name, dtype=self._dtype
            )
            model.to(self._torch_device)
            model.eval()
            self._model = model
            # English-only checkpoints raise if generate() is given
            # language/task at all, so read the truth off the checkpoint
            # rather than trusting the model name.
            self._is_multilingual = bool(
                getattr(model.generation_config, "is_multilingual", False)
            )
            if not self._is_multilingual and self._language not in (None, "en"):
                logger.warning(
                    "WHISPER_LANGUAGE=%s ignored: %s is an English-only "
                    "checkpoint",
                    self._language,
                    self._model_name,
                )
            if not self._is_multilingual:
                self._language = "en"
            self._prefix_token_ids = self._build_prefix_token_ids()
        except Exception as exc:
            raise RuntimeError(
                f"failed to load Whisper model {self._model_name!r} "
                f"(device={self._torch_device}, dtype={self._dtype_name()}): "
                f"{exc}"
            ) from exc

        # Silero VAD, reused from faster-whisper (a base dependency) — the
        # torch path has no built-in VAD of its own.
        try:
            from faster_whisper.vad import VadOptions

            self._vad_options = VadOptions()
        except Exception as exc:  # pragma: no cover - optional hardening
            logger.warning(
                "Silero VAD unavailable (%s); the torch backend will run the "
                "encoder on every window and lean on the text filters alone",
                exc,
            )
            self._vad_options = None

        logger.info(
            "torch whisper model %s ready on %s", self._model_name, self._torch_device
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

    # ------------------------------------------------------------------ #
    # Engine
    # ------------------------------------------------------------------ #

    def _run_model(self, audio: np.ndarray) -> Iterable[RawSegment]:
        torch = self._torch
        speech_spans = self._speech_spans(audio)
        if speech_spans is not None:
            covered = sum(end - start for start, end in speech_spans)
            if covered / max(1, len(audio)) < self.MIN_SPEECH_RATIO:
                # No speech: skip the encoder entirely. Whisper pads every
                # input to 30 s, so this is the single biggest saving here.
                return []

        features = self._processor(
            audio, sampling_rate=SAMPLE_RATE, return_tensors="pt"
        ).input_features.to(self._torch_device, dtype=self._dtype)

        generate_kwargs: dict[str, Any] = {
            "max_new_tokens": self.MAX_NEW_TOKENS,
            # Yields per-segment start/end plus tokens. With
            # return_dict_in_generate this implies return_segments=True.
            "return_timestamps": True,
            "return_dict_in_generate": True,
        }
        # ONLY multilingual checkpoints accept these; whisper-*.en raises.
        if self._is_multilingual:
            generate_kwargs["task"] = "transcribe"
            if self._language is not None:
                generate_kwargs["language"] = self._language

        with torch.inference_mode():
            # Run the encoder ONCE and hand it to both generation and the
            # scoring pass — the encoder is the expensive half, so this makes
            # avg_logprob nearly free instead of doubling the work.
            encoder_outputs = self._model.model.encoder(features)
            outputs = self._model.generate(
                encoder_outputs=encoder_outputs, **generate_kwargs
            )
            sequence = outputs["sequences"][0]
            avg_logprob = self._avg_logprob(encoder_outputs, sequence)

        return self._to_raw_segments(outputs, audio, speech_spans, avg_logprob)

    def _to_raw_segments(
        self,
        outputs: Any,
        audio: np.ndarray,
        speech_spans: list[tuple[int, int]] | None,
        avg_logprob: float,
    ) -> list[RawSegment]:
        """Turn transformers' segment dicts into scored RawSegments."""
        window_seconds = len(audio) / SAMPLE_RATE
        tokenizer = self._processor.tokenizer
        batch_segments = outputs.get("segments") if hasattr(outputs, "get") else None
        raw_segments = batch_segments[0] if batch_segments else []

        pieces: list[tuple[str, float, float]] = []
        for segment in raw_segments:
            text = tokenizer.decode(segment["tokens"], skip_special_tokens=True)
            pieces.append(
                (text, float(segment["start"]), float(segment["end"]))
            )
        if not pieces:
            # No timestamped segments (e.g. generation hit the token cap
            # before emitting one): fall back to the whole window.
            text = tokenizer.decode(
                outputs["sequences"][0], skip_special_tokens=True
            )
            pieces = [(text, 0.0, window_seconds)]

        segments: list[RawSegment] = []
        for text, start, end in pieces:
            cleaned = text.strip()
            if not cleaned:
                continue
            # Whisper can emit timestamps past the (padded) window; clamp so
            # the pipeline's absolute stream clock never runs ahead.
            start = max(0.0, min(start, window_seconds))
            end = max(start, min(end, window_seconds))
            segments.append(
                SimpleRawSegment(
                    text=cleaned,
                    start=start,
                    end=end,
                    avg_logprob=avg_logprob,
                    no_speech_prob=self._no_speech_prob(
                        speech_spans, start, end, len(audio)
                    ),
                )
            )
        return segments

    def _speech_spans(self, audio: np.ndarray) -> list[tuple[int, int]] | None:
        """Silero speech ranges as ``(start_sample, end_sample)``.

        ``None`` means VAD is unavailable, which the callers treat as "no
        information" rather than "no speech".
        """
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
        """1 - (VAD speech coverage of this segment's time range).

        This is a genuine measurement rather than Whisper's own
        ``no_speech_prob``, and it is what keeps filter #2 alive on this
        backend. With VAD unavailable it returns 0.0, which disables that
        filter — logged once so the degradation is never silent.
        """
        if speech_spans is None:
            if not self._warned_missing_scores:
                self._warned_missing_scores = True
                logger.warning(
                    "no VAD available: no_speech_prob is pinned to 0.0, so the "
                    "no-speech filter is inactive on this backend"
                )
            return 0.0
        segment_start = max(0, int(start_s * SAMPLE_RATE))
        segment_end = min(
            total_samples, max(segment_start + 1, int(end_s * SAMPLE_RATE))
        )
        overlap = 0
        for span_start, span_end in speech_spans:
            overlap += max(
                0, min(segment_end, span_end) - max(segment_start, span_start)
            )
        ratio = overlap / max(1, segment_end - segment_start)
        return float(min(1.0, max(0.0, 1.0 - ratio)))

    def _build_prefix_token_ids(self) -> list[int]:
        """Whisper's forced decoder prefix, e.g. ``<|startoftranscript|>``.

        Reconstructed because ``generate`` STRIPS it from the returned
        ``sequences`` (they are rebuilt from the per-segment ``tokens``), yet
        the decoder saw it while generating. Scoring without it starts the
        decoder on a bare timestamp token and costs ~17 nats — enough to push
        every correctly transcribed segment under ``MIN_AVG_LOGPROB`` and
        drop all real speech. See :meth:`_avg_logprob`.
        """
        generation_config = self._model.generation_config
        start_id = getattr(generation_config, "decoder_start_token_id", None)
        if start_id is None:
            return []
        prefix = [int(start_id)]
        if self._is_multilingual:
            # Multilingual checkpoints force <|lang|><|task|> after the start
            # token. With language=auto the real token is whatever generate
            # detected; "en" is the best available guess and being wrong here
            # costs a fraction of a nat, not the ~17 the start token costs.
            lang_to_id = getattr(generation_config, "lang_to_id", None) or {}
            lang_id = lang_to_id.get(f"<|{self._language or 'en'}|>")
            if lang_id is not None:
                prefix.append(int(lang_id))
            task_to_id = getattr(generation_config, "task_to_id", None) or {}
            task_id = task_to_id.get("transcribe")
            if task_id is not None:
                prefix.append(int(task_id))
        return prefix

    def _avg_logprob(self, encoder_outputs: Any, sequence: Any) -> float:
        """Mean per-token log-probability of the generated sequence.

        Whisper's own ``avg_logprob`` is exactly this, and the shared filter
        thresholds it at -1.0. Whisper's long-form ``generate`` refuses to
        return scores (``output_scores`` is ignored), so this teacher-forces
        the generated tokens through the decoder in ONE pass, reusing the
        encoder output that generation already used — the encoder is the
        expensive half, so the extra cost is small.

        The forced prefix is prepended first (see
        :meth:`_build_prefix_token_ids`) because ``sequences`` comes back
        without it; only the tokens of ``sequence`` itself are then averaged,
        so the forced tokens do not inflate the score. If a future
        transformers keeps the prefix, the first token matches and nothing is
        prepended.

        On failure it returns 0.0 (a "confident" value that lets the segment
        through) and warns once: the filter fails OPEN rather than silently
        discarding good speech.
        """
        torch = self._torch
        try:
            if sequence.numel() < 2:
                return 0.0
            scored_count = int(sequence.numel())
            prefix = self._prefix_token_ids
            if prefix and int(sequence[0]) != prefix[0]:
                prefix_tensor = torch.tensor(
                    prefix, dtype=sequence.dtype, device=sequence.device
                )
                sequence = torch.cat([prefix_tensor, sequence])
            decoder_input = sequence[:-1].unsqueeze(0)
            targets = sequence[1:]
            output = self._model(
                encoder_outputs=encoder_outputs, decoder_input_ids=decoder_input
            )
            logprobs = torch.log_softmax(output.logits.float(), dim=-1)
            token_logprobs = logprobs[0, torch.arange(targets.shape[0]), targets]
            # Average over the generated tokens only, never the forced prefix.
            token_logprobs = token_logprobs[-scored_count:]
            finite = token_logprobs[torch.isfinite(token_logprobs)]
            if finite.numel() == 0:
                self._warn_missing_logprob()
                return 0.0
            return float(finite.mean().item())
        except Exception as exc:  # pragma: no cover - transformers drift
            logger.debug("computing avg_logprob failed: %s", exc)
            self._warn_missing_logprob()
            return 0.0

    def _warn_missing_logprob(self) -> None:
        if not self._warned_missing_scores:
            self._warned_missing_scores = True
            logger.warning(
                "generation scores unavailable: avg_logprob is pinned to 0.0, "
                "so the low-confidence filter is inactive on this backend"
            )
