"""GET /stats/* + /dashboard: aggregate math and honest-metrics rules."""

import sqlite3
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.db import SCHEMA_SQL
from app.models import utc_now_iso
from tests.conftest import (
    FakeGenAIClient,
    FakeTranscriber,
    make_test_settings,
    open_test_client,
)

OLD_DAY = "2001-01-01T00:00:00Z"


def seed(db_path: str) -> None:
    """Two channels + one channel-less session, claims across the funnel."""
    now = utc_now_iso()
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        # streamer_a: 2h session TODAY with claims/verdicts.
        conn.execute(
            "INSERT INTO sessions (id, platform, channel, title, started_at,"
            " ended_at, speech_seconds, gate_calls, verify_calls, est_cost_usd)"
            " VALUES ('s-a', 'twitch', 'streamer_a', 'Big Stream', ?, ?,"
            " 3600, 10, 4, 0.02)",
            (now.replace("Z", "") + "Z", now),
        )
        # Make it a 2-hour session WITHOUT crossing a UTC date line: anchor
        # both ends inside today. The old "-2 hours before now" version made
        # this test fail every day between 00:00 and 02:00 UTC, because the
        # start date fell on yesterday and the today[] block lost the session.
        conn.execute(
            "UPDATE sessions SET"
            " started_at = strftime('%Y-%m-%dT00:00:00Z', ended_at),"
            " ended_at = strftime('%Y-%m-%dT02:00:00Z', ended_at)"
            " WHERE id = 's-a'"
        )
        # streamer_b: OLD session, no verdicts.
        conn.execute(
            "INSERT INTO sessions (id, platform, channel, started_at, ended_at,"
            " speech_seconds) VALUES ('s-b', 'youtube', 'streamer_b', ?, ?, 60)",
            (OLD_DAY, OLD_DAY),
        )
        # Channel-less session: must be EXCLUDED from /stats/channels.
        conn.execute(
            "INSERT INTO sessions (id, started_at) VALUES ('s-x', ?)", (OLD_DAY,)
        )
        claims = [
            ("c-1", "verified", now, 1),
            ("c-2", "verified", now, 0),
            ("c-3", "verified", now, 0),
            ("c-4", "verify_failed", now, 0),
            ("c-5", "below_threshold", None, 0),
            ("c-6", "duplicate", None, 0),
            ("c-7", "pending", None, 0),
        ]
        for claim_id, outcome, completed_at, cue in claims:
            conn.execute(
                "INSERT INTO claims (id, session_id, text, normalized, topic,"
                " check_worthiness, gated_at, outcome, completed_at,"
                " has_visual_cue) VALUES (?, 's-a', 'text', 'text', 'other',"
                " 0.9, ?, ?, ?, ?)",
                (claim_id, now, outcome, completed_at, cue),
            )
        verdicts = [
            ("v-1", "c-1", "FALSE"),
            ("v-2", "c-2", "TRUE"),
            ("v-3", "c-3", "UNVERIFIED"),
        ]
        for verdict_id, claim_id, label in verdicts:
            conn.execute(
                "INSERT INTO verdicts (id, claim_id, session_id, label,"
                " explanation, checked_at) VALUES (?, ?, 's-a', ?, 'because', ?)",
                (verdict_id, claim_id, label, now),
            )
        conn.execute(
            "INSERT INTO feedback (verdict_id, rating, created_at)"
            " VALUES ('v-1', 'down', ?)",
            (now,),
        )
        conn.commit()


@pytest.fixture()
def seeded_client(
    tmp_path, fake_genai_client: FakeGenAIClient, fake_transcriber: FakeTranscriber
) -> Iterator[TestClient]:
    settings = make_test_settings(db_path=str(tmp_path / "stats.db"))
    seed(settings.db_path)
    with open_test_client(settings, fake_genai_client, fake_transcriber) as client:
        yield client


class TestSummary:
    def test_totals_and_today_blocks(self, seeded_client: TestClient) -> None:
        body = seeded_client.get("/stats/summary").json()
        totals = body["totals"]
        assert totals["sessions"] == 3
        assert totals["claims"] == 7
        assert totals["verify_calls"] == 4  # verified + verify_failed
        assert totals["est_cost_usd"] == pytest.approx(4 * 0.005)
        assert totals["labels"] == {"FALSE": 1, "TRUE": 1, "UNVERIFIED": 1}
        assert totals["funnel"] == {
            "verified": 3,
            "verify_failed": 1,
            "below_threshold": 1,
            "duplicate": 1,
            "pending": 1,
        }
        today = body["today"]
        assert today["sessions"] == 1  # the two OLD sessions fall out
        assert today["claims"] == 7  # all claims were gated today


class TestChannels:
    def test_rate_based_metrics_with_sample_sizes(
        self, seeded_client: TestClient
    ) -> None:
        channels = seeded_client.get("/stats/channels").json()
        assert [c["channel"] for c in channels] == ["streamer_a", "streamer_b"]
        streamer_a = channels[0]
        assert streamer_a["platform"] == "twitch"
        assert streamer_a["sessions"] == 1
        assert streamer_a["watch_seconds"] == pytest.approx(7200, abs=5)
        assert streamer_a["claims"] == 7
        assert streamer_a["claims_per_hour"] == pytest.approx(3.5, abs=0.1)
        # Adjudicated-only percentages, with n exposed for the UI's floor.
        assert streamer_a["adjudicated"]["n"] == 2  # TRUE + FALSE
        assert streamer_a["adjudicated"]["false_pct"] == 50.0
        # UNVERIFIED reported as coverage, never in the adjudicated rate.
        assert streamer_a["unverified_share"] == pytest.approx(33.3, abs=0.1)

    def test_channel_less_sessions_are_excluded(
        self, seeded_client: TestClient
    ) -> None:
        channels = seeded_client.get("/stats/channels").json()
        assert all(entry["channel"] is not None for entry in channels)

    def test_channel_without_verdicts_has_null_rates(
        self, seeded_client: TestClient
    ) -> None:
        streamer_b = seeded_client.get("/stats/channels").json()[1]
        assert streamer_b["adjudicated"] == {
            "n": 0,
            "false_pct": None,
            "misleading_pct": None,
        }
        assert streamer_b["unverified_share"] is None


class TestSessions:
    def test_list_is_newest_first_and_limited(self, seeded_client: TestClient) -> None:
        sessions = seeded_client.get("/stats/sessions?limit=2").json()
        assert len(sessions) == 2
        assert sessions[0]["id"] == "s-a"

    def test_detail_nests_claims_verdicts_sources_feedback(
        self, seeded_client: TestClient
    ) -> None:
        detail = seeded_client.get("/stats/sessions/s-a").json()
        assert detail["session"]["channel"] == "streamer_a"
        assert len(detail["claims"]) == 7
        assert len(detail["verdicts"]) == 3
        by_id = {v["id"]: v for v in detail["verdicts"]}
        assert by_id["v-1"]["feedback"]["rating"] == "down"
        assert by_id["v-2"]["feedback"] is None
        assert by_id["v-1"]["sources"] == []

    def test_unknown_session_is_404(self, seeded_client: TestClient) -> None:
        assert seeded_client.get("/stats/sessions/ghost").status_code == 404


class TestDashboard:
    def test_dashboard_serves_html(self, seeded_client: TestClient) -> None:
        response = seeded_client.get("/dashboard")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
