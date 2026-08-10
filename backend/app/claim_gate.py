"""Interval-throttled claim extraction ("the gate") — provider-neutral core.

The gate batches transcript text and sends *one* ungrounded structured call
per interval instead of one per chunk — claim detection needs no web search,
so it runs on the cheap gate model with temperature 0.

This module owns everything that is independent of the LLM provider:
transcript buffering, the interval/min-words throttle, the context tail used
for pronoun resolution, and the drain-before-call crash-safety dance. The
actual LLM transport is a single abstract method, :meth:`ClaimGate._extract`,
implemented by :class:`app.llm_gemini.GeminiClaimGate` and
:class:`app.llm_openrouter.OpenRouterClaimGate` so SDK drift stays local to
those modules.
"""

import logging
import re
import time
from abc import ABC, abstractmethod
from typing import Any

from pydantic import ValidationError

from app.models import TOPICS, ContradictionJudgement, GateClaim, GateResult, \
    TranscriptSegment

logger = logging.getLogger(__name__)

# Fence stripper for the tolerant JSON parses below (a copy of the pattern in
# app.fact_checker — deliberately not imported to keep this module
# dependency-light).
_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

# Strict gate schema, written out by hand (rather than generated from the
# Pydantic model) so it contains no $refs/titles/numeric-bound keywords that
# strict-mode validators are known to reject. Range enforcement for
# check_worthiness still happens in Pydantic when the response is parsed.
# Provider-neutral: shared by the OpenRouter and local (Ollama) transports.
GATE_JSON_SCHEMA: dict[str, Any] = {
    "name": "gate_result",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim_text": {"type": "string"},
                        "check_worthiness": {"type": "number"},
                        "topic": {"type": "string", "enum": list(TOPICS)},
                    },
                    "required": ["claim_text", "check_worthiness", "topic"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["claims"],
        "additionalProperties": False,
    },
}

# Flat strict schema for the contradiction judge (same doctrine as the
# verify schema: minimal fields, no nesting).
CONTRADICTION_JSON_SCHEMA: dict[str, Any] = {
    "name": "contradiction_judgement",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "contradicts": {"type": "boolean"},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "explanation": {"type": "string"},
        },
        "required": ["contradicts", "confidence", "explanation"],
        "additionalProperties": False,
    },
}


class GateError(Exception):
    """A gate pass failed (API error, timeout, or unparseable model output)."""


def _extract_json_object(raw: str, what: str) -> str:
    """The outermost ``{...}`` in a fence/prose-tolerant response body."""
    if not raw:
        raise GateError(f"{what} response contained no text to parse")
    cleaned = _JSON_FENCE_RE.sub("", raw).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise GateError(f"no JSON object found in {what} response: {cleaned[:120]!r}")
    return cleaned[start : end + 1]


def parse_gate_result(raw: str) -> list[GateClaim]:
    """Robust parse (fences/prose tolerated) into ``GateResult.claims``.

    Raises:
        GateError: on empty text, missing JSON object, or schema mismatch.
    """
    payload = _extract_json_object(raw, "gate")
    try:
        return GateResult.model_validate_json(payload).claims
    except ValidationError as exc:
        raise GateError(f"gate response failed schema validation: {exc}") from exc


def parse_contradiction_judgement(raw: str) -> ContradictionJudgement:
    """Robust parse into a :class:`ContradictionJudgement`.

    Raises:
        GateError: on empty text, missing JSON object, or schema mismatch.
    """
    payload = _extract_json_object(raw, "contradiction judge")
    try:
        return ContradictionJudgement.model_validate_json(payload)
    except ValidationError as exc:
        raise GateError(
            f"contradiction judgement failed schema validation: {exc}"
        ) from exc


class ClaimGate(ABC):
    """Accumulates transcript text and periodically extracts checkable claims."""

    MIN_NEW_WORDS = 8
    CONTEXT_TAIL_WORDS = 40

    def __init__(
        self,
        gate_interval_s: float = 12.0,
        gate_timeout_s: float = 15.0,
    ) -> None:
        self._gate_interval_s = gate_interval_s
        self._gate_timeout_s = gate_timeout_s
        self._pending_texts: list[str] = []
        self._pending_word_count = 0
        self._context_words: list[str] = []
        # -inf so the very first run only waits for MIN_NEW_WORDS.
        self._last_run_at = float("-inf")
        # Actual LLM gate calls made (the empty-buffer short-circuit in
        # run() does NOT count). Session-scoped for free — the gate is
        # built fresh per session; the pipeline reads it at session end.
        self.calls_made = 0

    def add_transcript(self, segment: TranscriptSegment) -> None:
        """Buffer one filtered transcript segment for the next gate pass."""
        text = segment.text.strip()
        if not text:
            return
        self._pending_texts.append(text)
        self._pending_word_count += len(text.split())

    def should_run(self, now: float) -> bool:
        """True when the interval elapsed AND enough new words accumulated."""
        return (
            now - self._last_run_at >= self._gate_interval_s
            and self._pending_word_count >= self.MIN_NEW_WORDS
        )

    async def run(self) -> list[GateClaim]:
        """Drain ALL accumulated text into one gate call (batching, not per-chunk).

        The buffer is drained and the context tail updated *before* the LLM
        call: on failure the batch is dropped — a missed gate is acceptable,
        a crash (or a retry storm) is not. Raises :class:`GateError` on API,
        timeout, or parse failure; the caller logs and moves on.
        """
        new_text = " ".join(self._pending_texts).strip()
        context = " ".join(self._context_words)
        self._pending_texts = []
        self._pending_word_count = 0
        self._last_run_at = time.monotonic()
        if not new_text:
            return []
        self._context_words = (self._context_words + new_text.split())[
            -self.CONTEXT_TAIL_WORDS :
        ]
        self.calls_made += 1
        return await self.extract_claims(context, new_text)

    async def extract_claims(self, context: str, new_text: str) -> list[GateClaim]:
        """One structured gate call over ``CONTEXT`` + ``NEW TRANSCRIPT``.

        Shared by :meth:`run` and the ``/debug/text`` path (which gates raw
        text without touching the session buffer). Raises :class:`GateError`
        on API, timeout, or parse failure.
        """
        return await self._extract(context, new_text)

    @abstractmethod
    async def _extract(self, context: str, new_text: str) -> list[GateClaim]:
        """Provider transport: run the gate prompt and parse the claims.

        Implementations must honor ``self._gate_timeout_s`` and raise
        :class:`GateError` for every failure mode (never a provider-SDK
        exception) so the batch is dropped and the session lives.
        """

    @abstractmethod
    async def judge_contradiction(
        self, current: str, prior: str
    ) -> ContradictionJudgement:
        """One structured judge call on the gate model (contradiction stage).

        Same failure contract as :meth:`_extract`: honor
        ``self._gate_timeout_s`` and raise :class:`GateError` for every
        failure mode — the detector logs and moves on, never crashes.
        """
