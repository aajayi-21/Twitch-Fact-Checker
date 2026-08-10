"""FastAPI app factory, lifespan wiring, CORS, and GET /healthz.

Run from ``backend/`` with::

    uvicorn app.main:app --host 127.0.0.1 --port 8710
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from app.config import SERVER_VERSION, Settings
from app.db import Database, DayCounter
from app.debug import router as debug_router
from app.feedback import router as feedback_router
from app.llm_provider import LLMRuntime, build_llm_runtime, close_llm_runtime
from app.logging_setup import banner, configure_logging
from app.rate_limit import QuotaCooldown, TokenBucket
from app.setup import router as setup_router
from app.stats import router as stats_router
from app.transcriber import create_transcriber
from app.ws import router as ws_router

logger = logging.getLogger(__name__)

# Extension pages (popup healthz preflight) plus local dev tools.
_CORS_ORIGIN_REGEX = (
    r"^(chrome-extension://[a-z]{32}|https?://(localhost|127\.0\.0\.1)(:\d+)?)$"
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build all process-wide shared state on ``app.state``.

    Slots:

    - ``app.state.settings``        — :class:`app.config.Settings` (boot-time)
    - ``app.state.llm_runtime``     — :class:`app.llm_provider.LLMRuntime`:
      the shared provider client plus the process-wide gate/checker used by
      the debug endpoints. When the backend boots UNCONFIGURED (no API key),
      the runtime's ``client``/``gate``/``checker`` are ``None``; ``POST
      /setup/credentials`` later swaps the slot atomically with a live stack.
      Sessions and debug requests read the slot fresh per request/session.
    - ``app.state.verify_bucket``   — :class:`app.rate_limit.TokenBucket`
    - ``app.state.quota_cooldown``  — :class:`app.rate_limit.QuotaCooldown`
    - ``app.state.transcriber``     — :class:`app.transcriber.Transcriber`;
      the Whisper model loads via ``asyncio.to_thread`` so a failed download
      aborts startup loudly instead of failing mid-session. Whisper loads
      even while unconfigured — only the LLM stack is deferred.
    - ``app.state.stt_executor``    — ``ThreadPoolExecutor(max_workers=1)``
      for ``transcribe_window`` (ctranslate2 releases the GIL; a single
      worker keeps CPU use predictable); shut down after ``yield`` with
      ``shutdown(wait=False, cancel_futures=True)``.
    """
    settings = Settings()
    configure_logging(settings.log_level)
    cooldown = QuotaCooldown()

    app.state.settings = settings
    app.state.quota_cooldown = cooldown
    app.state.verify_bucket = TokenBucket(rate_per_min=settings.verify_rpm, burst=2)
    app.state.llm_runtime = build_llm_runtime(settings, cooldown)

    # Analytics persistence + the in-memory "checks today" counter (seeded
    # from the db so a restart doesn't zero the popup readout).
    db = Database(settings.db_path)
    await db.open()
    app.state.db = db
    app.state.verify_counter = DayCounter(initial=await db.count_checks_today())

    # STT_BACKEND picks the engine (faster-whisper or torch); the pipeline
    # never learns which one it got.
    transcriber = create_transcriber(settings)
    # Blocking model load off the event loop; a failure here (bad model name,
    # download error, unavailable accelerator) propagates and aborts startup
    # — deliberately loud.
    await asyncio.to_thread(transcriber.load)
    app.state.transcriber = transcriber
    stt_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt")
    app.state.stt_executor = stt_executor

    configured = app.state.llm_runtime.configured
    rows: list[tuple[str, str]] = [
        ("listening", f"http://{settings.host}:{settings.port}"),
        ("speech", transcriber.describe()),
    ]
    if configured:
        rows += [
            (
                "gate",
                f"{settings.resolved_gate_provider} · {settings.active_gate_model}",
            ),
            (
                "verify",
                f"{settings.resolved_verify_provider} · "
                f"{settings.active_verify_model}",
            ),
        ]
    else:
        rows.append(("gate/verify", "NOT CONFIGURED — add an API key"))
    checks_today = app.state.verify_counter.value
    rows += [
        ("sensitivity", f"gate every {settings.gate_interval_s:.0f}s"),
        (
            "analytics",
            f"{settings.db_path} · {checks_today} check(s) today "
            f"(~${checks_today * settings.cost_per_verify_usd:.2f})",
        ),
        ("dashboard", f"http://{settings.host}:{settings.port}/dashboard"),
        (
            "extras",
            f"vision={'on' if settings.vision_enabled else 'off'} "
            f"transcripts={'on' if settings.send_transcripts else 'off'} "
            f"debug_endpoints={'on' if settings.debug_endpoints else 'off'}",
        ),
    ]
    logger.info(
        "\n%s",
        banner(rows, f"Live Stream Fact-Checker backend {SERVER_VERSION}"),
    )
    if not configured:
        logger.warning(
            "no API key yet — add one from the extension's options page; "
            "capture will refuse to start until then"
        )
    try:
        yield
    finally:
        # cancel_futures drops QUEUED windows; wait=True then blocks for the
        # one that is already running. Both halves matter: without the wait,
        # shutdown returns while a transcription is still in flight and the
        # unload below frees the model out from under it (on a GPU that means
        # releasing device memory mid-kernel). The wait is bounded by a single
        # ~4 s window, and runs off the event loop so shutdown stays async.
        await asyncio.to_thread(
            stt_executor.shutdown, wait=True, cancel_futures=True
        )
        # Release the speech model AFTER its executor is down, so no job is
        # mid-inference. Matters most on GPUs, where the weights would
        # otherwise hold VRAM for the whole process lifetime.
        try:
            transcriber.unload()
        except Exception as exc:
            logger.warning("error unloading the speech model: %s", exc)
        # Read the slot fresh: a setup hot-swap may have replaced the boot
        # runtime (the swap closes the OLD clients itself). close_llm_runtime
        # closes each DISTINCT stage client exactly once.
        runtime: LLMRuntime = app.state.llm_runtime
        try:
            await close_llm_runtime(runtime)
        except Exception as exc:
            logger.warning("error closing LLM clients: %s", exc)
        # The db closes LAST so in-flight session-end writes from cancelled
        # websocket tasks have the best chance of landing; close() drains
        # the db executor queue before shutting it down.
        try:
            await db.close()
        except Exception as exc:
            logger.warning("error closing analytics database: %s", exc)


