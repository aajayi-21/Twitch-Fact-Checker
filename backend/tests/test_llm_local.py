"""Local (Ollama) gate transport: strict-schema ladder, errors, judge calls.

Mirrors ``tests/test_llm_openrouter.py``: the fake client scripts one queued
result per expected call and records every call's kwargs for shape asserts.
"""

import httpx
import openai
import pytest

from app.claim_gate import (
    CONTRADICTION_JSON_SCHEMA,
    GATE_JSON_SCHEMA,
    GateError,
    parse_contradiction_judgement,
    parse_gate_result,
)
from app.llm_local import (
    LOCAL_GATE_MAX_TOKENS,
    OLLAMA_DEFAULT_API_KEY,
    LocalClaimGate,
    create_local_client,
)
from tests.conftest import (
    FakeLocalClient,
    make_chat_completion,
    make_openrouter_status_error,
    make_openrouter_timeout_error,
)

LOCAL_BASE_URL = "http://127.0.0.1:1/v1"

GATE_CLAIMS_JSON = (
    '{"claims": [{"claim_text": "The Eiffel Tower is 330 meters tall.",'
    ' "check_worthiness": 0.9, "topic": "history"}]}'
)

JUDGEMENT_JSON = (
    '{"contradicts": true, "confidence": "high",'
    ' "explanation": "The heights conflict."}'
)


def make_connection_error() -> openai.APIConnectionError:
    return openai.APIConnectionError(
        request=httpx.Request("POST", f"{LOCAL_BASE_URL}/chat/completions")
    )


@pytest.fixture()
def gate(fake_local_client: FakeLocalClient) -> LocalClaimGate:
    return LocalClaimGate(
        client=fake_local_client,  # type: ignore[arg-type] — duck-typed fake
        model="fake-local-model",
        base_url=LOCAL_BASE_URL,
        gate_interval_s=12.0,
        gate_timeout_s=0.5,
    )


class TestStrictMode:
    async def test_happy_path_call_shape(
        self, gate: LocalClaimGate, fake_local_client: FakeLocalClient
    ) -> None:
        fake_local_client.completion_results.append(
            make_chat_completion(GATE_CLAIMS_JSON)
        )
        claims = await gate.extract_claims("", "some transcript text")
        assert [claim.claim_text for claim in claims] == [
            "The Eiffel Tower is 330 meters tall."
        ]
        call = fake_local_client.completion_calls[0]
        assert call["model"] == "fake-local-model"
        assert call["temperature"] == 0.0
        assert call["max_tokens"] == LOCAL_GATE_MAX_TOKENS
        assert call["response_format"] == {
            "type": "json_schema",
            "json_schema": GATE_JSON_SCHEMA,
        }
        # No OpenRouter-isms: no plugins/reasoning/provider extra_body.
        assert "extra_body" not in call

    async def test_structural_rejection_latches_json_object_mode(
        self, gate: LocalClaimGate, fake_local_client: FakeLocalClient
    ) -> None:
        fake_local_client.completion_results.append(
            make_openrouter_status_error(400, "response_format not supported")
        )
        fake_local_client.completion_results.append(
            make_chat_completion(GATE_CLAIMS_JSON)
        )
        claims = await gate.extract_claims("", "text one")
        assert len(claims) == 1
        assert LocalClaimGate._json_schema_unsupported is True
        # Second call skips strict mode entirely.
        fake_local_client.completion_results.append(
            make_chat_completion(GATE_CLAIMS_JSON)
        )
        await gate.extract_claims("", "text two")
        formats = [
            call["response_format"]["type"]
            for call in fake_local_client.completion_calls
        ]
        assert formats == ["json_schema", "json_object", "json_object"]

    async def test_non_structural_status_is_gate_error_without_latch(
        self, gate: LocalClaimGate, fake_local_client: FakeLocalClient
    ) -> None:
        fake_local_client.completion_results.append(
            make_openrouter_status_error(500, "server exploded")
        )
        with pytest.raises(GateError, match="500"):
            await gate.extract_claims("", "text")
        assert LocalClaimGate._json_schema_unsupported is False

    async def test_timeout_is_gate_error(
        self, gate: LocalClaimGate, fake_local_client: FakeLocalClient
    ) -> None:
        fake_local_client.completion_results.append(make_openrouter_timeout_error())
        with pytest.raises(GateError, match="timed out"):
            await gate.extract_claims("", "text")

    async def test_connection_error_names_the_base_url(
        self, gate: LocalClaimGate, fake_local_client: FakeLocalClient
    ) -> None:
        fake_local_client.completion_results.append(make_connection_error())
        with pytest.raises(GateError, match=r"127\.0\.0\.1:1/v1"):
            await gate.extract_claims("", "text")

    async def test_json_object_mode_tolerates_fenced_output(
        self, gate: LocalClaimGate, fake_local_client: FakeLocalClient
    ) -> None:
        LocalClaimGate._json_schema_unsupported = True
        fake_local_client.completion_results.append(
            make_chat_completion(f"Sure!\n```json\n{GATE_CLAIMS_JSON}\n```")
        )
        claims = await gate.extract_claims("", "text")
        assert len(claims) == 1


