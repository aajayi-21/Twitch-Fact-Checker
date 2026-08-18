"""Streamer-app routes: events socket, bot control, Twitch setup, pages.

Auth posture: everything here is same-origin localhost (TrustedHost rejects
foreign Hosts), plus two additions the shared app never needed:

- ``/ws/events`` does an EXPLICIT Origin check. CORS middleware does not
  apply to WebSocket handshakes — browsers neither preflight nor same-origin
  enforce them — so without this, any web page could subscribe to the verdict
  stream. Absent/"null" origins are allowed: an OBS browser source loading a
  local URL sends one or the other.
- An optional ``EVENTS_TOKEN`` (``?token=`` — OBS cannot set headers) for
  when the port is ever exposed beyond localhost. It is a locally-generated
  string, NEVER the Twitch token: query strings land in logs and OBS scene
  configs.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.events import EventHub, Subscription
from app.models import Source, Verdict, VerdictFrame
from app.sessions import SessionRegistry

from streamer import STREAMER_VERSION
from streamer.chat.bot import ChannelBot
from streamer.chat.commands import parse_mute_duration
from streamer.config import StreamerSettings
from streamer.topics import topics_payload
from streamer.twitch_auth import (
    DeviceCodeGrant,
    poll_device_token,
    start_device_flow,
    token_hint,
    validate_token,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_STATIC_DIR = Path(__file__).resolve().parent / "static"

# Same origins the HTTP CORS layer trusts (app/main.py) — reused here because
# WebSockets bypass CORS entirely.
_ALLOWED_ORIGIN_RE = re.compile(
    r"^(chrome-extension://[a-z]{32}|https?://(localhost|127\.0\.0\.1)(:\d+)?)$"
)


def _origin_allowed(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    if origin is None or origin == "null":
        return True  # OBS browser sources send one of these
    return _ALLOWED_ORIGIN_RE.match(origin) is not None


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #


@router.get("/overlay")
async def overlay_page() -> FileResponse:
    """The OBS browser-source overlay (single file, no build step)."""
    return FileResponse(_STATIC_DIR / "overlay.html", media_type="text/html")


@router.get("/control")
async def control_page() -> FileResponse:
    """The streamer control panel (single file, no build step)."""
    return FileResponse(_STATIC_DIR / "control.html", media_type="text/html")


@router.get("/meta/topics")
async def meta_topics() -> dict[str, Any]:
    return topics_payload()


# --------------------------------------------------------------------------- #
# /ws/events — read-only fan-out to overlay + control panel
# --------------------------------------------------------------------------- #


@router.websocket("/ws/events")
async def events_ws(websocket: WebSocket) -> None:
    """Pure reader over the hub: no hello, no session, preempts nothing."""
    settings: StreamerSettings = websocket.app.state.settings
    if not _origin_allowed(websocket):
        await websocket.close(code=1008)
        return
    token = settings.events_token.strip()
    if token and websocket.query_params.get("token", "") != token:
        await websocket.close(code=1008)
        return
    await websocket.accept()

    channels_param = websocket.query_params.get("channel", "")
    types_param = websocket.query_params.get(
        # Transcripts are the streamer's raw speech; a browser-source page is
        # a wider audience than an extension tab, so they are opt-in here.
        "types",
        "verdict,status,contradiction,error",
    )
    hub: EventHub = websocket.app.state.events
    subscription: Subscription = hub.subscribe(
        name=f"events-ws:{websocket.client.host if websocket.client else '?'}",
        channels=({channels_param.strip().lower()} if channels_param.strip() else None),
        types={t.strip() for t in types_param.split(",") if t.strip()},
    )
    await websocket.send_json(
        {"type": "events_ready", "server_version": STREAMER_VERSION}
    )

    async def watch_disconnect() -> None:
        # The only reason to read: noticing a dead OBS source promptly.
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return

    async def pump() -> None:
        async for event in subscription:
            await websocket.send_json(
                {
                    "type": "event",
                    "session_id": event.session_id,
                    "platform": event.platform,
                    "channel": event.channel,
                    "claim_id": event.claim_id,
                    "check_worthiness": event.check_worthiness,
                    "claim_age_s": event.claim_age_s,
                    "stream_time_s": event.stream_time_s,
                    "frame": event.frame,
                }
            )

    try:
        async with asyncio.TaskGroup() as group:
            watcher = group.create_task(watch_disconnect())
            pumper = group.create_task(pump())

            def cancel_other(done: asyncio.Task[None]) -> None:
                for task in (watcher, pumper):
                    if task is not done and not task.done():
                        task.cancel()

            watcher.add_done_callback(cancel_other)
            pumper.add_done_callback(cancel_other)
    except* (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except* Exception as group:  # noqa: BLE001 - log and drop the socket
        logger.info("events socket ended: %s", group.exceptions[0])
    finally:
        subscription.close()


class TestEventRequest(BaseModel):
    label: Literal["TRUE", "FALSE", "MISLEADING", "UNVERIFIED"] = "FALSE"
    topic: str = "other"


@router.post("/events/test")
async def send_test_event(body: TestEventRequest, request: Request) -> dict[str, Any]:
    """Synthetic verdict onto the hub, so the overlay can be positioned in
    OBS before ever going live. Never touches chat: the hub subscription the
    bot holds filters by channel, and this event carries a reserved one."""
    verdict = Verdict(
        claim="The Eiffel Tower is 450 metres tall. (test verdict)",
        topic=body.topic if body.topic else "other",  # type: ignore[arg-type]
        label=body.label,
        explanation=(
            "This is a TEST verdict fired from the control panel; it was "
            "never spoken on stream. It is 330 m, for the record."
        ),
        sources=[
            Source(url="https://www.toureiffel.paris/en", title="Official site"),
            Source(url="https://www.britannica.com/topic/Eiffel-Tower", title=None),
        ],
    )
    hub: EventHub = request.app.state.events
    hub.publish(
        VerdictFrame.from_verdict(verdict),
        session_id="test",
        platform=None,
        channel="__test__",  # reserved: never a real Twitch login (see bot)
        claim_id=None,
        check_worthiness=0.99,
        claim_age_s=0.0,
    )
    return {"sent": True, "label": body.label}


# --------------------------------------------------------------------------- #
# Bot status & control (the panel's API)
# --------------------------------------------------------------------------- #


def _bot_of(request: Request) -> ChannelBot | None:
    return getattr(request.app.state, "chat_bot", None)


def _bot_status(request: Request) -> dict[str, Any]:
    settings: StreamerSettings = request.app.state.settings
    registry: SessionRegistry = request.app.state.sessions
    bot = _bot_of(request)
    status: dict[str, Any] = {
        "configured": settings.chat_bot_configured,
        "dry_run": settings.chat_dry_run if bot is None else bot.dry_run,
        "channel": settings.twitch_channel or None,
        "bot_login": settings.twitch_bot_login or None,
        "token_hint": token_hint(settings.twitch_bot_token),
        "token_expires_at": settings.twitch_bot_expires_at or None,
        "client_id_set": bool(settings.twitch_client_id.strip()),
        "live_sessions": [
            {
                "session_id": live.session_id,
                "platform": live.platform,
                "channel": live.channel,
            }
            for live in registry.all()
        ],
        "bot": None,
    }
    if bot is not None:
        now = bot._now()
        status["bot"] = {
            "connected": bot.transport.connected,
            "is_moderator": bot.transport.is_moderator,
            "mode": bot.policy.mode,
            "trusted": bot.trusted,
            "armed": bot._cached_record is not None and bot._cached_record.armed,
            "muted": bot.muted_until is not None and now < bot.muted_until,
            "muted_remaining_s": (
                None
                if bot.muted_until is None or bot.muted_until == float("inf")
                else max(0.0, bot.muted_until - now)
            ),
            "latched": bot.latch.active,
            "disabled": bot.disabled,
            "posts_this_hour": bot.history.posts_in_window(3600.0, now=now),
            "posts_per_hour": bot.policy.posts_per_hour,
            "labels": sorted(bot.policy.labels),
            "queue": [
                {
                    "post_id": pending.post_id,
                    "label": pending.verdict.label,
                    "claim": pending.verdict.claim,
                    "message": pending.message,
                    "age_s": now - pending.created_at,
                }
                for pending in bot.review_queue.values()
            ],
        }
    return status


@router.get("/bot/status")
async def bot_status(request: Request) -> dict[str, Any]:
    return _bot_status(request)


class ModeRequest(BaseModel):
    mode: Literal["auto", "review", "off"]


@router.post("/bot/mode")
async def set_mode(body: ModeRequest, request: Request) -> dict[str, Any]:
    bot = _bot_of(request)
    if bot is None:
        raise HTTPException(status_code=409, detail="chat bot is not configured")
    bot.policy = bot._policy_with(mode=body.mode)
    await bot._db.upsert_chat_channel(
        platform=bot.platform, channel=bot.channel, mode=body.mode
    )
    return _bot_status(request)


class MuteRequest(BaseModel):
    muted: bool
    duration: str | None = None  # "15m", "2h", "rest"


@router.post("/bot/mute")
async def set_mute(body: MuteRequest, request: Request) -> dict[str, Any]:
    bot = _bot_of(request)
    if bot is None:
        raise HTTPException(status_code=409, detail="chat bot is not configured")
    if not body.muted:
        bot.muted_until = None
        await bot._db.upsert_chat_channel(
            platform=bot.platform, channel=bot.channel, muted_until=None
        )
        return _bot_status(request)
    duration = parse_mute_duration((body.duration,) if body.duration else ())
    if duration is None:
        raise HTTPException(status_code=400, detail="bad duration (5m..4h or rest)")
    bot.muted_until = bot._now() + duration
    return _bot_status(request)


class DryRunRequest(BaseModel):
    dry_run: bool


@router.post("/bot/dry-run")
async def set_dry_run(body: DryRunRequest, request: Request) -> dict[str, Any]:
    """The go-live switch. Turning dry-run OFF is the moment the bot may
    actually speak; it stays a deliberate, panel-level act."""
    bot = _bot_of(request)
    if bot is None:
        raise HTTPException(status_code=409, detail="chat bot is not configured")
    bot.dry_run = body.dry_run
    logger.warning(
        "dry run %s via control panel", "ENABLED" if body.dry_run else "DISABLED"
    )
    return _bot_status(request)


class ApproveRequest(BaseModel):
    approved_by: str = "control-panel"


@router.post("/bot/queue/{post_id}/post")
async def approve_queued(
    post_id: str, body: ApproveRequest, request: Request
) -> dict[str, Any]:
    bot = _bot_of(request)
    if bot is None:
        raise HTTPException(status_code=409, detail="chat bot is not configured")
    posted = await bot.approve(post_id, approved_by=body.approved_by)
    if not posted:
        raise HTTPException(status_code=404, detail="unknown or expired queue item")
    return _bot_status(request)


@router.post("/bot/queue/{post_id}/skip")
async def skip_queued(post_id: str, request: Request) -> dict[str, Any]:
    bot = _bot_of(request)
    if bot is None:
        raise HTTPException(status_code=409, detail="chat bot is not configured")
    if not await bot.skip(post_id, skipped_by="control-panel"):
        raise HTTPException(status_code=404, detail="unknown queue item")
    return _bot_status(request)


@router.get("/stats/chat")
async def chat_stats(request: Request) -> dict[str, Any]:
    return await request.app.state.db.fetch_chat_summary()


@router.get("/bot/posts")
async def recent_decisions(
    request: Request, status: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """The panel's transparency feed: every decision row, reason included —
    "why didn't it post that?" answered from data, not logs."""
    settings: StreamerSettings = request.app.state.settings
    channel = settings.twitch_channel.strip().lstrip("#").lower()
    if not channel:
        return []
    return await request.app.state.db.fetch_chat_posts(
        "twitch", channel, status=status, limit=max(1, min(limit, 200))
    )


# --------------------------------------------------------------------------- #
# Twitch credential setup
# --------------------------------------------------------------------------- #


class TwitchPasteRequest(BaseModel):
    token: str
    channel: str | None = None


@router.get("/setup/twitch")
async def twitch_status(request: Request) -> dict[str, Any]:
    settings: StreamerSettings = request.app.state.settings
    return {
        "configured": settings.chat_bot_configured,
        "login": settings.twitch_bot_login or None,
        "token_hint": token_hint(settings.twitch_bot_token),
        "expires_at": settings.twitch_bot_expires_at or None,
        "channel": settings.twitch_channel or None,
        "client_id_set": bool(settings.twitch_client_id.strip()),
        "has_refresh": bool(settings.twitch_bot_refresh.strip()),
    }


@router.post("/setup/twitch")
async def twitch_paste(body: TwitchPasteRequest, request: Request) -> dict[str, Any]:
    """Paste-a-token path: validate live, persist, hot-apply."""
    from app.setup import ProviderKeyRejected, ProviderUnreachable
    from streamer.twitch_auth import persist_credentials

    try:
        info = await validate_token(body.token)
    except ProviderKeyRejected as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except ProviderUnreachable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    persist_credentials(token=body.token, refresh=None, info=info, channel=body.channel)
    _apply_credentials(request, token=body.token, info=info, channel=body.channel)
    return await twitch_status(request)


class DeviceStartResponse(BaseModel):
    user_code: str
    verification_uri: str
    interval_s: int


@router.post("/setup/twitch/device")
async def twitch_device_start(request: Request) -> DeviceStartResponse:
    settings: StreamerSettings = request.app.state.settings
    client_id = settings.twitch_client_id.strip()
    if not client_id:
        raise HTTPException(
            status_code=409,
            detail=(
                "No TWITCH_CLIENT_ID configured. Register a (free, public) "
                "application at dev.twitch.tv/console/apps, or paste a token "
                "instead."
            ),
        )
    from app.setup import ProviderUnreachable

    try:
        grant = await start_device_flow(client_id)
    except ProviderUnreachable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    request.app.state.device_grant = grant
    return DeviceStartResponse(
        user_code=grant.user_code,
        verification_uri=grant.verification_uri,
        interval_s=grant.interval_s,
    )


class DevicePollRequest(BaseModel):
    channel: str | None = None


@router.post("/setup/twitch/device/poll")
async def twitch_device_poll(
    body: DevicePollRequest, request: Request
) -> dict[str, Any]:
    from app.setup import ProviderKeyRejected, ProviderUnreachable
    from streamer.twitch_auth import persist_credentials

    settings: StreamerSettings = request.app.state.settings
    grant: DeviceCodeGrant | None = getattr(request.app.state, "device_grant", None)
    if grant is None:
        raise HTTPException(status_code=409, detail="no device flow in progress")
    try:
        pair = await poll_device_token(
            settings.twitch_client_id.strip(), grant.device_code
        )
    except ProviderKeyRejected as exc:
        request.app.state.device_grant = None
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except ProviderUnreachable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if pair is None:
        return {"pending": True}
    token, refresh = pair
    request.app.state.device_grant = None
    info = await validate_token(token)
    persist_credentials(token=token, refresh=refresh, info=info, channel=body.channel)
    _apply_credentials(
        request, token=token, refresh=refresh, info=info, channel=body.channel
    )
    return {"pending": False, **(await twitch_status(request))}


def _apply_credentials(
    request: Request,
    *,
    token: str,
    info: Any,
    refresh: str | None = None,
    channel: str | None = None,
) -> None:
    """Hot-apply without a restart: update settings in place and (re)start
    the bot. The transport picks the new token up on its next connect."""
    settings: StreamerSettings = request.app.state.settings
    settings.twitch_bot_token = token.removeprefix("oauth:").strip()
    settings.twitch_bot_login = info.login
    settings.twitch_bot_expires_at = info.expires_at_iso
    if refresh is not None:
        settings.twitch_bot_refresh = refresh
    if channel:
        settings.twitch_channel = channel.strip().lstrip("#").lower()
    ensure_bot = getattr(request.app.state, "ensure_chat_bot", None)
    if ensure_bot is not None:
        ensure_bot()
