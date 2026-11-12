"""String-level guards on the prompts (no LLM involved).

Each guard maps to a failure observed in production verdicts: a claim class
the gate let through, or search-tool wording the web plugin cannot honor.
"""

from app.prompts import (
    GATE_PROMPT_TEMPLATE,
    VERIFY_IMAGE_NOTE,
    VERIFY_LABEL_GUIDANCE,
    build_anthropic_verify_fallback_messages,
    build_anthropic_verify_messages,
    build_gate_prompt,
    build_gate_prompt_parts,
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


class TestGatePromptParts:
    """The Anthropic gate caches the static instructions as a system block."""

    def test_parts_concatenate_to_the_shared_prompt(self) -> None:
        for context, new in (("", "fresh words"), ("earlier words", "fresh words")):
            instructions, transcript = build_gate_prompt_parts(context, new)
            assert instructions + transcript == build_gate_prompt(context, new)

    def test_instructions_are_identical_on_every_pass(self) -> None:
        first, _ = build_gate_prompt_parts("a", "b")
        second, _ = build_gate_prompt_parts("completely different", "input")
        assert first == second
        assert "Now process the real input." in first
        assert "NEW TRANSCRIPT" not in first.split("Now process the real input.")[1]

    def test_transcript_part_holds_only_the_batch(self) -> None:
        _, transcript = build_gate_prompt_parts("", "fresh words")
        assert transcript == (
            "CONTEXT (reference resolution only): (none)\n\n"
            "NEW TRANSCRIPT: fresh words\n"
        )


class TestAnthropicVerifyPrompts:
    """Anthropic's model runs web_search itself, so its prompts say SEARCH."""

    def test_structured_prompt_tells_the_model_to_search_within_the_cap(self) -> None:
        system, user = build_anthropic_verify_messages(CLAIM, "2026-11-11", 2)
        assert user == CLAIM
        assert "Today is 2026-11-11" in system
        assert "Use the web_search tool" in system
        assert "at most\n2 times" in system
        assert "attached to this request" not in system  # OpenRouter wording
        assert '"source_urls"' in system
        assert "never from memory alone" in system
        assert "Politburo" in system  # shared calibration guidance

    def test_explanation_length_is_capped_below_the_popup_clamp(self) -> None:
        # Live Haiku runs wrote 450+ character explanations that the 450-char
        # clamp cut mid-sentence; both prompts state a tighter budget.
        structured, _ = build_anthropic_verify_messages(CLAIM, "2026-11-11", 2)
        fallback, _ = build_anthropic_verify_fallback_messages(CLAIM, "2026-11-11", 2)
        assert "UNDER 350\ncharacters" in structured
        assert "under 350 characters" in fallback

    def test_unsearched_or_indecisive_means_unverified(self) -> None:
        system, _ = build_anthropic_verify_messages(CLAIM, "2026-11-11", 1)
        assert "If you did not\nsearch" in system
        assert 'the evidence is "none"' in system

    def test_fallback_asks_for_the_three_line_format(self) -> None:
        system, user = build_anthropic_verify_fallback_messages(CLAIM, "2026-11-11", 3)
        assert user == CLAIM
        assert "Use the web_search tool" in system
        assert "EVIDENCE: <strong|partial|none>" in system
        assert "source_urls" not in system

    def test_image_note_is_appended(self) -> None:
        system, user = build_anthropic_verify_messages(
            CLAIM, "2026-11-11", 2, with_image=True
        )
        assert system.endswith(VERIFY_IMAGE_NOTE)
        assert user == CLAIM


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
