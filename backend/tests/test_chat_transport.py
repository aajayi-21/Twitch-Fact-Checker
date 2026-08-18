"""Tests for the Twitch IRC transport, against a local fake server.

The security-critical assertions: PASS never appears un-redacted in logs, tag
unescaping never reintroduces a newline, a NAKed tags capability refuses to
run, and outbound text with embedded CR/LF is refused rather than repaired.
"""

import asyncio
import logging

import pytest

from streamer.chat.limits import TwitchWriteLimiter
from streamer.chat.transport import (
    AuthFailed,
    CapabilityNegotiationFailed,
    ChatMessage,
    ModStateEvent,
    NoticeEvent,
    ReconnectRequested,
    TwitchIRCTransport,
    login_from_prefix,
    parse_irc_line,
    parse_tags,
    redact_irc_line,
)
from tests.conftest import FakeClock
from tests.fake_irc import FakeTwitchIRC, privmsg_line


def make_transport(server: FakeTwitchIRC, **overrides) -> TwitchIRCTransport:
    defaults = dict(
        login="factbot",
        token="oauth:secrettokenvalue1234",
        channel="teststreamer",
        url=server.url,
    )
    defaults.update(overrides)
    return TwitchIRCTransport(**defaults)


async def collect_events(transport, count, timeout=5.0):
    events = []

    async def pump():
        async for event in transport.events():
            events.append(event)
            if len(events) >= count:
                return

    await asyncio.wait_for(pump(), timeout=timeout)
    return events


# --------------------------------------------------------------------------- #
# Pure parsing
# --------------------------------------------------------------------------- #


class TestParseIrcLine:
    def test_full_tagged_privmsg(self) -> None:
        parsed = parse_irc_line(privmsg_line("hello world"))
        assert parsed is not None
        assert parsed.command == "PRIVMSG"
        assert parsed.params[0] == "#teststreamer"
        assert parsed.trailing == "hello world"
        assert parsed.tags["user-id"] == "1001"

    def test_ping(self) -> None:
        parsed = parse_irc_line("PING :tmi.twitch.tv")
        assert parsed is not None
        assert (parsed.command, parsed.trailing) == ("PING", "tmi.twitch.tv")

    def test_notice_with_msg_id(self) -> None:
        parsed = parse_irc_line(
            "@msg-id=msg_ratelimit :tmi.twitch.tv NOTICE #chan :Your message was not sent."
        )
        assert parsed is not None
        assert parsed.tags["msg-id"] == "msg_ratelimit"

    @pytest.mark.parametrize("junk", ["", "   ", ":", "@", "@tags-only"])
    def test_malformed_lines_return_none_never_raise(self, junk: str) -> None:
        assert parse_irc_line(junk) is None

    def test_trailing_colon_content_keeps_spaces_and_colons(self) -> None:
        parsed = parse_irc_line(":srv NOTICE * :a: b : c")
        assert parsed is not None
        assert parsed.trailing == "a: b : c"


class TestParseTags:
    def test_standard_escapes(self) -> None:
        tags = parse_tags(r"display-name=A\sB\:C\\D")
        assert tags["display-name"] == "A B;C\\D"

    def test_cr_lf_escapes_become_spaces_never_newlines(self) -> None:
        r"""IRCv3 defines \r/\n escapes as real CR/LF; we deliberately map
        them to spaces — a tag value flows into logs, the db, and comparisons,
        none of which may ever see a wire-supplied line break."""
        tags = parse_tags(r"note=line1\nline2\rline3")
        assert "\n" not in tags["note"] and "\r" not in tags["note"]
        assert tags["note"] == "line1 line2 line3"

    def test_trailing_lone_backslash_is_dropped(self) -> None:
        assert parse_tags("k=value\\")["k"] == "value"

    def test_empty_value_and_missing_equals(self) -> None:
        tags = parse_tags("subscriber=;turbo")
        assert tags["subscriber"] == ""
        assert tags["turbo"] == ""


class TestRedaction:
    def test_pass_line_shows_only_a_hint(self) -> None:
        redacted = redact_irc_line("PASS oauth:supersecrettoken9876")
        assert "supersecrettoken" not in redacted
        assert redacted.endswith("…9876")

    def test_short_pass_shows_nothing(self) -> None:
        assert redact_irc_line("PASS oauth:x") == "PASS oauth:…"

    def test_other_lines_pass_through(self) -> None:
        line = "PRIVMSG #chan :hello"
        assert redact_irc_line(line) == line


class TestLoginFromPrefix:
    def test_extracts_and_lowercases_nick(self) -> None:
        assert login_from_prefix("Bot!bot@bot.tmi.twitch.tv") == "bot"

    def test_absent_prefix_is_empty(self) -> None:
        assert login_from_prefix(None) == ""


