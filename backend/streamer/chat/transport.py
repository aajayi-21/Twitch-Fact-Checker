"""Twitch chat transport: raw IRC over WebSocket, behind a swappable seam.

Why raw IRC and not twitchio/EventSub: the bot needs "post a line, read
``!fc`` commands" on ONE channel. TwitchIO 3.x moved to EventSub and brings
its own OAuth callback server, token-store file, and subscription management
for that. Twitch still supports IRC, and the entire protocol surface we use
fits in this file. The :class:`ChatTransport` protocol is the seam that keeps
an EventSub/Helix implementation a drop-in swap later — which is also why
:meth:`ChatTransport.send` returns an id rather than a bool: Helix supports
message deletion by id, and changing the seam's signature later would touch
every policy call site.

Security rules this module owns (each carries a test):

- **The ``PASS`` line is never logged un-redacted.** It is the OAuth token.
  All wire logging goes through :func:`redact_irc_line`.
- **Tag unescaping never reintroduces CR/LF.** IRCv3 defines ``\\r``/``\\n``
  escapes; we deliberately unescape them to a space, because a tag value is
  headed for logs, the database, and comparison logic — none of which may
  ever see a line break that came off the wire. (Documented deviation from
  the spec; nothing legitimate in Twitch tags needs a literal newline.)
- **Outbound text containing CR/LF is refused, not repaired** — a second line
  would be a second IRC command.
- **If the tags capability is refused, the transport refuses to run.** Tags
  carry ``user-id``/``badges``/``mod``; without them no command authorization
  is possible, and an unauthorizable bot must not be in the room.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol
from uuid import uuid4

import websockets

from streamer.chat.limits import TwitchWriteLimiter

logger = logging.getLogger(__name__)

TWITCH_IRC_URL = "wss://irc-ws.chat.twitch.tv:443"

# Both are required: tags for authorization metadata, commands for
# RECONNECT/NOTICE/USERSTATE.
REQUIRED_CAPS = ("twitch.tv/tags", "twitch.tv/commands")

# How long the handshake may take before we call the connection dead.
HANDSHAKE_TIMEOUT_S = 15.0

# The most a send may be delayed by the account budget before it is dropped
# instead: a chat line held longer than this is about the wrong moment.
MAX_SEND_DELAY_S = 3.0

_CHANNEL_RE = re.compile(r"^[a-z0-9_]{1,25}$")


class ChatTransportError(Exception):
    """Base for transport failures."""


class AuthFailed(ChatTransportError):
    """Twitch rejected the token (``NOTICE * :Login authentication failed``).

    Distinct from a network failure because the reaction differs: a network
    drop reconnects with backoff; an auth failure needs a token refresh (or a
    human) first — reconnecting with the same dead token just loops.
    """


class CapabilityNegotiationFailed(ChatTransportError):
    """Twitch NAKed a required capability. No tags => no authorization =>
    the bot must not run."""


class ReconnectRequested(ChatTransportError):
    """Twitch sent ``RECONNECT``: close and reconnect promptly (server
    maintenance; the connection will be dropped shortly regardless)."""


# --------------------------------------------------------------------------- #
# Wire parsing (pure)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class IrcLine:
    """One parsed IRC line: ``[@tags] [:prefix] COMMAND [params] [:trailing]``."""

    tags: dict[str, str]
    prefix: str | None
    command: str
    params: tuple[str, ...]

    @property
    def trailing(self) -> str:
        return self.params[-1] if self.params else ""


# IRCv3 tag-value escapes. CR/LF deliberately map to a space — see the module
# docstring; a wire-supplied line break must never survive into tag values.
_TAG_ESCAPES = {":": ";", "s": " ", "\\": "\\", "r": " ", "n": " "}


def _unescape_tag_value(value: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            out.append(_TAG_ESCAPES.get(value[index + 1], value[index + 1]))
            index += 2
        elif char == "\\":
            index += 1  # trailing lone backslash: dropped per spec
        else:
            out.append(char)
            index += 1
    return "".join(out)


def parse_tags(raw: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    for part in raw.split(";"):
        if not part:
            continue
        key, _, value = part.partition("=")
        tags[key] = _unescape_tag_value(value)
    return tags


def parse_irc_line(line: str) -> IrcLine | None:
    """Parse one IRC line; ``None`` for anything malformed (never raises)."""
    line = line.strip("\r\n")
    if not line:
        return None
    tags: dict[str, str] = {}
    rest = line
    if rest.startswith("@"):
        raw_tags, _, rest = rest[1:].partition(" ")
        tags = parse_tags(raw_tags)
    prefix: str | None = None
    if rest.startswith(":"):
        prefix, _, rest = rest[1:].partition(" ")
    if not rest.strip():
        return None
    command, _, param_str = rest.partition(" ")
    if not command:
        return None
    params: list[str] = []
    while param_str:
        if param_str.startswith(":"):
            params.append(param_str[1:])
            break
        param, _, param_str = param_str.partition(" ")
        if param:
            params.append(param)
    return IrcLine(
        tags=tags, prefix=prefix, command=command.upper(), params=tuple(params)
    )


def redact_irc_line(line: str) -> str:
    """Wire-log form of an outbound line; the PASS token becomes a hint.

    The handshake's first line is ``PASS oauth:<token>`` — a naive "log every
    line sent" logs the credential in plaintext on line one.
    """
    if line.upper().startswith("PASS "):
        secret = line[5:].strip()
        return f"PASS oauth:…{secret[-4:]}" if len(secret) > 8 else "PASS oauth:…"
    return line


def login_from_prefix(prefix: str | None) -> str:
    """``nick!user@host`` -> ``nick`` (lowercased); empty when absent."""
    if not prefix:
        return ""
    return prefix.split("!", 1)[0].lower()


# --------------------------------------------------------------------------- #
# Transport events
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One inbound PRIVMSG, transport-neutral.

    Authorization consumers must key on ``user_id``/``room_id`` (numeric,
    Twitch-assigned), never on ``login`` or ``display_name`` — display names
    accept homoglyphs and logins can be re-registered after a rename.
    """

    channel: str  # no '#', lowercased
    login: str
    display_name: str
    user_id: str
    room_id: str
    text: str
    is_broadcaster: bool
    is_moderator: bool  # includes the broadcaster
    received_at: float = field(default_factory=time.monotonic)


