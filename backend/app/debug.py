"""POST /debug/text — the no-audio test path (gate -> dedupe -> verify).

The keystone of testability: prompts and the overlay UI can be exercised by
curling localhost with raw text, no stream or capture involved. If a live
WebSocket session exists, verdict frames are additionally pushed onto its
outbound queue so popups appear on a real Twitch tab.

``app.ws`` is created by a later milestone, so it is imported LAZILY inside
the handler — this module must import cleanly both before and after ws.py
exists.
"""

import logging

from fastapi import APIRouter, HTTPException, Request

from app.claim_gate import GateError
from app.config import SENSITIVITY_THRESHOLDS, Settings
from app.fact_checker import QuotaExceededError, VerificationError
from app.llm_provider import LLMRuntime
from app.models import (
    DebugTextRequest,
    DebugTextResponse,
    Verdict,
    VerdictFrame,
    resolve_enabled_topics,
)
from app.rate_limit import QuotaCooldown, TokenBucket
from app.sessions import SessionRegistry

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
    """Run the full gate -> dedupe -> verify path synchronously on raw text.

    Responses: 400 on empty text, 404 when DEBUG_ENDPOINTS=false, 409 while
    the backend has no API key yet, 502 with a descriptive detail on LLM
    failure — never a bare 500.
    """
    settings: Settings = request.app.state.settings
    if not settings.debug_endpoints:
        raise HTTPException(status_code=404, detail="Not Found")

    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text must be non-empty")

    # Fetched through the hot-swappable runtime per request, so a setup
    # credentials POST takes effect immediately.
    runtime: LLMRuntime = request.app.state.llm_runtime
    gate = runtime.gate
    checker = runtime.checker
    if gate is None or checker is None:
        raise HTTPException(
            status_code=409,
            detail="backend not configured — add an API key via the "
            "extension options",
        )
    bucket: TokenBucket = request.app.state.verify_bucket
    cooldown: QuotaCooldown = request.app.state.quota_cooldown

    try:
        claims = await gate.extract_claims(context="", new_text=text)
    except GateError as exc:
        raise HTTPException(
            status_code=502, detail=f"claim gate failed: {exc}"
        ) from exc

    threshold = SENSITIVITY_THRESHOLDS[body.sensitivity]
    enabled_topics = resolve_enabled_topics(body.enabled_topics)
    verdicts: list[Verdict] = []
    try:
        for claim in claims:
            if claim.check_worthiness < threshold:
                logger.info(
                    "claim below %s threshold (%.2f < %.2f): %r",
                    body.sensitivity,
                    claim.check_worthiness,
                    threshold,
                    claim.claim_text,
                )
                continue
            # Same semantics as the live pipeline's topic filter: the claim
            # stays in the response's ``claims`` (gate output is always
            # reported) but produces no verdict, and never registers in the
            # dedupe memory.
            if claim.topic not in enabled_topics:
                logger.info(
                    "claim topic %r disabled by filter: %r",
                    claim.topic,
                    claim.claim_text,
                )
                continue
            if checker.is_duplicate(claim.claim_text):
                logger.info("dropping duplicate claim: %r", claim.claim_text)
                continue
            if cooldown.active:
                logger.warning(
                    "quota cooldown active (%.0fs left); dropping claim: %r",
                    cooldown.remaining_s,
                    claim.claim_text,
                )
                continue
            await bucket.acquire()
            try:
                verdicts.append(
                    await checker.check(claim.claim_text, topic=claim.topic)
                )
            except QuotaExceededError as exc:
                # Cooldown is now active; remaining claims will be dropped
                # above, mirroring the live pipeline's drop-during-cooldown.
                logger.warning("quota exceeded mid-request: %s", exc)
            except VerificationError as exc:
                raise HTTPException(
                    status_code=502, detail=f"verification failed: {exc}"
                ) from exc
    except HTTPException:
        raise
    except Exception as exc:  # never a bare 500 from this endpoint
        logger.exception("unexpected error in /debug/text")
        raise HTTPException(status_code=502, detail=f"unexpected error: {exc}") from exc

    _push_verdicts_to_live_sessions(request.app.state.sessions, verdicts)
    return DebugTextResponse(claims=claims, verdicts=verdicts)
