"""Tests for the channel bot orchestrator (streamer/chat/bot.py).

Driven through the bot's handler methods with a fake transport — no task
machinery, no sockets, no sleeps (engagement windows run at 0 s). The e2e
wiring through a real app comes with the streamer app phase.
"""

from pathlib import Path

import pytest

from app.events import EventHub, SessionEvent
from app.models import Source, Verdict, utc_now_iso
from app.sessions import SessionRegistry

from streamer.chat.bot import ChannelBot
from streamer.chat.format import verdict_handle
from streamer.chat.policy import PostingPolicy
from streamer.chat.transport import ChatMessage, NoticeEvent
from streamer.db import StreamerDatabase
from tests.conftest import FakeClock
from tests.test_sessions import FakePipeline

REUTERS = Source(url="https://www.reuters.com/world/a", title="Reuters")
APNEWS = Source(url="https://apnews.com/article/b", title="AP")

CHANNEL = "teststreamer"
BROADCASTER_ID = "42"


class FakeChatTransport:
    """In-memory ChatTransport: records sends, never touches a socket."""

    def __init__(self, *, channel: str = CHANNEL, is_moderator: bool = True) -> None:
        self.channel = channel
        self.is_moderator = is_moderator
        self.room_id = BROADCASTER_ID
        self.connected = True
        self.sent: list[str] = []
        self.drop_sends = False

    async def connect(self) -> None:
        self.connected = True

    async def send(self, text: str) -> str | None:
        if self.drop_sends:
            return None
        self.sent.append(text)
        return f"mid-{len(self.sent)}"

    async def close(self) -> None:
        self.connected = False

    def events(self):  # pragma: no cover - the loops aren't driven in tests
        raise NotImplementedError


def make_verdict(**overrides) -> Verdict:
    defaults = dict(
        claim="The Eiffel Tower is 450 metres tall.",
        label="FALSE",
        explanation="It is 330 m tall including antennas, not 450.",
        sources=[REUTERS, APNEWS],
        topic="other",
    )
    defaults.update(overrides)
    return Verdict(**defaults)  # type: ignore[arg-type]


def make_event(verdict: Verdict, **overrides) -> SessionEvent:
    from app.models import VerdictFrame

    defaults = dict(
        session_id="s1",
        platform="twitch",
        channel=CHANNEL,
        frame=VerdictFrame.from_verdict(verdict).model_dump(),
        claim_id="c1",
        check_worthiness=0.9,
        topic=verdict.topic,
        claim_age_s=30.0,
        stream_time_s=120.0,
    )
    defaults.update(overrides)
    return SessionEvent(**defaults)  # type: ignore[arg-type]


def chat_message(
    text: str,
    *,
    user_id: str = "1001",
    is_broadcaster: bool = False,
    is_moderator: bool = False,
) -> ChatMessage:
    return ChatMessage(
        channel=CHANNEL,
        login="someone",
        display_name="Someone",
        user_id=user_id,
        room_id=BROADCASTER_ID,
        text=text,
        is_broadcaster=is_broadcaster,
        is_moderator=is_moderator or is_broadcaster,
    )


BROADCASTER = dict(user_id=BROADCASTER_ID, is_broadcaster=True)
MOD = dict(user_id="777", is_moderator=True)


@pytest.fixture()
async def db(tmp_path: Path):
    database = StreamerDatabase(str(tmp_path / "bot-test.db"))
    await database.open()
    yield database
    await database.close()


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
async def bot(db, clock):
    """A fully-armed, live, trusted, NON-dry-run bot in auto mode — each test
    then breaks exactly the thing it is about."""
    registry = SessionRegistry(scope="none")
    await registry.register(FakePipeline("s1", "twitch", CHANNEL))
    transport = FakeChatTransport()
    instance = ChannelBot(
        platform="twitch",
        channel=CHANNEL,
        transport=transport,  # type: ignore[arg-type]
        db=db,
        hub=EventHub(),
        registry=registry,
        allowlist=frozenset({CHANNEL}),
        policy=PostingPolicy(mode="auto"),
        dry_run=False,
        engagement_window_s=0.0,
        now=clock,
    )
    instance.joined_at = clock() - 300.0  # past the join grace
    instance.trusted = True  # probation passed
    await db.upsert_chat_channel(
        platform="twitch",
        channel=CHANNEL,
        armed_at=utc_now_iso(),
        armed_by_user_id=BROADCASTER_ID,
        room_id=BROADCASTER_ID,
    )
    await instance.refresh_channel_record()
    return instance


