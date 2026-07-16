"""OpenRouter transport: gate modes, verify chain, citations, error mapping.

Runs fully offline against :class:`tests.conftest.FakeOpenRouterClient`.
Responses are REAL ``openai.types.chat.ChatCompletion`` objects and failures
are REAL ``openai`` exception instances (built over minimal ``httpx``
request/response pairs), so every ``except openai.…`` clause in
``app.llm_openrouter`` is exercised against the exact types the SDK raises.
"""

from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

from app import llm_openrouter as llm_openrouter_module
from app.claim_gate import GateError
from app.fact_checker import QuotaExceededError, VerificationError
from app.llm_openrouter import (
    CONSECUTIVE_503_LATCH_THRESHOLD,
    CREDITS_EXHAUSTED_COOLDOWN_S,
    GATE_JSON_SCHEMA,
    GATE_MAX_TOKENS,
    JSON_SCHEMA_RETRY_WINDOW_S,
    VERDICT_JSON_SCHEMA,
    WEB_SEARCH_PROMPT,
    OpenRouterClaimGate,
    OpenRouterFactChecker,
    _openrouter_error_message,
    _ReasoningSupport,
)
from app.models import TOPICS, Source, TranscriptSegment
from app.rate_limit import QuotaCooldown
from tests.conftest import (
    FakeOpenRouterClient,
    make_chat_completion,
    make_openrouter_rate_limit_error,
    make_openrouter_status_error,
    make_openrouter_timeout_error,
    make_verdict_completion,
)

CLAIM = "The Eiffel Tower is 450 meters tall."

GATE_CLAIMS_JSON = (
    '{"claims": [{"claim_text": "The Eiffel Tower is 450 meters tall.", '
    '"check_worthiness": 0.9}]}'
)


@pytest.fixture()
def cooldown() -> QuotaCooldown:
    return QuotaCooldown()


def make_gate(
    fake_client: FakeOpenRouterClient, reasoning_effort: str | None = "low"
) -> OpenRouterClaimGate:
    return OpenRouterClaimGate(
        client=fake_client,  # type: ignore[arg-type] — duck-typed fake
        model="fake-or-gate-model",
        gate_interval_s=100.0,
        gate_timeout_s=5.0,
        reasoning_effort=reasoning_effort,
    )


@pytest.fixture()
def gate(fake_openrouter_client: FakeOpenRouterClient) -> OpenRouterClaimGate:
    return make_gate(fake_openrouter_client)


