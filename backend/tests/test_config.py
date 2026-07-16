"""Settings / API-key startup checks for BOTH providers.

Regression coverage for the ``.env.example`` inline-comment trap: python-dotenv
parses an inline comment after an EMPTY value as the value itself, so a
``.env`` copied verbatim from ``.env.example`` must still fail the startup
key check loudly — for whichever provider is active.
"""

from pathlib import Path

import pytest

from app.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"


class TestRequireGeminiApiKey:
    def test_env_example_copied_verbatim_fails_loudly(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text(ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        settings = Settings(_env_file=str(env_file))
        with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
            settings.require_gemini_api_key()

    def test_env_example_non_key_values_still_parse(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("WHISPER_MODEL", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text(ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        settings = Settings(_env_file=str(env_file))
        assert settings.whisper_model == "distil-small.en"

    @pytest.mark.parametrize(
        "bad_key",
        ["", "   ", "# required, backend-only, never in the extension"],
    )
    def test_empty_whitespace_or_comment_keys_are_rejected(self, bad_key: str) -> None:
        settings = Settings(gemini_api_key=bad_key, _env_file=None)
        with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
            settings.require_gemini_api_key()

    def test_real_key_passes(self) -> None:
        settings = Settings(gemini_api_key="AIza-fake-but-plausible", _env_file=None)
        settings.require_gemini_api_key()


class TestRequireOpenRouterApiKey:
    def test_env_example_copied_verbatim_fails_loudly(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text(ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        settings = Settings(_env_file=str(env_file))
        assert settings.llm_provider == "openrouter"  # .env.example default
        with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
            settings.require_openrouter_api_key()

    @pytest.mark.parametrize(
        "bad_key",
        ["", "   ", "# required, backend-only, never in the extension"],
    )
    def test_empty_whitespace_or_comment_keys_are_rejected(self, bad_key: str) -> None:
        settings = Settings(openrouter_api_key=bad_key, _env_file=None)
        with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
            settings.require_openrouter_api_key()

    def test_real_key_passes(self) -> None:
        settings = Settings(openrouter_api_key="sk-or-v1-plausible", _env_file=None)
        settings.require_openrouter_api_key()


class TestRequireLlmApiKey:
    """Startup requires the ACTIVE provider's key — and only that one."""

    def test_openrouter_provider_without_openrouter_key_fails(self) -> None:
        settings = Settings(
            llm_provider="openrouter",
            openrouter_api_key="",
            gemini_api_key="AIza-present-but-inactive",
            _env_file=None,
        )
        with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
            settings.require_llm_api_key()

    def test_openrouter_provider_needs_no_gemini_key(self) -> None:
        settings = Settings(
            llm_provider="openrouter",
            openrouter_api_key="sk-or-v1-plausible",
            gemini_api_key="",
            _env_file=None,
        )
        settings.require_llm_api_key()

    def test_gemini_provider_without_gemini_key_fails(self) -> None:
        settings = Settings(
            llm_provider="gemini",
            openrouter_api_key="sk-or-present-but-inactive",
            gemini_api_key="",
            _env_file=None,
        )
        with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
            settings.require_llm_api_key()

    def test_gemini_provider_needs_no_openrouter_key(self) -> None:
        settings = Settings(
            llm_provider="gemini",
            openrouter_api_key="",
            gemini_api_key="AIza-fake-but-plausible",
            _env_file=None,
        )
        settings.require_llm_api_key()
