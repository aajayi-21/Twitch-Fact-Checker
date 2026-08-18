"""Streamer persistence: the shared analytics schema plus the chat tables.

:class:`StreamerDatabase` extends :class:`app.db.Database` — same single
db-executor thread, same fire-and-forget (``_swallow``) vs strict (``_run``)
split — and applies the chat schema on top of the shared one. It opens the
STREAMER'S OWN file (``streamer.db``), never the viewer tool's: the two
products run side by side and must never contaminate each other's data.

Which side of the swallow/strict split each write sits on is a safety
decision, not a style one:

- ``record_chat_post`` / ``record_chat_command`` / ``record_chat_engagement``
  are **fire-and-forget**: they run on the bot's hot path, and a persistence
  failure must never touch posting behaviour.
- The ``chat_channels`` writes (arm/disarm, mode, mute) are **strict**: a
  lost consent record or an unpersisted ``!fc off`` is a safety failure, not
  a lost metric — the caller must know.

Privacy commitments encoded in the schema itself:

- ``chat_commands.command`` stores the parsed VERB only, never arguments — a
  dispute note or a mistyped password must never land in an analytics table.
- ``chat_engagement`` stores counts computed from an in-memory window, never
  chat text — "does chat engage?" gets answered without becoming a chat-log
  warehouse.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from app.db import Database
from app.models import utc_now_iso

from streamer.chat.consent import ChannelRecord

logger = logging.getLogger(__name__)

CHAT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chat_channels (
    platform          TEXT NOT NULL,
    channel           TEXT NOT NULL,
    room_id           TEXT,
    mode              TEXT NOT NULL DEFAULT 'review',
    armed_at          TEXT,
    armed_by_user_id  TEXT,
    consent_method    TEXT,
    disarmed_at       TEXT,
    muted_until       TEXT,
    trusted           INTEGER NOT NULL DEFAULT 0,
    approved_posts    INTEGER NOT NULL DEFAULT 0,
    retractions       INTEGER NOT NULL DEFAULT 0,
    config_json       TEXT NOT NULL DEFAULT '{}',
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (platform, channel)
);

-- One row per verdict per DECISION: what we did with it, not only what we
-- posted. The review queue is status='queued' here rather than a third
-- table — "what happened to this verdict" is one fact.
--
-- Deliberately NO foreign keys on verdict_id/session_id: those rows are
-- written fire-and-forget from a different task, so ordering is not
-- guaranteed, and an FK violation here would be swallowed — silently losing
-- the measurement row this table exists for. Indexes give the joins.
CREATE TABLE IF NOT EXISTS chat_posts (
    id                   TEXT PRIMARY KEY,
    verdict_id           TEXT,
    claim_id             TEXT,
    session_id           TEXT,
    platform             TEXT NOT NULL,
    channel              TEXT NOT NULL,
    handle               TEXT,
    status               TEXT NOT NULL,
    reason               TEXT NOT NULL,
    mode                 TEXT NOT NULL,
    label                TEXT,
    topic                TEXT,
    check_worthiness     REAL,
    source_count         INTEGER,
    distinct_domains     INTEGER,
    best_tier            TEXT,
    used_fallback        INTEGER,
    claim_age_s          REAL,
    stream_time_s        REAL,
    message_text         TEXT,
    message_id           TEXT,
    posted_at            TEXT,
    approved_by_user_id  TEXT,
    retracted_at         TEXT,
    retracted_by_user_id TEXT,
    created_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_posts_channel
    ON chat_posts(platform, channel, created_at);
CREATE INDEX IF NOT EXISTS idx_chat_posts_verdict ON chat_posts(verdict_id);
CREATE INDEX IF NOT EXISTS idx_chat_posts_status
    ON chat_posts(status, created_at);

CREATE TABLE IF NOT EXISTS chat_commands (
    id         TEXT PRIMARY KEY,
    platform   TEXT NOT NULL,
    channel    TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    role       TEXT NOT NULL,
    command    TEXT NOT NULL,
    accepted   INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_commands_channel
    ON chat_commands(platform, channel, created_at);

CREATE TABLE IF NOT EXISTS chat_engagement (
    post_id                TEXT PRIMARY KEY,
    window_s               INTEGER NOT NULL,
    messages_before        INTEGER NOT NULL,
    messages_after         INTEGER NOT NULL,
    unique_chatters_after  INTEGER NOT NULL,
    computed_at            TEXT NOT NULL
);
"""

