"""SQLite persistence for sessions, claims, verdicts, feedback, contradictions.

Design contract (report §3.3):

- One ``Database`` instance lives on ``app.state.db``. The ``sqlite3``
  connection is created INSIDE a dedicated single-worker executor and never
  leaves that thread, so the stdlib's default ``check_same_thread=True`` is
  correct rather than worked around.
- Pipeline-facing writes (``record_*``) are **fire-and-forget**: they await
  the executor hop (sub-millisecond WAL inserts, so ordering vs. subsequent
  reads stays trivial) but **log-never-raise** — a persistence failure must
  never kill a session, matching the pipeline's per-item error doctrine.
- Reads and the feedback upsert are **strict** (they may raise); HTTP
  handlers convert failures to status codes.
- ``DayCounter`` keeps the "checks today" number in process memory so the
  popup's 3-second ``/healthz`` poll never touches SQLite.

Video frames are deliberately NOT persisted anywhere (report §5.3).
"""

import asyncio
import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from app.claim_gate import GatePass
from app.fact_checker import normalize_claim
from app.models import GateClaim, Verdict, utc_now_iso
from app.reports import load_labelled_verdicts, source_tier_breakdown

logger = logging.getLogger(__name__)

# Claim funnel outcomes (claims.outcome). "pending" = enqueued for
# verification, never completed (e.g. the session ended first). Cooldown
# drops map to "verify_failed" — the enum deliberately has no cooldown slot.
CLAIM_OUTCOMES: frozenset[str] = frozenset(
    {
        "pending",
        "below_threshold",
        "topic_skipped",
        "duplicate",
        "queue_dropped",
        "verify_failed",
        "verified",
    }
)
_TERMINAL_OUTCOMES: frozenset[str] = frozenset({"verified", "verify_failed"})

# Exported so tests can build a schema with plain sqlite3 (no event loop).
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    platform        TEXT,
    channel         TEXT,
    title           TEXT,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    speech_seconds  REAL    NOT NULL DEFAULT 0,
    audio_seconds   REAL    NOT NULL DEFAULT 0,
    gate_calls      INTEGER NOT NULL DEFAULT 0,
    verify_calls    INTEGER NOT NULL DEFAULT 0,
    est_cost_usd    REAL    NOT NULL DEFAULT 0,
    stt_drop_counts TEXT    NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS claims (
    id               TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL REFERENCES sessions(id),
    text             TEXT NOT NULL,
    normalized       TEXT NOT NULL,
    topic            TEXT NOT NULL,
    check_worthiness REAL NOT NULL,
    stream_time_s    REAL,
    gated_at         TEXT NOT NULL,
    outcome          TEXT NOT NULL,
    completed_at     TEXT,
    has_visual_cue   INTEGER NOT NULL DEFAULT 0,
    gate_pass_id     TEXT
);
CREATE INDEX IF NOT EXISTS idx_claims_session ON claims(session_id);
CREATE INDEX IF NOT EXISTS idx_claims_outcome ON claims(outcome, completed_at);

CREATE TABLE IF NOT EXISTS verdicts (
    id            TEXT PRIMARY KEY,
    claim_id      TEXT NOT NULL REFERENCES claims(id),
    session_id    TEXT NOT NULL REFERENCES sessions(id),
    label         TEXT NOT NULL,
    explanation   TEXT NOT NULL,
    checked_at    TEXT NOT NULL,
    used_fallback INTEGER NOT NULL DEFAULT 0,
    latency_ms    INTEGER,
    provider      TEXT,
    model         TEXT
);
CREATE INDEX IF NOT EXISTS idx_verdicts_session ON verdicts(session_id);
CREATE INDEX IF NOT EXISTS idx_verdicts_day ON verdicts(checked_at);

CREATE TABLE IF NOT EXISTS sources (
    verdict_id TEXT    NOT NULL REFERENCES verdicts(id),
    rank       INTEGER NOT NULL,
    url        TEXT    NOT NULL,
    domain     TEXT,
    title      TEXT,
    PRIMARY KEY (verdict_id, rank)
);

CREATE TABLE IF NOT EXISTS feedback (
    verdict_id      TEXT PRIMARY KEY REFERENCES verdicts(id),
    rating          TEXT NOT NULL,
    corrected_label TEXT,
    note            TEXT,
    created_at      TEXT NOT NULL
);

