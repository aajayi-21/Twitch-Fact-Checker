"""Jev pre-screen: the real SDK's Decisions HTTP path through MockTransport."""

import asyncio
import json
from datetime import date
from unittest.mock import MagicMock

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import ValidationError

from app.claim_gate import ClaimGate, GateError, GatePass
from app.config import DEFAULT_JEV_MODEL, Settings
from app.llm_jev import (
    JEV_DECISIONS_URL,
    JEV_QUESTIONS,
    JevScreenedGate,
    build_jev_body,
)
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
        "model": "typesafe/jev-1.13-20260917",
    }


def segment(text: str) -> TranscriptSegment:
    return TranscriptSegment(
        text=text, start=0, end=1, avg_logprob=-0.1, no_speech_prob=0.0
    )


@pytest.fixture
async def build_gate():
    clients = []

    def build(
        payload=None, *, status=200, raw=None, handler=None, mode="screen", **kwargs
    ):
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
        gate = JevScreenedGate(
            client, DEFAULT_JEV_MODEL, extractor, mode=mode, **kwargs
        )
        return gate, extractor, requests

    yield build
    for client in clients:
        await client.close()


async def run_pass(gate: JevScreenedGate, text: str = "Fresh speech here"):
    """One recorded gate pass through run(), like the session pipeline."""
    gate.add_transcript(segment(text))
    return await gate.run()


class TestWireContract:
    async def test_decisions_body_and_extraction(self, build_gate) -> None:
        gate, extractor, requests = build_gate()
        claims = await gate.extract_claims("The Eiffel Tower", "It is 450 m tall.")
        assert claims == [CLAIM]
        extractor.extract_claims.assert_awaited_once_with(
            "The Eiffel Tower", "It is 450 m tall."
        )
        assert len(requests) == 1
        request = requests[0]
        assert str(request.url) == JEV_DECISIONS_URL
        assert request.headers["authorization"] == "Bearer sk-offline-test"
        body = json.loads(request.content)
        assert body == build_jev_body(
            DEFAULT_JEV_MODEL, "The Eiffel Tower", "It is 450 m tall."
        )
        assert body["model"] == "typesafe/jev-1.13"
        assert body["questions"] == JEV_QUESTIONS
        assert body["state"]["context"] == "The Eiffel Tower"
        assert body["state"]["new_transcript"] == "It is 450 m tall."
        date.fromisoformat(body["state"]["current_date"])

    def test_constructor_rejects_bad_modes_and_timeouts(self) -> None:
        extractor = MagicMock(spec=ClaimGate)
        with pytest.raises(ValueError, match="mode"):
            JevScreenedGate(MagicMock(), DEFAULT_JEV_MODEL, extractor, mode="off")
        with pytest.raises(ValueError, match="jev_timeout_s"):
            JevScreenedGate(
                MagicMock(),
                DEFAULT_JEV_MODEL,
                extractor,
                mode="screen",
                jev_timeout_s=15.0,
                gate_timeout_s=15.0,
            )