class TestConstruction:
    @pytest.mark.parametrize(
        "bad", ["", "UPPER CASE", "way-too-long" * 5, "a b", "x;y"]
    )
    def test_invalid_channel_names_are_rejected_before_any_io(self, bad: str) -> None:
        with pytest.raises(ValueError, match="channel"):
            TwitchIRCTransport(login="b", token="t", channel=bad)

    def test_oauth_prefix_is_accepted_and_normalized(self) -> None:
        with_prefix = TwitchIRCTransport(login="b", token="oauth:abc", channel="chan")
        without = TwitchIRCTransport(login="b", token="abc", channel="chan")
        assert with_prefix._token == without._token == "abc"


# --------------------------------------------------------------------------- #
# Against the fake server
# --------------------------------------------------------------------------- #


class TestHandshake:
    async def test_connects_and_orders_the_handshake(self) -> None:
        async with FakeTwitchIRC() as server:
            transport = make_transport(server)
            await transport.connect()
            try:
                assert transport.connected is True
                assert transport.is_moderator is True  # fake grants mod
                assert transport.room_id == "42"
                commands = [line.split(" ")[0] for line in server.received]
                # CAP must precede PASS (caps decide whether to run at all),
                # PASS must precede NICK, JOIN comes only after welcome.
                assert commands.index("CAP") < commands.index("PASS")
                assert commands.index("PASS") < commands.index("NICK")
                assert commands.index("NICK") < commands.index("JOIN")
            finally:
                await transport.close()

    async def test_naked_capabilities_refuse_to_run(self) -> None:
        """No tags => no user-ids => no command authorization => the bot must
        not be in the room at all."""
        async with FakeTwitchIRC(nak_caps=True) as server:
            transport = make_transport(server)
            with pytest.raises(CapabilityNegotiationFailed):
                await transport.connect()
            assert transport.connected is False

    async def test_auth_failure_is_its_own_exception(self) -> None:
        """AuthFailed must be distinguishable from a network drop: the
        reaction is a token refresh, not a reconnect loop with a dead token."""
        async with FakeTwitchIRC(reject_auth=True) as server:
            transport = make_transport(server)
            with pytest.raises(AuthFailed):
                await transport.connect()

    async def test_pass_is_redacted_in_debug_logs(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The handshake's first line carries the OAuth token; a naive
        log-every-line would print the credential on line one."""
        async with FakeTwitchIRC() as server:
            transport = make_transport(server, token="oauth:supersecrettoken9876")
            with caplog.at_level(logging.DEBUG, logger="streamer.chat.transport"):
                await transport.connect()
                await transport.close()
        assert "supersecrettoken" not in caplog.text
        assert "…9876" in caplog.text

    async def test_non_mod_userstate_is_reflected(self) -> None:
        async with FakeTwitchIRC(bot_is_mod=False) as server:
            transport = make_transport(server)
            await transport.connect()
            try:
                assert transport.is_moderator is False
            finally:
                await transport.close()


class TestInbound:
    async def test_privmsg_yields_a_chat_message(self) -> None:
        async with FakeTwitchIRC() as server:
            transport = make_transport(server)
            await transport.connect()
            try:
                await server.push(privmsg_line("!fc status"))
                events = await collect_events(transport, 2)
                message = next(e for e in events if isinstance(e, ChatMessage))
                assert message.text == "!fc status"
                assert message.login == "someviewer"
                assert message.user_id == "1001"
                assert message.is_moderator is False
            finally:
                await transport.close()

    async def test_one_frame_with_many_lines_is_split(self) -> None:
        """Twitch batches several \\r\\n-delimited IRC lines into one
        WebSocket frame — missing that is THE classic parsing bug."""
        async with FakeTwitchIRC() as server:
            transport = make_transport(server)
            await transport.connect()
            try:
                await server.push(
                    "PING :keepalive-abc",
                    privmsg_line("first"),
                    privmsg_line("second"),
                )
                events = await collect_events(transport, 3)
                texts = [e.text for e in events if isinstance(e, ChatMessage)]
                assert texts == ["first", "second"]
                # And the PING inside the same frame was answered exactly.
                pong = await server.wait_for_line("PONG")
                assert pong == "PONG :keepalive-abc"
            finally:
                await transport.close()

    async def test_broadcaster_is_identified_by_id_equality(self) -> None:
        """user-id == room-id is the primary signal; badges corroborate. A
        homoglyph display-name with a different id must confer nothing."""
        async with FakeTwitchIRC() as server:
            transport = make_transport(server)
            await transport.connect()
            try:
                await server.push(
                    privmsg_line(
                        "!fc enable",
                        login="teststreamer",
                        display_name="TestStreamer",
                        user_id="42",
                        room_id="42",
                        badges="broadcaster/1",
                    ),
                    privmsg_line(
                        "!fc enable",
                        login="evil",
                        display_name="ТestStreamer",  # Cyrillic Т homoglyph
                        user_id="666",
                        room_id="42",
                    ),
                )
                events = await collect_events(transport, 3)
                messages = [e for e in events if isinstance(e, ChatMessage)]
                real, fake = messages
                assert real.is_broadcaster is True and real.is_moderator is True
                assert fake.is_broadcaster is False and fake.is_moderator is False
            finally:
                await transport.close()

    async def test_notice_yields_its_msg_id(self) -> None:
        async with FakeTwitchIRC() as server:
            transport = make_transport(server)
            await transport.connect()
            try:
                await server.push(
                    "@msg-id=msg_duplicate :tmi.twitch.tv NOTICE #teststreamer "
                    ":Your message was not sent because it is identical."
                )
                events = await collect_events(transport, 2)
                notice = next(e for e in events if isinstance(e, NoticeEvent))
                assert notice.msg_id == "msg_duplicate"
            finally:
                await transport.close()

    async def test_userstate_demod_is_surfaced(self) -> None:
        """A mid-stream demod revokes consent; the bot must see it happen."""
        async with FakeTwitchIRC() as server:
            transport = make_transport(server)
            await transport.connect()
            try:
                await server.push(
                    "@badges=;mod=0 :tmi.twitch.tv USERSTATE #teststreamer"
                )
                events = await collect_events(transport, 2)
                assert any(
                    isinstance(e, ModStateEvent) and e.is_moderator is False
                    for e in events
                )
                assert transport.is_moderator is False
            finally:
                await transport.close()

    async def test_reconnect_raises_out_of_the_event_stream(self) -> None:
        async with FakeTwitchIRC() as server:
            transport = make_transport(server)
            await transport.connect()
            try:
                await server.push(":tmi.twitch.tv RECONNECT")
                with pytest.raises(ReconnectRequested):
                    # High count: the handshake's leftover USERSTATE yields
                    # first, so the pump must keep going until the RECONNECT
                    # line raises out of the stream.
                    await collect_events(transport, 99)
            finally:
                await transport.close()

    async def test_privmsg_for_another_channel_is_ignored(self) -> None:
        async with FakeTwitchIRC() as server:
            transport = make_transport(server)
            await transport.connect()
            try:
                await server.push(
                    privmsg_line("wrong room", channel="otherchannel"),
                    privmsg_line("right room"),
                )
                events = await collect_events(transport, 2)
                texts = [e.text for e in events if isinstance(e, ChatMessage)]
                assert texts == ["right room"]
            finally:
                await transport.close()


class TestOutbound:
    async def test_send_writes_a_privmsg_and_returns_an_id(self) -> None:
        async with FakeTwitchIRC() as server:
            transport = make_transport(server)
            await transport.connect()
            try:
                message_id = await transport.send("🤖 hello chat")
                assert message_id is not None
                line = await server.wait_for_line("PRIVMSG #teststreamer")
                assert line == "PRIVMSG #teststreamer :🤖 hello chat"
            finally:
                await transport.close()

    async def test_embedded_newline_is_refused_not_repaired(self) -> None:
        """A second line is a second IRC command; text reaching the transport
        should already be sanitized, so this state is an upstream bug — the
        exact moment sending is most dangerous."""
        async with FakeTwitchIRC() as server:
            transport = make_transport(server)
            await transport.connect()
            try:
                assert await transport.send("🤖 hi\r\n/ban everyone") is None
                await asyncio.sleep(0.05)
                assert not any("ban" in line for line in server.received)
            finally:
                await transport.close()

    async def test_disconnected_send_drops(self) -> None:
        async with FakeTwitchIRC() as server:
            transport = make_transport(server)
            assert await transport.send("🤖 too early") is None

    async def test_exhausted_account_budget_drops_instead_of_waiting(self) -> None:
        """A held-back chat line is about the wrong moment by the time it
        lands, so past the small delay ceiling the send is dropped."""
        clock = FakeClock()
        limiter = TwitchWriteLimiter(now=clock)
        for _ in range(18):  # exhaust the 18/30 s global window
            limiter.record_send("elsewhere")
        async with FakeTwitchIRC() as server:
            transport = make_transport(server, limiter=limiter)
            await transport.connect()
            try:
                assert await transport.send("🤖 over budget") is None
                await asyncio.sleep(0.05)
                assert not any("over budget" in line for line in server.received)
            finally:
                await transport.close()
