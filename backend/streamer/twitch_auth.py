"""Twitch credential acquisition and validation for the bot account.

Two acquisition paths, one storage path:

- **Device Code Flow** (primary, needs ``TWITCH_CLIENT_ID``): the streamer
  types a short code at twitch.tv/activate — no secret is ever pasted, and we
  get a refresh token, which is the only real answer to token expiry. Public
  clients refresh WITHOUT a client secret; refresh tokens are one-time-use
  (they rotate) and expire after 30 idle days, so the rotated pair is
  re-persisted on every refresh and a monthly streamer never re-authenticates.
- **Paste a token** (fallback, zero setup): validated live, re-pasted by the
  operator when it expires.

Both end at :func:`persist_credentials` → ``app.setup.upsert_env_values``
(atomic, 0600, every-occurrence rewrite).

Why the validation probe is strict about scopes: a token missing
``chat:edit`` connects fine and then silently cannot post — the single
biggest support ticket this feature could generate, converted here into an
immediate, specific error. And the expiry story is subtle: **Twitch does NOT
drop a live IRC connection when the token expires** — the connection keeps
working until it drops for some other reason, and then the reconnect fails.
Hence :func:`refresh_if_needed` runs T-15 minutes rather than on failure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from app.config import resolve_env_file
from app.setup import ProviderKeyRejected, ProviderUnreachable, upsert_env_values

logger = logging.getLogger(__name__)

VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
DEVICE_URL = "https://id.twitch.tv/oauth2/device"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"

REQUIRED_SCOPES = frozenset({"chat:read", "chat:edit"})
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

# Refresh this long before expiry — a live IRC connection survives an expired
# token, but the NEXT reconnect (a routine network blip hours in) does not.
REFRESH_MARGIN_S = 15 * 60


@dataclass(frozen=True, slots=True)
class TwitchTokenInfo:
    login: str
    user_id: str
    scopes: frozenset[str]
    expires_in_s: int

    @property
    def expires_at_iso(self) -> str:
        deadline = datetime.now(timezone.utc) + timedelta(seconds=self.expires_in_s)
        return deadline.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, slots=True)
class DeviceCodeGrant:
    device_code: str
    user_code: str
    verification_uri: str
    interval_s: int
    expires_in_s: int


def _client(transport: httpx.AsyncBaseTransport | None) -> httpx.AsyncClient:
    # The injectable transport is the same test seam app/setup.py's provider
    # probes use — the suite never touches id.twitch.tv.
    return httpx.AsyncClient(transport=transport, timeout=10.0)


async def validate_token(
    token: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> TwitchTokenInfo:
    """``GET /oauth2/validate`` — free, and strict about what it accepts.

    Raises:
        ProviderKeyRejected: 401 (bad/expired), an app token (no ``login`` —
            app tokens cannot chat), or missing required scopes; the message
            names exactly what is wrong.
        ProviderUnreachable: network failure or an unexpected status.
    """
    bare = token.removeprefix("oauth:").strip()
    if not bare:
        raise ProviderKeyRejected("empty token")
    async with _client(transport) as client:
        try:
            response = await client.get(
                VALIDATE_URL, headers={"Authorization": f"OAuth {bare}"}
            )
        except httpx.HTTPError as exc:
            raise ProviderUnreachable(f"could not reach id.twitch.tv: {exc}") from exc
    if response.status_code == 401:
        raise ProviderKeyRejected("Twitch rejected the token (expired or revoked)")
    if response.status_code != 200:
        raise ProviderUnreachable(
            f"unexpected {response.status_code} from /oauth2/validate"
        )
    payload = response.json()
    login = (payload.get("login") or "").lower()
    if not login:
        raise ProviderKeyRejected(
            "this is an APP access token — app tokens cannot chat. Generate a "
            "USER token for the bot account."
        )
    scopes = frozenset(payload.get("scopes") or [])
    missing = REQUIRED_SCOPES - scopes
    if missing:
        raise ProviderKeyRejected(
            f"token is missing the {', '.join(sorted(missing))} scope(s) — the "
            "bot could not chat. Regenerate it with chat:read AND chat:edit."
        )
    info = TwitchTokenInfo(
        login=login,
        user_id=str(payload.get("user_id", "")),
        scopes=scopes,
        expires_in_s=int(payload.get("expires_in", 0)),
    )
    if 0 < info.expires_in_s < 3600:
        logger.warning(
            "Twitch token for %s expires in %d minutes",
            login,
            info.expires_in_s // 60,
        )
    return info


async def start_device_flow(
    client_id: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> DeviceCodeGrant:
    """Kick off DCF: returns the code the streamer types at the URL."""
    async with _client(transport) as client:
        try:
            response = await client.post(
                DEVICE_URL,
                data={
                    "client_id": client_id,
                    "scopes": " ".join(sorted(REQUIRED_SCOPES)),
                },
            )
        except httpx.HTTPError as exc:
            raise ProviderUnreachable(f"could not reach id.twitch.tv: {exc}") from exc
    if response.status_code != 200:
        raise ProviderUnreachable(
            f"device authorization failed ({response.status_code}): "
            f"{response.text[:200]}"
        )
    payload = response.json()
    return DeviceCodeGrant(
        device_code=payload["device_code"],
        user_code=payload["user_code"],
        verification_uri=payload.get(
            "verification_uri", "https://www.twitch.tv/activate"
        ),
        interval_s=int(payload.get("interval", 5)),
        expires_in_s=int(payload.get("expires_in", 1800)),
    )


async def poll_device_token(
    client_id: str,
    device_code: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[str, str] | None:
    """One poll: ``(access, refresh)`` on success, ``None`` while pending.

    Raises ProviderKeyRejected when the user declined or the code expired.
    """
    async with _client(transport) as client:
        try:
            response = await client.post(
                TOKEN_URL,
                data={
                    "client_id": client_id,
                    "device_code": device_code,
                    "grant_type": DEVICE_GRANT,
                    "scopes": " ".join(sorted(REQUIRED_SCOPES)),
                },
            )
        except httpx.HTTPError as exc:
            raise ProviderUnreachable(f"could not reach id.twitch.tv: {exc}") from exc
    if response.status_code == 200:
        payload = response.json()
        return payload["access_token"], payload.get("refresh_token", "")
    message = ""
    try:
        message = str(response.json().get("message", ""))
    except Exception:
        pass
    if "authorization_pending" in message or "slow_down" in message:
        return None
    raise ProviderKeyRejected(
        f"device authorization failed: {message or response.status_code}"
    )


async def refresh_token(
    client_id: str,
    refresh: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[str, str]:
    """``(new_access, new_refresh)``. The old refresh token is now DEAD
    (one-time use) — the caller must persist the new pair immediately."""
    async with _client(transport) as client:
        try:
            response = await client.post(
                TOKEN_URL,
                data={
                    "client_id": client_id,
                    "refresh_token": refresh,
                    "grant_type": "refresh_token",
                },
            )
        except httpx.HTTPError as exc:
            raise ProviderUnreachable(f"could not reach id.twitch.tv: {exc}") from exc
    if response.status_code != 200:
        raise ProviderKeyRejected(
            f"refresh failed ({response.status_code}) — reconnect Twitch from "
            "the control panel"
        )
    payload = response.json()
    return payload["access_token"], payload.get("refresh_token", refresh)


def persist_credentials(
    *,
    token: str,
    refresh: str | None,
    info: TwitchTokenInfo,
    channel: str | None = None,
    client_id: str | None = None,
) -> None:
    """Write the pair (and cached identity) to ``.env`` — atomic, 0600."""
    updates = {
        "TWITCH_BOT_TOKEN": token.removeprefix("oauth:").strip(),
        "TWITCH_BOT_LOGIN": info.login,
        "TWITCH_BOT_EXPIRES_AT": info.expires_at_iso,
    }
    if refresh is not None:
        updates["TWITCH_BOT_REFRESH"] = refresh
    if channel is not None:
        updates["TWITCH_CHANNEL"] = channel.strip().lstrip("#").lower()
    if client_id is not None:
        updates["TWITCH_CLIENT_ID"] = client_id.strip()
    upsert_env_values(resolve_env_file(), updates)


def token_hint(token: str) -> str | None:
    """The ``…abcd`` display form; never more."""
    bare = token.removeprefix("oauth:").strip()
    if len(bare) < 8:
        return None
    return f"…{bare[-4:]}"
