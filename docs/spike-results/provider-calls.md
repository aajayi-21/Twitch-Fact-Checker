# Spike S2 — Provider calls from inside the extension: results

*Measured 2026-11-11 on branch `spike/provider-calls`. This is Phase 0, spike S2 of
[`viewer-app-build-guide.md`](../viewer-app-build-guide.md): "does each provider return a
web-grounded verdict as strict JSON from an extension context?"*

## 1. Answer

**Yes for the two providers tested live; the other two are built and unit-tested but have
not made a real call.**

| Provider | Live result | What is still unproven |
|---|---|---|
| **Anthropic** (direct) | Passed everything, three runs | — |
| **OpenRouter** | Passed everything with three different model families | — |
| **OpenAI** (direct) | Not tested — no key supplied | Whether web search and strict JSON combine in one call; the default model name |
| **Gemini** (direct) | Not tested — no key supplied | The one-call-or-two question this spike was meant to settle |

Two findings matter beyond the spike:

- **The backend's default OpenRouter model no longer exists.**
  `inception/mercury-2.5-preview` returns 404 ("no provider serves this model anymore"),
  and OpenRouter lists no Mercury model at all. Anyone running the backend on OpenRouter
  with default settings is currently broken. It needs a new default (§4.1).
- **Three of the four providers need no host permission.** Anthropic, OpenRouter and
  OpenAI all answer cross-origin requests from the extension. Only Gemini is blocked
  without one (§4.2).

## 2. What was tested

The real unpacked extension was loaded into Chrome 157, and every call was made from an
**offscreen document** — the context the browser engine will use — with the manifest as
committed (no provider host permissions).

For each provider with a key:

1. Key check with a free call, and the same call with a deliberately wrong key.
2. **Gate:** extract claims from the 128-word fixture transcript, which mixes four
   checkable claims with opinions, game talk, a prediction and a sponsor read.
3. **Verify:** three claims with a known right answer, one per label:

| Claim | Right answer |
|---|---|
| The Eiffel Tower is 450 meters tall. | FALSE |
| The speed of light is approximately 300,000 kilometers per second. | TRUE |
| Police are arresting protesters at the port of Dover right now. | UNVERIFIED (live, unverifiable) |

Prompts and schemas are the backend's own, exported by script (§5).

## 3. Results

### 3.1 Correctness

| Provider → model | Gate: expected claims found | Gate: junk extracted | Verdicts correct |
|---|---|---|---|
| Anthropic → Claude Haiku 5.5 (3 runs) | 4 of 4, every run | none | 9 of 9 |
| OpenRouter → Claude Haiku 5.5 | 4 of 4 | none | 3 of 3 |
| OpenRouter → GPT-6 Luna | 4 of 4 | none | 3 of 3 |
| OpenRouter → Gemini 3.5 Flash-Lite | 4 of 4 | none | 3 of 3 |

Every response was schema-valid JSON on the first attempt; no repair or retry was needed.
Every TRUE and FALSE verdict carried 3–5 sources taken from the search results.

### 3.2 Speed and cost per call

| Provider → model | Gate | Verify | Cost per fact-check |
|---|---|---|---|
| Anthropic → Claude Haiku 5.5 | 1.3–1.6 s | 2.9–4.6 s (1.0–1.9 s when it declines to search) | ≈ $0.011 (estimate) |
| OpenRouter → Claude Haiku 5.5 | 3.7 s | 1.7–3.6 s | $0.0075 (exact) |
| OpenRouter → GPT-6 Luna | 4.1 s | 2.9–4.7 s | $0.0074 (exact) |
| OpenRouter → Gemini 3.5 Flash-Lite | 1.4 s | 1.6–2.1 s | $0.0078–0.0081 (exact) |

- **The search fee is the cost.** On OpenRouter about $0.007 of each $0.0075 is the web
  search; tokens are a rounding error. On Anthropic the search is $0.01 and tokens add
  about a tenth of a cent. A gate call costs $0.0002–0.0011.
- OpenRouter's figures are exact, reported per request. Anthropic's is computed from the
  token counts and list prices.
- Anthropic's verify reads far more tokens (7,600–11,900) than OpenRouter's (2,100–4,800)
  because its search tool returns full result text into the prompt. At Haiku prices that
  is still about a tenth of a cent.
- **Anthropic skips the search for unverifiable claims** — it answered the live-event
  claim in one second with no search, so that check cost almost nothing. OpenRouter's
  plugin always searches first, so it pays the fee even when the answer is "can't be
  verified".

### 3.3 Other things confirmed

- **Prompt caching works from the extension.** On repeat gate calls, 2,959 of 3,227
  prompt tokens were read from Anthropic's cache.
- **Wrong keys are reported as an authentication problem** by all four providers
  (checked with fake keys, free).
- **Calls work from an offscreen document** with only the manifest already committed.

## 4. Findings that change the plan

### 4.1 The backend's default OpenRouter model is dead