def create_app() -> FastAPI:
    """Assemble the FastAPI application (factory keeps tests self-contained)."""
    app = FastAPI(
        title="Twitch Live Fact-Checker",
        version=SERVER_VERSION,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=_CORS_ORIGIN_REGEX,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # DNS-rebinding hardening: CORS does nothing against a malicious page
    # whose own domain is re-pointed at 127.0.0.1 (same-origin, arbitrary
    # Host), so reject any Host that is not localhost with a 400. Starlette
    # strips the port before matching (":8710" variants are covered), and
    # the check applies to both HTTP and the /ws/audio websocket scope. If
    # the backend is ever served on a LAN hostname, add it here.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    app.include_router(debug_router)
    app.include_router(feedback_router)
    app.include_router(setup_router)
    app.include_router(stats_router)
    app.include_router(ws_router)

    @app.get("/healthz")
    async def healthz(request: Request) -> dict[str, Any]:
        """Liveness + config echo; the extension popup preflights this.

        While unconfigured, ``configured`` is ``False`` and every LLM field
        (``llm_provider``/``gate_model``/``verify_model``) is ``null``.
        """
        settings: Settings = request.app.state.settings
        runtime: LLMRuntime = request.app.state.llm_runtime
        counter: DayCounter = request.app.state.verify_counter
        configured = runtime.configured
        return {
            "status": "ok",
            "server_version": SERVER_VERSION,
            "whisper_model": settings.whisper_model,
            "configured": configured,
            "llm_provider": runtime.settings.llm_provider if configured else None,
            "gate_provider": (
                runtime.settings.resolved_gate_provider if configured else None
            ),
            "verify_provider": (
                runtime.settings.resolved_verify_provider if configured else None
            ),
            "gate_model": runtime.settings.active_gate_model if configured else None,
            "verify_model": (
                runtime.settings.active_verify_model if configured else None
            ),
            "checks_today": counter.value,
            "est_cost_today_usd": round(
                counter.value * settings.cost_per_verify_usd, 4
            ),
        }

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    _settings = Settings()
    uvicorn.run(
        "app.main:app",
        host=_settings.host,
        port=_settings.port,
        log_level=_settings.log_level.lower(),
    )
