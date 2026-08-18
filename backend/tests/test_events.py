"""Unit tests for the event-hub fan-out (app/events.py).

The two properties worth defending are structural, so they are tested
structurally: ``publish`` must never block or raise no matter what a
subscriber does, and a backed-up subscriber must lose its OLDEST events, not
its newest.
"""

import asyncio

import pytest
from pydantic import BaseModel

from app.events import EventHub, SessionEvent


class Frame(BaseModel):
    type: str = "verdict"
    label: str = "FALSE"


def make_hub(maxsize: int = 4) -> EventHub:
    return EventHub(queue_maxsize=maxsize)


async def drain(subscription, expected: int) -> list[SessionEvent]:
    return [await subscription.get() for _ in range(expected)]


class TestPublishIsFreeWithoutSubscribers:
    def test_no_subscribers_never_serializes_the_frame(self) -> None:
        """The model_dump() sits inside the empty check, so the viewer backend
        pays nothing for a hub it never uses."""
        hub = make_hub()

        class ExplodingFrame(BaseModel):
            def model_dump(self, *args, **kwargs):  # type: ignore[override]
                raise AssertionError("frame must not be serialized")

        hub.publish(ExplodingFrame(), session_id="s1")  # must not raise

    def test_subscriber_count_tracks_attachments(self) -> None:
        hub = make_hub()
        assert hub.subscriber_count == 0
        first = hub.subscribe(name="a")
        hub.subscribe(name="b")
        assert hub.subscriber_count == 2
        first.close()
        assert hub.subscriber_count == 1


class TestFanOut:
    async def test_every_subscriber_receives_the_event(self) -> None:
        hub = make_hub()
        a = hub.subscribe(name="a")
        b = hub.subscribe(name="b")

        hub.publish(Frame(), session_id="s1", platform="twitch", channel="alice")

        for subscription in (a, b):
            event = await subscription.get()
            assert event is not None
            assert event.session_id == "s1"
            assert event.channel == "alice"
            assert event.frame["label"] == "FALSE"

    async def test_identity_and_claim_metadata_ride_along(self) -> None:
        """VerdictFrame carries none of this, and the posting policy needs
        all of it."""
        hub = make_hub()
        subscription = hub.subscribe(name="bot")

        hub.publish(
            Frame(),
            session_id="s1",
            platform="twitch",
            channel="alice",
            claim_id="c1",
            check_worthiness=0.82,
            topic="politics",
            claim_age_s=12.5,
            stream_time_s=1234.5,
        )

        event = await subscription.get()
        assert event is not None
        assert (event.claim_id, event.topic) == ("c1", "politics")
        assert event.check_worthiness == pytest.approx(0.82)
        assert event.claim_age_s == pytest.approx(12.5)
        assert event.stream_time_s == pytest.approx(1234.5)

    async def test_publish_is_synchronous(self) -> None:
        """It is a plain def, so a caller cannot accidentally await it into a
        latency path."""
        hub = make_hub()
        subscription = hub.subscribe(name="a")
        hub.publish(Frame(), session_id="s1")
        # Already queued without any await having happened in between.
        assert subscription._queue.qsize() == 1


class TestFiltering:
    async def test_type_filter_excludes_other_frames(self) -> None:
        hub = make_hub()
        subscription = hub.subscribe(name="bot", types={"verdict"})

        hub.publish(Frame(type="transcript"), session_id="s1")
        hub.publish(Frame(type="verdict"), session_id="s1")

        event = await subscription.get()
        assert event is not None and event.type == "verdict"
        assert subscription._queue.empty()

    async def test_channel_filter_excludes_other_channels(self) -> None:
        hub = make_hub()
        subscription = hub.subscribe(name="bot", channels={"alice"})

        hub.publish(Frame(), session_id="s1", channel="bob")
        hub.publish(Frame(), session_id="s2", channel="alice")

        event = await subscription.get()
        assert event is not None and event.session_id == "s2"

    async def test_filtered_events_do_not_consume_queue_depth(self) -> None:
        """An uninterested subscriber's mailbox stays empty, so it can never
        displace events a busy subscriber still wants."""
        hub = make_hub(maxsize=2)
        subscription = hub.subscribe(name="bot", types={"verdict"})

        for _ in range(10):
            hub.publish(Frame(type="transcript"), session_id="s1")

        assert subscription._queue.empty()
        assert subscription.dropped == 0


