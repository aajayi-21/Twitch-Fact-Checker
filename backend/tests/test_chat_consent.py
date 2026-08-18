"""Tests for the four consent proofs (streamer/chat/consent.py).

Every proof must fail individually with its own slug, fail-closed on missing
data, and the ordering must surface the earliest broken link.
"""

from streamer.chat.consent import ChannelRecord, consent_failure


def check(**overrides) -> str | None:
    defaults = dict(
        platform="twitch",
        channel="teststreamer",
        allowlist=frozenset({"teststreamer"}),
        bot_is_moderator=True,
        record=ChannelRecord(armed=True, armed_by_user_id="42", room_id="42"),
        live_session_keys=frozenset({("twitch", "teststreamer")}),
    )
    defaults.update(overrides)
    return consent_failure(**defaults)  # type: ignore[arg-type]


class TestAllProofsHold:
    def test_returns_none(self) -> None:
        assert check() is None


class TestProofOne_OperatorAllowlist:
    def test_unlisted_channel_fails(self) -> None:
        assert check(allowlist=frozenset({"someoneelse"})) == "channel_not_allowlisted"

    def test_empty_allowlist_fails_everything(self) -> None:
        assert check(allowlist=frozenset()) == "channel_not_allowlisted"

    def test_allowlist_matching_is_normalized(self) -> None:
        """#-prefixes, case, and stray whitespace in the env var must not
        break the match — an allowlist that silently never matches is an
        allowlist the operator will disable."""
        assert check(allowlist=frozenset({" #TestStreamer "})) is None


class TestProofTwo_ModeratorGrant:
    def test_unmodded_bot_fails(self) -> None:
        """The proof only the broadcaster can create: without mod status the
        operator is pointing the bot at a channel they do not control."""
        assert check(bot_is_moderator=False) == "not_moderator"


class TestProofThree_RecordedConsent:
    def test_missing_record_fails_closed(self) -> None:
        assert check(record=None) == "not_armed"

    def test_disarmed_record_fails(self) -> None:
        record = ChannelRecord(armed=False, armed_by_user_id="42", room_id="42")
        assert check(record=record) == "not_armed"


class TestProofFour_ChannelBinding:
    def test_no_live_session_fails(self) -> None:
        """The stream ended (or never started): the bot must fall silent —
        there is nothing being fact-checked to speak about."""
        assert check(live_session_keys=frozenset()) == "channel_unbound"

    def test_session_for_a_different_channel_fails(self) -> None:
        """The weaponization case: ingesting person A's stream must never
        authorize posting into person B's chat."""
        assert (
            check(live_session_keys=frozenset({("twitch", "somebodyelse")}))
            == "channel_unbound"
        )

    def test_session_on_a_different_platform_fails(self) -> None:
        assert (
            check(live_session_keys=frozenset({("kick", "teststreamer")}))
            == "channel_unbound"
        )

    def test_binding_is_case_insensitive(self) -> None:
        assert (
            check(
                channel="TestStreamer",
                live_session_keys=frozenset({("twitch", "teststreamer")}),
            )
            is None
        )

    def test_empty_channel_name_fails(self) -> None:
        assert check(channel="") == "channel_unbound"


class TestOrdering:
    def test_earliest_broken_proof_wins(self) -> None:
        """With several proofs broken, surface the first — it is the one a
        human must fix first."""
        assert (
            check(
                allowlist=frozenset({"someoneelse"}),
                bot_is_moderator=False,
                record=None,
                live_session_keys=frozenset(),
            )
            == "channel_not_allowlisted"
        )

    def test_mod_beats_armed(self) -> None:
        assert check(bot_is_moderator=False, record=None) == "not_moderator"
