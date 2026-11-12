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
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SERVER_VERSION: str = "0.1.0"

# Server-side check_worthiness thresholds. Sensitivity is *never* a prompt
# change — a constant prompt plus a numeric threshold keeps gating
# deterministic and unit-testable.
SENSITIVITY_THRESHOLDS: dict[str, float] = {"low": 0.75, "medium": 0.55, "high": 0.35}

_DEFAULT_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

# OpenRouter is the primary provider; these are the shipped model slugs.
# inception/mercury-2.5-preview: cheap ($0.04/M input) and fast enough for the
# ~300 gate calls an hour, lists temperature + structured_outputs + reasoning
# on its endpoint (so strict JSON mode works first time), and produced zero
# fallback verdicts in production. Override per stage in .env or the options
# page; slugs are validated against the live catalogue on Apply.
DEFAULT_OPENROUTER_GATE_MODEL = "inception/mercury-2.5-preview"
DEFAULT_OPENROUTER_VERIFY_MODEL = "inception/mercury-2.5-preview"

# Anthropic (direct Messages API) is the default provider. Claude Haiku 5.5:
# fast, $0.10/$0.50 per million tokens, strict JSON output, and Anthropic's
# own server-side web_search tool for grounded verification.
DEFAULT_ANTHROPIC_MODEL = "claude-haiku-5-5"
# Estimated marginal cost of one Anthropic verification: web search is
# $10 per 1,000 searches ($0.01 each), a verify runs one or two searches, and
# Haiku's tokens add ~$0.001-0.002. Used only when COST_PER_VERIFY_USD is not
# set explicitly and verification runs on Anthropic.
ANTHROPIC_EST_COST_PER_VERIFY_USD = 0.015
# Exa's documented per-request price for OpenRouter's web plugin.
OPENROUTER_EST_COST_PER_VERIFY_USD = 0.007

#: Accepted reasoning-effort values for the Anthropic stages. "none" turns
#: thinking OFF; "" sends nothing (the model's default); the rest are
#: ``output_config.effort`` levels with adaptive thinking.
AnthropicEffort = Literal["none", "", "low", "medium", "high", "xhigh", "max"]

# Environment variables that, together, configure Workload Identity
# Federation for the Anthropic SDK (the token may come from either variable).
_ANTHROPIC_WIF_REQUIRED_ENV = (
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_ORGANIZATION_ID",
)
_ANTHROPIC_WIF_TOKEN_ENV = ("ANTHROPIC_IDENTITY_TOKEN", "ANTHROPIC_IDENTITY_TOKEN_FILE")


