"""The Anthropic transport (app/llm_anthropic.py), fully offline.

Mirrors ``tests/test_llm_openrouter.py``: a scripted
:class:`tests.conftest.FakeAnthropicClient` stands in for ``AsyncAnthropic``,
responses are REAL ``anthropic.types.Message`` objects and errors REAL SDK
exceptions, so the request shapes, the content-block parsing and every
``except`` clause are exercised against what the SDK actually produces.
"""

import asyncio
from typing import Any

import pytest

import anthropic
from app.claim_gate import GATE_JSON_SCHEMA, GateError
from app.fact_checker import (
    NO_SOURCES_NOTE,
    QuotaExceededError,
    VerificationError,
)
from app.llm_anthropic import (
    ANTHROPIC_VERDICT_SCHEMA,
    CREDITS_EXHAUSTED_COOLDOWN_S,
    MAX_PAUSE_CONTINUATIONS,
    AnthropicClaimGate,
    AnthropicFactChecker,
    final_text,
    parse_structured_verdict,
    select_sources,
    thinking_params,
)
from app.prompts import build_gate_prompt
from app.rate_limit import QuotaCooldown
from tests.conftest import (
    FakeAnthropicClient,
    anthropic_search_blocks,
    anthropic_text_block,
    make_anthropic_message,
    make_anthropic_status_error,
)

CLAIM = "The Eiffel Tower is 450 meters tall."
TOUR = ("https://www.toureiffel.paris/en/the-monument", "The Eiffel Tower")
BRIT = ("https://www.britannica.com/topic/Eiffel-Tower", "Eiffel Tower | Britannica")


def make_gate(client: FakeAnthropicClient, **kwargs: Any) -> AnthropicClaimGate:
    return AnthropicClaimGate(
        client=client,  # type: ignore[arg-type] — duck-typed fake
        model="claude-gate-test",
        gate_interval_s=12.0,
        gate_timeout_s=15.0,
        **kwargs,
    )


def make_checker(
    client: FakeAnthropicClient, cooldown: QuotaCooldown | None = None, **kwargs: Any
) -> AnthropicFactChecker:
    return AnthropicFactChecker(
        client=client,  # type: ignore[arg-type] — duck-typed fake
        verify_model="claude-verify-test",
        cooldown=cooldown or QuotaCooldown(),
        verify_timeout_s=45.0,
        **kwargs,
    )


def verdict_json(
    label: str = "FALSE",
    explanation: str = "The tower is about 330 metres tall, not 450.",
    evidence: str = "strong",
    source_urls: tuple[str, ...] = (TOUR[0],),
) -> str:
    import json

    return json.dumps(
        {
            "evidence": evidence,
            "explanation": explanation,
            "label": label,
            "source_urls": list(source_urls),
        }
    )


def grounded_message(
    text: str,
    results: tuple[tuple[str, str], ...] = (TOUR, BRIT),
    citations: tuple[tuple[str, str], ...] = (),
    stop_reason: str = "end_turn",
) -> Any:
    return make_anthropic_message(
        [
            anthropic_text_block("I'll search for the tower's height."),
            *anthropic_search_blocks(results),
            anthropic_text_block(text, citations),
        ],
        stop_reason=stop_reason,
        web_search_requests=1,
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


class TestThinkingParams:
    def test_none_disables_thinking_at_low_effort(self) -> None:
        assert thinking_params("none") == ({"type": "disabled"}, "low")

    def test_empty_sends_nothing(self) -> None:
        assert thinking_params("") == (None, None)

    @pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "max"])
    def test_levels_are_adaptive(self, level: str) -> None:
        assert thinking_params(level) == ({"type": "adaptive"}, level)


class TestFinalText:
    def test_takes_prose_after_the_last_tool_block(self) -> None:
        message = grounded_message('{"label": "TRUE"}')
        assert final_text(message.content) == '{"label": "TRUE"}'

    def test_joins_split_text_blocks_and_skips_thinking(self) -> None:
        message = make_anthropic_message(
            [
                {"type": "thinking", "thinking": "", "signature": "sig"},
                anthropic_text_block("LABEL: TRUE\n"),
                anthropic_text_block("EXPLANATION: confirmed."),
            ]
        )
        assert final_text(message.content) == "LABEL: TRUE\nEXPLANATION: confirmed."


