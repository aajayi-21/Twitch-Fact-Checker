"""POST /feedback: 204/404/422 contract + upsert-on-repeat."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.models import utc_now_iso


def seed_verdict(db_path: str, verdict_id: str = "v-1") -> None:
    """Seed session/claim/verdict rows via a fresh test-thread connection.

    The schema exists once the client's (fake) lifespan has entered; WAL
    mode permits this second connection alongside the app's db executor.
    """
    now = utc_now_iso()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO sessions (id, started_at) VALUES ('s-1', ?)", (now,)
        )
        conn.execute(
            "INSERT INTO claims (id, session_id, text, normalized, topic,"
            " check_worthiness, gated_at, outcome) VALUES"
            " ('c-1', 's-1', 'x', 'x', 'other', 0.9, ?, 'verified')",
            (now,),
        )
        conn.execute(
            "INSERT INTO verdicts (id, claim_id, session_id, label,"
            " explanation, checked_at) VALUES (?, 'c-1', 's-1', 'TRUE', 'ok', ?)",
            (verdict_id, now),
        )
        conn.commit()


def read_feedback(db_path: str) -> list[tuple]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT verdict_id, rating, corrected_label, note FROM feedback"
        ).fetchall()


class TestFeedbackEndpoint:
    def test_happy_path_is_204_and_persists(
        self, client: TestClient, app_settings: Settings
    ) -> None:
        seed_verdict(app_settings.db_path)
        response = client.post(
            "/feedback", json={"verdict_id": "v-1", "rating": "up"}
        )
        assert response.status_code == 204
        assert read_feedback(app_settings.db_path) == [("v-1", "up", None, None)]

    def test_unknown_verdict_is_404(self, client: TestClient) -> None:
        response = client.post(
            "/feedback", json={"verdict_id": "ghost", "rating": "up"}
        )
        assert response.status_code == 404
        assert "unknown verdict_id" in response.json()["detail"]

    def test_bad_rating_is_422(
        self, client: TestClient, app_settings: Settings
    ) -> None:
        seed_verdict(app_settings.db_path)
        response = client.post(
            "/feedback", json={"verdict_id": "v-1", "rating": "meh"}
        )
        assert response.status_code == 422

    def test_repeat_feedback_replaces(
        self, client: TestClient, app_settings: Settings
    ) -> None:
        seed_verdict(app_settings.db_path)
        assert (
            client.post(
                "/feedback", json={"verdict_id": "v-1", "rating": "up"}
            ).status_code
            == 204
        )
        response = client.post(
            "/feedback",
            json={
                "verdict_id": "v-1",
                "rating": "down",
                "corrected_label": "MISLEADING",
                "note": "missing context",
            },
        )
        assert response.status_code == 204
        assert read_feedback(app_settings.db_path) == [
            ("v-1", "down", "MISLEADING", "missing context")
        ]
