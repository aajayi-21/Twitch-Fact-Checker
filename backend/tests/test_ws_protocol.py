"""End-to-end protocol tests: /healthz, /debug/text, and /ws/audio with fakes.

Everything runs through the real app (routes, CORS, pipeline, endpoint) with
the fake Gemini client and fake transcriber installed by the conftest
lifespan — fully offline.
"""

import asyncio
import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.websockets import WebSocketDisconnect

from app import ws as ws_module
from app.llm_gemini import GeminiClaimGate, GeminiFactChecker
from app.models import ClientHello, StatusFrame, TranscriptSegment
from app.pipeline import SessionPipeline
from app.rate_limit import QuotaCooldown, TokenBucket
from tests.conftest import (
    FakeGenAIClient,
    FakeInteractionsError,
    FakeTranscriber,
    make_fake_llm_runtime,
    make_gate_response,
    make_hello,
    make_test_settings,
    make_verdict_interaction,
    open_test_client,
    pcm_silence,
)

SEVEN_WORD_SEGMENT = TranscriptSegment(
    # Seven words: below ClaimGate.MIN_NEW_WORDS, so with the huge test
    # gate_interval the gate only runs on the unconditional final flush pass.
    text="The Eiffel Tower is 450 meters tall",
    start=0.0,
    end=1.0,
    avg_logprob=-0.3,
    no_speech_prob=0.05,
)
CLAIM = "The Eiffel Tower is 450 meters tall."


def collect_frames_until_close(session: Any) -> tuple[list[dict[str, Any]], int]:
    """Drain server frames until the server closes; return (frames, close code)."""
    frames: list[dict[str, Any]] = []
    while True:
        try:
            frames.append(session.receive_json())
        except WebSocketDisconnect as disconnect:
            return frames, disconnect.code


def wait_until_sync(predicate: Callable[[], bool], timeout_s: float = 5.0) -> None:
    """Block the TEST thread until ``predicate()`` holds (server runs in the
    TestClient portal thread meanwhile)."""
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(f"condition not met within {timeout_s}s")
        time.sleep(0.01)


async def wait_until_async(
    predicate: Callable[[], bool], timeout_s: float = 5.0
) -> None:
    """Yield to the event loop until ``predicate()`` holds."""
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(f"condition not met within {timeout_s}s")
        await asyncio.sleep(0.005)


# --------------------------------------------------------------------------- #
# HTTP surface
# --------------------------------------------------------------------------- #


class TestHealthz:
    def test_reports_status_and_models(self, client) -> None:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {
            "status": "ok",
            "server_version": "0.1.0",
            "whisper_model": "fake-whisper.en",
            "configured": True,
            "llm_provider": "gemini",
            "gate_provider": "gemini",
            "verify_provider": "gemini",
            "gate_model": "fake-gate-model",
            "verify_model": "fake-verify-model",
            "checks_today": 0,
            "est_cost_today_usd": 0.0,
        }


