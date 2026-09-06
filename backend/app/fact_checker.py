"""Grounded claim verification — provider-neutral core with hard invariants.

This module owns everything that is independent of the LLM provider:

- rapidfuzz duplicate suppression (:meth:`FactChecker.is_duplicate`),
- the :meth:`FactChecker.check` failure ladder (one retry on timeout, quota
  cooldown via :class:`app.rate_limit.QuotaCooldown`),
- the structured-call -> fallback-chain orchestration
  (:meth:`FactChecker._check_once`), and
- :meth:`FactChecker._enforce_invariants` — the HARD anti-hallucination rule
  that a non-UNVERIFIED verdict without citations is downgraded, plus the
  explanation length clamp.

The actual LLM transport is two abstract methods —
:meth:`FactChecker._grounded_structured` and
:meth:`FactChecker._grounded_fallback` — implemented by
:class:`app.llm_gemini.GeminiFactChecker` and
:class:`app.llm_openrouter.OpenRouterFactChecker` so SDK drift stays local to
those modules. Citations come exclusively from provider grounding metadata
(``url_citation`` annotations), never from model prose — models fabricate
URLs.

Anti-hallucination is structural: the grounded schema is deliberately flat
(two fields — complex schemas plus grounding are the known 400 sharp edge),
and :meth:`FactChecker._enforce_invariants` downgrades any non-UNVERIFIED
verdict that arrived without citations.
"""

import asyncio
import logging
import re
import string
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError
from rapidfuzz import fuzz

from app.models import Source, Topic, Verdict, VerdictPayload
from app.rate_limit import QuotaCooldown

logger = logging.getLogger(__name__)

DUPLICATE_SIMILARITY_THRESHOLD = 85
MAX_SOURCES = 5
MAX_EXPLANATION_CHARS = 450
#: The verify model's rating of how directly the results addressed the claim
#: (see :data:`app.models.Evidence`); only "strong" may carry a real verdict.
EVIDENCE_LEVELS: tuple[str, ...] = ("strong", "partial", "none")
NO_SOURCES_NOTE = "No verifiable sources were retrieved."
WEAK_EVIDENCE_NOTE = "Retrieved sources did not directly address this claim."

# Flat two-field schema for the grounded structured call (plan §4): complex
# schemas combined with search grounding are the known 400 risk, so this stays
# minimal and sources are extracted from annotations in code.
FLAT_VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "label": {
            "type": "string",
            "enum": ["TRUE", "FALSE", "MISLEADING", "UNVERIFIED"],
        },
        "explanation": {"type": "string"},
    },
    "required": ["label", "explanation"],
}

_LABEL_LINE_RE = re.compile(
    r"LABEL\s*:\s*\**\s*(TRUE|FALSE|MISLEADING|UNVERIFIED)", re.IGNORECASE
)
_EVIDENCE_LINE_RE = re.compile(
    r"EVIDENCE\s*:\s*\**\s*(strong|partial|none)", re.IGNORECASE
)
_EXPLANATION_LINE_RE = re.compile(r"EXPLANATION\s*:\s*(.+)", re.IGNORECASE | re.DOTALL)
_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

DEFAULT_RETRY_AFTER_S = 60.0


class QuotaExceededError(Exception):
    """The provider returned a quota failure; the cooldown was already tripped."""


class VerificationError(Exception):
    """Verification failed after the whole fallback chain was exhausted."""


class _FallbackNeeded(Exception):
    """Internal: the structured grounded call failed or produced unusable output.

    Shared by both provider transports: raising it from
    :meth:`FactChecker._grounded_structured` routes the claim through
    :meth:`FactChecker._grounded_fallback` with ``used_fallback=True``.
    """


