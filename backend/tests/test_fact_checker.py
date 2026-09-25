"""FactChecker base-class behaviour: dedupe, hard invariants, the vision ladder.

Provider-neutral logic, exercised on the OpenRouter checker (the only verify
transport) or a scripted base-class harness. The OpenRouter transport's own
behaviour — strict schema, fallback chain, 429/402 cooldowns, timeouts,
citation extraction — has its own suite in ``test_llm_openrouter.py``.
"""

from typing import Any

import pytest

from app.fact_checker import (
    MAX_EXPLANATION_CHARS,
    FactChecker,
    QuotaExceededError,
    VerificationError,
)
from app.llm_openrouter import OpenRouterFactChecker
from app.models import Source, VerdictPayload
from app.rate_limit import QuotaCooldown
from tests.conftest import (
    TEST_VERIFY_MODEL,
    FakeLLMClient,
    make_verdict_completion,
)

CLAIM = "The Eiffel Tower is 450 meters tall."


@pytest.fixture()
def cooldown() -> QuotaCooldown:
    return QuotaCooldown()


@pytest.fixture()
def checker(
    fake_llm_client: FakeLLMClient, cooldown: QuotaCooldown
) -> OpenRouterFactChecker:
    return OpenRouterFactChecker(
        fake_llm_client,  # type: ignore[arg-type] — duck-typed fake
        verify_model=TEST_VERIFY_MODEL,
        cooldown=cooldown,
        verify_timeout_s=0.5,
    )


# --------------------------------------------------------------------------- #
# Dedupe
# --------------------------------------------------------------------------- #


class TestDedupe:
    def test_first_sighting_is_not_duplicate(self, checker: FactChecker) -> None:
        assert checker.is_duplicate(CLAIM) is False

    def test_exact_repeat_is_duplicate(self, checker: FactChecker) -> None:
        checker.is_duplicate(CLAIM)
        assert checker.is_duplicate(CLAIM) is True

    def test_paraphrase_is_duplicate(self, checker: FactChecker) -> None:
        checker.is_duplicate("The Eiffel Tower is 450 meters tall.")
        assert (
            checker.is_duplicate("chat, the eiffel tower is 450 meters tall!") is True
        )

    def test_word_order_and_punctuation_ignored(self, checker: FactChecker) -> None:
        checker.is_duplicate("Messi has won eight Ballon d'Or awards.")
        assert checker.is_duplicate("Eight Ballon d'Or awards Messi has won") is True

    def test_distinct_claim_is_not_duplicate(self, checker: FactChecker) -> None:
        checker.is_duplicate(CLAIM)
        assert checker.is_duplicate("The Great Wall is visible from space.") is False


# --------------------------------------------------------------------------- #
# Invariants
# --------------------------------------------------------------------------- #


class TestInvariants:
    def test_non_unverified_without_citations_is_downgraded(self) -> None:
        payload = FactChecker._enforce_invariants(
            VerdictPayload(label="FALSE", explanation="The tower is shorter."), []
        )
        assert payload.label == "UNVERIFIED"
        assert payload.explanation == (
            "The tower is shorter. No verifiable sources were retrieved."
        )

    def test_downgrade_note_not_duplicated(self) -> None:
        payload = FactChecker._enforce_invariants(
            VerdictPayload(
                label="TRUE",
                explanation="Sure. No verifiable sources were retrieved.",
            ),
            [],
        )
        assert payload.explanation.count("No verifiable sources") == 1

    def test_unverified_without_citations_is_untouched(self) -> None:
        payload = FactChecker._enforce_invariants(
            VerdictPayload(label="UNVERIFIED", explanation="Inconclusive."), []
        )
        assert payload.explanation == "Inconclusive."

    def test_labeled_verdict_with_citations_is_untouched(self) -> None:
        payload = FactChecker._enforce_invariants(
            VerdictPayload(label="TRUE", explanation="Confirmed."),
            [Source(url="https://example.com")],
        )
        assert payload.label == "TRUE"

    def test_long_explanation_is_clamped(self) -> None:
        payload = FactChecker._enforce_invariants(
            VerdictPayload(label="TRUE", explanation="x" * 600),
            [Source(url="https://example.com")],
        )
        assert len(payload.explanation) == MAX_EXPLANATION_CHARS
        assert payload.explanation.endswith("…")

    @pytest.mark.parametrize("evidence", ["partial", "none"])
    def test_weak_evidence_downgrades_even_with_citations(self, evidence: str) -> None:
        """Five topically-adjacent results are not confirmation."""
        payload = FactChecker._enforce_invariants(
            VerdictPayload(label="TRUE", explanation="Related.", evidence=evidence),
            [Source(url="https://example.com")],
        )
        assert payload.label == "UNVERIFIED"
        assert payload.evidence == evidence
        assert payload.explanation == (
            "Related. Retrieved sources did not directly address this claim."
        )

    def test_weak_evidence_note_not_duplicated(self) -> None:
        payload = FactChecker._enforce_invariants(
            VerdictPayload(
                label="FALSE",
                explanation="No. Retrieved sources did not directly address this claim.",
                evidence="none",
            ),
            [Source(url="https://example.com")],
        )
        assert payload.explanation.count("did not directly address") == 1

    def test_strong_or_absent_evidence_leaves_the_label(self) -> None:
        for evidence in ("strong", None):
            payload = FactChecker._enforce_invariants(
                VerdictPayload(
                    label="FALSE", explanation="Refuted.", evidence=evidence
                ),
                [Source(url="https://example.com")],
            )
            assert payload.label == "FALSE"
            assert payload.evidence == evidence

    def test_unverified_with_weak_evidence_gets_no_note(self) -> None:
        payload = FactChecker._enforce_invariants(
            VerdictPayload(label="UNVERIFIED", explanation="Unclear.", evidence="none"),
            [Source(url="https://example.com")],
        )
        assert payload.explanation == "Unclear."


