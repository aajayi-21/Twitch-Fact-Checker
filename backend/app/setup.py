"""Key-only onboarding: GET /setup/status and POST /setup/credentials.

The extension options page drives this flow: the user pastes a provider API
key, the backend validates it LIVE against the provider (zero-cost probes),
persists it into the ``.env`` file resolved by
:func:`app.config.resolve_env_file`, and hot-swaps ``app.state.llm_runtime``
so the very next session/debug request uses the new provider — no restart.
Any LIVE capture session is preempted with a fatal ``credentials_updated``
frame (its gate/checker hold the old client for the session's lifetime and
would silently break once that client closes), and the quota cooldown is
replaced with a fresh instance so the old provider's cooldown reason never
bleeds into the new provider.

Key-material hygiene (the whole point of this module):

- The key is NEVER logged, echoed, or returned. Responses carry at most a
  last-4 ``key_hint`` (``"…abcd"``).
- Validation probes cost $0: OpenRouter ``GET /api/v1/key`` (plus the free
  ``GET /api/v1/credits`` for the ``credits`` field); Gemini a models-list
  call via google-genai with the candidate key.
- Nothing is persisted unless the provider accepted the key.
- The ``.env`` upsert touches ONLY ``LLM_PROVIDER`` and the submitted
  provider's key line; every other line (comments, ordering, the other
  provider's key) is preserved byte-for-byte, and the write is atomic
  (temp file + ``os.replace``).
"""

import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import ws
from app.config import Settings, resolve_env_file
from app.llm_openrouter import reset_openrouter_capability_latches
from app.llm_provider import LLMRuntime, build_llm_runtime, close_llm_runtime
from app.rate_limit import QuotaCooldown

logger = logging.getLogger(__name__)

router = APIRouter()

PROBE_TIMEOUT_S = 10.0
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/credits"
# Public, keyless, free: the catalogue used to validate model slugs.
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

Provider = Literal["openrouter", "gemini", "ollama"]

# Stage routing: which providers each pipeline stage accepts. Ollama is
# GATE-ONLY — local verify has no web-search grounding, so every verdict
# would be downgraded to UNVERIFIED.
GATE_PROVIDERS: tuple[str, ...] = ("openrouter", "gemini", "ollama")
VERIFY_PROVIDERS: tuple[str, ...] = ("openrouter", "gemini")

# Keyed providers only — Ollama has no key and no settings field to persist.
_PROVIDER_ENV_KEYS: dict[str, str] = {
    "openrouter": "OPENROUTER_API_KEY",
    "gemini": "GEMINI_API_KEY",
}
_PROVIDER_SETTINGS_FIELDS: dict[str, str] = {
    "openrouter": "openrouter_api_key",
    "gemini": "gemini_api_key",
}
_KNOWN_PROVIDERS: frozenset[str] = frozenset({"openrouter", "gemini", "ollama"})


class ProviderKeyRejected(Exception):
    """The provider answered and explicitly rejected the candidate key."""


class ProviderUnreachable(Exception):
    """The provider could not be reached (or answered unusably)."""


class CreditsInfo(BaseModel):
    """OpenRouter account credits (``GET /api/v1/credits``, a free probe)."""

    total: float
    usage: float


class StageStatus(BaseModel):
    """One pipeline stage's resolved provider + model + usability."""

    provider: str | None
    model: str | None
    configured: bool


class OpenRouterStatus(BaseModel):
    """``key_hint`` is the ONLY key material that ever leaves the backend:
    an ellipsis plus the last four characters. ``credits`` is best-effort.

    ``gate_model``/``verify_model`` are the stored OpenRouter slugs, reported
    even when OpenRouter is not the active provider for that stage.
    ``StageStatus.model`` shows only the ACTIVE provider's model, so without
    these the options page could never prefill (or dirty-check) a slug for a
    stage currently routed to Ollama or Gemini.
    """

    configured: bool
    key_hint: str | None
    credits: CreditsInfo | None
    gate_model: str
    verify_model: str


class GeminiStatus(BaseModel):
    configured: bool
    key_hint: str | None


