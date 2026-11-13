"""Per-call usage: reading provider responses, collection scope, wire frames."""

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient
from openai.types.chat import ChatCompletion
from openai.types.completion_usage import CompletionUsage

from app.models import TranscriptSegment
from app.usage import (
    UsageEvent,
    anthropic_usage,
    chat_completion_usage,
    collect_usage,
    report_usage,
)
from tests.conftest import (
    FakeLLMClient,
    FakeTranscriber,
    anthropic_text_block,
    make_anthropic_message,
    make_chat_completion,
    make_gate_response,
    make_hello,
    make_verdict_completion,
    pcm_silence,
)
from tests.test_pipeline_persistence import collect_frames_until_close

GATE_EVENT = UsageEvent(stage="gate", provider="anthropic", model="claude-haiku-5-5")


def with_usage(completion: ChatCompletion, **usage: Any) -> ChatCompletion:
    """``completion`` with a usage block, validated by the SDK's own model.

    ``cost`` and ``server_tool_use`` are OpenRouter additions the SDK model
    does not declare; it keeps them as extras, exactly as on a live response.
    """
    return completion.model_copy(
        update={"usage": CompletionUsage.model_validate(usage)}
    )


class TestAnthropicUsage:
    def test_reads_tokens_cache_and_searches(self) -> None:
        message = make_anthropic_message(
            [anthropic_text_block("{}")], web_search_requests=2
        )
        message.usage.cache_read_input_tokens = 2959
        message.usage.cache_creation_input_tokens = 40
        event = anthropic_usage(message, "verify", "claude-haiku-5-5")
        assert event == UsageEvent(
            stage="verify",
            provider="anthropic",
            model="claude-haiku-5-5",
            # Full-rate input = uncached input + tokens written to the cache.
            input_tokens=140,
            cached_input_tokens=2959,
            output_tokens=50,
            web_searches=2,
            cost_usd=None,
        )

    def test_response_without_usage_reports_nothing(self) -> None:
        assert anthropic_usage(object(), "gate", "claude-haiku-5-5") is None


class TestChatCompletionUsage:
    def test_subtracts_cached_tokens_and_keeps_exact_cost(self) -> None:
        completion = with_usage(
            make_chat_completion("{}"),
            prompt_tokens=2100,
            completion_tokens=90,
            total_tokens=2190,
            prompt_tokens_details={"cached_tokens": 600},
            cost=0.0078,
        )
        event = chat_completion_usage(
            completion, "verify", "openrouter", "some/model", assumed_web_searches=1
        )
        assert event == UsageEvent(
            stage="verify",
            provider="openrouter",
            model="some/model",
            input_tokens=1500,
            cached_input_tokens=600,
            output_tokens=90,
            web_searches=1,
            cost_usd=0.0078,
        )

    def test_reported_search_count_beats_the_assumption(self) -> None:
        completion = with_usage(
            make_chat_completion("{}"),
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            server_tool_use={"web_search_requests": 3},
        )
        event = chat_completion_usage(
            completion, "verify", "openrouter", "some/model", assumed_web_searches=1
        )
        assert event is not None and event.web_searches == 3

    def test_missing_cost_stays_none(self) -> None:
        completion = with_usage(
            make_chat_completion("{}"),
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
        )
        event = chat_completion_usage(completion, "gate", "ollama", "llama")
        assert event is not None
        assert event.cost_usd is None
        assert event.web_searches == 0

    def test_completion_without_usage_reports_nothing(self) -> None:
        completion = make_chat_completion("{}")
        assert chat_completion_usage(completion, "gate", "openrouter", "m") is None


