"""Check a piece of raw text through the real gate -> dedupe -> verify path.

Shared by two routes:

- ``POST /setup/test-check`` (app/setup.py): the app's sample check during
  setup. Always available; returns the result and touches nothing else.
- ``POST /debug/text`` (app/debug.py): the development path, off unless
  ``DEBUG_ENDPOINTS=true``. It also pushes the verdicts into every live
  session so they appear on a real stream tab.

Both make paid provider calls, and both are reachable only from origins that
:mod:`app.origins` allows.
"""

import logging

from fastapi import HTTPException, Request

from app.claim_gate import GateError
from app.config import SENSITIVITY_THRESHOLDS
from app.fact_checker import QuotaExceededError, VerificationError
from app.llm_provider import LLMRuntime
from app.models import (
    DebugTextRequest,
    DebugTextResponse,
    Verdict,
    resolve_enabled_topics,
)
from app.rate_limit import QuotaCooldown, TokenBucket

logger = logging.getLogger(__name__)


async def check_text(body: DebugTextRequest, request: Request) -> DebugTextResponse:
    """Run ``body.text`` through claim detection and verification.

    Raises:
        HTTPException: 400 on empty text, 409 while the backend has no API
            key, 502 with a descriptive detail on an LLM failure; never a bare
            500.
    """
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
            detail="backend not configured — add an API key in the extension's "
            "app (AI provider)",
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
    except Exception as exc:  # never a bare 500 from this path
        logger.exception("unexpected error checking text")
        raise HTTPException(status_code=502, detail=f"unexpected error: {exc}") from exc

    return DebugTextResponse(claims=claims, verdicts=verdicts)
