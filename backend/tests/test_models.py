"""Model vocabulary tests: wire-frame shapes, validation, helpers."""

import re

import pytest
from pydantic import ValidationError

from app.models import (
    TOPICS,
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
    resolve_enabled_topics,
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

    def test_enabled_topics_defaults_to_none_meaning_all(self) -> None:
        hello = ClientHello.model_validate(make_hello())
        assert hello.enabled_topics is None

    def test_enabled_topics_parses_slug_list(self) -> None:
        hello = ClientHello.model_validate(
            make_hello(enabled_topics=["politics", "health"])
        )
        assert hello.enabled_topics == ["politics", "health"]

    def test_unknown_topic_slugs_never_reject_the_hello(self) -> None:
        # Unknown slugs are ignored at resolve time with a WARNING, never a
        # validation error (a 1008 close would strand forward-compat clients).
        hello = ClientHello.model_validate(
            make_hello(enabled_topics=["politics", "astrology"])
        )
        assert hello.enabled_topics == ["politics", "astrology"]

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
        assert config.enabled_topics is None  # absent field = leave unchanged

    def test_topics_only_config_is_valid(self) -> None:
        config = ClientConfig.model_validate(
            {"type": "config", "enabled_topics": ["sports", "gaming"]}
        )
        assert config.sensitivity is None
        assert config.enabled_topics == ["sports", "gaming"]

    def test_both_fields_optional(self) -> None:
        config = ClientConfig.model_validate({"type": "config"})
        assert config.sensitivity is None
        assert config.enabled_topics is None

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

    def test_topic_defaults_to_other(self) -> None:
        claim = GateClaim(claim_text="The sky is blue.", check_worthiness=0.5)
        assert claim.topic == "other"

    @pytest.mark.parametrize("topic", TOPICS)
    def test_known_topic_slugs_pass_through(self, topic: str) -> None:
        claim = GateClaim.model_validate(
            {"claim_text": "x", "check_worthiness": 0.5, "topic": topic}
        )
        assert claim.topic == topic

    @pytest.mark.parametrize("topic", ["astrology", "", None, 42, ["politics"]])
    def test_unknown_or_missing_topic_coerces_to_other(self, topic: object) -> None:
        claim = GateClaim.model_validate(
            {"claim_text": "x", "check_worthiness": 0.5, "topic": topic}
        )
        assert claim.topic == "other"

    @pytest.mark.parametrize(
        ("raw_topic", "expected"),
        [("Politics", "politics"), (" SPORTS ", "sports"), ("Health", "health")],
    )
    def test_case_or_whitespace_topic_normalizes_to_canonical_slug(
        self, raw_topic: str, expected: str
    ) -> None:
        claim = GateClaim.model_validate(
            {"claim_text": "x", "check_worthiness": 0.5, "topic": raw_topic}
        )
        assert claim.topic == expected


class TestResolveEnabledTopics:
    def test_none_means_all_topics(self) -> None:
        assert resolve_enabled_topics(None) == frozenset(TOPICS)

    def test_other_is_always_enabled(self) -> None:
        assert resolve_enabled_topics(["politics"]) == {"politics", "other"}
        assert resolve_enabled_topics([]) == {"other"}

    def test_unknown_slugs_ignored_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING", logger="app.models"):
            enabled = resolve_enabled_topics(["health", "astrology", "vibes"])
        assert enabled == {"health", "other"}
        assert "astrology" in caplog.text
        assert "vibes" in caplog.text


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
        assert verdict.topic == "other"
        assert verdict.sources[0].title is None

    def test_verdict_frame_round_trip_matches_wire_shape(self) -> None:
        verdict = Verdict(
            claim="The Eiffel Tower is 450 meters tall.",
            topic="history",
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
            "topic",
            "label",
            "explanation",
            "sources",
            "checked_at",
            "used_fallback",
        }
        assert frame["type"] == "verdict"
        assert frame["id"] == verdict.id
        assert frame["topic"] == "history"
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

    def test_status_frame_stage_defaults_to_verifying_without_topic_key(self) -> None:
        # "verifying" frames stay byte-identical to the pre-topic wire shape.
        frame = StatusFrame(claim="The moon is made of cheese.")
        assert frame.model_dump() == {
            "type": "status",
            "stage": "verifying",
            "claim": "The moon is made of cheese.",
        }

    def test_status_frame_topic_skipped_carries_topic(self) -> None:
        frame = StatusFrame(
            stage="topic_skipped",
            claim="The moon is made of cheese.",
            topic="science_tech",
        )
        assert frame.model_dump() == {
            "type": "status",
            "stage": "topic_skipped",
            "claim": "The moon is made of cheese.",
            "topic": "science_tech",
        }

    def test_status_frame_rejects_unknown_stage(self) -> None:
        with pytest.raises(ValidationError):
            StatusFrame(stage="skipped", claim="x")  # type: ignore[arg-type]

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

    def test_enabled_topics_defaults_to_none_meaning_all(self) -> None:
        request = DebugTextRequest(text="The earth is flat.")
        assert request.enabled_topics is None


class TestSessionIdentityFields:
    def test_old_hello_without_identity_stays_valid(self) -> None:
        hello = ClientHello.model_validate(make_hello())
        assert hello.platform is None
        assert hello.channel is None
        assert hello.stream_title is None

    def test_identity_fields_parse(self) -> None:
        hello = ClientHello.model_validate(
            make_hello(platform="kick", channel="somebody", stream_title="Live!")
        )
        assert hello.platform == "kick"
        assert hello.channel == "somebody"
        assert hello.stream_title == "Live!"

    def test_unknown_platform_is_rejected(self) -> None:
        import pytest as _pytest
        from pydantic import ValidationError as _ValidationError

        with _pytest.raises(_ValidationError):
            ClientHello.model_validate(make_hello(platform="myspace"))


class TestGateClaimId:
    def test_server_assigns_a_unique_id(self) -> None:
        from app.models import GateClaim

        first = GateClaim(claim_text="a claim", check_worthiness=0.5, topic="other")
        second = GateClaim(claim_text="a claim", check_worthiness=0.5, topic="other")
        assert first.id and second.id and first.id != second.id

    def test_model_supplied_id_is_discarded(self) -> None:
        """Gemini derives its response schema from this Pydantic class, so a
        model COULD emit ids; the before-validator must strip them or they
        become colliding primary keys."""
        from app.models import GateClaim

        claim = GateClaim.model_validate(
            {
                "id": "1",
                "claim_text": "a claim",
                "check_worthiness": 0.5,
                "topic": "other",
            }
        )
        assert claim.id != "1"
        assert len(claim.id) == 32  # server uuid4 hex


class TestFeedbackRequest:
    def test_minimal_body_parses(self) -> None:
        from app.models import FeedbackRequest

        body = FeedbackRequest.model_validate({"verdict_id": "v1", "rating": "up"})
        assert body.corrected_label is None and body.note is None

    def test_bad_rating_rejected(self) -> None:
        import pytest as _pytest
        from pydantic import ValidationError as _ValidationError

        from app.models import FeedbackRequest

        with _pytest.raises(_ValidationError):
            FeedbackRequest.model_validate({"verdict_id": "v1", "rating": "meh"})
