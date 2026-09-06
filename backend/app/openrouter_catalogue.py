"""OpenRouter's public model catalogue: slugs + per-model parameter support.

``GET https://openrouter.ai/api/v1/models`` is public, free, and needs no
key. Besides the slug list (used to validate model names on Apply), every
entry carries ``supported_parameters`` — the UNION over that model's
endpoints of what they accept (``temperature``, ``reasoning``,
``response_format``, ``structured_outputs``, …) — and a ``pricing`` block
whose ``web_search`` key marks models with a native web-search engine.

Why this matters: the transport sends every strict request with
``provider.require_parameters: true`` so OpenRouter routes only to endpoints
that honour the whole request. A parameter no endpoint supports therefore
fails the call outright (404/400) instead of being ignored. That is exactly
what happened in production: ``openai/gpt-5.6-luna`` lists no
``temperature`` anywhere, the gate and verifier always sent
``temperature=0.0``, and every strict call was rejected — the gate latched
its ``json_object`` fallback for the whole process and every verification
walked the 2–3-call text fallback chain. Looking the capabilities up first
and building requests from them removes the guesswork; the latches stay as
the safety net for the offline case.

Data flow:

- boot (``app.main``): prime the cache for the active OpenRouter slugs;
- ``POST /setup/stages``: one catalogue fetch validates the slugs AND primes
  the cache for the new models (``reset_openrouter_capability_latches`` runs
  first);
- ``POST /setup/credentials`` (OpenRouter): best-effort prime;
- offline / unreachable: the cache stays empty, :data:`UNKNOWN_CAPABILITIES`
  says "assume everything", requests look exactly like before, one warning.

The cache is keyed by slug and process-wide, like the latches, because
parameter support is a property of the model, not of a session.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 10.0
# Public, keyless, free: the catalogue used to validate model slugs.
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

#: The parameters this app sends and therefore cares about.
TRACKED_PARAMETERS: tuple[str, ...] = (
    "temperature",
    "reasoning",
    "response_format",
    "structured_outputs",
)


class ProviderUnreachable(Exception):
    """The provider could not be reached (or answered unusably)."""


@dataclass(frozen=True)
class ModelCapabilities:
    """What one model's endpoints accept, per the catalogue.

    ``supported_parameters`` is ``None`` when the catalogue could not be
    consulted — every parameter is then assumed supported and the transport's
    latches decide at runtime, exactly as before the lookup existed.
    """

    supported_parameters: frozenset[str] | None
    native_web_search: bool = False

    @property
    def known(self) -> bool:
        return self.supported_parameters is not None

    def supports(self, parameter: str) -> bool:
        if self.supported_parameters is None:
            return True
        return parameter in self.supported_parameters

    def disabled(self) -> list[str]:
        """The tracked parameters this model does NOT support (none if unknown)."""
        return [name for name in TRACKED_PARAMETERS if not self.supports(name)]

    def as_dict(self) -> dict[str, Any]:
        """The ``/healthz`` shape."""
        return {
            **{name: self.supports(name) for name in TRACKED_PARAMETERS},
            "native_web_search": self.native_web_search,
            "source": "catalogue" if self.known else "assumed",
        }


UNKNOWN_CAPABILITIES = ModelCapabilities(supported_parameters=None)


@dataclass(frozen=True)
class OpenRouterCatalogue:
    """One fetched catalogue: ``{slug: capabilities}``."""

    models: dict[str, ModelCapabilities]

    @property
    def slugs(self) -> set[str]:
        return set(self.models)

    def capabilities(self, slug: str) -> ModelCapabilities:
        """A slug's capabilities, or UNKNOWN when the catalogue lacks it."""
        return self.models.get(slug, UNKNOWN_CAPABILITIES)