`DEFAULT_OPENROUTER_GATE_MODEL` and `DEFAULT_OPENROUTER_VERIFY_MODEL` in
`backend/app/config.py` both point at `inception/mercury-2.5-preview`, which OpenRouter
has withdrawn. This is a live backend defect, not a spike issue; it was not changed here
because the choice of replacement is a product decision. Candidates measured above, all
supporting strict JSON:

| Model on OpenRouter | List price in / out per 1M | Verify time here |
|---|---|---|
| `google/gemini-3.5-flash-lite` | $0.30 / $2.50 | 1.6–2.1 s (fastest) |
| `anthropic/claude-haiku-5.5` | $0.10 / $0.50 | 1.7–3.6 s |
| `openai/gpt-6-luna` | $0.10 / $0.50 | 2.9–4.7 s |

The extension inherits the same default through the prompt export, so fixing the backend
default fixes both.

It also shows why the capability check matters: the backend consults OpenRouter's model
catalogue before sending strict-JSON requests. The extension module does not yet, and
should before it ships, so a withdrawn or incapable model produces a clear message rather
than a 404.

### 4.2 Host permissions: only Gemini needs one

| Provider | From the extension with no host permission |
|---|---|
| Anthropic | Works (real calls) — needs the `anthropic-dangerous-direct-browser-access` header, now confirmed |
| OpenRouter | Works (real calls) |
| OpenAI | Reaches the API (key check only) |
| Gemini | **Blocked** — the request never leaves the browser. Works once the host permission is granted |

So the store build can ship with **no provider host permissions for three of four
providers**, and request one optional permission only when a viewer adds a Gemini key.
The build guide assumed four.

### 4.3 Provider-supplied error text can contain key fragments

OpenAI's "incorrect key" message quotes the key back, partly masked. Error messages are
shown to the viewer and written to reports, so the shared HTTP helper now strips the key
that was sent, and anything key-shaped, before the message goes anywhere.

### 4.4 Design decisions made here

- **Raw HTTP, not vendor SDKs.** The extension has no build step and may not load remote
  code, so each SDK would have to be vendored as source. Four small modules over `fetch`
  with one shared error vocabulary is less to carry and to review. This is the one place
  the Anthropic module departs from Anthropic's own guidance to prefer its SDK; revisit if
  a build step is ever introduced.
- **One prompt source.** `backend/scripts/export_prompts.py` renders the backend's
  prompts, schemas and label rules into `extension/engine/generated/prompts.mjs`, and a
  backend test fails when that file is stale.
- **The backend's safety rules are ported and applied to every provider:** no sources →
  UNVERIFIED; evidence not rated "strong" → UNVERIFIED; a source URL the model merely
  wrote down, and the search never returned, is dropped.
- **Gemini's verify has a fallback built in** — strict JSON with search first, then a
  prompt-only JSON call, then a repair call — and reports which path it took, so the live
  test will show how often the cheap path works.

## 5. What was built

| Path | What it is |
|---|---|
| `extension/engine/providers/{anthropic,openai,gemini,openrouter}.mjs` | One module per provider, same surface: `validateKey`, `runGate`, `runVerify` |
| `extension/engine/providers/common.mjs` | HTTP helper, error kinds the UI can act on, key redaction, usage totals |
| `extension/engine/pipeline/verdict_rules.mjs` | The backend's verdict and source invariants |
| `extension/engine/pipeline/prompt_builders.mjs` | Fills in the exported prompt templates |
| `extension/engine/generated/prompts.mjs` | Generated from the backend — do not edit |
| `backend/scripts/export_prompts.py`, `backend/tests/test_prompt_export.py` | The export and its staleness test |
| `extension/spike/providers.*`, `offscreen_providers.*` | The live test pages |
| `extension/tests/providers.test.mjs` | 53 unit tests against simulated responses (127 in total) |
| `docs/spike-results/reports/providers-*.json` | The raw reports behind every number here (checked to contain no key material) |

Run it:

```bash
node --test "extension/tests/*.test.mjs"
node scripts/run_extension_spike.mjs --mode providers
```

The second command needs keys in a git-ignored `.env.spike` in the repo root, one
`OPENROUTER_API_KEY=` / `ANTHROPIC_API_KEY=` / `OPENAI_API_KEY=` / `GEMINI_API_KEY=` line
per provider; blank ones are skipped.

## 6. Not covered

- **OpenAI and Gemini direct APIs — no live call.** Their modules follow the current
  documentation and pass unit tests against simulated responses, which proves the code is
  self-consistent, not that the APIs accept it. Specific unknowns: the OpenAI default
  model (`gpt-6-luna`), whether OpenAI returns the consulted-sources list alongside strict
  JSON, whether Gemini accepts the schema as sent, and Gemini's single-call reliability.
  Both run with one command once keys are in `.env.spike`.
- **Three claims is a smoke test, not an accuracy evaluation.** It shows the plumbing and
  the label rules work; `backend/scripts/eval_verify.py` and the production verdicts are
  the place to compare answer quality across models.
- **Rate limits, retries and long sessions.** A handful of sequential calls; no bursts.
- **The model capability check** for OpenRouter (§4.1).
