"""Tests for the streamer posting limiters (streamer/chat/limits.py).

Everything runs on a FakeClock — a 60-minute sliding window cannot be tested
with real sleeps, and this suite never waits on wall time for window logic.
"""

import pytest

from streamer.chat.limits import (
    DEFAULT_LATCH_REASON,
    NOTICE_ACTIONS,
    PostingLatch,
    SlidingWindowCap,
    TwitchWriteLimiter,
    action_for_notice,
)
from tests.conftest import FakeClock


class TestSlidingWindowCap:
    def test_allows_up_to_the_limit(self) -> None:
        clock = FakeClock()
        cap = SlidingWindowCap(3, 60.0, now=clock)
        for _ in range(3):
            assert cap.allows() is True
            cap.record()
        assert cap.allows() is False

    def test_events_age_out_of_the_window(self) -> None:
        clock = FakeClock()
        cap = SlidingWindowCap(2, 60.0, now=clock)
        cap.record()
        cap.record()
        assert cap.allows() is False
        clock.advance(60.1)
        assert cap.allows() is True
        assert cap.count() == 0

    def test_window_slides_it_does_not_reset(self) -> None:
        """The boundary case a fixed hourly bucket gets wrong: 6 posts at the
        end of one bucket plus 6 at the start of the next is 12 posts in two
        minutes. A sliding window never allows that."""
        clock = FakeClock()
        cap = SlidingWindowCap(6, 3600.0, now=clock)
        for _ in range(6):
            cap.record()
        clock.advance(3599.0)
        assert cap.allows() is False  # fixed window would already have reset
        clock.advance(2.0)  # oldest event is now 3601 s old
        assert cap.allows() is True

    def test_partial_ageing_frees_slots_one_at_a_time(self) -> None:
        clock = FakeClock()
        cap = SlidingWindowCap(2, 100.0, now=clock)
        cap.record()
        clock.advance(50.0)
        cap.record()
        clock.advance(51.0)  # first event (t=0) aged out; second (t=50) not
        assert cap.count() == 1
        assert cap.allows() is True

    def test_seconds_until_slot(self) -> None:
        clock = FakeClock()
        cap = SlidingWindowCap(1, 60.0, now=clock)
        assert cap.seconds_until_slot() == 0.0
        cap.record()
        clock.advance(20.0)
        assert cap.seconds_until_slot() == pytest.approx(40.0)

    def test_allows_does_not_consume_a_slot(self) -> None:
        """Decisions get evaluated (and reported) without spending capacity."""
        clock = FakeClock()
        cap = SlidingWindowCap(1, 60.0, now=clock)
        for _ in range(5):
            assert cap.allows() is True
        assert cap.count() == 0

    def test_rejects_nonsense_construction(self) -> None:
        with pytest.raises(ValueError, match="limit"):
            SlidingWindowCap(0, 60.0)
        with pytest.raises(ValueError, match="window_s"):
            SlidingWindowCap(1, 0.0)


class TestPostingLatch:
    def test_starts_inactive(self) -> None:
        latch = PostingLatch(now=FakeClock())
        assert latch.active is False
        assert latch.remaining_s == 0.0
        assert latch.reason == DEFAULT_LATCH_REASON

    def test_trips_and_expires(self) -> None:
        clock = FakeClock()
        latch = PostingLatch(now=clock)
        latch.trip(100.0, "slow mode")
        assert latch.active is True
        assert latch.remaining_s == pytest.approx(100.0)
        assert latch.reason == "slow mode"
        clock.advance(100.1)
        assert latch.active is False

    def test_extends_but_never_shortens(self) -> None:
        """Same doctrine as QuotaCooldown: a shorter second trip must not cut
        an existing hold short."""
        clock = FakeClock()
        latch = PostingLatch(now=clock)
        latch.trip(3600.0, "rate limit")
        latch.trip(30.0, "slow mode")
        assert latch.remaining_s == pytest.approx(3600.0)
        assert latch.reason == "slow mode"  # reason updates; deadline holds


class TestTwitchWriteLimiter:
    def test_per_channel_gap_is_enforced(self) -> None:
        clock = FakeClock()
        limiter = TwitchWriteLimiter(now=clock)
        assert limiter.wait_time("alice") == 0.0
        limiter.record_send("alice")
        assert limiter.wait_time("alice") == pytest.approx(1.2)
        clock.advance(1.2)
        assert limiter.wait_time("alice") == 0.0

    def test_channels_have_independent_gaps(self) -> None:
        clock = FakeClock()
        limiter = TwitchWriteLimiter(now=clock)
        limiter.record_send("alice")
        assert limiter.wait_time("bob") == 0.0

    def test_global_window_spans_channels(self) -> None:
        """The 18/30 s budget protects the ACCOUNT, so sends to different
        channels all count against it."""
        clock = FakeClock()
        limiter = TwitchWriteLimiter(global_limit=3, global_window_s=30.0, now=clock)
        for channel in ("a", "b", "c"):
            limiter.record_send(channel)
            clock.advance(2.0)
        wait = limiter.wait_time("d")
        assert wait > 0.0  # blocked by the global window, not any gap
        clock.advance(wait + 0.1)
        assert limiter.wait_time("d") == 0.0

    def test_runs_below_twitch_real_limits(self) -> None:
        """Defaults leave headroom under 1 msg/s and 20/30 s — crossing the
        real line gets the account silently ignored for an hour."""
        limiter = TwitchWriteLimiter()
        assert limiter._gap > 1.0
        assert limiter._window.limit < 20


class TestNoticeActions:
    def test_rate_limit_latches_a_full_hour_and_alerts(self) -> None:
        action = action_for_notice("msg_ratelimit")
        assert action.kind == "latch"
        assert action.latch_s == 3600.0
        assert action.alert is True

    def test_ban_class_notices_hard_disable(self) -> None:
        for msg_id in ("msg_banned", "msg_channel_suspended", "msg_timedout"):
            action = action_for_notice(msg_id)
            assert action.kind == "disable", msg_id
            assert action.alert is True, msg_id

    def test_duplicate_drops_without_mutation_or_latch(self) -> None:
        """Dropping is the whole reaction: the text must never be mutated to
        defeat unique-chat, because a posted verdict has to stay byte-identical
        to what the policy approved."""
        action = action_for_notice("msg_duplicate")
        assert action.kind == "drop"
        assert action.latch_s == 0.0

    def test_account_configuration_problems_alert(self) -> None:
        for msg_id in ("msg_verified_email", "msg_requires_verified_phone_number"):
            action = action_for_notice(msg_id)
            assert action.kind == "latch" and action.alert is True, msg_id

    def test_unknown_notice_is_a_logged_drop_not_a_crash(self) -> None:
        action = action_for_notice("msg_totally_new_thing")
        assert action.kind == "drop"

    def test_missing_msg_id_is_ignored(self) -> None:
        assert action_for_notice(None).kind == "ignore"
        assert action_for_notice("").kind == "ignore"

    def test_every_action_kind_is_handled(self) -> None:
        assert {action.kind for action in NOTICE_ACTIONS.values()} <= {
            "latch",
            "disable",
            "drop",
            "ignore",
        }
