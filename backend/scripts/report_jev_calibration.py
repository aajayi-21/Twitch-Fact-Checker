"""Should Jev screen gate batches, and at what threshold? (offline, free)

Reads the ``gate_passes`` rows that ``JEV_MODE=shadow`` records — Jev's
probability for every gate batch NEXT TO what the claim extractor actually
found in that batch — and prints what screen mode would have done at each
threshold: how many extraction calls it would save, and which claims and
verdicts it would have thrown away. Read-only; makes no network calls.

Only shadow passes are an unbiased sample (extraction ran on every batch).
Screen-mode passes are summarized separately — route mix, how often Jev
failed open, Jev latency — because a skipped batch never shows what it held.

    cd backend
    uv run python scripts/report_jev_calibration.py
    uv run python scripts/report_jev_calibration.py --thresholds 0.2 0.35 0.5
    uv run python scripts/report_jev_calibration.py --jev-model typesafe/jev-1.13
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
    DEFAULT_JEV_THRESHOLDS,
    jev_screen_summary,
    jev_threshold_table,
    load_shadow_passes,
)

#: Below this many shadow passes the table is anecdote, not calibration.
MIN_PASSES_FOR_CONFIDENCE = 200


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", default=str(BACKEND_DIR / "fact_checker.db"))
    parser.add_argument(
        "--thresholds", type=float, nargs="+", default=list(DEFAULT_JEV_THRESHOLDS)
    )
    parser.add_argument(
        "--jev-model", default=None, help="only passes from this configured release"
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    path = Path(args.db)
    if not path.exists():
        print(f"no database at {path}", file=sys.stderr)
        return 2
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "gate_passes" not in tables:
            print(
                "no gate_passes table yet: start the backend once (it migrates the "
                "database), then run some streams with JEV_MODE=shadow",
                file=sys.stderr,
            )
            return 2
        passes = load_shadow_passes(conn, args.jev_model)
        screen = jev_screen_summary(conn)
    table = jev_threshold_table(passes, sorted(args.thresholds))

    if args.json:
        print(json.dumps({"shadow": table, "screen": screen}, indent=2))
        return 0

    releases = sorted({p.resolved_model or "?" for p in passes})
    with_claims = sum(1 for p in passes if p.claims_count)
    print(
        f"{len(passes)} shadow pass(es), {with_claims} with claims; "
        f"releases: {', '.join(releases) or '-'}"
    )
    if len(passes) < MIN_PASSES_FOR_CONFIDENCE:
        print(
            f"WARNING: fewer than {MIN_PASSES_FOR_CONFIDENCE} shadow passes — "
            "treat these numbers as anecdotal"
        )
    if passes:
        print(
            f"\n{'thresh':>6} {'skip%':>6} {'saved':>6} {'recall':>7} "
            f"{'claims-':>7} {'verif-':>6}  lost verdicts (FALSE/MISLEADING first)"
        )
        for row in table:
            labels = row["labels_lost"]
            ordered = [
                f"{label}:{labels[label]}"
                for label in ("FALSE", "MISLEADING", "TRUE", "UNVERIFIED")
                if labels.get(label)
            ]
            recall = (
                "-" if row["batch_recall"] is None else f"{row['batch_recall']:.3f}"
            )
            print(
                f"{row['threshold']:>6.2f} {row['skip_rate'] * 100:>5.1f}% "
                f"{row['skipped']:>6} {recall:>7} {row['claims_lost']:>7} "
                f"{row['verified_lost']:>6}  {' '.join(ordered) or '-'}"
            )
        print(
            "\nsaved = extraction calls skipped; recall = share of claim-bearing "
            "batches still extracted;\nclaims-/verif- = claims (all / verified) "
            "that screen mode would have discarded."
        )

    print(f"\nscreen-mode passes (biased, operational only): {screen['passes']}")
    if screen["passes"]:
        print(
            f"  routes {screen['routes']}  jev errors {screen['jev_errors']} "
            f"({screen['jev_error_rate']:.1%})  latency p50 "
            f"{screen['jev_latency_ms_p50']}ms p95 {screen['jev_latency_ms_p95']}ms"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