@dataclass(frozen=True, slots=True)
class NoticeEvent:
    """A server NOTICE, usually a send rejection; ``msg_id`` keys the
    reaction table in :mod:`streamer.chat.limits`."""

    msg_id: str | None
    text: str


@dataclass(frozen=True, slots=True)
class ModStateEvent:
    """Our OWN moderator state in the room, from USERSTATE.

    This is the consent signal: only the broadcaster (or someone they trusted
    with mod powers) can make it True, and Twitch asserts it in a tag we
    cannot forge.
    """

    is_moderator: bool


ChatEvent = ChatMessage | NoticeEvent | ModStateEvent


def _classify_privmsg(parsed: IrcLine, expected_channel: str) -> ChatMessage | None:
    channel = parsed.params[0].lstrip("#").lower() if parsed.params else ""
    if channel != expected_channel:
        return None
    tags = parsed.tags
    badges = tags.get("badges", "")
    user_id = tags.get("user-id", "")
    room_id = tags.get("room-id", "")
    # Broadcaster: id equality is primary (room-id IS the broadcaster's user
    # id); the badge merely corroborates. Fail closed on missing ids.
    is_broadcaster = bool(user_id) and user_id == room_id
    is_moderator = is_broadcaster or tags.get("mod") == "1" or "moderator/" in badges
    return ChatMessage(
        channel=channel,
        login=login_from_prefix(parsed.prefix),
        display_name=tags.get("display-name", ""),
        user_id=user_id,
        room_id=room_id,
        text=parsed.trailing,
        is_broadcaster=is_broadcaster,
        is_moderator=is_moderator,
    )


# --------------------------------------------------------------------------- #
# The seam
# --------------------------------------------------------------------------- #


class ChatTransport(Protocol):
    """What the bot needs from a chat platform — deliberately tiny.

    Everything Twitch-IRC-specific (tags, PING/PONG, the account budget)
    lives behind this, so an EventSub/Helix implementation swaps in without
    touching policy code.
    """

    channel: str

    @property
    def connected(self) -> bool: ...

    @property
    def is_moderator(self) -> bool: ...

    @property
    def room_id(self) -> str | None: ...

    async def connect(self) -> None: ...

    async def send(self, text: str) -> str | None: ...

    def events(self) -> AsyncIterator[ChatEvent]: ...

    async def close(self) -> None: ...


# --------------------------------------------------------------------------- #
# The Twitch IRC implementation
# --------------------------------------------------------------------------- #


