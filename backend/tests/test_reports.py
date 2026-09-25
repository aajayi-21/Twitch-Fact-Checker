"""app.reports: offline analytics over the SQLite schema (no event loop)."""

import sqlite3
from collections.abc import Iterator

import pytest

from app.db import SCHEMA_SQL
from app.reports import (
    load_labelled_verdicts,
    source_tier_breakdown,
    unrecognized_domains,
    would_downgrade_list,
)

NOW = "2026-09-24T12:00:00Z"


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA_SQL)
    connection.execute("INSERT INTO sessions (id, started_at) VALUES ('s', ?)", (NOW,))
    yield connection
    connection.close()


def add_verdict(
    conn: sqlite3.Connection, verdict_id: str, label: str, urls: list[str]
) -> None:
    claim_id = f"c-{verdict_id}"
    conn.execute(
        "INSERT INTO claims (id, session_id, text, normalized, topic,"
        " check_worthiness, gated_at, outcome) VALUES (?, 's', ?, ?, 'politics',"
        " 0.9, ?, 'verified')",
        (claim_id, f"claim {verdict_id}", f"claim {verdict_id}", NOW),
    )
    conn.execute(
        "INSERT INTO verdicts (id, claim_id, session_id, label, explanation,"
        " checked_at) VALUES (?, ?, 's', ?, 'because', ?)",
        (verdict_id, claim_id, label, NOW),
    )
    conn.executemany(
        "INSERT INTO sources (verdict_id, rank, url) VALUES (?, ?, ?)",
        [(verdict_id, rank, url) for rank, url in enumerate(urls)],
    )


@pytest.fixture()
def seeded(conn: sqlite3.Connection) -> sqlite3.Connection:
    add_verdict(conn, "a", "TRUE", ["https://www.nasa.gov/x", "https://dw.com/y"])
    add_verdict(conn, "b", "FALSE", ["https://www.reuters.com/z"])
    add_verdict(conn, "c", "MISLEADING", ["https://dw.com/1", "https://x.org/2"])
    add_verdict(conn, "d", "TRUE", ["https://reddit.com/r/q"])
    add_verdict(conn, "e", "TRUE", [])
    add_verdict(conn, "u", "UNVERIFIED", ["https://dw.com/3"])
    return conn


class TestLoad:
    def test_only_labelled_verdicts_with_ordered_sources(
        self, seeded: sqlite3.Connection
    ) -> None:
        verdicts = {v.verdict_id: v for v in load_labelled_verdicts(seeded)}
        assert set(verdicts) == {"a", "b", "c", "d", "e"}
        assert verdicts["a"].urls == ("https://www.nasa.gov/x", "https://dw.com/y")
        assert verdicts["e"].urls == ()
        assert verdicts["a"].claim == "claim a"

    def test_works_with_row_factory(self, seeded: sqlite3.Connection) -> None:
        seeded.row_factory = sqlite3.Row
        assert len(load_labelled_verdicts(seeded)) == 5


class TestBreakdown:
    def test_best_tier_and_rule_impact(self, seeded: sqlite3.Connection) -> None:
        breakdown = source_tier_breakdown(load_labelled_verdicts(seeded))
        assert breakdown["labelled"] == 5
        # a -> A (nasa), b -> B (reuters, www. stripped), c -> C, d -> D.
        assert breakdown["best_tier"] == {"A": 1, "B": 1, "C": 1, "D": 1}
        assert breakdown["no_sources"] == 1
        assert breakdown["with_denylisted"] == 1
        # c (C only), d (D only), e (nothing).
        assert breakdown["would_downgrade"] == 3
        assert breakdown["would_downgrade_rate"] == 0.6
        assert breakdown["by_label"]["TRUE"] == {
            "A": 1,
            "B": 0,
            "C": 0,
            "D": 1,
            "none": 1,
        }

    def test_empty_database(self, conn: sqlite3.Connection) -> None:
        breakdown = source_tier_breakdown(load_labelled_verdicts(conn))
        assert breakdown["labelled"] == 0
        assert breakdown["would_downgrade_rate"] == 0.0


class TestUnrecognizedDomains:
    def test_ranked_by_rescues_then_verdicts(self, seeded: sqlite3.Connection) -> None:
        rows = unrecognized_domains(load_labelled_verdicts(seeded))
        by_domain = {row["domain"]: row for row in rows}
        # Listed domains (nasa, reuters, reddit) never appear.
        assert set(by_domain) == {"dw.com", "x.org"}
        # dw.com: cited by a (kept anyway) and c (demoted) -> 1 rescue.
        assert by_domain["dw.com"] == {
            "domain": "dw.com",
            "citations": 2,
            "verdicts": 2,
            "rescues": 1,
        }
        assert rows[0]["domain"] == "dw.com"

    def test_limit(self, seeded: sqlite3.Connection) -> None:
        assert len(unrecognized_domains(load_labelled_verdicts(seeded), limit=1)) == 1


