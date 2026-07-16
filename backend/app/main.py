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
from app.debug import router as debug_router
from app.llm_provider import LLMRuntime, build_llm_runtime, close_llm_client
from app.rate_limit import QuotaCooldown, TokenBucket
from app.setup import router as setup_router
from app.transcriber import Transcriber
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
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cooldown = QuotaCooldown()

    app.state.settings = settings
    app.state.quota_cooldown = cooldown
    app.state.verify_bucket = TokenBucket(rate_per_min=settings.verify_rpm, burst=2)
    app.state.llm_runtime = build_llm_runtime(settings, cooldown)

    transcriber = Transcriber(
        model_name=settings.whisper_model,
        device=settings.whisper_device,
        compute_type=settings.whisper_compute_type,
    )
    # Blocking model load off the event loop; a failure here (bad model name,
    # download error) propagates and aborts startup — deliberately loud.
    await asyncio.to_thread(transcriber.load)
    app.state.transcriber = transcriber
    stt_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt")
    app.state.stt_executor = stt_executor

    if app.state.llm_runtime.configured:
        logger.info(
            "backend %s ready: provider=%s gate=%s verify=%s whisper=%s "
            "debug_endpoints=%s",
            SERVER_VERSION,
            settings.llm_provider,
            settings.active_gate_model,
            settings.active_verify_model,
            settings.whisper_model,
            settings.debug_endpoints,
        )
    else:
        logger.warning(
            "backend %s started UNCONFIGURED: no API key yet — add one via "
            "the extension options (POST /setup/credentials); whisper=%s",
            SERVER_VERSION,
            settings.whisper_model,
        )
    try:
        yield
    finally:
        stt_executor.shutdown(wait=False, cancel_futures=True)
        # Read the slot fresh: a setup hot-swap may have replaced the boot
        # runtime (the swap closes the OLD client itself).
        runtime: LLMRuntime = app.state.llm_runtime
        if runtime.client is not None:
            try:
                await close_llm_client(runtime.client)
            except Exception as exc:
                logger.warning("error closing LLM client: %s", exc)


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
    app.include_router(setup_router)
    app.include_router(ws_router)

    @app.get("/healthz")
    async def healthz(request: Request) -> dict[str, Any]:
        """Liveness + config echo; the extension popup preflights this.

        While unconfigured, ``configured`` is ``False`` and every LLM field
        (``llm_provider``/``gate_model``/``verify_model``) is ``null``.
        """
        settings: Settings = request.app.state.settings
        runtime: LLMRuntime = request.app.state.llm_runtime
        configured = runtime.configured
        return {
            "status": "ok",
            "server_version": SERVER_VERSION,
            "whisper_model": settings.whisper_model,
            "configured": configured,
            "llm_provider": runtime.settings.llm_provider if configured else None,
            "gate_model": runtime.settings.active_gate_model if configured else None,
            "verify_model": (
                runtime.settings.active_verify_model if configured else None
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
