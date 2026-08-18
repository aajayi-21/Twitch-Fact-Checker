"""``/ws/audio`` endpoint: hello handshake, preemption, session lifecycle.

Sessions live in ``app.state.sessions`` (:class:`app.sessions.SessionRegistry`),
which also owns the preemption rule. Under the default ``"channel"`` scope a
new connection preempts only sessions on the same ``(platform, channel)`` — so
an extension reconnect on one channel still self-heals with zero bookkeeping,
while two different channels coexist (which the streamer bot needs). See
``app/sessions.py`` for the scopes and for why capacity stays small.

The preempted socket receives ``{"type":"error","code":"superseded",
"fatal":true}`` and a 1000 close, exactly as before.

Close codes (§2.1): 1000 normal (stop / superseded), 1008 protocol violation
(bad or missing hello), 1011 internal fatal error or at-capacity.
"""

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.config import SERVER_VERSION, Settings
from app.contradiction import ContradictionDetector
from app.embeddings import OllamaEmbedder
from app.llm_provider import LLMRuntime, create_claim_gate, create_fact_checker
from app.models import ClientHello, ErrorCode, ErrorFrame, ReadyFrame
from app.pipeline import SessionPipeline
from app.sessions import SessionLimitExceeded, SessionRegistry

NOT_CONFIGURED_MESSAGE = (
    "Backend has no API key yet — add one in the extension options."
)

logger = logging.getLogger(__name__)

router = APIRouter()

HELLO_TIMEOUT_S = 5.0


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


async def _reject(
    websocket: WebSocket, *, code: ErrorCode, message: str, close_code: int
) -> None:
    """Deliver a fatal error frame and close; never raises."""
    logger.warning("rejecting /ws/audio connection: %s", code)
    try:
        await websocket.send_json(
            ErrorFrame(code=code, message=message, fatal=True).model_dump()
        )
        await websocket.close(code=close_code)
    except Exception as exc:
        logger.debug("could not deliver %s rejection: %s", code, exc)


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
    claim_gate = create_claim_gate(settings, runtime.gate_client)
    return SessionPipeline(
        websocket=websocket,
        hello=hello,
        settings=settings,
        transcriber=state.transcriber,
        stt_executor=state.stt_executor,
        claim_gate=claim_gate,
        fact_checker=create_fact_checker(
            settings, runtime.verify_client, state.quota_cooldown
        ),
        verify_bucket=state.verify_bucket,
        quota_cooldown=state.quota_cooldown,
        db=state.db,
        verify_counter=state.verify_counter,
        # Fan-out to non-socket consumers. Inert (and free) until something
        # subscribes, which on the viewer backend is never.
        event_hub=getattr(state, "events", None),
        # Judge rides the session's own gate; the embedder is stateless
        # per-call, degrading to lexical matching when Ollama is absent.
        contradiction_detector=ContradictionDetector(
            gate=claim_gate,
            embedder=OllamaEmbedder(
                settings.ollama_base_url, settings.ollama_embed_model
            ),
        ),
    )


@router.websocket("/ws/audio")
async def audio_ws(websocket: WebSocket) -> None:
    """Accept -> preempt superseded -> hello -> ready -> run the pipeline."""
    registry: SessionRegistry = websocket.app.state.sessions
    await websocket.accept()

    # Global scope only: preempt promptly on connect (§3.2) so the old session
    # dies without waiting up to 5 s for this connection's hello. Under the
    # default channel scope this is a no-op — the channel is not known until
    # the hello parses, so the preempt happens inside register() instead.
    await registry.preempt_on_accept()

    hello = await _receive_hello(websocket)
    if hello is None:
        return

    # Contract §setup: while unconfigured, a valid hello is answered with a
    # fatal not_configured error frame and a 1011 close — no session starts.
    runtime: LLMRuntime = websocket.app.state.llm_runtime
    if not runtime.configured:
        await _reject(
            websocket,
            code="not_configured",
            message=NOT_CONFIGURED_MESSAGE,
            close_code=1011,
        )
        return

    # Construction moved OUT of the lock (it must precede register(), which
    # needs the pipeline's channel key). It is pure and side-effect-free, so
    # nothing observable happens until the registry accepts it.
    pipeline = _build_pipeline(websocket, hello)
    try:
        await registry.register(pipeline)
    except SessionLimitExceeded as exc:
        await _reject(
            websocket, code="too_many_sessions", message=str(exc), close_code=1011
        )
        return

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
        registry.unregister(pipeline)
