"""A local fake Twitch IRC server for offline transport tests.

Speaks just enough of the real handshake (CAP/PASS/NICK/001/JOIN/USERSTATE/
ROOMSTATE) that :class:`streamer.chat.transport.TwitchIRCTransport` connects
against it unmodified. Scriptable failure modes cover the paths that matter:
capability NAK, auth rejection, server-initiated RECONNECT.

Loopback-only — the suite stays fully offline.
"""

from __future__ import annotations

import asyncio

from websockets.asyncio.server import ServerConnection, serve


class FakeTwitchIRC:
    """One-server-per-test; ``url`` is ready after ``start()``."""

    def __init__(
        self,
        *,
        nak_caps: bool = False,
        reject_auth: bool = False,
        bot_is_mod: bool = True,
        room_id: str = "42",
    ) -> None:
        self.nak_caps = nak_caps
        self.reject_auth = reject_auth
        self.bot_is_mod = bot_is_mod
        self.room_id = room_id
        self.received: list[str] = []  # every raw line, in arrival order
        self.connections = 0
        self.url = ""
        self._server = None
        self._active: ServerConnection | None = None
        self._client_seen = asyncio.Event()

    async def __aenter__(self) -> FakeTwitchIRC:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    async def start(self) -> None:
        self._server = await serve(self._handler, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def push(self, *lines: str) -> None:
        """Send raw IRC line(s) to the connected client as ONE frame —
        which is exactly how Twitch batches, and the classic parsing trap."""
        await asyncio.wait_for(self._client_seen.wait(), timeout=5.0)
        assert self._active is not None
        await self._active.send("\r\n".join(lines) + "\r\n")

    async def wait_for_line(self, needle: str, timeout: float = 5.0) -> str:
        """Block until a received line contains ``needle``; return it."""

        async def _wait() -> str:
            while True:
                for line in self.received:
                    if needle in line:
                        return line
                await asyncio.sleep(0.01)

        return await asyncio.wait_for(_wait(), timeout=timeout)

    # ------------------------------------------------------------------ #

    async def _handler(self, connection: ServerConnection) -> None:
        self.connections += 1
        self._active = connection
        self._client_seen.set()
        nick = "bot"
        try:
            async for payload in connection:
                text = (
                    payload.decode(errors="replace")
                    if isinstance(payload, bytes)
                    else payload
                )
                for line in text.split("\r\n"):
                    if not line:
                        continue
                    self.received.append(line)
                    await self._react(connection, line, nick)
                    if line.startswith("NICK "):
                        nick = line.split(" ", 1)[1].strip().lower()
        except Exception:
            pass  # client went away mid-test; the test asserts elsewhere

    async def _react(self, connection: ServerConnection, line: str, nick: str) -> None:
        if line.startswith("CAP REQ "):
            caps = line.partition(":")[2]
            verb = "NAK" if self.nak_caps else "ACK"
            await connection.send(f":tmi.twitch.tv CAP * {verb} :{caps}\r\n")
        elif line.startswith("NICK "):
            nick = line.split(" ", 1)[1].strip().lower()
            if self.reject_auth:
                await connection.send(
                    ":tmi.twitch.tv NOTICE * :Login authentication failed\r\n"
                )
            else:
                await connection.send(f":tmi.twitch.tv 001 {nick} :Welcome, GLHF!\r\n")
        elif line.startswith("JOIN "):
            channel = line.split(" ", 1)[1].strip()
            mod_flag = "1" if self.bot_is_mod else "0"
            badges = "moderator/1" if self.bot_is_mod else ""
            await connection.send(
                f":{nick}!{nick}@{nick}.tmi.twitch.tv JOIN {channel}\r\n"
                f"@room-id={self.room_id} :tmi.twitch.tv ROOMSTATE {channel}\r\n"
                f"@badges={badges};mod={mod_flag} "
                f":tmi.twitch.tv USERSTATE {channel}\r\n"
            )


def privmsg_line(
    text: str,
    *,
    channel: str = "teststreamer",
    login: str = "someviewer",
    display_name: str = "SomeViewer",
    user_id: str = "1001",
    room_id: str = "42",
    badges: str = "",
    mod: str = "0",
) -> str:
    """A realistic tagged PRIVMSG line (same doctrine as the SDK-object
    builders in conftest: tests parse REAL wire shapes, not conveniences)."""
    return (
        f"@badge-info=;badges={badges};color=;display-name={display_name};"
        f"emotes=;id=abc-123;mod={mod};room-id={room_id};subscriber=0;"
        f"tmi-sent-ts=1700000000000;turbo=0;user-id={user_id};user-type= "
        f":{login}!{login}@{login}.tmi.twitch.tv PRIVMSG #{channel} :{text}"
    )