class TestScreenMode:
    @pytest.mark.parametrize(
        ("probability", "expected"),
        [(0, False), (0.349, False), (0.35, True), (0.5, True), (1, True)],
    )
    async def test_threshold_boundary(self, build_gate, probability, expected) -> None:
        gate, extractor, _ = build_gate(answer(probability))
        claims = await run_pass(gate)
        assert claims == ([CLAIM] if expected else [])
        assert extractor.extract_claims.await_count == int(expected)
        screen = gate.last_pass.screen
        assert screen.route == ("extract" if expected else "skip")
        assert screen.probability == probability
        assert screen.resolved_model == "typesafe/jev-1.13-20260917"
        assert screen.threshold == 0.35
        assert gate.last_pass.claims_count == int(expected)

    async def test_configurable_cutoff(self, build_gate) -> None:
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
    async def test_malformed_answer_fails_open(self, build_gate, payload) -> None:
        gate, extractor, requests = build_gate(payload)
        assert await run_pass(gate) == [CLAIM]
        extractor.extract_claims.assert_awaited_once()
        assert len(requests) == 1
        screen = gate.last_pass.screen
        assert screen.route == "fail_open"
        assert screen.probability is None
        assert screen.error == "response failed schema validation"

    @pytest.mark.parametrize(
        "raw",
        [
            "not json",
            "null",
            "[]",
            '{"answers":{"needs_fact_check":{"type":"noul","noul":NaN}},'
            '"model":"jev"}',
        ],
    )
    async def test_invalid_json_fails_open(self, build_gate, raw) -> None:
        gate, extractor, _ = build_gate(raw=raw)
        assert await run_pass(gate) == [CLAIM]
        assert gate.last_pass.screen.route == "fail_open"
        extractor.extract_claims.assert_awaited_once()

    @pytest.mark.parametrize("status", [400, 401, 402, 403, 429, 503])
    async def test_http_errors_fail_open_without_retry(
        self, build_gate, status
    ) -> None:
        gate, extractor, requests = build_gate(
            {"error": {"message": "failed"}}, status=status
        )
        assert await run_pass(gate) == [CLAIM]
        assert len(requests) == 1
        screen = gate.last_pass.screen
        assert screen.route == "fail_open"
        assert screen.error.startswith(f"HTTP {status}")

    async def test_jev_timeout_fails_open_within_the_gate_deadline(
        self, build_gate
    ) -> None:
        async def slow_response(request):
            await asyncio.sleep(1)
            return httpx.Response(200, json=answer())

        gate, extractor, _ = build_gate(
            handler=slow_response, jev_timeout_s=0.02, gate_timeout_s=5.0
        )
        assert await run_pass(gate) == [CLAIM]
        screen = gate.last_pass.screen
        assert screen.route == "fail_open"
        assert "timed out" in screen.error
        extractor.extract_claims.assert_awaited_once()

    async def test_slow_extraction_hits_the_shared_deadline(self, build_gate) -> None:
        gate, extractor, _ = build_gate(jev_timeout_s=0.01, gate_timeout_s=0.05)

        async def slow_extract(*args):
            await asyncio.sleep(1)
            return [CLAIM]

        extractor.extract_claims.side_effect = slow_extract
        with pytest.raises(GateError, match="timed out"):
            await run_pass(gate)
        # The Jev decision survives the extraction failure.
        assert gate.last_pass.screen.route == "extract"
        assert gate.last_pass.error is not None

    async def test_extraction_failure_propagates_after_one_decision(
        self, build_gate
    ) -> None:
        gate, extractor, requests = build_gate()
        extractor.extract_claims.side_effect = GateError("bad extraction")
        with pytest.raises(GateError, match="bad extraction"):
            await run_pass(gate)
        assert len(requests) == 1
        assert gate.last_pass.error == "bad extraction"

    async def test_cancellation_propagates(self, build_gate) -> None:
        gate, extractor, _ = build_gate()
        extractor.extract_claims.side_effect = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await gate.extract_claims("", "Fresh speech")


class TestShadowMode:
    @pytest.mark.parametrize("probability", [0.0, 0.2, 0.9])
    async def test_claims_are_unaffected_and_answer_recorded(
        self, build_gate, probability
    ) -> None:
        gate, extractor, requests = build_gate(answer(probability), mode="shadow")
        assert await run_pass(gate) == [CLAIM]
        extractor.extract_claims.assert_awaited_once()
        assert len(requests) == 1
        screen = gate.last_pass.screen
        assert (screen.mode, screen.route) == ("shadow", "shadow")
        assert screen.probability == probability
        assert screen.latency_ms is not None

    async def test_jev_failure_changes_nothing(self, build_gate) -> None:
        gate, _, _ = build_gate(
            {"error": {"message": "down"}}, status=503, mode="shadow"
        )
        assert await run_pass(gate) == [CLAIM]
        screen = gate.last_pass.screen
        assert screen.route == "shadow"
        assert screen.probability is None
        assert screen.error.startswith("HTTP 503")

    async def test_extraction_failure_cancels_and_awaits_the_jev_call(
        self, build_gate
    ) -> None:
        jev_started = asyncio.Event()
        jev_cancelled = asyncio.Event()

        async def hanging_response(request):
            jev_started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                jev_cancelled.set()
                raise
            return httpx.Response(200, json=answer())

        gate, extractor, _ = build_gate(handler=hanging_response, mode="shadow")

        async def failing_extract(*args):
            await jev_started.wait()
            raise GateError("extraction broke")

        extractor.extract_claims.side_effect = failing_extract
        with pytest.raises(GateError, match="extraction broke"):
            await run_pass(gate)
        assert jev_cancelled.is_set()
        assert gate.last_pass.screen.route == "cancelled"
        assert not [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        ]


