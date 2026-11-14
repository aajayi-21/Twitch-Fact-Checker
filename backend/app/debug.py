"""Development endpoints, off unless ``DEBUG_ENDPOINTS=true``.

- ``POST /debug/text`` — the no-audio test path (gate -> dedupe -> verify):
  prompts and the overlay UI can be exercised by curling localhost with raw
  text, no stream or capture involved. Verdict frames are also pushed onto
  every live session's outbound queue so popups appear on a real stream tab.
  The checking itself lives in :mod:`app.text_check`, shared with the app's
  always-on sample check (``POST /setup/test-check``).
- ``POST /debug/stt/fail`` — rehearse the speech engine's failure path.
"""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.config import Settings
from app.models import (
    DebugTextRequest,
    DebugTextResponse,
    SttFaultRequest,
    Verdict,
    VerdictFrame,
)
from app.sessions import SessionRegistry
from app.stt_supervisor import SttSupervisor
from app.text_check import check_text

logger = logging.getLogger(__name__)

router = APIRouter()


def _push_verdicts_to_live_sessions(
    registry: SessionRegistry, verdicts: list[Verdict]
) -> None:
    """Push verdict frames onto every live pipeline's outbound queue.

    Reads ``app.state.sessions`` directly — the old contract duck-typed
    ``ws.current_pipeline`` through ``getattr`` because the registry did not
    exist yet. Any failure here is logged and never fails the debug request.
    """
    for pipeline in registry.all():
        for verdict in verdicts:
            frame = VerdictFrame.from_verdict(verdict).model_dump()
            try:
                pipeline.enqueue_frame(frame)
            except Exception as exc:
                logger.warning(
                    "failed to push verdict frame to session %s: %s",
                    pipeline.session_id,
                    exc,
                )


@router.post("/debug/text", response_model=DebugTextResponse)
async def inject_text(body: DebugTextRequest, request: Request) -> DebugTextResponse:
    """Run the full gate -> dedupe -> verify path on raw text, and push the
    verdicts into every live session.

    Responses: 404 when DEBUG_ENDPOINTS is off (the default), otherwise those
    of :func:`app.text_check.check_text` (400 empty text, 409 no API key, 502
    LLM failure; never a bare 500).
    """
    settings: Settings = request.app.state.settings
    if not settings.debug_endpoints:
        raise HTTPException(status_code=404, detail="Not Found")
    result = await check_text(body, request)
    _push_verdicts_to_live_sessions(request.app.state.sessions, result.verdicts)
    return result


@router.post("/debug/stt/fail")
async def inject_stt_fault(body: SttFaultRequest, request: Request) -> dict[str, Any]:
    """Make the next ``windows`` transcription windows fail (breaker rehearsal).

    Drives :class:`app.stt_supervisor.SttSupervisor` through its real
    recovery path: at the failure threshold it reloads the engine on the CPU
    (``stt_degraded`` frame, ``/healthz`` status ``degraded``); injecting
    again afterwards exercises the fatal ``stt_failure`` path. 404 when
    ``DEBUG_ENDPOINTS=false``.
    """
    settings: Settings = request.app.state.settings
    if not settings.debug_endpoints:
        raise HTTPException(status_code=404, detail="Not Found")
    supervisor: SttSupervisor = request.app.state.stt_supervisor
    supervisor.inject_failures = body.windows
    logger.warning(
        "debug: the next %d transcription window(s) will fail (threshold %d)",
        body.windows,
        supervisor.failure_threshold,
    )
    return {"injected": body.windows, "stt": supervisor.snapshot()}
