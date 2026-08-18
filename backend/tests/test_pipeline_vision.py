"""Frame ring, visual-cue vision attach, and contradiction frames on the wire.

WS-level tests follow test_ws_protocol.py conventions (scripted fakes, real
session). Frame-ring unit tests drive ``_handle_text_frame`` directly on a
pipeline built à la TestShutdownEnqueueContract.
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.llm_gemini import GeminiClaimGate, GeminiFactChecker
from app.models import ClientHello, TranscriptSegment
from app.pipeline import (
    FRAME_MAX_AGE_S,
    MAX_FRAME_B64_LEN,
    SessionPipeline,
    StoredFrame,
    has_visual_cue,
)
from app.rate_limit import QuotaCooldown, TokenBucket
from tests.conftest import (
    FakeGenAIClient,
    FakeInteractionsError,
    FakeTranscriber,
    make_frame_message,
    make_gate_response,
    make_hello,
    make_test_settings,
    make_verdict_interaction,
    open_test_client,
    pcm_silence,
)

CUE_CLAIM = "As you can see, this chart shows unemployment at five percent."
PLAIN_CLAIM = "The Eiffel Tower is 330 meters tall."

CUE_SEGMENT = TranscriptSegment(
    text="as you can see this chart shows unemployment at five percent",
    start=0.0,
    end=1.0,
    avg_logprob=-0.3,
    no_speech_prob=0.05,
)


def collect_frames_until_close(session: Any) -> tuple[list[dict[str, Any]], int]:
    frames: list[dict[str, Any]] = []
    while True:
        try:
            frames.append(session.receive_json())
        except WebSocketDisconnect as disconnect:
            return frames, disconnect.code


def build_pipeline(executor: ThreadPoolExecutor) -> SessionPipeline:
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


class TestVisualCue:
    def test_cue_phrases_match_case_insensitively(self) -> None:
        assert has_visual_cue(CUE_CLAIM)
        assert has_visual_cue("LOOK AT THIS headline about inflation")
        assert not has_visual_cue(PLAIN_CLAIM)


class TestFrameRingUnit:
    def test_valid_frame_lands_in_ring(self) -> None:
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            pipeline = build_pipeline(executor)
            pipeline._handle_text_frame(json.dumps(make_frame_message("Zm9v")))
            assert len(pipeline._frames) == 1
            assert pipeline._select_frame() == "Zm9v"
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def test_ring_keeps_only_newest_three(self) -> None:
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            pipeline = build_pipeline(executor)
            for index in range(5):
                pipeline._handle_text_frame(
                    json.dumps(make_frame_message(f"frame{index}"))
                )
            assert [frame.image_b64 for frame in pipeline._frames] == [
                "frame2",
                "frame3",
                "frame4",
            ]
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def test_oversize_and_malformed_frames_drop_non_fatally(self) -> None:
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            pipeline = build_pipeline(executor)
            pipeline._handle_text_frame(
                json.dumps(make_frame_message("x" * (MAX_FRAME_B64_LEN + 1)))
            )
            pipeline._handle_text_frame(json.dumps({"type": "frame"}))  # invalid
            assert len(pipeline._frames) == 0
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def test_stale_frame_is_never_selected(self) -> None:
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            pipeline = build_pipeline(executor)
            pipeline._frames.append(
                StoredFrame(
                    image_b64="old",
                    captured_at_ms=0,
                    received_at=time.monotonic() - FRAME_MAX_AGE_S - 1,
                )
            )
            assert pipeline._select_frame() is None
        finally:
            executor.shutdown(wait=False, cancel_futures=True)


class TestVisionAttachEndToEnd:
    def _run_session(
        self,
        client: TestClient,
        fake_transcriber: FakeTranscriber,
        send_frame: bool,
    ) -> tuple[list[dict[str, Any]], int]:
        with client.websocket_connect("/ws/audio") as session:
            session.send_json(make_hello())
            assert session.receive_json()["type"] == "ready"
            if send_frame:
                session.send_json(make_frame_message("dGVzdC1qcGVn"))
            for _ in range(4):
                session.send_bytes(pcm_silence(0.25))
            session.send_json({"type": "stop"})
            return collect_frames_until_close(session)

    def test_cue_claim_with_frame_attaches_image_parts(
        self,
        client: TestClient,
        fake_genai_client: FakeGenAIClient,
        fake_transcriber: FakeTranscriber,
    ) -> None:
        fake_transcriber.segments_script.append([CUE_SEGMENT])
        fake_genai_client.generate_results.append(
            make_gate_response([(CUE_CLAIM, 0.9, "money")])
        )
        fake_genai_client.interaction_results.append(
            make_verdict_interaction("TRUE", "The chart is legible and agrees.")
        )
        frames, close_code = self._run_session(
            client, fake_transcriber, send_frame=True
        )
        assert close_code == 1000
        assert [f["label"] for f in frames if f["type"] == "verdict"] == ["TRUE"]
        call_input = fake_genai_client.interaction_calls[0]["input"]
        assert isinstance(call_input, list)
        assert call_input[0]["type"] == "text"
        assert "A frame captured from the live stream" in call_input[0]["text"]
        assert call_input[1] == {
            "type": "image",
            "data": "dGVzdC1qcGVn",
            "mime_type": "image/jpeg",
        }

    def test_cueless_claim_keeps_plain_string_input(
        self,
        client: TestClient,
        fake_genai_client: FakeGenAIClient,
        fake_transcriber: FakeTranscriber,
    ) -> None:
        fake_transcriber.segments_script.append([CUE_SEGMENT])
        fake_genai_client.generate_results.append(
            make_gate_response([(PLAIN_CLAIM, 0.9, "history")])
        )
        fake_genai_client.interaction_results.append(
            make_verdict_interaction("FALSE", "It is 330 meters.")
        )
        frames, close_code = self._run_session(
            client, fake_transcriber, send_frame=True
        )
        assert close_code == 1000
        call_input = fake_genai_client.interaction_calls[0]["input"]
        assert isinstance(call_input, str)  # pre-vision wire shape untouched

    def test_image_failure_retries_without_image(
        self,
        client: TestClient,
        fake_genai_client: FakeGenAIClient,
        fake_transcriber: FakeTranscriber,
    ) -> None:
        """The HARD RULE end-to-end: a failing image-bearing call degrades
        to the text-only ladder and the verdict still arrives."""
        fake_transcriber.segments_script.append([CUE_SEGMENT])
        fake_genai_client.generate_results.append(
            make_gate_response([(CUE_CLAIM, 0.9, "money")])
        )
        fake_genai_client.interaction_results.append(
            FakeInteractionsError(500, "vision endpoint exploded")
        )
        fake_genai_client.interaction_results.append(
            make_verdict_interaction("TRUE", "Text-only pass succeeded.")
        )
        frames, close_code = self._run_session(
            client, fake_transcriber, send_frame=True
        )
        assert close_code == 1000
        assert [f["label"] for f in frames if f["type"] == "verdict"] == ["TRUE"]
        first, second = fake_genai_client.interaction_calls[:2]
        assert isinstance(first["input"], list)  # image attempt
        assert isinstance(second["input"], str)  # image-free fall-through


class TestContradictionEndToEnd:
    def test_high_confidence_contradiction_frame_reaches_the_wire(
        self,
        fake_genai_client: FakeGenAIClient,
        fake_transcriber: FakeTranscriber,
    ) -> None:
        """Two live gate batches produce a negation pair; the (lexical-mode,
        dead-port embedder) detector judges it and the frame is delivered.

        generate_results order is deterministic: gate batch 1, gate batch 2,
        then the judge (claim A finds no candidates, so it never judges).
        """
        from tests.conftest import make_judgement_response

        settings = make_test_settings(gate_interval_s=0.0)
        batch1 = TranscriptSegment(
            text="I have never been to Japan in my life",
            start=0.0,
            end=1.0,
            avg_logprob=-0.3,
            no_speech_prob=0.05,
        )
        batch2 = TranscriptSegment(
            text="I have been to Japan twice with my brother",
            start=1.0,
            end=2.0,
            avg_logprob=-0.3,
            no_speech_prob=0.05,
        )
        fake_transcriber.segments_script.append([batch1])
        fake_transcriber.segments_script.append([batch2])
        fake_genai_client.generate_results.append(
            make_gate_response([("I have never been to Japan.", 0.9)])
        )
        fake_genai_client.generate_results.append(
            make_gate_response([("I have been to Japan twice.", 0.9)])
        )
        fake_genai_client.generate_results.append(
            make_judgement_response(True, "high", "Never vs twice.")
        )
        # Claim A verifies; claim B is a rapidfuzz duplicate of A (>=85) so
        # it never reaches verification — no second verdict scripted.
        fake_genai_client.interaction_results.append(
            make_verdict_interaction("UNVERIFIED", "Personal claim.")
        )
        with open_test_client(settings, fake_genai_client, fake_transcriber) as client:
            with client.websocket_connect("/ws/audio") as session:
                session.send_json(make_hello())
                assert session.receive_json()["type"] == "ready"
                for _ in range(4):
                    session.send_bytes(pcm_silence(0.25))
                # Let batch 1 flow through gate + detector, then send batch 2.
                first_frames: list[dict[str, Any]] = []
                while not any(f["type"] == "verdict" for f in first_frames):
                    first_frames.append(session.receive_json())
                for _ in range(4):
                    session.send_bytes(pcm_silence(0.25))
                contradiction: dict[str, Any] | None = None
                while contradiction is None:
                    frame = session.receive_json()
                    if frame["type"] == "contradiction":
                        contradiction = frame
                session.send_json({"type": "stop"})
                collect_frames_until_close(session)
        assert contradiction == {
            "type": "contradiction",
            "current_claim": "I have been to Japan twice.",
            "prior_claim": "I have never been to Japan.",
            "prior_claimed_at": contradiction["prior_claimed_at"],
            "confidence": "high",
            "explanation": "Never vs twice.",
        }
        assert contradiction["prior_claimed_at"].endswith("Z")
