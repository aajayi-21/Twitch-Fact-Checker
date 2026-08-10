"""Provider registry: the right client/gate/checker per stage provider."""

from types import SimpleNamespace

import pytest
from openai import AsyncOpenAI

from app.config import Settings
from app.llm_gemini import GeminiClaimGate, GeminiFactChecker
from app.llm_local import LocalClaimGate
from app.llm_openrouter import (
    OPENROUTER_BASE_URL,
    OpenRouterClaimGate,
    OpenRouterFactChecker,
)
from app.llm_provider import (
    LLMRuntime,
    build_llm_runtime,
    close_llm_client,
    close_llm_runtime,
    create_claim_gate,
    create_fact_checker,
    create_llm_client,
    get_provider_spec,
)
from app.rate_limit import QuotaCooldown


def make_settings(provider: str, **overrides: object) -> Settings:
    return Settings(
        llm_provider=provider,  # type: ignore[arg-type]
        openrouter_api_key="sk-or-offline-test",
        gemini_api_key="offline-test-key",
        **overrides,  # type: ignore[arg-type]
        _env_file=None,
    )


class TestCreateLlmClient:
    async def test_openrouter_builds_asyncopenai_at_openrouter_base_url(self) -> None:
        client = create_llm_client(make_settings("openrouter"), "openrouter")
        try:
            assert isinstance(client, AsyncOpenAI)
            assert str(client.base_url).rstrip("/") == OPENROUTER_BASE_URL
            assert client.max_retries == 0  # the app owns retries/cooldowns
        finally:
            await close_llm_client(client)

    async def test_gemini_builds_genai_client(self) -> None:
        from google import genai

        client = create_llm_client(make_settings("gemini"), "gemini")
        try:
            assert isinstance(client, genai.Client)
        finally:
            await close_llm_client(client)

    async def test_ollama_builds_asyncopenai_at_local_base_url(self) -> None:
        settings = make_settings(
            "openrouter", ollama_base_url="http://127.0.0.1:11434/v1"
        )
        client = create_llm_client(settings, "ollama")
        try:
            assert isinstance(client, AsyncOpenAI)
            assert str(client.base_url).rstrip("/") == "http://127.0.0.1:11434/v1"
            assert client.max_retries == 0
        finally:
            await close_llm_client(client)

    def test_unknown_provider_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="unknown LLM provider"):
            get_provider_spec("anthropic")


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

    def test_ollama_gate_class(self) -> None:
        settings = make_settings("openrouter", gate_provider="ollama")
        gate = create_claim_gate(settings, SimpleNamespace())
        assert isinstance(gate, LocalClaimGate)

    def test_ollama_cannot_verify(self) -> None:
        """Belt-and-braces guard behind the config-level Literal rejection."""
        settings = make_settings("openrouter")
        object.__setattr__(settings, "verify_provider", "ollama")
        with pytest.raises(ValueError, match="cannot verify"):
            create_fact_checker(settings, SimpleNamespace(), QuotaCooldown())

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

    def test_active_models_follow_stage_overrides(self) -> None:
        settings = Settings(
            llm_provider="openrouter",
            gate_provider="ollama",
            openrouter_verify_model="or-verify",
            ollama_gate_model="local-gate",
            _env_file=None,
        )
        assert settings.active_gate_model == "local-gate"
        assert settings.active_verify_model == "or-verify"


class TestBuildLlmRuntime:
    async def test_same_provider_shares_one_client(self) -> None:
        runtime = build_llm_runtime(make_settings("openrouter"), QuotaCooldown())
        try:
            assert runtime.configured
            assert runtime.verify_client is runtime.gate_client
        finally:
            await close_llm_runtime(runtime)

    async def test_split_providers_build_distinct_clients(self) -> None:
        settings = make_settings(
            "openrouter",
            gate_provider="ollama",
            verify_provider="openrouter",
        )
        runtime = build_llm_runtime(settings, QuotaCooldown())
        try:
            assert runtime.configured
            assert runtime.gate_client is not runtime.verify_client
            assert isinstance(runtime.gate, LocalClaimGate)
            assert isinstance(runtime.checker, OpenRouterFactChecker)
        finally:
            await close_llm_runtime(runtime)

    def test_unconfigured_stage_yields_bare_runtime(self) -> None:
        settings = Settings(
            llm_provider="openrouter",
            gate_provider="ollama",
            verify_provider="openrouter",
            openrouter_api_key="",
            _env_file=None,
        )
        runtime = build_llm_runtime(settings, QuotaCooldown())
        assert not runtime.configured
        assert runtime.gate_client is None and runtime.verify_client is None

    def test_ollama_gate_alone_does_not_configure_a_keyless_verify(self) -> None:
        """Local gate never masks a missing verify key (is_configured)."""
        settings = Settings(
            llm_provider="openrouter",
            gate_provider="ollama",
            openrouter_api_key="",
            _env_file=None,
        )
        assert settings.provider_configured("ollama")
        assert not settings.is_configured


class TestCloseLlmRuntime:
    class _CountingClient:
        def __init__(self) -> None:
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1

    async def test_shared_client_closed_exactly_once(self) -> None:
        client = self._CountingClient()
        runtime = LLMRuntime(
            settings=make_settings("openrouter"),
            gate_client=client,
            verify_client=client,
        )
        await close_llm_runtime(runtime)
        assert client.close_calls == 1

    async def test_distinct_clients_each_closed_once(self) -> None:
        gate_client = self._CountingClient()
        verify_client = self._CountingClient()
        runtime = LLMRuntime(
            settings=make_settings("openrouter"),
            gate_client=gate_client,
            verify_client=verify_client,
        )
        await close_llm_runtime(runtime)
        assert gate_client.close_calls == 1
        assert verify_client.close_calls == 1


class TestUnknownProviderIsRejected:
    def test_settings_reject_unknown_provider(self) -> None:
        with pytest.raises(ValueError):
            Settings(llm_provider="anthropic", _env_file=None)  # type: ignore[arg-type]

    def test_settings_reject_ollama_verify_provider(self) -> None:
        """LOCAL GATE ONLY: verify_provider excludes ollama at the type level."""
        with pytest.raises(ValueError):
            Settings(verify_provider="ollama", _env_file=None)  # type: ignore[arg-type]