class TestSelectSources:
    def test_model_picks_that_search_returned_win(self) -> None:
        content = grounded_message("x").content
        sources = select_sources(content, [BRIT[0]])
        assert [(s.url, s.title) for s in sources] == [BRIT]

    def test_fabricated_pick_is_dropped_and_falls_back_to_results(self) -> None:
        content = grounded_message("x").content
        sources = select_sources(content, ["https://made-up.example/page"])
        assert [s.url for s in sources] == [TOUR[0], BRIT[0]]

    def test_trailing_slash_still_matches(self) -> None:
        content = grounded_message("x").content
        sources = select_sources(content, [TOUR[0] + "/"])
        assert [s.url for s in sources] == [TOUR[0]]

    def test_citations_beat_uncited_results(self) -> None:
        content = grounded_message("x", citations=(BRIT,)).content
        assert [s.url for s in select_sources(content)] == [BRIT[0]]

    def test_no_search_means_no_sources(self) -> None:
        content = make_anthropic_message([anthropic_text_block("{}")]).content
        assert select_sources(content, [TOUR[0]]) == []

    def test_search_error_object_is_ignored(self) -> None:
        content = make_anthropic_message(
            [
                {
                    "type": "server_tool_use",
                    "id": "srvtoolu_9",
                    "name": "web_search",
                    "input": {"query": "q"},
                },
                {
                    "type": "web_search_tool_result",
                    "tool_use_id": "srvtoolu_9",
                    "content": {
                        "type": "web_search_tool_result_error",
                        "error_code": "max_uses_exceeded",
                    },
                },
                anthropic_text_block("{}"),
            ]
        ).content
        assert select_sources(content) == []

    def test_capped_and_deduplicated(self) -> None:
        results = tuple((f"https://s{i}.example/", f"S{i}") for i in range(8))
        content = grounded_message("x", results=results + results[:2]).content
        sources = select_sources(content)
        assert len(sources) == 5
        assert len({s.url for s in sources}) == 5


class TestParseStructuredVerdict:
    def test_splits_payload_and_urls(self) -> None:
        payload, urls = parse_structured_verdict(verdict_json())
        assert payload.label == "FALSE"
        assert payload.evidence == "strong"
        assert urls == [TOUR[0]]

    def test_rejects_non_json(self) -> None:
        with pytest.raises(ValueError):
            parse_structured_verdict("I could not decide.")


# --------------------------------------------------------------------------- #
# Gate
# --------------------------------------------------------------------------- #


def gate_message(text: str, stop_reason: str = "end_turn") -> Any:
    return make_anthropic_message([anthropic_text_block(text)], stop_reason=stop_reason)


GATE_JSON = (
    '{"claims": [{"claim_text": "The Eiffel Tower is 450 meters tall.", '
    '"check_worthiness": 0.9, "topic": "science_tech"}]}'
)


class TestGateRequestShape:
    async def test_strict_schema_cached_instructions_thinking_off(self) -> None:
        client = FakeAnthropicClient()
        client.message_results.append(gate_message(GATE_JSON))
        claims = await make_gate(client).extract_claims("earlier words", "new words")

        assert [c.claim_text for c in claims] == [
            "The Eiffel Tower is 450 meters tall."
        ]
        (call,) = client.message_calls
        assert call["model"] == "claude-gate-test"
        assert "temperature" not in call  # Haiku 5.5 rejects non-default values
        assert call["thinking"] == {"type": "disabled"}
        assert call["output_config"] == {
            "effort": "low",
            "format": {"type": "json_schema", "schema": GATE_JSON_SCHEMA["schema"]},
        }
        (system_block,) = call["system"]
        assert system_block["cache_control"] == {"type": "ephemeral"}
        user_text = call["messages"][0]["content"]
        # The two parts together are exactly the shared gate prompt.
        assert system_block["text"] + user_text == build_gate_prompt(
            "earlier words", "new words"
        )
        assert "NEW TRANSCRIPT: new words" in user_text
        assert client.with_options_calls[0]["timeout"].read == 15.0

    async def test_configured_effort_turns_thinking_on(self) -> None:
        client = FakeAnthropicClient()
        client.message_results.append(gate_message(GATE_JSON))
        await make_gate(client, reasoning_effort="medium").extract_claims(
            "", "new words"
        )
        call = client.message_calls[0]
        assert call["thinking"] == {"type": "adaptive"}
        assert call["output_config"]["effort"] == "medium"