class TestBackpressure:
    async def test_full_queue_drops_oldest_and_keeps_newest(self) -> None:
        """For a backed-up consumer the NEWEST verdict is the one the live
        conversation is about."""
        hub = make_hub(maxsize=2)
        subscription = hub.subscribe(name="slow")

        for index in range(4):
            hub.publish(Frame(label=str(index)), session_id="s1")

        events = await drain(subscription, 2)
        assert [event.frame["label"] for event in events] == ["2", "3"]
        assert subscription.dropped == 2

    async def test_slow_subscriber_never_blocks_publish(self) -> None:
        hub = make_hub(maxsize=1)
        hub.subscribe(name="wedged")  # never read from

        for _ in range(1000):
            hub.publish(Frame(), session_id="s1")  # must not block or raise

    async def test_one_slow_subscriber_does_not_starve_a_fast_one(self) -> None:
        hub = make_hub(maxsize=2)
        hub.subscribe(name="wedged")
        fast = hub.subscribe(name="fast")

        hub.publish(Frame(label="a"), session_id="s1")
        first = await fast.get()
        hub.publish(Frame(label="b"), session_id="s1")
        second = await fast.get()

        assert first is not None and second is not None
        assert [first.frame["label"], second.frame["label"]] == ["a", "b"]
        assert fast.dropped == 0


class TestIsolation:
    async def test_a_throwing_subscriber_is_detached_not_propagated(self) -> None:
        """A subscriber must never be able to kill the session that published."""
        hub = make_hub()
        healthy = hub.subscribe(name="healthy")
        broken = hub.subscribe(name="broken")

        def explode(event: SessionEvent) -> None:
            raise RuntimeError("subscriber is broken")

        broken._offer = explode  # type: ignore[method-assign]

        hub.publish(Frame(), session_id="s1")  # must not raise

        assert hub.subscriber_count == 1
        event = await healthy.get()
        assert event is not None

    async def test_closing_during_publish_does_not_break_the_loop(self) -> None:
        hub = make_hub()
        first = hub.subscribe(name="a")
        second = hub.subscribe(name="b")
        original = first._offer

        def close_then_offer(event: SessionEvent) -> None:
            second.close()
            original(event)

        first._offer = close_then_offer  # type: ignore[method-assign]

        hub.publish(Frame(), session_id="s1")  # must not raise

        assert await first.get() is not None


class TestSubscriptionLifecycle:
    async def test_get_returns_none_once_closed(self) -> None:
        hub = make_hub()
        subscription = hub.subscribe(name="a")
        subscription.close()

        assert await subscription.get() is None

    async def test_close_drains_what_was_already_queued(self) -> None:
        """A graceful shutdown still delivers events that had already
        arrived — they are paid-for verdicts."""
        hub = make_hub()
        subscription = hub.subscribe(name="a")
        hub.publish(Frame(), session_id="s1")
        subscription.close()

        assert await subscription.get() is not None
        assert await subscription.get() is None

    async def test_pending_get_wakes_on_close(self) -> None:
        hub = make_hub()
        subscription = hub.subscribe(name="a")
        pending = asyncio.ensure_future(subscription.get())
        await asyncio.sleep(0)

        subscription.close()

        assert await asyncio.wait_for(pending, timeout=1.0) is None

    async def test_async_iteration_stops_at_close(self) -> None:
        hub = make_hub()
        subscription = hub.subscribe(name="a")
        hub.publish(Frame(label="a"), session_id="s1")
        hub.publish(Frame(label="b"), session_id="s1")
        subscription.close()

        labels = [event.frame["label"] async for event in subscription]

        assert labels == ["a", "b"]

    async def test_context_manager_unsubscribes(self) -> None:
        hub = make_hub()
        async with hub.subscribe(name="a"):
            assert hub.subscriber_count == 1
        assert hub.subscriber_count == 0

    async def test_close_is_idempotent(self) -> None:
        hub = make_hub()
        subscription = hub.subscribe(name="a")
        subscription.close()
        subscription.close()  # must not raise

    async def test_hub_close_detaches_everything(self) -> None:
        hub = make_hub()
        first = hub.subscribe(name="a")
        second = hub.subscribe(name="b")

        hub.close()

        assert hub.subscriber_count == 0
        assert await first.get() is None
        assert await second.get() is None