# chat_posts.status values. "dry_run" is a would-have-posted row: the exact
# message, recorded but never sent — the pre-launch review artifact.
POST_STATUSES = (
    "posted",
    "queued",
    "suppressed",
    "failed",
    "expired",
    "skipped",
    "dry_run",
)

# Fields a partial chat_channels upsert may touch. An upsert must only write
# what the caller supplied — a `!fc cap 6` must never clobber the armed_at.
_CHANNEL_FIELDS = frozenset(
    {
        "room_id",
        "mode",
        "armed_at",
        "armed_by_user_id",
        "consent_method",
        "disarmed_at",
        "muted_until",
        "trusted",
        "approved_posts",
        "retractions",
        "config_json",
    }
)


class StreamerDatabase(Database):
    """The shared schema plus the chat tables, on the streamer's own file."""

    def _open_sync(self) -> None:
        super()._open_sync()
        conn = self._require_conn()
        conn.executescript(CHAT_SCHEMA_SQL)
        conn.commit()

    # ------------------------------------------------------------------ #
    # chat_channels — STRICT (consent and kill-switch state)
    # ------------------------------------------------------------------ #

    async def upsert_chat_channel(
        self, *, platform: str, channel: str, **fields: Any
    ) -> None:
        """Partial upsert: only supplied fields are written.

        Strict on purpose — an unpersisted ``!fc off`` or a lost ``armed_at``
        is a safety failure the caller must hear about.
        """
        unknown = set(fields) - _CHANNEL_FIELDS
        if unknown:
            raise ValueError(f"unknown chat_channels fields: {sorted(unknown)}")

        def _write() -> None:
            conn = self._require_conn()
            columns = ", ".join(fields)
            placeholders = ", ".join("?" for _ in fields)
            updates = ", ".join(f"{name} = excluded.{name}" for name in fields)
            conn.execute(
                f"INSERT INTO chat_channels (platform, channel, {columns},"
                f" updated_at) VALUES (?, ?, {placeholders}, ?)"
                f" ON CONFLICT(platform, channel) DO UPDATE SET {updates},"
                " updated_at = excluded.updated_at",
                (platform, channel, *fields.values(), utc_now_iso()),
            )
            conn.commit()

        await self._run(_write)

    async def fetch_chat_channel(
        self, platform: str, channel: str
    ) -> dict[str, Any] | None:
        def _read() -> dict[str, Any] | None:
            conn = self._require_conn()
            row = conn.execute(
                "SELECT * FROM chat_channels WHERE platform = ? AND channel = ?",
                (platform, channel),
            ).fetchone()
            return dict(row) if row is not None else None

        return await self._run(_read)

    async def fetch_channel_record(
        self, platform: str, channel: str
    ) -> ChannelRecord | None:
        """The consent-proof view of a channel row (None = unarmed, off)."""
        row = await self.fetch_chat_channel(platform, channel)
        if row is None:
            return None
        armed = bool(row["armed_at"]) and not (
            row["disarmed_at"] and row["disarmed_at"] > row["armed_at"]
        )
        return ChannelRecord(
            armed=armed,
            armed_by_user_id=row["armed_by_user_id"],
            room_id=row["room_id"],
        )

    # ------------------------------------------------------------------ #
    # chat_posts — fire-and-forget writes, strict reads
    # ------------------------------------------------------------------ #

    async def record_chat_post(
        self,
        *,
        post_id: str | None = None,
        verdict_id: str | None,
        claim_id: str | None,
        session_id: str | None,
        platform: str,
        channel: str,
        handle: str | None,
        status: str,
        reason: str,
        mode: str,
        label: str | None = None,
        topic: str | None = None,
        check_worthiness: float | None = None,
        source_count: int | None = None,
        distinct_domains: int | None = None,
        best_tier: str | None = None,
        used_fallback: bool | None = None,
        claim_age_s: float | None = None,
        stream_time_s: float | None = None,
        message_text: str | None = None,
        message_id: str | None = None,
        posted_at: str | None = None,
        approved_by_user_id: str | None = None,
    ) -> str:
        """One decision row (posted/queued/suppressed/…). Returns the row id.

        Fire-and-forget: this runs on the bot's hot path and a persistence
        failure must never affect posting behaviour.
        """
        row_id = post_id or uuid4().hex

        def _write() -> None:
            conn = self._require_conn()
            conn.execute(
                "INSERT INTO chat_posts (id, verdict_id, claim_id, session_id,"
                " platform, channel, handle, status, reason, mode, label,"
                " topic, check_worthiness, source_count, distinct_domains,"
                " best_tier, used_fallback, claim_age_s, stream_time_s,"
                " message_text, message_id, posted_at, approved_by_user_id,"
                " created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                " ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET status = excluded.status,"
                " reason = excluded.reason, message_id = excluded.message_id,"
                " posted_at = COALESCE(excluded.posted_at, posted_at),"
                " approved_by_user_id ="
                " COALESCE(excluded.approved_by_user_id, approved_by_user_id)",
                (
                    row_id,
                    verdict_id,
                    claim_id,
                    session_id,
                    platform,
                    channel,
                    handle,
                    status,
                    reason,
                    mode,
                    label,
                    topic,
                    check_worthiness,
                    source_count,
                    distinct_domains,
                    best_tier,
                    None if used_fallback is None else int(used_fallback),
                    claim_age_s,
                    stream_time_s,
                    message_text,
                    message_id,
                    posted_at,
                    approved_by_user_id,
                    utc_now_iso(),
                ),
            )
            conn.commit()

        await self._swallow(_write)
        return row_id

    async def mark_chat_post_retracted(
        self, *, post_id: str, retracted_by_user_id: str
    ) -> None:
        def _write() -> None:
            conn = self._require_conn()
            conn.execute(
                "UPDATE chat_posts SET retracted_at = ?,"
                " retracted_by_user_id = ? WHERE id = ?",
                (utc_now_iso(), retracted_by_user_id, post_id),
            )
            conn.commit()

        await self._swallow(_write)

    async def fetch_chat_posts(
        self,
        platform: str,
        channel: str,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        def _read() -> list[dict[str, Any]]:
            conn = self._require_conn()
            query = "SELECT * FROM chat_posts WHERE platform = ? AND channel = ?"
            params: list[Any] = [platform, channel]
            if status is not None:
                query += " AND status = ?"
                params.append(status)
            query += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            return [dict(row) for row in conn.execute(query, params)]

        return await self._run(_read)

    async def fetch_chat_post_by_handle(
        self, platform: str, channel: str, handle: str
    ) -> dict[str, Any] | None:
        """Newest post matching a short handle (``!fc why/wrong <handle>``)."""

        def _read() -> dict[str, Any] | None:
            conn = self._require_conn()
            row = conn.execute(
                "SELECT * FROM chat_posts WHERE platform = ? AND channel = ?"
                " AND handle = ? ORDER BY created_at DESC LIMIT 1",
                (platform, channel, handle),
            ).fetchone()
            return dict(row) if row is not None else None

        return await self._run(_read)

    async def fetch_chat_summary(self) -> dict[str, Any]:
        """Decision counts grouped by status and reason — the drop-reason
        histogram is the predicate's tuning instrument."""

        def _read() -> dict[str, Any]:
            conn = self._require_conn()
            by_reason: dict[str, int] = {}
            by_status: dict[str, int] = {}
            for row in conn.execute(
                "SELECT status, reason, COUNT(*) AS n FROM chat_posts"
                " GROUP BY status, reason"
            ):
                by_status[row["status"]] = by_status.get(row["status"], 0) + row["n"]
                if row["status"] != "posted":
                    by_reason[row["reason"]] = (
                        by_reason.get(row["reason"], 0) + row["n"]
                    )
            return {"by_status": by_status, "drop_reasons": by_reason}

        return await self._run(_read)

    # ------------------------------------------------------------------ #
    # chat_commands / chat_engagement — fire-and-forget
    # ------------------------------------------------------------------ #

    async def record_chat_command(
        self,
        *,
        platform: str,
        channel: str,
        user_id: str,
        role: str,
        command: str,
        accepted: bool,
    ) -> None:
        """The parsed VERB only — arguments are never persisted here."""

        def _write() -> None:
            conn = self._require_conn()
            conn.execute(
                "INSERT INTO chat_commands (id, platform, channel, user_id,"
                " role, command, accepted, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    uuid4().hex,
                    platform,
                    channel,
                    user_id,
                    role,
                    command,
                    int(accepted),
                    utc_now_iso(),
                ),
            )
            conn.commit()

        await self._swallow(_write)

    async def record_chat_engagement(
        self,
        *,
        post_id: str,
        window_s: int,
        messages_before: int,
        messages_after: int,
        unique_chatters_after: int,
    ) -> None:
        def _write() -> None:
            conn = self._require_conn()
            conn.execute(
                "INSERT OR REPLACE INTO chat_engagement (post_id, window_s,"
                " messages_before, messages_after, unique_chatters_after,"
                " computed_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    post_id,
                    window_s,
                    messages_before,
                    messages_after,
                    unique_chatters_after,
                    utc_now_iso(),
                ),
            )
            conn.commit()

        await self._swallow(_write)
