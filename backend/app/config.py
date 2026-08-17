"""Application configuration.

All runtime configuration is sourced from environment variables (optionally
via the ``.env`` file resolved by :func:`resolve_env_file`) and exposed
through a single typed ``Settings`` object. The provider API keys
deliberately default to the empty string: the backend can boot UNCONFIGURED
(no key at all) and acquire one later through ``POST /setup/credentials``.
Use :attr:`Settings.is_configured` to ask whether the active provider has a
usable key.
"""

import os
from pathlib import Path
from typing import Any, Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

SERVER_VERSION: str = "0.1.0"

# Server-side check_worthiness thresholds. Sensitivity is *never* a prompt
# change — a constant prompt plus a numeric threshold keeps gating
# deterministic and unit-testable.
SENSITIVITY_THRESHOLDS: dict[str, float] = {"low": 0.75, "medium": 0.55, "high": 0.35}

_DEFAULT_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

# The analytics database lives next to `.env` by default; tests point DB_PATH
# at temp files instead.
_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "fact_checker.db"


def resolve_env_file() -> Path:
    """The ``.env`` path used for BOTH reading settings and setup persistence.

    The ``ENV_FILE`` environment variable overrides the default
    ``backend/.env`` — tests and smoke runs point it at temp paths so the
    real key file is never touched. Resolved fresh on every call so a
    monkeypatched environment takes effect immediately.
    """
    override = os.environ.get("ENV_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return _DEFAULT_ENV_FILE


class Settings(BaseSettings):
    """Typed view over every key in the resolved ``.env`` (see ``.env.example``)."""

    model_config = SettingsConfigDict(
        env_file=str(_DEFAULT_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    def __init__(self, **kwargs: Any) -> None:
        """Route env-file loading through :func:`resolve_env_file`.

        An explicit ``_env_file`` argument (including ``None`` to disable
        file loading entirely, as tests do) always wins over the override.
        """
        kwargs.setdefault("_env_file", str(resolve_env_file()))
        super().__init__(**kwargs)

    llm_provider: Literal["openrouter", "gemini"] = "openrouter"

    # Per-stage provider overrides; "" = follow the legacy ``llm_provider``
    # switch, so existing single-provider setups are untouched. The verify
    # Literal deliberately excludes "ollama": local verify has no web-search
    # grounding, so every verdict would be downgraded to UNVERIFIED — a
    # hand-edited VERIFY_PROVIDER=ollama fails loudly at boot instead of
    # half-working.
    gate_provider: Literal["", "openrouter", "gemini", "ollama"] = ""
    verify_provider: Literal["", "openrouter", "gemini"] = ""

    # Ollama (or any OpenAI-compatible local server: LM Studio, vLLM,
    # llama.cpp). The base URL is the OpenAI-compatible root INCLUDING /v1;
    # embeddings derive the native root from it (app.embeddings).
    ollama_base_url: str = "http://127.0.0.1:11434/v1"
    ollama_gate_model: str = "gemma3:4b"
    ollama_embed_model: str = "nomic-embed-text"

    openrouter_api_key: str = ""
    openrouter_gate_model: str = "google/gemma-4-26b-a4b-it:free"
    openrouter_verify_model: str = "google/gemma-4-26b-a4b-it:free"
    openrouter_web_max_results: int = 5
    # Reasoning-effort cap sent with every OpenRouter call (bounds latency on
    # reasoning-default models). Empty string = never send ``reasoning`` —
    # for models whose providers reject it under require_parameters routing.
    openrouter_reasoning_effort: str = "low"

    @property
    def openrouter_reasoning_effort_or_none(self) -> str | None:
        """The reasoning effort, with empty/whitespace normalized to None."""
        return self.openrouter_reasoning_effort.strip() or None

    gemini_api_key: str = ""
    gemini_gate_model: str = "gemini-3.1-flash-lite"
    gemini_verify_model: str = "gemini-3.5-flash"

    # Speech-to-text engine:
    #   "faster-whisper" (default) — ctranslate2; CPU and CUDA only, fastest
    #     on CPU, and the model name is a ctranslate2 name ("distil-small.en").
    #   "torch" — transformers + PyTorch; the ONLY way to reach Intel XPU or
    #     AMD ROCm, and the model name is a Hugging Face repo id
    #     ("openai/whisper-small.en"). Install it with
    #     scripts/install_stt_gpu.sh.
    stt_backend: Literal["faster-whisper", "torch"] = "faster-whisper"

    whisper_model: str = "distil-small.en"
    # cpu | cuda | rocm | xpu | auto. "rocm" is an alias for PyTorch's HIP
    # build, which reports itself through the CUDA API surface; the torch
    # backend validates that distinction so a ROCm typo cannot silently fall
    # back to CPU. faster-whisper accepts only cpu/cuda/auto.
    whisper_device: str = "cpu"
    # ctranslate2 quantization ("int8", "float16", …) for faster-whisper; the
    # torch backend maps it onto a dtype (int8 -> float32 on CPU, float16 on
    # GPU) since PyTorch has no equivalent quantized path here.
    whisper_compute_type: str = "int8"
    # Transcription language. Empty = derive it: English-only checkpoints
    # (".en" / "…-en" names) pin "en", everything else auto-detects. Set it
    # explicitly for a multilingual model on a known-language stream.
    whisper_language: str = ""

    @property
    def whisper_language_or_none(self) -> str | None:
        """The configured language, with empty/whitespace normalized to None."""
        return self.whisper_language.strip() or None

    stt_window_s: float = 4.0
    stt_hop_s: float = 3.5
    max_audio_buffer_s: float = 30.0
    audio_high_watermark_s: float = 12.0
    audio_low_watermark_s: float = 8.0

    gate_interval_s: float = 12.0
    gate_timeout_s: float = 15.0
    verify_rpm: float = 8.0
    verify_timeout_s: float = 45.0

    send_transcripts: bool = True
    debug_endpoints: bool = True
    # Attach a captured stream frame to verification when the claim contains
    # a visual cue ("this chart", "on screen", ...). Frames only exist when
    # the extension's opt-in capture toggle is on; they are never persisted.
    vision_enabled: bool = True

    # Which live sessions a new /ws/audio connection preempts (app/sessions.py):
    #   global  - any existing session (DEFAULT — today's behaviour, unchanged).
    #   channel - same (platform, channel) only, so two channels coexist.
    #   none    - never preempt (load tests, deliberate multi-tab).
    #
    # Default stays "global" deliberately. Only the global scope can preempt on
    # CONNECT; the channel scope cannot know the channel until the hello parses,
    # so it leaves the old session alive for up to HELLO_TIMEOUT_S — losing the
    # §3.2 promptness rule the extension's reconnect relies on. And it buys
    # little: the STT executor has ONE worker, so concurrent sessions serialize
    # on a single Whisper model rather than adding throughput. The deployment
    # unit for multiple channels is one backend PROCESS per channel (which is
    # also what the business analysis's "one VPS, <=5 channels" describes).
    session_preempt_scope: Literal["global", "channel", "none"] = "global"
    # Concurrent capture sessions, once the scope allows any. Kept small for the
    # single-STT-worker reason above; raising it does not add throughput.
    max_sessions: int = 4
    # Per-subscriber mailbox depth on the event hub (app/events.py). Deep
    # enough to ride out a page repaint, shallow enough that a wedged consumer
    # shows up as dropped events rather than unbounded memory.
    event_queue_maxsize: int = 64

    # Analytics persistence (app/db.py). One SQLite file; delete it to reset.
    db_path: str = str(_DEFAULT_DB_PATH)
    # Estimated marginal cost of ONE verification attempt (the web-search fee
    # dominates; tokens are noise). Used for the popup/dashboard cost readouts.
    cost_per_verify_usd: float = 0.005

    host: str = "127.0.0.1"
    port: int = 8710
    log_level: str = "INFO"

    @property
    def resolved_gate_provider(self) -> str:
        """The gate stage's provider ("" override falls back to llm_provider)."""
        return self.gate_provider or self.llm_provider

    @property
    def resolved_verify_provider(self) -> str:
        """The verify stage's provider ("" override falls back to llm_provider)."""
        return self.verify_provider or self.llm_provider

    @property
    def active_gate_model(self) -> str:
        """The gate model of the gate stage's provider (logs/healthz/ready)."""
        return {
            "openrouter": self.openrouter_gate_model,
            "gemini": self.gemini_gate_model,
            "ollama": self.ollama_gate_model,
        }[self.resolved_gate_provider]

    @property
    def active_verify_model(self) -> str:
        """The verify model of the verify stage's provider."""
        return {
            "openrouter": self.openrouter_verify_model,
            "gemini": self.gemini_verify_model,
        }[self.resolved_verify_provider]

    @property
    def active_api_key(self) -> str:
        """The LEGACY provider's API key (may be empty when unconfigured).

        Still keyed off ``llm_provider`` (which can only be a keyed
        provider); per-stage code paths use :meth:`provider_configured`.
        """
        if self.llm_provider == "openrouter":
            return self.openrouter_api_key
        return self.gemini_api_key

    def provider_configured(self, provider: str) -> bool:
        """Whether ``provider`` is usable.

        Keyed providers need a non-placeholder key. Ollama is keyless and
        counts as ALWAYS configured — runtime unreachability surfaces as
        per-call ``GateError`` (and the setup probe reports reachability).
        Unknown names are never configured.
        """
        if provider == "openrouter":
            return not self._is_placeholder_key(self.openrouter_api_key)
        if provider == "gemini":
            return not self._is_placeholder_key(self.gemini_api_key)
        return provider == "ollama"

    @property
    def is_configured(self) -> bool:
        """True when EVERY distinct resolved stage provider is configured.

        Reuses the placeholder hardening from the ``require_*`` checks: an
        empty value, whitespace, or a leftover ``#`` comment (the
        ``.env.example`` inline-comment trap) all count as NOT configured.
        """
        return all(
            self.provider_configured(provider)
            for provider in {self.resolved_gate_provider, self.resolved_verify_provider}
        )

    def require_llm_api_key(self) -> None:
        """Fail loudly at startup when an ACTIVE stage provider has no key.

        Only the resolved stage providers' keys are required (Ollama needs
        none): an OpenRouter setup needs no Gemini key and vice versa.

        Raises:
            RuntimeError: if a resolved stage provider's API key is empty,
                whitespace, or a leftover comment rather than a real key.
        """
        for provider in {self.resolved_gate_provider, self.resolved_verify_provider}:
            if provider == "openrouter":
                self.require_openrouter_api_key()
            elif provider == "gemini":
                self.require_gemini_api_key()

    def require_openrouter_api_key(self) -> None:
        """Fail loudly when ``OPENROUTER_API_KEY`` is missing or a placeholder.

        Same hardening as the Gemini check: a ``.env`` copied verbatim from
        ``.env.example`` must fail here too — python-dotenv parses an inline
        comment after an EMPTY value as the value itself, so a ``#``-prefixed
        "key" is a leftover comment, not a real key.

        Raises:
            RuntimeError: if ``OPENROUTER_API_KEY`` is empty, whitespace, or
                a leftover comment rather than a real key.
        """
        if self._is_placeholder_key(self.openrouter_api_key):
            raise RuntimeError(
                "OPENROUTER_API_KEY is not set (and LLM_PROVIDER=openrouter). "
                "Copy backend/.env.example to backend/.env and fill in your "
                "OpenRouter API key (https://openrouter.ai/keys). Web search "
                "costs credits even on :free models, so hold a small credit "
                "balance. The key is backend-only and must never be shipped "
                "in the extension."
            )

    def require_gemini_api_key(self) -> None:
        """Fail loudly when ``GEMINI_API_KEY`` is missing or a placeholder.

        A ``.env`` copied verbatim from ``.env.example`` must fail here too:
        python-dotenv parses an inline comment after an EMPTY value as the
        value itself, so a ``#``-prefixed "key" is a leftover comment, not a
        real key.

        Raises:
            RuntimeError: if ``GEMINI_API_KEY`` is empty, whitespace, or a
                leftover comment rather than a real key.
        """
        if self._is_placeholder_key(self.gemini_api_key):
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Copy backend/.env.example to "
                "backend/.env and fill in your Gemini API key "
                "(https://aistudio.google.com/apikey). The key is backend-only "
                "and must never be shipped in the extension."
            )

    @staticmethod
    def _is_placeholder_key(value: str) -> bool:
        """True for empty/whitespace keys and leftover ``#`` comments."""
        key = value.strip()
        return not key or key.startswith("#")
