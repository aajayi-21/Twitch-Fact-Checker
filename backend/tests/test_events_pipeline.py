"""Integration: the pipeline publishes verdicts onto the event hub.

Complements ``test_events.py`` (which tests the hub in isolation) by proving
the two things a unit test cannot: that the publish call sits on the code path
BOTH the live loop and the graceful-stop flush phase run through, and that the
claim metadata the chat-posting policy needs actually arrives.
"""

import asyncio
import threading
from typing import Any

import pytest

from app.events import SessionEvent
from app.models import TranscriptSegment
from app.sessions import SessionRegistry
from tests.conftest import (
    FakeGenAIClient,
    FakeTranscriber,
    make_gate_response,
    make_hello,
    make_verdict_interaction,
    pcm_silence,
)
from tests.test_ws_protocol import (
    CLAIM,
    collect_frames_until_close,
    wait_until_sync,
)

NINE_WORD_SEGMENT = TranscriptSegment(
    # >= ClaimGate.MIN_NEW_WORDS words, so the gate's first pass fires.
    text="The Eiffel Tower in Paris is 450 meters tall",
    start=0.0,
    end=1.0,
    avg_logprob=-0.3,
    no_speech_prob=0.05,
)


def collected(client: Any, **subscribe_kwargs: Any) -> list[SessionEvent]:
    """Subscribe to the app's hub and accumulate events into a list.

    The hub hands events to a queue rather than a callback, so a small pump
    task stands in for the real consumers (chat bot, /ws/events).
    """
    events: list[SessionEvent] = []
    subscription = client.app.state.events.subscribe(name="test", **subscribe_kwargs)

    def pump() -> None:
        while True:
            try:
                events.append(subscription._queue.get_nowait())
            except asyncio.QueueEmpty:
                return

    subscription.pump = pump  # type: ignore[attr-defined]
    return events, subscription  # type: ignore[return-value]


class TestLivePhasePublish:
    def test_verdict_reaches_the_hub_with_claim_metadata(
        self,
        client: Any,
        fake_genai_client: FakeGenAIClient,
        fake_transcriber: FakeTranscriber,
    ) -> None:
        events, subscription = collected(client, types={"verdict"})  # type: ignore[misc]
        fake_transcriber.segments_script.append([NINE_WORD_SEGMENT])
        fake_genai_client.generate_results.append(make_gate_response([(CLAIM, 0.9)]))
        fake_genai_client.interaction_results.append(
            make_verdict_interaction("FALSE", "About 330 meters.")
        )

        with client.websocket_connect("/ws/audio") as session:
            session.send_json(make_hello(platform="twitch", channel="TestStreamer"))
            assert session.receive_json()["type"] == "ready"
            for _ in range(4):
                session.send_bytes(pcm_silence(0.25))
            wait_until_sync(lambda: len(fake_genai_client.interaction_calls) >= 1)
            session.send_json({"type": "stop"})
            collect_frames_until_close(session)

        subscription.pump()
        assert len(events) == 1
        event = events[0]
        assert event.frame["type"] == "verdict"
        assert event.frame["label"] == "FALSE"
        # Identity the wire frame does not carry, normalized for the bot's key.
        assert event.platform == "twitch"
        assert event.channel == "teststreamer"
        assert event.session_id
        # Claim metadata the posting policy filters on.
        assert event.claim_id
        assert event.check_worthiness == pytest.approx(0.9)
        assert event.claim_age_s is not None and event.claim_age_s >= 0.0
        assert event.stream_time_s is not None


class TestFlushPhasePublish:
    def test_in_flight_verdict_is_published_during_the_flush(
        self,
        client: Any,
        fake_genai_client: FakeGenAIClient,
        fake_transcriber: FakeTranscriber,
    ) -> None:
        """The flush phase writes the socket DIRECTLY, bypassing the outbound
        queue, because the send loop is already dead. A fan-out hooked into
        the live emitter alone would silently lose these — and an end-of-stream
        verdict is one the chat bot still has an audience for.
        """
        events, subscription = collected(client, types={"verdict"})  # type: ignore[misc]
        fake_transcriber.segments_script.append([NINE_WORD_SEGMENT])
        fake_genai_client.generate_results.append(make_gate_response([(CLAIM, 0.9)]))
        release_verdict = threading.Event()

        async def blocked_verdict(**_call: Any) -> Any:
            while not release_verdict.is_set():
                await asyncio.sleep(0.01)
            return make_verdict_interaction("FALSE", "About 330 meters.")

        fake_genai_client.interaction_results.append(blocked_verdict)

        with client.websocket_connect("/ws/audio") as session:
            session.send_json(make_hello())
            assert session.receive_json()["type"] == "ready"
            for _ in range(4):
                session.send_bytes(pcm_silence(0.25))
            wait_until_sync(lambda: len(fake_genai_client.interaction_calls) >= 1)
            session.send_json({"type": "stop"})
            registry: SessionRegistry = client.app.state.sessions
            wait_until_sync(
                lambda: any(live._stop_requested.is_set() for live in registry.all())
            )
            release_verdict.set()
            frames, close_code = collect_frames_until_close(session)

        assert close_code == 1000
        assert [f["label"] for f in frames if f["type"] == "verdict"] == ["FALSE"]

        subscription.pump()
        assert [event.frame["label"] for event in events] == ["FALSE"]


class TestStreamTimePersistence:
    def test_claims_record_their_position_in_the_stream(
        self,
        client: Any,
        fake_genai_client: FakeGenAIClient,
        fake_transcriber: FakeTranscriber,
    ) -> None:
        """``claims.stream_time_s`` has existed in the schema since it was
        written but nothing ever populated it, so a disputed verdict could not
        be located in the VOD."""
        fake_transcriber.segments_script.append([NINE_WORD_SEGMENT])
        fake_genai_client.generate_results.append(make_gate_response([(CLAIM, 0.9)]))
        fake_genai_client.interaction_results.append(
            make_verdict_interaction("FALSE", "About 330 meters.")
        )

        with client.websocket_connect("/ws/audio") as session:
            session.send_json(make_hello())
            assert session.receive_json()["type"] == "ready"
            for _ in range(4):
                session.send_bytes(pcm_silence(0.25))
            wait_until_sync(lambda: len(fake_genai_client.interaction_calls) >= 1)
            session.send_json({"type": "stop"})
            collect_frames_until_close(session)

        sessions = client.get("/stats/sessions?limit=1").json()
        detail = client.get(f"/stats/sessions/{sessions[0]['id']}").json()
        claims = detail["claims"]
        assert claims, "expected at least one recorded claim"
        assert all(claim["stream_time_s"] is not None for claim in claims)
