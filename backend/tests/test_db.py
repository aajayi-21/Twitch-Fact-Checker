"""Database unit tests: schema, upserts, fire-and-forget doctrine, DayCounter."""

import logging
import sqlite3
from pathlib import Path

import pytest

from app import db as db_module
from app.db import Database, DayCounter
from app.models import GateClaim, Source, Verdict


def make_claim(text: str = "The Eiffel Tower is 330 meters tall.") -> GateClaim:
    return GateClaim(claim_text=text, check_worthiness=0.9, topic="history")


def make_verdict(**overrides: object) -> Verdict:
    base: dict = {
        "claim": "The Eiffel Tower is 330 meters tall.",
        "topic": "history",
        "label": "TRUE",
        "explanation": "Official height including antennas.",
        "sources": [
            Source(url="https://www.toureiffel.paris/facts", title="Key figures"),
            Source(url="https://en.wikipedia.org/wiki/Eiffel_Tower", title=None),
        ],
    }
    base.update(overrides)
    return Verdict(**base)


@pytest.fixture()
async def database(tmp_path: Path):
    db = Database(str(tmp_path / "test.db"))
    await db.open()
    yield db
    await db.close()


def read_rows(path: str, query: str) -> list[tuple]:
    with sqlite3.connect(path) as conn:
        return conn.execute(query).fetchall()


class TestLifecycle:
    async def test_open_is_idempotent_across_restarts(self, tmp_path: Path) -> None:
        path = str(tmp_path / "test.db")
        first = Database(path)
        await first.open()
        await first.record_session_start(
            session_id="s1", platform="twitch", channel="chan", title="t"
        )
        await first.close()
        second = Database(path)
        await second.open()  # existing schema: must not fail or wipe data
        assert await second.count_checks_today() == 0
        rows = read_rows(path, "SELECT id FROM sessions")
        assert rows == [("s1",)]
        await second.close()


class TestRecordClaim:
    async def test_upsert_preserves_gated_at_and_stamps_completed_at(
        self, database: Database, tmp_path: Path
    ) -> None:
        claim = make_claim()
        await database.record_session_start(
            session_id="s1", platform=None, channel=None, title=None
        )
        await database.record_claim(claim=claim, session_id="s1", outcome="pending")
        first = read_rows(
            database._path.as_posix(),
            "SELECT gated_at, outcome, completed_at FROM claims",
        )[0]
        assert first[1] == "pending" and first[2] is None
        await database.record_claim(claim=claim, session_id="s1", outcome="verified")
        second = read_rows(
            database._path.as_posix(),
            "SELECT gated_at, outcome, completed_at FROM claims",
        )[0]
        assert second[1] == "verified"
        assert second[2] is not None
        assert second[0] == first[0]  # gated_at survives the upsert

    async def test_terminal_outcome_without_prior_row_inserts_whole(
        self, database: Database
    ) -> None:
        """The flush-phase path: claims can skip the pending state."""
        await database.record_session_start(
            session_id="s1", platform=None, channel=None, title=None
        )
        await database.record_claim(
            claim=make_claim(), session_id="s1", outcome="verified"
        )
        rows = read_rows(
            database._path.as_posix(),
            "SELECT outcome, completed_at, normalized, has_visual_cue FROM claims",
        )
        assert rows[0][0] == "verified"
        assert rows[0][1] is not None
        assert rows[0][2] == "the eiffel tower is 330 meters tall"
        assert rows[0][3] == 0


class TestRecordVerdict:
    async def test_writes_verdict_and_ranked_sources_with_domains(
        self, database: Database
    ) -> None:
        claim = make_claim()
        verdict = make_verdict()
        await database.record_session_start(
            session_id="s1", platform=None, channel=None, title=None
        )
        await database.record_claim(claim=claim, session_id="s1", outcome="verified")
        await database.record_verdict(
            verdict=verdict,
            claim_id=claim.id,
            session_id="s1",
            latency_ms=1234,
            provider="gemini",
            model="fake-verify-model",
        )
        verdict_rows = read_rows(
            database._path.as_posix(),
            "SELECT id, claim_id, label, latency_ms, provider, model FROM verdicts",
        )
        assert verdict_rows == [
            (verdict.id, claim.id, "TRUE", 1234, "gemini", "fake-verify-model")
        ]
        source_rows = read_rows(
            database._path.as_posix(),
            "SELECT rank, url, domain, title FROM sources ORDER BY rank",
        )
        assert source_rows == [
            (
                0,
                "https://www.toureiffel.paris/facts",
                "www.toureiffel.paris",
                "Key figures",
            ),
            (1, "https://en.wikipedia.org/wiki/Eiffel_Tower", "en.wikipedia.org", None),
        ]


class TestRecordFeedback:
    async def test_unknown_verdict_returns_false(self, database: Database) -> None:
        assert await database.record_feedback("nope", "up", None, None) is False

    async def test_known_verdict_upserts(self, database: Database) -> None:
        claim = make_claim()
        verdict = make_verdict()
        await database.record_session_start(
            session_id="s1", platform=None, channel=None, title=None
        )
        await database.record_claim(claim=claim, session_id="s1", outcome="verified")
        await database.record_verdict(
            verdict=verdict,
            claim_id=claim.id,
            session_id="s1",
            latency_ms=1,
            provider="gemini",
            model="m",
        )
        assert await database.record_feedback(verdict.id, "up", None, None) is True
        assert (
            await database.record_feedback(verdict.id, "down", "FALSE", "wrong") is True
        )
        rows = read_rows(
            database._path.as_posix(),
            "SELECT verdict_id, rating, corrected_label, note FROM feedback",
        )
        assert rows == [(verdict.id, "down", "FALSE", "wrong")]  # replaced, not doubled


class TestFireAndForgetDoctrine:
    async def test_write_after_close_logs_and_never_raises(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        db = Database(str(tmp_path / "doomed.db"))
        await db.open()
        await db.close()
        with caplog.at_level(logging.WARNING, logger="app.db"):
            await db.record_claim(
                claim=make_claim(), session_id="s1", outcome="pending"
            )
        assert any("db write failed" in r.getMessage() for r in caplog.records)


class TestCountsAndCounter:
    async def test_count_checks_today_counts_only_todays_terminal_claims(
        self, database: Database
    ) -> None:
        await database.record_session_start(
            session_id="s1", platform=None, channel=None, title=None
        )
        for text, outcome in (
            ("Claim one is about apples.", "verified"),
            ("Claim two is about bridges.", "verify_failed"),
            ("Claim three is about comets.", "pending"),
        ):
            await database.record_claim(
                claim=make_claim(text), session_id="s1", outcome=outcome
            )

        # Backdate one terminal claim to yesterday-ish.
        def _backdate() -> None:
            conn = database._require_conn()
            conn.execute(
                "UPDATE claims SET completed_at = '2001-01-01T00:00:00Z'"
                " WHERE outcome = 'verify_failed'"
            )
            conn.commit()

        await database._run(_backdate)
        assert await database.count_checks_today() == 1

    def test_day_counter_rolls_over_on_new_utc_day(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(db_module, "_utc_today", lambda: "2026-08-09")
        counter = DayCounter(initial=5)
        counter.increment()
        assert counter.value == 6
        monkeypatch.setattr(db_module, "_utc_today", lambda: "2026-08-10")
        assert counter.value == 0
        counter.increment()
        assert counter.value == 1