class TestAutoPosting:
    async def test_a_good_verdict_posts_with_a_recorded_row(self, bot, db) -> None:
        verdict = make_verdict()
        await bot.handle_verdict_event(make_event(verdict))

        posts = [m for m in bot.transport.sent if "FALSE" in m and "!fc why" in m]
        assert len(posts) == 1
        assert verdict.claim in posts[0]
        rows = await db.fetch_chat_posts("twitch", CHANNEL, status="posted")
        assert len(rows) == 1
        assert rows[0]["reason"] == "ok"
        assert rows[0]["message_id"] == "mid-2"  # mid-1 was the disclosure

    async def test_first_post_is_preceded_by_the_disclosure(self, bot) -> None:
        await bot.handle_verdict_event(make_event(make_verdict()))
        assert "Fact-check bot" in bot.transport.sent[0]
        assert "FALSE" in bot.transport.sent[1]

    async def test_disclosure_is_not_repeated_every_post(self, bot, clock) -> None:
        for index in range(2):
            clock.advance(600.0)
            await bot.handle_verdict_event(
                make_event(
                    make_verdict(
                        claim=f"Distinct claim number {index} about a topic thing."
                    )
                )
            )
        disclosures = [m for m in bot.transport.sent if "Fact-check bot" in m]
        assert len(disclosures) == 1

    async def test_suppressed_verdict_sends_nothing_but_records_why(
        self, bot, db
    ) -> None:
        await bot.handle_verdict_event(make_event(make_verdict(label="UNVERIFIED")))
        assert bot.transport.sent == []
        rows = await db.fetch_chat_posts("twitch", CHANNEL, status="suppressed")
        assert [row["reason"] for row in rows] == ["label_not_postable"]

    async def test_send_drop_is_recorded_as_failed(self, bot, db) -> None:
        bot.transport.drop_sends = True
        await bot.handle_verdict_event(make_event(make_verdict()))
        rows = await db.fetch_chat_posts("twitch", CHANNEL, status="failed")
        assert [row["reason"] for row in rows] == ["send_dropped"]


class TestConsentGates:
    async def test_unarmed_channel_posts_nothing(self, bot, db) -> None:
        bot._cached_record = None  # no !fc enable ever happened
        await bot.handle_verdict_event(make_event(make_verdict()))
        assert bot.transport.sent == []
        rows = await db.fetch_chat_posts("twitch", CHANNEL)
        assert rows[0]["reason"] == "not_armed"

    async def test_unmodded_bot_posts_nothing(self, bot, db) -> None:
        bot.transport.is_moderator = False
        await bot.handle_verdict_event(make_event(make_verdict()))
        assert bot.transport.sent == []
        rows = await db.fetch_chat_posts("twitch", CHANNEL)
        assert rows[0]["reason"] == "not_moderator"

    async def test_dead_session_stops_posting(self, bot, db) -> None:
        for session in bot._registry.all():
            bot._registry.unregister(session)
        await bot.handle_verdict_event(make_event(make_verdict()))
        assert bot.transport.sent == []
        rows = await db.fetch_chat_posts("twitch", CHANNEL)
        assert rows[0]["reason"] == "channel_unbound"

    async def test_hard_disabled_channel_posts_nothing(self, bot, db) -> None:
        bot.handle_notice(NoticeEvent(msg_id="msg_banned", text="banned"))
        await bot.handle_verdict_event(make_event(make_verdict()))
        assert bot.transport.sent == []
        rows = await db.fetch_chat_posts("twitch", CHANNEL)
        assert rows[0]["reason"] == "channel_disabled"


class TestDryRun:
    async def test_dry_run_records_the_exact_message_but_sends_nothing(
        self, bot, db
    ) -> None:
        bot.dry_run = True
        await bot.handle_verdict_event(make_event(make_verdict()))
        assert bot.transport.sent == []
        rows = await db.fetch_chat_posts("twitch", CHANNEL, status="dry_run")
        assert len(rows) == 1
        assert rows[0]["message_text"].startswith("❌ FALSE")

    async def test_dry_run_also_silences_command_replies(self, bot) -> None:
        bot.dry_run = True
        await bot.handle_chat_event(chat_message("!fc status", **MOD))
        assert bot.transport.sent == []


