# Live Stream Fact-Checker — Improvement & Feature Report

*Written 2026-08-08 against commit `15327ac`. Scope: the five features discussed across
two rounds (voice filtering, local inference, analytics/leaderboard, self-contradiction
tracking, video-frame analysis) plus findings from a full read of `backend/app/` and
`extension/`. Line references are to the commit above.*

---

## 0. Where the codebase stands

Before proposing anything, an honest baseline: **this is a well-built repo.** The parts
that usually rot in a project like this are the parts that are strongest here — the
provider seam is a real abstraction (`ClaimGate._extract` is one abstract method,
`FactChecker` two), the anti-hallucination rule is enforced in code rather than in a
prompt (`fact_checker.py:235-253`), the session pipeline's failure semantics are
deliberate and documented, and there are 254 backend tests.

That matters for what follows, because it means **most of what's below is additive rather
than a rewrite.** The first three requested features slot into seams that already exist;
the last two (§4, §5) are genuinely bigger builds and are flagged as such where they
appear.

The three genuine structural gaps, which the rest of this report keeps returning to:

| Gap | Consequence |
|---|---|
| **Nothing is persisted.** No database, no files, no counters. | No analytics, no cross-session dedupe, no accuracy measurement, no cost visibility. |
| **Nothing measures whether a verdict was right.** | Every quality claim in the repo (including the README's "4/4 live eval") is a sample of four. Model changes cannot be compared. |
| **The backend does not know whose stream it is checking.** `ClientHello` (`models.py:101-115`) carries audio format and filter settings, nothing else. | Per-channel anything — leaderboard, report, history — is impossible until this is fixed, and it is a two-line fix. |

---

## 1. Voice-activity filtering

### 1.1 What actually happens today

Worth being precise, because the obvious framing ("we're sending requests during
silence") isn't quite what's happening:

- The extension emits a 250 ms PCM frame **unconditionally**, every 250 ms, forever
  (`audio_capture.js:134-155`). That's 32 kB/s — **~115 MB per streaming hour** over the
  WebSocket. On localhost this is free. On a hosted backend it is not.
- The STT loop fires whenever 4.0 s of audio is buffered and consumes a 3.5 s hop
  (`pipeline.py:221-240`) — so **~1,030 Whisper invocations per hour, regardless of
  whether anyone spoke.** At the measured 0.3–0.6× realtime on CPU, that is 30–60% of a
  core burning continuously through ad breaks, BRB screens, and music.
- `vad_filter=True` is passed into `model.transcribe()` (`transcriber.py:269-275`), so
  faster-whisper's internal Silero pass does suppress *pure silence* fairly well. The
  cost that remains is the per-window scheduling, VAD execution, and — the real problem —
  everything that is **not silence but also not speech.**

That last category is where the money goes. Silero VAD is trained to separate speech from
silence, not speech from *music, game audio, crowd noise, or stream stingers*. Those pass
the VAD, reach the Whisper decoder, and produce garbage — which is precisely why
`transcriber.py:155-182` needs a 24-entry hallucination blacklist and why there are
`no_speech_prob` and `avg_logprob` backstops. **The blacklist is a symptom of a missing
front-end filter.** Every hallucinated "thanks for watching" that slips through is also
words in the gate buffer, pushing `MIN_NEW_WORDS` over the line and spending a gate call.

### 1.2 Recommended design

Three tiers. Ship them in this order; each is independently useful.

**Tier 1 — client-side energy gate (small, ships in an afternoon).**

In `pcm_worklet.js`, track per-block RMS against an adaptive noise floor (a slow
percentile tracker, not a fixed threshold — stream volumes vary wildly). Gate frame
emission in `audio_capture.js` with hysteresis: **~300 ms pre-roll** (a small circular
buffer of blocks, so word onsets aren't clipped) and **~800 ms hangover** (so inter-word
pauses don't chop utterances).

This catches only true silence, but true silence on a typical stream is not rare, and the
bandwidth win matters the moment the backend isn't on localhost.

> ⚠️ **This breaks the stream clock, and the fix must ship with it.**
> `AudioRingBuffer` derives absolute stream time from `_released_samples`
> (`transcriber.py:117-140`) — the count of samples consumed *or dropped*. Dropped
> audio is accounted for correctly today. **Audio that was never sent is not.** Gate
> out 30 s of silence and every subsequent transcript timestamp is 30 s behind the
> real stream position, permanently, and the drift accumulates.
>
> Fix: a `{"type":"gap","seconds":N}` control frame. `_handle_text_frame`
> (`pipeline.py:310-339`) already dispatches on `type`, and the ring already has the
> right primitive — a `skip(seconds)` method that advances `_released_samples` without
> touching the sample array is about six lines.

**Tier 2 — server-side VAD-driven utterance segmentation (the real win).**

Instead of fixed 4.0 s windows with a 3.5 s hop, run Silero VAD (already a
`faster-whisper` dependency — `faster_whisper.vad`) over the ring buffer directly and cut
on **utterance boundaries**: emit a segment when a speech region ends with ≥400 ms of
trailing silence, or when buffered speech hits a ~15 s cap.

Four things get better at once:

1. **Whisper sees complete utterances.** Fixed windowing chops sentences mid-clause,
   which is the single largest source of ASR error and hallucination in this pipeline.
2. **STT calls scale with speech, not wall-clock.** On music, ad breaks, and AFK segments
   the cost goes to roughly zero rather than staying flat.
3. **A large chunk of `transcriber.py` becomes unnecessary.** The 0.5 s overlap,
   `OVERLAP_TRIM_TOLERANCE_S`, `_matches_emitted_suffix`, and most of `SessionTextState`
   exist *only* to repair the damage fixed windowing does. Utterance boundaries make
   overlap unnecessary, and roughly 60 lines of the most delicate code in the backend can
   go.
4. **The gate gets cleaner input**, which improves claim extraction quality for free.

Cost: latency becomes utterance-bound rather than fixed. Median won't move much (most
utterances are short); the tail grows, which the 15 s cap bounds. Given verdict latency
is currently ~7 s end-to-end, this is acceptable.

Risk: this is a rewrite of the *most-tested* part of the backend (`test_transcriber.py`,
32 tests). **Put it behind `STT_SEGMENTATION=window|vad` in `Settings` and keep the
windowed path.** That also gives you an A/B harness for measuring the accuracy claim
rather than asserting it.

**Tier 3 — speech-vs-music discrimination (measure before you build).**

The tempting move is a small audio classifier (YAMNet-class, ~4 MB) in front of Whisper.
Before spending that: **instrument first.** `_drop_reason` (`transcriber.py:296-324`)
already computes exactly why every segment was rejected and throws it away at
`logger.debug`. Count those reasons per session and you will know, with real numbers,
whether music leakage is a 2% problem or a 40% problem. If it's small, tightening
`MAX_NO_SPEECH_PROB` and growing the blacklist is the whole fix and costs nothing.

### 1.3 Effort and payoff

| | Effort | Payoff |
|---|---|---|
| Tier 1 + gap frame | ~1 day | Bandwidth; prerequisite for any hosted deployment |
| Tier 2 (flagged) | ~3–4 days | 30–50% STT CPU cut on typical content, near-100% on music; better ASR; net code *deletion* |
| Tier 3 instrumentation | ~2 hours | Tells you whether Tier 3 proper is worth building |

**Recommendation: Tier 1 + gap frame, then Tier 2 behind a flag, then measure.** Tier 2
is the highest-value single change in this report for the existing product.

---

## 2. Local inference

### 2.1 The seam is already there

`llm_provider.py` is a factory with four functions that each branch on
`settings.llm_provider`, lazily importing the provider module. `ClaimGate` and
`FactChecker` are ABCs with one and two abstract methods. **Adding a third provider
touches no pipeline code at all** — not `pipeline.py`, not `ws.py`, not `models.py`.

What it does touch:

- `config.py:60` — `llm_provider` Literal gains `"local"`.
- `config.py:124` — `is_configured` must special-case local (no key exists; readiness
  means "the base URL answers").
- `llm_provider.py:76-149` — four `if provider == "openrouter"` branches. At three
  providers this pattern is at its limit; **replace it with a registry dict**
  `{name: ProviderSpec(client, gate, checker, close)}` while you're in there.
- `setup.py` — the validation probe gains a local branch: `GET {base_url}/models`.

### 2.2 Split the problem — gate and verify are completely different jobs

This is the key insight and it should drive the design:

| | Calls/hour | Needs web search | Difficulty |
|---|---|---|---|
| **Gate** | ~300 (one per 12 s) | No | Low — structured extraction from short text |
| **Verify** | ~5–20 (after dedupe + RPM cap) | **Yes** | High — judgment over conflicting sources |

**The gate is where all the volume is and none of the difficulty. It is the ideal local
target.** Moving it local:

- eliminates ~95% of all API calls;
- **completely removes the OpenRouter free-tier daily cap problem** — the README
  correctly identifies the 50-requests/day limit as the thing that "dies in minutes," and
  it dies because of the gate;
- **gets *better* JSON reliability than the hosted path, not worse.** All the
  strict-schema latching machinery in `llm_openrouter.py` (the 400/404/422 permanent
  latch, the 503 time-bounded latch, the no-reasoning retry — ~150 lines) exists because
  free hosted models fail structured output unpredictably. llama.cpp GBNF grammars,
  vLLM guided decoding, and Ollama's `format: <json-schema>` are *hard* constraints. The
  local gate can drop the entire fallback ladder. **This is the rare case where the local
  path is simpler than the hosted one.**

**Verify locally is a genuinely bigger project**, because grounding is the hard part.
Citations currently come exclusively from provider metadata (`_extract_citations`,
`llm_openrouter.py:707-730`). A local verify path needs its own retrieval stack:

```
app/search.py    query → SearXNG | Brave | Tavily | DDG
                       → fetch top N → trafilatura extract → truncated chunks
```

Roughly 300–400 lines. Two things make it more attractive than it sounds:

1. **Provenance gets strictly better.** Sources become the URLs you actually fetched and
   fed to the model, rather than annotations you're trusting the provider to have
   grounded on. `_enforce_invariants` (no citations ⇒ downgrade to UNVERIFIED) keeps
   working unchanged and becomes *more* meaningful.
2. **It kills the dominant marginal cost.** The business analysis puts web search at
   ~$0.005/check and calls it "the dominant marginal cost; token costs are noise."
   Self-hosted SearXNG is $0. Brave and Tavily have usable free tiers.

The risk is quality: an 8B model reading five scraped pages will be measurably worse at
MISLEADING calls — the label that most needs judgment — than `gemini-3.5-flash` with
native grounding. **Do not switch verify blind. Measure it** (see §3.5 — this is exactly
what the eval harness is for).

### 2.3 Concrete recommendation: per-stage providers

Add `gate_provider` and `verify_provider` settings and let `LLMRuntime` hold two clients.
The factory already builds gate and checker independently, so this is a small change with
a large payoff:

```
GATE_PROVIDER=local        # 300 calls/hr, free, no rate limit, grammar-constrained JSON
VERIFY_PROVIDER=openrouter # 5-20 calls/hr, native grounding, keeps quality
```

**That hybrid is the sweet spot** and I'd make it the documented default recommendation
for anyone with a GPU. Full-local becomes a supported configuration for people who want
it, not the only way to benefit.

### 2.4 Proposed module layout

```
app/llm_openai_base.py   # shared OpenAI-SDK transport + JSON-tolerant parse
app/llm_openrouter.py    # + web plugin, require_parameters, annotations  (existing)
app/llm_local.py         # + configurable base_url, grammar JSON, no extras
app/search.py            # retrieval for the local verify path
```

Extracting `llm_openai_base.py` is optional but worthwhile — Ollama, llama.cpp
(`llama-server`), LM Studio, and vLLM **all** expose OpenAI-compatible `/v1` endpoints,
so the transport is genuinely shared. Do not try to reuse `llm_openrouter.py` directly;
it is saturated with OpenRouter-specific `extra_body` and that coupling is correct.

### 2.5 Hardware guidance to document

| Setup | Gate | Verify | STT |
|---|---|---|---|
| CPU only (8-core) | 4B-class instruct, q4 — ~3–6 s/call, fits the 12 s budget | hosted | `distil-small.en` int8 (current default) |
| 8–12 GB GPU | 8B–12B q4 | local + self-hosted search (~10–25 s) | `distil-large-v3` fp16 on the same GPU |
| 24 GB GPU | 20B–30B MoE | local, comfortable | `large-v3` |

Also worth a README note regardless of local LLM plans: **`WHISPER_DEVICE=cuda` with
`WHISPER_COMPUTE_TYPE=float16` is a large, free win** for anyone with a GPU, and it makes
the §1 VAD work less urgent for those users.

---

## 3. Analytics — and the leaderboard question

### 3.1 The honest answer on the leaderboard first

You asked about a misinformation leaderboard. I'd build the analytics and **not** build
the public leaderboard, for three reasons that are worth stating plainly:

1. **The obvious metric is statistically invalid.** Raw FALSE count is approximately
   `watch_hours × claim_density × false_rate`. A leaderboard ranked on it ranks *how long
   you left the extension running on someone*, with claim density and mic quality as the
   next largest terms. Bad audio produces garbled transcripts, which produce malformed
   claims, which verify as FALSE — so **a streamer with a cheap microphone outranks a
   liar.**
2. **The sample is one person's viewing habits.** The extension only ever sees streams
   *you* watched. There is no denominator, no coverage, no comparability between
   channels.
3. **Published, it is a named accusation against a real person**, generated by an
   automated pipeline with an unmeasured error rate and no appeal process. The business
   analysis already flags both "public wrongness is expensive" and "weaponization in
   drama" as top risks (§6) — a leaderboard is the maximally-exposed version of both,
   and unlike the chat bot it has no consenting broadcaster in the loop.

**What I'd build instead delivers most of what makes a leaderboard appealing, with none
of that:**

- **Personal dashboard** (private, local): what you watched, what was checked, what the
  distribution looked like, what it cost.
- **Per-channel cards** (private by default): rate-based, with denominators and sample
  floors — an actual accuracy signal rather than a body count.
- **Per-session reports**: shareable, timestamped, source-cited. This is exactly the
  artifact the business analysis §4c already identifies as the plausible B2B product, and
  it's the version a streamer might consent to and even promote.

If you still want a public leaderboard after that, the conditions that would make it
defensible are: opt-in channels only, **rate**-based metrics with an n≥30 floor and
Wilson confidence intervals, UNVERIFIED excluded from the numerator entirely, a human
review/appeal path, and a published methodology. That's your call, and none of the
groundwork below is wasted if you make it.

### 3.2 Prerequisite: session identity

`ClientHello` has no idea what it's listening to. Fix:

```python
class ClientHello(BaseModel):
    ...
    platform: Literal["twitch","youtube","kick","rumble"] | None = None
    channel: str | None = None      # slug
    stream_title: str | None = None
```

Optional fields ⇒ wire-compatible. The content script **already** resolves the platform
(`PLATFORM_ADAPTERS`, `content.js:107-176`) and has `location.pathname` for the channel
slug; it just never sends it. The plumbing is content → service worker → offscreen →
hello, all of which already carry payloads.

### 3.3 Persistence: SQLite, and it should be small

Write volume is on the order of 10–30 rows per hour. `aiosqlite` (or plain `sqlite3` on
the existing thread pool) is more than sufficient; the README's "no database" line becomes
"one SQLite file next to `.env`."

```
sessions  (id, platform, channel, title, started_at, ended_at,
           speech_seconds, audio_seconds, gate_calls, verify_calls, est_cost_usd)
claims    (id, session_id, text, normalized, topic, check_worthiness,
           stream_time_s, gated_at, outcome)          -- see funnel below
verdicts  (id, claim_id, label, explanation, checked_at,
           used_fallback, latency_ms, provider, model)
sources   (verdict_id, url, domain, title, rank)
feedback  (verdict_id, rating, corrected_label, note, created_at)
```

Two design notes:

- **Write fire-and-forget from a background task.** The verify loop must never block on
  disk, and a persistence failure must be a logged warning, never a session kill — that
  matches the existing "one failure never kills the session" philosophy in
  `pipeline.py`.
- **`claims.outcome` is the most valuable column in the schema.** Store *rejected* claims
  too, with why: `below_threshold | topic_skipped | duplicate | queue_dropped |
  verified | verify_failed`.

### 3.4 The funnel is the analytics that actually pays for itself

Right now every stage of claim attrition is a `logger.info` and nothing else
(`_filter_claims`, `pipeline.py:378-418`). Persist it and you get:

```
speech seconds → words gated → claims proposed
   → dropped below sensitivity threshold
   → dropped by topic filter
   → dropped as duplicate
   → dropped by full verify queue
   → verified → label distribution
```

This turns sensitivity tuning from vibes into arithmetic. Today there is no way to answer
"is `medium` throwing away good claims or saving me money?" — with the funnel it's one
query. It is also, not incidentally, the best debugging tool the project could have.

### 3.5 The measurement gap: nothing knows if a verdict was right

**This is the highest-leverage item in the entire report**, and it's small.

Add a 👍 / 👎 / "wrong label" control to each toast and history entry in `overlay.js`,
POST it to `/feedback`, store it. That gives you:

- **An eval set that grows by itself**, which is exactly what §2 needs before anyone can
  responsibly move verification to a local model.
- **A measurable version of the business analysis's own kill criterion** ("≤1
  disputed-wrong public verdict per 10 streams") — currently unmeasurable.
- The ability to compare gate models, verify models, sensitivity thresholds, and the §1
  windowing change against something other than intuition.

Pair it with `backend/scripts/eval_claims.py`: a fixed labeled claim set run through
gate + verify, reporting precision/recall per label and per topic. The README currently
documents a manual eval of *four* claims as the basis for the default model choice. That
was a reasonable thing to do once; it is not a thing to keep doing.

### 3.6 Metrics that survive scrutiny

For a per-channel card:

- **Claims checked per streamed hour** — claim density, a channel property, honest.
- **Of *adjudicated* claims (TRUE/FALSE/MISLEADING only): % FALSE, % MISLEADING** — with
  Wilson CIs and an n≥30 floor before displaying anything.
- **UNVERIFIED share** — presented as a *coverage* metric of the pipeline, never as a
  property of the streamer.
- **Median claim→verdict latency**, **topic mix**, **estimated cost**.

### 3.7 Delivery surface

Serve it from FastAPI: `GET /stats/*` plus a static `/dashboard` page. No build step
(matching the project's ethos), it can query SQLite directly, and it survives extension
reloads — unlike anything rendered in the options page. The extension's history panel
stays as the live in-session view it already is.

---

## 4. Self-contradiction tracking

This is a fourth product concept, not a bug fix, and it's worth stating plainly why it's
structurally different from everything else in the pipeline: grounded verification asks
*"is this true, relative to the world"*; this asks **"is this consistent with what this
person already told you"** — a question web search cannot answer no matter how good the
model is, because the answer isn't published anywhere. Exa and Google Search index the
public web, not a transcript of what this streamer said 40 minutes ago. This is a
first-party memory problem, and it's also **free of the pipeline's single largest cost** —
no grounded search call is needed to compare two pieces of text you already have.

### 4.1 Hard dependency: this needs Phase 1

Contradiction detection needs a growing store of "what this channel has claimed," which
means it needs exactly the persistence and channel-identity work already recommended in
§3 (the `claims` table, plus `platform`/`channel` on `ClientHello`). Worth calling out as
a second, independent argument for building that phase first: it isn't just a dashboard's
data source, it's a substrate two different features now need.

### 4.2 Architecture

Retrieval is the part to get right, and it's cheap: this doesn't need an LLM call at all
until a plausible pair has been found.

1. **Embed every gated claim** (not just ones that pass the fact-check filters — a
   contradiction between two low-stakes personal claims is still a contradiction, and the
   gate's own hard exclusions already keep this to real assertions, not opinions). A
   small local sentence-embedding model (MiniLM-class, ~80 MB, CPU-friendly) run on the
   same executor pattern as Whisper is more than sufficient — no LLM round-trip needed
   here.
2. **Compare against the same channel's history.** Brute-force cosine similarity over the
   last few hundred claims is sub-millisecond in numpy; store the vector as a BLOB
   alongside the claim row.
3. **Only send the LLM a judgment call over the top-k candidates** (say cosine > 0.55,
   k ≤ 3): "do these two statements from the same speaker logically contradict each
   other?" — a flat structured schema (`contradicts: bool`, `confidence:
   low|medium|high`, `explanation`), no web search, no citations. This is close to a pure
   NLI task and is a strong first candidate for the **local gate model** from §2: high
   potential volume, zero search cost, no reason to pay a hosted provider for it.

```python
class ContradictionFrame(BaseModel):
    type: Literal["contradiction"] = "contradiction"
    current_claim: str
    prior_claim: str
    prior_claimed_at: str       # ISO timestamp of the earlier claim
    confidence: Literal["low", "medium", "high"]
    explanation: str
```

A new frame type, not a repurposed `VerdictFrame` — it answers a different question and
deserves its own visual language in the overlay, not the TRUE/FALSE/MISLEADING/UNVERIFIED
palette.

### 4.3 The failure mode is the whole risk, so design against it explicitly

An eager contradiction detector is worse than none. "I said pizza was my favorite, now I
said I love tacos" is not a contradiction; changing your mind, joking, or being sarcastic
is not lying. The doctrine has to mirror the one already in `fact_checker.py`:
**default to no flag.**

- Require the pair to be about the same concrete entity or quantity, not merely the same
  topic (retrieval finds the same topic; judgment must find an actual logical clash).
- Weight direct negation ("I've never...", "always...", "first time...") higher than a
  plain factual delta — genuine opinion drift over a multi-hour stream shouldn't count.
- Confidence below `high` never surfaces as a toast — the same idea as the existing
  sensitivity threshold, just on a different axis.

### 4.4 Split same-session from cross-session — they carry very different risk

- **Same-session** ("earlier this stream you said X, just now you said Y") is bounded,
  timestamped, and the viewer can scroll back and check it themselves — closer to a
  highlight than an accusation. **Ship this first.**
- **Cross-session** (comparing against everything a channel has ever said) needs durable
  identity across streams — technically trivial once §3.2's `channel` field exists — but
  is a much bigger reputational tool, in the same category the business analysis already
  flags for the chat bot and the leaderboard idea (§3.1): it's most useful exactly where
  it's most dangerous. Treat it as a separate, later, opt-in feature, not a natural
  extension of the first.

### 4.5 Effort

| | Effort | Depends on |
|---|---|---|
| Same-session contradiction (embed + retrieve + judge + new frame + overlay) | ~1 week | Phase 1 persistence + channel identity |
| Cross-session contradiction | +2–3 days | Same-session shipped; explicit per-channel opt-in |

---

## 5. Analyzing video frames

The instinct behind this is right, and it targets a real blind spot: audio-only
transcription cannot see a claim that's *about* something on screen — a chart, a
screenshot, a stat overlay, an article someone's reading from. That is exactly the Just
Chatting / news-reaction content the business analysis identifies as the target category,
so the miss is plausibly concentrated where it matters most.

It's also the largest, most speculative, and highest-risk item in either round of this
report — worth saying before the design, not after.

### 5.1 Measure before building anything

Same instinct as §1's Tier 3: this is cheap to estimate and expensive to build blind.
Once the funnel logging from §3.4 exists, add one more counter: how often a gated claim's
source text contains a deictic/visual cue ("look at this," "as you can see," "this
chart," "on screen"). A day of keyword-matching against real logs tells you whether this
is 2% of content or 20% before any capture pipeline gets written.

### 5.2 If it clears that bar: architecture

- **Capture.** `chrome.tabCapture` already supports video — today `audio_capture.js:75-82`
  only requests `audio` in the `getUserMedia` constraints. Extending the graph means
  rendering the video track to an offscreen `<canvas>` and sampling a low-resolution JPEG
  (480p is enough for reading a chart or headline).
- **Gate the capture, don't stream it continuously.** Fire a frame grab only when the
  transcript reaching the gate contains the same deictic cue from §5.1 — this bounds both
  bandwidth and (the real cost) vision-model tokens to the claims that plausibly need an
  image at all.
- **Transport.** No new binary protocol needed at this frequency: reuse the existing JSON
  text-frame dispatch (`_handle_text_frame`, `pipeline.py:310-339`) with a new
  `{"type":"frame","image_b64":...}` message, and keep a tiny ring of the last 2–3 frames
  with timestamps in the session pipeline so the verify stage can grab whichever is
  temporally closest to the claim.
- **Attach at verify, not gate.** Verify runs 5–20 times/hour versus the gate's ~300 —
  it's the affordable place to pay vision-token cost, and it's also the stage that
  actually needs the evidence to decide TRUE/FALSE/MISLEADING. This needs a
  vision-capability flag on the configured model: Gemini is natively multimodal, but not
  every OpenRouter text model accepts images, so `Settings` needs to know which models
  can take one.
- **Extend the anti-hallucination invariant to images.** `_enforce_invariants`'s "no
  citations ⇒ UNVERIFIED" rule needs a visual counterpart: if the frame isn't clearly
  legible enough to support or refute the specific claim, it must not move the verdict
  off UNVERIFIED. This matters more for images than text, because there's no
  `url_citation`-style mechanism to fall back on — a model confidently misreading a
  small, blurry chart is a harder failure to catch than a fabricated URL.

### 5.3 Privacy is a materially bigger ask than audio, and needs its own controls

Screen content routinely contains usernames, chat messages, donation alerts with real
names, or other viewers' webcams — categories of exposure the audio-only design never had
to think about. Concretely:

- A **separate, explicit opt-in toggle**, never bundled into the existing Start flow.
- **No frame persistence** — pass the image straight through to the single LLM call and
  discard it; unlike claims and verdicts (§3.3), frames should never land in SQLite.
- Settings copy that says exactly what gets captured and when, not a generic
  camera-permission-style prompt.

### 5.4 Sequencing

This is explicitly the last thing to build across this report — after Phase 1 justifies
itself twice over (analytics *and* contradiction tracking need it), and only after the
one-day measurement in §5.1 says the miss rate is worth the privacy and cost surface.

---

## 6. Other findings

Ranked by value-to-effort, from the full read.

**6.1 — No cost or quota visibility anywhere.** The system spends real money per
verification and exposes zero counters. Given the README identifies the OpenRouter free
daily cap as the practical killer, a live "checks today: 37 · ~$0.19" readout in the
popup would prevent the single most common failure mode. `/healthz` is the natural place
for the counters. *Small, high value, and it falls out of §3 for free.*

**6.2 — Single-session preemption blocks the documented MVP.** `current_pipeline` is a
module global (`ws.py:38`). Correct for a viewer extension; it makes the business
analysis's §7 multi-channel bot MVP structurally impossible. Making it a dict keyed by
session id is contained (`ws.py` plus the duck-typed `debug.py` contract) and would clean
up the `getattr(ws, "current_pipeline")` coupling in `debug.py:34-49` at the same time.

**6.3 — Cross-session verdict caching.** Once SQLite exists, cache verdicts keyed by
normalized claim with a topic-aware TTL (short for `politics`/`money`, long for
`history`/`science_tech`). `is_duplicate` (`fact_checker.py:113-127`) already does exactly
this fuzzy matching, but the memory dies with the session — and streamers repeat
themselves across streams, not just within one. Direct cost reduction on the dominant
marginal cost.

**6.4 — No latency instrumentation.** Nothing times anything. `latency_ms` on the verdict
(gate-completed → verdict-emitted) costs two `time.monotonic()` calls and makes every
performance claim checkable.

**6.5 — Source quality is prompt-enforced, not code-enforced.** The verify prompt asks for
"reputable sources" and nothing verifies it. This is the one place the codebase's own
excellent doctrine — *invariants in code, not in prompts* — isn't applied. A domain
tier list applied in `_enforce_invariants` (downgrade a TRUE/FALSE whose citations are
entirely content farms or fan wikis) fits the existing structure exactly and is maybe
40 lines.

**6.6 — The extension has zero tests.** ~2,900 lines of JS. The pure-logic parts —
`getEnabledTopicSlugs`, the settings merge, `encodeInt16Le`, the reconnect backoff — are
testable with Node's built-in test runner and no build step. Worth doing before the §1
worklet changes, which are exactly the kind of numeric code that regresses silently.

**6.7 — No CI.** 254 tests and nothing runs them. A GitHub Actions workflow running
`pytest -m "not slow"` plus `black --check` is ~20 lines. `black` is already a dependency
but is unenforced; adding `ruff` would be cheap.

**6.8 — English-only.** `distil-small.en` plus an English gate prompt and an English
hallucination blacklist. The code already handles the `.en` language pin correctly
(`transcriber.py:200`), so multilingual is mostly a config-and-prompt matter — a real
audience expansion if you want it.

**6.9 — `used_fallback` crosses the wire and is never shown.** The overlay could mark
verdicts that came through the degraded parse chain. Cheap honesty signal.

**6.10 — Minor security note.** DNS-rebinding is already handled (`TrustedHostMiddleware`,
`main.py`), which is more than most local backends do. The remaining exposure is that the
CORS regex admits *any* installed extension (`chrome-extension://[a-z]{32}`,
`main.py:29-31`), and `POST /setup/credentials` writes `.env` while `/debug/text` spends
money. On a single-user local box this is low severity, but a first-run shared token
would close it and would become mandatory before any hosted deployment.

---

## 7. Suggested sequencing

Ordered so that each phase makes the next one measurable.

**Phase 1 — Instrument (≈1 week).** SQLite + session identity + the claim funnel +
latency + cost counters + the feedback control. Nothing here is glamorous, and everything
after it depends on it. *Ships: the personal dashboard, which is a real feature on its
own.*

**Phase 2 — Voice filtering (≈1 week).** Client energy gate + the gap frame, then
VAD utterance segmentation behind `STT_SEGMENTATION`. Phase 1 means you can *prove* the
accuracy and CPU claims instead of asserting them, and the drop-reason counters tell you
whether Tier 3 is worth building.

**Phase 3 — Local inference (≈1–2 weeks).** Provider registry refactor →
`gate_provider`/`verify_provider` split → local gate with grammar-constrained JSON. Stop
there and evaluate. Add `app/search.py` and the local verify path only if the eval harness
from Phase 1 says the quality holds.

**Phase 4 — Analytics surface (≈1 week).** `/stats/*` + `/dashboard`, per-channel cards
with proper denominators, session reports. Revisit the leaderboard question then, with
real data in hand about how often the pipeline is actually right.

**Phase 5 — Self-contradiction tracking, same-session only (≈1 week).** Builds directly
on Phase 1's schema and channel identity; the judgment call is a good first workload for
whichever local model Phase 3 stood up, if it did. Cross-session stays a later, opt-in
feature (§4.4).

**Phase 6 — Video-frame analysis, conditional (≈2–3 weeks if it proceeds).** Gate the
entire phase behind the one-day deictic-cue measurement in §5.1; build the capture,
transport, and vision-verify path only if the miss rate justifies the privacy and cost
surface it adds.

The through-line: **Phase 1 is the unlock, twice over.** Voice filtering and local
inference are quality/cost tradeoffs the project currently has no instrument to read;
contradiction tracking and frame analysis are new capabilities that need Phase 1's
persistence layer to exist at all before they can be built or measured.