class FakeClock:
    """Stand-in for the ``time`` module inside ``app.llm_openrouter``."""

    def __init__(self, start: float = 1_000.0) -> None:
        self._now = start

    def monotonic(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


@pytest.fixture()
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    clock = FakeClock()
    monkeypatch.setattr(llm_openrouter_module, "time", clock)
    return clock


@pytest.fixture()
def checker(
    fake_openrouter_client: FakeOpenRouterClient, cooldown: QuotaCooldown
) -> OpenRouterFactChecker:
    return OpenRouterFactChecker(
        client=fake_openrouter_client,  # type: ignore[arg-type] — duck-typed fake
        verify_model="fake-or-verify-model",
        cooldown=cooldown,
        web_max_results=3,
        verify_timeout_s=0.5,
    )


def make_segment(text: str) -> TranscriptSegment:
    return TranscriptSegment(
        text=text, start=0.0, end=1.0, avg_logprob=-0.3, no_speech_prob=0.05
    )


# --------------------------------------------------------------------------- #
# Gate: strict json_schema mode
# --------------------------------------------------------------------------- #


class TestGateJsonSchemaMode:
    async def test_happy_path_parses_claims(
        self, gate: OpenRouterClaimGate, fake_openrouter_client: FakeOpenRouterClient
    ) -> None:
        fake_openrouter_client.completion_results.append(
            make_chat_completion(GATE_CLAIMS_JSON)
        )
        claims = await gate.extract_claims("", "some transcript text")
        assert [(c.claim_text, c.check_worthiness) for c in claims] == [(CLAIM, 0.9)]

    async def test_call_shape_uses_strict_schema_and_routing_guard(
        self, gate: OpenRouterClaimGate, fake_openrouter_client: FakeOpenRouterClient
    ) -> None:
        fake_openrouter_client.completion_results.append(
            make_chat_completion('{"claims": []}')
        )
        await gate.extract_claims("earlier context", "some transcript text")
        call = fake_openrouter_client.completion_calls[0]
        assert call["model"] == "fake-or-gate-model"
        assert call["temperature"] == 0.0
        assert call["max_tokens"] == GATE_MAX_TOKENS
        assert call["response_format"] == {
            "type": "json_schema",
            "json_schema": GATE_JSON_SCHEMA,
        }
        # The strict schema the provider receives must demand the topic enum.
        claim_schema = call["response_format"]["json_schema"]["schema"]["properties"][
            "claims"
        ]["items"]
        assert claim_schema["properties"]["topic"] == {
            "type": "string",
            "enum": list(TOPICS),
        }
        assert claim_schema["required"] == ["claim_text", "check_worthiness", "topic"]
        # require_parameters keeps routing on providers that honor the schema;
        # response healing repairs near-miss JSON; low reasoning bounds latency.
        assert call["extra_body"]["provider"] == {"require_parameters": True}
        assert call["extra_body"]["plugins"] == [{"id": "response-healing"}]
        assert call["extra_body"]["reasoning"] == {"effort": "low"}
        assert "some transcript text" in call["messages"][0]["content"]
        # The per-call SDK timeout is the gate timeout, not the 30 s default.
        timeout = fake_openrouter_client.with_options_calls[0]["timeout"]
        assert timeout.read == 5.0
        assert timeout.connect == 3.0

    async def test_topic_parses_through_and_unknown_coerces_to_other(
        self, gate: OpenRouterClaimGate, fake_openrouter_client: FakeOpenRouterClient
    ) -> None:
        content = (
            '{"claims": ['
            '{"claim_text": "A sports claim.", "check_worthiness": 0.9,'
            ' "topic": "sports"},'
            '{"claim_text": "A mystery claim.", "check_worthiness": 0.9,'
            ' "topic": "astrology"},'
            '{"claim_text": "A bare claim.", "check_worthiness": 0.9}'
            "]}"
        )
        fake_openrouter_client.completion_results.append(make_chat_completion(content))
        claims = await gate.extract_claims("", "some transcript text")
        assert [c.topic for c in claims] == ["sports", "other", "other"]

    async def test_fenced_json_with_prose_is_tolerated(
        self, gate: OpenRouterClaimGate, fake_openrouter_client: FakeOpenRouterClient
    ) -> None:
        fake_openrouter_client.completion_results.append(
            make_chat_completion(f"Here you go:\n```json\n{GATE_CLAIMS_JSON}\n```")
        )
        claims = await gate.extract_claims("", "some transcript text")
        assert claims[0].claim_text == CLAIM

    async def test_malformed_json_raises_gate_error_and_drops_batch(
        self, gate: OpenRouterClaimGate, fake_openrouter_client: FakeOpenRouterClient
    ) -> None:
        gate.add_transcript(make_segment("the eiffel tower is 450 meters tall chat"))
        fake_openrouter_client.completion_results.append(
            make_chat_completion('{"claims": "not a list"}')
        )
        with pytest.raises(GateError, match="schema validation"):
            await gate.run()
        # Buffer was drained BEFORE the call: a second run has nothing to send.
        assert await gate.run() == []
        assert len(fake_openrouter_client.completion_calls) == 1

    async def test_empty_response_text_raises_gate_error(
        self, gate: OpenRouterClaimGate, fake_openrouter_client: FakeOpenRouterClient
    ) -> None:
        fake_openrouter_client.completion_results.append(make_chat_completion(""))
        with pytest.raises(GateError, match="no text"):
            await gate.extract_claims("", "some transcript text")

    async def test_timeout_raises_gate_error(
        self, gate: OpenRouterClaimGate, fake_openrouter_client: FakeOpenRouterClient
    ) -> None:
        fake_openrouter_client.completion_results.append(
            make_openrouter_timeout_error()
        )
        with pytest.raises(GateError, match="timed out"):
            await gate.extract_claims("", "some transcript text")

    async def test_402_raises_loud_gate_error_without_cooldown(
        self, gate: OpenRouterClaimGate, fake_openrouter_client: FakeOpenRouterClient
    ) -> None:
        fake_openrouter_client.completion_results.append(
            make_openrouter_status_error(402, "Insufficient credits")
        )
        with pytest.raises(GateError, match="top up at openrouter.ai"):
            await gate.extract_claims("", "some transcript text")

    async def test_502_maps_to_gate_error_with_openrouter_message(
        self, gate: OpenRouterClaimGate, fake_openrouter_client: FakeOpenRouterClient
    ) -> None:
        fake_openrouter_client.completion_results.append(
            make_openrouter_status_error(502, "Your chosen model is down")
        )
        with pytest.raises(GateError, match=r"\(502\).*model is down"):
            await gate.extract_claims("", "some transcript text")
        # 502 is not an unsupported-params status: mode must NOT flip.
        assert OpenRouterClaimGate._json_schema_unsupported is False


# --------------------------------------------------------------------------- #
# Gate: json_object fallback mode (once per process)
# --------------------------------------------------------------------------- #


class TestGateJsonObjectFallback:
    @pytest.mark.parametrize("status_code", [400, 404, 422])
    async def test_structural_unsupported_error_latches_json_object_mode(
        self,
        gate: OpenRouterClaimGate,
        fake_openrouter_client: FakeOpenRouterClient,
        status_code: int,
    ) -> None:
        # Two scripted errors: the strict call, then its one-shot retry
        # without ``reasoning`` (which fails too -> genuinely structural).
        for _ in range(2):
            fake_openrouter_client.completion_results.append(
                make_openrouter_status_error(
                    status_code, "response_format not supported"
                )
            )
        fake_openrouter_client.completion_results.append(
            make_chat_completion(GATE_CLAIMS_JSON)
        )
        claims = await gate.extract_claims("", "some transcript text")
        assert claims[0].claim_text == CLAIM
        assert len(fake_openrouter_client.completion_calls) == 3
        retry_call = fake_openrouter_client.completion_calls[2]
        assert retry_call["response_format"] == {"type": "json_object"}
        # No require_parameters guard in json_object mode.
        assert "provider" not in retry_call["extra_body"]
        assert OpenRouterClaimGate._json_schema_unsupported is True
        # The FAILED no-reasoning retry proves nothing about reasoning: it
        # stays enabled (and is still sent on the json_object call).
        assert _ReasoningSupport.unsupported is False
        assert retry_call["extra_body"]["reasoning"] == {"effort": "low"}

    async def test_latch_is_process_wide_and_skips_json_schema_afterwards(
        self, fake_openrouter_client: FakeOpenRouterClient
    ) -> None:
        first_gate = OpenRouterClaimGate(
            client=fake_openrouter_client,  # type: ignore[arg-type]
            model="fake-or-gate-model",
        )
        for _ in range(2):  # strict call + its no-reasoning retry
            fake_openrouter_client.completion_results.append(
                make_openrouter_status_error(422, "no structured outputs")
            )
        fake_openrouter_client.completion_results.append(
            make_chat_completion('{"claims": []}')
        )
        await first_gate.extract_claims("", "text one")
        # A FRESH gate (new session) starts directly in json_object mode.
        second_gate = OpenRouterClaimGate(
            client=fake_openrouter_client,  # type: ignore[arg-type]
            model="fake-or-gate-model",
        )
        fake_openrouter_client.completion_results.append(
            make_chat_completion('{"claims": []}')
        )
        await second_gate.extract_claims("", "text two")
        assert len(fake_openrouter_client.completion_calls) == 4
        assert fake_openrouter_client.completion_calls[3]["response_format"] == {
            "type": "json_object"
        }

    async def test_malformed_json_in_fallback_mode_still_gate_error(
        self, gate: OpenRouterClaimGate, fake_openrouter_client: FakeOpenRouterClient
    ) -> None:
        OpenRouterClaimGate._json_schema_unsupported = True
        fake_openrouter_client.completion_results.append(
            make_chat_completion("not json at all")
        )
        with pytest.raises(GateError, match="no JSON object"):
            await gate.extract_claims("", "some transcript text")


# --------------------------------------------------------------------------- #
# Gate: 503 handling (per-call fallback + time-bounded latch, never permanent)
# --------------------------------------------------------------------------- #


class TestGate503TimeBoundedLatch:
    """503 is ambiguous (transient provider overload vs structural routing
    exhaustion), so it must never permanently latch json_object mode.

    Gates here disable ``reasoning`` so each strict failure is exactly one
    scripted error (no no-reasoning retry in the way).
    """

    async def _drive_one_503_round(
        self, gate: OpenRouterClaimGate, fake_client: FakeOpenRouterClient
    ) -> None:
        """One extract pass: strict-mode 503, then a json_object success."""
        fake_client.completion_results.append(
            make_openrouter_status_error(503, "no available provider")
        )
        fake_client.completion_results.append(make_chat_completion('{"claims": []}'))
        await gate.extract_claims("", "some transcript text")

    async def test_single_503_falls_back_for_this_call_only(
        self, fake_openrouter_client: FakeOpenRouterClient
    ) -> None:
        gate = make_gate(fake_openrouter_client, reasoning_effort=None)
        fake_openrouter_client.completion_results.append(
            make_openrouter_status_error(503, "no available provider")
        )
        fake_openrouter_client.completion_results.append(
            make_chat_completion(GATE_CLAIMS_JSON)
        )
        claims = await gate.extract_claims("", "text one")
        assert claims[0].claim_text == CLAIM
        fallback_call = fake_openrouter_client.completion_calls[1]
        assert fallback_call["response_format"] == {"type": "json_object"}
        # NOT latched: neither permanently nor time-bounded.
        assert OpenRouterClaimGate._json_schema_unsupported is False
        assert OpenRouterClaimGate._consecutive_strict_503s == 1
        # The NEXT extract re-attempts strict json_schema mode immediately.
        fake_openrouter_client.completion_results.append(
            make_chat_completion('{"claims": []}')
        )
        await gate.extract_claims("", "text two")
        next_call = fake_openrouter_client.completion_calls[2]
        assert next_call["response_format"]["type"] == "json_schema"
        # The strict success reset the streak.
        assert OpenRouterClaimGate._consecutive_strict_503s == 0

    async def test_consecutive_503s_start_time_bounded_latch(
        self, fake_openrouter_client: FakeOpenRouterClient, fake_clock: FakeClock
    ) -> None:
        gate = make_gate(fake_openrouter_client, reasoning_effort=None)
        for _ in range(CONSECUTIVE_503_LATCH_THRESHOLD):
            await self._drive_one_503_round(gate, fake_openrouter_client)
        # Time-bounded latch: strict mode paused, NOT permanently unsupported.
        assert OpenRouterClaimGate._json_schema_unsupported is False
        assert (
            OpenRouterClaimGate._json_schema_retry_at
            == fake_clock.monotonic() + JSON_SCHEMA_RETRY_WINDOW_S
        )
        # While latched, extract goes straight to json_object (ONE call).
        fake_openrouter_client.completion_results.append(
            make_chat_completion('{"claims": []}')
        )
        await gate.extract_claims("", "text during latch window")
        assert (
            len(fake_openrouter_client.completion_calls)
            == 2 * CONSECUTIVE_503_LATCH_THRESHOLD + 1
        )
        assert fake_openrouter_client.completion_calls[-1]["response_format"] == {
            "type": "json_object"
        }

    async def test_latch_window_expiry_reprobes_json_schema(
        self, fake_openrouter_client: FakeOpenRouterClient, fake_clock: FakeClock
    ) -> None:
        gate = make_gate(fake_openrouter_client, reasoning_effort=None)
        for _ in range(CONSECUTIVE_503_LATCH_THRESHOLD):
            await self._drive_one_503_round(gate, fake_openrouter_client)
        fake_clock.advance(JSON_SCHEMA_RETRY_WINDOW_S)
        fake_openrouter_client.completion_results.append(
            make_chat_completion(GATE_CLAIMS_JSON)
        )
        claims = await gate.extract_claims("", "text after the window")
        assert claims[0].claim_text == CLAIM
        reprobe_call = fake_openrouter_client.completion_calls[-1]
        assert reprobe_call["response_format"]["type"] == "json_schema"
        # The successful re-probe fully restores strict mode.
        assert OpenRouterClaimGate._consecutive_strict_503s == 0

    async def test_strict_success_resets_consecutive_503_counter(
        self, fake_openrouter_client: FakeOpenRouterClient
    ) -> None:
        gate = make_gate(fake_openrouter_client, reasoning_effort=None)
        for _ in range(CONSECUTIVE_503_LATCH_THRESHOLD - 1):
            await self._drive_one_503_round(gate, fake_openrouter_client)
        assert (
            OpenRouterClaimGate._consecutive_strict_503s
            == CONSECUTIVE_503_LATCH_THRESHOLD - 1
        )
        fake_openrouter_client.completion_results.append(
            make_chat_completion('{"claims": []}')
        )
        await gate.extract_claims("", "strict success resets the streak")
        assert OpenRouterClaimGate._consecutive_strict_503s == 0
        # A later lone 503 starts a FRESH streak: still no latch.
        await self._drive_one_503_round(gate, fake_openrouter_client)
        assert OpenRouterClaimGate._consecutive_strict_503s == 1
        assert OpenRouterClaimGate._json_schema_retry_at == 0.0


# --------------------------------------------------------------------------- #
# Reasoning under require_parameters: one-shot no-reasoning retry
# --------------------------------------------------------------------------- #


class TestReasoningUnsupportedRetry:
    async def test_gate_retries_strict_without_reasoning_and_latches(
        self, gate: OpenRouterClaimGate, fake_openrouter_client: FakeOpenRouterClient
    ) -> None:
        fake_openrouter_client.completion_results.append(
            make_openrouter_status_error(404, "no endpoints support reasoning")
        )
        fake_openrouter_client.completion_results.append(
            make_chat_completion(GATE_CLAIMS_JSON)
        )
        claims = await gate.extract_claims("", "some transcript text")
        assert claims[0].claim_text == CLAIM
        first_call, retry_call = fake_openrouter_client.completion_calls
        assert first_call["extra_body"]["reasoning"] == {"effort": "low"}
        # The retry keeps strict json_schema + require_parameters and drops
        # ONLY the reasoning field.
        assert retry_call["response_format"]["type"] == "json_schema"
        assert retry_call["extra_body"]["provider"] == {"require_parameters": True}
        assert "reasoning" not in retry_call["extra_body"]
        # Reasoning was the disqualifier: latched off process-wide while
        # strict json_schema mode is PRESERVED.
        assert _ReasoningSupport.unsupported is True
        assert OpenRouterClaimGate._json_schema_unsupported is False
        # Subsequent calls omit reasoning up front and stay strict.
        fake_openrouter_client.completion_results.append(
            make_chat_completion('{"claims": []}')
        )
        await gate.extract_claims("", "more transcript text")
        next_call = fake_openrouter_client.completion_calls[2]
        assert next_call["response_format"]["type"] == "json_schema"
        assert "reasoning" not in next_call["extra_body"]

    async def test_verify_retries_strict_without_reasoning_and_latches(
        self,
        checker: OpenRouterFactChecker,
        fake_openrouter_client: FakeOpenRouterClient,
    ) -> None:
        fake_openrouter_client.completion_results.append(
            make_openrouter_status_error(503, "no available provider")
        )
        fake_openrouter_client.completion_results.append(
            make_verdict_completion("TRUE", "Confirmed without reasoning.")
        )
        verdict = await checker.check(CLAIM)
        assert verdict.label == "TRUE"
        # Strict mode preserved: this was NOT the fallback chain.
        assert verdict.used_fallback is False
        retry_call = fake_openrouter_client.completion_calls[1]
        assert retry_call["response_format"]["type"] == "json_schema"
        assert retry_call["extra_body"]["provider"] == {"require_parameters": True}
        assert "reasoning" not in retry_call["extra_body"]
        assert _ReasoningSupport.unsupported is True

    async def test_effort_disabled_by_config_never_sends_reasoning(
        self, fake_openrouter_client: FakeOpenRouterClient
    ) -> None:
        gate = make_gate(fake_openrouter_client, reasoning_effort=None)
        fake_openrouter_client.completion_results.append(
            make_chat_completion('{"claims": []}')
        )
        await gate.extract_claims("", "some transcript text")
        assert (
            "reasoning" not in fake_openrouter_client.completion_calls[0]["extra_body"]
        )


# --------------------------------------------------------------------------- #
# Error-message extraction (SDK-unwrapped body shape)
# --------------------------------------------------------------------------- #


class TestOpenRouterErrorMessage:
    def test_fake_errors_carry_the_sdk_unwrapped_body(self) -> None:
        error = make_openrouter_status_error(402, "Insufficient credits")
        # The SDK unwraps the {"error": ...} envelope before constructing the
        # exception, so .body is the INNER dict — the fakes must match.
        assert error.body == {
            "code": 402,
            "message": "Insufficient credits",
            "metadata": {},
        }
        assert _openrouter_error_message(error) == "Insufficient credits"

    def test_enveloped_body_still_read_defensively(self) -> None:
        response = httpx.Response(
            500,
            request=httpx.Request(
                "POST", "https://openrouter.ai/api/v1/chat/completions"
            ),
        )
        error = openai.APIStatusError(
            "boom", response=response, body={"error": {"message": "enveloped"}}
        )
        assert _openrouter_error_message(error) == "enveloped"

    def test_non_dict_body_falls_back_to_str(self) -> None:
        response = httpx.Response(
            502,
            request=httpx.Request(
                "POST", "https://openrouter.ai/api/v1/chat/completions"
            ),
        )
        error = openai.APIStatusError(
            "Error code: 502 - upstream unavailable",
            response=response,
            body="upstream unavailable",
        )
        assert _openrouter_error_message(error) == (
            "Error code: 502 - upstream unavailable"
        )


# --------------------------------------------------------------------------- #
# Verify: grounded structured happy path
# --------------------------------------------------------------------------- #


class TestVerifyGroundedStructured:
    async def test_verdict_assembled_from_json_and_annotations(
        self,
        checker: OpenRouterFactChecker,
        fake_openrouter_client: FakeOpenRouterClient,
    ) -> None:
        fake_openrouter_client.completion_results.append(
            make_verdict_completion(
                "FALSE",
                "The Eiffel Tower is about 330 meters tall.",
                citations=[("https://www.toureiffel.paris/x", "Key figures")],
            )
        )
        verdict = await checker.check(CLAIM)
        assert verdict.claim == CLAIM
        assert verdict.label == "FALSE"
        assert verdict.used_fallback is False
        # url + title ONLY: OpenRouter's extra excerpt field (present on the
        # fake annotations) must not leak into the frozen wire Source shape.
        assert verdict.sources == [
            Source(url="https://www.toureiffel.paris/x", title="Key figures")
        ]

    async def test_call_shape_combines_web_plugin_and_strict_schema(
        self,
        checker: OpenRouterFactChecker,
        fake_openrouter_client: FakeOpenRouterClient,
    ) -> None:
        fake_openrouter_client.completion_results.append(
            make_verdict_completion("TRUE", "Confirmed.")
        )
        await checker.check(CLAIM)
        call = fake_openrouter_client.completion_calls[0]
        assert call["model"] == "fake-or-verify-model"
        assert call["temperature"] == 0.0
        assert call["response_format"] == {
            "type": "json_schema",
            "json_schema": VERDICT_JSON_SCHEMA,
        }
        assert call["extra_body"]["provider"] == {"require_parameters": True}
        # The web plugin must carry the custom search prompt: the default one
        # demands inline markdown citations, which fights strict JSON output.
        assert call["extra_body"]["plugins"] == [
            {
                "id": "web",
                "engine": "exa",
                "max_results": 3,
                "search_prompt": WEB_SEARCH_PROMPT,
            }
        ]
        assert CLAIM in call["messages"][0]["content"]
        assert "Today is" in call["messages"][0]["content"]

    async def test_zero_citations_downgrades_to_unverified(
        self,
        checker: OpenRouterFactChecker,
        fake_openrouter_client: FakeOpenRouterClient,
    ) -> None:
        fake_openrouter_client.completion_results.append(
            make_verdict_completion("FALSE", "Refuted from memory.", citations=[])
        )
        verdict = await checker.check(CLAIM)
        assert verdict.label == "UNVERIFIED"
        assert "No verifiable sources were retrieved." in verdict.explanation
        assert verdict.sources == []


# --------------------------------------------------------------------------- #
# Verify: fallback chain
# --------------------------------------------------------------------------- #


class TestVerifyFallbackChain:
    async def test_unsupported_schema_falls_back_to_label_explanation(
        self,
        checker: OpenRouterFactChecker,
        fake_openrouter_client: FakeOpenRouterClient,
    ) -> None:
        # Two scripted errors: the strict call, then its one-shot retry
        # without ``reasoning`` (which fails too -> fallback chain).
        for _ in range(2):
            fake_openrouter_client.completion_results.append(
                make_openrouter_status_error(422, "response_format not supported")
            )
        fake_openrouter_client.completion_results.append(
            make_chat_completion(
                "LABEL: FALSE\nEXPLANATION: The tower is about 330 meters tall.",
                citations=[("https://example.com/tower", "Tower")],
            )
        )
        verdict = await checker.check(CLAIM)
        assert verdict.label == "FALSE"
        assert verdict.explanation == "The tower is about 330 meters tall."
        assert verdict.used_fallback is True
        assert verdict.sources[0].url == "https://example.com/tower"
        fallback_call = fake_openrouter_client.completion_calls[2]
        # Still grounded (web plugin) but with NO structured-output demand.
        assert "response_format" not in fallback_call
        assert fallback_call["extra_body"]["plugins"][0]["id"] == "web"

    async def test_unparseable_structured_output_falls_back(
        self,
        checker: OpenRouterFactChecker,
        fake_openrouter_client: FakeOpenRouterClient,
    ) -> None:
        fake_openrouter_client.completion_results.append(
            make_chat_completion(
                "I could not decide.", citations=[("https://example.com/a", "A")]
            )
        )
        fake_openrouter_client.completion_results.append(
            make_chat_completion(
                "LABEL: UNVERIFIED\nEXPLANATION: Sources were inconclusive.",
                citations=[("https://example.com/b", "B")],
            )
        )
        verdict = await checker.check(CLAIM)
        assert verdict.label == "UNVERIFIED"
        assert verdict.used_fallback is True

    async def test_last_resort_json_object_extraction(
        self,
        checker: OpenRouterFactChecker,
        fake_openrouter_client: FakeOpenRouterClient,
    ) -> None:
        for _ in range(2):  # strict call + its no-reasoning retry
            fake_openrouter_client.completion_results.append(
                make_openrouter_status_error(422, "response_format not supported")
            )
        fake_openrouter_client.completion_results.append(
            make_chat_completion(
                "After searching, this appears false; the tower is 330 m.",
                citations=[("https://example.com/grounded", "Grounded")],
            )
        )
        fake_openrouter_client.completion_results.append(
            make_chat_completion(
                '{"label": "FALSE", "explanation": "The tower is 330 meters tall."}'
            )
        )
        verdict = await checker.check(CLAIM)
        assert verdict.label == "FALSE"
        assert verdict.used_fallback is True
        # Citations still come from the grounded step, not the extraction.
        assert verdict.sources[0].url == "https://example.com/grounded"
        extraction_call = fake_openrouter_client.completion_calls[3]
        assert extraction_call["response_format"] == {"type": "json_object"}
        # The extraction pass runs WITHOUT the web plugin (nothing to search).
        assert extraction_call["extra_body"]["plugins"] == [{"id": "response-healing"}]

    async def test_exhausted_chain_raises_verification_error(
        self,
        checker: OpenRouterFactChecker,
        fake_openrouter_client: FakeOpenRouterClient,
    ) -> None:
        for _ in range(2):  # strict call + its no-reasoning retry
            fake_openrouter_client.completion_results.append(
                make_openrouter_status_error(422, "response_format not supported")
            )
        fake_openrouter_client.completion_results.append(
            make_chat_completion("free-form waffle with no label")
        )
        fake_openrouter_client.completion_results.append(
            make_chat_completion("still not json")
        )
        with pytest.raises(VerificationError, match="fallback chain"):
            await checker.check(CLAIM)

    async def test_empty_fallback_text_raises(
        self,
        checker: OpenRouterFactChecker,
        fake_openrouter_client: FakeOpenRouterClient,
    ) -> None:
        for _ in range(2):  # strict call + its no-reasoning retry
            fake_openrouter_client.completion_results.append(
                make_openrouter_status_error(422, "response_format not supported")
            )
        fake_openrouter_client.completion_results.append(make_chat_completion(""))
        with pytest.raises(VerificationError, match="no text"):
            await checker.check(CLAIM)

    async def test_unsupported_status_during_fallback_is_fatal(
        self,
        checker: OpenRouterFactChecker,
        fake_openrouter_client: FakeOpenRouterClient,
    ) -> None:
        for _ in range(2):  # strict call + its no-reasoning retry
            fake_openrouter_client.completion_results.append(
                make_openrouter_status_error(422, "no structured outputs")
            )
        fake_openrouter_client.completion_results.append(
            make_openrouter_status_error(422, "still unsupported")
        )
        with pytest.raises(VerificationError, match="grounded fallback"):
            await checker.check(CLAIM)


# --------------------------------------------------------------------------- #
# Verify: quota / credits / error mapping
# --------------------------------------------------------------------------- #


class TestVerifyErrorMapping:
    async def test_429_with_retry_after_trips_cooldown_for_that_duration(
        self,
        checker: OpenRouterFactChecker,
        fake_openrouter_client: FakeOpenRouterClient,
        cooldown: QuotaCooldown,
    ) -> None:
        fake_openrouter_client.completion_results.append(
            make_openrouter_rate_limit_error(retry_after="38")
        )
        with pytest.raises(QuotaExceededError, match="429"):
            await checker.check(CLAIM)
        assert cooldown.active is True
        assert 30.0 < cooldown.remaining_s <= 38.0
        assert len(fake_openrouter_client.completion_calls) == 1  # no fallback

    async def test_429_without_retry_after_defaults_to_sixty_seconds(
        self,
        checker: OpenRouterFactChecker,
        fake_openrouter_client: FakeOpenRouterClient,
        cooldown: QuotaCooldown,
    ) -> None:
        fake_openrouter_client.completion_results.append(
            make_openrouter_rate_limit_error(retry_after=None)
        )
        with pytest.raises(QuotaExceededError):
            await checker.check(CLAIM)
        assert 50.0 < cooldown.remaining_s <= 60.0

    async def test_402_trips_long_cooldown_with_loud_message(
        self,
        checker: OpenRouterFactChecker,
        fake_openrouter_client: FakeOpenRouterClient,
        cooldown: QuotaCooldown,
    ) -> None:
        fake_openrouter_client.completion_results.append(
            make_openrouter_status_error(402, "Insufficient credits")
        )
        with pytest.raises(QuotaExceededError, match="top up at openrouter.ai"):
            await checker.check(CLAIM)
        assert cooldown.active is True
        # The 15-minute credits latch, far beyond any Retry-After default.
        assert cooldown.remaining_s > CREDITS_EXHAUSTED_COOLDOWN_S - 10.0
        assert len(fake_openrouter_client.completion_calls) == 1

    async def test_403_maps_to_verification_error_with_openrouter_message(
        self,
        checker: OpenRouterFactChecker,
        fake_openrouter_client: FakeOpenRouterClient,
        cooldown: QuotaCooldown,
    ) -> None:
        fake_openrouter_client.completion_results.append(
            make_openrouter_status_error(403, "Input flagged by moderation")
        )
        with pytest.raises(VerificationError, match="flagged by moderation"):
            await checker.check(CLAIM)
        assert cooldown.active is False

    async def test_timeout_retries_once_then_succeeds(
        self,
        checker: OpenRouterFactChecker,
        fake_openrouter_client: FakeOpenRouterClient,
    ) -> None:
        fake_openrouter_client.completion_results.append(
            make_openrouter_timeout_error()
        )
        fake_openrouter_client.completion_results.append(
            make_verdict_completion("TRUE", "Confirmed on retry.")
        )
        verdict = await checker.check(CLAIM)
        assert verdict.explanation == "Confirmed on retry."
        assert verdict.used_fallback is False

    async def test_two_timeouts_raise_verification_error(
        self,
        checker: OpenRouterFactChecker,
        fake_openrouter_client: FakeOpenRouterClient,
    ) -> None:
        fake_openrouter_client.completion_results.append(
            make_openrouter_timeout_error()
        )
        fake_openrouter_client.completion_results.append(
            make_openrouter_timeout_error()
        )
        with pytest.raises(VerificationError, match="timed out twice"):
            await checker.check(CLAIM)


# --------------------------------------------------------------------------- #
# Citation extraction
# --------------------------------------------------------------------------- #


class TestCitationExtraction:
    def test_dedupe_by_url_and_cap_at_five(self) -> None:
        citations = [(f"https://example.com/{i}", f"T{i}") for i in range(6)]
        citations.insert(1, ("https://example.com/0", "dup of first"))
        message = make_chat_completion("{}", citations).choices[0].message
        sources = OpenRouterFactChecker._extract_citations(message)
        assert len(sources) == 5
        assert [s.url for s in sources] == [
            f"https://example.com/{i}" for i in range(5)
        ]

    def test_only_url_citation_annotations_count(self) -> None:
        message = SimpleNamespace(
            annotations=[
                SimpleNamespace(
                    type="file_citation",
                    url_citation=SimpleNamespace(
                        url="https://example.com/wrong-type", title=None
                    ),
                ),
                SimpleNamespace(type="url_citation", url_citation=None),
                SimpleNamespace(
                    type="url_citation",
                    url_citation=SimpleNamespace(url="", title="empty url"),
                ),
                SimpleNamespace(
                    type="url_citation",
                    url_citation=SimpleNamespace(
                        url="https://example.com/kept", title="Kept"
                    ),
                ),
            ]
        )
        sources = OpenRouterFactChecker._extract_citations(message)
        assert sources == [Source(url="https://example.com/kept", title="Kept")]

    def test_no_annotations_yields_no_sources(self) -> None:
        assert (
            OpenRouterFactChecker._extract_citations(SimpleNamespace(annotations=None))
            == []
        )

    def test_excerpt_field_is_not_surfaced(self) -> None:
        message = (
            make_chat_completion("{}", [("https://example.com/a", "A")])
            .choices[0]
            .message
        )
        # The fake carries OpenRouter's extra excerpt on the annotation…
        annotation: Any = message.annotations[0]
        assert getattr(annotation.url_citation, "content", None) is not None
        # …but the wire Source model exposes url and title ONLY.
        (source,) = OpenRouterFactChecker._extract_citations(message)
        assert source.model_dump() == {"url": "https://example.com/a", "title": "A"}