class TestReviewMode:
    async def test_review_queues_instead_of_posting(self, bot, db) -> None:
        bot.policy = bot._policy_with(mode="review")
        await bot.handle_verdict_event(make_event(make_verdict()))
        assert bot.transport.sent == []
        assert len(bot.review_queue) == 1
        rows = await db.fetch_chat_posts("twitch", CHANNEL, status="queued")
        assert len(rows) == 1

    async def test_probation_forces_review_even_in_auto(self, bot) -> None:
        bot.trusted = False
        await bot.handle_verdict_event(make_event(make_verdict()))
        assert bot.transport.sent == []
        assert len(bot.review_queue) == 1

    async def test_approve_posts_the_queued_message(self, bot, db) -> None:
        bot.policy = bot._policy_with(mode="review")
        await bot.handle_verdict_event(make_event(make_verdict()))
        post_id = next(iter(bot.review_queue))

        approved = await bot.approve(post_id, approved_by=BROADCASTER_ID)

        assert approved is True
        assert any("FALSE" in m for m in bot.transport.sent)
        rows = await db.fetch_chat_posts("twitch", CHANNEL, status="posted")
        assert rows[0]["approved_by_user_id"] == BROADCASTER_ID
        assert bot.review_queue == {}

    async def test_expired_item_cannot_be_approved(self, bot, db, clock) -> None:
        bot.policy = bot._policy_with(mode="review")
        await bot.handle_verdict_event(make_event(make_verdict()))
        post_id = next(iter(bot.review_queue))
        clock.advance(300.0)  # past the 180 s TTL

        approved = await bot.approve(post_id, approved_by=BROADCASTER_ID)

        assert approved is False
        assert bot.transport.sent == []
        rows = await db.fetch_chat_posts("twitch", CHANNEL, status="expired")
        assert len(rows) == 1

    async def test_skip_records_without_posting(self, bot, db) -> None:
        bot.policy = bot._policy_with(mode="review")
        await bot.handle_verdict_event(make_event(make_verdict()))
        post_id = next(iter(bot.review_queue))

        assert await bot.skip(post_id, skipped_by="777") is True
        assert bot.transport.sent == []
        rows = await db.fetch_chat_posts("twitch", CHANNEL, status="skipped")
        assert len(rows) == 1

    async def test_probation_graduates_after_enough_approvals(
        self, bot, db, clock
    ) -> None:
        bot.trusted = False
        bot.approved_posts = 9  # one short of graduation
        bot.policy = bot._policy_with(mode="review")
        await bot.handle_verdict_event(make_event(make_verdict()))
        post_id = next(iter(bot.review_queue))

        await bot.approve(post_id, approved_by=BROADCASTER_ID)

        assert bot.trusted is True
        row = await db.fetch_chat_channel("twitch", CHANNEL)
        assert row["trusted"] == 1


