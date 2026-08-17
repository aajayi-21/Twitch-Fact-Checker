"""Process-wide fan-out of session frames to non-WebSocket consumers.

``/ws/audio`` frames go to exactly one socket. The streamer product needs the
same frames in three more places — the Twitch chat bot, the OBS overlay page,
and the control panel — so this module is the seam that lets them subscribe
without touching :mod:`app.pipeline`'s socket path.

Two design rules, both load-bearing, both structural rather than conventional:

1. **:meth:`EventHub.publish` is synchronous, bounded, and runs no subscriber
   code.** Subscribers own a queue, not a callback. "A slow subscriber must
   never stall the pipeline" and "a throwing subscriber must not kill the
   session" are therefore true by construction, not by a ``try/except`` a
   future caller can forget to write.

2. **Drop-OLDEST on overflow**, matching the two claim queues in
   ``pipeline.py``. For a backed-up consumer the NEWEST verdict is the one the
   live conversation is actually about; a stale one posted late reads as being
   about the wrong moment.

With no subscribers, ``publish`` costs one ``dict`` truth test — the frame is
not even serialized. The viewer backend therefore pays nothing for a hub it
never uses.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Per-subscriber mailbox depth. Deep enough to ride out a page repaint or a
# reconnect, shallow enough that a wedged consumer surfaces as dropped events
# rather than unbounded memory.
DEFAULT_QUEUE_MAXSIZE = 64


@dataclass(frozen=True, slots=True)
class SessionEvent:
    """One outbound frame plus the identity the wire frame does not carry.

    ``VerdictFrame`` has no session, channel, or claim metadata — it was
    designed for a socket that already knows which session it belongs to.
    Every consumer here needs all three (the chat bot to decide *where* to
    post and whether the claim is fresh enough, the control panel to label
    rows), so the hub enriches at the publish site, where they are in scope.
    """

    session_id: str
    platform: str | None
    channel: str | None  # normalized via app.sessions.channel_key
    frame: dict[str, Any]  # already model_dump()ed, exactly once
    claim_id: str | None = None
    check_worthiness: float | None = None
    topic: str | None = None
    # Seconds since the claim left the gate. The chat policy's staleness cut
    # needs this; it cannot be derived from Verdict.checked_at, which is
    # stamped at verdict construction and so measures only the tail of the
    # latency, not the STT window + gate interval + queue wait before it.
    claim_age_s: float | None = None
    # Position in the stream, for locating a disputed post in the VOD.
    stream_time_s: float | None = None
    at: float = field(default_factory=time.monotonic)

    @property
    def type(self) -> str:
        return str(self.frame.get("type", ""))


class Subscription:
    """One consumer's bounded mailbox over the hub.

    Async-iterable, so a consumer is ``async for event in subscription:``.
    Closing it (or leaving its ``async with`` block) unsubscribes.
    """

    def __init__(
        self,
        hub: EventHub,
        *,
        name: str,
        channels: frozenset[str | None] | None,
        types: frozenset[str] | None,
        maxsize: int,
    ) -> None:
        self._hub = hub
        self.name = name
        self._channels = channels
        self._types = types
        self._queue: asyncio.Queue[SessionEvent] = asyncio.Queue(maxsize=maxsize)
        self._closed = asyncio.Event()
        # How many events this consumer was too slow to receive. Exposed so a
        # status endpoint can show a subscriber falling behind instead of
        # silently losing verdicts.
        self.dropped = 0

    def _accepts(self, event: SessionEvent) -> bool:
        if self._types is not None and event.type not in self._types:
            return False
        return self._channels is None or event.channel in self._channels

    def _offer(self, event: SessionEvent) -> None:
        """Called by :meth:`EventHub.publish`. Never awaits, never raises."""
        if not self._accepts(event):
            return
        if self._queue.full():
            # No await between the full() check and the put, so on a single
            # event loop this cannot interleave — the same race-freedom
            # argument the pipeline's claim queues rely on.
            self._queue.get_nowait()
            self.dropped += 1
            if self.dropped in (1, 10, 100) or self.dropped % 1000 == 0:
                logger.warning(
                    "event subscriber %r is falling behind: %d dropped",
                    self.name,
                    self.dropped,
                )
        self._queue.put_nowait(event)

    async def get(self) -> SessionEvent | None:
        """Next event, or ``None`` once the subscription is closed."""
        getter = asyncio.ensure_future(self._queue.get())
        closer = asyncio.ensure_future(self._closed.wait())
        try:
            done, _ = await asyncio.wait(
                {getter, closer}, return_when=asyncio.FIRST_COMPLETED
            )
            if getter in done:
                return getter.result()
            # Closed: drain anything already queued so a graceful shutdown
            # still delivers what it had, then report exhaustion.
            if not self._queue.empty():
                return self._queue.get_nowait()
            return None
        finally:
            for task in (getter, closer):
                if not task.done():
                    task.cancel()

    def __aiter__(self) -> AsyncIterator[SessionEvent]:
        return self

    async def __anext__(self) -> SessionEvent:
        event = await self.get()
        if event is None:
            raise StopAsyncIteration
        return event

    async def __aenter__(self) -> Subscription:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Idempotent: unsubscribe and wake any pending :meth:`get`."""
        self._closed.set()
        self._hub.unsubscribe(self)


