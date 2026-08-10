"""Same-session self-contradiction detection (report §4).

Structurally different from verification: grounded verify asks "is this true,
relative to the world"; this asks "is this consistent with what this speaker
already said" — a question web search cannot answer because the answer isn't
published anywhere. It is a first-party memory problem, and it is free of
the pipeline's dominant cost: no search call is needed to compare two pieces
of text we already have.

Pipeline per claim (all inside :meth:`ContradictionDetector.add`, called
serially from the session's contradiction loop):

1. Embed via Ollama ``/api/embed``; on the FIRST failure, log once and latch
   lexical mode (rapidfuzz token_set_ratio) for the session.
2. Retrieve top-k similar prior claims from the same session, EXCLUDING
   near-identical ones — a restatement is dedupe territory, not a
   contradiction.
3. Judge candidate pairs best-first on the gate model
   (:meth:`app.claim_gate.ClaimGate.judge_contradiction`), throttled.
4. Emit a :class:`ContradictionFrame` ONLY for ``contradicts`` with
   ``confidence == "high"`` — the doctrine mirrors the fact checker's
   UNVERIFIED-by-default: an eager contradiction detector is worse than
   none, so the default is NO flag.
"""

import logging
import time
from dataclasses import dataclass

import numpy as np
from rapidfuzz import fuzz

from app.claim_gate import ClaimGate, GateError
from app.embeddings import EmbeddingUnavailable, OllamaEmbedder
from app.fact_checker import normalize_claim
from app.models import ContradictionFrame, GateClaim, utc_now_iso

logger = logging.getLogger(__name__)

# Retrieval thresholds. Embedding cosine and lexical ratio bands differ in
# scale; both mark "same territory, worth a judge look".
COSINE_THRESHOLD = 0.55
LEXICAL_THRESHOLD = 70.0
# At/above this token_set_ratio the pair is a restatement (the fact
# checker's dedupe zone) — never a contradiction candidate.
NEAR_IDENTICAL_RATIO = 85.0
TOP_K = 3
# Modest self-throttle on judge calls (they ride the gate model; on hosted
# gates they share its rate budget).
MIN_JUDGE_INTERVAL_S = 2.0
MAX_STORED_CLAIMS = 500


@dataclass
class StoredClaim:
    text: str
    normalized: str
    vector: np.ndarray | None  # unit-normalized; None in lexical mode
    claimed_at: str  # ISO timestamp (wire value for prior_claimed_at)


class ContradictionDetector:
    """Per-session store + retrieve + judge. NOT thread-safe: single
    consumer (the session's contradiction loop) by design."""

    def __init__(self, gate: ClaimGate, embedder: OllamaEmbedder | None) -> None:
        self._gate = gate
        self._embedder = embedder
        self._lexical_mode = embedder is None
        self._store: list[StoredClaim] = []
        self._last_judge_at = float("-inf")

    async def add(self, claim: GateClaim) -> ContradictionFrame | None:
        """Process one gated claim; return a frame ONLY for high confidence.

        Never raises: judge/embedding failures are logged and swallowed —
        the claim is still stored so later claims can match against it.
        """
        vector = await self._embed_or_degrade(claim.claim_text)
        normalized = normalize_claim(claim.claim_text)
        candidates = self._retrieve_candidates(normalized, vector)
        frame = None
        if candidates:
            frame = await self._judge_candidates(claim.claim_text, candidates)
        # Store AFTER retrieval so a claim can never match itself.
        self._store.append(
            StoredClaim(
                text=claim.claim_text,
                normalized=normalized,
                vector=vector,
                claimed_at=utc_now_iso(),
            )
        )
        if len(self._store) > MAX_STORED_CLAIMS:
            del self._store[0]
        return frame

    async def _embed_or_degrade(self, text: str) -> np.ndarray | None:
        if self._lexical_mode:
            return None
        assert self._embedder is not None
        try:
            (vector,) = await self._embedder.embed([text])
            return vector
        except EmbeddingUnavailable as exc:
            logger.warning(
                "Ollama embeddings unavailable (%s); falling back to lexical "
                "matching for the rest of this session",
                exc,
            )
            self._lexical_mode = True
            return None

    def _retrieve_candidates(
        self, normalized: str, vector: np.ndarray | None
    ) -> list[StoredClaim]:
        """Top-k similar prior claims, near-identical restatements excluded."""
        scored: list[tuple[float, StoredClaim]] = []
        for stored in self._store:
            # Restatement zone: same claim reworded is dedupe territory.
            if fuzz.token_set_ratio(normalized, stored.normalized) >= (
                NEAR_IDENTICAL_RATIO
            ):
                continue
            if vector is not None and stored.vector is not None:
                score = float(np.dot(vector, stored.vector))
                if score > COSINE_THRESHOLD:
                    scored.append((score, stored))
            else:
                score = fuzz.token_set_ratio(normalized, stored.normalized)
                if score > LEXICAL_THRESHOLD:
                    scored.append((score, stored))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [stored for _score, stored in scored[:TOP_K]]

    async def _judge_candidates(
        self, current_text: str, candidates: list[StoredClaim]
    ) -> ContradictionFrame | None:
        """Judge best-first; stop at the first high-confidence contradiction.

        The throttle window is checked once per claim (not per candidate):
        if it hasn't elapsed, the whole batch is skipped — the claim still
        gets stored, so nothing is lost except this one look.
        """
        now = time.monotonic()
        if now - self._last_judge_at < MIN_JUDGE_INTERVAL_S:
            logger.debug("judge throttled; skipping %d candidates", len(candidates))
            return None
        self._last_judge_at = now
        for candidate in candidates:
            try:
                judgement = await self._gate.judge_contradiction(
                    current_text, candidate.text
                )
            except GateError as exc:
                logger.warning(
                    "contradiction judge failed (remaining candidates "
                    "skipped): %s",
                    exc,
                )
                return None
            if judgement.contradicts and judgement.confidence == "high":
                return ContradictionFrame(
                    current_claim=current_text,
                    prior_claim=candidate.text,
                    prior_claimed_at=candidate.claimed_at,
                    confidence=judgement.confidence,
                    explanation=judgement.explanation,
                )
        return None
