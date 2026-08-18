"""Live-session registry: every ``/ws/audio`` session, keyed by session id.

Replaces the module-global ``ws.current_pipeline`` that report §6.2 flagged:

    Single-session preemption blocks the documented MVP. [...] it makes the
    business analysis's §7 multi-channel bot MVP structurally impossible.

Preemption is SCOPED, and the scope is a setting:

- ``"global"`` (**default**) — a new connection preempts any existing session:
  today's behaviour, unchanged.
- ``"channel"`` — preempts only sessions on the same ``(platform, channel)``,
  so two channels coexist in one process. Clients that send no channel
  identity share the ``(None, None)`` bucket.
- ``"none"`` — never preempt (load tests, deliberate multi-tab).

**Why the default is still global.** Only the global scope can preempt on
*connect*. The channel scope cannot know the channel until the hello parses,
so it necessarily leaves the incumbent alive for up to ``HELLO_TIMEOUT_S`` —
which breaks the §3.2 promptness rule the extension's reconnect depends on
(and is exactly what ``TestPreemption::test_new_connection_preempts_old``
pins down). The scope exists so that property is a deliberate, documented
trade rather than an accident.

**On capacity.** Lifting the single-session limit is a structural fix, not a
throughput one: the STT executor is deliberately ``max_workers=1``
(``main.py``), so concurrent sessions serialize on one Whisper worker and a
single 4 s-window session already consumes a large fraction of real time on
CPU. The deployment unit for several channels is one backend PROCESS per
channel — which is also what the business analysis's "one VPS, ≤5 channels"
describes (five processes, not one process with five channels). Do not raise
``MAX_SESSIONS`` expecting free concurrency.

**What this module is actually for**, then, is not multi-channel throughput:
it replaces the module-global ``ws.current_pipeline`` that report §6.2
flagged, removing the ``getattr(ws, "current_pipeline")`` duck-typing in
``debug.py`` and the ``from app import ws`` import in ``setup.py``, and making
live-session state per-app (hence per-test, with no autouse reset fixture).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, see transcriber.py
    from app.pipeline import SessionPipeline

logger = logging.getLogger(__name__)

PreemptScope = Literal["global", "channel", "none"]

# (platform, channel) — the preemption key, the chat-bot binding key, and the
# ``channel_config`` primary key. Always built with :func:`channel_key`.
ChannelKey = tuple[str | None, str | None]


class SessionLimitExceeded(RuntimeError):
    """Raised by :meth:`SessionRegistry.register` at capacity."""

    def __init__(self, limit: int) -> None:
        super().__init__(
            f"backend is already running {limit} capture session(s); "
            "stop one before starting another"
        )
        self.limit = limit


def channel_key(platform: str | None, channel: str | None) -> ChannelKey:
    """Normalize ``(platform, channel)`` into the canonical key.

    Channel names are case-insensitive on every supported platform (and are
    lowercase on the wire for Twitch IRC), so the fold happens HERE, once, and
    every downstream consumer — registry, event envelope, chat bot, config
    rows — keys off the result. Empty strings collapse to ``None`` so a client
    sending ``channel=""`` lands in the same bucket as one that omits it.
    """
    return (platform, (channel or "").strip().lower() or None)


class SessionRegistry:
    """Every live :class:`~app.pipeline.SessionPipeline`, by session id.

    Lives on ``app.state.sessions`` rather than as a module global: every
    consumer already holds an ``app`` handle (``websocket.app.state``,
    ``request.app.state``), which removes the ``getattr(ws, "current_pipeline")``
    duck-typing in ``debug.py`` and makes registry state per-app — and
    therefore per-test, with no autouse reset fixture.
    """

    def __init__(self, scope: PreemptScope = "global", max_sessions: int = 4) -> None:
        self._sessions: dict[str, SessionPipeline] = {}
        self._scope = scope
        self._max = max_sessions
        # Same role as ws.py's old _registration_lock: keeps preempt-then-
        # register atomic, so a connection suspended inside preempt() (which
        # awaits a send and a close on the victim's socket) cannot be raced
        # past by a third registrant — which would otherwise end with two
        # live pipelines sharing one slot.
        self._lock = asyncio.Lock()

    @property
    def scope(self) -> PreemptScope:
        return self._scope

    async def preempt_on_accept(self) -> None:
        """GLOBAL scope only: kill the incumbent before this hello arrives.

        This is the §3.2 latency rule — the old session dies immediately
        instead of waiting up to 5 s for a hello that may never come. Channel
        scope CANNOT do it: the channel is only known once the hello parses,
        so under that scope the preempt happens entirely inside
        :meth:`register`.
        """
        if self._scope != "global":
            return
        for victim in list(self._sessions.values()):
            await victim.preempt()

    async def register(self, pipeline: SessionPipeline) -> None:
        """Preempt whatever this pipeline supersedes, then take a slot.

        Raises:
            SessionLimitExceeded: when ``max_sessions`` are already live. The
                cap is real rather than defensive — see the module docstring
                on the single STT worker.
        """
        async with self._lock:
            for victim in self._victims(pipeline):
                await victim.preempt()
            # Re-check AFTER preempting: a victim's ``finally`` unregisters it
            # concurrently, so the freed slot may or may not be visible yet.
            # Counting only the sessions that survived preemption keeps a
            # same-channel reconnect from spuriously hitting the cap.
            live = [
                session for session in self._sessions.values() if not session.preempted
            ]
            if len(live) >= self._max:
                raise SessionLimitExceeded(self._max)
            self._sessions[pipeline.session_id] = pipeline
            logger.info(
                "session registered: %s (%d live, scope=%s)",
                pipeline.session_id,
                len(self._sessions),
                self._scope,
            )

    def unregister(self, pipeline: SessionPipeline) -> None:
        """Identity-checked removal — the old ``current_pipeline is pipeline``.

        The identity check matters: a preempted pipeline's ``finally`` can run
        after its replacement has already claimed the id-keyed slot.
        """
        if self._sessions.get(pipeline.session_id) is pipeline:
            del self._sessions[pipeline.session_id]

    def get(self, session_id: str) -> SessionPipeline | None:
        return self._sessions.get(session_id)

    def all(self) -> list[SessionPipeline]:
        """A SNAPSHOT of the live sessions.

        Callers await inside their loop over the result (``preempt`` sends and
        closes a socket), and each preempt eventually mutates ``_sessions``
        from the victim's ``finally`` — so iterating the dict directly would
        risk "changed size during iteration".
        """
        return list(self._sessions.values())

    def for_channel(self, key: ChannelKey) -> list[SessionPipeline]:
        """Live sessions bound to one ``(platform, channel)``."""
        return [
            session for session in self._sessions.values() if session.channel_key == key
        ]

    def __len__(self) -> int:
        return len(self._sessions)

    def _victims(self, pipeline: SessionPipeline) -> list[SessionPipeline]:
        """Which live sessions this one supersedes, per the configured scope."""
        if self._scope == "none":
            return []
        if self._scope == "global":
            return [
                session
                for session in self._sessions.values()
                if session is not pipeline
            ]
        key = pipeline.channel_key
        return [
            session
            for session in self._sessions.values()
            if session is not pipeline and session.channel_key == key
        ]
