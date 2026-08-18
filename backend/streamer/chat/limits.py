"""Posting rate limits and the Twitch-rejection latch.

Three primitives plus one lookup table, all pure and all taking an injected
``now`` — a 60-minute sliding window cannot be tested with real sleeps, and
the suite's offline doctrine forbids them anyway.

Why not reuse :class:`app.rate_limit.TokenBucket` for the product caps: a
bucket with ``rate_per_min=0.1, burst=6`` gives six instant posts and then one
every ten minutes, which is not what a streamer means by "6 posts an hour" —
they mean a rolling window. And a FIXED hourly window would allow 12 posts
inside two minutes straddling a boundary. Hence :class:`SlidingWindowCap`.

The layering (who owns which limit):

- **Transport** (``transport.py``): the 1.2 s per-channel gap and the 18/30 s
  account budget — protects the *bot account* from Twitch's enforcement
  (exceeding the real cap = silently ignored for an hour).
- **Policy** (``policy.py``): the product caps — posts/hour, the 10-minute
  guard, per-claim and per-topic cooldowns. Protects the *channel* from spam.
- **Latch** (here): Twitch told us to stop, so we stop, regardless of what
  either of the above thinks.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

Clock = Callable[[], float]

DEFAULT_LATCH_REASON = "Twitch rejected a recent message."


class SlidingWindowCap:
    """At most ``limit`` events in any trailing ``window_s`` seconds.

    A deque of timestamps, evicted on read. O(limit) memory, and the boundary
    behaviour is honest: 6 posts at t=0 still count against the window at
    t=3599 and stop counting at t=3601.
    """

    def __init__(self, limit: int, window_s: float, *, now: Clock | None = None):
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")
        if window_s <= 0:
            raise ValueError(f"window_s must be > 0, got {window_s}")
        self._limit = limit
        self._window_s = window_s
        self._now = now or time.monotonic
        self._events: deque[float] = deque()

    def _evict(self) -> None:
        cutoff = self._now() - self._window_s
        while self._events and self._events[0] <= cutoff:
            self._events.popleft()

    def allows(self) -> bool:
        self._evict()
        return len(self._events) < self._limit

    def record(self) -> None:
        """Count one event. Deliberately separate from :meth:`allows` so a
        decision can be evaluated (and reported) without consuming a slot."""
        self._evict()
        self._events.append(self._now())

    def count(self) -> int:
        self._evict()
        return len(self._events)

    @property
    def limit(self) -> int:
        return self._limit

    def seconds_until_slot(self) -> float:
        """0.0 when a slot is free; else the wait until the oldest event ages
        out. Drives the control panel's "next post in 0:42" readout."""
        self._evict()
        if len(self._events) < self._limit:
            return 0.0
        return max(0.0, self._events[0] + self._window_s - self._now())


class PostingLatch:
    """Latched stop tripped by Twitch-side rejections (NOTICE msg-ids).

    The same shape as :class:`app.rate_limit.QuotaCooldown` — ``trip`` extends
    but never shortens — plus an injected clock. While active, nothing posts,
    no matter what policy or transport budgets say.
    """

    def __init__(self, *, now: Clock | None = None) -> None:
        self._now = now or time.monotonic
        self._until = 0.0
        self._reason: str | None = None

    def trip(self, seconds: float, reason: str | None = None) -> None:
        deadline = self._now() + max(seconds, 0.0)
        self._until = max(self._until, deadline)
        if reason:
            self._reason = reason

    @property
    def active(self) -> bool:
        return self._now() < self._until

    @property
    def remaining_s(self) -> float:
        return max(0.0, self._until - self._now())

    @property
    def reason(self) -> str:
        return self._reason or DEFAULT_LATCH_REASON


