"""Tests for the outbound message formatter.

``TestCommandInjectionGuard`` is the one that matters most: the bot is a
moderator, so a message Twitch reads as starting with ``/`` or ``.`` executes a
moderation command rather than saying anything.
"""

import pytest

from app.models import Source, Verdict

from streamer.chat.format import (
    MAX_CHAT_CHARS,
    SAFE_CHAT_CHARS,
    claim_is_postable,
    explanation_is_postable,
    format_correction,
    format_retraction,
    format_verdict_post,
    sanitize_for_chat,
    source_label,
    truncate_to_sentences,
    verdict_handle,
    visible_length,
)

REUTERS = Source(url="https://www.reuters.com/world/a", title="Reuters")
APNEWS = Source(url="https://apnews.com/article/b", title="AP")
NASA = Source(url="https://www.nasa.gov/c", title="NASA")


def make_verdict(**overrides) -> Verdict:
    defaults = dict(
        claim="The Eiffel Tower is 450 metres tall.",
        label="FALSE",
        explanation="It is 330 m tall including antennas, not 450.",
        sources=[REUTERS, APNEWS],
        topic="other",
    )
    defaults.update(overrides)
    return Verdict(**defaults)  # type: ignore[arg-type]


class TestVisibleLength:
    def test_counts_utf16_units_not_python_characters(self) -> None:
        """Twitch counts UTF-16 units, so an emoji is 2. Measuring with len()
        would let an emoji-heavy message sail past the check."""
        assert visible_length("🤖") == 2
        assert visible_length("abc") == 3

    def test_cjk_is_one_unit_but_three_utf8_bytes(self) -> None:
        """Why both budgets are enforced: neither bounds the other."""
        text = "日本語"
        assert visible_length(text) == 3
        assert len(text.encode("utf-8")) == 9


class TestCommandInjectionGuard:
    @pytest.mark.parametrize(
        "hostile",
        [
            "/ban everyone",
            ".timeout somemod 600",
            "!raid otherchannel",
            "/clear",
            "\n/ban x",
            "\r\nPRIVMSG #other :hi",
            "‮txet desrever",
            "​zero width",
            "  /ban leading-space",
        ],
    )
    def test_hostile_text_never_produces_a_command(self, hostile: str) -> None:
        verdict = make_verdict(
            claim="The Eiffel Tower is 450 metres tall.",
            explanation=f"{hostile} It is 330 m tall including antennas, not 450.",
        )
        message = format_verdict_post(verdict)
        if message is None:
            return  # dropping is always an acceptable outcome
        assert message.startswith(("❌", "⚠️", "✅", "❓", "↩️", "🤖", "🕰️"))
        assert "\n" not in message and "\r" not in message
        assert not message.lstrip().startswith(("/", ".", "!"))

    def test_sanitize_rejects_text_with_no_leading_marker(self) -> None:
        """A message we did not intend to build is exactly the one not to
        send, so this refuses rather than stripping."""
        assert sanitize_for_chat("/ban everyone") is None
        assert sanitize_for_chat("plain text") is None

    def test_sanitize_strips_control_and_format_characters(self) -> None:
        message = sanitize_for_chat("🤖 a​b‮c\rd\ne")
        assert message is not None
        for forbidden in ("​", "‮", "\r", "\n"):
            assert forbidden not in message

    def test_sanitize_collapses_whitespace(self) -> None:
        assert sanitize_for_chat("🤖   a    b  ") == "🤖 a b"


class TestContentFilter:
    @pytest.mark.parametrize(
        "text",
        [
            "🤖 contact me at someone@example.com",
            "🤖 call +1 415 555 0132 now",
            "🤖 ask @somestreamer about it",
        ],
    )
    def test_pii_and_mentions_are_refused(self, text: str) -> None:
        assert sanitize_for_chat(text) is None

    def test_mention_is_refused_because_it_pings_a_real_person(self) -> None:
        verdict = make_verdict(explanation="See @someuser for the correct figure.")
        assert format_verdict_post(verdict) is None


class TestLengthBudget:
    def test_worst_case_message_stays_within_both_budgets(self) -> None:
        verdict = make_verdict(
            claim="A" * 199 + ".",
            explanation="B" * 60 + ". " + "C" * 380 + ".",
            sources=[REUTERS, APNEWS, NASA],
        )
        message = format_verdict_post(verdict, template="verbose")
        assert message is not None
        assert visible_length(message) <= SAFE_CHAT_CHARS
        assert len(message.encode("utf-8")) <= MAX_CHAT_CHARS

    def test_emoji_heavy_claim_respects_the_utf16_budget(self) -> None:
        verdict = make_verdict(claim="🤖" * 90 + " is a robot claim.")
        message = format_verdict_post(verdict)
        if message is not None:
            assert visible_length(message) <= SAFE_CHAT_CHARS