class TestGateFailures:
    async def test_refusal_is_gate_error(self) -> None:
        client = FakeAnthropicClient()
        client.message_results.append(gate_message("", stop_reason="refusal"))
        with pytest.raises(GateError, match="refusal"):
            await make_gate(client).extract_claims("", "new words")

    async def test_max_tokens_is_gate_error(self) -> None:
        client = FakeAnthropicClient()
        client.message_results.append(
            gate_message('{"claims": [', stop_reason="max_tokens")
        )
        with pytest.raises(GateError, match="cap"):
            await make_gate(client).extract_claims("", "new words")

    @pytest.mark.parametrize(
        ("error", "match"),
        [
            (
                make_anthropic_status_error(401, "bad key", "authentication_error"),
                "ant auth login",
            ),
            (make_anthropic_status_error(402, "no money", "billing_error"), "credit"),
            (
                make_anthropic_status_error(
                    400, "Your credit balance is too low", "invalid_request_error"
                ),
                "credit",
            ),
            (make_anthropic_status_error(429, "slow down", "rate_limit_error"), "429"),
            (
                make_anthropic_status_error(529, "busy", "overloaded_error"),
                "overloaded",
            ),
        ],
    )
    async def test_status_errors_become_gate_errors(
        self, error: Exception, match: str
    ) -> None:
        client = FakeAnthropicClient()
        client.message_results.append(error)
        with pytest.raises(GateError, match=match):
            await make_gate(client).extract_claims("", "new words")
        # A credit 400 is not a schema problem: no schema-less retry.
        assert len(client.message_calls) == 1

    async def test_timeout_and_connection_errors(self) -> None:
        import httpx2

        request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        client = FakeAnthropicClient()
        client.message_results.append(anthropic.APITimeoutError(request=request))
        with pytest.raises(GateError, match="timed out"):
            await make_gate(client).extract_claims("", "new words")
        client.message_results.append(anthropic.APIConnectionError(request=request))
        with pytest.raises(GateError, match="could not reach"):
            await make_gate(client).extract_claims("", "new words")


class TestGateSchemaLatch:
    async def test_schema_400_retries_without_schema_and_latches(self) -> None:
        client = FakeAnthropicClient()
        client.message_results.extend(
            [
                make_anthropic_status_error(400, "output_config.format: unsupported"),
                gate_message(GATE_JSON),
                gate_message(GATE_JSON),
            ]
        )
        gate = make_gate(client)
        assert len(await gate.extract_claims("", "new words")) == 1
        assert "format" in client.message_calls[0]["output_config"]
        assert "format" not in client.message_calls[1].get("output_config", {})
        assert AnthropicClaimGate._json_schema_unsupported is True
        # Latched: the next pass goes straight to prompt-only JSON.
        await gate.extract_claims("", "more words")
        assert "format" not in client.message_calls[2].get("output_config", {})

    async def test_failed_retry_does_not_latch(self) -> None:
        client = FakeAnthropicClient()
        client.message_results.extend(
            [
                make_anthropic_status_error(400, "bad model id"),
                make_anthropic_status_error(400, "bad model id"),
            ]
        )
        with pytest.raises(GateError, match="bad model id"):
            await make_gate(client).extract_claims("", "new words")
        assert AnthropicClaimGate._json_schema_unsupported is False