def parse_catalogue(payload: Any) -> OpenRouterCatalogue:
    """Parse the ``GET /api/v1/models`` body.

    Raises:
        ProviderUnreachable: on a malformed or empty payload (the caller's
            "OpenRouter answered unusably" contract).
    """
    try:
        models: dict[str, ModelCapabilities] = {}
        for entry in payload["data"]:
            slug = entry.get("id")
            if not slug:
                continue
            parameters = entry.get("supported_parameters")
            pricing = entry.get("pricing") or {}
            models[str(slug)] = ModelCapabilities(
                supported_parameters=(
                    frozenset(str(name) for name in parameters)
                    if isinstance(parameters, list)
                    else None
                ),
                native_web_search=isinstance(pricing, dict)
                and pricing.get("web_search") not in (None, "", "0"),
            )
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ProviderUnreachable(f"malformed OpenRouter model list: {exc}") from exc
    if not models:
        raise ProviderUnreachable("OpenRouter model list came back empty")
    return OpenRouterCatalogue(models=models)


async def fetch_openrouter_catalogue(
    transport: httpx.AsyncBaseTransport | None = None,
) -> OpenRouterCatalogue:
    """Fetch and parse the live catalogue.

    Fetching the live list (rather than hardcoding one) means a model works
    the day it launches and a retired one is caught immediately.
    ``transport`` is a test seam for ``httpx.MockTransport``.

    Raises:
        ProviderUnreachable: on network failure, timeout, or a bad payload.
    """
    try:
        async with httpx.AsyncClient(
            timeout=PROBE_TIMEOUT_S, transport=transport
        ) as http_client:
            response = await http_client.get(OPENROUTER_MODELS_URL)
    except httpx.HTTPError as exc:
        raise ProviderUnreachable(
            f"could not reach OpenRouter's model list: {exc}"
        ) from exc
    if response.status_code != 200:
        raise ProviderUnreachable(
            f"OpenRouter model list returned HTTP {response.status_code}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderUnreachable(f"malformed OpenRouter model list: {exc}") from exc
    return parse_catalogue(payload)


# --------------------------------------------------------------------------- #
# Process-wide capability cache
# --------------------------------------------------------------------------- #

_CAPABILITIES: dict[str, ModelCapabilities] = {}


def lookup_model_capabilities(slug: str) -> ModelCapabilities:
    """The cached capabilities for ``slug`` (UNKNOWN when never primed)."""
    return _CAPABILITIES.get(slug, UNKNOWN_CAPABILITIES)


def set_model_capabilities(slug: str, capabilities: ModelCapabilities) -> None:
    _CAPABILITIES[slug] = capabilities


def clear_model_capabilities() -> None:
    """Forget every cached entry (tests; never needed at runtime — the cache
    is keyed by slug, so a model change simply primes new keys)."""
    _CAPABILITIES.clear()


async def prime_openrouter_capabilities(
    models: Iterable[str],
    *,
    catalogue: OpenRouterCatalogue | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Cache the capabilities of ``models``, logging one line per model.

    Best-effort by design: when the catalogue cannot be fetched the cache is
    left untouched (so the transport assumes full support and its latches
    take over) and ONE warning explains it. Never raises.
    """
    wanted = sorted({slug for slug in models if slug})
    if not wanted:
        return
    if catalogue is None:
        try:
            catalogue = await fetch_openrouter_catalogue(transport)
        except ProviderUnreachable as exc:
            logger.warning(
                "OpenRouter catalogue unreachable (%s); assuming every parameter "
                "is supported for %s — the strict-mode latches remain the "
                "safety net",
                exc,
                ", ".join(wanted),
            )
            return
    for slug in wanted:
        capabilities = catalogue.capabilities(slug)
        set_model_capabilities(slug, capabilities)
        if not capabilities.known:
            logger.warning(
                "OpenRouter model %s is not in the catalogue; assuming every "
                "parameter is supported",
                slug,
            )
            continue
        disabled = capabilities.disabled()
        supported = [name for name in TRACKED_PARAMETERS if name not in disabled]
        if disabled:
            logger.warning(
                "OpenRouter model %s: omitting %s (not in supported_parameters); "
                "%s supported%s",
                slug,
                ", ".join(disabled),
                ", ".join(supported) or "nothing else",
                (
                    "; native web search available"
                    if capabilities.native_web_search
                    else ""
                ),
            )
        else:
            logger.info(
                "OpenRouter model %s: %s supported%s",
                slug,
                ", ".join(supported),
                (
                    "; native web search available"
                    if capabilities.native_web_search
                    else ""
                ),
            )
