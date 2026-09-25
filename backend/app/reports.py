"""Read-only analytics over the SQLite database — pure, synchronous, testable.

Everything here takes a plain ``sqlite3.Connection`` (row factory agnostic:
columns are read by position), so the same functions back the dashboard's
``/stats/summary`` block (run on the database executor thread) and the
offline ``scripts/report_*.py`` tools (a read-only connection of their own).

Source tiers are computed at READ time from ``sources.url`` with
:mod:`app.source_quality`, never stored: results then always reflect the
current tier list (extend the list, re-run, see the effect), and no schema
change or backfill is needed. The stored ``sources.domain`` column is not
used because it keeps ``www.``.
"""

import sqlite3
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.models import Source
from app.source_quality import (
    SourceSummary,
    Tier,
    is_recognized_domain,
    registrable_domain,
    summarize_sources,
    tier_for_domain,
)

#: Verdict labels that assert something (UNVERIFIED asserts nothing).
LABELLED: tuple[str, ...] = ("TRUE", "FALSE", "MISLEADING")
TIERS: tuple[Tier, ...] = ("A", "B", "C", "D")
#: The measured rule, identical to the chat bot's ``low_source_quality``
#: posting bar: a labelled verdict needs at least one A/B source.
TRUSTED_TIERS: frozenset[str] = frozenset({"A", "B"})


@dataclass(frozen=True)
class LabelledVerdict:
    """One TRUE/FALSE/MISLEADING verdict with its claim and cited URLs."""

    verdict_id: str
    label: str
    claim: str
    topic: str
    urls: tuple[str, ...]

    def tier_summary(self) -> SourceSummary | None:
        """Best/worst tier of the citations, or ``None`` with no citations."""
        if not self.urls:
            return None
        return summarize_sources([Source(url=url) for url in self.urls])

    def would_downgrade(self) -> bool:
        """True when the A/B source rule would demote this verdict."""
        summary = self.tier_summary()
        return summary is None or summary.best_tier not in TRUSTED_TIERS


def load_labelled_verdicts(conn: sqlite3.Connection) -> list[LabelledVerdict]:
    """All labelled verdicts, oldest first, with sources in citation order."""
    placeholders = ", ".join("?" for _ in LABELLED)
    rows = conn.execute(
        "SELECT v.id, v.label, COALESCE(c.text, ''), COALESCE(c.topic, ''), s.url"
        " FROM verdicts v"
        " LEFT JOIN claims c ON c.id = v.claim_id"
        " LEFT JOIN sources s ON s.verdict_id = v.id"
        f" WHERE v.label IN ({placeholders})"
        " ORDER BY v.checked_at, v.id, s.rank",
        LABELLED,
    ).fetchall()
    verdicts: dict[str, dict[str, Any]] = {}
    for verdict_id, label, claim, topic, url in rows:
        entry = verdicts.setdefault(
            verdict_id,
            {"label": label, "claim": claim, "topic": topic, "urls": []},
        )
        if url is not None:
            entry["urls"].append(url)
    return [
        LabelledVerdict(
            verdict_id=verdict_id,
            label=entry["label"],
            claim=entry["claim"],
            topic=entry["topic"],
            urls=tuple(entry["urls"]),
        )
        for verdict_id, entry in verdicts.items()
    ]


def source_tier_breakdown(verdicts: Sequence[LabelledVerdict]) -> dict[str, Any]:
    """Best-tier distribution of labelled verdicts and the A/B-rule impact.

    Returns ``labelled`` (N), ``best_tier`` counts over verdicts WITH
    sources, ``no_sources``, ``with_denylisted`` (a D-tier citation present),
    ``by_label`` (best tier per label, ``none`` = no sources), and
    ``would_downgrade`` / ``would_downgrade_rate``: how many labelled
    verdicts an "at least one A/B source" rule would turn into UNVERIFIED.
    """
    best_tier: Counter[str] = Counter({tier: 0 for tier in TIERS})
    by_label: dict[str, Counter[str]] = {
        label: Counter({tier: 0 for tier in (*TIERS, "none")}) for label in LABELLED
    }
    no_sources = with_denylisted = would_downgrade = 0
    for verdict in verdicts:
        summary = verdict.tier_summary()
        label_counts = by_label.setdefault(verdict.label, Counter())
        if summary is None:
            no_sources += 1
            would_downgrade += 1
            label_counts["none"] += 1
            continue
        best_tier[summary.best_tier] += 1
        label_counts[summary.best_tier] += 1
        with_denylisted += int(summary.has_denylisted)
        would_downgrade += int(summary.best_tier not in TRUSTED_TIERS)
    labelled = len(verdicts)
    return {
        "labelled": labelled,
        "best_tier": dict(best_tier),
        "no_sources": no_sources,
        "with_denylisted": with_denylisted,
        "by_label": {label: dict(counts) for label, counts in by_label.items()},
        "would_downgrade": would_downgrade,
        "would_downgrade_rate": (
            round(would_downgrade / labelled, 3) if labelled else 0.0
        ),
    }


def unrecognized_domains(
    verdicts: Sequence[LabelledVerdict], limit: int | None = 25
) -> list[dict[str, Any]]:
    """Cited domains the tier list does not know (C only by default).

    Per domain: ``citations`` (source rows), ``verdicts`` (labelled verdicts
    citing it), and ``rescues`` — verdicts the A/B rule would demote that
    promoting this one domain to B would keep. Sorted by rescues, then
    verdicts: the top of this list is where extending the tier list pays.
    """
    citations: Counter[str] = Counter()
    citing: defaultdict[str, set[str]] = defaultdict(set)
    rescues: defaultdict[str, set[str]] = defaultdict(set)
    for verdict in verdicts:
        demoted = verdict.would_downgrade()
        for url in verdict.urls:
            domain = registrable_domain(url)
            if domain is None or is_recognized_domain(domain):
                continue
            citations[domain] += 1
            citing[domain].add(verdict.verdict_id)
            if demoted:
                rescues[domain].add(verdict.verdict_id)
    ranked = sorted(
        citations,
        key=lambda domain: (-len(rescues[domain]), -len(citing[domain]), domain),
    )
    if limit is not None:
        ranked = ranked[:limit]
    return [
        {
            "domain": domain,
            "citations": citations[domain],
            "verdicts": len(citing[domain]),
            "rescues": len(rescues[domain]),
        }
        for domain in ranked
    ]


def would_downgrade_list(
    verdicts: Sequence[LabelledVerdict],
) -> list[dict[str, Any]]:
    """The verdicts an A/B source rule would demote, for manual review."""
    rows: list[dict[str, Any]] = []
    for verdict in verdicts:
        if not verdict.would_downgrade():
            continue
        domains: list[tuple[str, str]] = []
        for url in verdict.urls:
            domain = registrable_domain(url)
            entry = (domain or url, tier_for_domain(domain))
            if entry not in domains:
                domains.append(entry)
        rows.append(
            {
                "verdict_id": verdict.verdict_id,
                "label": verdict.label,
                "topic": verdict.topic,
                "claim": verdict.claim,
                "domains": domains,
            }
        )
    return rows
