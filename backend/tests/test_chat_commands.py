"""Tests for the pure command grammar and authorization (commands.py)."""

import pytest

from streamer.chat.commands import (
    COMMAND_ROLES,
    Command,
    is_authorized,
    mod_attempted_broadcaster_verb,
    parse_command,
    parse_mute_duration,
    reply_dispute_noted,
    reply_help,
    valid_handle,
)
from streamer.chat.transport import ChatMessage


def make_message(
    *, is_broadcaster: bool = False, is_moderator: bool = False
) -> ChatMessage:
    return ChatMessage(
        channel="chan",
        login="user",
        display_name="User",
        user_id="1" if not is_broadcaster else "42",
        room_id="42",
        text="",
        is_broadcaster=is_broadcaster,
        is_moderator=is_moderator or is_broadcaster,
    )


VIEWER = make_message()
MOD = make_message(is_moderator=True)
BROADCASTER = make_message(is_broadcaster=True)


class TestParse:
    def test_bare_prefix_is_status(self) -> None:
        assert parse_command("!fc") == Command(verb="status", args=())

    def test_verb_is_case_insensitive(self) -> None:
        assert parse_command("!FC MUTE 15m") == Command(verb="mute", args=("15m",))

    def test_extra_whitespace_is_tolerated(self) -> None:
        assert parse_command("  !fc   cap   6  ") == Command(verb="cap", args=("6",))

    def test_prefix_must_be_a_whole_word(self) -> None:
        assert parse_command("!fcx off") is None
        assert parse_command("!fcoff") is None

    def test_unknown_verbs_are_silence(self) -> None:
        assert parse_command("!fc frobnicate") is None

    def test_non_commands_are_none(self) -> None:
        assert parse_command("hello chat") is None
        assert parse_command("") is None

    def test_oversized_args_are_rejected_not_truncated(self) -> None:
        assert parse_command("!fc why " + "a" * 65) is None
        assert parse_command("!fc dispute " + "x " * 9) is None


class TestAuthorization:
    @pytest.mark.parametrize("verb", ["help", "about", "status", "why", "dispute"])
    def test_viewer_verbs(self, verb: str) -> None:
        assert is_authorized(VIEWER, verb) is True

    @pytest.mark.parametrize(
        "verb",
        ["on", "off", "review", "mute", "unmute", "cap", "topics", "labels", "retract"],
    )
    def test_mod_verbs_denied_to_viewers(self, verb: str) -> None:
        assert is_authorized(VIEWER, verb) is False
        assert is_authorized(MOD, verb) is True
        assert is_authorized(BROADCASTER, verb) is True

    @pytest.mark.parametrize("verb", ["enable", "disable", "correct", "trust"])
    def test_broadcaster_verbs_denied_to_mods(self, verb: str) -> None:
        assert is_authorized(MOD, verb) is False
        assert is_authorized(BROADCASTER, verb) is True
        assert mod_attempted_broadcaster_verb(MOD, verb) is True
        assert mod_attempted_broadcaster_verb(VIEWER, verb) is False

    def test_unknown_verb_is_never_authorized(self) -> None:
        assert is_authorized(BROADCASTER, "sudo") is False

    def test_every_verb_has_a_role(self) -> None:
        assert set(COMMAND_ROLES.values()) <= {"viewer", "moderator", "broadcaster"}


class TestMuteDuration:
    def test_default_is_fifteen_minutes(self) -> None:
        assert parse_mute_duration(()) == 900.0

    @pytest.mark.parametrize(
        ("arg", "seconds"), [("5m", 300.0), ("30m", 1800.0), ("2h", 7200.0)]
    )
    def test_minutes_and_hours(self, arg: str, seconds: float) -> None:
        assert parse_mute_duration((arg,)) == seconds

    def test_rest_is_unbounded(self) -> None:
        assert parse_mute_duration(("rest",)) == float("inf")

    @pytest.mark.parametrize("junk", ["forever", "5", "0m", "9h", "-5m", "1000m"])
    def test_junk_is_invalid_not_defaulted(self, junk: str) -> None:
        assert parse_mute_duration((junk,)) is None


class TestHandleValidation:
    def test_valid_handles_pass_lowercased(self) -> None:
        assert valid_handle("3F2A") == "3f2a"
        assert valid_handle("abcdef") == "abcdef"

    @pytest.mark.parametrize(
        "junk", ["xyz", "3f2", "1234567", "/ban", "<script>", "3f2a; DROP"]
    )
    def test_anything_else_is_rejected(self, junk: str) -> None:
        """The validator that makes replies injection-proof: a handle is only
        ever echoed after matching ^[0-9a-f]{4,6}$."""
        assert valid_handle(junk) is None


class TestReplies:
    def test_replies_start_with_the_bot_marker(self) -> None:
        assert reply_help().startswith("🤖")
        assert reply_dispute_noted("3f2a").startswith("🤖")

    def test_dispute_reply_carries_only_the_validated_handle(self) -> None:
        reply = reply_dispute_noted("3f2a")
        assert "3f2a" in reply