class TestCollectUsage:
    def test_reporting_outside_a_scope_is_a_no_op(self) -> None:
        report_usage(GATE_EVENT)  # must not raise

    def test_collects_what_was_reported_inside_the_block(self) -> None:
        with collect_usage() as events:
            report_usage(GATE_EVENT)
            report_usage(GATE_EVENT)
        report_usage(GATE_EVENT)  # after the block: not collected
        assert events == [GATE_EVENT, GATE_EVENT]

    def test_keeps_events_when_the_block_raises(self) -> None:
        with pytest.raises(RuntimeError):
            with collect_usage() as events:
                report_usage(GATE_EVENT)
                raise RuntimeError("verify failed after being billed")
        assert events == [GATE_EVENT]

    def test_nested_scopes_restore_the_outer_one(self) -> None:
        with collect_usage() as outer:
            with collect_usage() as inner:
                report_usage(GATE_EVENT)
            report_usage(GATE_EVENT)
        assert len(inner) == 1
        assert len(outer) == 1

    async def test_concurrent_tasks_collect_independently(self) -> None:
        async def run_call(model: str) -> list[UsageEvent]:
            with collect_usage() as events:
                await asyncio.sleep(0)  # let the other task interleave
                report_usage(
                    UsageEvent(stage="verify", provider="anthropic", model=model)
                )
                await asyncio.sleep(0)
            return events

        first, second = await asyncio.gather(run_call("a"), run_call("b"))
        assert [event.model for event in first] == ["a"]
        assert [event.model for event in second] == ["b"]


class TestUsageFramesOnTheWire:
    def test_gate_and_verify_calls_each_send_a_usage_frame(
        self,
        client: TestClient,
        fake_llm_client: FakeLLMClient,
        fake_transcriber: FakeTranscriber,
    ) -> None:
        fake_transcriber.segments_script.append(
            [
                TranscriptSegment(
                    text="the eiffel tower is three hundred thirty meters",
                    start=0.0,
                    end=1.0,
                    avg_logprob=-0.3,
                    no_speech_prob=0.05,
                )
            ]
        )
        fake_llm_client.gate_results.append(
            with_usage(
                make_gate_response(
                    [("The Eiffel Tower is 330 meters tall.", 0.9, "history")]
                ),
                prompt_tokens=3200,
                completion_tokens=40,
                total_tokens=3240,
                cost=0.0004,
            )
        )
        fake_llm_client.verify_results.append(
            with_usage(
                make_verdict_completion("TRUE", "Official figure."),
                prompt_tokens=2100,
                completion_tokens=90,
                total_tokens=2190,
                cost=0.0078,
            )
        )
        with client.websocket_connect("/ws/audio") as session:
            session.send_json(make_hello())
            assert session.receive_json()["type"] == "ready"
            for _ in range(4):
                session.send_bytes(pcm_silence(0.25))
            session.send_json({"type": "stop"})
            frames, close_code = collect_frames_until_close(session)
        assert close_code == 1000

        verdicts = [frame for frame in frames if frame["type"] == "verdict"]
        assert len(verdicts) == 1
        verdict = verdicts[0]
        # Provenance for the viewer's history rides on the verdict frame.
        assert verdict["provider"] == "openrouter"
        assert isinstance(verdict["model"], str) and verdict["model"]
        assert isinstance(verdict["claim_id"], str) and verdict["claim_id"]
        assert verdict["latency_ms"] >= 0

        usage = {frame["stage"]: frame for frame in frames if frame["type"] == "usage"}
        assert set(usage) == {"gate", "verify"}
        assert usage["gate"]["input_tokens"] == 3200
        assert usage["gate"]["cost_usd"] == 0.0004
        assert usage["gate"]["web_searches"] == 0
        assert "verdict_id" not in usage["gate"]
        assert usage["verify"]["cost_usd"] == 0.0078
        # The web plugin searched once; the response did not count it.
        assert usage["verify"]["web_searches"] == 1
        assert usage["verify"]["verdict_id"] == verdict["id"]
        # The usage frame follows its verdict, so a client can link them.
        assert frames.index(usage["verify"]) > frames.index(verdict)

    def test_calls_without_a_usage_block_send_no_usage_frame(
        self,
        client: TestClient,
        fake_llm_client: FakeLLMClient,
        fake_transcriber: FakeTranscriber,
    ) -> None:
        fake_transcriber.segments_script.append(
            [
                TranscriptSegment(
                    text="the eiffel tower is three hundred thirty meters",
                    start=0.0,
                    end=1.0,
                    avg_logprob=-0.3,
                    no_speech_prob=0.05,
                )
            ]
        )
        fake_llm_client.gate_results.append(make_gate_response([]))
        with client.websocket_connect("/ws/audio") as session:
            session.send_json(make_hello())
            assert session.receive_json()["type"] == "ready"
            for _ in range(4):
                session.send_bytes(pcm_silence(0.25))
            session.send_json({"type": "stop"})
            frames, _close_code = collect_frames_until_close(session)
        assert not [frame for frame in frames if frame["type"] == "usage"]