def anthropic_config_dir() -> Path:
    """The Anthropic SDK/CLI configuration directory (``ant auth login`` store).

    Mirrors the SDK's resolution: ``$ANTHROPIC_CONFIG_DIR``, else
    ``%APPDATA%\\Anthropic`` on Windows, else ``~/.config/anthropic``.
    """
    override = os.environ.get("ANTHROPIC_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if os.name == "nt" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "Anthropic"
    return Path.home() / ".config" / "anthropic"


def anthropic_active_profile_name(config_dir: Path) -> str:
    """The profile the SDK would load: ``$ANTHROPIC_PROFILE``, the one-line
    ``active_config`` file written by ``ant profile activate``, else
    ``default``. Only that pointer file is read — never a credentials file.
    """
    explicit = os.environ.get("ANTHROPIC_PROFILE", "").strip()
    if explicit:
        return explicit
    try:
        active = (config_dir / "active_config").read_text(encoding="utf-8").strip()
    except OSError:
        active = ""
    return active or "default"


def anthropic_sdk_credentials_available() -> bool:
    """Whether the Anthropic SDK can authenticate WITHOUT an explicit API key.

    True for ``ANTHROPIC_AUTH_TOKEN``, a complete Workload Identity
    Federation environment, or an ``ant auth login`` profile on disk (both
    ``configs/<profile>.json`` and ``credentials/<profile>.json`` exist).
    Checks environment variables and file EXISTENCE only: the credentials
    file holds tokens and is never opened here. A profile whose refresh
    token has expired still counts — that surfaces per call as a clear
    "run `ant auth login` again" error.
    """
    if os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip():
        return True
    if all(
        os.environ.get(name, "").strip() for name in _ANTHROPIC_WIF_REQUIRED_ENV
    ) and any(os.environ.get(name, "").strip() for name in _ANTHROPIC_WIF_TOKEN_ENV):
        return True
    config_dir = anthropic_config_dir()
    profile = anthropic_active_profile_name(config_dir)
    return (config_dir / "configs" / f"{profile}.json").is_file() and (
        config_dir / "credentials" / f"{profile}.json"
    ).is_file()


# Jev (TypeSafe, via OpenRouter's alpha Decisions API) is an optional
# PRE-SCREEN in front of the gate model, never a gate model itself: it
# answers typed questions with probabilities and cannot write claim text.
# Pinned: "~typesafe/jev-latest" floats to new releases, and the screening
# threshold is calibrated against one release's probabilities.
DEFAULT_JEV_MODEL = "typesafe/jev-1.13"
#: Any Jev id, floating alias included — used to keep Jev OUT of chat slots.
JEV_FAMILY_RE = re.compile(r"^~?typesafe/jev-", re.IGNORECASE)
#: What JEV_MODEL accepts: a pinned release, optionally with its build date
#: (the catalogue's canonical slug, e.g. "typesafe/jev-1.13-20260917").
JEV_PINNED_RE = re.compile(r"^typesafe/jev-\d+\.\d+(-\d{8})?$")


def is_jev_model(model: str) -> bool:
    """Whether ``model`` names a Jev decisions model (floating or pinned)."""
    return bool(JEV_FAMILY_RE.match(model.strip()))


def jev_misplaced_message(field: str, slug: str) -> str:
    """The migration hint for a Jev id found in a chat-model setting."""
    return (
        f"{field}={slug!r}: Jev is a decisions model and cannot extract or "
        "verify claims. It is now an optional pre-screen in front of the gate "
        "model. Set OPENROUTER_GATE_MODEL to a chat model (e.g. "
        f"{DEFAULT_OPENROUTER_GATE_MODEL}), delete OPENROUTER_EXTRACTION_MODEL, "
        "and enable Jev with JEV_MODE=shadow (log only) or JEV_MODE=screen."
    )


#: Padding Silero adds around each speech span in VAD segmentation (pre-roll
#: so word onsets are not clipped). A constant, not a setting: it only has to
#: stay below STT_VAD_MIN_SILENCE_MS.
VAD_SPEECH_PAD_MS = 200

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

    # The keyed LLM provider for both stages (unless overridden per stage):
    #   anthropic  (default) — the direct Anthropic Messages API (Claude
    #              Haiku 5.5 + Anthropic's server-side web_search tool).
    #              Authenticates with ANTHROPIC_API_KEY or, with no key, an
    #              `ant auth login` sign-in (app/llm_anthropic.py).
    #   openrouter — OpenRouter (any catalogue model + the `web` plugin).
    llm_provider: Literal["anthropic", "openrouter"] = "anthropic"

    # Per-stage provider overrides; "" = follow ``llm_provider``. The gate
    # may run on a local Ollama server. The verify Literal deliberately
    # excludes "ollama": local verify has no web-search grounding, so every
    # verdict would be downgraded to UNVERIFIED — a hand-edited
    # VERIFY_PROVIDER=ollama fails loudly at boot instead of half-working.
    gate_provider: Literal["", "anthropic", "openrouter", "ollama"] = ""
    verify_provider: Literal["", "anthropic", "openrouter"] = ""

    # Anthropic (direct API). The key is OPTIONAL: empty = let the Anthropic
    # SDK resolve credentials itself (ANTHROPIC_AUTH_TOKEN, ANTHROPIC_PROFILE,
    # Workload Identity Federation, or the active `ant auth login` profile).
    anthropic_api_key: str = ""
    anthropic_gate_model: str = DEFAULT_ANTHROPIC_MODEL
    anthropic_verify_model: str = DEFAULT_ANTHROPIC_MODEL
    # Gate (claim extraction + contradiction judge): "none" turns thinking
    # OFF — extraction needs no deliberation and thinking tokens would count
    # against the gate's output cap (same finding as the OpenRouter gate).
    anthropic_gate_reasoning_effort: AnthropicEffort = "none"
    # Verify: adaptive thinking at low effort — enough to read search results
    # carefully without adding seconds to every popup.
    anthropic_verify_reasoning_effort: AnthropicEffort = "low"
    # Upper bound on web searches per verification ($0.01 each). Two lets the
    # model refine a first query that missed; the prompt states the cap too.
    anthropic_web_max_uses: int = Field(default=2, ge=1, le=5)
    # Which server-side web_search tool version:
    #   basic   (default) — web_search_20250305: results go straight to the
    #           model; the fastest, simplest round trip.
    #   dynamic — web_search_20260209: the model filters results with code
    #           before reading them (fewer input tokens, more latency).
    anthropic_web_search_tool: Literal["basic", "dynamic"] = "basic"

    # Ollama (or any OpenAI-compatible local server: LM Studio, vLLM,
    # llama.cpp). The base URL is the OpenAI-compatible root INCLUDING /v1;
    # embeddings derive the native root from it (app.embeddings).
    ollama_base_url: str = "http://127.0.0.1:11434/v1"
    ollama_gate_model: str = "gemma3:4b"
    ollama_embed_model: str = "nomic-embed-text"

    openrouter_api_key: str = ""
    openrouter_gate_model: str = DEFAULT_OPENROUTER_GATE_MODEL
    openrouter_verify_model: str = DEFAULT_OPENROUTER_VERIFY_MODEL

    # Jev pre-screen (app/llm_jev.py). Needs the OpenRouter gate (it rides
    # the same key/client); ignored, with a warning, for other gate providers.
    #   off    (default) — no Jev calls.
    #   shadow — Jev runs ALONGSIDE every normal gate pass and its answer is
    #            only recorded (gate_passes table) for calibration; claims
    #            are unaffected. Costs one Decisions request per pass.
    #   screen — Jev runs FIRST; a batch below the threshold skips claim
    #            extraction. Any Jev failure fails OPEN (extraction runs).
    #            Can only lower recall — calibrate in shadow mode first
    #            (scripts/report_jev_calibration.py).
    jev_mode: Literal["off", "shadow", "screen"] = "off"
    jev_model: str = DEFAULT_JEV_MODEL
    # Probability that a checkable assertion exists — NOT probability of
    # truth. Uncalibrated starting point.
    jev_min_check_probability: float = Field(
        default=0.35, ge=0.0, le=1.0, allow_inf_nan=False
    )
    # Deadline for the Jev call alone; must leave room inside GATE_TIMEOUT_S
    # for extraction in screen mode. Jev typically answers in 0.1-0.5 s.
    jev_timeout_s: float = Field(default=3.0, gt=0.0, allow_inf_nan=False)
    openrouter_web_max_results: int = 5
    # Web-search engine for the verify call's ``web`` plugin:
    #   exa    (default) — OpenRouter's Exa search, works for EVERY model and
    #          returns uniform url_citation annotations; $0.007/request.
    #   native — the model provider's own search (OpenAI/Google/...); higher
    #          per-call price, fails on models without one (e.g. mercury).
    #   auto   — native when the model has it, Exa otherwise.
    openrouter_web_engine: Literal["exa", "native", "auto"] = "exa"
    # Reasoning-effort cap for the VERIFY call (bounds latency on
    # reasoning-default models). Empty string = never send ``reasoning`` —
    # for models whose providers reject it under require_parameters routing.
    openrouter_reasoning_effort: str = "low"
    # Reasoning for the GATE call (claim extraction + contradiction judge):
    # "none" (default) explicitly turns reasoning off; "" omits the field
    # (model default); anything else is an effort level. Off because
    # extraction needs no deliberation and thinking tokens count against the
    # gate's output cap: measured on deepseek/deepseek-v4.1-flash, "low"
    # reasoning ran 1,000-1,200 tokens on claim-heavy batches, hit the cap,
    # and returned half-finished thoughts instead of JSON on 7 of 13 of
    # them; with reasoning off the same batches all parsed, in 0.3-2.4 s.
    openrouter_gate_reasoning_effort: str = "none"

    @model_validator(mode="after")
    def validate_jev_settings(self) -> "Settings":
        """Keep Jev out of chat slots and its pre-screen settings coherent."""
        for field in (
            "openrouter_gate_model",
            "openrouter_verify_model",
            "anthropic_gate_model",
            "anthropic_verify_model",
        ):
            slug = getattr(self, field)
            if is_jev_model(slug):
                raise ValueError(jev_misplaced_message(field.upper(), slug))
        if not JEV_PINNED_RE.match(self.jev_model.strip()):
            raise ValueError(
                f"JEV_MODEL={self.jev_model!r}: use a pinned Jev release such as "
                f"{DEFAULT_JEV_MODEL!r} (the floating ~typesafe/jev-latest would "
                "change under a threshold calibrated for one release)"
            )
        if self.jev_mode != "off" and self.jev_timeout_s >= self.gate_timeout_s:
            raise ValueError(
                f"JEV_TIMEOUT_S ({self.jev_timeout_s:g}) must be below "
                f"GATE_TIMEOUT_S ({self.gate_timeout_s:g}) so extraction still "
                "has time after the pre-screen"
            )
        return self

    @property
    def jev_active_mode(self) -> str:
        """``jev_mode`` when the gate runs on OpenRouter, else ``"off"``."""
        if self.resolved_gate_provider != "openrouter":
            return "off"
        return self.jev_mode

    @property
    def active_openrouter_chat_models(self) -> set[str]:
        """OpenRouter chat slugs routed to a stage (Jev is not a chat model)."""
        models: set[str] = set()
        if self.resolved_gate_provider == "openrouter":
            models.add(self.openrouter_gate_model)
        if self.resolved_verify_provider == "openrouter":
            models.add(self.openrouter_verify_model)
        return models

    @property
    def openrouter_reasoning_effort_or_none(self) -> str | None:
        """The verify reasoning effort, with empty/whitespace normalized to None."""
        return self.openrouter_reasoning_effort.strip() or None

    @property
    def openrouter_gate_reasoning_effort_or_none(self) -> str | None:
        """The gate reasoning effort ("none" = off), empty normalized to None."""
        return self.openrouter_gate_reasoning_effort.strip() or None

    # Speech-to-text engine:
    #   "faster-whisper" (default) — ctranslate2; CPU and CUDA only, fastest
    #     on CPU, and the model name is a ctranslate2 name ("distil-small.en").
    #   "torch" — Whisper via transformers + PyTorch; reaches Intel XPU and
    #     AMD ROCm, and the model name is a Hugging Face repo id
    #     ("openai/whisper-small.en"). Install it with
    #     scripts/install_stt_gpu.sh.
    #   "parakeet" — NVIDIA Parakeet TDT via transformers + PyTorch (same
    #     install); markedly more accurate than whisper-small.en and cheap on
    #     variable-length VAD clips. Model: PARAKEET_MODEL. Device/dtype come
    #     from WHISPER_DEVICE / WHISPER_COMPUTE_TYPE.
    stt_backend: Literal["faster-whisper", "torch", "parakeet"] = "faster-whisper"
    # Hugging Face repo id for STT_BACKEND=parakeet. v3 is the Parakeet with
    # official transformers weights (25 European languages, auto-detected).
    parakeet_model: str = "nvidia/parakeet-tdt-0.6b-v3"

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
    def stt_model_name(self) -> str:
        """The active STT engine's model id (healthz / ready frame / logs)."""
        if self.stt_backend == "parakeet":
            return self.parakeet_model
        return self.whisper_model

    @property
    def whisper_language_or_none(self) -> str | None:
        """The configured language, with empty/whitespace normalized to None."""
        return self.whisper_language.strip() or None

    # --- STT resilience (app/stt_supervisor.py) ---
    # Run the full inference path once at startup so accelerator kernel
    # compilation (SYCL/CUDA JIT, several seconds) never lands inside a live
    # session, where it would stall the STT loop and overflow the ring.
    stt_warm_up: bool = True
    # Consecutive failed transcription windows before the engine is treated
    # as broken. A poisoned GPU context fails deterministically within
    # seconds; one or two failures may still be a transient.
    stt_failure_threshold: int = Field(default=3, ge=1)
    # After the threshold: reload the same model on CPU (fp32) ONCE and keep
    # sessions alive in a degraded state, instead of ending them. Off = end
    # the session with a fatal ``stt_failure`` frame straight away.
    stt_cpu_fallback: bool = True
    # Persist the running session counters every N seconds so a crash
    # mid-session does not lose them (they used to be written at end only).
    session_stats_flush_s: float = Field(default=60.0, gt=0)

    # How audio is cut into STT inputs (app/segmenter.py):
    #   window — fixed STT_WINDOW_S windows every STT_HOP_S (overlapping; the
    #            transcriber trims/dedupes the overlap).
    #   vad    — Silero VAD utterances: a clip is transcribed once its speech
    #            ends (STT_VAD_MIN_SILENCE_MS of silence) or reaches
    #            STT_VAD_MAX_SEGMENT_S; silence and music cost no STT call.
    #   auto   (default) — vad for parakeet (no 30 s padding, so variable
    #            lengths are cheap), window for the Whisper backends.
    stt_segmentation: Literal["auto", "window", "vad"] = "auto"
    # Must stay below AUDIO_HIGH_WATERMARK_S: a longer utterance would sit
    # in the ring until the overflow guard dropped its beginning.
    stt_vad_max_segment_s: float = Field(default=10.0, gt=0.0)
    stt_vad_min_silence_ms: int = Field(default=500, gt=0)

    stt_window_s: float = 4.0
    stt_hop_s: float = 3.5
    max_audio_buffer_s: float = 30.0
    audio_high_watermark_s: float = 12.0
    audio_low_watermark_s: float = 8.0

    @property
    def resolved_stt_segmentation(self) -> str:
        """``window`` or ``vad`` (``auto`` resolved by STT backend)."""
        if self.stt_segmentation != "auto":
            return self.stt_segmentation
        return "vad" if self.stt_backend == "parakeet" else "window"

    @property
    def stt_warm_up_budget_s(self) -> float:
        """Steady-state seconds one STT call may take before it falls behind.

        Window mode: the hop (one call per hop). VAD mode: half the longest
        clip — a clip arrives no faster than it is spoken, and the other half
        is headroom for the audio that queues up while the engine works.
        """
        if self.resolved_stt_segmentation == "vad":
            return self.stt_vad_max_segment_s / 2
        return self.stt_hop_s

    @model_validator(mode="after")
    def validate_vad_segmentation(self) -> "Settings":
        """VAD timing must fit the ring buffer (checked only when VAD is on)."""
        if self.resolved_stt_segmentation != "vad":
            return self
        if self.stt_vad_max_segment_s > self.audio_high_watermark_s - 1.0:
            raise ValueError(
                f"STT_VAD_MAX_SEGMENT_S ({self.stt_vad_max_segment_s:g}) must be "
                "at least 1 s below AUDIO_HIGH_WATERMARK_S "
                f"({self.audio_high_watermark_s:g}); a longer utterance would "
                "overflow the audio buffer and lose its beginning"
            )
        if self.stt_vad_min_silence_ms <= VAD_SPEECH_PAD_MS:
            raise ValueError(
                f"STT_VAD_MIN_SILENCE_MS must exceed {VAD_SPEECH_PAD_MS} (the "
                "speech padding the VAD adds around each utterance)"
            )
        return self

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
    # dominates; tokens are noise). Used for the popup/dashboard cost
    # readouts. Unset = derived from the verify provider by
    # ``apply_provider_cost_default``: Exa's $0.007/request on OpenRouter,
    # ~$0.015 on Anthropic (one or two $0.01 searches plus tokens).
    cost_per_verify_usd: float = OPENROUTER_EST_COST_PER_VERIFY_USD

    @model_validator(mode="after")
    def apply_provider_cost_default(self) -> "Settings":
        """Use the Anthropic cost estimate when verify runs there and
        COST_PER_VERIFY_USD was not set explicitly (env, .env or kwarg)."""
        if (
            "cost_per_verify_usd" not in self.model_fields_set
            and self.resolved_verify_provider == "anthropic"
        ):
            object.__setattr__(
                self, "cost_per_verify_usd", ANTHROPIC_EST_COST_PER_VERIFY_USD
            )
        return self

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
            "anthropic": self.anthropic_gate_model,
            "openrouter": self.openrouter_gate_model,
            "ollama": self.ollama_gate_model,
        }[self.resolved_gate_provider]

    @property
    def active_verify_model(self) -> str:
        """The verify model of the verify stage's provider."""
        return {
            "anthropic": self.anthropic_verify_model,
            "openrouter": self.openrouter_verify_model,
        }[self.resolved_verify_provider]

    @property
    def active_api_key(self) -> str:
        """The ``llm_provider``'s API key (may be empty when unconfigured)."""
        if self.llm_provider == "anthropic":
            return self.anthropic_api_key
        return self.openrouter_api_key

    @property
    def anthropic_api_key_or_none(self) -> str | None:
        """The Anthropic key, or None when empty/placeholder (= use sign-in)."""
        if self._is_placeholder_key(self.anthropic_api_key):
            return None
        return self.anthropic_api_key.strip()

    @property
    def anthropic_auth_method(self) -> Literal["api_key", "sign_in"] | None:
        """How the Anthropic provider authenticates, or None if it cannot.

        ``api_key`` when ANTHROPIC_API_KEY holds a real key; ``sign_in`` when
        the SDK can resolve credentials on its own (an ``ant auth login``
        profile, ANTHROPIC_AUTH_TOKEN, or federation).
        """
        if self.anthropic_api_key_or_none is not None:
            return "api_key"
        if anthropic_sdk_credentials_available():
            return "sign_in"
        return None

    def provider_configured(self, provider: str) -> bool:
        """Whether ``provider`` is usable.

        OpenRouter needs a non-placeholder key. Anthropic needs a key OR SDK
        credentials (an ``ant auth login`` profile). Ollama is keyless and
        counts as ALWAYS configured — runtime unreachability surfaces as
        per-call ``GateError`` (and the setup probe reports reachability).
        Unknown names are never configured.
        """
        if provider == "openrouter":
            return not self._is_placeholder_key(self.openrouter_api_key)
        if provider == "anthropic":
            return self.anthropic_auth_method is not None
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

        Ollama needs no key; Anthropic needs a key or an ``ant`` sign-in.

        Raises:
            RuntimeError: if a resolved stage provider has no usable
                credential (an empty, whitespace, or leftover-comment key, and
                for Anthropic no SDK sign-in either).
        """
        for provider in {self.resolved_gate_provider, self.resolved_verify_provider}:
            if provider == "openrouter":
                self.require_openrouter_api_key()
            elif provider == "anthropic":
                self.require_anthropic_credentials()

    def require_anthropic_credentials(self) -> None:
        """Fail loudly when the Anthropic provider has no way to authenticate.

        Raises:
            RuntimeError: if ANTHROPIC_API_KEY is empty/placeholder AND the
                Anthropic SDK finds no sign-in (``ant auth login``),
                ANTHROPIC_AUTH_TOKEN, or federation configuration.
        """
        if self.anthropic_auth_method is None:
            raise RuntimeError(
                "No Anthropic credentials (LLM_PROVIDER=anthropic). Either run "
                "`ant auth login` (no API key needed; pick the Console "
                "organization and workspace that hold your credits) or set "
                "ANTHROPIC_API_KEY in backend/.env "
                "(https://platform.claude.com/settings/keys). The key is "
                "backend-only and must never be shipped in the extension."
            )

    def require_openrouter_api_key(self) -> None:
        """Fail loudly when ``OPENROUTER_API_KEY`` is missing or a placeholder.

        A ``.env`` copied verbatim from
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

    @staticmethod
    def _is_placeholder_key(value: str) -> bool:
        """True for empty/whitespace keys and leftover ``#`` comments."""
        key = value.strip()
        return not key or key.startswith("#")
