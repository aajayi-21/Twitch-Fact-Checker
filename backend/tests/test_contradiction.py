"""ContradictionDetector: retrieval, judging, degradation, doctrine.

The judge rides a real GeminiClaimGate around FakeGenAIClient (scripted via
``make_judgement_response``); embeddings come from the scripted FakeEmbedder.
"""

import numpy as np
import pytest

from app.contradiction import ContradictionDetector
from app.embeddings import EmbeddingUnavailable
from app.llm_gemini import GeminiClaimGate
from app.models import GateClaim
from tests.conftest import (
    FakeClassicAPIError,
    FakeEmbedder,
    FakeGenAIClient,
    make_judgement_response,
)

# Unit vectors: A/B are near-parallel (cosine ~0.98 > 0.55); C is orthogonal.
VEC_A = [np.asarray([1.0, 0.0], dtype=np.float32)]
VEC_B = [np.asarray([0.9848, 0.1736], dtype=np.float32)]
VEC_C = [np.asarray([0.0, 1.0], dtype=np.float32)]


def claim(text: str) -> GateClaim:
    return GateClaim(claim_text=text, check_worthiness=0.9, topic="other")


@pytest.fixture()
def fake_genai_client() -> FakeGenAIClient:
    return FakeGenAIClient()


@pytest.fixture()
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture()
def detector(
    fake_genai_client: FakeGenAIClient, embedder: FakeEmbedder
) -> ContradictionDetector:
    gate = GeminiClaimGate(
        client=fake_genai_client,  # type: ignore[arg-type] — duck-typed fake
        model="fake-gate-model",
        gate_timeout_s=5.0,
    )
    return ContradictionDetector(gate=gate, embedder=embedder)  # type: ignore[arg-type]


