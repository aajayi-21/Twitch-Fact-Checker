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
