"""Replay historical claims through the configured verifier.

Answers "are verdicts any good?" with evidence rather than a sample of four:
reads claims from the analytics database (or a CSV), runs each through the
real fact checker of the chosen provider (``--provider anthropic`` or
``openrouter``; default: the configured verify provider) with the current
prompts, and prints per-claim rows plus label / evidence / fallback
histograms and latency percentiles.

This SPENDS credits (web search: ~$0.007 per claim on OpenRouter, ~$0.01 per
search on Anthropic, plus tokens), so it refuses to run without
``--yes-spend-credits`` and prints the estimated cost first.

    cd backend
    uv run python scripts/eval_verify.py --provider anthropic --limit 40 \\
        --yes-spend-credits --out /tmp/eval.jsonl
    jq -r '[.label,.evidence,.used_fallback,.claim]|@tsv' /tmp/eval.jsonl | sort
"""

import argparse
import asyncio
import csv
import json
import sqlite3
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import Settings  # noqa: E402
from app.llm_openrouter import verify_mode_snapshot  # noqa: E402
from app.llm_provider import create_fact_checker, create_llm_client  # noqa: E402
from app.logging_setup import configure_logging  # noqa: E402
from app.openrouter_catalogue import (  # noqa: E402
    lookup_model_capabilities,
    prime_openrouter_capabilities,
)
from app.rate_limit import QuotaCooldown  # noqa: E402

#: Verify providers this script can replay through -> their model setting.
VERIFY_MODEL_FIELDS = {
    "anthropic": "anthropic_verify_model",
    "openrouter": "openrouter_verify_model",
}


def load_claims_from_db(path: Path, limit: int | None) -> list[dict[str, str]]:
    """Distinct verified claims, newest first, with their previous label."""
    uri = f"file:{path}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT c.text AS claim, c.topic AS topic, v.label AS previous_label,"
            " v.model AS previous_model FROM claims c"
            " LEFT JOIN verdicts v ON v.claim_id = c.id"
            " WHERE c.outcome = 'verified' ORDER BY c.gated_at DESC"
        ).fetchall()
    seen: set[str] = set()
    claims: list[dict[str, str]] = []
    for row in rows:
        if row["claim"] in seen:
            continue
        seen.add(row["claim"])
        claims.append(dict(row))
        if limit is not None and len(claims) >= limit:
            break
    return claims


def load_claims_from_csv(path: Path, limit: int | None) -> list[dict[str, str]]:
    """``claim,topic`` rows (header optional)."""
    claims: list[dict[str, str]] = []
    with path.open(newline="") as handle:
        for row in csv.reader(handle):
            if not row or row[0].strip().lower() == "claim":
                continue
            claims.append(
                {
                    "claim": row[0].strip(),
                    "topic": (row[1].strip() if len(row) > 1 else "other"),
                }
            )
            if limit is not None and len(claims) >= limit:
                break
    return claims


