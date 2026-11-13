"""Transcript sessions: the client transcribes, the backend gates and verifies.

Covers the hello ``format: "transcript"`` path end to end over the real
WebSocket, the ``STT_BACKEND=none`` option that serves only such sessions,
and the guard rails between the two session kinds.
"""

import sqlite3
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.models import ClientHello, ClientTranscript
from app.transcriber import NullTranscriber, create_transcriber
from tests.conftest import (
    FakeLLMClient,
    FakeTranscriber,
    make_gate_response,
    make_hello,
    make_test_settings,
    make_verdict_completion,
    open_test_client,
    pcm_silence,
)
from tests.test_pipeline_persistence import collect_frames_until_close, wait_until

CLAIM = "The Eiffel Tower is 330 meters tall."


def transcript(text: str, start: float = 0.0, end: float = 4.0) -> dict[str, Any]:
    return {"type": "transcript", "text": text, "start": start, "end": end}


def run_transcript_session(
    client: TestClient, frames_to_send: list[Any], **hello_overrides: Any
) -> tuple[list[dict[str, Any]], int]:
    """Open a transcript session, send the frames, stop, collect the rest."""
    with client.websocket_connect("/ws/audio") as session:
        session.send_json(make_hello(format="transcript", **hello_overrides))
        assert session.receive_json()["type"] == "ready"
        for frame in frames_to_send:
            if isinstance(frame, bytes):
                session.send_bytes(frame)
            else:
                session.send_json(frame)
        session.send_json({"type": "stop"})
        return collect_frames_until_close(session)


class TestTranscriptSession:
    def test_client_text_is_gated_and_verified_without_the_speech_engine(
        self,
        client: TestClient,
        fake_llm_client: FakeLLMClient,
        fake_transcriber: FakeTranscriber,
    ) -> None:
        fake_llm_client.gate_results.append(
            make_gate_response([(CLAIM, 0.9, "history")])
        )
        fake_llm_client.verify_results.append(
            make_verdict_completion("TRUE", "Official figure.")
        )
        frames, close_code = run_transcript_session(
            client,
            [
                transcript("so the eiffel tower is", 0.0, 2.0),
                transcript("three hundred and thirty meters tall", 2.0, 5.5),
            ],
        )
        assert close_code == 1000
        assert [f["label"] for f in frames if f["type"] == "verdict"] == ["TRUE"]
        # The gate saw both utterances, in order, as one stretch of speech.
        gate_prompt = fake_llm_client.gate_calls[0]["messages"][0]["content"]
        assert (
            "so the eiffel tower is three hundred and thirty meters tall" in gate_prompt
        )
        # The speech engine was never asked for anything.
        assert fake_transcriber.calls == []
        # Nothing is echoed back: the client already has its own text.
        assert not [f for f in frames if f["type"] == "transcript"]

    def test_audio_sent_on_a_transcript_session_is_ignored(
        self,
        client: TestClient,
        fake_llm_client: FakeLLMClient,
        fake_transcriber: FakeTranscriber,
    ) -> None:
        fake_llm_client.gate_results.append(make_gate_response([]))
        frames, close_code = run_transcript_session(
            client,
            [
                pcm_silence(0.25),
                pcm_silence(0.25),
                transcript("a few words spoken here today"),
            ],
        )
        assert close_code == 1000
        assert fake_transcriber.calls == []
        assert not [f for f in frames if f["type"] == "error"]

    def test_an_immediately_repeated_utterance_is_dropped(
        self, client: TestClient, fake_llm_client: FakeLLMClient
    ) -> None:
        fake_llm_client.gate_results.append(make_gate_response([]))
        run_transcript_session(
            client,
            [
                transcript("Thank you.", 0.0, 1.0),
                transcript("thank  you.", 1.0, 2.0),
                transcript("Thank you.", 2.0, 3.0),
                transcript("and now back to the budget numbers we discussed", 3.0, 6.0),
            ],
        )
        gate_prompt = fake_llm_client.gate_calls[0]["messages"][0]["content"]
        assert gate_prompt.count("Thank you.") == 1

    def test_invalid_transcript_frames_are_ignored_not_fatal(
        self, client: TestClient, fake_llm_client: FakeLLMClient
    ) -> None:
        fake_llm_client.gate_results.append(make_gate_response([]))
        frames, close_code = run_transcript_session(
            client,
            [
                {"type": "transcript", "text": "", "start": 0, "end": 1},
                {"type": "transcript", "text": "no times"},
                {"type": "transcript", "text": "x" * 2001, "start": 0, "end": 1},
                {"type": "transcript", "text": "negative", "start": -1, "end": 1},
                transcript("this one is perfectly fine and long enough"),
            ],
        )
        assert close_code == 1000
        assert not [f for f in frames if f["type"] == "error"]
        gate_prompt = fake_llm_client.gate_calls[0]["messages"][0]["content"]
        assert "perfectly fine" in gate_prompt
        assert "negative" not in gate_prompt

    def test_session_row_counts_speech_from_the_transcript_times(
        self,
        client: TestClient,
        app_settings: Settings,
        fake_llm_client: FakeLLMClient,
    ) -> None:
        fake_llm_client.gate_results.append(make_gate_response([]))
        run_transcript_session(
            client,
            [transcript("one two three four five six", 10.0, 14.0)],
            platform="youtube",
            channel="@newsanalyst",
        )

        def ended_row() -> tuple | None:
            with sqlite3.connect(app_settings.db_path) as conn:
                return conn.execute(
                    "SELECT channel, speech_seconds, audio_seconds FROM sessions"
                    " WHERE ended_at IS NOT NULL"
                ).fetchone()

        wait_until(lambda: ended_row() is not None)
        assert ended_row() == ("@newsanalyst", 4.0, 14.0)

    def test_a_transcript_frame_on_an_audio_session_is_ignored(
        self,
        client: TestClient,
        fake_llm_client: FakeLLMClient,
    ) -> None:
        with client.websocket_connect("/ws/audio") as session:
            session.send_json(make_hello())
            assert session.receive_json()["type"] == "ready"
            session.send_json(transcript("words that must never reach the gate at all"))
            session.send_json({"type": "stop"})
            _frames, close_code = collect_frames_until_close(session)
        assert close_code == 1000
        # Nothing was buffered, so the final gate pass had nothing to send.
        assert fake_llm_client.gate_calls == []


