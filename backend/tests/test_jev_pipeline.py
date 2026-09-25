"""Jev pre-screen + real extraction/verifier adapters through HTTP and live audio."""

import json
import sqlite3
from collections import deque

import pytest

from app.llm_provider import create_claim_gate
from app.models import TranscriptSegment
from tests.conftest import (
    FakeOpenRouterClient,
    make_chat_completion,
    make_gate_response,
    make_hello,
    make_verdict_completion,
    pcm_silence,
)
from tests.test_llm_jev import answer
from tests.test_pipeline_persistence import wait_until
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


def install_jev(client, mode: str) -> DecisionsClient:
    runtime = client.app.state.llm_runtime
    settings = runtime.settings.model_copy(
        update={
            "gate_provider": "openrouter",
            "openrouter_api_key": "offline",
            "jev_mode": mode,
        }
    )
    transport = DecisionsClient()
    runtime.settings = settings
    runtime.gate_client = transport
    runtime.gate = create_claim_gate(settings, transport)
    client.app.state.settings = settings
    return transport


@pytest.fixture
def screen_client(client):
    return client, install_jev(client, "screen")


@pytest.fixture
def shadow_client(client):
    return client, install_jev(client, "shadow")


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


def gate_pass_rows(client) -> list[sqlite3.Row]:
    with sqlite3.connect(client.app.state.settings.db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM gate_passes").fetchall()


def claim_links(client) -> list[tuple]:
    with sqlite3.connect(client.app.state.settings.db_path) as conn:
        return conn.execute("SELECT text, gate_pass_id, outcome FROM claims").fetchall()


def stream_one_segment(client, fake_transcriber, text: str, *, live: bool) -> list:
    fake_transcriber.segments_script.append(
        [
            TranscriptSegment(
                text=text, start=0, end=1, avg_logprob=-0.3, no_speech_prob=0.05
            )
        ]
    )
    with client.websocket_connect("/ws/audio") as session:
        session.send_json(make_hello())
        assert session.receive_json()["type"] == "ready"
        for _ in range(4):
            session.send_bytes(pcm_silence(0.25))
        if live:
            transport = client.app.state.llm_runtime.gate_client
            wait_until_sync(lambda: len(transport.decision_calls) == 1)
        session.send_json({"type": "stop"})
        frames, code = collect_frames_until_close(session)
    assert code == 1000
    return frames


class TestDebugPath:
    @pytest.mark.parametrize(
        ("probability", "score", "topic", "expected"),
        [
            (0.1, 0.9, "other", 0),
            (0.5, 0.9, "other", 1),
            (0.9, 0.2, "other", 0),
            (0.9, 0.9, "health", 0),
        ],
    )
    def test_screening_then_existing_filters(
        self, screen_client, fake_llm_client, probability, score, topic, expected
    ):
        client, transport = screen_client
        transport.decisions.append(answer(probability))
        if probability >= 0.35:
            extraction(transport, score=score, topic=topic)
        if expected:
            fake_llm_client.verify_results.append(
                make_verdict_completion("FALSE", "About 330 meters.")
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
        assert len(fake_llm_client.verify_calls) == expected

    def test_malformed_jev_response_fails_open(
        self, screen_client, fake_llm_client
    ) -> None:
        client, transport = screen_client
        transport.decisions.append({"answers": {}})
        extraction(transport)
        fake_llm_client.verify_results.append(
            make_verdict_completion("FALSE", "About 330 meters.")
        )
        response = client.post("/debug/text", json={"text": CLAIM})
        assert response.status_code == 200
        assert len(response.json()["verdicts"]) == 1
        assert len(transport.completion_calls) == 1

    def test_still_deduplicates(self, screen_client, fake_llm_client) -> None:
        client, transport = screen_client
        transport.decisions.extend([answer(), answer()])
        extraction(transport)
        extraction(transport)
        fake_llm_client.verify_results.append(
            make_verdict_completion("FALSE", "About 330 meters.")
        )
        first = client.post("/debug/text", json={"text": CLAIM})
        second = client.post("/debug/text", json={"text": CLAIM})
        assert len(first.json()["verdicts"]) == 1
        assert second.json()["verdicts"] == []
        assert len(fake_llm_client.verify_calls) == 1

    def test_records_no_gate_pass(self, screen_client) -> None:
        client, transport = screen_client
        transport.decisions.append(answer(0.1))
        assert client.post("/debug/text", json={"text": CLAIM}).status_code == 200
        assert gate_pass_rows(client) == []


class TestLiveSessions:
    @pytest.mark.parametrize("live", [False, True])
    @pytest.mark.parametrize("approved", [False, True])
    def test_screen_routes_live_and_flush_passes_and_records_them(
        self, screen_client, fake_transcriber, fake_llm_client, live, approved
    ) -> None:
        client, transport = screen_client
        transport.decisions.append(answer(0.9 if approved else 0.1))
        if approved:
            extraction(transport)
            fake_llm_client.verify_results.append(
                make_verdict_completion("FALSE", "About 330 meters.")
            )
        text = (
            "The Eiffel Tower in Paris is 450 meters tall"
            if live
            else "The Eiffel Tower is 450 meters tall"
        )
        frames = stream_one_segment(client, fake_transcriber, text, live=live)
        assert len(transport.decision_calls) == 1
        assert len(transport.completion_calls) == int(approved)
        verdicts = [frame for frame in frames if frame["type"] == "verdict"]
        assert len(verdicts) == int(approved)
        assert len(fake_llm_client.verify_calls) == int(approved)

        wait_until(lambda: len(gate_pass_rows(client)) == 1)
        (row,) = gate_pass_rows(client)
        assert row["phase"] == ("live" if live else "flush")
        assert row["jev_mode"] == "screen"
        assert row["jev_model"] == "typesafe/jev-1.13"
        assert row["jev_route"] == ("extract" if approved else "skip")
        assert row["jev_probability"] == (0.9 if approved else 0.1)
        assert row["claims_count"] == int(approved)
        assert row["gate_provider"] == "openrouter"
        # Jev is on, so the batch text is kept for calibration.
        assert row["new_text"] == text
        if approved:
            wait_until(lambda: claim_links(client) != [])
            ((claim_text, gate_pass_id, outcome),) = claim_links(client)
            assert gate_pass_id == row["id"]
            assert outcome == "verified"

    def test_shadow_extracts_even_when_jev_says_no(
        self, shadow_client, fake_transcriber, fake_llm_client
    ) -> None:
        client, transport = shadow_client
        transport.decisions.append(answer(0.05))
        extraction(transport)
        fake_llm_client.verify_results.append(
            make_verdict_completion("FALSE", "About 330 meters.")
        )
        frames = stream_one_segment(
            client, fake_transcriber, "The Eiffel Tower is 450 meters tall", live=False
        )
        assert len([f for f in frames if f["type"] == "verdict"]) == 1
        wait_until(lambda: len(gate_pass_rows(client)) == 1)
        (row,) = gate_pass_rows(client)
        assert (row["jev_mode"], row["jev_route"]) == ("shadow", "shadow")
        assert row["jev_probability"] == 0.05
        assert row["claims_count"] == 1


class TestJevOff:
    def test_gate_pass_rows_keep_metadata_but_no_text(
        self, client, fake_transcriber, fake_llm_client
    ) -> None:
        fake_llm_client.gate_results.append(make_gate_response([]))
        stream_one_segment(
            client, fake_transcriber, "just some chatter about nothing", live=False
        )
        wait_until(lambda: len(gate_pass_rows(client)) == 1)
        (row,) = gate_pass_rows(client)
        assert row["jev_mode"] == "off"
        assert row["jev_route"] is None
        assert row["new_text"] is None and row["context"] is None
        assert row["word_count"] == 5
        assert row["claims_count"] == 0