class TwitchIRCTransport:
    """One connection, one channel. ``connect()`` either completes the full
    handshake (caps → auth → join) or raises; reconnection policy belongs to
    the caller."""

    def __init__(
        self,
        *,
        login: str,
        token: str,
        channel: str,
        url: str = TWITCH_IRC_URL,
        limiter: TwitchWriteLimiter | None = None,
    ) -> None:
        channel = channel.strip().lstrip("#").lower()
        if not _CHANNEL_RE.match(channel):
            raise ValueError(f"invalid Twitch channel name: {channel!r}")
        self.channel = channel
        self._login = login.strip().lower()
        # Accept both bare tokens and "oauth:"-prefixed ones; the wire form
        # always carries the prefix exactly once.
        self._token = token.removeprefix("oauth:")
        self._url = url
        self._limiter = limiter or TwitchWriteLimiter()
        self._ws: websockets.ClientConnection | None = None
        self._is_moderator = False
        self._room_id: str | None = None
        # Lines that arrived during the handshake but belong to the caller
        # (e.g. a USERSTATE right after JOIN) are replayed by events().
        self._handshake_leftovers: list[IrcLine] = []

    # -- state ----------------------------------------------------------- #

    @property
    def connected(self) -> bool:
        return self._ws is not None

    @property
    def is_moderator(self) -> bool:
        return self._is_moderator

    @property
    def room_id(self) -> str | None:
        return self._room_id

    # -- lifecycle ------------------------------------------------------- #

    async def connect(self) -> None:
        """CAP → PASS/NICK → 001 → JOIN, or raise. Never partially connected."""
        ws = await websockets.connect(self._url, max_size=2**20)
        try:
            await asyncio.wait_for(self._handshake(ws), timeout=HANDSHAKE_TIMEOUT_S)
        except BaseException:
            await ws.close()
            raise
        self._ws = ws
        logger.info(
            "chat connected: %s in #%s (mod=%s)",
            self._login,
            self.channel,
            self._is_moderator,
        )

    async def _handshake(self, ws: websockets.ClientConnection) -> None:
        await self._raw(ws, f"CAP REQ :{' '.join(REQUIRED_CAPS)}")
        await self._raw(ws, f"PASS oauth:{self._token}")
        await self._raw(ws, f"NICK {self._login}")

        capabilities_acked = False
        welcomed = False
        joined = False
        while not (capabilities_acked and welcomed and joined):
            for parsed in await self._read_lines(ws):
                if parsed.command == "CAP":
                    # "CAP * ACK :caps" / "CAP * NAK :caps"
                    verb = parsed.params[1] if len(parsed.params) > 1 else ""
                    if verb == "NAK":
                        raise CapabilityNegotiationFailed(
                            f"Twitch refused required capabilities: {parsed.trailing}"
                        )
                    if verb == "ACK":
                        acked = parsed.trailing.split()
                        missing = [c for c in REQUIRED_CAPS if c not in acked]
                        if missing:
                            raise CapabilityNegotiationFailed(
                                f"capabilities not acknowledged: {missing}"
                            )
                        capabilities_acked = True
                elif parsed.command == "NOTICE":
                    self._raise_on_auth_notice(parsed)
                elif parsed.command == "001":
                    welcomed = True
                    await self._raw(ws, f"JOIN #{self.channel}")
                elif parsed.command == "JOIN":
                    if login_from_prefix(parsed.prefix) == self._login:
                        joined = True
                elif parsed.command == "PING":
                    await self._raw(ws, f"PONG :{parsed.trailing}")
                elif parsed.command in ("USERSTATE", "ROOMSTATE"):
                    self._absorb_state(parsed)
                    self._handshake_leftovers.append(parsed)
                elif parsed.command == "RECONNECT":
                    raise ReconnectRequested()

    @staticmethod
    def _raise_on_auth_notice(parsed: IrcLine) -> None:
        text = parsed.trailing.lower()
        if "authentication failed" in text or "improperly formatted auth" in text:
            raise AuthFailed(parsed.trailing)

    async def close(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception as exc:  # closing a dead socket is fine
                logger.debug("chat close failed (already dead?): %s", exc)

    # -- inbound --------------------------------------------------------- #

    async def events(self) -> AsyncIterator[ChatEvent]:
        """Yield chat events until the connection ends.

        Raises :class:`ReconnectRequested` on a server-initiated RECONNECT
        and :class:`AuthFailed` on an authentication NOTICE; ends normally on
        a clean close. PING is answered inline — the consumer (the bot's read
        task) drains continuously, so keepalive never starves.
        """
        ws = self._require_ws()
        for leftover in self._drain_leftovers():
            event = self._to_event(leftover)
            if event is not None:
                yield event
        async for payload in ws:
            text = (
                payload.decode(errors="replace")
                if isinstance(payload, bytes)
                else payload
            )
            # One WebSocket frame may carry SEVERAL \r\n-delimited IRC lines —
            # missing that is the classic Twitch-IRC parsing bug.
            for raw_line in text.split("\r\n"):
                parsed = parse_irc_line(raw_line)
                if parsed is None:
                    continue
                if parsed.command == "PING":
                    await self._raw(ws, f"PONG :{parsed.trailing}")
                    continue
                if parsed.command == "RECONNECT":
                    raise ReconnectRequested()
                if parsed.command == "NOTICE":
                    self._raise_on_auth_notice(parsed)
                event = self._to_event(parsed)
                if event is not None:
                    yield event

    def _drain_leftovers(self) -> list[IrcLine]:
        leftovers, self._handshake_leftovers = self._handshake_leftovers, []
        return leftovers

    def _to_event(self, parsed: IrcLine) -> ChatEvent | None:
        if parsed.command == "PRIVMSG":
            message = _classify_privmsg(parsed, self.channel)
            if message is not None and message.room_id and self._room_id is None:
                self._room_id = message.room_id
            return message
        if parsed.command == "NOTICE":
            return NoticeEvent(msg_id=parsed.tags.get("msg-id"), text=parsed.trailing)
        if parsed.command in ("USERSTATE", "ROOMSTATE"):
            before = self._is_moderator
            self._absorb_state(parsed)
            if parsed.command == "USERSTATE" and self._is_moderator != before:
                return ModStateEvent(is_moderator=self._is_moderator)
            if parsed.command == "USERSTATE":
                return ModStateEvent(is_moderator=self._is_moderator)
        return None

    def _absorb_state(self, parsed: IrcLine) -> None:
        if parsed.command == "USERSTATE":
            badges = parsed.tags.get("badges", "")
            self._is_moderator = (
                parsed.tags.get("mod") == "1"
                or "moderator/" in badges
                or "broadcaster/" in badges
            )
        room_id = parsed.tags.get("room-id")
        if room_id:
            self._room_id = room_id

    # -- outbound -------------------------------------------------------- #

    async def send(self, text: str) -> str | None:
        """PRIVMSG the channel; returns a local id, or ``None`` if dropped.

        Drops (never queues) when disconnected, when the text is unsafe, or
        when the account budget would delay it past ``MAX_SEND_DELAY_S`` — a
        held-back chat line is about the wrong moment by the time it lands.
        """
        ws = self._ws
        if ws is None:
            logger.warning("chat send dropped (disconnected): %r", text)
            return None
        if "\r" in text or "\n" in text:
            # Refused, not repaired: a second line is a second IRC command,
            # and text reaching here should already be sanitized — this state
            # is a bug upstream, exactly when sending is most dangerous.
            logger.error("chat send REFUSED (embedded newline): %r", text)
            return None
        wait_s = self._limiter.wait_time(self.channel)
        if wait_s > MAX_SEND_DELAY_S:
            logger.warning(
                "chat send dropped (account budget, %.1fs wait): %r", wait_s, text
            )
            return None
        if wait_s > 0:
            await asyncio.sleep(wait_s)
        self._limiter.record_send(self.channel)
        await self._raw(ws, f"PRIVMSG #{self.channel} :{text}")
        return uuid4().hex

    # -- plumbing -------------------------------------------------------- #

    def _require_ws(self) -> websockets.ClientConnection:
        if self._ws is None:
            raise ChatTransportError("transport is not connected")
        return self._ws

    async def _read_lines(self, ws: websockets.ClientConnection) -> list[IrcLine]:
        payload = await ws.recv()
        text = (
            payload.decode(errors="replace") if isinstance(payload, bytes) else payload
        )
        parsed_lines = []
        for raw_line in text.split("\r\n"):
            parsed = parse_irc_line(raw_line)
            if parsed is not None:
                parsed_lines.append(parsed)
        return parsed_lines

    @staticmethod
    async def _raw(ws: websockets.ClientConnection, line: str) -> None:
        logger.debug("chat >> %s", redact_irc_line(line))
        await ws.send(line + "\r\n")