class TestHelloFormat:
    def test_both_formats_are_valid_and_others_are_not(self) -> None:
        assert ClientHello.model_validate(make_hello()).format == "pcm_s16le"
        assert (
            ClientHello.model_validate(make_hello(format="transcript")).format
            == "transcript"
        )
        with pytest.raises(ValidationError):
            ClientHello.model_validate(make_hello(format="mp3"))

    def test_transcript_frame_bounds(self) -> None:
        ClientTranscript.model_validate(transcript("ok"))
        for bad in (
            {"type": "transcript", "text": "", "start": 0, "end": 1},
            {"type": "transcript", "text": "x" * 2001, "start": 0, "end": 1},
            {"type": "transcript", "text": "x", "start": -0.1, "end": 1},
        ):
            with pytest.raises(ValidationError):
                ClientTranscript.model_validate(bad)


class TestNoSpeechModel:
    """STT_BACKEND=none: the backend serves transcript sessions only."""

    @pytest.fixture()
    def no_stt_client(self, fake_llm_client: FakeLLMClient) -> Any:
        settings = make_test_settings(stt_backend="none")
        transcriber = FakeTranscriber()
        with open_test_client(settings, fake_llm_client, transcriber) as test_client:
            yield test_client

    def test_factory_builds_a_transcriber_that_loads_nothing(self) -> None:
        transcriber = create_transcriber(make_test_settings(stt_backend="none"))
        assert isinstance(transcriber, NullTranscriber)
        transcriber.load()
        assert transcriber.is_loaded is False
        assert "STT_BACKEND=none" in transcriber.describe()
        with pytest.raises(RuntimeError, match="no speech model"):
            transcriber._run_model(None)  # type: ignore[arg-type]

    def test_settings_report_no_model(self) -> None:
        settings = make_test_settings(stt_backend="none")
        assert settings.stt_enabled is False
        assert settings.stt_model_name == "none"
        assert make_test_settings().stt_enabled is True

    def test_healthz_says_what_a_session_may_send(
        self, no_stt_client: TestClient, client: TestClient
    ) -> None:
        without_model = no_stt_client.get("/healthz").json()
        assert without_model["accepts_audio"] is False
        assert without_model["accepts_transcripts"] is True
        with_model = client.get("/healthz").json()
        assert with_model["accepts_audio"] is True
        assert with_model["accepts_transcripts"] is True

    def test_an_audio_session_is_refused_with_a_reason(
        self, no_stt_client: TestClient
    ) -> None:
        with no_stt_client.websocket_connect("/ws/audio") as session:
            session.send_json(make_hello())
            frames, close_code = collect_frames_until_close(session)
        assert close_code == 1011
        assert frames[0]["type"] == "error"
        assert frames[0]["code"] == "stt_disabled"
        assert frames[0]["fatal"] is True
        assert "This browser" in frames[0]["message"]

    def test_a_transcript_session_works(
        self, no_stt_client: TestClient, fake_llm_client: FakeLLMClient
    ) -> None:
        fake_llm_client.gate_results.append(
            make_gate_response([(CLAIM, 0.9, "history")])
        )
        fake_llm_client.verify_results.append(
            make_verdict_completion("TRUE", "Official figure.")
        )
        frames, close_code = run_transcript_session(
            no_stt_client,
            [transcript("the eiffel tower is three hundred thirty meters tall")],
        )
        assert close_code == 1000
        assert [f["label"] for f in frames if f["type"] == "verdict"] == ["TRUE"]
