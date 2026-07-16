"""Prompt templates for the claim gate and the verification stage.

Prompt-engineering notes
========================

**Gate prompt.** Live-stream transcripts are noisy (ASR errors, half
sentences, jargon) and mostly non-factual, so the prompt does three jobs:

1. *Hard exclusions are enumerated, not implied.* Each leak class observed in
   practice (opinions, predictions, in-game events, hype, anecdotes, sponsor
   reads, lyrics, garbled ASR output) gets its own bullet — models follow
   explicit lists far more reliably than a general "ignore non-facts".
2. *Chunk-boundary recovery.* Transcripts arrive in ~12 s batches, so a claim
   frequently starts in one batch and completes in the next. The input is laid
   out as ``CONTEXT`` (already-processed tail, reference resolution ONLY) plus
   ``NEW TRANSCRIPT``; a claim is extracted iff its assertion *completes* in
   NEW, which both catches boundary claims and prevents double-extraction.
3. *Self-contained rewriting.* Each claim must be rewritten as one
   declarative sentence with pronouns resolved from context, because the
   verification stage sees the claim in isolation. Unresolvable pronoun ->
   skip; unsure -> return ``[]`` (a missed claim is cheap, a junk grounded
   search is not).

``check_worthiness`` is a 0-1 score ("would a professional fact-checker
bother?"). Sensitivity is applied *server-side* as a numeric threshold
(:data:`app.config.SENSITIVITY_THRESHOLDS`) — the prompt never changes, which
keeps gating deterministic and unit-testable. The few-shots cover the exact
leak classes above so threshold tuning stays meaningful.

``topic`` follows the same doctrine: the model *labels* every claim with one
of the nine canonical slugs (:data:`app.models.TOPICS`) and the server
*filters* against the session's enabled set — the prompt never changes with
the user's topic selection. Unverifiable philosophical positions are not
claims at all (the gate returns ``[]``); the verifiable residue about
philosophers/texts ("Nietzsche wrote X in 1886") is labelled ``history``.

**Verify prompt.** Anti-hallucination is structural, not rhetorical: the
model must decide strictly from retrieved sources, ``UNVERIFIED`` is the
explicit default for weak/inconclusive results, and the requested output is a
flat two-field object (label + explanation) — source URLs come exclusively
from grounding metadata in code, never from model text (models fabricate
URLs). The current date is injected because live streams discuss current
events and the model's training cutoff is otherwise ambiguous.
"""

GATE_PROMPT_TEMPLATE = """\
You extract objectively verifiable, substantial factual claims from noisy, \
auto-transcribed live-stream speech (Twitch). The transcript comes from \
automatic speech recognition: expect missing punctuation, wrong words, and \
sentence fragments.

HARD EXCLUSIONS — never extract any of the following:
- Opinions or matters of taste ("this game is terrible", "she's the best rapper").
- Predictions or statements of intent ("I'm pushing left lane", "they'll win worlds").
- In-game events, stats, builds, items, patch mechanics, or gaming jargon of any kind.
- Hype, banter, trash talk, sarcasm, or jokes.
- Personal anecdotes or claims about the streamer's own life ("I ate three pizzas").
- Sponsor reads, ads, promo codes, and calls to action ("use code X", "hit that follow").
- Song lyrics or quoted media dialogue.
- Garbled, incoherent, or clearly mis-transcribed text.

INPUT LAYOUT:
- "CONTEXT" is transcript that was ALREADY processed. Use it ONLY to resolve
  references (pronouns, "that country", "the tower"). Never extract a claim
  that was fully asserted inside CONTEXT.
- "NEW TRANSCRIPT" is the fresh text. Extract a claim if its assertion
  COMPLETES in NEW TRANSCRIPT, even if it began in CONTEXT.

RULES:
1. Rewrite every claim as ONE self-contained declarative sentence. Resolve
   pronouns and vague references using CONTEXT. If a reference cannot be
   resolved, SKIP the claim.
2. Only real-world claims a third party could verify against reputable
   published sources qualify.
3. A factual claim wrapped in opinion framing ("I think...", "everyone knows
   ...") is still a claim — extract the factual core.
4. Score each claim's check_worthiness from 0 to 1: would a professional
   fact-checker bother checking this? Substantial, contestable, real-world
   assertions score high; trivia and near-tautologies score low.
5. Classify each claim's topic as EXACTLY ONE of these slugs:
   - "politics": elections, legislation, geopolitics, breaking-news claims.
   - "health": medicine, nutrition, fitness, disease.
   - "science_tech": science, space, climate, technology, AI.
   - "money": prices, markets, salaries, company and crypto facts.
   - "history": historical events, dates, figures.
   - "sports": records, results, athlete facts.
   - "gaming": VERIFIABLE game-industry facts only — patch numbers, sales
     figures, esports/speedrun records, developer history. In-game strategy,
     stats, and jargon remain hard exclusions, never claims.
   - "entertainment": movies, music, celebrities, charts.
   - "other": everything that fits none of the above.
   Unverifiable philosophical positions ("free will is an illusion") are NOT
   claims at all; verifiable claims about philosophers or their texts are
   "history".
6. If you are unsure, return an empty claims list. Missing a claim is fine;
   inventing one is not.

Return JSON matching the schema: {{"claims": [{{"claim_text": str,
"check_worthiness": float, "topic": str}}]}}.

EXAMPLES:

Example 1 (gaming jargon):
NEW TRANSCRIPT: okay I'm gonna rotate mid we need baron buff their jungler is dead for forty seconds
Output: {{"claims": []}}

Example 2 (prediction / intent):
NEW TRANSCRIPT: chat trust me T1 is winning worlds this year no doubt I'd bet my house
Output: {{"claims": []}}

Example 3 (opinion / taste):
NEW TRANSCRIPT: honestly this patch is terrible the devs have no idea what they're doing worst update ever
Output: {{"claims": []}}

Example 4 (in-game stat):
NEW TRANSCRIPT: bro I'm doing forty percent of the team's damage with a support item that's insane
Output: {{"claims": []}}

Example 5 (real-world sports fact):
NEW TRANSCRIPT: you know Messi has won eight Ballon d'Or awards right no other player is close
Output: {{"claims": [{{"claim_text": "Lionel Messi has won eight Ballon d'Or awards.", "check_worthiness": 0.8, "topic": "sports"}}]}}

Example 6 (current event):
NEW TRANSCRIPT: did you see the news the EU just fined Apple five hundred million euros over the App Store thing
Output: {{"claims": [{{"claim_text": "The European Union fined Apple 500 million euros over App Store practices.", "check_worthiness": 0.9, "topic": "politics"}}]}}

Example 7 (geography trivia, completes in NEW, pronoun resolved from CONTEXT):
CONTEXT (reference resolution only): so we were talking about the Eiffel Tower earlier and someone in chat said
NEW TRANSCRIPT: that it's actually taller than four hundred and fifty meters which sounds wrong to me but whatever
Output: {{"claims": [{{"claim_text": "The Eiffel Tower is taller than 450 meters.", "check_worthiness": 0.9, "topic": "other"}}]}}

Example 8 (opinion-wrapped fact):
NEW TRANSCRIPT: I think the earth is like six thousand years old that's just facts chat look it up
Output: {{"claims": [{{"claim_text": "The Earth is approximately 6,000 years old.", "check_worthiness": 0.9, "topic": "science_tech"}}]}}

Now process the real input.

CONTEXT (reference resolution only): {context}

NEW TRANSCRIPT: {new_transcript}
"""


