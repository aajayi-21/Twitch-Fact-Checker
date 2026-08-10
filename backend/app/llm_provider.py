"""Provider registry: build the per-stage LLM clients + gate/checker pair.

The rest of the app never mentions a concrete provider. Each provider is a
:class:`ProviderSpec` (client factory, gate factory, checker factory, close)
in a lazy registry — only the active providers' SDK machinery is ever
imported/constructed. ``app.main``'s lifespan builds ONE process-wide
:class:`LLMRuntime` holding the per-stage clients plus the process-wide
gate/checker used by the debug endpoints; ``app.ws`` builds a FRESH
gate/checker per session (so the transcript buffer and dedupe memory are
session-scoped) around those same shared clients.

Per-stage providers: ``settings.resolved_gate_provider`` and
``settings.resolved_verify_provider`` may differ (e.g. a local Ollama gate
with an OpenRouter verify). When they coincide, ONE client is built and
shared by both stages; :func:`close_llm_runtime` closes each DISTINCT client
exactly once. Ollama is gate-only (``make_checker=None``) — local verify has
no web-search grounding.

The runtime is a single ``app.state.llm_runtime`` slot so that
``POST /setup/credentials`` and ``POST /setup/stages`` can hot-swap the whole
provider stack atomically (one attribute assignment) — sessions and debug
requests read the slot fresh each time. An UNCONFIGURED backend (missing key
for a keyed stage provider) holds a runtime whose clients/gate/checker are
``None``.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.claim_gate import ClaimGate
from app.config import Settings
from app.fact_checker import FactChecker
from app.rate_limit import QuotaCooldown


async def close_llm_client(client: Any) -> None:
    """Close whichever client a provider spec built.

    ``genai.Client`` closes via ``client.aio.aclose()``; ``AsyncOpenAI``
    (OpenRouter and local servers alike — no ``aio`` attribute) closes via
    ``client.close()``.
    """
    aio = getattr(client, "aio", None)
    if aio is not None:
        await aio.aclose()
    else:
        await client.close()


@dataclass(frozen=True)
class ProviderSpec:
    """Everything the app needs to run one provider.

    ``make_checker`` is ``None`` for gate-only providers (Ollama): asking
    them to verify is a configuration error, guarded in
    :func:`create_fact_checker`.
    """

    make_client: Callable[[Settings], Any]
    make_gate: Callable[[Settings, Any], ClaimGate]
    make_checker: Callable[[Settings, Any, QuotaCooldown], FactChecker] | None
    close: Callable[[Any], Awaitable[None]]


def _openrouter_spec() -> ProviderSpec:
    from app.llm_openrouter import (
        OpenRouterClaimGate,
        OpenRouterFactChecker,
        create_openrouter_client,
    )

    return ProviderSpec(
        make_client=lambda s: create_openrouter_client(s.openrouter_api_key),
        make_gate=lambda s, client: OpenRouterClaimGate(
            client=client,
            model=s.openrouter_gate_model,
            gate_interval_s=s.gate_interval_s,
            gate_timeout_s=s.gate_timeout_s,
            reasoning_effort=s.openrouter_reasoning_effort_or_none,
        ),
        make_checker=lambda s, client, cooldown: OpenRouterFactChecker(
            client=client,
            verify_model=s.openrouter_verify_model,
            cooldown=cooldown,
            web_max_results=s.openrouter_web_max_results,
            verify_timeout_s=s.verify_timeout_s,
            reasoning_effort=s.openrouter_reasoning_effort_or_none,
        ),
        close=close_llm_client,
    )


def _gemini_spec() -> ProviderSpec:
    from google import genai

    from app.llm_gemini import GeminiClaimGate, GeminiFactChecker

    return ProviderSpec(
        make_client=lambda s: genai.Client(api_key=s.gemini_api_key),
        make_gate=lambda s, client: GeminiClaimGate(
            client=client,
            model=s.gemini_gate_model,
            gate_interval_s=s.gate_interval_s,
            gate_timeout_s=s.gate_timeout_s,
        ),
        make_checker=lambda s, client, cooldown: GeminiFactChecker(
            client=client,
            verify_model=s.gemini_verify_model,
            extraction_model=s.gemini_gate_model,
            cooldown=cooldown,
            verify_timeout_s=s.verify_timeout_s,
        ),
        close=close_llm_client,
    )


def _ollama_spec() -> ProviderSpec:
    from app.llm_local import LocalClaimGate, create_local_client

    return ProviderSpec(
        make_client=lambda s: create_local_client(s.ollama_base_url),
        make_gate=lambda s, client: LocalClaimGate(
            client=client,
            model=s.ollama_gate_model,
            base_url=s.ollama_base_url,
            gate_interval_s=s.gate_interval_s,
            gate_timeout_s=s.gate_timeout_s,
        ),
        # Gate-only: local verify has no grounding, every verdict would be
        # downgraded to UNVERIFIED. Config rejects VERIFY_PROVIDER=ollama at
        # the type level; this is the belt-and-braces guard.
        make_checker=None,
        close=close_llm_client,
    )


_SPEC_FACTORIES: dict[str, Callable[[], ProviderSpec]] = {
    "openrouter": _openrouter_spec,
    "gemini": _gemini_spec,
    "ollama": _ollama_spec,
}


def get_provider_spec(name: str) -> ProviderSpec:
    """The registry entry for ``name``; ValueError on unknown providers."""
    try:
        return _SPEC_FACTORIES[name]()
    except KeyError as exc:
        raise ValueError(f"unknown LLM provider {name!r}") from exc


def create_llm_client(settings: Settings, provider: str) -> Any:
    """One client for ``provider`` — the single client-construction seam.

    Everything (including :func:`build_llm_runtime`) routes through here, so
    tests can monkeypatch this one function to keep the suite offline.
    """
    return get_provider_spec(provider).make_client(settings)


@dataclass
class LLMRuntime:
    """Everything provider-specific, swapped atomically on setup changes.

    ``settings`` is always present (it also carries the stage-provider
    choices); the clients/``gate``/``checker`` are ``None`` while the
    backend is unconfigured. ``verify_client`` IS ``gate_client`` (same
    object) when both stages resolve to the same provider. ``gate``/
    ``checker`` here are the PROCESS-WIDE pair used by the debug endpoints;
    live sessions build fresh pairs around the shared clients.
    """

    settings: Settings
    gate_client: Any | None = None
    verify_client: Any | None = None
    gate: ClaimGate | None = None
    checker: FactChecker | None = None

    @property
    def configured(self) -> bool:
        """True when a live provider stack is installed."""
        return (
            self.gate_client is not None
            and self.verify_client is not None
            and self.gate is not None
            and self.checker is not None
        )

    @property
    def provider(self) -> str | None:
        """The legacy single-provider name, or ``None`` while unconfigured."""
        return self.settings.llm_provider if self.configured else None


def build_llm_runtime(settings: Settings, cooldown: QuotaCooldown) -> LLMRuntime:
    """An :class:`LLMRuntime` for ``settings`` — unconfigured when keyless.

    When any resolved stage provider lacks its key, NO LLM objects are built
    and the runtime's clients/``gate``/``checker`` stay ``None``.
    """
    if not settings.is_configured:
        return LLMRuntime(settings=settings)
    gate_provider = settings.resolved_gate_provider
    verify_provider = settings.resolved_verify_provider
    gate_client = create_llm_client(settings, gate_provider)
    verify_client = (
        gate_client
        if verify_provider == gate_provider
        else create_llm_client(settings, verify_provider)
    )
    return LLMRuntime(
        settings=settings,
        gate_client=gate_client,
        verify_client=verify_client,
        gate=create_claim_gate(settings, gate_client),
        checker=create_fact_checker(settings, verify_client, cooldown),
    )


async def close_llm_runtime(runtime: LLMRuntime) -> None:
    """Close each DISTINCT client exactly once (identity comparison)."""
    closed: list[Any] = []
    for client in (runtime.gate_client, runtime.verify_client):
        if client is None or any(client is seen for seen in closed):
            continue
        closed.append(client)
        await close_llm_client(client)


def create_claim_gate(settings: Settings, client: Any) -> ClaimGate:
    """A claim gate for the GATE stage's provider around the shared client."""
    return get_provider_spec(settings.resolved_gate_provider).make_gate(
        settings, client
    )


def create_fact_checker(
    settings: Settings, client: Any, cooldown: QuotaCooldown
) -> FactChecker:
    """A fact checker for the VERIFY stage's provider around the shared client."""
    spec = get_provider_spec(settings.resolved_verify_provider)
    if spec.make_checker is None:
        raise ValueError(
            f"provider {settings.resolved_verify_provider!r} cannot verify "
            "(gate-only provider)"
        )
    return spec.make_checker(settings, client, cooldown)
