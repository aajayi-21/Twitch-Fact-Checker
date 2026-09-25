"""String-level guards on the prompts (no LLM involved).

Each guard maps to a failure observed in production verdicts: a claim class
the gate let through, or search-tool wording the web plugin cannot honor.
"""

from app.prompts import (
    GATE_PROMPT_TEMPLATE,
    VERIFY_IMAGE_NOTE,
    VERIFY_LABEL_GUIDANCE,
    build_gate_prompt,
    build_verdict_extraction_messages,
    build_verify_fallback_messages,
    build_verify_messages,
)

CLAIM = "The Eiffel Tower is 450 meters tall."

# Observed gate leaks -> the rule text that now excludes each class.
OBSERVED_LEAKS = {
    "Police must remove the protesters immediately.": "Imperatives, demands",
    "An elderly man is being arrested at the location where the stream is happening.": (
        "Live, hyper-local events"
    ),
    "The police are outnumbered by people at the event and are gaining ground.": (
        '"gaining ground"'
    ),
    "Poland is a lot closer to Russia.": "Vague comparatives",
    "Real polls had Hong ahead before the election.": "never a bare surname",
}


class TestGatePrompt:
    def test_each_observed_leak_class_has_an_exclusion(self) -> None:
        prompt = build_gate_prompt("", "anything")
        for leak, rule in OBSERVED_LEAKS.items():
            assert rule in prompt, f"no rule for {leak!r}"

    def test_new_examples_appear_exactly_once(self) -> None:
        assert GATE_PROMPT_TEMPLATE.count("Example 9 (live scene + imperative") == 1
        assert GATE_PROMPT_TEMPLATE.count("Example 10 (entity resolution") == 1
        assert GATE_PROMPT_TEMPLATE.count("Francesca Hong") >= 2

    def test_live_observation_score_cap_is_stated(self) -> None:
        assert "scores at most 0.3" in GATE_PROMPT_TEMPLATE

    def test_real_input_stays_last(self) -> None:
        prompt = build_gate_prompt("earlier words", "fresh words")
        assert prompt.rstrip().endswith("NEW TRANSCRIPT: fresh words")


class TestVerifyPrompts:
    def test_openrouter_user_message_is_the_bare_claim(self) -> None:
        system, user = build_verify_messages(CLAIM, "2026-09-06")
        assert user == CLAIM
        assert "Today is 2026-09-06" in system
        assert "attached to this request" in system
        assert '"evidence"' in system

    def test_openrouter_fallback_asks_for_the_evidence_line(self) -> None:
        system, user = build_verify_fallback_messages(CLAIM, "2026-09-06")
        assert user == CLAIM
        assert "EVIDENCE: <strong|partial|none>" in system

    def test_calibration_guidance_is_shared(self) -> None:
        assert "Never use it\n  for pedantic precision" in VERIFY_LABEL_GUIDANCE
        assert "Politburo" in VERIFY_LABEL_GUIDANCE
        system, _ = build_verify_messages(CLAIM, "2026-09-06")
        assert "Politburo" in system

    def test_image_note_no_longer_claims_images_produce_no_citations(self) -> None:
        assert "url_citation" not in VERIFY_IMAGE_NOTE
        assert "not a source" in VERIFY_IMAGE_NOTE
        system, user = build_verify_messages(CLAIM, "2026-09-06", with_image=True)
        assert "frame captured from the live stream" in system
        assert user == CLAIM

    def test_extraction_messages_keep_the_text_in_the_user_turn(self) -> None:
        system, user = build_verdict_extraction_messages("  LABEL: TRUE ...  ")
        assert user == "LABEL: TRUE ..."
        assert '"evidence"' in system
