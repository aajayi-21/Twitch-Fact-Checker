"""Tests for the streamer database extension (streamer/db.py).

Schema idempotence, the partial-upsert guarantee on chat_channels (a
``!fc cap 6`` must never clobber ``armed_at``), the strict-vs-swallow split,
and the privacy commitment that chat_commands stores verbs only.
"""

from pathlib import Path

import pytest

from streamer.chat.consent import ChannelRecord
from streamer.db import StreamerDatabase


@pytest.fixture()
async def database(tmp_path: Path):
    db = StreamerDatabase(str(tmp_path / "streamer-test.db"))
    await db.open()
    yield db
    await db.close()


class TestSchema:
    async def test_open_is_idempotent_across_restarts(self, tmp_path: Path) -> None:
        path = str(tmp_path / "streamer.db")
        first = StreamerDatabase(path)
        await first.open()
        await first.upsert_chat_channel(platform="twitch", channel="alice", mode="auto")
        await first.close()

        second = StreamerDatabase(path)
        await second.open()  # CREATE IF NOT EXISTS must not complain or wipe
        row = await second.fetch_chat_channel("twitch", "alice")
        await second.close()
        assert row is not None and row["mode"] == "auto"

    async def test_shared_tables_also_exist(self, database) -> None:
        """The streamer db is a SUPERSET: sessions/claims/verdicts from the
        shared schema plus the chat tables, in one file."""
        await database.record_session_start(
            session_id="s1", platform="twitch", channel="alice", title=None
        )
        rows = await database.fetch_sessions(10)
        assert [row["id"] for row in rows] == ["s1"]


class TestChannelUpsert:
    async def test_partial_upsert_touches_only_supplied_fields(self, database) -> None:
        """`!fc cap 6` (a config write) must never clobber the consent row."""
        await database.upsert_chat_channel(
            platform="twitch",
            channel="alice",
            armed_at="2026-08-18T00:00:00Z",
            armed_by_user_id="42",
            consent_method="mod+command",
        )
        await database.upsert_chat_channel(
            platform="twitch", channel="alice", mode="auto"
        )
        row = await database.fetch_chat_channel("twitch", "alice")
        assert row["mode"] == "auto"
        assert row["armed_at"] == "2026-08-18T00:00:00Z"
        assert row["armed_by_user_id"] == "42"

    async def test_unknown_fields_are_rejected_loudly(self, database) -> None:
        with pytest.raises(ValueError, match="unknown chat_channels fields"):
            await database.upsert_chat_channel(
                platform="twitch", channel="alice", nonsense="x"
            )

    async def test_missing_channel_reads_as_none(self, database) -> None:
        assert await database.fetch_chat_channel("twitch", "ghost") is None
        assert await database.fetch_channel_record("twitch", "ghost") is None


class TestChannelRecord:
    async def test_armed_record(self, database) -> None:
        await database.upsert_chat_channel(
            platform="twitch",
            channel="alice",
            armed_at="2026-08-18T00:00:00Z",
            armed_by_user_id="42",
            room_id="42",
        )
        record = await database.fetch_channel_record("twitch", "alice")
        assert record == ChannelRecord(armed=True, armed_by_user_id="42", room_id="42")

    async def test_disarm_after_arm_reads_unarmed(self, database) -> None:
        await database.upsert_chat_channel(
            platform="twitch",
            channel="alice",
            armed_at="2026-08-18T00:00:00Z",
            armed_by_user_id="42",
        )
        await database.upsert_chat_channel(
            platform="twitch", channel="alice", disarmed_at="2026-08-18T01:00:00Z"
        )
        record = await database.fetch_channel_record("twitch", "alice")
        assert record is not None and record.armed is False

    async def test_rearm_after_disarm_reads_armed(self, database) -> None:
        await database.upsert_chat_channel(
            platform="twitch",
            channel="alice",
            armed_at="2026-08-18T00:00:00Z",
            disarmed_at="2026-08-18T01:00:00Z",
        )
        await database.upsert_chat_channel(
            platform="twitch", channel="alice", armed_at="2026-08-18T02:00:00Z"
        )
        record = await database.fetch_channel_record("twitch", "alice")
        assert record is not None and record.armed is True