class TestTruncation:
    def test_keeps_whole_sentences_only(self) -> None:
        text = "One sentence here. Two sentence here. Three sentence here."
        # 18 units for the first, 37 for the first two — so a 30-unit budget
        # takes exactly one and never a fragment of the second.
        assert truncate_to_sentences(text, 30) == "One sentence here."
        assert (
            truncate_to_sentences(text, 40) == "One sentence here. Two sentence here."
        )

    def test_returns_none_when_even_one_sentence_will_not_fit(self) -> None:
        assert truncate_to_sentences("A very long single sentence here.", 5) is None

    def test_short_text_is_returned_whole(self) -> None:
        assert truncate_to_sentences("Short.", 100) == "Short."

    def test_claim_is_never_truncated(self) -> None:
        """A cut claim could invert its own meaning, so an over-long claim is
        rejected by the predicate instead of being trimmed."""
        claim = "The Eiffel Tower is 450 metres tall."
        verdict = make_verdict(claim=claim, explanation="X" * 400 + ".")
        message = format_verdict_post(verdict)
        if message is not None:
            assert claim in message

    def test_url_style_degrades_to_domain_rather_than_cutting(self) -> None:
        long_url = "https://example.gov/" + "p" * 400
        verdict = make_verdict(sources=[Source(url=long_url, title=None)])
        rendered = source_label(verdict, style="url", budget=40, limit=2)
        assert rendered == "[example.gov]"
        assert "pppp" not in rendered


class TestTemplates:
    def test_standard_contains_label_claim_sources_and_handle(self) -> None:
        verdict = make_verdict()
        message = format_verdict_post(verdict)
        assert message is not None
        assert message.startswith("❌ FALSE · ")
        assert '"The Eiffel Tower is 450 metres tall."' in message
        assert "reuters.com" in message
        assert f"!fc why {verdict_handle(verdict)}" in message

    def test_compact_omits_the_explanation(self) -> None:
        verdict = make_verdict()
        message = format_verdict_post(verdict, template="compact")
        assert message is not None
        assert "330 m tall" not in message
        assert "reuters.com" in message

    def test_misleading_uses_its_own_marker(self) -> None:
        message = format_verdict_post(make_verdict(label="MISLEADING"))
        assert message is not None and message.startswith("⚠️ MISLEADING")

    def test_extra_sources_are_summarized_not_listed(self) -> None:
        verdict = make_verdict(sources=[REUTERS, APNEWS, NASA])
        rendered = source_label(verdict, style="domain", budget=200, limit=2)
        assert rendered == "[reuters.com, apnews.com +1]"

    def test_every_message_carries_the_bot_marker(self) -> None:
        """A cropped screenshot must still identify itself as automated."""
        for message in (
            format_verdict_post(make_verdict()),
            format_retraction("3f2a"),
            format_correction("3f2a", "MISLEADING"),
        ):
            assert message is not None and "🤖" in message


class TestRetraction:
    def test_retraction_wording_is_fixed(self) -> None:
        message = format_retraction("3f2a")
        assert message is not None
        assert message.startswith("↩️ RETRACTED")
        assert "3f2a" in message


class TestClaimShape:
    @pytest.mark.parametrize(
        ("claim", "reason"),
        [
            ("The Eiffel Tower is 450 metres tall.", "ok"),
            ("Too short.", "claim_too_short"),
            ("A" * 201 + ".", "claim_too_long"),
            ("The Eiffel Tower in Paris is 450 metres tall", "malformed_claim"),
            ('He said "it is 450 m" yesterday.', "malformed_claim"),
            ("I have never been to Japan and never will.", "first_person_claim"),
            ("Your tax plan raises rates on everyone.", "first_person_claim"),
            ("Musk said Tesla sold two million cars last year.", "reported_speech"),
        ],
    )
    def test_shape_rules(self, claim: str, reason: str) -> None:
        ok, actual = claim_is_postable(claim)
        assert actual == reason
        assert ok is (reason == "ok")


class TestExplanationShape:
    def test_short_explanation_is_not_a_receipt(self) -> None:
        assert explanation_is_postable("False.") == (False, "explanation_too_short")

    def test_url_in_prose_is_rejected(self) -> None:
        ok, reason = explanation_is_postable(
            "The real figure is 330 m, see https://example.gov/tower for detail."
        )
        assert (ok, reason) == (False, "explanation_has_url")

    def test_a_real_explanation_passes(self) -> None:
        assert explanation_is_postable(
            "It is 330 m tall including antennas, not 450."
        ) == (True, "ok")