class TestLabelExplanationParsing:
    def test_two_line_format_has_no_evidence(self) -> None:
        payload = FactChecker._parse_label_explanation(
            "LABEL: TRUE\nEXPLANATION: Confirmed by two sources."
        )
        assert payload is not None
        assert payload.label == "TRUE"
        assert payload.evidence is None
        assert payload.explanation == "Confirmed by two sources."

    def test_three_line_format_parses_evidence(self) -> None:
        payload = FactChecker._parse_label_explanation(
            "LABEL: **MISLEADING**\nEVIDENCE: partial\nEXPLANATION: Kernel of truth."
        )
        assert payload is not None
        assert payload.label == "MISLEADING"
        assert payload.evidence == "partial"
        assert payload.explanation == "Kernel of truth."

    def test_evidence_after_explanation_is_trimmed_off(self) -> None:
        payload = FactChecker._parse_label_explanation(
            "LABEL: FALSE\nEXPLANATION: The number is wrong.\nEVIDENCE: strong"
        )
        assert payload is not None
        assert payload.explanation == "The number is wrong."
        assert payload.evidence == "strong"

    async def test_downgrade_applies_end_to_end(
        self, checker: FactChecker, fake_llm_client: FakeLLMClient
    ) -> None:
        fake_llm_client.verify_results.append(
            make_verdict_completion("FALSE", "Refuted from memory.", citations=[])
        )
        verdict = await checker.check(CLAIM)
        assert verdict.label == "UNVERIFIED"
        assert "No verifiable sources were retrieved." in verdict.explanation
        assert verdict.sources == []


# --------------------------------------------------------------------------- #
# Vision failure ladder (provider-neutral, on the base class)
# --------------------------------------------------------------------------- #


class _ScriptedChecker(FactChecker):
    """Base-class harness: scripts _check_once outcomes per (claim, image)."""

    def __init__(self, cooldown: QuotaCooldown) -> None:
        super().__init__(cooldown=cooldown, verify_timeout_s=0.5)
        self.calls: list[str | None] = []  # the image_b64 seen per call
        self.script: list[Any] = []  # exception instance or VerdictPayload

    async def _grounded_structured(
        self, claim: str, image_b64: str | None = None
    ) -> tuple[VerdictPayload, list[Source]]:
        self.calls.append(image_b64)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item, [Source(url="https://example.com/s")]

    async def _grounded_fallback(
        self, claim: str, image_b64: str | None = None
    ) -> tuple[VerdictPayload, list[Source]]:
        raise AssertionError("fallback must not run in these tests")


class TestVisionLadder:
    async def test_image_success_is_one_call(self, cooldown: QuotaCooldown) -> None:
        checker = _ScriptedChecker(cooldown)
        checker.script = [VerdictPayload(label="TRUE", explanation="ok")]
        verdict = await checker.check(CLAIM, image_b64="anImage=")
        assert verdict.label == "TRUE"
        assert checker.calls == ["anImage="]

    async def test_image_failure_falls_through_to_text_ladder(
        self, cooldown: QuotaCooldown
    ) -> None:
        """HARD RULE: an image must never fail a check that would succeed
        without it."""
        checker = _ScriptedChecker(cooldown)
        checker.script = [
            VerificationError("vision model choked"),
            VerdictPayload(label="FALSE", explanation="text-only worked"),
        ]
        verdict = await checker.check(CLAIM, image_b64="anImage=")
        assert verdict.label == "FALSE"
        # First call carried the image; the retry deliberately did not.
        assert checker.calls == ["anImage=", None]

    async def test_quota_error_on_image_call_propagates_without_retry(
        self, cooldown: QuotaCooldown
    ) -> None:
        """The cooldown is already tripped; a retry would spend money to
        fail again."""
        checker = _ScriptedChecker(cooldown)
        checker.script = [QuotaExceededError("429")]
        with pytest.raises(QuotaExceededError):
            await checker.check(CLAIM, image_b64="anImage=")
        assert checker.calls == ["anImage="]

    async def test_no_image_keeps_the_old_ladder(self, cooldown: QuotaCooldown) -> None:
        checker = _ScriptedChecker(cooldown)
        checker.script = [VerdictPayload(label="TRUE", explanation="ok")]
        verdict = await checker.check(CLAIM)
        assert verdict.label == "TRUE"
        assert checker.calls == [None]
