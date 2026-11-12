"""The generated extension prompt module must match the backend's prompts.

``extension/engine/generated/prompts.mjs`` is rendered from this backend by
``scripts/export_prompts.py``. These tests fail when a prompt, schema or
label rule changes here without the export being re-run, which is what keeps
the in-extension pipeline from drifting away from the backend.
"""

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import export_prompts  # noqa: E402

from app import prompts  # noqa: E402
from app.claim_gate import GATE_JSON_SCHEMA  # noqa: E402


def test_checked_in_module_is_up_to_date() -> None:
    assert export_prompts.OUTPUT_PATH.exists(), (
        "run: cd backend && uv run --no-sync python scripts/export_prompts.py"
    )
    assert export_prompts.OUTPUT_PATH.read_text(encoding="utf-8") == (
        export_prompts.render()
    ), "extension prompts are stale — re-run scripts/export_prompts.py"


def test_gate_parts_reassemble_into_the_backend_prompt() -> None:
    exported = export_prompts.build_export()
    rendered = exported["GATE_INSTRUCTIONS"] + exported[
        "GATE_INPUT_TEMPLATE"
    ].replace("{context}", "earlier words").replace("{new_transcript}", "new words")
    assert rendered == prompts.build_gate_prompt("earlier words", "new words")


def test_self_search_template_matches_the_anthropic_prompt() -> None:
    exported = export_prompts.build_export()
    rendered = (
        exported["VERIFY_SELF_SEARCH_SYSTEM_TEMPLATE"]
        .replace("{date}", "2026-11-11")
        .replace("{max_uses}", "2")
        .replace("{search_tool}", "the web_search tool")
    )
    system, _user = prompts.build_anthropic_verify_messages(
        "any claim", "2026-11-11", 2
    )
    assert rendered == system


def test_attached_results_template_matches_the_openrouter_prompt() -> None:
    exported = export_prompts.build_export()
    rendered = exported["VERIFY_ATTACHED_RESULTS_SYSTEM_TEMPLATE"].replace(
        "{date}", "2026-11-11"
    )
    system, _user = prompts.build_verify_messages("any claim", "2026-11-11")
    assert rendered == system


@pytest.mark.parametrize(
    "template_name, placeholders",
    [
        ("GATE_INPUT_TEMPLATE", {"{context}", "{new_transcript}"}),
        (
            "VERIFY_SELF_SEARCH_SYSTEM_TEMPLATE",
            {"{date}", "{max_uses}", "{search_tool}"},
        ),
        ("VERIFY_ATTACHED_RESULTS_SYSTEM_TEMPLATE", {"{date}"}),
    ],
)
def test_templates_carry_exactly_their_placeholders(
    template_name: str, placeholders: set[str]
) -> None:
    template = export_prompts.build_export()[template_name]
    for placeholder in placeholders:
        assert template.count(placeholder) >= 1, placeholder
    # No Python format escapes may leak into the JavaScript strings.
    assert "{{" not in template and "}}" not in template


def test_schemas_are_the_backend_schemas() -> None:
    exported = export_prompts.build_export()
    assert exported["GATE_SCHEMA"] == GATE_JSON_SCHEMA["schema"]
    assert set(exported["VERDICT_SELF_SEARCH_SCHEMA"]["required"]) == {
        "evidence",
        "explanation",
        "label",
        "source_urls",
    }
    assert exported["VERDICT_ATTACHED_RESULTS_SCHEMA"]["required"] == [
        "label",
        "explanation",
        "evidence",
    ]