class TestCommands:
    async def test_viewer_mod_verbs_are_silently_ignored(self, bot, db) -> None:
        await bot.handle_chat_event(chat_message("!fc off"))
        assert bot.transport.sent == []
        assert bot.policy.mode == "auto"  # unchanged

        def _read():
            conn = db._require_conn()
            return [dict(r) for r in conn.execute("SELECT * FROM chat_commands")]

        rows = await db._run(_read)
        assert rows[0]["accepted"] == 0  # but the attempt was recorded

    async def test_mod_can_turn_posting_off_and_it_persists(self, bot, db) -> None:
        await bot.handle_chat_event(chat_message("!fc off", **MOD))
        assert bot.policy.mode == "off"
        row = await db.fetch_chat_channel("twitch", CHANNEL)
        assert row["mode"] == "off"
        assert any("OFF" in m for m in bot.transport.sent)

    async def test_mute_is_persisted_as_an_absolute_deadline(
        self, bot, db, clock
    ) -> None:
        await bot.handle_chat_event(chat_message("!fc mute 30m", **MOD))
        assert bot.muted_until == pytest.approx(clock() + 1800.0)
        row = await db.fetch_chat_channel("twitch", CHANNEL)
        assert row["muted_until"]  # absolute UTC, not a duration

    async def test_mod_attempting_broadcaster_verb_gets_one_correction(
        self, bot
    ) -> None:
        await bot.handle_chat_event(chat_message("!fc enable", **MOD))
        assert bot.transport.sent == [
            "🤖 !fc enable / disable / correct / trust are broadcaster-only."
        ]

    async def test_enable_by_broadcaster_arms_the_channel(self, bot, db) -> None:
        bot._cached_record = None
        await bot.handle_chat_event(chat_message("!fc enable", **BROADCASTER))
        record = await db.fetch_channel_record("twitch", CHANNEL)
        assert record is not None and record.armed is True
        assert record.armed_by_user_id == BROADCASTER_ID
        assert bot._cached_record is not None and bot._cached_record.armed

    async def test_dispute_note_is_never_echoed(self, bot, db) -> None:
        """The §-echo-injection rule: no user text in any outbound message."""
        verdict = make_verdict()
        await bot.handle_verdict_event(make_event(verdict))
        handle = verdict_handle(verdict)

        await bot.handle_chat_event(
            chat_message(f"!fc dispute {handle} /ban everyone please")
        )

        replies = bot.transport.sent[2:]  # after disclosure + post
        assert len(replies) == 1
        assert "/ban" not in replies[0] and "everyone" not in replies[0]
        assert handle in replies[0]

    async def test_dispute_with_invalid_handle_gets_no_reply_at_all(self, bot) -> None:
        await bot.handle_chat_event(chat_message("!fc dispute NOTHEX"))
        assert bot.transport.sent == []

    async def test_three_distinct_disputers_auto_mute_never_retract(
        self, bot, db, clock
    ) -> None:
        verdict = make_verdict()
        await bot.handle_verdict_event(make_event(verdict))
        handle = verdict_handle(verdict)
        for user_id in ("u1", "u2", "u3"):
            clock.advance(10.0)
            await bot.handle_chat_event(
                chat_message(f"!fc dispute {handle}", user_id=user_id)
            )

        assert bot.muted_until is not None and bot.muted_until > clock()
        # Never a retraction message — silence is safe, retraction is a
        # statement (and auto-retract is a brigade button).
        assert not any("RETRACTED" in m for m in bot.transport.sent)

    async def test_retract_by_mod_posts_retraction_and_feeds_the_eval_set(
        self, bot, db
    ) -> None:
        verdict = make_verdict()
        # Persist the session/claim/verdict chain the pipeline writes in the
        # real app (feedback rows are FK-checked against verdicts).
        from app.models import GateClaim

        claim = GateClaim(claim_text=verdict.claim, check_worthiness=0.9, topic="other")
        await db.record_session_start(
            session_id="s1", platform="twitch", channel=CHANNEL, title=None
        )
        await db.record_claim(claim=claim, session_id="s1", outcome="verified")
        await db.record_verdict(
            verdict=verdict,
            claim_id=claim.id,
            session_id="s1",
            latency_ms=1,
            provider="test",
            model="test",
        )
        await bot.handle_verdict_event(make_event(verdict))
        handle = verdict_handle(verdict)

        await bot.handle_chat_event(chat_message(f"!fc retract {handle}", **MOD))

        assert any(m.startswith("↩️ RETRACTED") for m in bot.transport.sent)
        row = await db.fetch_chat_post_by_handle("twitch", CHANNEL, handle)
        assert row["retracted_at"] is not None

        def _read():
            conn = db._require_conn()
            return [dict(r) for r in conn.execute("SELECT * FROM feedback")]

        feedback = await db._run(_read)
        assert [f["rating"] for f in feedback] == ["down"]

    async def test_cap_above_the_clamp_is_silently_ignored(self, bot) -> None:
        await bot.handle_chat_event(chat_message("!fc cap 50", **MOD))
        assert bot.policy.posts_per_hour == 6  # unchanged; clamp held

    async def test_command_replies_are_flood_controlled_per_user(
        self, bot, clock
    ) -> None:
        await bot.handle_chat_event(chat_message("!fc status", **MOD))
        await bot.handle_chat_event(chat_message("!fc status", **MOD))
        assert len(bot.transport.sent) == 1  # second within 3 s: silence
        clock.advance(5.0)
        await bot.handle_chat_event(chat_message("!fc status", **MOD))
        assert len(bot.transport.sent) == 2


class TestNotices:
    async def test_rate_limit_notice_latches_posting(self, bot, db) -> None:
        bot.handle_notice(NoticeEvent(msg_id="msg_ratelimit", text="stop"))
        await bot.handle_verdict_event(make_event(make_verdict()))
        assert bot.transport.sent == []
        rows = await db.fetch_chat_posts("twitch", CHANNEL)
        assert rows[0]["reason"] == "posting_latched"

    async def test_unknown_notice_never_crashes(self, bot) -> None:
        bot.handle_notice(NoticeEvent(msg_id="msg_new_thing", text="?"))
        bot.handle_notice(NoticeEvent(msg_id=None, text="?"))


class TestEngagement:
    async def test_engagement_rows_carry_counts_only(self, bot, db) -> None:
        import asyncio

        for _ in range(4):
            await bot.handle_chat_event(chat_message("hello there"))
        await bot.handle_verdict_event(make_event(make_verdict()))
        await asyncio.sleep(0.01)  # let the 0 s engagement task run

        def _read():
            conn = db._require_conn()
            return [dict(r) for r in conn.execute("SELECT * FROM chat_engagement")]

        rows = await db._run(_read)
        assert len(rows) == 1
        assert rows[0]["messages_before"] >= 0