class TestGateSurface:
    async def test_batch_drain_tail_and_recovery_after_error(self, build_gate) -> None:
        gate, extractor, requests = build_gate()
        extractor.extract_claims.side_effect = GateError("down")
        first = " ".join(f"word{i}" for i in range(50))
        gate.add_transcript(segment(first))
        with pytest.raises(GateError):
            await gate.run()
        assert await gate.run() == []
        assert gate.last_pass is None  # empty-buffer run records nothing
        assert gate.calls_made == 1
        gate.add_transcript(segment("new assertion"))
        with pytest.raises(GateError):
            await gate.run()
        state = json.loads(requests[-1].content)["state"]
        assert state["context"] == " ".join(first.split()[-40:])
        assert state["new_transcript"] == "new assertion"
        assert gate.calls_made == 2

    async def test_empty_text_short_circuits_and_contradiction_delegates(
        self, build_gate
    ) -> None:
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

    async def test_debug_path_records_nothing(self, build_gate) -> None:
        gate, _, _ = build_gate()
        await gate.extract_claims("", "Fresh speech")
        assert gate.last_pass is None

    async def test_mixed_claims_keep_individual_topics_and_scores(
        self, build_gate
    ) -> None:
        gate, extractor, _ = build_gate()
        mixed = [
            CLAIM,
            GateClaim(claim_text="Claim two", topic="health", check_worthiness=0.2),
        ]
        extractor.extract_claims.return_value = mixed
        assert await gate.extract_claims("", "Mixed text") == mixed

    async def test_error_text_never_contains_transcript(self, build_gate) -> None:
        secret = "Supersecret transcript words"
        gate, _, _ = build_gate({"answers": {"needs_fact_check": secret}})
        await run_pass(gate, secret)
        assert secret not in (gate.last_pass.screen.error or "")


class TestFactoryAndHealth:
    @pytest.mark.parametrize("mode", ["shadow", "screen"])
    def test_factory_wraps_the_openrouter_gate(self, mode) -> None:
        settings = Settings(_env_file=None, jev_mode=mode, openrouter_api_key="test")
        gate = create_claim_gate(settings, MagicMock())
        assert isinstance(gate, JevScreenedGate)
        assert gate.mode == mode
        assert isinstance(gate._extractor, OpenRouterClaimGate)
        assert gate._extractor._model == settings.openrouter_gate_model
        health = _openrouter_health(settings)
        assert health["decision_gate"]["mode"] == mode
        assert health["decision_gate"]["model"] == DEFAULT_JEV_MODEL
        assert DEFAULT_JEV_MODEL not in health["capabilities"]

    def test_off_is_the_plain_gate(self) -> None:
        settings = Settings(_env_file=None)
        assert settings.jev_mode == "off"
        assert isinstance(create_claim_gate(settings, MagicMock()), OpenRouterClaimGate)
        assert _openrouter_health(settings)["decision_gate"]["mode"] == "off"

    def test_non_openrouter_gate_makes_jev_inert(self) -> None:
        settings = Settings(
            _env_file=None,
            jev_mode="screen",
            gate_provider="ollama",
            verify_provider="openrouter",
        )
        assert settings.jev_active_mode == "off"
        gate = create_claim_gate(settings, MagicMock())
        assert not isinstance(gate, JevScreenedGate)


class TestSettings:
    @pytest.mark.parametrize(
        "field", ["openrouter_gate_model", "openrouter_verify_model"]
    )
    @pytest.mark.parametrize("slug", ["~typesafe/jev-latest", "typesafe/jev-1.13"])
    def test_jev_cannot_be_a_chat_model(self, field, slug) -> None:
        with pytest.raises(ValidationError, match="JEV_MODE=shadow"):
            Settings(_env_file=None, **{field: slug})

    @pytest.mark.parametrize(
        "slug", ["typesafe/jev-1.13", "typesafe/jev-1.13-20260917", "typesafe/jev-2.0"]
    )
    def test_pinned_jev_models_accepted(self, slug) -> None:
        assert Settings(_env_file=None, jev_model=slug).jev_model == slug

    @pytest.mark.parametrize(
        "slug", ["~typesafe/jev-latest", "typesafe/jev-latest", "openai/gpt-oss-120b"]
    )
    def test_floating_or_foreign_jev_model_rejected(self, slug) -> None:
        with pytest.raises(ValidationError, match="pinned Jev release"):
            Settings(_env_file=None, jev_model=slug)

    def test_jev_timeout_must_leave_room_for_extraction(self) -> None:
        with pytest.raises(ValidationError, match="JEV_TIMEOUT_S"):
            Settings(
                _env_file=None, jev_mode="screen", jev_timeout_s=15, gate_timeout_s=15
            )
        # Irrelevant (and allowed) while Jev is off.
        Settings(_env_file=None, jev_timeout_s=15, gate_timeout_s=15)

    @pytest.mark.parametrize("value", [-0.1, 1.1, float("nan"), float("inf")])
    def test_invalid_threshold_rejected(self, value) -> None:
        with pytest.raises(ValidationError):
            Settings(_env_file=None, jev_min_check_probability=value)

    def test_gate_pass_record_word_count(self) -> None:
        assert GatePass(context="", new_text="one two  three").word_count == 3
