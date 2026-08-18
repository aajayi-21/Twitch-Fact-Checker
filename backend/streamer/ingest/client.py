"""The protocol client: hello, PCM frames out, server frames in, reconnect.

A Python port of the extension's ``BackendSocket`` (0.5→15 s backoff), with
one deliberate difference: it never gives up on its own — the CLI's user
wants it to survive a router reboot at 3 a.m., and every retry prints.

:func:`classify_error` is pure, which is what makes the whole reconnect
policy testable without a socket. The one decision with teeth:
``superseded`` is TERMINAL. If a browser tab running the extension owns the
session, a CLI that blindly reconnects would preempt it, get preempted back,
and ping-pong every half second forever.
"""

from __future__ import annotations

import asyncio
import enum
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

import websockets

from streamer.ingest import FRAME_SECONDS, SAMPLE_RATE
from streamer.ingest.sources import Identity


class Action(enum.Enum):
    CONTINUE = "continue"  # non-fatal: print (rate-limited) and keep going
    RECONNECT = "reconnect"
    FATAL = "fatal"


@dataclass(frozen=True, slots=True)
class ErrorPolicy:
    action: Action
    exit_code: int = 0
    message: str = ""


_ERROR_POLICIES: dict[str, ErrorPolicy] = {
    "bad_hello": ErrorPolicy(
        Action.FATAL,
        2,
        "the backend rejected our handshake — this is a bug; please report it",
    ),
    "not_configured": ErrorPolicy(
        Action.FATAL,
        3,
        "the backend has no LLM key yet — open http://127.0.0.1:8711/control "
        "and finish setup",
    ),
    "too_many_sessions": ErrorPolicy(
        Action.FATAL,
        4,
        "the backend is at its session limit — stop the other capture first",
    ),
    "superseded": ErrorPolicy(
        Action.FATAL,
        4,
        "another capture client took over (a browser tab running the "
        "extension?). The backend runs one session at a time — stop the "
        "other one, then start this again.",
    ),
    "credentials_updated": ErrorPolicy(
        Action.RECONNECT, message="API credentials changed; reconnecting"
    ),
}

# Everything else non-fatal (stt_overload, rate_limited, quota_cooldown,
# llm_failure) continues, printed at most once per this many seconds per code.
_CONTINUE_LOG_INTERVAL_S = 30.0


def classify_error(frame: dict) -> ErrorPolicy:
    """Map a server error frame to what the CLI must do about it."""
    code = str(frame.get("code", ""))
    policy = _ERROR_POLICIES.get(code)
    if policy is not None:
        return policy
    if frame.get("fatal"):
        return ErrorPolicy(Action.FATAL, 2, frame.get("message", code) or code)
    return ErrorPolicy(Action.CONTINUE, message=frame.get("message", code) or code)


def build_hello(
    *,
    identity: Identity,
    stream_title: str | None = None,
    sensitivity: str = "medium",
    enabled_topics: list[str] | None = None,
    send_transcripts: bool = False,
) -> dict:
    """The §2.1 hello — WITH identity, which ``stream_wav.py`` never sent, so
    its sessions landed with NULL channel and were invisible to
    ``/stats/channels`` (and unusable for the chat bot's channel binding)."""
    hello: dict = {
        "type": "hello",
        "version": 1,
        "format": "pcm_s16le",
        "sample_rate": SAMPLE_RATE,
        "channels": 1,
        "sensitivity": sensitivity,
        "send_transcripts": send_transcripts,
        "platform": identity.platform,
        "channel": identity.channel,
    }
    if stream_title:
        hello["stream_title"] = stream_title
    if enabled_topics is not None:
        hello["enabled_topics"] = enabled_topics
    return hello


class IngestSocket:
    """One logical session over any number of physical connections."""

    def __init__(
        self,
        url: str,
        hello: dict,
        *,
        on_frame: Callable[[dict], None],
        fail: Callable[[int, str], None],
    ) -> None:
        self._url = url
        self._hello = hello  # held by reference: reconnect hellos stay current
        self._on_frame = on_frame
        self._fail = fail  # (exit_code, message) -> never returns
        self._ws: websockets.ClientConnection | None = None
        self._backoff_s = 0.5
        self._last_continue_log: dict[str, float] = {}
        self.dropped_frames = 0
        self.stopping = False

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def run_reader(self) -> None:
        """Connect (with backoff, forever) and read server frames."""
        while not self.stopping:
            try:
                websocket = await websockets.connect(self._url, max_size=None)
            except (OSError, websockets.WebSocketException) as exc:
                self._note(
                    f"backend unreachable ({exc}); retrying in {self._backoff_s:.1f}s"
                )
                await asyncio.sleep(self._backoff_s)
                self._backoff_s = min(self._backoff_s * 2, 15.0)
                continue
            try:
                await websocket.send(json.dumps(self._hello))
                self._ws = websocket
                async for message in websocket:
                    if isinstance(message, bytes):
                        continue
                    try:
                        frame = json.loads(message)
                    except json.JSONDecodeError:
                        continue
                    if not self._handle(frame):
                        return
                # Clean server close after a stop: done.
                if self.stopping:
                    return
                self._note("backend closed the connection; reconnecting")
            except websockets.ConnectionClosed:
                if self.stopping:
                    return
                self._note("connection lost; reconnecting")
            finally:
                self._ws = None
            await asyncio.sleep(self._backoff_s)
            self._backoff_s = min(self._backoff_s * 2, 15.0)

    def _handle(self, frame: dict) -> bool:
        """True = keep reading; False = reader is done (fatal path exits)."""
        if frame.get("type") == "ready":
            self._backoff_s = 0.5
            self._note("session ready")
        if frame.get("type") == "error":
            policy = classify_error(frame)
            if policy.action is Action.FATAL:
                self._fail(policy.exit_code, policy.message)
                return False
            if policy.action is Action.RECONNECT:
                self._note(policy.message)
                return True
            code = str(frame.get("code", ""))
            now = time.monotonic()
            last = self._last_continue_log.get(code, float("-inf"))
            if now - last >= _CONTINUE_LOG_INTERVAL_S:
                self._last_continue_log[code] = now
                self._note(f"⚠ {code}: {policy.message}")
        self._on_frame(frame)
        return True

    async def send_pcm(self, frame: bytes) -> None:
        """Best-effort: while disconnected, live audio DROPS (a stream cannot
        be paused; buffering an outage only makes stale fact-checks)."""
        websocket = self._ws
        if websocket is None:
            self.dropped_frames += 1
            return
        try:
            await websocket.send(frame)
        except websockets.ConnectionClosed:
            self.dropped_frames += 1

    async def send_json(self, payload: dict) -> None:
        """Best-effort control/frame message; silently dropped while down
        (same doctrine as PCM — a stale video frame helps nobody)."""
        websocket = self._ws
        if websocket is None:
            return
        try:
            await websocket.send(json.dumps(payload))
        except websockets.ConnectionClosed:
            pass

    async def send_stop(self) -> None:
        websocket = self._ws
        if websocket is not None:
            try:
                await websocket.send(json.dumps({"type": "stop"}))
            except websockets.ConnectionClosed:
                pass

    async def close(self) -> None:
        self.stopping = True
        websocket, self._ws = self._ws, None
        if websocket is not None:
            try:
                await websocket.close()
            except Exception:
                pass

    @property
    def dropped_seconds(self) -> float:
        return self.dropped_frames * FRAME_SECONDS

    @staticmethod
    def _note(message: str) -> None:
        print(f"note: {message}", file=sys.stderr, flush=True)