class TestChatPosts:
    async def test_records_a_decision_row(self, database) -> None:
        post_id = await database.record_chat_post(
            verdict_id="v1",
            claim_id="c1",
            session_id="s1",
            platform="twitch",
            channel="alice",
            handle="3f2a",
            status="posted",
            reason="ok",
            mode="auto",
            label="FALSE",
            topic="other",
            message_text="❌ FALSE · ...",
            posted_at="2026-08-18T00:00:00Z",
        )
        rows = await database.fetch_chat_posts("twitch", "alice")
        assert len(rows) == 1
        assert rows[0]["id"] == post_id
        assert rows[0]["status"] == "posted"

    async def test_status_filter(self, database) -> None:
        for status, reason in (
            ("posted", "ok"),
            ("suppressed", "hourly_cap"),
            ("queued", "review_mode"),
        ):
            await database.record_chat_post(
                verdict_id=None,
                claim_id=None,
                session_id=None,
                platform="twitch",
                channel="alice",
                handle=None,
                status=status,
                reason=reason,
                mode="auto",
            )
        queued = await database.fetch_chat_posts("twitch", "alice", status="queued")
        assert [row["reason"] for row in queued] == ["review_mode"]

    async def test_upsert_by_id_advances_a_queued_row_to_posted(self, database) -> None:
        """The review flow: one row per verdict — queueing writes it, the
        approve click advances the SAME row."""
        post_id = await database.record_chat_post(
            verdict_id="v1",
            claim_id=None,
            session_id=None,
            platform="twitch",
            channel="alice",
            handle="3f2a",
            status="queued",
            reason="review_mode",
            mode="review",
        )
        await database.record_chat_post(
            post_id=post_id,
            verdict_id="v1",
            claim_id=None,
            session_id=None,
            platform="twitch",
            channel="alice",
            handle="3f2a",
            status="posted",
            reason="approved",
            mode="review",
            posted_at="2026-08-18T00:00:00Z",
            approved_by_user_id="42",
        )
        rows = await database.fetch_chat_posts("twitch", "alice")
        assert len(rows) == 1
        assert rows[0]["status"] == "posted"
        assert rows[0]["approved_by_user_id"] == "42"

    async def test_lookup_by_handle_returns_newest(self, database) -> None:
        for index in range(2):
            await database.record_chat_post(
                verdict_id=f"v{index}",
                claim_id=None,
                session_id=None,
                platform="twitch",
                channel="alice",
                handle="3f2a",
                status="posted",
                reason="ok",
                mode="auto",
            )
        row = await database.fetch_chat_post_by_handle("twitch", "alice", "3f2a")
        assert row is not None and row["verdict_id"] == "v1"

    async def test_retraction_marks_the_row(self, database) -> None:
        post_id = await database.record_chat_post(
            verdict_id="v1",
            claim_id=None,
            session_id=None,
            platform="twitch",
            channel="alice",
            handle="3f2a",
            status="posted",
            reason="ok",
            mode="auto",
        )
        await database.mark_chat_post_retracted(
            post_id=post_id, retracted_by_user_id="42"
        )
        row = await database.fetch_chat_post_by_handle("twitch", "alice", "3f2a")
        assert row is not None
        assert row["retracted_at"] is not None
        assert row["retracted_by_user_id"] == "42"

    async def test_summary_builds_the_drop_reason_histogram(self, database) -> None:
        for status, reason in (
            ("posted", "ok"),
            ("suppressed", "hourly_cap"),
            ("suppressed", "hourly_cap"),
            ("suppressed", "stale"),
        ):
            await database.record_chat_post(
                verdict_id=None,
                claim_id=None,
                session_id=None,
                platform="twitch",
                channel="alice",
                handle=None,
                status=status,
                reason=reason,
                mode="auto",
            )
        summary = await database.fetch_chat_summary()
        assert summary["by_status"] == {"posted": 1, "suppressed": 3}
        assert summary["drop_reasons"] == {"hourly_cap": 2, "stale": 1}

    async def test_record_after_close_is_swallowed(self, tmp_path: Path) -> None:
        """Fire-and-forget doctrine: the bot's hot path must survive a dead
        database. (Local instance: the shared fixture would double-close.)"""
        db = StreamerDatabase(str(tmp_path / "dead.db"))
        await db.open()
        await db.close()
        await db.record_chat_post(  # must not raise
            verdict_id=None,
            claim_id=None,
            session_id=None,
            platform="twitch",
            channel="alice",
            handle=None,
            status="posted",
            reason="ok",
            mode="auto",
        )


class TestChatCommands:
    async def test_records_the_verb_only(self, database) -> None:
        """Privacy commitment: a dispute note or mistyped password in the
        arguments must never land in an analytics table — the schema call
        takes only the verb, so there is nowhere for args to go."""
        await database.record_chat_command(
            platform="twitch",
            channel="alice",
            user_id="1001",
            role="moderator",
            command="mute",
            accepted=True,
        )

        def _read():
            conn = database._require_conn()
            return [dict(row) for row in conn.execute("SELECT * FROM chat_commands")]

        rows = await database._run(_read)
        assert len(rows) == 1
        assert rows[0]["command"] == "mute"
        assert "args" not in rows[0]


class TestChatEngagement:
    async def test_records_counts_only(self, database) -> None:
        await database.record_chat_engagement(
            post_id="p1",
            window_s=60,
            messages_before=5,
            messages_after=12,
            unique_chatters_after=7,
        )

        def _read():
            conn = database._require_conn()
            return [dict(row) for row in conn.execute("SELECT * FROM chat_engagement")]

        rows = await database._run(_read)
        assert rows[0]["messages_after"] == 12
        # No text column exists at all: counts, never chat logs.
        assert not any("text" in key for key in rows[0])