class TestContradictionJudge:
    async def test_strict_judgement(self) -> None:
        client = FakeAnthropicClient()
        client.message_results.append(
            gate_message(
                '{"contradicts": true, "confidence": "high", '
                '"explanation": "Never vs. twice."}'
            )
        )
        judgement = await make_gate(client).judge_contradiction(
            "I have been to Japan twice.", "I have never left the country."
        )
        assert judgement.contradicts is True
        assert judgement.confidence == "high"
        call = client.message_calls[0]
        assert "system" not in call
        assert call["output_config"]["format"]["type"] == "json_schema"


# --------------------------------------------------------------------------- #
# Verify — structured path
# --------------------------------------------------------------------------- #


class TestVerifyStructured:
    async def test_request_shape(self) -> None:
        client = FakeAnthropicClient()
        client.message_results.append(grounded_message(verdict_json()))
        await make_checker(client).check(CLAIM)

        (call,) = client.message_calls
        assert call["model"] == "claude-verify-test"
        assert "temperature" not in call
        assert call["tools"] == [
            {"type": "web_search_20250305", "name": "web_search", "max_uses": 2}
        ]
        assert call["thinking"] == {"type": "adaptive"}
        assert call["output_config"] == {
            "effort": "low",
            "format": {"type": "json_schema", "schema": ANTHROPIC_VERDICT_SCHEMA},
        }
        assert call["messages"] == [{"role": "user", "content": CLAIM}]
        (system_block,) = call["system"]
        assert system_block["cache_control"] == {"type": "ephemeral"}
        assert "web_search tool" in system_block["text"]
        assert "at most\n2 times" in system_block["text"]

    async def test_dynamic_search_tool_and_max_uses(self) -> None:
        client = FakeAnthropicClient()
        client.message_results.append(grounded_message(verdict_json()))
        await make_checker(client, web_max_uses=1, web_search_tool="dynamic").check(
            CLAIM
        )
        assert client.message_calls[0]["tools"] == [
            {"type": "web_search_20260209", "name": "web_search", "max_uses": 1}
        ]

    async def test_verdict_with_model_picked_sources(self) -> None:
        client = FakeAnthropicClient()
        client.message_results.append(
            grounded_message(
                verdict_json(source_urls=(BRIT[0], "https://fake.example"))
            )
        )
        verdict = await make_checker(client).check(CLAIM, topic="science_tech")
        assert verdict.label == "FALSE"
        assert verdict.evidence == "strong"
        assert verdict.used_fallback is False
        assert [s.url for s in verdict.sources] == [BRIT[0]]

    async def test_answer_without_search_is_downgraded(self) -> None:
        client = FakeAnthropicClient()
        client.message_results.append(
            make_anthropic_message([anthropic_text_block(verdict_json(label="TRUE"))])
        )
        verdict = await make_checker(client).check(CLAIM)
        assert verdict.label == "UNVERIFIED"
        assert verdict.sources == []
        assert NO_SOURCES_NOTE in verdict.explanation

    async def test_partial_evidence_is_downgraded(self) -> None:
        client = FakeAnthropicClient()
        client.message_results.append(
            grounded_message(verdict_json(label="TRUE", evidence="partial"))
        )
        verdict = await make_checker(client).check(CLAIM)
        assert verdict.label == "UNVERIFIED"

    async def test_image_is_attached_before_the_claim(self) -> None:
        client = FakeAnthropicClient()
        client.message_results.append(grounded_message(verdict_json()))
        await make_checker(client).check(CLAIM, image_b64="QUJD")
        call = client.message_calls[0]
        content = call["messages"][0]["content"]
        assert content == [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": "QUJD",
                },
            },
            {"type": "text", "text": CLAIM},
        ]
        assert "frame captured from the live stream" in call["system"][0]["text"]

    async def test_pause_turn_is_resumed_with_the_paused_content(self) -> None:
        client = FakeAnthropicClient()
        paused = make_anthropic_message(
            [*anthropic_search_blocks((TOUR,))], stop_reason="pause_turn"
        )
        client.message_results.extend(
            [paused, make_anthropic_message([anthropic_text_block(verdict_json())])]
        )
        verdict = await make_checker(client).check(CLAIM)
        assert verdict.label == "FALSE"
        # Sources from the paused half of the turn still count.
        assert [s.url for s in verdict.sources] == [TOUR[0]]
        resumed = client.message_calls[1]["messages"]
        assert resumed[0] == {"role": "user", "content": CLAIM}
        assert resumed[1]["role"] == "assistant"
        assert [b.type for b in resumed[1]["content"]] == [
            "server_tool_use",
            "web_search_tool_result",
        ]

    async def test_endless_pause_is_a_verification_error(self) -> None:
        client = FakeAnthropicClient()
        for _ in range(MAX_PAUSE_CONTINUATIONS + 1):
            client.message_results.append(
                make_anthropic_message(
                    [*anthropic_search_blocks((TOUR,))], stop_reason="pause_turn"
                )
            )
        with pytest.raises(VerificationError, match="paused"):
            await make_checker(client).check(CLAIM)

    async def test_refusal_is_a_verification_error(self) -> None:
        client = FakeAnthropicClient()
        client.message_results.append(
            make_anthropic_message([anthropic_text_block("")], stop_reason="refusal")
        )
        with pytest.raises(VerificationError, match="refusal"):
            await make_checker(client).check(CLAIM)


