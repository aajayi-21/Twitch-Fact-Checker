"""FactChecker failure ladder, citations, dedupe, and hard invariants.

Covers the plan-§8 matrix on the GEMINI transport (the OpenRouter transport
has its own suite in ``test_llm_openrouter.py``): 400 -> grounded fallback,
429 -> cooldown trip, malformed output -> ungrounded extraction ->
VerificationError, zero citations -> UNVERIFIED downgrade, and rapidfuzz
paraphrase dedupe (provider-neutral, on the base class).
"""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.fact_checker import (
    FLAT_VERDICT_SCHEMA,
    MAX_EXPLANATION_CHARS,
    FactChecker,
    QuotaExceededError,
    VerificationError,
)
from app.llm_gemini import (
    GeminiFactChecker,
    _error_status_code,
    _parse_retry_after_s,
)
from app.models import Source, VerdictPayload
from app.rate_limit import QuotaCooldown
from tests.conftest import (
    FakeClassicAPIError,
    FakeGenAIClient,
    FakeGenerateContentResponse,
    FakeInteractionsError,
    make_interaction,
    make_verdict_interaction,
)

CLAIM = "The Eiffel Tower is 450 meters tall."


@pytest.fixture()
def cooldown() -> QuotaCooldown:
    return QuotaCooldown()