def normalize_claim(claim: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace (dedupe key).

    Public: also the ``claims.normalized`` column in :mod:`app.db` and the
    lexical-similarity key in :mod:`app.contradiction`.
    """
    lowered = claim.lower().translate(str.maketrans("", "", string.punctuation))
    return " ".join(lowered.split())


# Backwards-compatible alias (pre-analytics name).
_normalize_claim = normalize_claim


def _today() -> str:
    """Current UTC date for the verify prompt (live streams are 'now')."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class FactChecker(ABC):
    """Session-scoped verifier: dedupe, grounded check, fallbacks, invariants."""

    def __init__(
        self,
        cooldown: QuotaCooldown,
        verify_timeout_s: float = 45.0,
    ) -> None:
        self._cooldown = cooldown
        self._verify_timeout_s = verify_timeout_s
        self._seen_normalized_claims: list[str] = []

    def is_duplicate(self, claim: str) -> bool:
        """Fuzzy check-and-register against every prior claim this session.

        Uses rapidfuzz ``token_set_ratio >= 85`` on normalized text (the
        biggest quota saver — streamers repeat themselves). A novel claim is
        recorded as a side effect so the caller can simply drop duplicates.
        """
        normalized = _normalize_claim(claim)
        for existing in self._seen_normalized_claims:
            if fuzz.token_set_ratio(normalized, existing) >= (
                DUPLICATE_SIMILARITY_THRESHOLD
            ):
                return True
        self._seen_normalized_claims.append(normalized)
        return False

    async def check(
        self, claim: str, topic: Topic = "other", image_b64: str | None = None
    ) -> Verdict:
        """Verify one claim; one retry on timeout; invariants always enforced.

        ``topic`` is the gate's classification for the claim; it is carried
        through onto the assembled :class:`Verdict` untouched (verification
        itself is topic-agnostic).

        ``image_b64`` (a captured stream frame) gets ONE image-bearing
        attempt under the HARD RULE that an image must never make a check
        fail that would succeed without it: any non-quota failure of that
        attempt falls through to the unchanged image-free ladder below. A
        quota failure propagates immediately — the cooldown is tripped and a
        retry would spend money to fail again.

        Raises:
            QuotaExceededError: on a quota failure (cooldown already tripped).
            VerificationError: on timeout after retry or exhausted fallbacks.
        """
        result: tuple[VerdictPayload, list[Source], bool] | None = None
        if image_b64 is not None:
            try:
                result = await asyncio.wait_for(
                    self._check_once(claim, image_b64),
                    timeout=self._verify_timeout_s,
                )
            except QuotaExceededError:
                raise
            except Exception as exc:
                logger.warning(
                    "image-bearing verification failed (%s); retrying "
                    "without the image: %r",
                    exc,
                    claim,
                )
        if result is None:
            result = await self._check_text_only_with_retry(claim)
        payload, sources, used_fallback = result
        payload = self._enforce_invariants(payload, sources)
        return Verdict(
            claim=claim,
            topic=topic,
            label=payload.label,
            explanation=payload.explanation,
            sources=sources,
            used_fallback=used_fallback,
            evidence=payload.evidence,
        )

    async def _check_text_only_with_retry(
        self, claim: str
    ) -> tuple[VerdictPayload, list[Source], bool]:
        """The pre-vision ladder: one timeout retry, image never involved."""
        try:
            return await asyncio.wait_for(
                self._check_once(claim), timeout=self._verify_timeout_s
            )
        except asyncio.TimeoutError:
            logger.warning(
                "verification timed out after %.0fs, retrying once: %r",
                self._verify_timeout_s,
                claim,
            )
            try:
                return await asyncio.wait_for(
                    self._check_once(claim), timeout=self._verify_timeout_s
                )
            except asyncio.TimeoutError as exc:
                raise VerificationError(
                    f"verification timed out twice "
                    f"({self._verify_timeout_s:.0f}s each) for claim: {claim!r}"
                ) from exc

    async def _check_once(
        self, claim: str, image_b64: str | None = None
    ) -> tuple[VerdictPayload, list[Source], bool]:
        """Structured grounded call; on failure/unusable output run the fallback."""
        try:
            payload, sources = await self._grounded_structured(claim, image_b64)
            return payload, sources, False
        except _FallbackNeeded as exc:
            logger.warning(
                "grounded structured call unusable (%s); using fallback chain", exc
            )
        payload, sources = await self._grounded_fallback(claim, image_b64)
        return payload, sources, True

    @abstractmethod
    async def _grounded_structured(
        self, claim: str, image_b64: str | None = None
    ) -> tuple[VerdictPayload, list[Source]]:
        """Provider transport: grounded search + flat structured output.

        ``image_b64`` (bare base64 JPEG), when set, is attached as an image
        content part alongside the prompt (built with ``with_image=True``).

        Implementations must raise :class:`_FallbackNeeded` when the
        structured path is unsupported or its output is unparseable,
        :class:`QuotaExceededError` after tripping the cooldown on quota
        failures, :class:`VerificationError` on other API failures, and
        :class:`asyncio.TimeoutError` on transport timeouts (so the
        :meth:`check` ladder retries once).
        """

    @abstractmethod
    async def _grounded_fallback(
        self, claim: str, image_b64: str | None = None
    ) -> tuple[VerdictPayload, list[Source]]:
        """Provider transport: grounded plain-text call + lenient parse chain.

        Citations still come from the grounded step regardless of which parse
        succeeds. Same exception contract as :meth:`_grounded_structured`,
        except :class:`_FallbackNeeded` must not be raised (this IS the
        fallback).
        """

    @staticmethod
    def _parse_verdict_json(raw: str) -> VerdictPayload:
        """Parse a (possibly fenced) JSON object into a :class:`VerdictPayload`."""
        cleaned = _JSON_FENCE_RE.sub("", raw).strip()
        if not cleaned:
            raise ValueError("empty response text")
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise ValueError(f"no JSON object found in: {cleaned[:120]!r}")
        return VerdictPayload.model_validate_json(cleaned[start : end + 1])

    @staticmethod
    def _parse_label_explanation(raw: str) -> VerdictPayload | None:
        """Lenient parse of the ``LABEL:`` / [``EVIDENCE:``] / ``EXPLANATION:``
        fallback format. The evidence line is optional (Gemini's fallback has
        none); when a model puts it AFTER the explanation it is trimmed off
        the explanation text.
        """
        label_match = _LABEL_LINE_RE.search(raw)
        explanation_match = _EXPLANATION_LINE_RE.search(raw)
        if label_match is None or explanation_match is None:
            return None
        evidence_match = _EVIDENCE_LINE_RE.search(raw)
        explanation_text = explanation_match.group(1)
        if (
            evidence_match is not None
            and evidence_match.start() > explanation_match.start()
        ):
            explanation_text = raw[explanation_match.start(1) : evidence_match.start()]
        explanation = " ".join(explanation_text.split()).strip()
        if not explanation:
            return None
        return VerdictPayload(
            label=label_match.group(1).upper(),  # type: ignore[arg-type]
            explanation=explanation,
            evidence=(
                evidence_match.group(1).lower()  # type: ignore[arg-type]
                if evidence_match is not None
                else None
            ),
        )

    @staticmethod
    def _enforce_invariants(
        payload: VerdictPayload, sources: list[Source]
    ) -> VerdictPayload:
        """HARD RULES, applied to every verdict before it leaves the checker.

        1. A non-UNVERIFIED verdict without citations is downgraded.
        2. A non-UNVERIFIED verdict whose own ``evidence`` rating is not
           ``strong`` is downgraded: a web search always returns results, so
           five topically-adjacent pages must not read as confirmation.
        3. The explanation is clamped to :data:`MAX_EXPLANATION_CHARS`.
        """
        label = payload.label
        explanation = payload.explanation.strip()
        if label != "UNVERIFIED" and not sources:
            logger.warning(
                "downgrading %s verdict to UNVERIFIED: no url_citation sources",
                label,
            )
            label = "UNVERIFIED"
            if NO_SOURCES_NOTE not in explanation:
                explanation = f"{explanation} {NO_SOURCES_NOTE}".strip()
        if label != "UNVERIFIED" and payload.evidence in ("partial", "none"):
            logger.warning(
                "downgrading %s verdict to UNVERIFIED: evidence rated %s",
                label,
                payload.evidence,
            )
            label = "UNVERIFIED"
            if WEAK_EVIDENCE_NOTE not in explanation:
                explanation = f"{explanation} {WEAK_EVIDENCE_NOTE}".strip()
        if len(explanation) > MAX_EXPLANATION_CHARS:
            explanation = explanation[: MAX_EXPLANATION_CHARS - 1].rstrip() + "…"
        return VerdictPayload(
            label=label, explanation=explanation, evidence=payload.evidence
        )