# --------------------------------------------------------------------------- #
# Verify — fallback chain
# --------------------------------------------------------------------------- #


class TestVerifyFallback:
    async def test_structured_400_latches_and_uses_the_text_chain(self) -> None:
        client = FakeAnthropicClient()
        client.message_results.extend(
            [
                make_anthropic_status_error(
                    400, "output_config.format cannot be combined with citations"
                ),
                grounded_message(
                    "LABEL: FALSE\nEVIDENCE: strong\nEXPLANATION: It is about 330 m.",
                    citations=(TOUR,),
                ),
            ]
        )
        checker = make_checker(client)
        verdict = await checker.check(CLAIM)
        assert verdict.used_fallback is True
        assert verdict.label == "FALSE"
        assert [s.url for s in verdict.sources] == [TOUR[0]]
        fallback_call = client.message_calls[1]
        assert "format" not in fallback_call["output_config"]
        assert fallback_call["tools"][0]["name"] == "web_search"
        assert "LABEL:" in fallback_call["system"][0]["text"]
        assert AnthropicFactChecker._structured_unsupported is True

        # Latched: the next claim skips the structured attempt entirely.
        client.message_results.append(
            grounded_message("LABEL: TRUE\nEVIDENCE: strong\nEXPLANATION: Yes.")
        )
        await checker.check("Paris is the capital of France.")
        assert len(client.message_calls) == 3

    async def test_unparseable_structured_output_falls_back_without_latching(
        self,
    ) -> None:
        client = FakeAnthropicClient()
        client.message_results.extend(
            [
                grounded_message("not json at all"),
                grounded_message("LABEL: TRUE\nEVIDENCE: strong\nEXPLANATION: Yes."),
            ]
        )
        verdict = await make_checker(client).check(CLAIM)
        assert verdict.used_fallback is True
        assert AnthropicFactChecker._structured_unsupported is False

    async def test_free_text_goes_through_the_extraction_pass(self) -> None:
        client = FakeAnthropicClient()
        client.message_results.extend(
            [
                grounded_message("not json"),
                grounded_message("The tower is roughly 330 metres, so this is false."),
                make_anthropic_message(
                    [
                        anthropic_text_block(
                            '{"label": "FALSE", "explanation": "It is about 330 m."}'
                        )
                    ]
                ),
            ]
        )
        verdict = await make_checker(client).check(CLAIM)
        assert verdict.label == "FALSE"
        extraction_call = client.message_calls[2]
        assert "tools" not in extraction_call  # no search on the extraction pass
        assert extraction_call["thinking"] == {"type": "disabled"}
        assert extraction_call["output_config"]["format"]["type"] == "json_schema"

    async def test_extraction_failure_exhausts_the_chain(self) -> None:
        client = FakeAnthropicClient()
        client.message_results.extend(
            [
                grounded_message("not json"),
                grounded_message("no verdict here"),
                make_anthropic_message([anthropic_text_block("still nothing")]),
            ]
        )
        with pytest.raises(VerificationError, match="fallback chain"):
            await make_checker(client).check(CLAIM)

    async def test_fallback_400_is_a_verification_error(self) -> None:
        client = FakeAnthropicClient()
        client.message_results.extend(
            [
                make_anthropic_status_error(400, "format unsupported"),
                make_anthropic_status_error(400, "bad request again"),
            ]
        )
        with pytest.raises(VerificationError, match="bad request again"):
            await make_checker(client).check(CLAIM)