VERIFY_PROMPT_TEMPLATE = """\
Fact-check EXACTLY this claim using Google Search. Today is {date}.

CLAIM: "{claim}"

Search for reputable, independent sources and decide STRICTLY from what the
retrieved sources say — never from memory alone.

Label definitions (apply strictly):
- TRUE: reputable sources clearly and directly confirm the claim.
- FALSE: reputable sources clearly and directly refute the claim.
- MISLEADING: the claim contains a kernel of truth but its framing, numbers,
  or missing context make it materially deceptive.
- UNVERIFIED: the DEFAULT. Use it whenever search results are inconclusive or
  conflicting, the sources are weak (forums, fan wikis, content farms), or the
  claim is too vague or too recent to verify.

Write the explanation as 2-3 plain-language sentences grounded only in the
retrieved sources, including the key fact or number that decides the verdict.
Do not put URLs in the explanation text.

Respond with a JSON object with exactly two fields:
{{"label": "TRUE" | "FALSE" | "MISLEADING" | "UNVERIFIED", "explanation": "..."}}
"""


VERIFY_FALLBACK_PROMPT_TEMPLATE = """\
Fact-check EXACTLY this claim using Google Search. Today is {date}.

CLAIM: "{claim}"

Search for reputable, independent sources and decide STRICTLY from what the
retrieved sources say — never from memory alone.

Label definitions (apply strictly):
- TRUE: reputable sources clearly and directly confirm the claim.
- FALSE: reputable sources clearly and directly refute the claim.
- MISLEADING: the claim contains a kernel of truth but its framing, numbers,
  or missing context make it materially deceptive.
- UNVERIFIED: the DEFAULT. Use it whenever search results are inconclusive or
  conflicting, the sources are weak (forums, fan wikis, content farms), or the
  claim is too vague or too recent to verify.

Respond in EXACTLY this two-line format and nothing else:
LABEL: <TRUE|FALSE|MISLEADING|UNVERIFIED>
EXPLANATION: <2-3 plain-language sentences grounded only in the retrieved \
sources, including the key fact or number. No URLs.>
"""


VERDICT_EXTRACTION_PROMPT_TEMPLATE = """\
The text below is a fact-check verdict written in free form. Extract it into
JSON with exactly two fields: "label" (one of TRUE, FALSE, MISLEADING,
UNVERIFIED) and "explanation" (2-3 sentences copied or minimally condensed
from the text — do not add any new information). If no clear label is stated,
use "UNVERIFIED".

TEXT:
{raw_text}
"""


def build_gate_prompt(context: str, new_transcript: str) -> str:
    """Render the claim-gate prompt for one drained transcript batch."""
    return GATE_PROMPT_TEMPLATE.format(
        context=context.strip() or "(none)",
        new_transcript=new_transcript.strip(),
    )


def build_verify_prompt(claim: str, date: str) -> str:
    """Render the grounded structured verification prompt."""
    return VERIFY_PROMPT_TEMPLATE.format(claim=claim, date=date)


def build_verify_fallback_prompt(claim: str, date: str) -> str:
    """Render the grounded plain-text (LABEL:/EXPLANATION:) fallback prompt."""
    return VERIFY_FALLBACK_PROMPT_TEMPLATE.format(claim=claim, date=date)


def build_verdict_extraction_prompt(raw_text: str) -> str:
    """Render the last-resort ungrounded structured-extraction prompt."""
    return VERDICT_EXTRACTION_PROMPT_TEMPLATE.format(raw_text=raw_text.strip())
