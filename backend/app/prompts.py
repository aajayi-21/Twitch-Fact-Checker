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
flat object (label, evidence rating, explanation)
— source URLs come exclusively from grounding metadata in code, never from
model text (models fabricate URLs). The current date is injected because
live streams discuss current events and the model's training cutoff is
otherwise ambiguous.

OpenRouter does not let the model search. Its ``web`` plugin runs a search
on the request's USER message before the model runs and injects the results.
The instructions therefore live in a ``system`` message and the user message
is the bare claim — anything else in it pollutes the search query
(``build_verify_messages``). The wording says "the results attached to this
request", never "search for".

The verify prompts share :data:`VERIFY_LABEL_GUIDANCE`, which carries the calibration
learned from production verdicts: MISLEADING was being used for pedantry and
FALSE for near-correct facts.
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
- Live, hyper-local events unfolding at or around the stream right now: what
  the crowd, the police, or a passer-by is doing, who is being arrested, how
  many people are present, who is "gaining ground". No published source can
  exist yet, so nothing can verify them.
- Imperatives, demands, and should/must statements ("police must remove the
  protesters", "they should resign").
- Vague comparatives or superlatives with no explicit referent or quantity
  ("Poland is a lot closer to Russia", "prices went up a lot").

INPUT LAYOUT:
- "CONTEXT" is transcript that was ALREADY processed. Use it ONLY to resolve
  references (pronouns, "that country", "the tower"). Never extract a claim
  that was fully asserted inside CONTEXT.
- "NEW TRANSCRIPT" is the fresh text. Extract a claim if its assertion
  COMPLETES in NEW TRANSCRIPT, even if it began in CONTEXT.

RULES:
1. Rewrite every claim as ONE self-contained declarative sentence. Resolve
   pronouns and vague references using CONTEXT. If a reference cannot be
   resolved, SKIP the claim. The sentence must stand alone as a web search
   query: use people's full names, and name the place, event, and year when
   CONTEXT supplies them ("Francesca Hong", "the Wisconsin Democratic
   primary"), never a bare surname or "the election". If CONTEXT cannot
   supply them, SKIP the claim.
2. Only real-world claims a third party could verify against reputable
   published sources qualify.
3. A factual claim wrapped in opinion framing ("I think...", "everyone knows
   ...") is still a claim — extract the factual core.
4. Score each claim's check_worthiness from 0 to 1: would a professional
   fact-checker bother checking this? Substantial, contestable, real-world
   assertions score high; trivia and near-tautologies score low. A claim
   about what the speaker is watching happen right now scores at most 0.3
   even when phrased factually.
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

Example 9 (live scene + imperative, IRL stream):
CONTEXT (reference resolution only): we're at the port in Dover chat look at all these people
NEW TRANSCRIPT: cops are totally outnumbered right now they're gaining ground police must clear them out immediately
Output: {{"claims": []}}

Example 10 (entity resolution from CONTEXT):
CONTEXT (reference resolution only): so the Wisconsin Democratic primary for governor last night Francesca Hong versus David Crowley and Crowley barely won
NEW TRANSCRIPT: real polls had Hong ahead before the election though that's the crazy part
Output: {{"claims": [{{"claim_text": "Pre-election polls showed Francesca Hong leading David Crowley before the Wisconsin Democratic gubernatorial primary.", "check_worthiness": 0.8, "topic": "politics"}}]}}

Now process the real input.

CONTEXT (reference resolution only): {context}

NEW TRANSCRIPT: {new_transcript}
"""


VERIFY_LABEL_GUIDANCE = """\
Label definitions (apply strictly):
- TRUE: reputable sources confirm the SUBSTANCE of the claim. A detail that is
  approximate, rounded, hedged ("about", "a lot", "for a long time"), or
  slightly out of date does not stop a substantively correct claim from being
  TRUE.
- FALSE: reputable sources clearly and directly refute the substance of the
  claim — a direct contradiction, not a nuance or a missing caveat.
- MISLEADING: the claim has a kernel of truth but its framing, numbers, or
  omitted context would materially deceive a reasonable listener. Never use it
  for pedantic precision, and never for a hedged approximation that is broadly
  right.
- UNVERIFIED: the DEFAULT. Use it whenever the results are inconclusive,
  conflicting, off-topic, or weak (forums, fan wikis, content farms), or the
  claim is too vague or too recent to verify.

Unverifiable claim types — always UNVERIFIED, whatever the results say: a claim
about something happening live at the speaker's location right now ("is being
arrested", "showed up", "are gaining ground"), a demand or should/must
statement, an opinion, or a prediction. Published sources cannot settle these;
a similar incident somewhere else is not this one.

Calibration examples:
- "Migrants have been arriving in Kent for a long time" + sources showing
  small-boat arrivals since 2014 -> TRUE (hedged duration; substance right).
- "The Politburo has 24 members" + sources: 24 elected, one seat currently
  vacant -> TRUE (substantively correct; the vacancy belongs in the
  explanation, not in the verdict).
- "China accounts for 30% of U.S. trade" + sources showing about 6% -> FALSE
  (the number IS the claim, and it is directly refuted).
"""


# OpenRouter (primary): instructions in the SYSTEM message, the bare claim in
# the USER message. OpenRouter's web plugin searches on the user message before
# the model runs, so anything else there pollutes the search query.
VERIFY_SYSTEM_TEMPLATE = """\
You are a fact-checker for live-stream speech. Today is {date}.

The user message is ONE claim heard on a live stream. Web search results
retrieved for that claim are attached to this request. Decide STRICTLY from
those results — never from memory alone — and ignore results that are merely
on the same topic without addressing the claim itself.

{label_guidance}
Evidence rating:
- "strong": at least one reputable result reports the SAME specific event,
  entity, place, and time as the claim and directly confirms or refutes it.
- "partial": results are related and suggestive but not decisive — a similar
  incident elsewhere or at another time, a general rule, or the same topic
  without the claim's specifics.
- "none": results are off-topic or absent, or the claim names no identifiable
  event, entity, place, or time that a result could match.
TRUE, FALSE, and MISLEADING require "strong" evidence; anything less is
UNVERIFIED. Unverifiable claim types (above) are always "none".

Write the explanation as 2-3 plain-language sentences grounded only in the
retrieved results, including the key fact or number that decides the verdict.
Do not put URLs in the explanation text.

Respond with a JSON object with exactly three fields:
{{"label": "TRUE" | "FALSE" | "MISLEADING" | "UNVERIFIED", "explanation": "...",
"evidence": "strong" | "partial" | "none"}}
"""

VERIFY_USER_TEMPLATE = "{claim}"


VERIFY_FALLBACK_SYSTEM_TEMPLATE = """\
You are a fact-checker for live-stream speech. Today is {date}.

The user message is ONE claim heard on a live stream. Web search results
retrieved for that claim are attached to this request. Decide STRICTLY from
those results — never from memory alone — and ignore results that are merely
on the same topic without addressing the claim itself.

{label_guidance}
Evidence rating: "strong" = at least one reputable result reports the SAME
specific event, entity, place, and time as the claim and directly confirms or
refutes it; "partial" = related but not decisive (a similar incident
elsewhere, a general rule, the same topic without the specifics); "none" =
off-topic, absent, or the claim names nothing a result could match. TRUE,
FALSE, and MISLEADING require "strong"; unverifiable claim types are "none".

Respond in EXACTLY this three-line format and nothing else:
LABEL: <TRUE|FALSE|MISLEADING|UNVERIFIED>
EVIDENCE: <strong|partial|none>
EXPLANATION: <2-3 plain-language sentences grounded only in the retrieved \
results, including the key fact or number. No URLs.>
"""


# Appended to verify prompts when a captured stream frame is attached. The
# frame is context, never evidence: the label and evidence rules stay
# anchored to the retrieved sources, so a misread frame cannot move a verdict.
VERIFY_IMAGE_NOTE = """\


A frame captured from the live stream is attached. Use it ONLY if it is
clearly legible and directly relevant to the claim; otherwise ignore it
entirely. The frame is not a source: never move off UNVERIFIED, and never
rate the evidence higher than the retrieved results justify, on the basis of
the image alone.
"""


CONTRADICTION_PROMPT_TEMPLATE = """\
You compare two statements made by the SAME speaker at different points in ONE
live stream, and decide whether they LOGICALLY CONTRADICT each other.

EARLIER STATEMENT: "{prior}"
LATER STATEMENT: "{current}"

A contradiction requires ALL of the following:
- Both statements are about the SAME concrete entity, event, or quantity —
  being about the same topic is NOT enough.
- The two statements cannot both be true (a direct logical clash).
- The clash is factual, not a shift in opinion, preference, plan, or mood.

These are NOT contradictions (default to contradicts=false):
- Changing one's mind, opinion drift, or updated preferences over the stream.
- Jokes, sarcasm, hyperbole, banter, or obvious exaggeration.
- Vague overlap, or statements reconcilable by time passing or added context.
- Restating or refining the same claim with slightly different wording or
  numbers.

Weight direct negations ("I have never...", "always...", "first time...")
higher than a plain factual delta.

confidence is "high" ONLY when both statements are unambiguous, concern the
same concrete fact, and cannot be reconciled. If you are unsure, return
contradicts=false with confidence "low". Missing a contradiction is fine;
inventing one is not.

Respond with a JSON object with exactly three fields:
{{"contradicts": true | false, "confidence": "low" | "medium" | "high",
"explanation": "<one sentence naming the clashing fact>"}}
"""


VERDICT_EXTRACTION_INSTRUCTIONS = """\
The text is a fact-check verdict written in free form. Extract it into JSON
with the fields "label" (one of TRUE, FALSE, MISLEADING, UNVERIFIED) and
"explanation" (2-3 sentences copied or minimally condensed from the text — do
not add any new information), plus "evidence" (one of strong, partial, none)
ONLY when the text states how directly the sources addressed the claim;
otherwise omit "evidence". If no clear label is stated, use "UNVERIFIED".\
"""


def build_gate_prompt(context: str, new_transcript: str) -> str:
    """Render the claim-gate prompt for one drained transcript batch."""
    return GATE_PROMPT_TEMPLATE.format(
        context=context.strip() or "(none)",
        new_transcript=new_transcript.strip(),
    )


def build_verify_messages(
    claim: str, date: str, with_image: bool = False
) -> tuple[str, str]:
    """``(system, user)`` for the OpenRouter structured verify call.

    The user message is the bare claim: OpenRouter's web plugin uses it as
    the search query. Everything else — including the image note — goes in
    the system message.
    """
    system = VERIFY_SYSTEM_TEMPLATE.format(
        date=date, label_guidance=VERIFY_LABEL_GUIDANCE
    )
    if with_image:
        system += VERIFY_IMAGE_NOTE
    return system, VERIFY_USER_TEMPLATE.format(claim=claim)


def build_verify_fallback_messages(
    claim: str, date: str, with_image: bool = False
) -> tuple[str, str]:
    """``(system, user)`` for the OpenRouter LABEL:/EVIDENCE:/EXPLANATION: call."""
    system = VERIFY_FALLBACK_SYSTEM_TEMPLATE.format(
        date=date, label_guidance=VERIFY_LABEL_GUIDANCE
    )
    if with_image:
        system += VERIFY_IMAGE_NOTE
    return system, VERIFY_USER_TEMPLATE.format(claim=claim)


def build_verdict_extraction_messages(raw_text: str) -> tuple[str, str]:
    """``(system, user)`` for the OpenRouter extraction pass (no search)."""
    return VERDICT_EXTRACTION_INSTRUCTIONS, raw_text.strip()


def build_contradiction_prompt(current: str, prior: str) -> str:
    """Render the contradiction-judge prompt (gate-model call, no search)."""
    return CONTRADICTION_PROMPT_TEMPLATE.format(current=current, prior=prior)
