"""Exercise the real SDK's Decisions HTTP path entirely through MockTransport."""

import asyncio
import json
from datetime import date
from unittest.mock import MagicMock

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import ValidationError

from app.claim_gate import ClaimGate, GateError
from app.config import (
    DEFAULT_OPENROUTER_GATE_MODEL,
    JEV_MODELS,
    Settings,
)
from app.llm_jev import JEV_DECISIONS_URL, JEV_QUESTIONS, JevClaimGate
from app.llm_openrouter import OPENROUTER_BASE_URL, OpenRouterClaimGate
from app.llm_provider import create_claim_gate
from app.main import _openrouter_health
from app.models import ContradictionJudgement, GateClaim, TranscriptSegment

CLAIM = GateClaim(
    claim_text="The Eiffel Tower is 450 meters tall.",
    check_worthiness=0.9,
    topic="other",
)


def answer(probability=0.9):
    return {
        "answers": {"needs_fact_check": {"type": "noul", "noul": probability}},
        "model": "typesafe/jev-1.13-resolved",
    }


@pytest.fixture
async def build_gate():
    clients = []

    def build(payload=None, *, status=200, raw=None, handler=None, **kwargs):
        requests = []

        async def respond(request):
            requests.append(request)
            if handler is not None:
                return await handler(request)
            if raw is not None:
                return httpx.Response(status, text=raw)
            return httpx.Response(status, json=answer() if payload is None else payload)

        client = AsyncOpenAI(
            api_key="sk-offline-test",
            base_url=OPENROUTER_BASE_URL,
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        )
        clients.append(client)
        extractor = MagicMock(spec=ClaimGate)
        extractor.extract_claims.return_value = [CLAIM]
        gate = JevClaimGate(client, DEFAULT_OPENROUTER_GATE_MODEL, extractor, **kwargs)
        return gate, extractor, requests

    yield build
    for client in clients:
        await client.close()


async def test_decisions_wire_contract_and_extraction(build_gate):
    gate, extractor, requests = build_gate()
    claims = await gate.extract_claims("The Eiffel Tower", "It is 450 meters tall.")
    assert claims == [CLAIM]
    extractor.extract_claims.assert_awaited_once_with(
        "The Eiffel Tower", "It is 450 meters tall."
    )
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == JEV_DECISIONS_URL
    assert request.headers["authorization"] == "Bearer sk-offline-test"
    body = json.loads(request.content)
    assert set(body) == {"model", "state", "questions"}
    assert body["model"] == DEFAULT_OPENROUTER_GATE_MODEL
    assert body["questions"] == JEV_QUESTIONS
    assert body["state"]["context"] == "The Eiffel Tower"
    assert body["state"]["new_transcript"] == "It is 450 meters tall."
    date.fromisoformat(body["state"]["current_date"])


@pytest.mark.parametrize(
    ("probability", "expected"),
    [(0, False), (0.349, False), (0.35, True), (0.5, True), (1, True)],
)
async def test_probability_boundary_and_uncertain_route(
    build_gate, probability, expected
):
    gate, extractor, _ = build_gate(answer(probability))
    claims = await gate.extract_claims("", "A statement to classify")
    assert claims == ([CLAIM] if expected else [])
    assert extractor.extract_claims.await_count == int(expected)


async def test_configurable_cutoff(build_gate):
    gate, extractor, _ = build_gate(answer(0.5), min_check_probability=0.6)
    assert await gate.extract_claims("", "Fresh speech") == []
    extractor.extract_claims.assert_not_awaited()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"answers": {}, "model": "jev"},
        {
            "answers": {"needs_fact_check": {"type": "choice", "noul": 0.9}},
            "model": "jev",
        },
        answer(-0.1),
        answer(1.1),
        answer("0.9"),
        answer(True),
        answer(None),
    ],
)
async def test_malformed_answer_drops_without_fallback(build_gate, payload):
    gate, extractor, requests = build_gate(payload)
    with pytest.raises(GateError, match="schema validation"):
        await gate.extract_claims("", "Fresh speech")
    extractor.extract_claims.assert_not_awaited()
    assert len(requests) == 1


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "null",
        "[]",
        '{"answers":{"needs_fact_check":{"type":"noul","noul":NaN}},"model":"jev"}',
    ],
)
async def test_invalid_json_and_nonfinite_probability(build_gate, raw):
    gate, extractor, _ = build_gate(raw=raw)
    with pytest.raises(GateError):
        await gate.extract_claims("", "Fresh speech")
    extractor.extract_claims.assert_not_awaited()