class TestCors:
    def test_preflight_allows_extension_origin(self, client) -> None:
        origin = "chrome-extension://" + "a" * 32
        response = client.options(
            "/debug/text",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin

    def test_preflight_rejects_foreign_origin(self, client) -> None:
        response = client.options(
            "/debug/text",
            headers={
                "Origin": "https://evil.example.com",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert "access-control-allow-origin" not in response.headers


class TestDebugText:
    def test_empty_text_is_400(self, client) -> None:
        response = client.post("/debug/text", json={"text": "   "})
        assert response.status_code == 400

    def test_disabled_debug_endpoints_is_404(
        self, fake_genai_client: FakeGenAIClient, fake_transcriber: FakeTranscriber
    ) -> None:
        settings = make_test_settings(debug_endpoints=False)
        with open_test_client(settings, fake_genai_client, fake_transcriber) as client:
            response = client.post("/debug/text", json={"text": "The earth is flat."})
        assert response.status_code == 404

    def test_gate_failure_is_502_with_detail(
        self, client, fake_genai_client: FakeGenAIClient
    ) -> None:
        fake_genai_client.generate_results.append(RuntimeError("gate exploded"))
        response = client.post("/debug/text", json={"text": "The earth is flat."})
        assert response.status_code == 502
        assert "claim gate failed" in response.json()["detail"]

    def test_verification_failure_is_502_with_detail(
        self, client, fake_genai_client: FakeGenAIClient
    ) -> None:
        fake_genai_client.generate_results.append(make_gate_response([(CLAIM, 0.9)]))
        fake_genai_client.interaction_results.append(FakeInteractionsError(500))
        response = client.post("/debug/text", json={"text": "some claim text"})
        assert response.status_code == 502
        assert "verification failed" in response.json()["detail"]

    def test_happy_path_returns_claims_and_verdicts(
        self, client, fake_genai_client: FakeGenAIClient
    ) -> None:
        fake_genai_client.generate_results.append(make_gate_response([(CLAIM, 0.9)]))
        fake_genai_client.interaction_results.append(
            make_verdict_interaction(
                "FALSE",
                "The Eiffel Tower is about 330 meters tall.",
                citations=[("https://www.toureiffel.paris/x", "Key figures")],
            )
        )
        response = client.post(
            "/debug/text", json={"text": "the eiffel tower is 450 meters tall chat"}
        )
        assert response.status_code == 200
        body = response.json()
        # The server-generated claim id is opaque; assert it exists, then
        # compare the model-produced fields exactly.
        (claim_body,) = body["claims"]
        assert claim_body.pop("id")
        assert claim_body == {
            "claim_text": CLAIM,
            "check_worthiness": 0.9,
            "topic": "other",
        }
        assert len(body["verdicts"]) == 1
        verdict = body["verdicts"][0]
        assert verdict["label"] == "FALSE"
        assert verdict["topic"] == "other"
        assert verdict["used_fallback"] is False
        assert verdict["sources"] == [
            {"url": "https://www.toureiffel.paris/x", "title": "Key figures"}
        ]

    def test_opinion_yields_no_claims_and_no_verify_call(
        self, client, fake_genai_client: FakeGenAIClient
    ) -> None:
        fake_genai_client.generate_results.append(make_gate_response([]))
        response = client.post("/debug/text", json={"text": "this game is terrible"})
        assert response.status_code == 200
        assert response.json() == {"claims": [], "verdicts": []}
        assert fake_genai_client.interaction_calls == []

    @pytest.mark.parametrize(
        ("sensitivity", "expected_verified"),
        [("low", 1), ("medium", 1), ("high", 2)],
    )
    def test_sensitivity_threshold_filters_claims(
        self,
        client,
        fake_genai_client: FakeGenAIClient,
        sensitivity: str,
        expected_verified: int,
    ) -> None:
        # 0.5 passes only "high" (0.35); 0.9 passes every threshold.
        fake_genai_client.generate_results.append(
            make_gate_response(
                [("A borderline factual claim.", 0.5), ("A strong claim.", 0.9)]
            )
        )
        for _ in range(expected_verified):
            fake_genai_client.interaction_results.append(
                make_verdict_interaction("TRUE", "Confirmed.")
            )
        response = client.post(
            "/debug/text",
            json={"text": "two claims worth of text", "sensitivity": sensitivity},
        )
        assert response.status_code == 200
        body = response.json()
        assert len(body["claims"]) == 2  # gate output is always reported
        assert len(body["verdicts"]) == expected_verified

    def test_enabled_topics_filters_claims_before_verification(
        self, client, fake_genai_client: FakeGenAIClient
    ) -> None:
        fake_genai_client.generate_results.append(
            make_gate_response(
                [
                    ("A claim about an election.", 0.9, "politics"),
                    ("A claim about a vaccine.", 0.9, "health"),
                ]
            )
        )
        # Only ONE verdict scripted: the politics claim must never reach the
        # verify call.
        fake_genai_client.interaction_results.append(
            make_verdict_interaction("TRUE", "Confirmed.")
        )
        response = client.post(
            "/debug/text",
            json={"text": "two claims here", "enabled_topics": ["health"]},
        )
        assert response.status_code == 200
        body = response.json()
        # Gate output is always reported, including topic-skipped claims...
        assert [(c["claim_text"], c["topic"]) for c in body["claims"]] == [
            ("A claim about an election.", "politics"),
            ("A claim about a vaccine.", "health"),
        ]
        # ...but only enabled-topic claims are verified.
        assert [v["claim"] for v in body["verdicts"]] == ["A claim about a vaccine."]
        assert body["verdicts"][0]["topic"] == "health"
        assert len(fake_genai_client.interaction_calls) == 1

    def test_other_topic_cannot_be_disabled(
        self, client, fake_genai_client: FakeGenAIClient
    ) -> None:
        fake_genai_client.generate_results.append(
            make_gate_response([("Some general trivia claim.", 0.9, "other")])
        )
        fake_genai_client.interaction_results.append(
            make_verdict_interaction("TRUE", "Confirmed.")
        )
        # "other" absent from the enabled list, unknown slug thrown in too:
        # both are tolerated; "other" is force-enabled server-side.
        response = client.post(
            "/debug/text",
            json={
                "text": "trivia text",
                "enabled_topics": ["politics", "astrology"],
            },
        )
        assert response.status_code == 200
        assert len(response.json()["verdicts"]) == 1

    def test_paraphrase_deduped_across_requests(
        self, client, fake_genai_client: FakeGenAIClient
    ) -> None:
        fake_genai_client.generate_results.append(make_gate_response([(CLAIM, 0.9)]))
        fake_genai_client.interaction_results.append(
            make_verdict_interaction("FALSE", "About 330 meters.")
        )
        first = client.post("/debug/text", json={"text": "tower claim take one"})
        assert len(first.json()["verdicts"]) == 1

        fake_genai_client.generate_results.append(
            make_gate_response([("the eiffel tower is 450 meters tall", 0.9)])
        )
        second = client.post("/debug/text", json={"text": "tower claim take two"})
        assert second.status_code == 200
        assert second.json()["verdicts"] == []
        assert len(fake_genai_client.interaction_calls) == 1

    def test_429_mid_request_drops_remaining_claims(
        self, client, fake_genai_client: FakeGenAIClient
    ) -> None:
        fake_genai_client.generate_results.append(
            make_gate_response(
                [
                    ("First distinct claim about history.", 0.9),
                    ("Second distinct claim about science.", 0.9),
                ]
            )
        )
        fake_genai_client.interaction_results.append(FakeInteractionsError(429))
        response = client.post("/debug/text", json={"text": "two claims here"})
        assert response.status_code == 200  # partial results, not an error
        assert response.json()["verdicts"] == []
        assert len(fake_genai_client.interaction_calls) == 1
        assert client.app.state.quota_cooldown.active is True


# --------------------------------------------------------------------------- #
# WebSocket handshake
# --------------------------------------------------------------------------- #


class TestHandshake:
    def test_valid_hello_gets_ready(self, client) -> None:
        with client.websocket_connect("/ws/audio") as session:
            session.send_json(make_hello())
            ready = session.receive_json()
            assert ready == {
                "type": "ready",
                "server_version": "0.1.0",
                "model": "fake-whisper.en",
            }
            session.send_json({"type": "stop"})
            _frames, close_code = collect_frames_until_close(session)
            assert close_code == 1000

    def test_hello_timeout_closes_1008(self, client, monkeypatch) -> None:
        monkeypatch.setattr(ws_module, "HELLO_TIMEOUT_S", 0.2)
        with client.websocket_connect("/ws/audio") as session:
            error = session.receive_json()
            assert error["type"] == "error"
            assert error["code"] == "bad_hello"
            assert error["fatal"] is True
            with pytest.raises(WebSocketDisconnect) as disconnect:
                session.receive_json()
            assert disconnect.value.code == 1008

    def test_invalid_hello_schema_closes_1008(self, client) -> None:
        with client.websocket_connect("/ws/audio") as session:
            session.send_json(make_hello(version=2))
            error = session.receive_json()
            assert error["code"] == "bad_hello"
            with pytest.raises(WebSocketDisconnect) as disconnect:
                session.receive_json()
            assert disconnect.value.code == 1008

    def test_non_json_hello_closes_1008(self, client) -> None:
        with client.websocket_connect("/ws/audio") as session:
            session.send_text("definitely not json")
            error = session.receive_json()
            assert error["code"] == "bad_hello"
            with pytest.raises(WebSocketDisconnect) as disconnect:
                session.receive_json()
            assert disconnect.value.code == 1008

    def test_binary_first_frame_closes_1008(self, client) -> None:
        with client.websocket_connect("/ws/audio") as session:
            session.send_bytes(pcm_silence(0.25))
            error = session.receive_json()
            assert error["code"] == "bad_hello"
            assert "binary" in error["message"]
            with pytest.raises(WebSocketDisconnect) as disconnect:
                session.receive_json()
            assert disconnect.value.code == 1008


class TestPreemption:
    def test_new_connection_preempts_old(self, client) -> None:
        with client.websocket_connect("/ws/audio") as first:
            first.send_json(make_hello())
            assert first.receive_json()["type"] == "ready"
            with client.websocket_connect("/ws/audio") as second:
                superseded = first.receive_json()
                assert superseded["type"] == "error"
                assert superseded["code"] == "superseded"
                assert superseded["fatal"] is True
                with pytest.raises(WebSocketDisconnect) as disconnect:
                    first.receive_json()
                assert disconnect.value.code == 1000

                second.send_json(make_hello())
                assert second.receive_json()["type"] == "ready"
                second.send_json({"type": "stop"})
                _frames, close_code = collect_frames_until_close(second)
                assert close_code == 1000


class RaceWebSocket:
    """Minimal WebSocket double for driving ``audio_ws`` directly.

    ``send_json`` waits on a controllable gate so a real ``preempt()``
    genuinely suspends mid-send — exactly the window the registration lock
    must close. ``receive()`` blocks until the socket is closed or
    force-disconnected (these sessions carry no audio).
    """

    def __init__(self, app: Any) -> None:
        self.app = app
        self.sent: list[dict[str, Any]] = []
        self.closed_code: int | None = None
        self.hello_requested = asyncio.Event()
        self.hello_release = asyncio.Event()
        self.send_gate = asyncio.Event()
        self.send_gate.set()
        self._disconnected = asyncio.Event()

    async def accept(self) -> None:
        await asyncio.sleep(0)

    async def receive_text(self) -> str:
        self.hello_requested.set()
        await self.hello_release.wait()
        return json.dumps(make_hello())

    async def receive(self) -> dict[str, Any]:
        await self._disconnected.wait()
        return {"type": "websocket.disconnect", "code": 1000}

    async def send_json(self, data: dict[str, Any]) -> None:
        await self.send_gate.wait()
        if self.closed_code is not None:
            raise WebSocketDisconnect(code=1006)
        self.sent.append(data)

    async def close(self, code: int = 1000) -> None:
        self.closed_code = code
        self._disconnected.set()
        await asyncio.sleep(0)

    def force_disconnect(self) -> None:
        self._disconnected.set()

    def frame_types(self) -> list[str]:
        return [frame.get("type", "") for frame in self.sent]


def make_fake_ws_app(executor: ThreadPoolExecutor) -> Any:
    """An ``app.state`` lookalike with the process-wide fakes ws.py expects."""
    settings = make_test_settings()
    cooldown = QuotaCooldown()
    state = SimpleNamespace(
        settings=settings,
        transcriber=FakeTranscriber(),
        stt_executor=executor,
        llm_runtime=make_fake_llm_runtime(settings, FakeGenAIClient(), cooldown),
        verify_bucket=TokenBucket(rate_per_min=6000.0, burst=10),
        quota_cooldown=cooldown,
        # Analytics slots: None disables persistence in SessionPipeline.
        db=None,
        verify_counter=None,
    )
    return SimpleNamespace(state=state)


class TestPreemptionRegistrationRace:
    async def test_concurrent_connects_leave_exactly_one_live_pipeline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression for the preempt/register race (§2.1 single-user
        invariant): while one connection is suspended inside ``preempt()``
        of the current session, a third connection must not slip past the
        (synchronously early-returning) preempt and register a SECOND live
        pipeline. Drives the endpoint coroutine directly with fake sockets
        whose sends can be held open — TestClient sequencing cannot exercise
        two connections suspended inside the endpoint at once."""
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt-race")
        fake_app = make_fake_ws_app(executor)

        built_pipelines: list[SessionPipeline] = []
        original_build = ws_module._build_pipeline

        def record_build(websocket: Any, hello: Any) -> SessionPipeline:
            pipeline = original_build(websocket, hello)
            built_pipelines.append(pipeline)
            return pipeline

        monkeypatch.setattr(ws_module, "_build_pipeline", record_build)

        socket_b = RaceWebSocket(fake_app)
        socket_c = RaceWebSocket(fake_app)
        socket_d = RaceWebSocket(fake_app)
        tasks: list[asyncio.Task[None]] = []
        try:
            # C connects first and parks awaiting its hello (the slot is
            # empty, so its pre-hello preempt is a no-op).
            tasks.append(asyncio.create_task(ws_module.audio_ws(socket_c)))
            await wait_until_async(socket_c.hello_requested.is_set)

            # B connects, completes its hello immediately, registers, runs.
            socket_b.hello_release.set()
            tasks.append(asyncio.create_task(ws_module.audio_ws(socket_b)))
            await wait_until_async(
                lambda: ws_module.current_pipeline is not None
                and "ready" in socket_b.frame_types()
            )
            pipeline_b = ws_module.current_pipeline
            assert pipeline_b is not None

            # C's hello arrives; its re-check preempts B — and SUSPENDS
            # mid-send, because B's socket gate is held shut.
            socket_b.send_gate.clear()
            socket_c.hello_release.set()
            await wait_until_async(lambda: pipeline_b._preempted)

            # D connects while C is suspended inside B.preempt(). Unless
            # registration is serialized, D sees the already-preempted B,
            # gets preempt()'s synchronous early return, and registers a
            # second live pipeline alongside C's.
            socket_d.hello_release.set()
            tasks.append(asyncio.create_task(ws_module.audio_ws(socket_d)))
            await asyncio.sleep(0.05)  # let D run to its park point

            # Release B's send: C resumes and finishes preempt + register.
            socket_b.send_gate.set()
            await wait_until_async(
                lambda: "ready" in socket_c.frame_types()
                and "ready" in socket_d.frame_types()
                and ws_module.current_pipeline is not None
                and ws_module.current_pipeline._websocket in (socket_c, socket_d)
            )

            live_pipelines = [p for p in built_pipelines if not p._preempted]
            assert len(live_pipelines) == 1, (
                "single-user invariant violated: "
                f"{len(live_pipelines)} live pipelines after concurrent connects"
            )
            assert ws_module.current_pipeline is live_pipelines[0]
            winner_socket = live_pipelines[0]._websocket
            loser_socket = socket_c if winner_socket is socket_d else socket_d
            assert any(frame.get("code") == "superseded" for frame in loser_socket.sent)
            assert loser_socket.closed_code == 1000
            assert winner_socket.closed_code is None
        finally:
            for sock in (socket_b, socket_c, socket_d):
                sock.hello_release.set()
                sock.send_gate.set()
                sock.force_disconnect()
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), timeout=5.0
                )
            except TimeoutError:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            executor.shutdown(wait=False, cancel_futures=True)


# --------------------------------------------------------------------------- #
# Full audio pipeline (binary audio in -> verdict out, all fakes)
# --------------------------------------------------------------------------- #


class TestAudioToVerdict:
    def test_stream_then_stop_yields_transcript_status_verdict(
        self,
        client,
        fake_genai_client: FakeGenAIClient,
        fake_transcriber: FakeTranscriber,
    ) -> None:
        fake_transcriber.segments_script.append([SEVEN_WORD_SEGMENT])
        fake_genai_client.generate_results.append(make_gate_response([(CLAIM, 0.9)]))
        fake_genai_client.interaction_results.append(
            make_verdict_interaction(
                "FALSE",
                "The Eiffel Tower is about 330 meters tall.",
                citations=[("https://www.toureiffel.paris/x", "Key figures")],
            )
        )
        with client.websocket_connect("/ws/audio") as session:
            session.send_json(make_hello())
            assert session.receive_json()["type"] == "ready"
            for _ in range(4):  # 1.0 s of audio in 250 ms frames
                session.send_bytes(pcm_silence(0.25))
            session.send_json({"type": "stop"})
            frames, close_code = collect_frames_until_close(session)

        assert close_code == 1000
        by_type: dict[str, list[dict[str, Any]]] = {}
        for frame in frames:
            by_type.setdefault(frame["type"], []).append(frame)

        assert [t["text"] for t in by_type["transcript"]] == [
            "The Eiffel Tower is 450 meters tall"
        ]
        assert [s["claim"] for s in by_type["status"]] == [CLAIM]
        assert by_type["status"][0]["stage"] == "verifying"
        (verdict,) = by_type["verdict"]
        assert verdict["claim"] == CLAIM
        assert verdict["topic"] == "other"
        assert verdict["label"] == "FALSE"
        assert verdict["used_fallback"] is False
        assert verdict["sources"] == [
            {"url": "https://www.toureiffel.paris/x", "title": "Key figures"}
        ]

    def test_hello_can_opt_out_of_transcripts(
        self,
        client,
        fake_genai_client: FakeGenAIClient,
        fake_transcriber: FakeTranscriber,
    ) -> None:
        fake_transcriber.segments_script.append([SEVEN_WORD_SEGMENT])
        fake_genai_client.generate_results.append(make_gate_response([(CLAIM, 0.9)]))
        fake_genai_client.interaction_results.append(
            make_verdict_interaction("FALSE", "About 330 meters.")
        )
        with client.websocket_connect("/ws/audio") as session:
            session.send_json(make_hello(send_transcripts=False))
            assert session.receive_json()["type"] == "ready"
            for _ in range(4):
                session.send_bytes(pcm_silence(0.25))
            session.send_json({"type": "stop"})
            frames, close_code = collect_frames_until_close(session)

        assert close_code == 1000
        frame_types = {frame["type"] for frame in frames}
        assert "transcript" not in frame_types
        assert "verdict" in frame_types

    def test_mid_session_config_changes_sensitivity(
        self,
        client,
        fake_genai_client: FakeGenAIClient,
        fake_transcriber: FakeTranscriber,
    ) -> None:
        # Worthiness 0.6 fails the hello's "low" (0.75) but passes the
        # mid-session "high" (0.35) update.
        fake_transcriber.segments_script.append([SEVEN_WORD_SEGMENT])
        fake_genai_client.generate_results.append(make_gate_response([(CLAIM, 0.6)]))
        fake_genai_client.interaction_results.append(
            make_verdict_interaction("FALSE", "About 330 meters.")
        )
        with client.websocket_connect("/ws/audio") as session:
            session.send_json(make_hello(sensitivity="low"))
            assert session.receive_json()["type"] == "ready"
            session.send_json({"type": "config", "sensitivity": "high"})
            for _ in range(4):
                session.send_bytes(pcm_silence(0.25))
            session.send_json({"type": "stop"})
            frames, close_code = collect_frames_until_close(session)

        assert close_code == 1000
        assert any(frame["type"] == "verdict" for frame in frames)

    def test_topic_filter_drops_claim_and_emits_topic_skipped(
        self,
        client,
        fake_genai_client: FakeGenAIClient,
        fake_transcriber: FakeTranscriber,
    ) -> None:
        fake_transcriber.segments_script.append([SEVEN_WORD_SEGMENT])
        fake_genai_client.generate_results.append(
            make_gate_response([("An election claim.", 0.9, "politics")])
        )
        # No interaction scripted: the claim must be dropped BEFORE the
        # bucket/verify step ever runs.
        with client.websocket_connect("/ws/audio") as session:
            session.send_json(make_hello(enabled_topics=["health"]))
            assert session.receive_json()["type"] == "ready"
            for _ in range(4):
                session.send_bytes(pcm_silence(0.25))
            session.send_json({"type": "stop"})
            frames, close_code = collect_frames_until_close(session)

        assert close_code == 1000
        status_frames = [f for f in frames if f["type"] == "status"]
        assert status_frames == [
            {
                "type": "status",
                "stage": "topic_skipped",
                "claim": "An election claim.",
                "topic": "politics",
            }
        ]
        assert not any(frame["type"] == "verdict" for frame in frames)
        assert fake_genai_client.interaction_calls == []

    def test_mid_session_config_flips_topics(
        self,
        client,
        fake_genai_client: FakeGenAIClient,
        fake_transcriber: FakeTranscriber,
    ) -> None:
        # The hello disables politics; the mid-session config re-enables it
        # before the claim is gated, so verification proceeds.
        fake_transcriber.segments_script.append([SEVEN_WORD_SEGMENT])
        fake_genai_client.generate_results.append(
            make_gate_response([("An election claim.", 0.9, "politics")])
        )
        fake_genai_client.interaction_results.append(
            make_verdict_interaction("TRUE", "Confirmed.")
        )
        with client.websocket_connect("/ws/audio") as session:
            session.send_json(make_hello(enabled_topics=["health"]))
            assert session.receive_json()["type"] == "ready"
            session.send_json(
                {"type": "config", "enabled_topics": ["health", "politics"]}
            )
            for _ in range(4):
                session.send_bytes(pcm_silence(0.25))
            session.send_json({"type": "stop"})
            frames, close_code = collect_frames_until_close(session)

        assert close_code == 1000
        verdicts = [f for f in frames if f["type"] == "verdict"]
        assert [v["topic"] for v in verdicts] == ["politics"]
        assert not any(f.get("stage") == "topic_skipped" for f in frames)

    def test_topics_only_config_leaves_sensitivity_untouched(
        self,
        client,
        fake_genai_client: FakeGenAIClient,
        fake_transcriber: FakeTranscriber,
    ) -> None:
        # Worthiness 0.6 fails the hello's "low" (0.75); the topics-only
        # config frame must NOT reset sensitivity, so it still fails.
        fake_transcriber.segments_script.append([SEVEN_WORD_SEGMENT])
        fake_genai_client.generate_results.append(make_gate_response([(CLAIM, 0.6)]))
        with client.websocket_connect("/ws/audio") as session:
            session.send_json(make_hello(sensitivity="low"))
            assert session.receive_json()["type"] == "ready"
            session.send_json({"type": "config", "enabled_topics": ["politics"]})
            for _ in range(4):
                session.send_bytes(pcm_silence(0.25))
            session.send_json({"type": "stop"})
            frames, close_code = collect_frames_until_close(session)

        assert close_code == 1000
        assert not any(frame["type"] == "verdict" for frame in frames)
        assert fake_genai_client.interaction_calls == []

    def test_other_topic_cannot_be_disabled(
        self,
        client,
        fake_genai_client: FakeGenAIClient,
        fake_transcriber: FakeTranscriber,
    ) -> None:
        fake_transcriber.segments_script.append([SEVEN_WORD_SEGMENT])
        fake_genai_client.generate_results.append(
            make_gate_response([("Some general trivia claim.", 0.9, "other")])
        )
        fake_genai_client.interaction_results.append(
            make_verdict_interaction("TRUE", "Confirmed.")
        )
        with client.websocket_connect("/ws/audio") as session:
            # "other" deliberately absent from the enabled list.
            session.send_json(make_hello(enabled_topics=["sports"]))
            assert session.receive_json()["type"] == "ready"
            for _ in range(4):
                session.send_bytes(pcm_silence(0.25))
            session.send_json({"type": "stop"})
            frames, close_code = collect_frames_until_close(session)

        assert close_code == 1000
        verdicts = [f for f in frames if f["type"] == "verdict"]
        assert [v["topic"] for v in verdicts] == ["other"]

    def test_hello_without_enabled_topics_enables_all(
        self,
        client,
        fake_genai_client: FakeGenAIClient,
        fake_transcriber: FakeTranscriber,
    ) -> None:
        fake_transcriber.segments_script.append([SEVEN_WORD_SEGMENT])
        fake_genai_client.generate_results.append(
            make_gate_response([("A sports record claim.", 0.9, "sports")])
        )
        fake_genai_client.interaction_results.append(
            make_verdict_interaction("TRUE", "Confirmed.")
        )
        with client.websocket_connect("/ws/audio") as session:
            session.send_json(make_hello())  # no enabled_topics key at all
            assert session.receive_json()["type"] == "ready"
            for _ in range(4):
                session.send_bytes(pcm_silence(0.25))
            session.send_json({"type": "stop"})
            frames, close_code = collect_frames_until_close(session)

        assert close_code == 1000
        verdicts = [f for f in frames if f["type"] == "verdict"]
        assert [v["topic"] for v in verdicts] == ["sports"]
        assert not any(f.get("stage") == "topic_skipped" for f in frames)

    def test_malformed_frames_are_ignored_not_fatal(
        self,
        client,
        fake_transcriber: FakeTranscriber,
    ) -> None:
        with client.websocket_connect("/ws/audio") as session:
            session.send_json(make_hello())
            assert session.receive_json()["type"] == "ready"
            session.send_text("not json {{{")
            session.send_json({"type": "mystery_frame"})
            session.send_bytes(b"\x00\x00\x00")  # odd byte count -> dropped
            session.send_json({"type": "stop"})
            frames, close_code = collect_frames_until_close(session)

        assert close_code == 1000
        assert frames == []  # nothing transcribed, nothing verified
        assert fake_transcriber.calls == []  # bad audio never reached the ring

    def test_stt_overload_emits_throttled_error_frame(
        self,
        fake_genai_client: FakeGenAIClient,
        fake_transcriber: FakeTranscriber,
    ) -> None:
        # Tiny watermarks + an STT window the test never fills: pending audio
        # can only leave the ring via the overflow drop.
        settings = make_test_settings(
            audio_high_watermark_s=1.0,
            audio_low_watermark_s=0.5,
            stt_window_s=50.0,
            stt_hop_s=50.0,
        )
        with open_test_client(settings, fake_genai_client, fake_transcriber) as client:
            with client.websocket_connect("/ws/audio") as session:
                session.send_json(make_hello())
                assert session.receive_json()["type"] == "ready"
                for _ in range(6):  # 1.5 s > 1.0 s high watermark
                    session.send_bytes(pcm_silence(0.25))
                overload = session.receive_json()
                assert overload["type"] == "error"
                assert overload["code"] == "stt_overload"
                assert overload["fatal"] is False
                assert "dropped" in overload["message"]
                session.send_json({"type": "stop"})
                _frames, close_code = collect_frames_until_close(session)
                assert close_code == 1000


class TestSttWindowOverlap:
    def test_stt_waits_for_full_window_and_consumes_only_hop(
        self,
        client: Any,
        fake_transcriber: FakeTranscriber,
    ) -> None:
        """Regression: the live STT loop must gate on a FULL window
        (window_s), not the hop — triggering on the hop reads ~hop-sized
        windows and consumes nearly all of each, destroying the
        (window - hop) overlap. Test settings: window 1.0 s, hop 0.5 s."""
        with client.websocket_connect("/ws/audio") as session:
            session.send_json(make_hello())
            assert session.receive_json()["type"] == "ready"

            # One hop (0.5 s) pending but only half a window: the loop must
            # NOT fire yet (a hop-gated loop fires here with a short window).
            session.send_bytes(pcm_silence(0.5))
            time.sleep(0.6)  # > 2 poll cycles of QUEUE_POLL_S
            assert fake_transcriber.calls == []

            # A full window accumulates -> exactly one full-window read.
            session.send_bytes(pcm_silence(0.5))
            wait_until_sync(lambda: len(fake_transcriber.calls) >= 1)
            # The next hop arrives -> the next window starts one HOP later,
            # re-reading the trailing (window - hop) = 0.5 s as overlap.
            session.send_bytes(pcm_silence(0.5))
            wait_until_sync(lambda: len(fake_transcriber.calls) >= 2)

            session.send_json({"type": "stop"})
            _frames, close_code = collect_frames_until_close(session)

        assert close_code == 1000
        live_calls = fake_transcriber.calls[:2]
        # Full 1.0 s windows (16000 samples at 16 kHz)...
        assert [call["samples"] for call in live_calls] == [16000, 16000]
        # ...advancing by the hop, not the window: 0.5 s overlap preserved.
        assert live_calls[0]["window_start_s"] == pytest.approx(0.0)
        assert live_calls[1]["window_start_s"] == pytest.approx(0.5)
        # The stop flush drains the 0.5 s residue left by consume(hop):
        # 1.5 s sent in total, 2 x 0.5 s hops consumed -> [1.0, 1.5) remains.
        assert len(fake_transcriber.calls) == 3
        assert fake_transcriber.calls[2]["samples"] == 8000
        assert fake_transcriber.calls[2]["window_start_s"] == pytest.approx(1.0)


class TestGracefulStopDeliversInFlightWork:
    def test_in_flight_verdict_survives_stop(
        self,
        client: Any,
        fake_genai_client: FakeGenAIClient,
        fake_transcriber: FakeTranscriber,
    ) -> None:
        """Regression: a verification already running when the client sends
        ``stop`` must still deliver its verdict via the flush phase instead
        of being silently dropped by the shutdown guard."""
        nine_word_segment = TranscriptSegment(
            # >= ClaimGate.MIN_NEW_WORDS words: the gate's FIRST pass only
            # waits for enough words, so verification starts pre-stop.
            text="The Eiffel Tower in Paris is 450 meters tall",
            start=0.0,
            end=1.0,
            avg_logprob=-0.3,
            no_speech_prob=0.05,
        )
        fake_transcriber.segments_script.append([nine_word_segment])
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
            for _ in range(4):  # 1.0 s of audio -> one full STT window
                session.send_bytes(pcm_silence(0.25))
            # Wait until the verification is genuinely in flight...
            wait_until_sync(lambda: len(fake_genai_client.interaction_calls) >= 1)
            session.send_json({"type": "stop"})
            # ...and until the server has processed the stop frame, so the
            # verdict completes strictly during the wind-down.
            wait_until_sync(
                lambda: ws_module.current_pipeline is not None
                and ws_module.current_pipeline._stop_requested.is_set()
            )
            release_verdict.set()
            frames, close_code = collect_frames_until_close(session)

        assert close_code == 1000
        verdict_labels = [f["label"] for f in frames if f["type"] == "verdict"]
        assert verdict_labels == ["FALSE"]


class TestShutdownEnqueueContract:
    @staticmethod
    def build_pipeline(executor: ThreadPoolExecutor) -> SessionPipeline:
        """A SessionPipeline wired from fakes; its websocket is never used."""
        fake_client = FakeGenAIClient()
        settings = make_test_settings()
        return SessionPipeline(
            websocket=SimpleNamespace(),  # type: ignore[arg-type]
            hello=ClientHello.model_validate(make_hello()),
            settings=settings,
            transcriber=FakeTranscriber(),  # type: ignore[arg-type]
            stt_executor=executor,
            claim_gate=GeminiClaimGate(client=fake_client, model="fake-gate-model"),
            fact_checker=GeminiFactChecker(
                client=fake_client,
                verify_model="fake-verify-model",
                extraction_model="fake-gate-model",
                cooldown=QuotaCooldown(),
            ),
            verify_bucket=TokenBucket(rate_per_min=6000.0, burst=10),
            quota_cooldown=QuotaCooldown(),
        )

    async def test_internal_emits_bypass_stop_guard_but_debug_path_does_not(
        self,
    ) -> None:
        """After a stop is requested, internally generated frames must still
        be queued (the flush phase delivers them), while the external
        debug.py contract keeps failing loudly."""
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt-unit")
        try:
            pipeline = self.build_pipeline(executor)
            pipeline._stop_requested.set()
            pipeline._enqueue_or_drop(StatusFrame(claim="left-over claim"))
            assert pipeline._outbound.qsize() == 1
            with pytest.raises(RuntimeError, match="shutting down"):
                pipeline.enqueue_frame({"type": "status", "claim": "debug push"})
        finally:
            executor.shutdown(wait=False, cancel_futures=True)


# --------------------------------------------------------------------------- #
# /debug/text -> live overlay push
# --------------------------------------------------------------------------- #


class TestDebugPushToLiveSession:
    def test_verdict_frame_is_pushed_onto_open_session(
        self, client, fake_genai_client: FakeGenAIClient
    ) -> None:
        fake_genai_client.generate_results.append(make_gate_response([(CLAIM, 0.9)]))
        fake_genai_client.interaction_results.append(
            make_verdict_interaction("FALSE", "About 330 meters.")
        )
        with client.websocket_connect("/ws/audio") as session:
            session.send_json(make_hello())
            assert session.receive_json()["type"] == "ready"

            response = client.post(
                "/debug/text", json={"text": "the eiffel tower is 450 meters tall"}
            )
            assert response.status_code == 200
            assert len(response.json()["verdicts"]) == 1

            pushed = session.receive_json()
            assert pushed["type"] == "verdict"
            assert pushed["claim"] == CLAIM
            assert pushed["id"] == response.json()["verdicts"][0]["id"]

            session.send_json({"type": "stop"})
            _frames, close_code = collect_frames_until_close(session)
            assert close_code == 1000

    def test_debug_text_without_session_still_succeeds(
        self, client, fake_genai_client: FakeGenAIClient
    ) -> None:
        assert ws_module.current_pipeline is None
        fake_genai_client.generate_results.append(make_gate_response([(CLAIM, 0.9)]))
        fake_genai_client.interaction_results.append(
            make_verdict_interaction("FALSE", "About 330 meters.")
        )
        response = client.post(
            "/debug/text", json={"text": "the eiffel tower is 450 meters tall"}
        )
        assert response.status_code == 200
        assert len(response.json()["verdicts"]) == 1


# --------------------------------------------------------------------------- #
# Quota cooldown surfaces on the socket
# --------------------------------------------------------------------------- #


class TestQuotaCooldownFrames:
    def test_active_cooldown_drops_claim_with_error_frame(
        self,
        client,
        fake_genai_client: FakeGenAIClient,
        fake_transcriber: FakeTranscriber,
    ) -> None:
        client.app.state.quota_cooldown.trip(
            60.0, reason="OpenRouter credits exhausted — top up at openrouter.ai"
        )
        fake_transcriber.segments_script.append([SEVEN_WORD_SEGMENT])
        fake_genai_client.generate_results.append(make_gate_response([(CLAIM, 0.9)]))
        # No interaction scripted: the claim must be dropped before verification.
        with client.websocket_connect("/ws/audio") as session:
            session.send_json(make_hello())
            assert session.receive_json()["type"] == "ready"
            for _ in range(4):
                session.send_bytes(pcm_silence(0.25))
            session.send_json({"type": "stop"})
            frames, close_code = collect_frames_until_close(session)

        assert close_code == 1000
        cooldown_frames = [f for f in frames if f.get("code") == "quota_cooldown"]
        assert len(cooldown_frames) == 1
        assert cooldown_frames[0]["fatal"] is False
        # The frame replays the reason recorded by whichever provider tripped
        # the cooldown instead of hard-coding a provider name.
        assert "top up at openrouter.ai" in cooldown_frames[0]["message"]
        assert "Gemini" not in cooldown_frames[0]["message"]
        assert not any(frame["type"] == "verdict" for frame in frames)
        assert fake_genai_client.interaction_calls == []