class TestJudgeContradiction:
    async def test_happy_path_shares_strict_schema(
        self, gate: LocalClaimGate, fake_local_client: FakeLocalClient
    ) -> None:
        fake_local_client.completion_results.append(
            make_chat_completion(JUDGEMENT_JSON)
        )
        judgement = await gate.judge_contradiction(
            "I have never been to Paris.", "I lived in Paris for a year."
        )
        assert judgement.contradicts is True
        assert judgement.confidence == "high"
        call = fake_local_client.completion_calls[0]
        assert call["response_format"] == {
            "type": "json_schema",
            "json_schema": CONTRADICTION_JSON_SCHEMA,
        }
        prompt = call["messages"][0]["content"]
        assert "I have never been to Paris." in prompt
        assert "I lived in Paris for a year." in prompt

    async def test_judge_uses_json_object_after_latch(
        self, gate: LocalClaimGate, fake_local_client: FakeLocalClient
    ) -> None:
        LocalClaimGate._json_schema_unsupported = True
        fake_local_client.completion_results.append(
            make_chat_completion(JUDGEMENT_JSON)
        )
        await gate.judge_contradiction("a", "b")
        call = fake_local_client.completion_calls[0]
        assert call["response_format"] == {"type": "json_object"}

    async def test_failure_is_gate_error(
        self, gate: LocalClaimGate, fake_local_client: FakeLocalClient
    ) -> None:
        fake_local_client.completion_results.append(
            make_openrouter_status_error(500, "boom")
        )
        with pytest.raises(GateError):
            await gate.judge_contradiction("a", "b")


class TestCreateLocalClient:
    async def test_client_parameters(self) -> None:
        client = create_local_client(LOCAL_BASE_URL)
        try:
            assert str(client.base_url).rstrip("/") == LOCAL_BASE_URL
            assert client.api_key == OLLAMA_DEFAULT_API_KEY
            assert client.max_retries == 0
        finally:
            await client.close()


class TestSharedParseHelpers:
    def test_parse_gate_result_rejects_prose(self) -> None:
        with pytest.raises(GateError, match="no JSON object"):
            parse_gate_result("I could not find any claims, sorry!")

    def test_parse_gate_result_rejects_schema_mismatch(self) -> None:
        with pytest.raises(GateError, match="schema validation"):
            parse_gate_result('{"claims": [{"claim_text": 42}]}')

    def test_parse_judgement_tolerates_fences_and_prose(self) -> None:
        judgement = parse_contradiction_judgement(
            f"Here's my analysis:\n```json\n{JUDGEMENT_JSON}\n```"
        )
        assert judgement.contradicts is True

    def test_parse_judgement_rejects_empty(self) -> None:
        with pytest.raises(GateError, match="no text"):
            parse_contradiction_judgement("")