@pytest.fixture()
def checker(
    fake_genai_client: FakeGenAIClient, cooldown: QuotaCooldown
) -> GeminiFactChecker:
    return GeminiFactChecker(
        fake_genai_client,  # type: ignore[arg-type] — duck-typed fake
        verify_model="fake-verify-model",
        extraction_model="fake-extract-model",
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
# Happy path (grounded structured)
# --------------------------------------------------------------------------- #


class TestGroundedStructured:
    async def test_verdict_assembled_from_json_and_citations(
        self, checker: FactChecker, fake_genai_client: FakeGenAIClient
    ) -> None:
        fake_genai_client.interaction_results.append(
            make_verdict_interaction(
                "FALSE",
                "The Eiffel Tower is about 330 meters tall.",
                citations=[("https://www.toureiffel.paris/x", "Key figures")],
            )
        )
        verdict = await checker.check(CLAIM)
        assert verdict.claim == CLAIM
        assert verdict.label == "FALSE"
        assert verdict.used_fallback is False
        assert verdict.sources == [
            Source(url="https://www.toureiffel.paris/x", title="Key figures")
        ]

    async def test_call_shape_matches_plan(
        self, checker: FactChecker, fake_genai_client: FakeGenAIClient
    ) -> None:
        fake_genai_client.interaction_results.append(
            make_verdict_interaction("TRUE", "Confirmed.")
        )
        await checker.check(CLAIM)
        call = fake_genai_client.interaction_calls[0]
        assert call["model"] == "fake-verify-model"
        assert call["tools"] == [{"type": "google_search"}]
        assert call["response_format"] == {
            "type": "text",
            "mime_type": "application/json",
            "schema": FLAT_VERDICT_SCHEMA,
        }
        assert call["generation_config"] == {"temperature": 0.0}
        assert CLAIM in call["input"]
        assert "Today is" in call["input"]

    async def test_fenced_json_is_tolerated(
        self, checker: FactChecker, fake_genai_client: FakeGenAIClient
    ) -> None:
        fenced = '```json\n{"label": "TRUE", "explanation": "Confirmed."}\n```'
        fake_genai_client.interaction_results.append(
            make_interaction(fenced, [("https://example.com/a", "A")])
        )
        verdict = await checker.check(CLAIM)
        assert verdict.label == "TRUE"
        assert verdict.used_fallback is False


# --------------------------------------------------------------------------- #
# Failure ladder
# --------------------------------------------------------------------------- #


class TestFallbackChain:
    async def test_400_falls_back_to_label_explanation_format(
        self, checker: FactChecker, fake_genai_client: FakeGenAIClient
    ) -> None:
        fake_genai_client.interaction_results.append(FakeInteractionsError(400))
        fake_genai_client.interaction_results.append(
            make_interaction(
                "LABEL: FALSE\nEXPLANATION: The tower is about 330 meters tall.",
                [("https://example.com/tower", "Tower")],
            )
        )
        verdict = await checker.check(CLAIM)
        assert verdict.label == "FALSE"
        assert verdict.explanation == "The tower is about 330 meters tall."
        assert verdict.used_fallback is True
        assert verdict.sources[0].url == "https://example.com/tower"
        # Fallback call is grounded but must NOT request structured output.
        assert fake_genai_client.interaction_calls[1]["response_format"] is None

    async def test_unparseable_structured_output_falls_back(
        self, checker: FactChecker, fake_genai_client: FakeGenAIClient
    ) -> None:
        fake_genai_client.interaction_results.append(
            make_interaction("I could not decide.", [("https://example.com/a", None)])
        )
        fake_genai_client.interaction_results.append(
            make_interaction(
                "LABEL: UNVERIFIED\nEXPLANATION: Sources were inconclusive.",
                [("https://example.com/b", "B")],
            )
        )
        verdict = await checker.check(CLAIM)
        assert verdict.label == "UNVERIFIED"
        assert verdict.used_fallback is True

    async def test_last_resort_ungrounded_extraction(
        self,
        checker: FactChecker,
        fake_genai_client: FakeGenAIClient,
    ) -> None:
        fake_genai_client.interaction_results.append(FakeInteractionsError(400))
        fake_genai_client.interaction_results.append(
            make_interaction(
                "After searching, this appears to be false; the tower is 330 m.",
                [("https://example.com/grounded", "Grounded")],
            )
        )
        fake_genai_client.generate_results.append(
            FakeGenerateContentResponse(
                parsed=VerdictPayload(
                    label="FALSE", explanation="The tower is 330 meters tall."
                )
            )
        )
        verdict = await checker.check(CLAIM)
        assert verdict.label == "FALSE"
        assert verdict.used_fallback is True
        # Citations still come from the grounded step, not the extraction.
        assert verdict.sources[0].url == "https://example.com/grounded"
        extraction_call = fake_genai_client.generate_calls[0]
        assert extraction_call["model"] == "fake-extract-model"

    async def test_exhausted_chain_raises_verification_error(
        self, checker: FactChecker, fake_genai_client: FakeGenAIClient
    ) -> None:
        fake_genai_client.interaction_results.append(FakeInteractionsError(400))
        fake_genai_client.interaction_results.append(
            make_interaction("free-form waffle with no label")
        )
        fake_genai_client.generate_results.append(
            FakeGenerateContentResponse(parsed=None, text="still not json")
        )
        with pytest.raises(VerificationError, match="fallback chain"):
            await checker.check(CLAIM)

    async def test_empty_fallback_text_raises(
        self, checker: FactChecker, fake_genai_client: FakeGenAIClient
    ) -> None:
        fake_genai_client.interaction_results.append(FakeInteractionsError(400))
        fake_genai_client.interaction_results.append(make_interaction(""))
        with pytest.raises(VerificationError, match="no text"):
            await checker.check(CLAIM)

    async def test_non_400_error_does_not_fall_back(
        self, checker: FactChecker, fake_genai_client: FakeGenAIClient
    ) -> None:
        fake_genai_client.interaction_results.append(FakeInteractionsError(500))
        with pytest.raises(VerificationError, match="grounded structured"):
            await checker.check(CLAIM)
        assert len(fake_genai_client.interaction_calls) == 1

    async def test_400_during_fallback_is_fatal(
        self, checker: FactChecker, fake_genai_client: FakeGenAIClient
    ) -> None:
        fake_genai_client.interaction_results.append(FakeInteractionsError(400))
        fake_genai_client.interaction_results.append(FakeInteractionsError(400))
        with pytest.raises(VerificationError, match="grounded fallback"):
            await checker.check(CLAIM)


class TestQuota:
    async def test_429_trips_cooldown_with_parsed_retry_after(
        self,
        checker: FactChecker,
        fake_genai_client: FakeGenAIClient,
        cooldown: QuotaCooldown,
    ) -> None:
        fake_genai_client.interaction_results.append(
            FakeInteractionsError(
                429, response=SimpleNamespace(headers={"retry-after": "38"})
            )
        )
        with pytest.raises(QuotaExceededError, match="429"):
            await checker.check(CLAIM)
        assert cooldown.active is True
        assert 30.0 < cooldown.remaining_s <= 38.0
        assert len(fake_genai_client.interaction_calls) == 1  # no fallback attempt

    async def test_429_retry_delay_parsed_from_body(
        self,
        checker: FactChecker,
        fake_genai_client: FakeGenAIClient,
        cooldown: QuotaCooldown,
    ) -> None:
        fake_genai_client.interaction_results.append(
            FakeInteractionsError(
                429, body='{"error": {"details": [{"retryDelay": "17s"}]}}'
            )
        )
        with pytest.raises(QuotaExceededError):
            await checker.check(CLAIM)
        assert 10.0 < cooldown.remaining_s <= 17.0

    async def test_classic_api_429_during_extraction_trips_cooldown(
        self,
        checker: FactChecker,
        fake_genai_client: FakeGenAIClient,
        cooldown: QuotaCooldown,
    ) -> None:
        fake_genai_client.interaction_results.append(FakeInteractionsError(400))
        fake_genai_client.interaction_results.append(
            make_interaction("free-form waffle with no label")
        )
        fake_genai_client.generate_results.append(FakeClassicAPIError(429))
        with pytest.raises(QuotaExceededError):
            await checker.check(CLAIM)
        assert cooldown.active is True


class TestTimeouts:
    async def test_timeout_retries_once_then_succeeds(
        self, checker: FactChecker, fake_genai_client: FakeGenAIClient
    ) -> None:
        async def slow_interaction(**_kwargs: Any) -> Any:
            await asyncio.sleep(2.0)  # > verify_timeout_s=0.5
            return make_verdict_interaction("TRUE", "too late")

        fake_genai_client.interaction_results.append(slow_interaction)
        fake_genai_client.interaction_results.append(
            make_verdict_interaction("TRUE", "Confirmed on retry.")
        )
        verdict = await checker.check(CLAIM)
        assert verdict.explanation == "Confirmed on retry."
        assert verdict.used_fallback is False

    async def test_two_timeouts_raise_verification_error(
        self, checker: FactChecker, fake_genai_client: FakeGenAIClient
    ) -> None:
        async def slow_interaction(**_kwargs: Any) -> Any:
            await asyncio.sleep(2.0)
            return make_verdict_interaction("TRUE", "too late")

        fake_genai_client.interaction_results.append(slow_interaction)
        fake_genai_client.interaction_results.append(slow_interaction)
        with pytest.raises(VerificationError, match="timed out twice"):
            await checker.check(CLAIM)


# --------------------------------------------------------------------------- #
# Citations
# --------------------------------------------------------------------------- #


class TestCitations:
    def test_dedupe_by_url_and_cap_at_five(self) -> None:
        citations = [(f"https://example.com/{i}", f"T{i}") for i in range(6)]
        citations.insert(1, ("https://example.com/0", "dup of first"))
        interaction = make_interaction("{}", citations)
        sources = GeminiFactChecker._extract_citations(interaction)
        assert len(sources) == 5
        assert [s.url for s in sources] == [
            f"https://example.com/{i}" for i in range(5)
        ]

    def test_only_model_output_steps_and_url_citations_count(self) -> None:
        interaction = SimpleNamespace(
            steps=[
                SimpleNamespace(  # wrong step type -> skipped entirely
                    type="google_search_call",
                    content=[
                        SimpleNamespace(
                            annotations=[
                                SimpleNamespace(
                                    type="url_citation",
                                    url="https://example.com/skipped",
                                    title=None,
                                )
                            ]
                        )
                    ],
                ),
                SimpleNamespace(
                    type="model_output",
                    content=[
                        SimpleNamespace(
                            annotations=[
                                SimpleNamespace(
                                    type="file_citation",
                                    url="https://example.com/wrong-type",
                                    title=None,
                                ),
                                SimpleNamespace(
                                    type="url_citation",
                                    url="https://example.com/kept",
                                    title="Kept",
                                ),
                            ]
                        ),
                        SimpleNamespace(annotations=None),  # no annotations
                    ],
                ),
            ]
        )
        sources = GeminiFactChecker._extract_citations(interaction)
        assert sources == [Source(url="https://example.com/kept", title="Kept")]

    def test_no_steps_yields_no_sources(self) -> None:
        assert GeminiFactChecker._extract_citations(SimpleNamespace(steps=None)) == []


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

    async def test_downgrade_applies_end_to_end(
        self, checker: FactChecker, fake_genai_client: FakeGenAIClient
    ) -> None:
        fake_genai_client.interaction_results.append(
            make_verdict_interaction("FALSE", "Refuted from memory.", citations=[])
        )
        verdict = await checker.check(CLAIM)
        assert verdict.label == "UNVERIFIED"
        assert "No verifiable sources were retrieved." in verdict.explanation
        assert verdict.sources == []


# --------------------------------------------------------------------------- #
# Error translation helpers
# --------------------------------------------------------------------------- #


class TestErrorHelpers:
    def test_status_code_families(self) -> None:
        assert _error_status_code(FakeInteractionsError(404)) == 404
        assert _error_status_code(FakeClassicAPIError(429)) == 429
        assert _error_status_code(ValueError("nope")) is None

    def test_non_int_code_is_ignored(self) -> None:
        error = ValueError("nope")
        error.code = "429"  # type: ignore[attr-defined]
        assert _error_status_code(error) is None

    def test_retry_after_header_wins(self) -> None:
        error = FakeInteractionsError(
            429,
            message="retryDelay: 99s",
            response=SimpleNamespace(headers={"retry-after": "12"}),
        )
        assert _parse_retry_after_s(error) == 12.0

    def test_retry_after_from_message(self) -> None:
        error = FakeInteractionsError(429, message='"retryDelay": "7s"')
        assert _parse_retry_after_s(error) == 7.0

    def test_retry_after_defaults_to_sixty(self) -> None:
        assert _parse_retry_after_s(FakeInteractionsError(429)) == 60.0


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
