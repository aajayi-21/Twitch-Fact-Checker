"""Provider factory: build the LLM client + gate/checker pair from Settings.

The rest of the app never mentions a concrete provider. ``app.main``'s
lifespan builds ONE process-wide client for the active provider
(``settings.llm_provider``) and the process-wide gate/checker used by the
debug endpoints; ``app.ws`` builds a FRESH gate/checker per session (so the
transcript buffer and dedupe memory are session-scoped) around that same
shared client.

Provider imports are deliberately lazy: only the active provider's SDK
machinery is constructed.
"""

from typing import Any

from app.claim_gate import ClaimGate
from app.config import Settings
from app.fact_checker import FactChecker
from app.rate_limit import QuotaCooldown


def create_llm_client(settings: Settings) -> Any:
    """One process-wide client for the ACTIVE provider only.

    Returns ``openai.AsyncOpenAI`` (OpenRouter) or ``google.genai.Client``
    (Gemini). Close it on shutdown via :func:`close_llm_client`.
    """
    if settings.llm_provider == "openrouter":
        from app.llm_openrouter import create_openrouter_client

        return create_openrouter_client(settings.openrouter_api_key)
    from google import genai

    return genai.Client(api_key=settings.gemini_api_key)


async def close_llm_client(client: Any) -> None:
    """Close whichever client :func:`create_llm_client` built.

    ``genai.Client`` closes via ``client.aio.aclose()``; ``AsyncOpenAI``
    (which has no ``aio`` attribute) closes via ``client.close()``.
    """
    aio = getattr(client, "aio", None)
    if aio is not None:
        await aio.aclose()
    else:
        await client.close()


def create_claim_gate(settings: Settings, client: Any) -> ClaimGate:
    """A claim gate for the active provider around the shared client."""
    if settings.llm_provider == "openrouter":
        from app.llm_openrouter import OpenRouterClaimGate

        return OpenRouterClaimGate(
            client=client,
            model=settings.openrouter_gate_model,
            gate_interval_s=settings.gate_interval_s,
            gate_timeout_s=settings.gate_timeout_s,
            reasoning_effort=settings.openrouter_reasoning_effort_or_none,
        )
    from app.llm_gemini import GeminiClaimGate

    return GeminiClaimGate(
        client=client,
        model=settings.gemini_gate_model,
        gate_interval_s=settings.gate_interval_s,
        gate_timeout_s=settings.gate_timeout_s,
    )


def create_fact_checker(
    settings: Settings, client: Any, cooldown: QuotaCooldown
) -> FactChecker:
    """A fact checker for the active provider around the shared client."""
    if settings.llm_provider == "openrouter":
        from app.llm_openrouter import OpenRouterFactChecker

        return OpenRouterFactChecker(
            client=client,
            verify_model=settings.openrouter_verify_model,
            cooldown=cooldown,
            web_max_results=settings.openrouter_web_max_results,
            verify_timeout_s=settings.verify_timeout_s,
            reasoning_effort=settings.openrouter_reasoning_effort_or_none,
        )
    from app.llm_gemini import GeminiFactChecker

    return GeminiFactChecker(
        client=client,
        verify_model=settings.gemini_verify_model,
        extraction_model=settings.gemini_gate_model,
        cooldown=cooldown,
        verify_timeout_s=settings.verify_timeout_s,
    )