@pytest.mark.parametrize("status", [400, 401, 402, 403, 429, 503])
async def test_http_errors_do_not_retry_or_extract(build_gate, status):
    gate, extractor, requests = build_gate(
        {"error": {"message": "failed"}}, status=status
    )
    with pytest.raises(GateError, match=str(status)):
        await gate.extract_claims("", "Fresh speech")
    extractor.extract_claims.assert_not_awaited()
    assert len(requests) == 1


@pytest.mark.parametrize("during_extraction", [False, True])
async def test_total_timeout_covers_both_stages(build_gate, during_extraction):
    async def slow_response(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json=answer())

    gate, extractor, _ = build_gate(
        handler=None if during_extraction else slow_response, gate_timeout_s=0.01
    )
    if during_extraction:

        async def slow_extract(*args):
            await asyncio.sleep(1)
            return [CLAIM]

        extractor.extract_claims.side_effect = slow_extract
    with pytest.raises(GateError, match="timed out"):
        await gate.extract_claims("", "Fresh speech")
    assert extractor.extract_claims.await_count == int(during_extraction)


async def test_cancellation_propagates(build_gate):
    gate, extractor, _ = build_gate()
    extractor.extract_claims.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await gate.extract_claims("", "Fresh speech")


async def test_extraction_failure_propagates_without_another_decision(build_gate):
    gate, extractor, requests = build_gate()
    extractor.extract_claims.side_effect = GateError("bad extraction")
    with pytest.raises(GateError, match="bad extraction"):
        await gate.extract_claims("", "Fresh speech")
    assert len(requests) == 1


async def test_batch_drain_tail_and_recovery_after_error(build_gate):
    gate, _, requests = build_gate(status=503)
    first = " ".join(f"word{i}" for i in range(50))
    gate.add_transcript(
        TranscriptSegment(text=first, start=0, end=5, avg_logprob=0, no_speech_prob=0)
    )
    with pytest.raises(GateError):
        await gate.run()
    assert await gate.run() == []
    assert gate.calls_made == 1
    gate.add_transcript(
        TranscriptSegment(
            text="new assertion", start=5, end=6, avg_logprob=0, no_speech_prob=0
        )
    )
    # run() bypasses cadence/min words for the graceful-stop flush.
    with pytest.raises(GateError):
        await gate.run()
    state = json.loads(requests[-1].content)["state"]
    assert state["context"] == " ".join(first.split()[-40:])
    assert state["new_transcript"] == "new assertion"
    assert gate.calls_made == 2


async def test_empty_text_short_circuits_and_contradiction_delegates(build_gate):
    gate, extractor, requests = build_gate()
    assert await gate.extract_claims("Old claim", "  ") == []
    assert requests == []
    expected = ContradictionJudgement(
        contradicts=True, confidence="high", explanation="Opposite assertions"
    )
    extractor.judge_contradiction.return_value = expected
    assert await gate.judge_contradiction("A", "B") == expected
    extractor.judge_contradiction.assert_awaited_once_with("A", "B")
    assert requests == []


async def test_mixed_claims_keep_individual_topics_and_scores(build_gate):
    gate, extractor, _ = build_gate()
    mixed = [
        CLAIM,
        GateClaim(claim_text="Claim two", topic="health", check_worthiness=0.2),
    ]
    extractor.extract_claims.return_value = mixed
    assert await gate.extract_claims("", "Mixed text") == mixed


@pytest.mark.parametrize("model", sorted(JEV_MODELS))
def test_factory_and_health_distinguish_decisions_from_chat(model):
    settings = Settings(
        _env_file=None, openrouter_gate_model=model, openrouter_api_key="test"
    )
    gate = create_claim_gate(settings, MagicMock())
    assert isinstance(gate, JevClaimGate)
    assert isinstance(gate._extractor, OpenRouterClaimGate)
    assert gate._extractor._model == settings.openrouter_extraction_model
    health = _openrouter_health(settings)
    assert model not in health["capabilities"]
    assert (
        health["decision_gate"]["extraction_model"]
        == settings.openrouter_extraction_model
    )


def test_legacy_model_retains_original_gate():
    settings = Settings(
        _env_file=None, openrouter_gate_model="inception/mercury-2.5-preview"
    )
    assert isinstance(create_claim_gate(settings, MagicMock()), OpenRouterClaimGate)
    assert _openrouter_health(settings)["decision_gate"] is None


@pytest.mark.parametrize(
    "field", ["openrouter_extraction_model", "openrouter_verify_model"]
)
def test_decisions_model_cannot_generate(field):
    with pytest.raises(ValidationError, match="gate decisions"):
        Settings(_env_file=None, **{field: DEFAULT_OPENROUTER_GATE_MODEL})


@pytest.mark.parametrize("value", [-0.1, 1.1, float("nan"), float("inf")])
def test_invalid_threshold_rejected(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, jev_min_check_probability=value)
