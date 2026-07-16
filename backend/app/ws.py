"""``/ws/audio`` endpoint: hello handshake, preemption, session lifecycle.

Single-user by design: a new connection PREEMPTS any existing one (the old
socket receives ``{"type":"error","code":"superseded","fatal":true}`` and a
1000 close). Extension reconnects therefore self-heal with zero session
bookkeeping on either side.

Close codes (§2.1): 1000 normal (stop / superseded), 1008 protocol violation
(bad or missing hello), 1011 internal fatal error.

``current_pipeline`` is the module attribute consumed by ``app.debug`` — when
a session is live, ``POST /debug/text`` pushes verdict frames onto it via
``enqueue_frame`` so popups appear on a real Twitch tab from a curl.
"""

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.config import SERVER_VERSION, Settings
from app.llm_provider import LLMRuntime, create_claim_gate, create_fact_checker
from app.models import ClientHello, ErrorFrame, ReadyFrame
from app.pipeline import SessionPipeline

NOT_CONFIGURED_MESSAGE = (
    "Backend has no API key yet — add one in the extension options."
)

logger = logging.getLogger(__name__)

router = APIRouter()

HELLO_TIMEOUT_S = 5.0

# The one live session, if any — single-user by design.
current_pipeline: SessionPipeline | None = None

# Serializes the preempt-then-register handoff in ``audio_ws``. Without it, a
# connection suspended inside ``preempt()`` (which awaits a send/close on the
# old socket) leaves a window in which a third connection sees the
# already-preempted pipeline, gets ``preempt()``'s synchronous early return,
# and registers — ending with TWO live pipelines and an orphaned slot.
_registration_lock = asyncio.Lock()


async def _receive_hello(websocket: WebSocket) -> ClientHello | None:
    """First frame must be a valid hello within 5 s; else error + close 1008.

    Returns the validated hello, or None after the connection has been
    rejected (or the client disconnected on its own).
    """
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=HELLO_TIMEOUT_S)
        return ClientHello.model_validate_json(raw)
    except TimeoutError:
        reason = f"no hello frame received within {HELLO_TIMEOUT_S:.0f}s"
    except KeyError:
        reason = "first frame must be a JSON text hello frame, got binary"
    except ValidationError as exc:
        reason = f"invalid hello frame: {exc.error_count()} validation error(s)"
    except WebSocketDisconnect:
        logger.info("client disconnected before sending hello")
        return None
    logger.warning("rejecting /ws/audio connection: %s", reason)
    try:
        await websocket.send_json(
            ErrorFrame(code="bad_hello", message=reason, fatal=True).model_dump()
        )
        await websocket.close(code=1008)
    except Exception as exc:
        logger.debug("could not deliver bad_hello rejection: %s", exc)
    return None


async def _reject_not_configured(websocket: WebSocket) -> None:
    """Error frame + close 1011 for a hello on an unconfigured backend."""
    logger.warning("rejecting /ws/audio connection: backend not configured")
    try:
        await websocket.send_json(
            ErrorFrame(
                code="not_configured", message=NOT_CONFIGURED_MESSAGE, fatal=True
            ).model_dump()
        )
        await websocket.close(code=1011)
    except Exception as exc:
        logger.debug("could not deliver not_configured rejection: %s", exc)


def _build_pipeline(websocket: WebSocket, hello: ClientHello) -> SessionPipeline:
    """Assemble a session pipeline from process-wide state.

    The claim gate and fact checker are constructed FRESH per session (via
    the provider factory, so the transcript buffer and the dedupe memory are
    session-scoped) around the CURRENT ``llm_runtime``'s shared client — a
    setup hot-swap preempts any live session (fatal ``credentials_updated``
    frame) and applies to every session from then on. The token bucket,
    quota cooldown, transcriber, and STT executor are shared process-wide
    (``app.state``, built in lifespan).
    """
    state = websocket.app.state
    runtime: LLMRuntime = state.llm_runtime
    settings: Settings = runtime.settings
    return SessionPipeline(
        websocket=websocket,
        hello=hello,
        settings=settings,
        transcriber=state.transcriber,
        stt_executor=state.stt_executor,
        claim_gate=create_claim_gate(settings, runtime.client),
        fact_checker=create_fact_checker(
            settings, runtime.client, state.quota_cooldown
        ),
        verify_bucket=state.verify_bucket,
        quota_cooldown=state.quota_cooldown,
    )


@router.websocket("/ws/audio")
async def audio_ws(websocket: WebSocket) -> None:
    """Accept -> preempt existing -> hello -> ready -> run the pipeline."""
    global current_pipeline
    await websocket.accept()

    # Preempt promptly on connect (§3.2) so the old session dies without
    # waiting up to 5 s for this connection's hello.
    if current_pipeline is not None:
        await current_pipeline.preempt()

    hello = await _receive_hello(websocket)
    if hello is None:
        return

    # Contract §setup: while unconfigured, a valid hello is answered with a
    # fatal not_configured error frame and a 1011 close — no session starts.
    runtime: LLMRuntime = websocket.app.state.llm_runtime
    if not runtime.configured:
        await _reject_not_configured(websocket)
        return

    async with _registration_lock:
        # Re-read under the lock: another connection may have raced us in
        # while we awaited the hello. The lock keeps preempt+register atomic
        # with respect to other registrants, so exactly one pipeline is live
        # once the dust settles (§2.1 single-user invariant). The old
        # pipeline's ``finally`` may still clear the slot concurrently; that
        # is harmless — it identity-checks, and we overwrite here anyway.
        if current_pipeline is not None:
            await current_pipeline.preempt()
        pipeline = _build_pipeline(websocket, hello)
        current_pipeline = pipeline
    settings: Settings = websocket.app.state.settings
    try:
        await websocket.send_json(
            ReadyFrame(
                server_version=SERVER_VERSION, model=settings.whisper_model
            ).model_dump()
        )
        await pipeline.run()
    except WebSocketDisconnect:
        logger.info("client disconnected")
    except Exception:
        logger.exception("unhandled error in /ws/audio session")
        try:
            await websocket.close(code=1011)
        except Exception as close_exc:
            logger.debug("websocket close failed: %s", close_exc)
    finally:
        pipeline.close()
        if current_pipeline is pipeline:
            current_pipeline = None