class TestWouldDowngradeList:
    def test_lists_domains_with_tiers(self, seeded: sqlite3.Connection) -> None:
        rows = {
            r["verdict_id"]: r
            for r in would_downgrade_list(load_labelled_verdicts(seeded))
        }
        assert set(rows) == {"c", "d", "e"}
        assert rows["c"]["domains"] == [("dw.com", "C"), ("x.org", "C")]
        assert rows["d"]["domains"] == [("reddit.com", "D")]
        assert rows["e"]["domains"] == []


def add_gate_pass(
    conn: sqlite3.Connection,
    pass_id: str,
    probability: float | None,
    *,
    mode: str = "shadow",
    claims: int = 0,
    route: str | None = None,
    error: str | None = None,
    jev_error: str | None = None,
    latency_ms: int | None = 200,
) -> None:
    conn.execute(
        "INSERT INTO gate_passes (id, session_id, started_at, phase, word_count,"
        " claims_count, error, gate_provider, gate_model, jev_mode, jev_model,"
        " jev_resolved_model, jev_probability, jev_threshold, jev_route,"
        " jev_latency_ms, jev_error) VALUES (?, 's', ?, 'live', 10, ?, ?,"
        " 'openrouter', 'm', ?, 'typesafe/jev-1.13', 'typesafe/jev-1.13-x', ?,"
        " 0.35, ?, ?, ?)",
        (
            pass_id,
            NOW,
            claims,
            error,
            mode,
            probability,
            route or mode,
            latency_ms,
            jev_error,
        ),
    )


def link_claim(
    conn: sqlite3.Connection, claim_id: str, pass_id: str, label: str | None
) -> None:
    conn.execute(
        "INSERT INTO claims (id, session_id, text, normalized, topic,"
        " check_worthiness, gated_at, outcome, gate_pass_id) VALUES"
        " (?, 's', 't', 't', 'other', 0.9, ?, ?, ?)",
        (claim_id, NOW, "verified" if label else "below_threshold", pass_id),
    )
    if label:
        conn.execute(
            "INSERT INTO verdicts (id, claim_id, session_id, label, explanation,"
            " checked_at) VALUES (?, ?, 's', ?, 'x', ?)",
            (f"v-{claim_id}", claim_id, label, NOW),
        )


class TestJevCalibration:
    @pytest.fixture()
    def shadow(self, conn: sqlite3.Connection) -> sqlite3.Connection:
        from app.db import Database

        Database._migrate(_row_conn(conn))
        add_gate_pass(conn, "p-low-empty", 0.05)
        add_gate_pass(conn, "p-low-claim", 0.2, claims=2)
        link_claim(conn, "c1", "p-low-claim", "FALSE")
        link_claim(conn, "c2", "p-low-claim", None)
        add_gate_pass(conn, "p-high", 0.9, claims=1)
        link_claim(conn, "c3", "p-high", "TRUE")
        # Excluded: extraction failed, Jev failed, and screen mode (biased).
        add_gate_pass(conn, "p-err", 0.9, error="gate call failed")
        add_gate_pass(conn, "p-jev-err", None, jev_error="HTTP 503")
        add_gate_pass(conn, "p-screen", 0.1, mode="screen", route="skip")
        return conn

    def test_only_complete_shadow_passes_are_loaded(
        self, shadow: sqlite3.Connection
    ) -> None:
        from app.reports import load_shadow_passes

        passes = {p.pass_id: p for p in load_shadow_passes(shadow)}
        assert set(passes) == {"p-low-empty", "p-low-claim", "p-high"}
        assert passes["p-low-claim"].claims_count == 2
        assert passes["p-low-claim"].verified_claims == 1
        assert passes["p-low-claim"].labels == ("FALSE",)
        assert load_shadow_passes(shadow, jev_model="typesafe/jev-9.9") == []

    def test_threshold_table(self, shadow: sqlite3.Connection) -> None:
        from app.reports import jev_threshold_table, load_shadow_passes

        rows = {
            row["threshold"]: row
            for row in jev_threshold_table(load_shadow_passes(shadow), [0.1, 0.35])
        }
        assert rows[0.1]["skipped"] == 1
        assert rows[0.1]["claims_lost"] == 0
        assert rows[0.1]["batch_recall"] == 1.0
        low = rows[0.35]
        assert (low["skipped"], low["skip_rate"]) == (2, 0.667)
        assert low["skipped_with_claims"] == 1
        assert low["batch_recall"] == 0.5
        assert (low["claims_lost"], low["verified_lost"]) == (2, 1)
        assert low["labels_lost"] == {"FALSE": 1}

    def test_screen_summary(self, shadow: sqlite3.Connection) -> None:
        from app.reports import jev_screen_summary

        summary = jev_screen_summary(shadow)
        assert summary["passes"] == 1
        assert summary["routes"] == {"skip": 1}
        assert summary["jev_errors"] == 0
        assert summary["jev_latency_ms_p50"] == 200


def _row_conn(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    return conn