class OllamaStatus(BaseModel):
    """Keyless local provider: ``configured`` is always True; ``reachable``
    is a live best-effort probe of the OpenAI-compatible endpoint."""

    configured: bool
    reachable: bool
    base_url: str


class ProvidersStatus(BaseModel):
    openrouter: OpenRouterStatus
    gemini: GeminiStatus
    ollama: OllamaStatus


class SetupStatusResponse(BaseModel):
    """Shape shared by GET /setup/status and successful setup POSTs."""

    configured: bool
    gate: StageStatus
    verify: StageStatus
    providers: ProvidersStatus


class SetupCredentialsRequest(BaseModel):
    """Body for POST /setup/credentials.

    Both fields default to ``""`` so missing keys surface as the contract's
    400 (via the handler's explicit checks) instead of FastAPI's 422.
    ``api_key`` stays ``""`` for ``provider="ollama"`` (keyless
    test-connection).
    """

    provider: str = ""
    api_key: str = ""


class SetupStagesRequest(BaseModel):
    """Body for POST /setup/stages — per-stage provider routing and models.

    ``gate_model``/``verify_model`` are OpenRouter slugs (e.g.
    ``"openai/gpt-oss-120b"``). Empty means "leave the stored slug alone",
    which keeps this endpoint backward-compatible with clients that send only
    the two provider fields.
    """

    gate_provider: str = ""
    verify_provider: str = ""
    gate_model: str = ""
    verify_model: str = ""


# --------------------------------------------------------------------------- #
# Zero-cost validation probes
# --------------------------------------------------------------------------- #


def _provider_error_message(payload: Any, fallback: str) -> str:
    """Best-effort human-readable message from a provider error body."""
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if payload.get("message"):
            return str(payload["message"])
    return fallback


