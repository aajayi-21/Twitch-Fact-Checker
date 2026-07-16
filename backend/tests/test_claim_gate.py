"""ClaimGate throttling, batching, context-tail handling, and error paths.

The buffering/throttle behavior lives on the provider-neutral base class but
is exercised here through the Gemini transport (the OpenRouter transport has
its own suite in ``test_llm_openrouter.py``).
"""

import asyncio
import time
from typing import Any

import pytest

from app.claim_gate import ClaimGate, GateError
from app.llm_gemini import GeminiClaimGate
from app.models import TOPICS, GateResult, TranscriptSegment
from tests.conftest import (
    FakeGenAIClient,
    FakeGenerateContentResponse,
    make_gate_response,
)

REAL_INPUT_MARKER = "CONTEXT (reference resolution only):"


def make_segment(text: str) -> TranscriptSegment:
    return TranscriptSegment(
        text=text, start=0.0, end=1.0, avg_logprob=-0.3, no_speech_prob=0.05
    )


def make_gate(
    client: FakeGenAIClient,
    gate_interval_s: float = 100.0,
    gate_timeout_s: float = 5.0,
) -> GeminiClaimGate:
    return GeminiClaimGate(
        client,  # type: ignore[arg-type] — duck-typed fake
        model="fake-gate-model",
        gate_interval_s=gate_interval_s,
        gate_timeout_s=gate_timeout_s,
    )


def real_input_sections(prompt: str) -> tuple[str, str]:
    """Extract the (context, new_transcript) of the final real-input block.

    The marker also appears inside the prompt's few-shot examples, so only
    the LAST occurrence is the real input.
    """
    tail = prompt.rsplit(REAL_INPUT_MARKER, 1)[1]
    context_part, new_part = tail.split("NEW TRANSCRIPT:", 1)
    return context_part.strip(), new_part.strip()


EIGHT_WORDS = "the eiffel tower is four hundred fifty meters"


class TestShouldRun:
    def test_false_without_enough_new_words(self, fake_genai_client) -> None:
        gate = make_gate(fake_genai_client)
        gate.add_transcript(make_segment("only three words"))
        assert gate.should_run(time.monotonic()) is False

    def test_true_once_min_words_accumulate(self, fake_genai_client) -> None:
        gate = make_gate(fake_genai_client)
        gate.add_transcript(make_segment(EIGHT_WORDS))
        assert gate.should_run(time.monotonic()) is True

    def test_words_accumulate_across_segments(self, fake_genai_client) -> None:
        gate = make_gate(fake_genai_client)
        gate.add_transcript(make_segment("four words right here"))
        gate.add_transcript(make_segment("and four more words"))
        assert gate.should_run(time.monotonic()) is True

    async def test_interval_throttles_after_a_run(self, fake_genai_client) -> None:
        gate = make_gate(fake_genai_client, gate_interval_s=100.0)
        gate.add_transcript(make_segment(EIGHT_WORDS))
        fake_genai_client.generate_results.append(make_gate_response([]))
        await gate.run()
        gate.add_transcript(make_segment(EIGHT_WORDS))
        now = time.monotonic()
        assert gate.should_run(now) is False  # interval not elapsed
        assert gate.should_run(now + 200.0) is True

    def test_whitespace_segments_are_ignored(self, fake_genai_client) -> None:
        gate = make_gate(fake_genai_client)
        gate.add_transcript(make_segment("   "))
        assert gate.should_run(time.monotonic()) is False


