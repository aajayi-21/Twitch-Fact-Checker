"""How much do labelled verdicts rely on low-tier sources? (offline, free)

Measures the source-tier rule the chat bot already posts under — "a
TRUE/FALSE/MISLEADING verdict needs at least one A/B-tier source" — against
the verdicts in the analytics database, WITHOUT enforcing it anywhere. Reads
the database read-only and makes no network calls.

Prints three things:

1. label x best-tier table (and how many verdicts the rule would downgrade);
2. the most-cited domains the tier list does not recognize (they are C only
   by default) — ranked by how many downgrades promoting each to B would
   avoid. This is where extending ``app/source_quality.py`` pays off;
3. every verdict the rule would downgrade, with its domains and tiers, for
   manual review.

    cd backend
    uv run python scripts/report_source_tiers.py
    uv run python scripts/report_source_tiers.py --db other.db --top 40 --json
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.reports import (  # noqa: E402
    LABELLED,
    TIERS,
    load_labelled_verdicts,
    source_tier_breakdown,
    unrecognized_domains,
    would_downgrade_list,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", default=str(BACKEND_DIR / "fact_checker.db"))
    parser.add_argument(
        "--top", type=int, default=25, help="unrecognized domains to list"
    )
    parser.add_argument(
        "--json", action="store_true", help="print one JSON document instead"
    )
    args = parser.parse_args()

    path = Path(args.db)
    if not path.exists():
        print(f"no database at {path}", file=sys.stderr)
        return 2
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        verdicts = load_labelled_verdicts(conn)
    breakdown = source_tier_breakdown(verdicts)
    domains = unrecognized_domains(verdicts, limit=args.top)
    downgrades = would_downgrade_list(verdicts)

    if args.json:
        print(
            json.dumps(
                {
                    "breakdown": breakdown,
                    "unrecognized_domains": domains,
                    "would_downgrade": downgrades,
                },
                indent=2,
            )
        )
        return 0

    labelled = breakdown["labelled"]
    print(f"{labelled} labelled verdict(s) in {path.name}\n")
    if not labelled:
        return 0

    header = f"{'label':<11}" + "".join(f"{tier:>6}" for tier in (*TIERS, "none"))
    print("best source tier by label")
    print(header)
    for label in LABELLED:
        counts = breakdown["by_label"].get(label, {})
        print(
            f"{label:<11}"
            + "".join(f"{counts.get(tier, 0):>6}" for tier in (*TIERS, "none"))
        )
    print(
        f"\nA/B-source rule would downgrade {breakdown['would_downgrade']} of "
        f"{labelled} ({breakdown['would_downgrade_rate']:.1%}); "
        f"{breakdown['with_denylisted']} cite a D-tier (user-generated) source."
    )

    print(f"\ntop unrecognized domains (C by default), max {args.top}")
    print(f"{'domain':<40}{'rescues':>8}{'verdicts':>9}{'cites':>7}")
    for row in domains:
        print(
            f"{row['domain']:<40}{row['rescues']:>8}{row['verdicts']:>9}"
            f"{row['citations']:>7}"
        )

    print(f"\nverdicts the rule would downgrade ({len(downgrades)})")
    for row in downgrades:
        domains_text = ", ".join(f"{domain}[{tier}]" for domain, tier in row["domains"])
        print(f"- {row['label']:<10} {row['claim']}")
        print(f"    {domains_text or 'no sources'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
