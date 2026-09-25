"""Jev + real extraction/verifier adapters through HTTP and live audio routes."""

from collections import deque
import json

import pytest

from app.llm_provider import create_claim_gate
from app.models import TranscriptSegment
from tests.conftest import (
    FakeOpenRouterClient,
    make_chat_completion,
    make_hello,
    make_verdict_interaction,
    pcm_silence,
)
from tests.test_llm_jev import answer
from tests.test_ws_protocol import collect_frames_until_close, wait_until_sync

CLAIM = "The Eiffel Tower is 450 meters tall."


class DecisionsClient(FakeOpenRouterClient):
    def __init__(self):
        super().__init__()
        self.decisions = deque()
        self.decision_calls = []

    async def post(self, path, *, cast_to, body):
        self.decision_calls.append(body)
        assert self.decisions, "Unscripted Jev decision"
        return self.decisions.popleft()


@pytest.fixture
def jev_client(client):
    runtime = client.app.state.llm_runtime
    settings = runtime.settings.model_copy(
        update={
            "gate_provider": "openrouter",
            "openrouter_api_key": "offline",
        }
    )
    transport = DecisionsClient()
    runtime.settings = settings
    runtime.gate_client = transport
    runtime.gate = create_claim_gate(settings, transport)
    client.app.state.settings = settings
    return client, transport


def extraction(transport, *, score=0.9, topic="other"):
    transport.completion_results.append(
        make_chat_completion(
            json.dumps(
                {
                    "claims": [
                        {
                            "claim_text": CLAIM,
                            "check_worthiness": score,
                            "topic": topic,
                        }
                    ]
                }
            )
        )
    )


@pytest.mark.parametrize(
    ("probability", "score", "topic", "expected"),
    [
        (0.1, 0.9, "other", 0),
        (0.5, 0.9, "other", 1),
        (0.9, 0.2, "other", 0),
        (0.9, 0.9, "health", 0),
    ],
)
def test_debug_screening_then_existing_filters(
    jev_client, fake_genai_client, probability, score, topic, expected
):
    client, transport = jev_client
    transport.decisions.append(answer(probability))
    if probability >= 0.35:
        extraction(transport, score=score, topic=topic)
    if expected:
        fake_genai_client.interaction_results.append(
            make_verdict_interaction("FALSE", "About 330 meters.")
        )
    response = client.post(
        "/debug/text",
        json={
            "text": CLAIM,
            "sensitivity": "medium",
            "enabled_topics": ["other"],
        },
    )
    assert response.status_code == 200
    assert len(response.json()["verdicts"]) == expected
    assert len(transport.completion_calls) == int(probability >= 0.35)
    assert len(fake_genai_client.interaction_calls) == expected


def test_debug_still_deduplicates(jev_client, fake_genai_client):
    client, transport = jev_client
    transport.decisions.extend([answer(), answer()])
    extraction(transport)
    extraction(transport)
    fake_genai_client.interaction_results.append(
        make_verdict_interaction("FALSE", "About 330 meters.")
    )
    first = client.post("/debug/text", json={"text": CLAIM})
    second = client.post("/debug/text", json={"text": CLAIM})
    assert len(first.json()["verdicts"]) == 1
    assert second.json()["verdicts"] == []
    assert len(fake_genai_client.interaction_calls) == 1


def test_debug_malformed_jev_response_is_502(jev_client, fake_genai_client):
    client, transport = jev_client
    transport.decisions.append({"answers": {}})
    response = client.post("/debug/text", json={"text": CLAIM})
    assert response.status_code == 502
    assert transport.completion_calls == []
    assert fake_genai_client.interaction_calls == []


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("approved", [False, True])
def test_live_and_final_flush_route_through_jev(
    jev_client, fake_transcriber, fake_genai_client, live, approved
):
    client, transport = jev_client
    transport.decisions.append(answer(0.9 if approved else 0.1))
    if approved:
        extraction(transport)
        fake_genai_client.interaction_results.append(
            make_verdict_interaction("FALSE", "About 330 meters.")
        )
    fake_transcriber.segments_script.append(
        [
            TranscriptSegment(
                text=(
                    "The Eiffel Tower in Paris is 450 meters tall"
                    if live
                    else "The Eiffel Tower is 450 meters tall"
                ),
                start=0,
                end=1,
                avg_logprob=-0.3,
                no_speech_prob=0.05,
            )
        ]
    )
    with client.websocket_connect("/ws/audio") as session:
        session.send_json(make_hello())
        assert session.receive_json()["type"] == "ready"
        for _ in range(4):
            session.send_bytes(pcm_silence(0.25))
        if live:
            wait_until_sync(lambda: len(transport.decision_calls) == 1)
        session.send_json({"type": "stop"})
        frames, code = collect_frames_until_close(session)
    assert code == 1000
    assert len(transport.decision_calls) == 1
    assert len(transport.completion_calls) == int(approved)
    assert len([frame for frame in frames if frame["type"] == "verdict"]) == int(
        approved
    )
    assert len(fake_genai_client.interaction_calls) == int(approved)