class TestRun:
    async def test_empty_buffer_returns_without_llm_call(
        self, fake_genai_client
    ) -> None:
        gate = make_gate(fake_genai_client)
        assert await gate.run() == []
        assert fake_genai_client.generate_calls == []

    async def test_run_batches_all_pending_text(self, fake_genai_client) -> None:
        gate = make_gate(fake_genai_client)
        gate.add_transcript(make_segment("first chunk of words"))
        gate.add_transcript(make_segment("second chunk of words"))
        fake_genai_client.generate_results.append(
            make_gate_response([("The Earth is round.", 0.9)])
        )
        claims = await gate.run()
        assert [(c.claim_text, c.check_worthiness) for c in claims] == [
            ("The Earth is round.", 0.9)
        ]
        _context, new_text = real_input_sections(
            fake_genai_client.generate_calls[0]["contents"]
        )
        assert new_text == "first chunk of words second chunk of words"

    async def test_first_run_has_empty_context(self, fake_genai_client) -> None:
        gate = make_gate(fake_genai_client)
        gate.add_transcript(make_segment(EIGHT_WORDS))
        fake_genai_client.generate_results.append(make_gate_response([]))
        await gate.run()
        context, _ = real_input_sections(
            fake_genai_client.generate_calls[0]["contents"]
        )
        assert context == "(none)"

    async def test_context_tail_carries_previous_batch(self, fake_genai_client) -> None:
        gate = make_gate(fake_genai_client)
        gate.add_transcript(make_segment("the great wall of china came up"))
        fake_genai_client.generate_results.extend(
            [make_gate_response([]), make_gate_response([])]
        )
        await gate.run()
        gate.add_transcript(make_segment("and someone said you can see it from space"))
        await gate.run()
        context, new_text = real_input_sections(
            fake_genai_client.generate_calls[1]["contents"]
        )
        assert context == "the great wall of china came up"
        assert new_text == "and someone said you can see it from space"

    async def test_context_tail_capped_at_forty_words(self, fake_genai_client) -> None:
        gate = make_gate(fake_genai_client)
        words = [f"w{i}" for i in range(50)]
        gate.add_transcript(make_segment(" ".join(words)))
        fake_genai_client.generate_results.extend(
            [make_gate_response([]), make_gate_response([])]
        )
        await gate.run()
        gate.add_transcript(make_segment("fresh words for the second pass"))
        await gate.run()
        context, _ = real_input_sections(
            fake_genai_client.generate_calls[1]["contents"]
        )
        assert context.split() == words[-ClaimGate.CONTEXT_TAIL_WORDS :]

    async def test_failed_batch_is_dropped_not_retried(self, fake_genai_client) -> None:
        gate = make_gate(fake_genai_client)
        gate.add_transcript(make_segment(EIGHT_WORDS))
        fake_genai_client.generate_results.append(RuntimeError("api exploded"))
        with pytest.raises(GateError, match="gate call failed"):
            await gate.run()
        # Buffer was drained BEFORE the call: a second run has nothing to send.
        assert await gate.run() == []
        assert len(fake_genai_client.generate_calls) == 1

    async def test_timeout_raises_gate_error(self, fake_genai_client) -> None:
        gate = make_gate(fake_genai_client, gate_timeout_s=0.05)
        gate.add_transcript(make_segment(EIGHT_WORDS))

        async def slow_response(**_kwargs: Any) -> FakeGenerateContentResponse:
            await asyncio.sleep(0.5)
            return make_gate_response([])

        fake_genai_client.generate_results.append(slow_response)
        with pytest.raises(GateError, match="timed out"):
            await gate.run()


class TestExtractClaims:
    async def test_prefers_sdk_parsed_result(self, fake_genai_client) -> None:
        gate = make_gate(fake_genai_client)
        fake_genai_client.generate_results.append(
            make_gate_response([("Messi has won eight Ballon d'Or awards.", 0.8)])
        )
        claims = await gate.extract_claims("", "some transcript text")
        assert claims[0].claim_text == "Messi has won eight Ballon d'Or awards."

    async def test_falls_back_to_raw_text_json(self, fake_genai_client) -> None:
        gate = make_gate(fake_genai_client)
        raw = (
            '{"claims": [{"claim_text": "The sun is a star.", '
            '"check_worthiness": 0.6, "topic": "science_tech"}]}'
        )
        fake_genai_client.generate_results.append(
            FakeGenerateContentResponse(parsed=None, text=raw)
        )
        claims = await gate.extract_claims("", "some transcript text")
        assert claims[0].check_worthiness == 0.6
        assert claims[0].topic == "science_tech"

    async def test_missing_topic_in_raw_json_defaults_to_other(
        self, fake_genai_client
    ) -> None:
        gate = make_gate(fake_genai_client)
        raw = '{"claims": [{"claim_text": "The sun is a star.", "check_worthiness": 0.6}]}'
        fake_genai_client.generate_results.append(
            FakeGenerateContentResponse(parsed=None, text=raw)
        )
        claims = await gate.extract_claims("", "some transcript text")
        assert claims[0].topic == "other"

    async def test_unparseable_text_raises_gate_error(self, fake_genai_client) -> None:
        gate = make_gate(fake_genai_client)
        fake_genai_client.generate_results.append(
            FakeGenerateContentResponse(parsed=None, text="not json at all")
        )
        with pytest.raises(GateError, match="schema validation"):
            await gate.extract_claims("", "some transcript text")

    async def test_empty_response_raises_gate_error(self, fake_genai_client) -> None:
        gate = make_gate(fake_genai_client)
        fake_genai_client.generate_results.append(
            FakeGenerateContentResponse(parsed=None, text="")
        )
        with pytest.raises(GateError, match="no text"):
            await gate.extract_claims("", "some transcript text")

    async def test_structured_call_configuration(self, fake_genai_client) -> None:
        gate = make_gate(fake_genai_client)
        fake_genai_client.generate_results.append(make_gate_response([]))
        await gate.extract_claims("", "some transcript text")
        call = fake_genai_client.generate_calls[0]
        assert call["model"] == "fake-gate-model"
        config = call["config"]
        assert config.response_mime_type == "application/json"
        assert config.temperature == 0.0
        # response_schema is the Pydantic model itself, so the gate schema
        # gains the topic enum automatically from GateClaim.
        assert config.response_schema is GateResult
        gate_claim_schema = GateResult.model_json_schema()["$defs"]["GateClaim"]
        topic_schema = gate_claim_schema["properties"]["topic"]
        assert topic_schema["enum"] == list(TOPICS)
        # topic must be REQUIRED: Gemini's controlled generation may legally
        # omit any non-required property, silently dumping claims into the
        # unfilterable "other" bucket (mirrors the OpenRouter strict schema).
        assert "topic" in gate_claim_schema["required"]
