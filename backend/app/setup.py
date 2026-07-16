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
from app.llm_provider import LLMRuntime, build_llm_runtime, close_llm_client
from app.rate_limit import QuotaCooldown

logger = logging.getLogger(__name__)

router = APIRouter()

PROBE_TIMEOUT_S = 10.0
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/credits"

Provider = Literal["openrouter", "gemini"]

_PROVIDER_ENV_KEYS: dict[str, str] = {
    "openrouter": "OPENROUTER_API_KEY",
    "gemini": "GEMINI_API_KEY",
}
_PROVIDER_SETTINGS_FIELDS: dict[str, str] = {
    "openrouter": "openrouter_api_key",
    "gemini": "gemini_api_key",
}


class ProviderKeyRejected(Exception):
    """The provider answered and explicitly rejected the candidate key."""


class ProviderUnreachable(Exception):
    """The provider could not be reached (or answered unusably)."""


class CreditsInfo(BaseModel):
    """OpenRouter account credits (``GET /api/v1/credits``, a free probe)."""

    total: float
    usage: float


class SetupStatusResponse(BaseModel):
    """Shape shared by GET /setup/status and a successful credentials POST.

    ``key_hint`` is the ONLY key material that ever leaves the backend:
    an ellipsis plus the last four characters. ``credits`` is populated for
    OpenRouter only (Gemini has no free credits probe) and is best-effort.
    """

    configured: bool
    provider: Provider | None
    key_hint: str | None
    gate_model: str | None
    verify_model: str | None
    credits: CreditsInfo | None


class SetupCredentialsRequest(BaseModel):
    """Body for POST /setup/credentials.

    Both fields default to ``""`` so missing keys surface as the contract's
    400 (via the handler's explicit checks) instead of FastAPI's 422.
    """

    provider: str = ""
    api_key: str = ""


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


def _status_payload(
    runtime: LLMRuntime, credits: CreditsInfo | None
) -> SetupStatusResponse:
    """The shared response shape for both endpoints; never leaks the key."""
    if not runtime.configured:
        return SetupStatusResponse(
            configured=False,
            provider=None,
            key_hint=None,
            gate_model=None,
            verify_model=None,
            credits=None,
        )
    settings = runtime.settings
    return SetupStatusResponse(
        configured=True,
        provider=settings.llm_provider,
        key_hint=f"…{settings.active_api_key[-4:]}",
        gate_model=settings.active_gate_model,
        verify_model=settings.active_verify_model,
        credits=credits,
    )


@router.get("/setup/status", response_model=SetupStatusResponse)
async def setup_status(request: Request) -> SetupStatusResponse:
    """Current onboarding state (never returns key material beyond last-4)."""
    runtime: LLMRuntime = request.app.state.llm_runtime
    credits: CreditsInfo | None = None
    if runtime.configured and runtime.settings.llm_provider == "openrouter":
        credits = await fetch_openrouter_credits(runtime.settings.openrouter_api_key)
    return _status_payload(runtime, credits)


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
    if provider not in _PROVIDER_ENV_KEYS:
        raise HTTPException(
            status_code=400,
            detail="provider must be one of: openrouter, gemini",
        )
    api_key = body.api_key.strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key must be non-empty")

    credits: CreditsInfo | None = None
    try:
        if provider == "openrouter":
            await validate_openrouter_key(api_key)
            credits = await fetch_openrouter_credits(api_key)
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
    try:
        new_settings = Settings(
            **{
                "llm_provider": provider,
                _PROVIDER_SETTINGS_FIELDS[provider]: api_key,
            }
        )
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
            detail=f"credentials saved but provider rebuild failed: {exc}",
        ) from exc

    old_runtime: LLMRuntime = request.app.state.llm_runtime
    request.app.state.llm_runtime = new_runtime  # atomic hot-swap
    request.app.state.quota_cooldown = new_cooldown
    logger.info(
        "provider hot-swapped: provider=%s gate=%s verify=%s (env file: %s)",
        new_settings.llm_provider,
        new_settings.active_gate_model,
        new_settings.active_verify_model,
        env_path,
    )
    # Preempt any live session BEFORE closing the old client: its session
    # gate/checker hold that client for the session's lifetime, so leaving
    # the session running would silently break every subsequent gate/verify
    # call. preempt() does not await task-group teardown, so an in-flight
    # check may still touch the old client while it closes — benign, the
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
    if old_runtime.client is not None:
        try:
            await close_llm_client(old_runtime.client)
        except Exception as exc:
            logger.warning("error closing the previous LLM client: %s", exc)

    return _status_payload(new_runtime, credits)
