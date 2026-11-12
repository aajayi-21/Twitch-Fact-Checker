#!/usr/bin/env python3
"""Export the backend's prompts and schemas for the in-extension pipeline.

The browser engine (``extension/engine/``) runs the same claim gate and
verification as this backend, in JavaScript. To stop the two drifting, the
prompt text, JSON schemas and label rules are NOT rewritten by hand there:
this script renders them from the Python source of truth into one generated
ES module, ``extension/engine/generated/prompts.mjs``.

``tests/test_prompt_export.py`` fails when the checked-in file differs from a
fresh render, so a prompt change that forgets to re-export breaks the build.

Placeholders left in the exported templates (substituted in JavaScript with a
plain string replace — single braces everywhere else are literal):

- ``{context}``, ``{new_transcript}`` — gate input
- ``{date}`` — today's date, both verify templates
- ``{max_uses}``, ``{search_tool}`` — self-search verify template only

Usage (from ``backend/``)::

    uv run --no-sync python scripts/export_prompts.py           # write the file
    uv run --no-sync python scripts/export_prompts.py --check   # exit 1 if stale
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app import prompts  # noqa: E402
from app.claim_gate import GATE_JSON_SCHEMA  # noqa: E402
from app.config import (  # noqa: E402
    DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_OPENROUTER_GATE_MODEL,
    DEFAULT_OPENROUTER_VERIFY_MODEL,
    SENSITIVITY_THRESHOLDS,
    Settings,
)
from app.fact_checker import (  # noqa: E402
    EVIDENCE_LEVELS,
    MAX_EXPLANATION_CHARS,
    MAX_SOURCES,
    NO_SOURCES_NOTE,
    WEAK_EVIDENCE_NOTE,
)
from app.llm_anthropic import ANTHROPIC_VERDICT_SCHEMA  # noqa: E402
from app.llm_openrouter import VERDICT_JSON_SCHEMA, WEB_SEARCH_PROMPT  # noqa: E402
from app.models import TOPICS  # noqa: E402

OUTPUT_PATH = (
    BACKEND_DIR.parent / "extension" / "engine" / "generated" / "prompts.mjs"
)

# The self-search template names Anthropic's tool. Other providers call theirs
# something else, so the export swaps the name for a placeholder; the assert
# below fails loudly if the prompt wording ever stops containing it.
_ANTHROPIC_SEARCH_TOOL_PHRASE = "the web_search tool"

HEADER = """\
// GENERATED FILE — do not edit.
//
// Rendered from the backend's prompts and schemas by
// backend/scripts/export_prompts.py. To change anything here, change the
// Python source (backend/app/prompts.py and friends) and re-run:
//
//     cd backend && uv run --no-sync python scripts/export_prompts.py
//
// backend/tests/test_prompt_export.py fails while this file is stale.
"""


def _field_default(name: str) -> Any:
    """A ``Settings`` field's default, without constructing ``Settings``.

    Instantiating ``Settings`` would read the developer's ``.env``, making the
    export depend on local configuration.
    """
    return Settings.model_fields[name].default


def build_export() -> dict[str, Any]:
    """Everything the extension needs, as plain JSON-serialisable data."""
    gate_instructions, gate_input_template = prompts.build_gate_prompt_parts(
        "{context}", "{new_transcript}"
    )

    self_search = prompts.ANTHROPIC_VERIFY_SYSTEM_TEMPLATE.format(
        date="{date}",
        max_uses="{max_uses}",
        label_guidance=prompts.VERIFY_LABEL_GUIDANCE,
    )
    if _ANTHROPIC_SEARCH_TOOL_PHRASE not in self_search:
        raise RuntimeError(
            "the Anthropic verify prompt no longer contains "
            f"{_ANTHROPIC_SEARCH_TOOL_PHRASE!r}; update export_prompts.py"
        )
    self_search = self_search.replace(_ANTHROPIC_SEARCH_TOOL_PHRASE, "{search_tool}")

    attached_results = prompts.VERIFY_SYSTEM_TEMPLATE.format(
        date="{date}", label_guidance=prompts.VERIFY_LABEL_GUIDANCE
    )

    return {
        "GATE_INSTRUCTIONS": gate_instructions,
        "GATE_INPUT_TEMPLATE": gate_input_template,
        "VERIFY_SELF_SEARCH_SYSTEM_TEMPLATE": self_search,
        "VERIFY_ATTACHED_RESULTS_SYSTEM_TEMPLATE": attached_results,
        "OPENROUTER_WEB_SEARCH_PROMPT": WEB_SEARCH_PROMPT,
        "GATE_SCHEMA": GATE_JSON_SCHEMA["schema"],
        "VERDICT_SELF_SEARCH_SCHEMA": ANTHROPIC_VERDICT_SCHEMA,
        "VERDICT_ATTACHED_RESULTS_SCHEMA": VERDICT_JSON_SCHEMA["schema"],
        "TOPICS": list(TOPICS),
        "LABELS": ["TRUE", "FALSE", "MISLEADING", "UNVERIFIED"],
        "EVIDENCE_LEVELS": list(EVIDENCE_LEVELS),
        "SENSITIVITY_THRESHOLDS": SENSITIVITY_THRESHOLDS,
        "MAX_SOURCES": MAX_SOURCES,
        "MAX_EXPLANATION_CHARS": MAX_EXPLANATION_CHARS,
        "NO_SOURCES_NOTE": NO_SOURCES_NOTE,
        "WEAK_EVIDENCE_NOTE": WEAK_EVIDENCE_NOTE,
        "BACKEND_DEFAULTS": {
            "anthropic_model": DEFAULT_ANTHROPIC_MODEL,
            "anthropic_web_max_uses": _field_default("anthropic_web_max_uses"),
            "openrouter_gate_model": DEFAULT_OPENROUTER_GATE_MODEL,
            "openrouter_verify_model": DEFAULT_OPENROUTER_VERIFY_MODEL,
            "openrouter_web_max_results": _field_default(
                "openrouter_web_max_results"
            ),
            "openrouter_web_engine": _field_default("openrouter_web_engine"),
        },
    }


def render() -> str:
    """The full text of the generated module."""
    lines = [HEADER]
    for name, value in build_export().items():
        literal = json.dumps(value, indent=2, ensure_ascii=False)
        if isinstance(value, (dict, list)):
            lines.append(f"export const {name} = Object.freeze({literal});\n")
        else:
            lines.append(f"export const {name} = {literal};\n")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 if the checked-in file is stale",
    )
    args = parser.parse_args()
    rendered = render()
    if args.check:
        current = OUTPUT_PATH.read_text(encoding="utf-8") if OUTPUT_PATH.exists() else ""
        if current != rendered:
            print(f"{OUTPUT_PATH} is stale — re-run export_prompts.py", file=sys.stderr)
            return 1
        print(f"{OUTPUT_PATH} is up to date.")
        return 0
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(rendered, encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
