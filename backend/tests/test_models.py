"""Model vocabulary tests: wire-frame shapes, validation, helpers."""

import re

import pytest
from pydantic import ValidationError

from app.models import (
    ClientConfig,
    ClientHello,
    DebugTextRequest,
    ErrorFrame,
    GateClaim,
    ReadyFrame,
    Source,
    StatusFrame,
    TranscriptSegment,
    Verdict,
    VerdictFrame,
    new_verdict_id,
    utc_now_iso,
)
from tests.conftest import make_hello

ISO_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class TestHelpers:
    def test_utc_now_iso_wire_format(self) -> None:
        assert ISO_Z_RE.match(utc_now_iso())

    def test_new_verdict_id_is_unique_hex(self) -> None:
        first, second = new_verdict_id(), new_verdict_id()
        assert first != second
        assert re.fullmatch(r"[0-9a-f]{32}", first)


class TestClientHello:
    def test_valid_hello_parses(self) -> None:
        hello = ClientHello.model_validate(make_hello())
        assert hello.sensitivity == "medium"
        assert hello.send_transcripts is True

    def test_optional_fields_default(self) -> None:
        minimal = make_hello()
        del minimal["sensitivity"]
        del minimal["send_transcripts"]
        hello = ClientHello.model_validate(minimal)
        assert hello.sensitivity == "medium"
        assert hello.send_transcripts is True

    @pytest.mark.parametrize(
        "overrides",
        [
            {"type": "hi"},
            {"version": 2},
            {"format": "pcm_f32le"},
            {"sample_rate": 44100},
            {"channels": 2},
            {"sensitivity": "extreme"},
        ],
    )
    def test_invalid_hello_rejected(self, overrides: dict) -> None:
        with pytest.raises(ValidationError):
            ClientHello.model_validate(make_hello(**overrides))


class TestClientConfig:
    def test_valid_config(self) -> None:
        config = ClientConfig.model_validate({"type": "config", "sensitivity": "high"})
        assert config.sensitivity == "high"

    def test_invalid_sensitivity_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ClientConfig.model_validate({"type": "config", "sensitivity": "max"})


class TestGateClaim:
    @pytest.mark.parametrize("score", [0.0, 0.55, 1.0])
    def test_check_worthiness_in_range(self, score: float) -> None:
        claim = GateClaim(claim_text="The sky is blue.", check_worthiness=score)
        assert claim.check_worthiness == score

    @pytest.mark.parametrize("score", [-0.1, 1.5])
    def test_check_worthiness_out_of_range_rejected(self, score: float) -> None:
        with pytest.raises(ValidationError):
            GateClaim(claim_text="The sky is blue.", check_worthiness=score)


class TestVerdict:
    def test_defaults(self) -> None:
        verdict = Verdict(
            claim="The Eiffel Tower is 450 meters tall.",
            label="FALSE",
            explanation="It is about 330 meters tall.",
            sources=[Source(url="https://example.com")],
        )
        assert re.fullmatch(r"[0-9a-f]{32}", verdict.id)
        assert ISO_Z_RE.match(verdict.checked_at)
        assert verdict.used_fallback is False
        assert verdict.sources[0].title is None

    def test_verdict_frame_round_trip_matches_wire_shape(self) -> None:
        verdict = Verdict(
            claim="The Eiffel Tower is 450 meters tall.",
            label="FALSE",
            explanation="It is about 330 meters tall.",
            sources=[Source(url="https://example.com/a", title="A")],
            used_fallback=True,
        )
        frame = VerdictFrame.from_verdict(verdict).model_dump()
        assert set(frame) == {
            "type",
            "id",
            "claim",
            "label",
            "explanation",
            "sources",
            "checked_at",
            "used_fallback",
        }
        assert frame["type"] == "verdict"
        assert frame["id"] == verdict.id
        assert frame["label"] == "FALSE"
        assert frame["used_fallback"] is True
        assert frame["sources"] == [{"url": "https://example.com/a", "title": "A"}]


class TestServerFrames:
    def test_ready_frame_shape(self) -> None:
        frame = ReadyFrame(server_version="0.1.0", model="distil-small.en")
        assert frame.model_dump() == {
            "type": "ready",
            "server_version": "0.1.0",
            "model": "distil-small.en",
        }

    def test_status_frame_stage_is_verifying(self) -> None:
        frame = StatusFrame(claim="The moon is made of cheese.")
        assert frame.model_dump() == {
            "type": "status",
            "stage": "verifying",
            "claim": "The moon is made of cheese.",
        }

    def test_error_frame_defaults_non_fatal(self) -> None:
        frame = ErrorFrame(code="llm_failure", message="boom")
        assert frame.model_dump() == {
            "type": "error",
            "code": "llm_failure",
            "message": "boom",
            "fatal": False,
        }

    def test_error_frame_rejects_unknown_code(self) -> None:
        with pytest.raises(ValidationError):
            ErrorFrame(code="mystery", message="boom")


class TestTranscriptSegment:
    def test_fields(self) -> None:
        segment = TranscriptSegment(
            text="hello world",
            start=1.0,
            end=2.5,
            avg_logprob=-0.3,
            no_speech_prob=0.1,
        )
        assert segment.end > segment.start


class TestDebugTextRequest:
    def test_sensitivity_defaults_to_medium(self) -> None:
        request = DebugTextRequest(text="The earth is flat.")
        assert request.sensitivity == "medium"