class EventHub:
    """Fan-out of :class:`SessionEvent` to any number of subscribers."""

    def __init__(self, queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE) -> None:
        self._subscribers: dict[int, Subscription] = {}
        self._ids = itertools.count()
        self._maxsize = queue_maxsize

    def subscribe(
        self,
        *,
        name: str = "anonymous",
        channels: Iterable[str | None] | None = None,
        types: Iterable[str] | None = None,
    ) -> Subscription:
        """Open a mailbox. ``channels``/``types`` of ``None`` mean "everything".

        Filtering here rather than in the consumer keeps an uninterested
        subscriber's queue empty, so it can never displace events a busy
        subscriber still wants.
        """
        subscription = Subscription(
            self,
            name=name,
            channels=None if channels is None else frozenset(channels),
            types=None if types is None else frozenset(types),
            maxsize=self._maxsize,
        )
        subscription_id = next(self._ids)
        self._subscribers[subscription_id] = subscription
        subscription._id = subscription_id  # type: ignore[attr-defined]
        logger.debug("event subscriber %r attached", name)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        subscription_id = getattr(subscription, "_id", None)
        if subscription_id is not None:
            self._subscribers.pop(subscription_id, None)

    def publish(
        self,
        frame: BaseModel,
        *,
        session_id: str,
        platform: str | None = None,
        channel: str | None = None,
        claim_id: str | None = None,
        check_worthiness: float | None = None,
        topic: str | None = None,
        claim_age_s: float | None = None,
        stream_time_s: float | None = None,
    ) -> None:
        """Best-effort synchronous fan-out. Never blocks, never raises.

        The ``model_dump()`` is deliberately *inside* the empty check: with no
        subscribers (the viewer backend's normal state) publishing costs a
        single truth test and the frame is never serialized.
        """
        if not self._subscribers:
            return
        event = SessionEvent(
            session_id=session_id,
            platform=platform,
            channel=channel,
            frame=frame.model_dump(),
            claim_id=claim_id,
            check_worthiness=check_worthiness,
            topic=topic,
            claim_age_s=claim_age_s,
            stream_time_s=stream_time_s,
        )
        # Snapshot: a subscriber may close (mutating the dict) from another
        # task between offers.
        for subscription in list(self._subscribers.values()):
            try:
                subscription._offer(event)
            except Exception:  # pragma: no cover - _offer is total by design
                logger.exception(
                    "event subscriber %r raised on offer; dropping it",
                    subscription.name,
                )
                self.unsubscribe(subscription)

    def close(self) -> None:
        """Wake every subscriber so their pumps exit, then detach them all."""
        for subscription in list(self._subscribers.values()):
            subscription.close()
        self._subscribers.clear()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
