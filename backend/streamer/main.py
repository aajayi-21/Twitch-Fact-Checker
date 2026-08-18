"""The STREAMER app: ``uvicorn streamer.main:app`` on port 8711.

A SEPARATE product from the viewer backend (``app.main:app``, port 8710) so
the two can run side by side and be compared — its own settings, its own
database (``streamer.db``), its own pages and bot, sharing the pipeline,
transcriber, LLM stack, and wire protocol by IMPORT rather than fork. It
mounts the shared routers too, so it is fully self-contained: point the
ingest CLI (or even the browser extension) at 8711 and everything works.

The extra moving parts over the viewer lifespan:

- the chat bot (created lazily by ``ensure_chat_bot`` so pasting a token in
  the panel starts it without a restart);
- the token-refresh loop — Twitch does NOT drop a live IRC connection when
  the token expires; the failure arrives at the NEXT reconnect, hours later.
  So refresh runs on a timer (T-15 min), not on failure, and re-persists the
  rotated pair every time (refresh tokens are one-time-use).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.db import DayCounter
from app.debug import router as debug_router
from app.events import EventHub
from app.feedback import router as feedback_router
from app.llm_provider import build_llm_runtime, close_llm_runtime
from app.logging_setup import configure_logging
from app.main import _CORS_ORIGIN_REGEX
from app.rate_limit import QuotaCooldown, TokenBucket
from app.sessions import SessionRegistry
from app.setup import router as setup_router
from app.stats import router as stats_router
from app.transcriber import create_transcriber
from app.ws import router as ws_router

from streamer import STREAMER_VERSION
from streamer.chat.bot import ChannelBot
from streamer.chat.transport import TwitchIRCTransport
from streamer.config import StreamerSettings
from streamer.db import StreamerDatabase
from streamer.routes import router as streamer_router
from streamer.twitch_auth import (
    REFRESH_MARGIN_S,
    TwitchTokenInfo,
    persist_credentials,
    refresh_token,
    validate_token,
)

logger = logging.getLogger(__name__)

TOKEN_CHECK_INTERVAL_S = 600.0  # ten minutes; /oauth2/validate is free


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = StreamerSettings()
    configure_logging(settings.log_level)
    cooldown = QuotaCooldown()

    app.state.settings = settings
    app.state.quota_cooldown = cooldown
    app.state.verify_bucket = TokenBucket(rate_per_min=settings.verify_rpm, burst=2)
    app.state.llm_runtime = build_llm_runtime(settings, cooldown)
    app.state.sessions = SessionRegistry(
        scope=settings.session_preempt_scope, max_sessions=settings.max_sessions
    )
    app.state.events = EventHub(settings.event_queue_maxsize)

    db = StreamerDatabase(settings.db_path)
    await db.open()
    app.state.db = db
    app.state.verify_counter = DayCounter(initial=await db.count_checks_today())

    transcriber = create_transcriber(settings)
    await asyncio.to_thread(transcriber.load)
    app.state.transcriber = transcriber
    stt_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt")
    app.state.stt_executor = stt_executor

    # ---- the chat bot ---------------------------------------------------- #
    app.state.chat_bot = None
    app.state.chat_bot_task = None

    def ensure_chat_bot() -> None:
        """Idempotent lazy start, callable from the setup routes so pasting a
        token brings the bot up without a restart."""
        if app.state.chat_bot is not None:
            transport = app.state.chat_bot.transport
            if isinstance(transport, TwitchIRCTransport):
                transport.update_token(settings.twitch_bot_token_value)
            return
        if not settings.chat_bot_configured:
            return
        transport = TwitchIRCTransport(
            login=settings.twitch_bot_login,
            token=settings.twitch_bot_token_value,
            channel=settings.twitch_channel,
        )
        bot = ChannelBot(
            platform="twitch",
            channel=settings.twitch_channel,
            transport=transport,
            db=db,
            hub=app.state.events,
            registry=app.state.sessions,
            allowlist=settings.chat_allowlist,
            dry_run=settings.chat_dry_run,
        )
        app.state.chat_bot = bot
        app.state.chat_bot_task = asyncio.ensure_future(bot.run())
        logger.info(
            "chat bot started: %s -> #%s (%s)",
            settings.twitch_bot_login,
            settings.twitch_channel,
            "DRY RUN" if bot.dry_run else "LIVE POSTING",
        )

    app.state.ensure_chat_bot = ensure_chat_bot
    ensure_chat_bot()

    refresh_task = asyncio.ensure_future(_token_refresh_loop(app))

    logger.info(
        "streamer backend v%s on http://%s:%d — control panel at /control%s",
        STREAMER_VERSION,
        settings.host,
        settings.port,
        "" if settings.chat_bot_configured else " (Twitch not connected yet)",
    )
    try:
        yield
    finally:
        refresh_task.cancel()
        bot: ChannelBot | None = app.state.chat_bot
        if bot is not None:
            bot.stop()
            task = app.state.chat_bot_task
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        app.state.events.close()
        await asyncio.to_thread(stt_executor.shutdown, wait=True, cancel_futures=True)
        try:
            transcriber.unload()
        except Exception as exc:  # pragma: no cover
            logger.warning("transcriber unload failed: %s", exc)
        try:
            await close_llm_runtime(app.state.llm_runtime)
        except Exception as exc:  # pragma: no cover
            logger.warning("LLM client close failed: %s", exc)
        await db.close()


async def _token_refresh_loop(app: FastAPI) -> None:
    """Refresh ahead of expiry, on a timer — never on failure (see module
    docstring). Persists the rotated pair immediately: the old refresh token
    is dead the moment the new one exists."""
    settings: StreamerSettings = app.state.settings
    while True:
        await asyncio.sleep(TOKEN_CHECK_INTERVAL_S)
        if not (
            settings.twitch_bot_refresh.strip()
            and settings.twitch_client_id.strip()
            and settings.twitch_bot_expires_at
        ):
            continue
        try:
            deadline = datetime.strptime(
                settings.twitch_bot_expires_at, "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
        if remaining > REFRESH_MARGIN_S:
            continue
        try:
            new_token, new_refresh = await refresh_token(
                settings.twitch_client_id.strip(), settings.twitch_bot_refresh.strip()
            )
            info: TwitchTokenInfo = await validate_token(new_token)
            persist_credentials(token=new_token, refresh=new_refresh, info=info)
            settings.twitch_bot_token = new_token
            settings.twitch_bot_refresh = new_refresh
            settings.twitch_bot_expires_at = info.expires_at_iso
            bot: ChannelBot | None = app.state.chat_bot
            if bot is not None and isinstance(bot.transport, TwitchIRCTransport):
                bot.transport.update_token(new_token)
            logger.info("Twitch token refreshed (expires %s)", info.expires_at_iso)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "Twitch token refresh FAILED (%s) — reconnect Twitch from the "
                "control panel before the current token dies",
                exc,
            )


def create_app() -> FastAPI:
    application = FastAPI(
        title="Live Stream Fact-Checker — streamer",
        version=STREAMER_VERSION,
        lifespan=lifespan,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origin_regex=_CORS_ORIGIN_REGEX,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"]
    )
    # The shared surface (ws/audio, LLM setup, stats, feedback, debug) plus
    # the streamer's own routes — a superset of the viewer app.
    application.include_router(debug_router)
    application.include_router(feedback_router)
    application.include_router(setup_router)
    application.include_router(stats_router)
    application.include_router(ws_router)
    application.include_router(streamer_router)

    @application.get("/healthz")
    async def healthz(request: Request) -> dict[str, Any]:
        settings: StreamerSettings = request.app.state.settings
        runtime = request.app.state.llm_runtime
        counter: DayCounter = request.app.state.verify_counter
        bot: ChannelBot | None = request.app.state.chat_bot
        return {
            "status": "ok",
            "product": "streamer",
            "server_version": STREAMER_VERSION,
            "configured": runtime.configured,
            "twitch_configured": settings.chat_bot_configured,
            "chat_connected": bot is not None and bot.transport.connected,
            "dry_run": bot.dry_run if bot is not None else settings.chat_dry_run,
            "checks_today": counter.value,
            "est_cost_today_usd": round(
                counter.value * settings.cost_per_verify_usd, 4
            ),
        }

    return application


app = create_app()