class TestClaimFrames:
    """Opt-in per-claim outcome frames (ClientHello.send_claims)."""

    def _run_session(
        self,
        client: TestClient,
        fake_llm_client: FakeLLMClient,
        fake_transcriber: FakeTranscriber,
        **hello_overrides: Any,
    ) -> list[dict[str, Any]]:
        fake_transcriber.segments_script.append(
            [
                TranscriptSegment(
                    text="the eiffel tower is three hundred thirty meters",
                    start=0.0,
                    end=1.0,
                    avg_logprob=-0.3,
                    no_speech_prob=0.05,
                )
            ]
        )
        fake_llm_client.gate_results.append(
            make_gate_response(
                [
                    ("Politicians always lie about their tax plans.", 0.2, "politics"),
                    ("The Eiffel Tower is 330 meters tall.", 0.9, "history"),
                ]
            )
        )
        fake_llm_client.verify_results.append(
            make_verdict_completion("TRUE", "Official figure.")
        )
        with client.websocket_connect("/ws/audio") as session:
            session.send_json(make_hello(**hello_overrides))
            assert session.receive_json()["type"] == "ready"
            for _ in range(4):
                session.send_bytes(pcm_silence(0.25))
            session.send_json({"type": "stop"})
            frames, close_code = collect_frames_until_close(session)
        assert close_code == 1000
        return frames

    def test_off_by_default(
        self,
        client: TestClient,
        fake_llm_client: FakeLLMClient,
        fake_transcriber: FakeTranscriber,
    ) -> None:
        frames = self._run_session(client, fake_llm_client, fake_transcriber)
        assert not [frame for frame in frames if frame["type"] == "claim"]

    def test_opted_in_client_sees_every_outcome(
        self,
        client: TestClient,
        fake_llm_client: FakeLLMClient,
        fake_transcriber: FakeTranscriber,
    ) -> None:
        frames = self._run_session(
            client, fake_llm_client, fake_transcriber, send_claims=True
        )
        claims = [frame for frame in frames if frame["type"] == "claim"]
        # A claim gated while live is announced as "pending" first; one found
        # by the final gate pass at stop goes straight to its outcome. Either
        # way the LAST frame per id is the truth, which is how a client that
        # upserts by id reads the stream.
        final = {frame["id"]: frame for frame in claims}
        by_text = {frame["text"]: frame for frame in final.values()}
        assert {text: frame["outcome"] for text, frame in by_text.items()} == {
            "Politicians always lie about their tax plans.": "below_threshold",
            "The Eiffel Tower is 330 meters tall.": "verified",
        }
        skipped = by_text["Politicians always lie about their tax plans."]
        assert skipped["topic"] == "politics"
        assert skipped["check_worthiness"] == 0.2
        # The verdict points at its claim.
        verdict = next(frame for frame in frames if frame["type"] == "verdict")
        assert (
            verdict["claim_id"] == by_text["The Eiffel Tower is 330 meters tall."]["id"]
        )