-- One row per session gate pass (live + stop flush). The transcript text
-- (context, new_text) is stored ONLY while the Jev pre-screen is on: it is
-- what calibrating Jev needs, and otherwise the table holds metadata only.
CREATE TABLE IF NOT EXISTS gate_passes (
    id                 TEXT PRIMARY KEY,
    session_id         TEXT NOT NULL REFERENCES sessions(id),
    started_at         TEXT NOT NULL,
    phase              TEXT NOT NULL,
    context            TEXT,
    new_text           TEXT,
    word_count         INTEGER NOT NULL,
    latency_ms         INTEGER,
    claims_count       INTEGER,
    error              TEXT,
    gate_provider      TEXT NOT NULL,
    gate_model         TEXT NOT NULL,
    jev_mode           TEXT NOT NULL DEFAULT 'off',
    jev_model          TEXT,
    jev_resolved_model TEXT,
    jev_probability    REAL,
    jev_threshold      REAL,
    jev_route          TEXT,
    jev_latency_ms     INTEGER,
    jev_error          TEXT
);
CREATE INDEX IF NOT EXISTS idx_gate_passes_session ON gate_passes(session_id);
CREATE INDEX IF NOT EXISTS idx_gate_passes_jev ON gate_passes(jev_mode, started_at);

CREATE TABLE IF NOT EXISTS contradictions (
    id               TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL REFERENCES sessions(id),
    current_claim    TEXT NOT NULL,
    prior_claim      TEXT NOT NULL,
    prior_claimed_at TEXT NOT NULL,
    confidence       TEXT NOT NULL,
    explanation      TEXT NOT NULL,
    emitted          INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL
);
"""


def _utc_today() -> str:
    """Current UTC date as ``YYYY-MM-DD`` (monkeypatched in DayCounter tests)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class DayCounter:
    """In-memory count of today's verification attempts (UTC day rollover).

    Touched only from the event-loop thread (the pipeline increments, healthz
    reads), so no locking. Seeded at startup from
    :meth:`Database.count_checks_today` so a restart doesn't zero the popup
    readout.
    """

    def __init__(self, initial: int = 0) -> None:
        self._day = _utc_today()
        self._value = initial

    def increment(self) -> None:
        self._roll_over_if_new_day()
        self._value += 1

    @property
    def value(self) -> int:
        self._roll_over_if_new_day()
        return self._value

    def _roll_over_if_new_day(self) -> None:
        today = _utc_today()
        if today != self._day:
            self._day = today
            self._value = 0


