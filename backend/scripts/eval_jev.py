"""Explicit, paid Decisions API smoke/evaluation on synthetic transcript cases.

    uv run python scripts/eval_jev.py --yes-spend-credits

No web searches, extraction calls, or saved user transcripts are sent. Prints
one JSON row per case and aggregate routing accuracy. This small example set
is a regression aid, not a calibration benchmark.
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.config import Settings  # noqa: E402
from app.llm_jev import (  # noqa: E402
    JEV_DECISIONS_URL,
    JEV_QUESTIONS,
    JevResponse,
    build_jev_state,
)
from app.llm_openrouter import create_openrouter_client  # noqa: E402


async def run(args: argparse.Namespace) -> int:
    settings = Settings()
    settings.require_openrouter_api_key()
    if not settings.uses_jev:
        print("Configure an OpenRouter Jev gate before evaluation.", file=sys.stderr)
        return 2
    cases = json.loads(args.cases.read_text())
    correct = errors = 0
    total_cost = 0.0
    async with create_openrouter_client(settings.openrouter_api_key) as client:
        for case in cases:
            started = time.monotonic()
            try:
                async with asyncio.timeout(settings.gate_timeout_s):
                    raw = await client.post(
                        JEV_DECISIONS_URL,
                        cast_to=dict[str, Any],
                        body={
                            "model": settings.openrouter_gate_model,
                            "state": build_jev_state(case["context"], case["text"]),
                            "questions": JEV_QUESTIONS,
                        },
                    )
                response = JevResponse.model_validate(raw)
                probability = response.answers.needs_fact_check.noul
                route = probability >= settings.jev_min_check_probability
                correct += int(route == case["expected"])
                total_cost += float((raw.get("usage") or {}).get("cost") or 0)
                print(
                    json.dumps(
                        {
                            "case": case["name"],
                            "expected": case["expected"],
                            "probability": probability,
                            "extract": route,
                            "model": response.model,
                            "elapsed_s": round(time.monotonic() - started, 3),
                        }
                    )
                )
            except Exception as exc:
                errors += 1
                print(json.dumps({"case": case["name"], "error": type(exc).__name__}))
                # Do not spend more calls after a transport/auth/schema failure.
                break
    print(
        json.dumps(
            {
                "correct": correct,
                "cases": len(cases),
                "errors": errors,
                "reported_cost_usd": total_cost,
            }
        )
    )
    return 0 if not errors and correct == len(cases) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes-spend-credits", action="store_true")
    parser.add_argument(
        "--cases", type=Path, default=BACKEND_DIR / "tests/fixtures/jev_cases.json"
    )
    args = parser.parse_args()
    if not args.yes_spend_credits:
        print("Refusing to spend credits without --yes-spend-credits", file=sys.stderr)
        return 2
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
