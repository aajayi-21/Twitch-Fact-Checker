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
import time
from abc import ABC, abstractmethod

from app.models import GateClaim, TranscriptSegment

logger = logging.getLogger(__name__)


class GateError(Exception):
    """A gate pass failed (API error, timeout, or unparseable model output)."""


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