class Database:
    """SQLite persistence confined to one dedicated executor thread."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db")
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def open(self) -> None:
        """Create/upgrade the schema; blocking work runs on the db executor."""
        await asyncio.get_running_loop().run_in_executor(
            self._executor, self._open_sync
        )
        logger.info("analytics database ready at %s", self._path)

    def _open_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA_SQL)
        self._migrate(conn)
        conn.commit()
        self._conn = conn

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Additive, idempotent upgrades for databases created by older schemas.

        ``CREATE TABLE IF NOT EXISTS`` never touches an existing table, so
        columns added later need an explicit guarded ``ALTER``.
        """
        verdict_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(verdicts)")
        }
        if "evidence" not in verdict_columns:
            # The verify model's own rating of how directly the sources
            # addressed the claim (strong/partial/none; NULL for providers
            # and fallbacks that do not produce it).
            conn.execute("ALTER TABLE verdicts ADD COLUMN evidence TEXT")
        claim_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(claims)")
        }
        if "gate_pass_id" not in claim_columns:
            # The gate_passes row that produced the claim. No foreign key on
            # purpose: pass rows are fire-and-forget, and a failed pass
            # insert must never cost the claim row.
            conn.execute("ALTER TABLE claims ADD COLUMN gate_pass_id TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_claims_gate_pass ON claims(gate_pass_id)"
        )

    async def close(self) -> None:
        """Flush queued work (executor drains), close the connection."""

        def _close_sync() -> None:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

        try:
            await asyncio.get_running_loop().run_in_executor(
                self._executor, _close_sync
            )
        finally:
            self._executor.shutdown(wait=True)

    # ------------------------------------------------------------------ #
    # Executor plumbing
    # ------------------------------------------------------------------ #

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("database is not open")
        return self._conn

    async def _run(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Strict executor hop: exceptions propagate to the caller."""
        return await asyncio.get_running_loop().run_in_executor(
            self._executor, fn, *args
        )

    async def _swallow(self, fn: Callable[..., Any], *args: Any) -> None:
        """Fire-and-forget executor hop: log-never-raise (pipeline doctrine)."""
        try:
            await asyncio.get_running_loop().run_in_executor(self._executor, fn, *args)
        except Exception:
            logger.warning("db write failed in %s", fn.__name__, exc_info=True)

    # ------------------------------------------------------------------ #
    # Fire-and-forget writes (pipeline-facing)
    # ------------------------------------------------------------------ #

    async def record_session_start(
        self,
        *,
        session_id: str,
        platform: str | None,
        channel: str | None,
        title: str | None,
    ) -> None:
        def _write() -> None:
            conn = self._require_conn()
            conn.execute(
                "INSERT OR REPLACE INTO sessions (id, platform, channel, title,"
                " started_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, platform, channel, title, utc_now_iso()),
            )
            conn.commit()

        await self._swallow(_write)

    async def record_session_end(
        self,
        *,
        session_id: str,
        speech_seconds: float,
        audio_seconds: float,
        gate_calls: int,
        verify_calls: int,
        est_cost_usd: float,
        stt_drop_counts: dict[str, int],
    ) -> None:
        def _write() -> None:
            conn = self._require_conn()
            conn.execute(
                "UPDATE sessions SET ended_at = ?, speech_seconds = ?,"
                " audio_seconds = ?, gate_calls = ?, verify_calls = ?,"
                " est_cost_usd = ?, stt_drop_counts = ? WHERE id = ?",
                (
                    utc_now_iso(),
                    speech_seconds,
                    audio_seconds,
                    gate_calls,
                    verify_calls,
                    est_cost_usd,
                    json.dumps(stt_drop_counts),
                    session_id,
                ),
            )
            conn.commit()

        await self._swallow(_write)

    async def record_session_progress(
        self,
        *,
        session_id: str,
        speech_seconds: float,
        audio_seconds: float,
        gate_calls: int,
        verify_calls: int,
        est_cost_usd: float,
        stt_drop_counts: dict[str, int],
    ) -> None:
        """Periodic flush of a LIVE session's running counters.

        Same columns as :meth:`record_session_end` but ``ended_at`` stays
        NULL — the dashboard derives "finished" and watch time from it — and
        a flush that races the end write can never clobber a finished row
        (``AND ended_at IS NULL``). Exists so a crash mid-session does not
        lose the whole session's numbers, which used to be written at end
        only.
        """

        def _write() -> None:
            conn = self._require_conn()
            conn.execute(
                "UPDATE sessions SET speech_seconds = ?, audio_seconds = ?,"
                " gate_calls = ?, verify_calls = ?, est_cost_usd = ?,"
                " stt_drop_counts = ? WHERE id = ? AND ended_at IS NULL",
                (
                    speech_seconds,
                    audio_seconds,
                    gate_calls,
                    verify_calls,
                    est_cost_usd,
                    json.dumps(stt_drop_counts),
                    session_id,
                ),
            )
            conn.commit()

        await self._swallow(_write)

    async def record_gate_pass(
        self,
        *,
        gate_pass: GatePass,
        session_id: str,
        phase: str,
        gate_provider: str,
        gate_model: str,
    ) -> None:
        """One ``gate_passes`` row. Transcript text only when Jev ran."""
        screen = gate_pass.screen
        store_text = screen is not None

        def _write() -> None:
            conn = self._require_conn()
            conn.execute(
                "INSERT OR REPLACE INTO gate_passes (id, session_id, started_at,"
                " phase, context, new_text, word_count, latency_ms, claims_count,"
                " error, gate_provider, gate_model, jev_mode, jev_model,"
                " jev_resolved_model, jev_probability, jev_threshold, jev_route,"
                " jev_latency_ms, jev_error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    gate_pass.id,
                    session_id,
                    gate_pass.started_at,
                    phase,
                    gate_pass.context if store_text else None,
                    gate_pass.new_text if store_text else None,
                    gate_pass.word_count,
                    gate_pass.latency_ms,
                    gate_pass.claims_count,
                    gate_pass.error,
                    gate_provider,
                    gate_model,
                    screen.mode if screen else "off",
                    screen.model if screen else None,
                    screen.resolved_model if screen else None,
                    screen.probability if screen else None,
                    screen.threshold if screen else None,
                    screen.route if screen else None,
                    screen.latency_ms if screen else None,
                    screen.error if screen else None,
                ),
            )
            conn.commit()

        await self._swallow(_write)

    async def record_claim(
        self,
        *,
        claim: GateClaim,
        session_id: str,
        outcome: str,
        has_visual_cue: bool = False,
        stream_time_s: float | None = None,
        gate_pass_id: str | None = None,
    ) -> None:
        """Upsert one funnel row.

        First write inserts the full row; later writes only advance
        ``outcome`` (and stamp ``completed_at`` on terminal outcomes) so
        ``gated_at``/text fields are never clobbered. The flush-phase path
        can write a terminal outcome with no prior "pending" row — the upsert
        inserts it whole in that case.
        """
        now = utc_now_iso()
        completed_at = now if outcome in _TERMINAL_OUTCOMES else None

        def _write() -> None:
            conn = self._require_conn()
            conn.execute(
                "INSERT INTO claims (id, session_id, text, normalized, topic,"
                " check_worthiness, stream_time_s, gated_at, outcome,"
                " completed_at, has_visual_cue, gate_pass_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET outcome = excluded.outcome,"
                " completed_at = COALESCE(excluded.completed_at, completed_at),"
                # Keep the FIRST non-null position: a later terminal write
                # happens after more audio has been transcribed, so its head
                # would point past where the claim was actually spoken.
                " stream_time_s = COALESCE(stream_time_s, excluded.stream_time_s),"
                " gate_pass_id = COALESCE(gate_pass_id, excluded.gate_pass_id)",
                (
                    claim.id,
                    session_id,
                    claim.claim_text,
                    normalize_claim(claim.claim_text),
                    claim.topic,
                    claim.check_worthiness,
                    stream_time_s,
                    now,
                    outcome,
                    completed_at,
                    int(has_visual_cue),
                    gate_pass_id,
                ),
            )
            conn.commit()

        await self._swallow(_write)

    async def record_verdict(
        self,
        *,
        verdict: Verdict,
        claim_id: str,
        session_id: str,
        latency_ms: int,
        provider: str,
        model: str,
    ) -> None:
        def _write() -> None:
            conn = self._require_conn()
            conn.execute(
                "INSERT OR REPLACE INTO verdicts (id, claim_id, session_id,"
                " label, explanation, checked_at, used_fallback, latency_ms,"
                " provider, model, evidence)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    verdict.id,
                    claim_id,
                    session_id,
                    verdict.label,
                    verdict.explanation,
                    verdict.checked_at,
                    int(verdict.used_fallback),
                    latency_ms,
                    provider,
                    model,
                    verdict.evidence,
                ),
            )
            conn.executemany(
                "INSERT OR REPLACE INTO sources (verdict_id, rank, url, domain,"
                " title) VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        verdict.id,
                        rank,
                        source.url,
                        urlsplit(source.url).hostname,
                        source.title,
                    )
                    for rank, source in enumerate(verdict.sources)
                ],
            )
            conn.commit()

        await self._swallow(_write)

    async def record_contradiction(
        self,
        *,
        session_id: str,
        current_claim: str,
        prior_claim: str,
        prior_claimed_at: str,
        confidence: str,
        explanation: str,
        emitted: bool,
    ) -> None:
        from uuid import uuid4

        row_id = uuid4().hex

        def _write() -> None:
            conn = self._require_conn()
            conn.execute(
                "INSERT INTO contradictions (id, session_id, current_claim,"
                " prior_claim, prior_claimed_at, confidence, explanation,"
                " emitted, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row_id,
                    session_id,
                    current_claim,
                    prior_claim,
                    prior_claimed_at,
                    confidence,
                    explanation,
                    int(emitted),
                    utc_now_iso(),
                ),
            )
            conn.commit()

        await self._swallow(_write)

    # ------------------------------------------------------------------ #
    # Strict operations (HTTP handlers convert exceptions to status codes)
    # ------------------------------------------------------------------ #

    async def record_feedback(
        self,
        verdict_id: str,
        rating: str,
        corrected_label: str | None,
        note: str | None,
    ) -> bool:
        """Upsert user feedback; False when the verdict id is unknown.

        The existence check and the upsert run inside one executor job, so
        they cannot interleave with another write.
        """

        def _write() -> bool:
            conn = self._require_conn()
            row = conn.execute(
                "SELECT 1 FROM verdicts WHERE id = ?", (verdict_id,)
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                "INSERT INTO feedback (verdict_id, rating, corrected_label,"
                " note, created_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(verdict_id) DO UPDATE SET rating = excluded.rating,"
                " corrected_label = excluded.corrected_label,"
                " note = excluded.note, created_at = excluded.created_at",
                (verdict_id, rating, corrected_label, note, utc_now_iso()),
            )
            conn.commit()
            return True

        return bool(await self._run(_write))

    async def count_checks_today(self) -> int:
        """Verification attempts completed today (seed for DayCounter)."""

        def _read() -> int:
            conn = self._require_conn()
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM claims WHERE outcome IN"
                " ('verified', 'verify_failed')"
                " AND substr(completed_at, 1, 10) = ?",
                (_utc_today(),),
            ).fetchone()
            return int(row["n"])

        return int(await self._run(_read))

    async def fetch_summary(self, cost_per_verify_usd: float) -> dict[str, Any]:
        """Totals + today blocks for GET /stats/summary."""

        def _read() -> dict[str, Any]:
            conn = self._require_conn()
            today = _utc_today()

            def block(day: str | None) -> dict[str, Any]:
                claim_filter = (
                    "" if day is None else " WHERE substr(gated_at, 1, 10) = ?"
                )
                claim_args = () if day is None else (day,)
                verdict_filter = (
                    "" if day is None else " WHERE substr(checked_at, 1, 10) = ?"
                )
                session_filter = (
                    "" if day is None else " WHERE substr(started_at, 1, 10) = ?"
                )
                sessions = conn.execute(
                    f"SELECT COUNT(*) AS n FROM sessions{session_filter}",
                    claim_args,
                ).fetchone()["n"]
                claims = conn.execute(
                    f"SELECT COUNT(*) AS n FROM claims{claim_filter}", claim_args
                ).fetchone()["n"]
                funnel = {
                    row["outcome"]: row["n"]
                    for row in conn.execute(
                        f"SELECT outcome, COUNT(*) AS n FROM claims"
                        f"{claim_filter} GROUP BY outcome",
                        claim_args,
                    )
                }
                labels = {
                    row["label"]: row["n"]
                    for row in conn.execute(
                        f"SELECT label, COUNT(*) AS n FROM verdicts"
                        f"{verdict_filter} GROUP BY label",
                        claim_args,
                    )
                }
                verify_calls = funnel.get("verified", 0) + funnel.get(
                    "verify_failed", 0
                )
                return {
                    "sessions": sessions,
                    "claims": claims,
                    "verify_calls": verify_calls,
                    "est_cost_usd": round(verify_calls * cost_per_verify_usd, 4),
                    "labels": labels,
                    "funnel": funnel,
                }

            # Per-model strict-vs-fallback split, persisted, survives restarts.
            # This is the number that would have exposed the production
            # fallback storm (70/70 verdicts on one model via the text chain).
            verify_modes = [
                {
                    "model": row["model"],
                    "n": row["n"],
                    "fallback_n": row["fallback_n"],
                    "fallback_rate": (
                        round(row["fallback_n"] / row["n"], 3) if row["n"] else 0.0
                    ),
                }
                for row in conn.execute(
                    "SELECT model, COUNT(*) AS n, COALESCE(SUM(used_fallback), 0)"
                    " AS fallback_n FROM verdicts GROUP BY model ORDER BY n DESC"
                )
            ]
            return {
                "totals": block(None),
                "today": block(today),
                "verify_modes": verify_modes,
                # Lifetime, like verify_modes: how many labelled verdicts rest
                # only on C/D-tier sources (measured, never enforced here).
                "source_tiers": source_tier_breakdown(load_labelled_verdicts(conn)),
            }

        return await self._run(_read)

    async def fetch_channels(self) -> list[dict[str, Any]]:
        """Per-(platform, channel) aggregates for GET /stats/channels.

        Returns the adjudicated sample size ``n`` so the UI can apply its
        n>=30 floor; UNVERIFIED share is reported separately as a coverage
        metric of the pipeline, never as a property of the channel.
        """

        def _read() -> list[dict[str, Any]]:
            conn = self._require_conn()
            channels: dict[tuple[str | None, str], dict[str, Any]] = {}
            for row in conn.execute(
                "SELECT platform, channel, COUNT(*) AS sessions,"
                " SUM(CASE WHEN ended_at IS NOT NULL THEN"
                " (julianday(ended_at) - julianday(started_at)) * 86400.0"
                " ELSE 0 END) AS watch_seconds,"
                " SUM(speech_seconds) AS speech_seconds,"
                " SUM(est_cost_usd) AS est_cost_usd"
                " FROM sessions WHERE channel IS NOT NULL"
                " GROUP BY platform, channel"
            ):
                channels[(row["platform"], row["channel"])] = {
                    "platform": row["platform"],
                    "channel": row["channel"],
                    "sessions": row["sessions"],
                    "watch_seconds": row["watch_seconds"] or 0.0,
                    "speech_seconds": row["speech_seconds"] or 0.0,
                    "est_cost_usd": round(row["est_cost_usd"] or 0.0, 4),
                    "claims": 0,
                    "labels": {},
                }
            for row in conn.execute(
                "SELECT s.platform AS platform, s.channel AS channel,"
                " COUNT(*) AS n FROM claims c JOIN sessions s"
                " ON c.session_id = s.id WHERE s.channel IS NOT NULL"
                " GROUP BY s.platform, s.channel"
            ):
                key = (row["platform"], row["channel"])
                if key in channels:
                    channels[key]["claims"] = row["n"]
            for row in conn.execute(
                "SELECT s.platform AS platform, s.channel AS channel,"
                " v.label AS label, COUNT(*) AS n FROM verdicts v"
                " JOIN sessions s ON v.session_id = s.id"
                " WHERE s.channel IS NOT NULL"
                " GROUP BY s.platform, s.channel, v.label"
            ):
                key = (row["platform"], row["channel"])
                if key in channels:
                    channels[key]["labels"][row["label"]] = row["n"]

            results: list[dict[str, Any]] = []
            for entry in channels.values():
                labels: dict[str, int] = entry.pop("labels")
                adjudicated_n = sum(
                    labels.get(label, 0) for label in ("TRUE", "FALSE", "MISLEADING")
                )
                total_verdicts = adjudicated_n + labels.get("UNVERIFIED", 0)
                watch_hours = entry["watch_seconds"] / 3600.0
                entry["claims_per_hour"] = (
                    round(entry["claims"] / watch_hours, 2) if watch_hours else None
                )
                entry["adjudicated"] = {
                    "n": adjudicated_n,
                    "false_pct": (
                        round(100.0 * labels.get("FALSE", 0) / adjudicated_n, 1)
                        if adjudicated_n
                        else None
                    ),
                    "misleading_pct": (
                        round(100.0 * labels.get("MISLEADING", 0) / adjudicated_n, 1)
                        if adjudicated_n
                        else None
                    ),
                }
                entry["unverified_share"] = (
                    round(100.0 * labels.get("UNVERIFIED", 0) / total_verdicts, 1)
                    if total_verdicts
                    else None
                )
                results.append(entry)
            results.sort(key=lambda e: e["watch_seconds"], reverse=True)
            return results

        return await self._run(_read)

    async def fetch_sessions(self, limit: int) -> list[dict[str, Any]]:
        def _read() -> list[dict[str, Any]]:
            conn = self._require_conn()
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?",
                    (limit,),
                )
            ]

        return await self._run(_read)

    async def fetch_session_detail(self, session_id: str) -> dict[str, Any] | None:
        def _read() -> dict[str, Any] | None:
            conn = self._require_conn()
            session = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if session is None:
                return None
            claims = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM claims WHERE session_id = ?" " ORDER BY gated_at",
                    (session_id,),
                )
            ]
            verdicts = []
            for row in conn.execute(
                "SELECT * FROM verdicts WHERE session_id = ? ORDER BY checked_at",
                (session_id,),
            ):
                verdict = dict(row)
                verdict["sources"] = [
                    dict(source)
                    for source in conn.execute(
                        "SELECT rank, url, domain, title FROM sources"
                        " WHERE verdict_id = ? ORDER BY rank",
                        (row["id"],),
                    )
                ]
                feedback = conn.execute(
                    "SELECT rating, corrected_label, note, created_at"
                    " FROM feedback WHERE verdict_id = ?",
                    (row["id"],),
                ).fetchone()
                verdict["feedback"] = dict(feedback) if feedback else None
                verdicts.append(verdict)
            return {"session": dict(session), "claims": claims, "verdicts": verdicts}

        return await self._run(_read)