class TwitchWriteLimiter:
    """The transport-layer account budget: per-channel gap + global window.

    Twitch's real limits are 1 msg/s/channel and 20 msgs/30 s for a
    non-privileged account; we run at 1.2 s and 18/30 s so a clock skew or an
    off-by-one can never cross the real line — crossing it means the account
    is silently ignored for an hour, which would kill the bot mid-stream with
    no error. This limiter exists purely as that bulkhead; the *product* pace
    (6/hour etc.) lives in policy and is far below these numbers.
    """

    def __init__(
        self,
        *,
        per_channel_gap_s: float = 1.2,
        global_limit: int = 18,
        global_window_s: float = 30.0,
        now: Clock | None = None,
    ) -> None:
        self._gap = per_channel_gap_s
        self._now = now or time.monotonic
        self._last_send: dict[str, float] = {}
        self._window = SlidingWindowCap(global_limit, global_window_s, now=self._now)

    def wait_time(self, channel: str) -> float:
        """Seconds until a send to ``channel`` is allowed (0.0 = now)."""
        waits = [self._window.seconds_until_slot()]
        last = self._last_send.get(channel)
        if last is not None:
            waits.append(max(0.0, last + self._gap - self._now()))
        return max(waits)

    def record_send(self, channel: str) -> None:
        self._last_send[channel] = self._now()
        self._window.record()


# --------------------------------------------------------------------------- #
# Twitch NOTICE msg-id -> what to do about it
# --------------------------------------------------------------------------- #

NoticeKind = Literal["latch", "disable", "drop", "ignore"]


@dataclass(frozen=True, slots=True)
class NoticeAction:
    """How the bot must react to one rejection class.

    ``alert`` marks the ones a human needs to hear about promptly (control
    panel banner + loud log), not just see in a feed later.
    """

    kind: NoticeKind
    latch_s: float = 0.0
    alert: bool = False
    note: str = ""


# Sources: Twitch IRC msg-id catalogue. Anything not listed maps to a plain
# logged drop via action_for_notice()'s default — unknown rejections must
# never crash the read loop.
NOTICE_ACTIONS: dict[str, NoticeAction] = {
    # Twitch is rate-limiting the account. Posting more digs the hole deeper
    # (the enforcement is a 1-hour shadow-ignore), so stop for the full hour.
    "msg_ratelimit": NoticeAction(
        "latch", latch_s=3600.0, alert=True, note="Twitch rate limit tripped"
    ),
    # Identical message within the duplicate window. NEVER mutate the text to
    # defeat this — a posted verdict must be byte-identical to what the policy
    # approved. Dropping is correct; the verdict already appeared once.
    "msg_duplicate": NoticeAction("drop", note="duplicate message"),
    "msg_slowmode": NoticeAction("latch", latch_s=35.0, note="slow mode"),
    "msg_r9k": NoticeAction("drop", note="unique-chat mode"),
    # Account/channel configuration problems: latch long enough that the
    # operator notices, and tell them loudly — these do not fix themselves.
    "msg_followersonly": NoticeAction(
        "latch", latch_s=900.0, alert=True, note="followers-only chat"
    ),
    "msg_subsonly": NoticeAction(
        "latch", latch_s=900.0, alert=True, note="subscribers-only chat"
    ),
    "msg_verified_email": NoticeAction(
        "latch", latch_s=900.0, alert=True, note="bot account needs a verified email"
    ),
    "msg_requires_verified_phone_number": NoticeAction(
        "latch", latch_s=900.0, alert=True, note="bot account needs a verified phone"
    ),
    # Terminal: the channel does not want us. A banned bot that keeps trying
    # is an evasion pattern — hard-disable and require a human decision.
    "msg_banned": NoticeAction("disable", alert=True, note="bot is banned"),
    "msg_channel_suspended": NoticeAction(
        "disable", alert=True, note="channel suspended"
    ),
    "msg_timedout": NoticeAction("disable", alert=True, note="bot is timed out"),
    "msg_channel_blocked": NoticeAction(
        "disable", alert=True, note="channel blocked the bot"
    ),
}


def action_for_notice(msg_id: str | None) -> NoticeAction:
    """The reaction for one NOTICE ``msg-id``; unknown ids are a logged drop."""
    if not msg_id:
        return NoticeAction("ignore")
    return NOTICE_ACTIONS.get(msg_id, NoticeAction("drop", note=f"notice {msg_id}"))