async def validate_openrouter_key(api_key: str) -> None:
    """Probe ``GET /api/v1/key`` with the candidate key (free, $0).

    Raises:
        ProviderKeyRejected: on a 401/403 (bad/disabled key).
        ProviderUnreachable: on network failure, timeout, or an unexpected
            status.
    """
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as http_client:
            response = await http_client.get(
                OPENROUTER_KEY_URL,
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.HTTPError as exc:
        raise ProviderUnreachable(f"could not reach OpenRouter: {exc}") from exc
    if response.status_code == 200:
        return
    try:
        payload: Any = response.json()
    except ValueError:
        payload = None
    message = _provider_error_message(
        payload, f"OpenRouter rejected the key (HTTP {response.status_code})"
    )
    if response.status_code in (401, 403):
        raise ProviderKeyRejected(message)
    raise ProviderUnreachable(
        f"unexpected OpenRouter response (HTTP {response.status_code}): {message}"
    )


async def fetch_openrouter_credits(api_key: str) -> CreditsInfo | None:
    """Best-effort ``GET /api/v1/credits`` (free); ``None`` on any failure."""
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as http_client:
            response = await http_client.get(
                OPENROUTER_CREDITS_URL,
                headers={"Authorization": f"Bearer {api_key}"},
            )
        response.raise_for_status()
        data = response.json().get("data") or {}
        return CreditsInfo(
            total=float(data["total_credits"]), usage=float(data["total_usage"])
        )
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        logger.warning("could not fetch OpenRouter credits: %s", exc)
        return None


async def validate_gemini_key(api_key: str) -> None:
    """Probe the Gemini API with a models-list call (free, $0).

    Raises:
        ProviderKeyRejected: when Gemini answers with a 4xx (invalid key,
            permission denied) other than 429.
        ProviderUnreachable: on timeout, 429, 5xx, or network failure.
    """
    from google import genai
    from google.genai import errors as genai_errors

    client = genai.Client(api_key=api_key)
    try:
        await asyncio.wait_for(
            client.aio.models.list(config={"page_size": 1}),
            timeout=PROBE_TIMEOUT_S,
        )
    except genai_errors.APIError as exc:
        message = exc.message or f"Gemini API error (HTTP {exc.code})"
        if 400 <= exc.code < 500 and exc.code != 429:
            raise ProviderKeyRejected(message) from exc
        raise ProviderUnreachable(
            f"Gemini API error (HTTP {exc.code}): {message}"
        ) from exc
    except TimeoutError as exc:
        raise ProviderUnreachable(
            f"Gemini models-list probe timed out after {PROBE_TIMEOUT_S:.0f}s"
        ) from exc
    except Exception as exc:
        raise ProviderUnreachable(f"could not reach Gemini: {exc}") from exc
    finally:
        try:
            await client.aio.aclose()
        except Exception as exc:
            logger.debug("error closing Gemini probe client: %s", exc)


class ModelSlugRejected(Exception):
    """OpenRouter answered, and does not publish this model slug."""


async def fetch_openrouter_model_slugs(
    transport: httpx.AsyncBaseTransport | None = None,
) -> set[str]:
    """Every model slug OpenRouter currently publishes.

    ``GET /api/v1/models`` is public, free, and needs no key. Fetching the
    live list (rather than hardcoding one) means a model works the day it
    launches and a retired one is caught immediately.

    ``transport`` is a test seam for ``httpx.MockTransport`` (same pattern as
    :class:`app.embeddings.OllamaEmbedder`), so the suite never patches httpx
    globally.

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
        slugs = {
            str(entry["id"]) for entry in payload["data"] if entry.get("id")
        }
    except (ValueError, KeyError, TypeError) as exc:
        raise ProviderUnreachable(
            f"malformed OpenRouter model list: {exc}"
        ) from exc
    if not slugs:
        raise ProviderUnreachable("OpenRouter model list came back empty")
    return slugs


async def validate_openrouter_model(slug: str, known_slugs: set[str]) -> None:
    """Check one slug against the live catalogue.

    OpenRouter serves the same model under a paid and a ``:free`` id, and
    those are distinct slugs — so an exact match is the right test, with the
    near-miss surfaced to make the ``:free`` suffix mistake obvious.

    Raises:
        ModelSlugRejected: when the slug is not published.
    """
    if slug in known_slugs:
        return
    alternatives = sorted(
        candidate
        for candidate in known_slugs
        if candidate.split(":", 1)[0] == slug.split(":", 1)[0]
    )
    hint = f" Did you mean: {', '.join(alternatives[:3])}?" if alternatives else ""
    raise ModelSlugRejected(f"OpenRouter has no model {slug!r}.{hint}")


async def probe_ollama(base_url: str) -> None:
    """Probe the local OpenAI-compatible server (``GET {base_url}/models``).

    Free and keyless. Also catches a misconfigured ``OLLAMA_BASE_URL`` (e.g.
    one missing the ``/v1`` suffix) at setup time.

    Raises:
        ProviderUnreachable: on network failure, timeout, or non-200.
    """
    url = f"{base_url.rstrip('/')}/models"
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as http_client:
            response = await http_client.get(url)
    except httpx.HTTPError as exc:
        raise ProviderUnreachable(
            f"could not reach the local LLM server at {base_url} — "
            f"is `ollama serve` running? ({exc})"
        ) from exc
    if response.status_code != 200:
        raise ProviderUnreachable(
            f"unexpected response from the local LLM server at {url} "
            f"(HTTP {response.status_code})"
        )


# --------------------------------------------------------------------------- #
# .env persistence
# --------------------------------------------------------------------------- #

_LINE_ENDING_RE = re.compile(r"(\r\n|\r|\n)$")


def upsert_env_values(env_path: Path, updates: dict[str, str]) -> None:
    """Replace or append ``KEY=value`` lines in ``env_path``.

    Every line NOT keyed by ``updates`` — comments, blank lines, the other
    provider's key, ordering — is preserved byte-for-byte (including its
    original line ending). Matching lines keep their own line ending; every
    occurrence of a key is rewritten (python-dotenv honors the LAST one, so
    a stale duplicate must not survive). Missing keys are appended. The file
    is created if absent (parents included) and the write is atomic: temp
    file in the same directory + ``os.replace``. The resulting file mode is
    ALWAYS 0600 — this file carries the API key, so a pre-existing permissive
    mode (e.g. a 0644 ``.env`` copied from ``.env.example``) is deliberately
    tightened rather than preserved.

    Raises:
        ValueError: if any value contains a line break (would inject lines).
        OSError: on filesystem failure.
    """
    for key, value in updates.items():
        if "\n" in value or "\r" in value:
            raise ValueError(f"refusing to write multi-line value for {key}")

    if env_path.exists():
        with env_path.open("r", encoding="utf-8", newline="") as handle:
            original = handle.read()
    else:
        original = ""

    key_patterns = {key: re.compile(rf"^\s*{re.escape(key)}\s*=") for key in updates}
    new_lines: list[str] = []
    seen: set[str] = set()
    for line in original.splitlines(keepends=True):
        ending_match = _LINE_ENDING_RE.search(line)
        ending = ending_match.group(0) if ending_match else ""
        content = line[: len(line) - len(ending)]
        replaced = False
        for key, pattern in key_patterns.items():
            if pattern.match(content):
                new_lines.append(f"{key}={updates[key]}{ending}")
                seen.add(key)
                replaced = True
                break
        if not replaced:
            new_lines.append(line)

    missing = [key for key in updates if key not in seen]
    if missing:
        if new_lines and not new_lines[-1].endswith(("\n", "\r")):
            new_lines[-1] += "\n"
        for key in missing:
            new_lines.append(f"{key}={updates[key]}\n")

    _atomic_write(env_path, "".join(new_lines))


def _atomic_write(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically (temp file + rename).

    The result is always mode 0600: ``tempfile.mkstemp`` creates the temp
    file 0600 and ``os.replace`` carries that mode onto the target. This
    writer only ever touches the secret-bearing ``.env``, so the target's
    previous (possibly world-readable) mode is intentionally NOT preserved.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        os.replace(tmp_name, path)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


def _key_hint(settings: Settings, provider: str) -> str | None:
    """Last-4 hint for a keyed provider, None when unconfigured/keyless."""
    if not settings.provider_configured(provider):
        return None
    key = getattr(settings, _PROVIDER_SETTINGS_FIELDS[provider], "").strip()
    return f"…{key[-4:]}" if key else None


def _stage_status(settings: Settings, provider: str, model: str) -> StageStatus:
    return StageStatus(
        provider=provider,
        model=model,
        configured=settings.provider_configured(provider),
    )


async def _build_status(settings: Settings) -> SetupStatusResponse:
    """The shared response shape for every setup endpoint.

    Never leaks key material beyond last-4 hints. The OpenRouter credits
    fetch and the Ollama reachability probe are both best-effort — a dead
    local port answers instantly, so the probe is cheap in practice.
    """
    credits: CreditsInfo | None = None
    # Fetch credits only when OpenRouter actually serves a stage — a stored
    # key alone must not trigger an internet round-trip on every status poll.
    openrouter_active = "openrouter" in {
        settings.resolved_gate_provider,
        settings.resolved_verify_provider,
    }
    if openrouter_active and settings.provider_configured("openrouter"):
        credits = await fetch_openrouter_credits(settings.openrouter_api_key)
    try:
        await probe_ollama(settings.ollama_base_url)
        ollama_reachable = True
    except ProviderUnreachable:
        ollama_reachable = False
    return SetupStatusResponse(
        configured=settings.is_configured,
        gate=_stage_status(
            settings, settings.resolved_gate_provider, settings.active_gate_model
        ),
        verify=_stage_status(
            settings, settings.resolved_verify_provider, settings.active_verify_model
        ),
        providers=ProvidersStatus(
            openrouter=OpenRouterStatus(
                configured=settings.provider_configured("openrouter"),
                key_hint=_key_hint(settings, "openrouter"),
                credits=credits,
                gate_model=settings.openrouter_gate_model,
                verify_model=settings.openrouter_verify_model,
            ),
            gemini=GeminiStatus(
                configured=settings.provider_configured("gemini"),
                key_hint=_key_hint(settings, "gemini"),
            ),
            ollama=OllamaStatus(
                configured=True,
                reachable=ollama_reachable,
                base_url=settings.ollama_base_url,
            ),
        ),
    )


async def _swap_runtime(request: Request, new_settings: Settings) -> None:
    """Build + atomically install a runtime for ``new_settings``.

    Shared hot-swap tail for credentials and stage changes: fresh cooldown,
    atomic ``app.state`` swap, live-session preemption, and closing each of
    the OLD runtime's distinct clients exactly once.

    Raises:
        HTTPException: 500 when the new runtime cannot be built (nothing is
            swapped in that case).
    """
    try:
        # A FRESH cooldown, installed together with the runtime below: the
        # old provider's cooldown (and its "top up at ..." reason) must not
        # bleed into the new provider. Any surviving old-provider session
        # keeps tripping its own, now-orphaned instance instead.
        new_cooldown = QuotaCooldown()
        new_runtime = build_llm_runtime(new_settings, new_cooldown)
    except Exception as exc:
        logger.exception("failed to build the new provider runtime")
        raise HTTPException(
            status_code=500,
            detail=f"settings saved but provider rebuild failed: {exc}",
        ) from exc

    old_runtime: LLMRuntime = request.app.state.llm_runtime
    request.app.state.llm_runtime = new_runtime  # atomic hot-swap
    request.app.state.quota_cooldown = new_cooldown
    logger.info(
        "provider stack hot-swapped: gate=%s/%s verify=%s/%s",
        new_settings.resolved_gate_provider,
        new_settings.active_gate_model,
        new_settings.resolved_verify_provider,
        new_settings.active_verify_model,
    )
    # Preempt any live session BEFORE closing the old clients: its session
    # gate/checker hold those clients for the session's lifetime, so leaving
    # the session running would silently break every subsequent gate/verify
    # call. preempt() does not await task-group teardown, so an in-flight
    # check may still touch an old client while it closes — benign, the
    # dying session's per-item error handling absorbs it.
    live_pipeline = ws.current_pipeline
    if live_pipeline is not None:
        await live_pipeline.preempt(
            code="credentials_updated",
            message=(
                "API credentials were updated; this session was closed so "
                "the new provider takes effect. Start capture again."
            ),
        )
    try:
        await close_llm_runtime(old_runtime)
    except Exception as exc:
        logger.warning("error closing the previous LLM clients: %s", exc)


@router.get("/setup/status", response_model=SetupStatusResponse)
async def setup_status(request: Request) -> SetupStatusResponse:
    """Current onboarding state (never returns key material beyond last-4)."""
    runtime: LLMRuntime = request.app.state.llm_runtime
    return await _build_status(runtime.settings)


@router.post("/setup/credentials", response_model=SetupStatusResponse)
async def submit_credentials(
    body: SetupCredentialsRequest, request: Request
) -> SetupStatusResponse:
    """Validate a candidate key live, persist it, and hot-swap the provider.

    Responses: 400 bad provider/empty key, 401 provider rejected the key,
    502 provider unreachable, 500 with a descriptive detail on local
    persistence/rebuild failure — never a bare 500. On success the runtime
    swap is atomic (one ``app.state`` assignment) and the response mirrors
    GET /setup/status.
    """
    provider = body.provider.strip().lower()
    if provider not in _KNOWN_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail="provider must be one of: openrouter, gemini, ollama",
        )

    runtime: LLMRuntime = request.app.state.llm_runtime

    if provider == "ollama":
        # Keyless test-connection: probe, persist nothing, swap nothing.
        try:
            await probe_ollama(runtime.settings.ollama_base_url)
        except ProviderUnreachable as exc:
            logger.warning("ollama probe failed: %s", exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return await _build_status(runtime.settings)

    api_key = body.api_key.strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key must be non-empty")

    try:
        if provider == "openrouter":
            await validate_openrouter_key(api_key)
        else:
            await validate_gemini_key(api_key)
    except ProviderKeyRejected as exc:
        logger.warning("%s rejected the submitted API key: %s", provider, exc)
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except ProviderUnreachable as exc:
        logger.warning("%s validation probe failed: %s", provider, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    env_path = resolve_env_file()
    try:
        upsert_env_values(
            env_path,
            {"LLM_PROVIDER": provider, _PROVIDER_ENV_KEYS[provider]: api_key},
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=500,
            detail=f"could not persist credentials to {env_path}: {exc}",
        ) from exc

    # Rebuild Settings from the just-written file; the validated pair is
    # passed explicitly so a stale LLM_PROVIDER/key in the process
    # environment can never override what the user just validated.
    new_settings = Settings(
        **{
            "llm_provider": provider,
            _PROVIDER_SETTINGS_FIELDS[provider]: api_key,
        }
    )
    await _swap_runtime(request, new_settings)
    return await _build_status(new_settings)


@router.post("/setup/stages", response_model=SetupStatusResponse)
async def submit_stages(
    body: SetupStagesRequest, request: Request
) -> SetupStatusResponse:
    """Route each pipeline stage to a provider and model; persist + hot-swap.

    ``gate_model``/``verify_model`` are OpenRouter slugs, validated against
    the live catalogue; empty leaves the stored slug untouched.

    Responses: 400 unknown provider (or ollama for verify — local verify is
    not supported) or an unknown model slug, 409 when a chosen provider is
    not configured (missing key, or Ollama unreachable), 502 when
    OpenRouter's catalogue is unreachable, 500 with a descriptive detail on
    persistence/rebuild failure. On success the runtime swap is atomic and
    the response mirrors GET /setup/status.
    """
    gate_provider = body.gate_provider.strip().lower()
    verify_provider = body.verify_provider.strip().lower()
    if gate_provider not in GATE_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail="gate_provider must be one of: openrouter, gemini, ollama",
        )
    if verify_provider not in VERIFY_PROVIDERS:
        if verify_provider == "ollama":
            raise HTTPException(
                status_code=400,
                detail=(
                    "local verify is not supported; verify_provider must be "
                    "openrouter or gemini"
                ),
            )
        raise HTTPException(
            status_code=400,
            detail="verify_provider must be one of: openrouter, gemini",
        )

    runtime: LLMRuntime = request.app.state.llm_runtime
    current = runtime.settings
    for stage, provider in (("gate", gate_provider), ("verify", verify_provider)):
        if provider == "ollama":
            try:
                await probe_ollama(current.ollama_base_url)
            except ProviderUnreachable as exc:
                raise HTTPException(
                    status_code=409,
                    detail=f"{stage} provider 'ollama' is not reachable: {exc}",
                ) from exc
        elif not current.provider_configured(provider):
            raise HTTPException(
                status_code=409,
                detail=f"{stage} provider {provider!r} has no API key configured",
            )

    # Model slugs: empty means "keep the stored one". Both are validated in
    # a single catalogue fetch so a two-model change costs one request.
    gate_model = body.gate_model.strip()
    verify_model = body.verify_model.strip()
    requested_models = {
        "gate_model": gate_model or current.openrouter_gate_model,
        "verify_model": verify_model or current.openrouter_verify_model,
    }
    if gate_model or verify_model:
        try:
            known_slugs = await fetch_openrouter_model_slugs()
        except ProviderUnreachable as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        for field, slug in requested_models.items():
            if not slug:
                raise HTTPException(
                    status_code=400, detail=f"{field} must not be empty"
                )
            try:
                await validate_openrouter_model(slug, known_slugs)
            except ModelSlugRejected as exc:
                raise HTTPException(
                    status_code=400, detail=f"{field}: {exc}"
                ) from exc

    env_path = resolve_env_file()
    try:
        upsert_env_values(
            env_path,
            {
                "GATE_PROVIDER": gate_provider,
                "VERIFY_PROVIDER": verify_provider,
                "OPENROUTER_GATE_MODEL": requested_models["gate_model"],
                "OPENROUTER_VERIFY_MODEL": requested_models["verify_model"],
            },
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=500,
            detail=f"could not persist stage routing to {env_path}: {exc}",
        ) from exc

    # Explicit kwargs beat any stale GATE_PROVIDER/VERIFY_PROVIDER in the
    # process environment (mirrors the credentials rebuild).
    new_settings = Settings(
        gate_provider=gate_provider,
        verify_provider=verify_provider,
        openrouter_gate_model=requested_models["gate_model"],
        openrouter_verify_model=requested_models["verify_model"],
    )
    models_changed = (
        requested_models["gate_model"] != current.openrouter_gate_model
        or requested_models["verify_model"] != current.openrouter_verify_model
    )
    if models_changed:
        reset_openrouter_capability_latches()
    await _swap_runtime(request, new_settings)
    return await _build_status(new_settings)