async def run(args: argparse.Namespace) -> int:
    configured = Settings()
    provider = args.provider or configured.resolved_verify_provider
    if provider not in VERIFY_MODEL_FIELDS:
        print(
            f"verify provider must be one of {sorted(VERIFY_MODEL_FIELDS)}, "
            f"got {provider!r}",
            file=sys.stderr,
        )
        return 2
    model_field = VERIFY_MODEL_FIELDS[provider]
    model = args.model or getattr(configured, model_field)
    # Rebuilt (not model_copy) so validators run: the provider-aware cost
    # default follows the chosen provider.
    settings = Settings(verify_provider=provider, **{model_field: model})
    if not settings.provider_configured(provider):
        settings.require_llm_api_key()  # raises with the provider's own hint

    claims = (
        load_claims_from_csv(Path(args.csv), args.limit)
        if args.csv
        else load_claims_from_db(Path(args.db), args.limit)
    )
    if not claims:
        print("no claims to replay", file=sys.stderr)
        return 2
    estimate = len(claims) * settings.cost_per_verify_usd
    print(
        f"{len(claims)} claim(s) -> {provider} · {model}; estimated cost "
        f"~${estimate:.2f} (${settings.cost_per_verify_usd:.3f}/claim)"
    )
    if not args.yes_spend_credits:
        print("refusing to spend credits without --yes-spend-credits", file=sys.stderr)
        return 2

    configure_logging("WARNING")
    if provider == "openrouter":
        await prime_openrouter_capabilities([model])
        print("capabilities:", json.dumps(lookup_model_capabilities(model).as_dict()))
    else:
        print(f"anthropic auth: {settings.anthropic_auth_method}")
    client = create_llm_client(settings, provider)
    checker = create_fact_checker(settings, client, QuotaCooldown())
    semaphore = asyncio.Semaphore(args.concurrency)
    results: list[dict[str, Any]] = []

    async def one(item: dict[str, str]) -> None:
        async with semaphore:
            started = time.perf_counter()
            record: dict[str, Any] = {
                "claim": item["claim"],
                "topic": item.get("topic", "other"),
                "previous_label": item.get("previous_label"),
                "model": model,
            }
            try:
                verdict = await checker.check(item["claim"], topic=item.get("topic", "other"))  # type: ignore[arg-type]
            except Exception as exc:  # report, never abort the replay
                record.update({"error": f"{type(exc).__name__}: {exc}"})
            else:
                record.update(
                    {
                        "label": verdict.label,
                        "evidence": verdict.evidence,
                        "used_fallback": verdict.used_fallback,
                        "explanation": verdict.explanation,
                        "sources": [source.url for source in verdict.sources],
                    }
                )
            record["latency_ms"] = int((time.perf_counter() - started) * 1000)
            results.append(record)
            flag = "FB" if record.get("used_fallback") else "  "
            print(
                f"{record.get('label', 'ERROR'):<11} {str(record.get('evidence')):<8} {flag}"
                f" {record['latency_ms']:>6}ms  {item['claim'][:90]}"
            )

    try:
        await asyncio.gather(*(one(item) for item in claims))
    finally:
        await client.close()

    if args.out:
        with Path(args.out).open("w") as handle:
            for record in results:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    labels = Counter(record.get("label", "ERROR") for record in results)
    evidence = Counter(str(record.get("evidence")) for record in results)
    fallbacks = sum(1 for record in results if record.get("used_fallback"))
    latencies = sorted(record["latency_ms"] for record in results)
    print()
    print("labels:  ", dict(labels))
    print("evidence:", dict(evidence))
    print(f"fallbacks: {fallbacks}/{len(results)}")
    if latencies:
        p95 = latencies[
            min(len(latencies) - 1, int(round(0.95 * (len(latencies) - 1))))
        ]
        print(f"latency p50 {statistics.median(latencies):.0f}ms  p95 {p95}ms")
    if provider == "openrouter":
        print("verify modes:", json.dumps(verify_mode_snapshot()))
    changed = [
        record
        for record in results
        if record.get("previous_label")
        and record.get("label")
        and record["previous_label"] != record["label"]
    ]
    if changed:
        print(f"\n{len(changed)} label(s) changed vs the stored verdict:")
        for record in changed:
            print(
                f"  {record['previous_label']:<11} -> {record['label']:<11} {record['claim'][:80]}"
            )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=str(BACKEND_DIR / "fact_checker.db"))
    parser.add_argument("--csv", help="claim,topic rows instead of the database")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--provider",
        choices=sorted(VERIFY_MODEL_FIELDS),
        help="verify provider (default: the configured VERIFY_PROVIDER/LLM_PROVIDER)",
    )
    parser.add_argument("--model", help="override the provider's verify model setting")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--out", help="write one JSON object per claim here")
    parser.add_argument("--yes-spend-credits", action="store_true")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
