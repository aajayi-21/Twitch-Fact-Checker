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

    whisper_model: str = "distil-small.en"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"

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

    host: str = "127.0.0.1"
    port: int = 8710
    log_level: str = "INFO"

    @property
    def active_gate_model(self) -> str:
        """The gate model of the active provider (for logs/healthz/ready)."""
        if self.llm_provider == "openrouter":
            return self.openrouter_gate_model
        return self.gemini_gate_model

    @property
    def active_verify_model(self) -> str:
        """The verify model of the active provider (for logs/healthz/ready)."""
        if self.llm_provider == "openrouter":
            return self.openrouter_verify_model
        return self.gemini_verify_model

    @property
    def active_api_key(self) -> str:
        """The ACTIVE provider's API key (may be empty when unconfigured)."""
        if self.llm_provider == "openrouter":
            return self.openrouter_api_key
        return self.gemini_api_key

    @property
    def is_configured(self) -> bool:
        """True when the active provider's key is present and non-placeholder.

        Reuses the placeholder hardening from the ``require_*`` checks: an
        empty value, whitespace, or a leftover ``#`` comment (the
        ``.env.example`` inline-comment trap) all count as NOT configured.
        """
        return not self._is_placeholder_key(self.active_api_key)

    def require_llm_api_key(self) -> None:
        """Fail loudly at startup when the ACTIVE provider has no usable key.

        Only the selected provider's key is required: an OpenRouter setup
        needs no Gemini key and vice versa.

        Raises:
            RuntimeError: if the active provider's API key is empty,
                whitespace, or a leftover comment rather than a real key.
        """
        if self.llm_provider == "openrouter":
            self.require_openrouter_api_key()
        else:
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