class TestRetrievalAndJudging:
    async def test_first_claim_stores_without_judging(
        self, detector: ContradictionDetector, embedder: FakeEmbedder
    ) -> None:
        embedder.results.append(VEC_A)
        frame = await detector.add(claim("I have never been to Paris."))
        assert frame is None
        assert embedder.calls == [["I have never been to Paris."]]

    async def test_similar_pair_high_confidence_emits_frame(
        self,
        detector: ContradictionDetector,
        embedder: FakeEmbedder,
        fake_genai_client: FakeGenAIClient,
    ) -> None:
        embedder.results.append(VEC_A)
        await detector.add(claim("I have never visited France."))
        embedder.results.append(VEC_B)
        fake_genai_client.generate_results.append(
            make_judgement_response(True, "high", "Never-visited vs lived there.")
        )
        frame = await detector.add(claim("I lived in Lyon for two years."))
        assert frame is not None
        assert frame.type == "contradiction"
        assert frame.current_claim == "I lived in Lyon for two years."
        assert frame.prior_claim == "I have never visited France."
        assert frame.confidence == "high"
        assert frame.prior_claimed_at.endswith("Z")

    async def test_dissimilar_claims_never_reach_the_judge(
        self, detector: ContradictionDetector, embedder: FakeEmbedder
    ) -> None:
        embedder.results.append(VEC_A)
        await detector.add(claim("I have never visited France."))
        embedder.results.append(VEC_C)
        # No judgement scripted: a judge call would fail loudly.
        frame = await detector.add(claim("The moon landing was in 1969."))
        assert frame is None

    @pytest.mark.parametrize("confidence", ["low", "medium"])
    async def test_below_high_confidence_never_emits(
        self,
        detector: ContradictionDetector,
        embedder: FakeEmbedder,
        fake_genai_client: FakeGenAIClient,
        confidence: str,
    ) -> None:
        embedder.results.append(VEC_A)
        await detector.add(claim("I have never visited France."))
        embedder.results.append(VEC_B)
        fake_genai_client.generate_results.append(
            make_judgement_response(True, confidence)
        )
        frame = await detector.add(claim("I lived in Lyon for two years."))
        assert frame is None

    async def test_contradicts_false_never_emits(
        self,
        detector: ContradictionDetector,
        embedder: FakeEmbedder,
        fake_genai_client: FakeGenAIClient,
    ) -> None:
        embedder.results.append(VEC_A)
        await detector.add(claim("I have never visited France."))
        embedder.results.append(VEC_B)
        fake_genai_client.generate_results.append(
            make_judgement_response(False, "high")
        )
        frame = await detector.add(claim("I lived in Lyon for two years."))
        assert frame is None

    async def test_near_identical_restatement_skips_the_judge(
        self, detector: ContradictionDetector, embedder: FakeEmbedder
    ) -> None:
        """token_set_ratio >= 85 is dedupe territory, not a contradiction."""
        embedder.results.append(VEC_A)
        await detector.add(claim("The Eiffel Tower is 330 meters tall."))
        embedder.results.append(VEC_B)
        # No judgement scripted: reaching the judge would fail loudly.
        frame = await detector.add(
            claim("The Eiffel Tower is 330 meters tall today.")
        )
        assert frame is None

    async def test_judge_error_returns_none_but_still_stores(
        self,
        detector: ContradictionDetector,
        embedder: FakeEmbedder,
        fake_genai_client: FakeGenAIClient,
    ) -> None:
        embedder.results.append(VEC_A)
        await detector.add(claim("I have never visited France."))
        embedder.results.append(VEC_B)
        fake_genai_client.generate_results.append(
            FakeClassicAPIError(500, "judge exploded")
        )
        frame = await detector.add(claim("I lived in Lyon for two years."))
        assert frame is None
        # The failed claim was still stored (visible to later claims).
        assert len(detector._store) == 2

    async def test_judge_throttle_skips_the_batch(
        self,
        detector: ContradictionDetector,
        embedder: FakeEmbedder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import time as time_module

        embedder.results.append(VEC_A)
        await detector.add(claim("I have never visited France."))
        # Simulate a judge call having JUST happened.
        monkeypatch.setattr(
            detector, "_last_judge_at", time_module.monotonic(), raising=False
        )
        embedder.results.append(VEC_B)
        # No judgement scripted: a judge call would fail loudly.
        frame = await detector.add(claim("I lived in Lyon for two years."))
        assert frame is None


class TestDegradation:
    async def test_embedding_failure_latches_lexical_mode(
        self,
        detector: ContradictionDetector,
        embedder: FakeEmbedder,
        fake_genai_client: FakeGenAIClient,
    ) -> None:
        embedder.results.append(EmbeddingUnavailable("no server"))
        await detector.add(claim("I have never been to Japan."))
        # Lexical mode latched: no further embed attempts...
        fake_genai_client.generate_results.append(
            make_judgement_response(True, "high", "Never vs twice.")
        )
        # Direct negation scores ~87 on token_set_ratio: inside the lexical
        # candidate band (70, 95) — the negation-blind exclusion must NOT
        # swallow it (regression guard for NEAR_IDENTICAL_RATIO).
        frame = await detector.add(claim("I have been to Japan twice."))
        assert embedder.calls == [["I have never been to Japan."]]
        # ...and the lexical band still finds + judges the candidate.
        assert frame is not None
        assert frame.prior_claim == "I have never been to Japan."

    async def test_none_embedder_starts_lexical(
        self, fake_genai_client: FakeGenAIClient
    ) -> None:
        gate = GeminiClaimGate(
            client=fake_genai_client,  # type: ignore[arg-type]
            model="fake-gate-model",
        )
        detector = ContradictionDetector(gate=gate, embedder=None)
        frame = await detector.add(claim("Anything at all works here."))
        assert frame is None


class TestStoreCap:
    async def test_oldest_claim_evicted_past_cap(
        self,
        detector: ContradictionDetector,
        embedder: FakeEmbedder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("app.contradiction.MAX_STORED_CLAIMS", 2)
        for text in (
            "Claim number one about apples.",
            "Claim number two about bridges.",
            "Claim number three about comets.",
        ):
            embedder.results.append(VEC_C)  # orthogonal: no candidates
            await detector.add(claim(text))
        stored_texts = [stored.text for stored in detector._store]
        assert stored_texts == [
            "Claim number two about bridges.",
            "Claim number three about comets.",
        ]
