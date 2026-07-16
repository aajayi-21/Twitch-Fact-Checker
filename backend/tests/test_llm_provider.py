"""Provider factory: the right client/gate/checker per ``LLM_PROVIDER``."""

from types import SimpleNamespace

import pytest
from openai import AsyncOpenAI

from app.config import Settings
from app.llm_gemini import GeminiClaimGate, GeminiFactChecker
from app.llm_openrouter import (
    OPENROUTER_BASE_URL,
    OpenRouterClaimGate,
    OpenRouterFactChecker,
)
from app.llm_provider import (
    close_llm_client,
    create_claim_gate,
    create_fact_checker,
    create_llm_client,
)
from app.rate_limit import QuotaCooldown


def make_settings(provider: str) -> Settings:
    return Settings(
        llm_provider=provider,  # type: ignore[arg-type]
        openrouter_api_key="sk-or-offline-test",
        gemini_api_key="offline-test-key",
        _env_file=None,
    )


class TestCreateLlmClient:
    async def test_openrouter_builds_asyncopenai_at_openrouter_base_url(self) -> None:
        client = create_llm_client(make_settings("openrouter"))
        try:
            assert isinstance(client, AsyncOpenAI)
            assert str(client.base_url).rstrip("/") == OPENROUTER_BASE_URL
            assert client.max_retries == 0  # the app owns retries/cooldowns
        finally:
            await close_llm_client(client)

    async def test_gemini_builds_genai_client(self) -> None:
        from google import genai

        client = create_llm_client(make_settings("gemini"))
        try:
            assert isinstance(client, genai.Client)
        finally:
            await close_llm_client(client)


class TestCreateGateAndChecker:
    def test_openrouter_classes(self) -> None:
        settings = make_settings("openrouter")
        dummy_client = SimpleNamespace()
        gate = create_claim_gate(settings, dummy_client)
        checker = create_fact_checker(settings, dummy_client, QuotaCooldown())
        assert isinstance(gate, OpenRouterClaimGate)
        assert isinstance(checker, OpenRouterFactChecker)

    def test_gemini_classes(self) -> None:
        settings = make_settings("gemini")
        dummy_client = SimpleNamespace()
        gate = create_claim_gate(settings, dummy_client)
        checker = create_fact_checker(settings, dummy_client, QuotaCooldown())
        assert isinstance(gate, GeminiClaimGate)
        assert isinstance(checker, GeminiFactChecker)

    def test_active_models_follow_the_provider(self) -> None:
        settings = Settings(
            llm_provider="openrouter",
            openrouter_gate_model="or-gate",
            openrouter_verify_model="or-verify",
            gemini_gate_model="gm-gate",
            gemini_verify_model="gm-verify",
            _env_file=None,
        )
        assert settings.active_gate_model == "or-gate"
        assert settings.active_verify_model == "or-verify"
        gemini_settings = settings.model_copy(update={"llm_provider": "gemini"})
        assert gemini_settings.active_gate_model == "gm-gate"
        assert gemini_settings.active_verify_model == "gm-verify"


class TestUnknownProviderIsRejected:
    def test_settings_reject_unknown_provider(self) -> None:
        with pytest.raises(ValueError):
            Settings(llm_provider="anthropic", _env_file=None)  # type: ignore[arg-type]