# --------------------------------------------------------------------------- #
# Verify — error mapping
# --------------------------------------------------------------------------- #


class TestVerifyErrorMapping:
    async def test_429_trips_the_cooldown_for_retry_after(self) -> None:
        cooldown = QuotaCooldown()
        client = FakeAnthropicClient()
        client.message_results.append(
            make_anthropic_status_error(
                429, "rate limited", "rate_limit_error", headers={"retry-after": "17"}
            )
        )
        with pytest.raises(QuotaExceededError, match="rate limit"):
            await make_checker(client, cooldown).check(CLAIM)
        assert cooldown.active
        assert 15 < cooldown.remaining_s <= 17

    @pytest.mark.parametrize(
        "error",
        [
            make_anthropic_status_error(402, "billing", "billing_error"),
            make_anthropic_status_error(
                400, "Your credit balance is too low to access the Anthropic API."
            ),
            make_anthropic_status_error(
                429,
                "monthly limit",
                "rate_limit_error",
                error_code="enforced_spend_limit_reached",
            ),
        ],
    )
    async def test_out_of_credit_trips_the_long_cooldown(
        self, error: Exception
    ) -> None:
        cooldown = QuotaCooldown()
        client = FakeAnthropicClient()
        client.message_results.append(error)
        with pytest.raises(QuotaExceededError, match="credit"):
            await make_checker(client, cooldown).check(CLAIM)
        assert cooldown.remaining_s > CREDITS_EXHAUSTED_COOLDOWN_S - 5
        # Never mistaken for a structured-output 400: no fallback call.
        assert len(client.message_calls) == 1

    @pytest.mark.parametrize(
        ("error", "match"),
        [
            (
                make_anthropic_status_error(401, "bad", "authentication_error"),
                "ant auth login",
            ),
            (
                make_anthropic_status_error(403, "denied", "permission_error"),
                "credentials",
            ),
            (
                make_anthropic_status_error(529, "busy", "overloaded_error"),
                "overloaded",
            ),
            (make_anthropic_status_error(500, "boom", "api_error"), "500"),
        ],
    )
    async def test_other_status_errors_are_verification_errors(
        self, error: Exception, match: str
    ) -> None:
        client = FakeAnthropicClient()
        client.message_results.append(error)
        with pytest.raises(VerificationError, match=match):
            await make_checker(client).check(CLAIM)

    async def test_connection_error_is_a_verification_error(self) -> None:
        import httpx2

        client = FakeAnthropicClient()
        client.message_results.append(
            anthropic.APIConnectionError(
                request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
            )
        )
        with pytest.raises(VerificationError, match="could not reach"):
            await make_checker(client).check(CLAIM)

    async def test_timeout_is_retried_once_then_fails(self) -> None:
        import httpx2

        request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        client = FakeAnthropicClient()
        client.message_results.extend(
            [
                anthropic.APITimeoutError(request=request),
                grounded_message(verdict_json()),
            ]
        )
        verdict = await make_checker(client).check(CLAIM)
        assert verdict.label == "FALSE"
        assert len(client.message_calls) == 2

        client.message_results.extend([anthropic.APITimeoutError(request=request)] * 2)
        with pytest.raises(VerificationError, match="timed out"):
            await make_checker(client).check("Another claim entirely about rivers.")

    def test_timeout_maps_to_asyncio_timeout(self) -> None:
        import httpx2

        checker = make_checker(FakeAnthropicClient())
        error = checker._translate_error(
            anthropic.APITimeoutError(
                request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
            ),
            "verify",
            structured=True,
        )
        assert isinstance(error, asyncio.TimeoutError)

    def test_unknown_search_tool_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="web search tool"):
            make_checker(FakeAnthropicClient(), web_search_tool="turbo")
