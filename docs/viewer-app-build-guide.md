# Viewer App: Plan, Spec & Build Guide

*Written 2026-11-11, revised the same day (scope narrowed to API keys; GPU speech-to-text
added). Companion to
[`distribution-monetization-plan.md`](distribution-monetization-plan.md) (rung 3 of its
distribution ladder is "the extension in the Chrome Web Store"; this document is how to
make that rung worth shipping). Repo facts are measured from this codebase. Provider and
store facts are cited in §13 and were checked on the date above.*

**Goal.** A viewer installs the extension from a store, a control panel opens in a new
tab, they paste one API key, and they land on a home screen with settings, their past
results across Twitch / YouTube / Kick, and their usage. Speech-to-text runs locally on
their own GPU. No terminal, no `.env`.

**Scope decisions (2026-11-11).**

- **Sign-in is API keys only:** OpenAI, Anthropic, Gemini, OpenRouter. Nothing else is
  offered in the app (§3.7 records what was dropped and why).
- **Speech-to-text runs on the viewer's GPU** through WebGPU, with a CPU fallback (§8).

---

## 1. Verdict up front

The UI is about two weeks of work. What decides whether the experience is *streamlined*
is not the UI — it is this:

> **A store install delivers only the extension. Today the extension cannot do anything
> without the local Python backend** (speech-to-text, claim gate, verification and the
> history database all live in `backend/app/`). A viewer who installs from the store and
> pastes an API key still has nothing that works.

So the plan has one structural decision and everything else hangs off it:

**Add an engine that runs entirely inside the extension** ("browser engine": speech-to-text
on the viewer's GPU, direct calls to the viewer's own provider, history in IndexedDB).
Today's backend stays as an optional **companion engine** for developers and Firefox.

| Requirement | Answer | Where |
|---|---|---|
| Control panel opens in a new tab after install | Yes — one `onInstalled` hook plus `open_in_tab` | §4.1 |
| OpenAI API key | Yes. New provider (none exists today) | §3.2 |
| Anthropic API key | Yes. Exists in the backend; needs a JavaScript port | §3.3 |
| Gemini API key | Yes. Was removed from the backend in `06ec287`; comes back as a JavaScript provider | §3.4 |
| OpenRouter API key | Yes. Exists in the backend; needs a JavaScript port | §3.5 |
| Local speech-to-text on the viewer's GPU | Yes — WebGPU in the extension, CPU fallback, optional hosted fallback | §8 |
| Home screen: settings + past results with more control | Yes — new full-tab app, extension-side ledger | §4, §5, §6 |
| Works across YouTube, Twitch, Kick | Mostly there; **YouTube sessions currently record no channel**, which must be fixed | §6.2 |
| Usage tracking | Yes — per-call ledger; exact for OpenRouter, estimated for the other three | §7 |
| Chrome Web Store scaffolding | Build script, two manifests, privacy disclosures, CI publish | §10 |

The two highest-risk items, both settled by spikes before real work starts (§11):

1. **Can a typical viewer's GPU transcribe in real time in the browser?** Everything in
   the browser engine assumes yes.
2. **Does every provider return a web-grounded verdict as strict JSON in one call?**
   Gemini is the known doubt (§3.4).

---

## 2. Engines

### 2.1 The two engines

```
                    ┌────────────────────── extension ──────────────────────┐
                    │  app (new tab)   popup   content script (overlay)     │
                    │        │           │            ▲                     │
                    │        └────► service worker ───┘  ◄── ledger (IDB)   │
                    │                    │                                  │
                    │              offscreen document                       │
                    │        tab audio → 16 kHz PCM → [ENGINE]              │
                    └──────────────┬─────────────────────┬──────────────────┘
                                   │                     │
                  BROWSER ENGINE   │                     │  COMPANION ENGINE
            (new, store default)   │                     │  (today's backend)
        ┌──────────────────────────▼───┐         ┌───────▼────────────────────┐
        │ speech-to-text on WebGPU     │         │ ws://127.0.0.1:8710        │
        │ JS gate → JS verify          │         │ faster-whisper / Parakeet  │
        │ fetch → provider API         │         │ Python gate + verify       │
        │ (viewer's own key)           │         │ native GPU (CUDA/XPU/ROCm) │
        └──────────────────────────────┘         └────────────────────────────┘
```

Both engines emit the **same event vocabulary** to the service worker (the existing
`BACKEND_EVENT` frames: transcript, claim, verdict, error). Everything downstream — the
overlay, the ledger, the history and usage screens — is engine-agnostic. That is what
keeps this from becoming two products.

### 2.2 What each engine is for

| | Browser engine | Companion engine |
|---|---|---|
| Install | Store install only | Store install + `./backend/run.sh` |
| Audience | Viewers | Developers, power users, streamers |
| Speech-to-text | WebGPU in the extension (§8); CPU fallback; hosted fallback with an OpenAI or Gemini key | faster-whisper / Parakeet on a native GPU stack |
| Providers | OpenAI, Anthropic, Gemini, OpenRouter — keys | Anthropic and OpenRouter keys today |
| Features | Gate + verify + source tiers | Everything: Jev pre-screen, contradictions, on-screen (vision) claims |
| Browsers | Chrome / Edge 116+ | Chrome and Firefox |
| Privacy story | Audio never leaves the device | Audio never leaves the machine |

Firefox stays companion-only for now: it has neither `tabCapture` nor offscreen documents
(`extension/shared/capabilities.js`), and the browser engine needs an offscreen document
to hold the speech model.

The app is designed for the browser engine. The companion appears behind a single
"I run the local backend" switch in Settings and is never part of the default path.

### 2.3 Why not just ship the companion

Because the measured install is a developer install (a multi-GB virtualenv, `uv`, a
model download). A store listing that requires it would collect one-star reviews and, more
practically, **Chrome's reviewers could not exercise the extension at all** — which is a
rejection risk on its own (§10.6).

---

## 3. Provider connections (API keys only)

> **Status — spike S2 partly done, 2026-11-11.** The four provider modules exist
> (`extension/engine/providers/`). Anthropic and OpenRouter were tested live from inside
> the extension and passed every check; OpenAI and Gemini are unit-tested only, because
> no keys were available. Results:
> [`spike-results/provider-calls.md`](spike-results/provider-calls.md).

### 3.1 Summary

Every provider follows the same four-step flow: **paste key → free validation call →
grounded test call → store**. The test call matters: a key can be valid and still unable
to verify claims (no billing enabled, search not available on the plan), and the viewer
should learn that during setup, not on their first stream.

| Provider | Key header | Free validation call | Web-grounded verify | Hosted speech fallback | Status after S2 |
|---|---|---|---|---|---|
| OpenAI | `Authorization: Bearer` | `GET /v1/models` | Responses API, `web_search` tool | ✅ transcription API | Module written; **no live call yet** |
| Anthropic | `x-api-key` | `GET /v1/models` | `web_search` server tool | ❌ | **Passed live** |
| Gemini | `x-goog-api-key` | `GET /v1beta/models` | `google_search` tool | ✅ audio input | Module written; **no live call yet** |
| OpenRouter | `Authorization: Bearer` | `GET /api/v1/key` | `web` plugin | treat as ❌ | **Passed live** with three model families |

All four expose the same interface to the pipeline, mirroring `backend/app/llm_provider.py`:

```js
// engine/providers/<name>.mjs
export const validateKey = async (apiKey) => {/* free call; throws KeyRejected */};
export const probeGrounding = async (apiKey, model) => {/* one cheap grounded call */};
export const runGate = async ({apiKey, model, prompt, schema, signal}) => {/* → {claims, usage} */};
export const runVerify = async ({apiKey, model, prompt, schema, signal}) => {/* → {verdict, sources, usage} */};
export const listModels = async (apiKey) => {/* for the model picker */};
```

**Stage routing** already exists in the backend (`GATE_PROVIDERS` / `VERIFY_PROVIDERS` in
`backend/app/setup.py`): the gate and verify stages can use different providers. Keep
that in the app. The default is one key for both stages; an "Advanced" disclosure lets a
viewer with two keys run a cheap gate on one and verify on the other.

**Default models** are the provider's small, fast tier for both stages — the same class as
the backend's Claude Haiku 5.5 default. Ship defaults in a table inside the package,
populate the picker from each provider's live model list, and let the viewer override.

### 3.2 OpenAI

- **Gate:** Responses API with a strict JSON schema (`text.format`).
- **Verify:** Responses API with the hosted `web_search` tool. Search is billed per call
  on top of tokens (about $10 per 1,000 calls at the time of writing — verify on the
  pricing page before quoting it in the UI).
- **Speech fallback:** an OpenAI key also unlocks hosted transcription
  (`gpt-4o-mini-transcribe`, roughly $0.18 per streamed hour per third-party trackers).
- **Backend:** there is no direct OpenAI provider today — `llm_openrouter.py` uses the
  OpenAI SDK but points it at OpenRouter. A backend provider is only needed for companion
  parity and is optional (§11, Phase 4).

### 3.3 Anthropic

- **Gate / verify:** Messages API, strict JSON via `output_config.format`, the
  server-side `web_search` tool for grounding — exactly what `llm_anthropic.py` does.
- **Browser detail:** Anthropic rejects requests carrying a browser `Origin` unless the
  request sets `anthropic-dangerous-direct-browser-access: true`. That header exists
  precisely for bring-your-own-key apps; set it on every call. Confirmed in S2, along
  with prompt caching working from the extension.
- **Default model:** Claude Haiku 5.5 (`claude-haiku-5-5`), matching the backend.
- **No speech fallback** — Anthropic has no transcription API, so an Anthropic-only
  viewer depends on local speech-to-text.

### 3.4 Gemini

Gemini was the original provider and was removed in commit `06ec287` ("Gemini is no
longer used"). The deleted `backend/app/llm_gemini.py` (349 lines) and the Gemini-only
verify prompt builders are in git history and are a useful reference for the port, but
the API has moved since — treat them as notes, not code to restore.

- **Key:** from Google AI Studio. Send it in the `x-goog-api-key` header, **never** as a
  `?key=` query parameter (URLs end up in logs and history).
- **Grounding:** the `google_search` tool. On Gemini 3 and newer it is billed **per
  search query the model runs**, not per request: 5,000 free per month, then $14 per
  1,000, with a default cap of 1,500 per day per project.
- **Free-tier keys cannot ground.** Google's pricing page says the free tier does not
  include grounding, so a free key passes validation and then fails verify. The
  `probeGrounding` step must catch this and say "enable billing on this key in AI Studio"
  in plain words. A free key *can* still run the gate stage.
- **Default model:** the Flash-Lite tier (Gemini 3.5 Flash-Lite at the time of writing:
  $0.30 input / $2.50 output per million tokens).
- **Speech fallback:** Gemini accepts audio input, so a Gemini key can transcribe when
  the local GPU cannot keep up (§8.6).
- **Known risk — grounded strict JSON.** Google documents combining structured output
  with `google_search` on Gemini 3+, but developers report empty source metadata and
  truncated JSON on some current Flash builds. If the spike (S2) reproduces that, Gemini
  verify runs as **two calls**: a grounded call that returns prose plus citations, then
  an ungrounded structured-output call that shapes it into the verdict schema. That
  doubles the token cost of a Gemini verify and must be reflected in the usage estimate.
- **Citations:** take source URLs from the response's `url_citation` annotations, and
  pass them through the same source-tier table as every other provider.

### 3.5 OpenRouter

- **Gate / verify:** chat completions with the `web` plugin for grounding and
  capability-aware strict JSON, as `llm_openrouter.py` does. The backend consults
  OpenRouter's public model catalogue to decide which models support strict JSON
  (`openrouter_catalogue.py`); the JavaScript port needs the same check, because an
  arbitrary model slug may not.
- **Validation:** `GET /api/v1/key` (free) and `GET /api/v1/credits` for the balance.
  Verification needs a few dollars of credit for web search — check and say so.
- **Why it earns a "good default" badge in onboarding:** one key reaches OpenAI,
  Anthropic and Google models, and OpenRouter reports the exact cost of every request
  (§7), so the usage screen is precise rather than estimated. S2 confirmed all three
  families work through it, at about $0.0075 per fact-check.
- **The backend's default model there is gone.** `inception/mercury-2.5-preview` was
  withdrawn from OpenRouter; the backend and the extension both need a new default
  (candidates and timings are in the S2 results).

### 3.6 Credential storage rules

| Rule | Why |
|---|---|
| Keys live in `chrome.storage.local`, never `storage.sync` | Sync would copy secrets to Google's servers and every signed-in device |
| Call `chrome.storage.local.setAccessLevel({accessLevel: "TRUSTED_CONTEXTS"})` at startup | By default content scripts can read `storage.local`; a compromised page context must not reach keys |
| Provider calls happen only in the service worker / offscreen document | Content scripts run under the page's CSP and are the exposed surface |
| UI shows a last-4 hint only, never the key | Same hygiene `setup.py` already enforces (`key_hint`) |
| Keys go in headers, never in URLs | URLs are logged and cached |
| "Remove key" deletes it immediately and stops any running session | A visible, working off-switch is a store-review and trust requirement |
| Companion keys stay in `backend/.env` as today | One owner per secret; the extension holds no copy |

Non-secret settings stay where they are (`storage.sync`, `extension/shared/settings.js`).

### 3.7 Out of scope — decided, with the reason

Researched and deliberately not offered in the app. The backend keeps whatever it
supports today; the app simply does not surface these.

| Option | Why it is out |
|---|---|
| Anthropic `ant auth login` | The credential is a file on disk an extension cannot read; companion-only by nature. It bills API credits, so a pasted key is equivalent |
| Claude Pro/Max subscription login | Not permitted: Anthropic's terms bar third-party apps from offering Claude.ai login |
| OpenAI subscription ("Sign in with ChatGPT") | Real but a preview: Plus/Pro only, needs a local `127.0.0.1` listener, no transcription, web search not guaranteed. Revisit if it reaches general availability |
| OpenRouter one-click sign-in | Its OAuth flow just returns an API key, so it is compatible with this scope and could be added later as a convenience; not needed for v1 |
| Ollama | Local models have no web grounding, so they can only run the gate stage, and Ollama rejects extension origins by default. Remains available in the backend |

---

## 4. The app shell

> **Status — Phase 1 done, 2026-11-11.** The app exists at `extension/app/`: it opens in
> a new tab on install, and has the welcome flow, Home, AI provider and Settings. It runs
> on the existing backend, so only Anthropic and OpenRouter keys can be added today. The
> old `options/` page is gone; everything it did is in the app (the per-stage routing,
> Ollama, Jev and `ant` sign-in controls live under AI provider → Advanced, because they
> are features of the local backend). Verified by `scripts/run_app_e2e.mjs`, which walks
> the app in a real Chrome against a built-in fake backend, or a real one.
>
> Differences from the sketch below, decided while building:
> - **Preact + htm without signals**, and a 60-line store instead — fewer vendored files.
> - **No import map.** The extension's content security policy blocks inline scripts,
>   import maps included, so `scripts/vendor_ui_runtime.py` rewrites the vendored
>   modules' bare imports to relative paths.
> - **The welcome flow has four steps, not five**: how it works, local backend, API key,
>   try it. "Set up speech" joins when the browser engine does (Phase 3).
> - **History and Usage are not in the navigation yet** — they arrive with the ledger
>   (Phase 2). Home shows today's and all-time totals from the backend and links to its
>   analytics page meanwhile.

### 4.1 Opening in a new tab

Three small changes cover every entry point:

```jsonc
// manifest.json
"options_ui": { "page": "app/app.html", "open_in_tab": true }
```

```js
// background/service_worker.js
chrome.runtime.onInstalled.addListener(({reason}) => {
  if (reason === "install") {
    chrome.tabs.create({url: chrome.runtime.getURL("app/app.html#/welcome")});
  }
});
```

- With `open_in_tab: true`, the two existing `chrome.runtime.openOptionsPage()` call
  sites (the popup, and the service worker's `OPEN_OPTIONS` handler that the overlay
  link uses) open the new app unchanged, and so does "Extension options" in
  `chrome://extensions`.
- The toolbar popup stays the quick Start/Stop control and gains an "Open dashboard"
  link. If no key is connected, Start is replaced by "Finish setup".
- Focus an existing app tab instead of opening duplicates (`chrome.tabs.query` on the
  extension URL — no `tabs` permission needed for the extension's own pages).

### 4.2 Stack: stay build-free

Reuse what `backend/streamer/static/` already proves works: **vendored Preact + htm +
signals as ES modules**, hash router, no bundler. Copy `vendor/preact` and `vendor/htm`
into `extension/vendor/`. Reasons:

- Manifest V3 forbids remotely hosted code; vendored modules satisfy that by construction.
- Reviewers read unminified source faster, and Chrome disallows obfuscation outright.
- The existing "no external fetches" page test can be extended to the extension.

### 4.3 File layout

```
extension/
  app/
    app.html  app.css  app.mjs  router.mjs  store.mjs
    views/  welcome.mjs  home.mjs  history.mjs  usage.mjs  settings.mjs  providers.mjs
    components/  provider_card.mjs  verdict_row.mjs  filters.mjs  chart.mjs
  engine/                     # browser engine
    stt/      stt_worker.mjs  vad.mjs  models.mjs  gpu_probe.mjs  benchmark.mjs
    pipeline/ segmenter.mjs  gate.mjs  verify.mjs  source_quality.mjs  budget.mjs
    providers/ openai.mjs  anthropic.mjs  gemini.mjs  openrouter.mjs
    generated/ prompts.json  source_tiers.json     # exported from Python, §9.1
  shared/
    ledger.mjs                # IndexedDB store (§5)
    credentials.mjs           # §3.6
    pricing.json              # §7.2
  vendor/  preact/  htm/  onnxruntime-web/  ...
```

`options/` is deleted once its controls have moved into `views/settings.mjs` and
`views/providers.mjs`.

### 4.4 Screens

| Route | Purpose | Key content |
|---|---|---|
| `#/welcome` | First run | 5-step wizard (below) |
| `#/home` | Landing after setup | Key status, speech engine status, "now watching" session, today's checks and spend, last 5 verdicts |
| `#/history` | Past results | Filterable verdict timeline, channel table, session list, verdict detail (§6) |
| `#/usage` | Spend and volume | Period totals, charts, budgets (§7) |
| `#/settings` | App behaviour | Everything on today's options page, plus speech, budgets, data & privacy |
| `#/providers` | API keys | One card per provider, stage routing, model pickers |

**Welcome wizard**

1. **What this does and what it touches.** Plain-language disclosure: it listens to the
   tab's audio, transcription happens on this device, claim text is sent to the provider
   whose key you add, history is stored only in this browser. Explicit "I understand"
   before anything is captured. This single screen satisfies Chrome's
   prominent-disclosure rule (§10.4) and the EU AI Act notice from the distribution plan.
2. **Add an API key.** Four cards — OpenRouter, Anthropic, OpenAI, Gemini — each with a
   "Get a key" link to the provider's key page and a one-line cost expectation.
3. **Verify the key.** Validation, then the grounded test call, with a human-readable
   failure reason. The provider's host permission is requested here, on the click (§10.3).
4. **Set up speech.** GPU detection, model download with size shown up front and a
   progress bar, then a short self-test that reports a result the viewer can
   understand: "Speech runs on this device, 18× faster than real time ✓" (§8.4).
5. **Try it.** Run a canned claim ("The Eiffel Tower is 450 metres tall") through the real
   gate and verify path and render the real overlay card. The viewer sees a FALSE verdict
   with sources before they ever open a stream, and the whole chain is proved.

**Settings additions** beyond today's page: speech device and model (§8.5), per-stage
provider and model, budget caps, history retention (30 / 90 / 365 days / forever),
per-platform enable toggles, export all data, delete all data, companion switch.

---

## 5. The ledger (extension-side data)

### 5.1 Decision: the extension owns viewer history

Record every session, claim, verdict and usage event **in the extension's IndexedDB**, in
the service worker, as events arrive — regardless of engine. The companion's
`fact_checker.db` keeps doing its own job (prompt evaluation, calibration reports) and is
no longer the viewer's source of truth.

Why: one store means one history and one usage screen for both engines, it works with no
backend at all, and it survives switching engines. The alternative (reading `/stats/*`
when the companion is up and IndexedDB otherwise) is two code paths that will disagree.

### 5.2 Schema (IndexedDB database `factchecker`, version 1)

Mirrors the SQLite schema in `backend/app/db.py` so a one-time import is a straight copy.

| Store | Key | Indexes | Fields beyond the SQLite equivalent |
|---|---|---|---|
| `sessions` | `id` | `started_at`, `[platform, channel_id]` | `channel_id`, `channel_name`, `stream_url`, `engine` |
| `claims` | `id` | `session_id`, `gated_at`, `topic` | — |
| `verdicts` | `id` | `session_id`, `checked_at`, `label`, `[platform, channel_id]` | `sources[]` embedded, denormalised `platform` / `channel_id` / `topic` / `claim_text` for fast filtering |
| `feedback` | `verdict_id` | — | — |
| `usage_events` | auto | `ts`, `session_id`, `provider` | see §7.1 |
| `meta` | key | — | schema version, pricing version, import markers |

- Request the `unlimitedStorage` permission (the speech model alone is hundreds of MB)
  and call `navigator.storage.persist()` so the browser does not evict history or the
  model under disk pressure.
- Apply the retention setting in a daily `chrome.alarms` sweep.
- **Import:** when a companion is detected for the first time, offer "Import N past
  sessions" via the existing `GET /stats/sessions` and `/stats/sessions/{id}`.

### 5.3 Wire change for the companion

Add an optional `usage` object to verdict and gate frames (provider, model, input /
cached / output tokens, web-search count). `llm_anthropic.py` already reads all of these
in `_log_usage`; they are logged and then discarded. Old extensions ignore unknown fields,
so this is backward compatible.

---

## 6. History ("leaderboard with more control")

### 6.1 Views

| View | Shows | Controls |
|---|---|---|
| **Timeline** (default) | Every verdict, newest first: label chip, claim, channel, platform icon, time, sources | Filter by platform, channel, label, topic, date range, feedback state; full-text search over claim and explanation |
| **Channels** | One row per channel across platforms: checks, adjudicated n, TRUE / FALSE / MISLEADING share, watch time, cost | Sort by any column, filter by platform, minimum-n slider |
| **Sessions** | One row per watch session: channel, duration, checks, cost | Open → session report |
| **Verdict detail** | Full explanation, all sources with tier, transcript context, model, latency | 👍/👎 with correction, re-check, copy, delete |

Row-level and bulk actions: export filtered results (CSV / JSON), delete one verdict, a
session, a channel, or everything.

### 6.2 Cross-platform identity — the gap to fix first

Today `deriveSessionIdentity` in `background/service_worker.js` sets a channel **only for
Twitch and Kick** ("YouTube/Rumble URLs carry video ids, not channel slugs"), and
`fetch_channels` drops sessions with no channel. So YouTube viewing is invisible in the
channel table. To make "across YouTube, Twitch and Kick" true:

| Platform | `channel_id` | `channel_name` | Source |
|---|---|---|---|
| Twitch | login from path | same | URL (works today) |
| Kick | slug from path | same | URL (works today) |
| YouTube | `@handle`, else `UC…` id | owner name | Content script reads the owner link on watch/live pages and reports it in `CONTENT_READY`; re-resolve on SPA navigation |
| Rumble | channel slug | owner name | Content script, same mechanism |

Store `stream_url` and `stream_title` too, so a VOD or a past live stream is identifiable
later. Identity is `(platform, channel_id)` — never merge "the same creator" across
platforms automatically; offer a manual "these are the same person" link later if asked.

### 6.3 Keep the honest-metrics doctrine

`backend/app/stats.py` already states the rule: percentages are computed over
**adjudicated** verdicts only (TRUE / FALSE / MISLEADING), the sample size is always
shown, and no accuracy percentage appears below n = 30. UNVERIFIED is a coverage metric
of the pipeline, not a property of the channel. A sortable "who lies most" table built on
six verdicts is exactly the clip-against-the-product the business analysis warns about;
the more-control version must keep the floor. Avoid the word "leaderboard" in the UI.

---

## 7. Usage tracking

### 7.1 What to record

One `usage_events` row per provider call:

```
ts, session_id, engine, stage (stt | gate | verify), provider, model,
input_tokens, cached_input_tokens, output_tokens, web_searches, audio_seconds,
cost_usd, cost_source ("provider" | "estimate"), pricing_version
```

### 7.2 Where cost comes from

| Provider | Per-call data in the response | Account-level numbers | Approach |
|---|---|---|---|
| OpenRouter | **Exact cost**, plus tokens | `GET /api/v1/key` (usage, limit), `GET /api/v1/credits` | Use provider figures; show remaining credit |
| Anthropic | Tokens and web-search count | Usage/Cost API needs **admin** credentials | Estimate from `pricing.json`. Never ask viewers for an admin key |
| OpenAI | Tokens; count `web_search` calls from the output items | Org usage API needs an admin key | Estimate from `pricing.json` |
| Gemini | Tokens; count search queries from the response's search-call steps | None for a plain key | Estimate from `pricing.json`; track the monthly free-search allowance |
| Local speech | — | — | Free; count audio seconds and GPU/CPU mode |
| Hosted speech | Audio seconds | — | Estimate per minute |

`shared/pricing.json` is a versioned price table shipped in the package (prices cannot be
fetched at runtime without a new host permission and a new data flow). Mark every
estimated figure with "≈" and a tooltip; let the viewer override rates. This replaces the
backend's single flat `cost_per_verify_usd` constant with real token arithmetic.

Two pricing facts the table must model, because they change the answer materially:
Gemini bills **per search query**, and a single verify can run several; and some Gemini
Flash prices are scheduled to double on 1 January 2027, so entries need an
effective-from date.

### 7.3 Usage screen

- Period switch: today / 7 days / 30 days / all time.
- Totals: checks run, hours listened, spend, spend per hour watched.
- Breakdowns: by provider and model, by platform, by channel, by stage.
- **Budgets:** daily check cap and monthly spend cap, each with warn-at-80 % and
  hard-stop, **on by default** with a conservative daily cap. A viewer who leaves a debate
  stream on at high sensitivity overnight is the support ticket to prevent. The backend
  already has a daily counter (`count_checks_today`); the browser engine needs the same
  guard in the pipeline.

---

## 8. Local speech-to-text on the viewer's GPU

This is the foundation of the browser engine.

> **Status — spike S1 done, 2026-11-11.** Measured on the dev machine's Intel Arc GPU and
> on simulated no-GPU, software-GPU and single-thread machines. Local transcription works
> in stock Chrome. Full results, method and how to collect reports from other GPUs:
> [`spike-results/stt-gpu-benchmark.md`](spike-results/stt-gpu-benchmark.md). The
> sections below are updated with what was measured. **NVIDIA, AMD and Apple GPUs are
> still untested.**
>
> **Status — spike S3 done, 2026-11-11.** The same models were then run inside the real
> extension: tab audio captured by the existing capture code and transcribed live in an
> offscreen document, on CPU and GPU, with no audio lost. One manifest line was needed.
> Results: [`spike-results/stt-in-extension.md`](spike-results/stt-in-extension.md).

### 8.1 How a browser extension uses the GPU

**WebGPU** is the browser's GPU API. **ONNX Runtime Web** (the engine underneath
Transformers.js) has a WebGPU backend that runs speech models on it, and a WebAssembly
backend that runs the same models on the CPU. So one model file serves both paths:

```
offscreen document
  ├─ audio capture (existing): tabCapture → AudioContext → 16 kHz PCM worklet
  └─ Worker: stt_worker.mjs
        VAD (small CPU model) → speech segments only
        ONNX Runtime Web session
           ├─ "webgpu"  → viewer's GPU        (preferred)
           └─ "wasm"    → CPU, SIMD + threads (fallback)
        → transcript text → pipeline
```

- It runs in a **Worker inside the offscreen document**, so inference never stalls audio
  capture. The offscreen document already exists and already owns the audio graph
  (`extension/offscreen/`); Chrome allows only one per extension, so the model shares it.
- It works with **any GPU vendor** — Intel, AMD, NVIDIA, Apple — with no drivers,
  toolkits or installs. That is the whole advantage over the backend's CUDA / XPU / ROCm
  stacks (`install_stt_gpu.sh`), which stay available through the companion for anyone
  who wants the fastest native path.

### 8.2 Where WebGPU is available

| Platform | Status |
|---|---|
| Windows, macOS, ChromeOS — Chrome / Edge 113+ | Available |
| Linux — Chrome 144+ (January 2026) | Rolling out, **Intel Gen12+ first**, AMD and NVIDIA following. Confirmed on by default in stock Chrome 157 on the dev machine (Intel Arc, Wayland) |
| Firefox | Not used: the browser engine is Chrome-only (§2.2) |
| Any machine without a usable GPU | CPU fallback |

The development machine (Linux, Intel Arc integrated graphics) is in the first Linux
rollout group, which makes it a fair worst-case test bench: an integrated GPU on the
newest-supported platform.

### 8.3 Detecting the GPU — and why detection is not enough

Implemented in `extension/engine/stt/gpu_probe.mjs`, run in the wizard and at every
session start. It reduces whatever the browser reports to a plain description:

| Outcome | Meaning | Action |
|---|---|---|
| No `navigator.gpu` | Browser without WebGPU | CPU |
| No adapter | GPU or driver blocklisted, or the GPU process is down | CPU |
| Software adapter | A CPU pretending to be a GPU (SwiftShader, llvmpipe, WARP) | CPU — measured at 0.2× real time, slower than the real CPU path |
| Hardware adapter | A real GPU | Try it (§8.4) |

Three rules came out of the spike:

1. **Recognise software renderers by name as well as by flag.** Linux's `llvmpipe` reaches
   Chrome as an ordinary device.
2. **The engine opens the `GPUDevice` itself and hands it to the runtime.** Left to
   choose, the runtime ignored a request for a specific adapter and ran on a different
   GPU than the one probed. Owning the device also gives direct access to `device.lost`.
3. **A description is not a guarantee.** The Intel GPU advertises 16-bit shader support,
   and two of four 16-bit configurations still produced fast, wrong output. Whether a
   configuration works is decided only by the self-test below.

### 8.4 Models and the self-test ladder

**Tiers, from measurement** (Intel Arc integrated GPU; speed is multiples of real time on
short live-style utterances):

| Tier | Model | Runs on | Speed | Download | Notes |
|---|---|---|---|---|---|
| **Fast** (default) | Moonshine Base | CPU | 13–24× (11× on one thread) | 63 MB | Works on any machine. Its GPU build is no faster and four times the download |
| **Balanced** | Whisper Base (English) | GPU | 3–7× | 250 MB | Too slow on CPU for short utterances; limited headroom on an integrated GPU |
| **Accurate** | Parakeet CTC 0.6B | GPU | 18–41× | 643 MB | The clear GPU winner: one pass per utterance instead of one token at a time. Also 11× on CPU |
| not offered | Distil-Whisper Small | GPU | 3× | 489 MB | The backend's default model; passes, but Parakeet is faster and Whisper Base is smaller |
| not offered | Moonshine Tiny | — | — | — | Too inaccurate |

The earlier assumption that the GPU would be the default path was wrong for the Fast
tier: the GPU's value is that it **unlocks the Accurate tier**.

**The ladder** (`stt_plan.mjs`, `models.mjs`). Each model lists its configurations in
measured preference order — for example Whisper: GPU with a 16-bit encoder → GPU at
32-bit → CPU. After download, the wizard runs a bundled eleven-second clip with a known
transcript through each rung in turn and takes the **first that passes both tests**:

| Test | Pass mark | Why |
|---|---|---|
| Correct | Word error rate under 25 % | Checked first. Broken precision paths score 100 %; healthy ones score 0–10 % |
| Fast enough | At least 2× real time | Headroom for bursts of speech and a GPU shared with video |

| Result | Action |
|---|---|
| A rung passes | Use it; cache the verdict against the GPU's identity, the model version and the browser version, and re-test when any of them changes |
| GPU rungs fail, CPU passes | Use the CPU and say so |
| Nothing passes | "This device can't transcribe live audio locally" → offer the Fast tier, hosted speech (§8.6) or the companion |

Show the result in plain terms and store it, so the Home screen can display "Speech:
local · GPU (Intel) · 18× real time".

Keep watching during a session. If the queue of untranscribed audio grows (the backend's
equivalent is its audio high-watermark), step down a tier, and if that is not enough,
drop the oldest audio rather than fall ever further behind — a late fact-check is
worthless. Record every drop in the session, as `stt_drop_counts` does today.

**Crash guard.** A GPU-process crash takes video down in every tab, and Chrome withholds
the GPU after repeated crashes. Before the first GPU inference, persist a "GPU attempt in
progress" marker and clear it afterwards; if it is still set at the next start, do not
retry automatically — use the CPU and tell the viewer. Never retry a failed GPU rung in
the same browser session. (Seen during the spike when the test disk filled and Chrome's
shader cache could not write.)

### 8.5 Viewer-facing settings

| Setting | Options | Default |
|---|---|---|
| Speech engine | Local · Hosted (if an OpenAI or Gemini key is connected) | Local |
| Device | Auto · GPU · CPU | Auto |
| Model | Fast · Balanced · Accurate | Fast; the self-test decides whether the others are offered |
| GPU preference | High performance · Power saving | High performance (matters on dual-GPU laptops) |
| Re-run benchmark | button | — |
| Manage model | size on disk, re-download, delete | — |

### 8.6 Hosted fallback

For machines that fail the benchmark, hosted transcription is offered only when the
viewer already has a key that supports it:

| Key connected | Hosted speech available? | Rough cost |
|---|---|---|
| OpenAI | Yes — transcription API | ≈ $0.18 per streamed hour |
| Gemini | Yes — audio input | Billed as audio input tokens |
| Anthropic | No | — |
| OpenRouter | Treat as no until verified | — |

Hosted speech changes the privacy story — audio leaves the device — so it is opt-in,
never automatic, and the switch carries its own one-sentence disclosure. It also changes
the store disclosure (§10.4).

### 8.7 Packaging rules (Manifest V3)

- **All JavaScript and WASM ships inside the package.** Nothing executable from a CDN —
  including ONNX Runtime's `.wasm` files, which its defaults try to fetch remotely.
- **Model weights are data, not code**, and may be downloaded at first run. Host them at
  one fixed, versioned URL you control, pin the SHA-256 in the package and verify after
  download, cache in Cache Storage / OPFS. Download as a stream straight to storage;
  holding a several-hundred-MB model in memory first is a known out-of-memory failure.
- `"content_security_policy": {"extension_pages": "script-src 'self'
  'wasm-unsafe-eval'; object-src 'self'"}` is required — WASM will not compile without
  it (confirmed in S3) — and is now in `manifest.json`. No host permission is needed for
  the model download.
- CPU threads need a cross-origin isolated page (the `cross_origin_embedder_policy` and
  `cross_origin_opener_policy` manifest keys). S3 showed isolation does not disturb tab
  capture, but its only beneficiary is Whisper on the CPU (3× → 6.5×); the Fast tier runs
  at 17–18× either way. **Not adopted**: it changes how every extension page may load
  cross-origin resources, for a tier the plan does not depend on.
- A pinned runtime is fetched by `scripts/vendor_stt_runtime.py` with integrity checks;
  the library's own WASM cache must be switched off, because it re-serves the loader from
  a `blob:` URL that the extension's content security policy blocks.
- Voice-activity detection runs first and on the CPU, so the GPU only wakes for speech.
  Silence, music and ad breaks cost nothing — the same behaviour as the backend's
  Silero-VAD segmentation (`STT_SEGMENTATION=vad`).

---

## 9. Browser engine: pipeline

### 9.1 Pipeline port — and how to stop it drifting

The browser engine needs a JavaScript version of the gate → verify path. The Python side
is about 1,700 lines across `segmenter.py`, `claim_gate.py`, `fact_checker.py`,
`prompts.py` and `source_quality.py`, plus the 1,400-line orchestrator `pipeline.py`.
Port the minimum, and make the shared parts single-sourced:

| Piece | Approach |
|---|---|
| Prompts, JSON schemas | **Export from Python** to `engine/generated/prompts.json` with a script; a pytest fails if the checked-in file is stale |
| Source-tier table | Same: export `source_quality.py`'s table to `source_tiers.json` |
| Segmenter, dedupe, label rules | Hand-port; share golden fixtures (`tests/fixtures/`) and run the same cases through both implementations |
| Jev pre-screen, contradictions, vision | **Not in v1.** Companion-only features |

The invariants that must survive the port unchanged: UNVERIFIED when sources are not
conclusive, the source-tier floors, never showing a fallback-parsed verdict as
authoritative, and the 350-character explanation cap.

Prompts are currently written for Anthropic and OpenRouter. OpenAI and Gemini need their
own request shaping (tool declaration, schema dialect, citation extraction) but should
share the same instruction text; put the differences in the provider module, not in forks
of the prompt.

### 9.2 Network path

Provider calls go out from the offscreen document or service worker with `fetch`. With
the provider's host permission granted, extension contexts are not subject to the page's
CSP or CORS. Streamed responses are fine in both. Keep one in-flight verify per claim and
a small concurrency cap; reuse the backend's quota-cooldown behaviour on HTTP 429
(Gemini's daily search cap will surface this way).

---

## 10. Store scaffolding

### 10.1 Blockers to clear first

| # | Blocker | Note |
|---|---|---|
| 1 | **No `LICENSE` file** | Still true today. Blocks store submission. Distribution plan says MIT for `extension/` |
| 2 | **No privacy policy URL** | Required by the listing. A static page is enough |
| 3 | **No CI** | No `.github/`. Needed for reproducible store packages |
| 4 | **Reviewers cannot run it** | Needs the browser engine and a demo mode (§10.6) |
| 5 | **Name and branding** | "Live Stream Fact-Checker" is fine. Never put Twitch / YouTube / Kick marks in the name or icon |

### 10.2 Build step: two manifests from one source

The single cross-browser manifest is convenient for development but makes each browser
warn about the other's keys. Store packages should be clean:

```
extension/manifest.base.json        shared fields
extension/manifest.chrome.json      service_worker, tabCapture, offscreen
extension/manifest.firefox.json     background.scripts, gecko settings
scripts/build_extension.py          merge → dist/chrome/, dist/firefox/, zip both
```

The script also stamps the version from the git tag, excludes dev files
(`icons/make_icons.py`), and fails if any file references an `http(s)://` script URL.
Development keeps working from a generated `extension/manifest.json`.

Firefox needs one new key for AMO: `browser_specific_settings.gecko
.data_collection_permissions` has been mandatory for new submissions since November 2025.

### 10.3 Permissions for the store build

| Permission | Type | Justification to give reviewers |
|---|---|---|
| `tabCapture` | required | Capture the audio of the stream tab the user explicitly starts checking |
| `offscreen` | required | Hold the audio graph and the speech model, which a service worker cannot |
| `storage`, `unlimitedStorage` | required | Settings, local history, cached speech model |
| `activeTab` | required | Read the stream tab's URL and title on the user's click |
| `alarms` | required | Retention sweep and budget resets |
| Stream-site content scripts | required | Draw the verdict overlay on the four supported sites |
| `https://generativelanguage.googleapis.com/*` | **optional**, requested when a Gemini key is added | Send claim text to Gemini, which blocks cross-origin requests otherwise |
| Anthropic, OpenRouter, OpenAI | **none needed** | S2: all three answer cross-origin requests from the extension (OpenAI confirmed for the key check only) |
| Model host | **optional**, requested at model download | Fetch speech-model weights |
| `http://127.0.0.1/*`, `http://localhost/*` (+ ws) | **optional**, requested when the user turns on the companion | Talk to software on the user's own machine |

Keeping every network host out of the required permissions means the install prompt
mentions only the four stream sites, and each further grant happens at the moment its
purpose is obvious. Narrow host lists also shorten review; broad patterns lengthen it.
After S2, most viewers will never see a provider permission prompt at all.

### 10.4 Listing and privacy checklist

**Account**

- Register a developer account (one-time fee, $5), with 2-step verification on.
- New publishers get **two extension slots** (policy since August 2026).

**Store listing**

- 128×128 icon (96×96 artwork, 16 px transparent padding).
- 1–5 screenshots at 1280×800: overlay on a stream, onboarding, history, usage.
- 440×280 small promo tile (listings without one rank lower).
- Description that leads with what the viewer sees, per the distribution plan's
  "referee, not truth ministry" positioning.

**Privacy practices tab**

- *Single purpose:* "Fact-checks spoken claims in a live stream the user is watching and
  shows sourced verdicts over the player."
- *Permission justifications:* the table in §10.3.
- *Remote code:* "No" — true as long as §8.7's packaging rules hold.
- *Data usage:* disclose website content (stream audio processed on-device; transcript
  claim text sent to the user's chosen provider), authentication information (API keys,
  stored locally) and the locally stored watch history. If hosted speech ships, disclose
  that audio is sent to that provider when the user enables it. Certify limited use.
- *Privacy policy:* must match the in-product disclosure in substance.

**August 2026 policy changes that apply here**

- Every data practice must be **prominently disclosed in the product**, including ones
  central to the purpose — hence wizard step 1.
- Data handling changes after install must be proactively announced — add a "what
  changed" notice on `onInstalled` with reason `update` when a release changes data flow.
- No collection beyond the single purpose — so **no analytics or telemetry** in the
  store build.

### 10.5 Publishing pipeline

1. First upload is manual in the Developer Dashboard (the API cannot create items).
   Uploads now run automated install checks on the draft.
2. Publish **Unlisted** first — installable by link, still fully reviewed — for pilots.
3. Review is usually a few days; new developers, new extensions and powerful permissions
   push it toward weeks. Expect the slow path for the first submission.
4. Automate later releases with the Chrome Web Store API v2 from GitHub Actions on tag:
   `…/upload/v2/publishers/{publisher}/items/{id}:upload`, then `:publish`, then poll
   `:fetchStatus`. Authenticate with a service account or an OAuth refresh token stored
   as CI secrets.
5. Same zip to Edge Add-ons; Firefox zip through `web-ext sign` to AMO.

CI jobs to add: backend tests, the prompt-export staleness check, an extension unit-test
run, `build_extension.py`, and a manifest lint.

Package size: the bundled ONNX Runtime WASM files add tens of MB to the zip. That is
within store limits, but "the more code an extension contains, the more work it takes to
verify" — ship only the runtime variants actually used.

### 10.6 Give the reviewer something that works

A reviewer has no API key. Two measures:

- **Demo mode** in the wizard: "Try without a key" plays a bundled 30-second sample
  through real local speech-to-text, with pre-recorded verdicts in the real overlay. It
  exercises capture, GPU transcription and the UI with no account, and is also a good
  first-run experience.
- **Test instructions** in the submission: a low-limit provider key created for review,
  step-by-step, with a suggested public stream.

---

## 11. Build order

**Phase 0 — spikes (about a week, throwaway code).** Each one can change the plan.

| # | Question | Pass condition |
|---|---|---|
| S1 ✅ | Can a viewer's GPU transcribe live in the browser? | **Passed 2026-11-11** on Intel Arc, plus simulated no-GPU / software-GPU / single-thread machines ([results](spike-results/stt-gpu-benchmark.md)). Still open: reports from NVIDIA, AMD and Apple GPUs |
| S2 ◐ | Does each provider return a grounded, strict-JSON verdict from an extension context? | **Anthropic and OpenRouter passed 2026-11-11** ([results](spike-results/provider-calls.md)). Open: one live run each for OpenAI and Gemini, which also settles Gemini's one call or two |
| S3 ✅ | Does the speech worker live happily in the offscreen document? | **Passed 2026-11-11** ([results](spike-results/stt-in-extension.md)): live capture + transcription on CPU and GPU with no audio lost; 643 MB model download works; isolation works but is not needed |

**Phase 1 ✅ — app shell on the existing backend (done 2026-11-11).** New-tab app, open
on install, welcome wizard, providers screen (Anthropic and OpenRouter keys through
today's `/setup/*` endpoints), settings migrated from `options/`, which is deleted.
*Done means:* a developer-mode install is fully configurable without the old options
page, and capture is unchanged — both checked by `scripts/run_app_e2e.mjs`.

**Phase 2 ✅ — ledger, history, usage (built 2026-11-12).** IndexedDB ledger written by
the service worker, YouTube/Rumble channel resolution, usage frames from the backend,
History and Usage screens, import from `fact_checker.db`, export and delete. Checked by
247 unit tests and by `scripts/run_app_e2e.mjs`, which drives a scripted session through
the real capture path into IndexedDB and reads it back on screen. *Done means:* a week of
mixed Twitch / YouTube / Kick viewing shows up correctly attributed, with spend — **not
yet observed on real streams**; see "still open" below.

What was built differs from §5–§7 in these ways:

| Plan | Built | Why |
|---|---|---|
| A `usage` object on verdict and gate frames | A separate `usage` frame per provider call (`backend/app/usage.py`, `UsageFrame`) | There is no gate frame to hang it on, and a verify that fails still costs money and has no verdict frame |
| Claims recorded from existing frames | An opt-in `claim` frame (`ClientHello.send_claims`) | The wire only ever announced claims that were being checked; the history needs the ones that were heard and dropped too |
| One ledger session per backend session | One ledger session per continuous stretch of **one channel**, carrying `backend_session_id` | A tab that moves to another channel mid-capture must not credit its watch time and spend to the first one. Import skips by `backend_session_id` |
| `shared/pricing.json` | `ledger/pricing.mjs`, Anthropic models only | No build step to bundle JSON; OpenRouter reports exact cost, Ollama is free, and OpenAI/Gemini prices wait for their live runs (S2) |
| Budgets on the usage screen | Not built | They need a guard in the pipeline; belongs with the browser engine in Phase 4 |
| Verdict detail: source tiers, transcript context, re-check | Explanation, sources, model, latency, rating with correction, copy, delete | Tiers and transcript context are not on the wire; re-check needs the Phase 4 pipeline |

Still open from this phase:

- **YouTube and Rumble channel detection is unverified against the live sites.** It
  reads the channel link under the player (`content/page_identity.js`); the selectors are
  unit-tested against a fake page only. From the development machine YouTube was
  unreachable and Rumble answered headless Chrome with a bot check. Twitch and Kick were
  confirmed live. `node scripts/check_page_identity.mjs --headed URL…` checks any page.
- The Jev pre-screen's calls are not reported as usage (they bypass the SDK call sites).
- Retention is applied by a daily alarm and was exercised by calling the sweep directly,
  not by waiting a day.

**Phase 3 ✅ — local speech in real sessions (built 2026-11-12).** A viewer can choose
"This browser" under Settings → Speech recognition, download and test a model, and from
then on a capture session transcribes in the offscreen document and sends the backend
**text, not audio**.

- **Backend:** a transcript session (`hello` `format: "transcript"`, `transcript` frames)
  feeds the same gate → verify path with no speech engine involved, and
  `STT_BACKEND=none` starts the backend with no model at all (about three seconds, and it
  refuses audio sessions with `stt_disabled`). This replaces the plan's "debug text path":
  a real session keeps batching, dedupe, persistence and the history frames.
- **Extension:** `engine/stt/speech_setup.mjs` (what this device decided, and the rules),
  `session_speech.mjs` (load, crash-guard calls, GPU → CPU recovery), the Speech card,
  model revisions pinned to a commit, a bundled eleven-second self-test clip.
- **Crash guard:** a marker is stored before the GPU is first used and cleared when that
  use returns; a marker with nobody holding the GPU lock at the next start blocks the
  GPU until the viewer re-tests. A GPU lost mid-session moves to the model's CPU build.

*Done means:* live transcripts on a stream with the backend's speech model switched off.
**Met in the test harness, not yet on a real stream:** `scripts/run_browser_speech_e2e.mjs`
downloads the real model, runs the self-test through the UI, captures a tab playing 43 s
of recorded speech and checks the text that reaches a model-less backend — on the dev
machine, Moonshine Base on the CPU (10× real time, 13 % word error rate on the clip) and
Whisper Base on the Intel Arc GPU (4.5×, 14 %), with no audio frame sent in either. The
real backend was separately started with `STT_BACKEND=none` and handshaken both ways.

Not built in this phase, and where each belongs:

| Item | Status |
|---|---|
| Neural voice-activity detection | **Open.** Segmentation is still energy-based, so music and crowd noise reach the model. The backend drops an utterance repeated verbatim, which catches the commonest hallucination, not all of them |
| Speech step in the welcome wizard | Deferred to Phase 4, when the browser engine becomes the default path |
| Stepping down a tier when the queue grows | Open. Today the oldest waiting audio is dropped and counted |
| Model files from a URL we control, with a pinned SHA-256 | Partly: downloads are pinned to a Hugging Face commit, which is immutable; self-hosting belongs with the store work (Phase 6) |
| Hosted speech fallback | Phase 5, unchanged |
| GPUs other than Intel Arc | Still untested; the ladder and self-test are what protect them |

**Phase 4 — in-extension pipeline and four providers (3–4 weeks, gated on S2).** Prompt
export, JavaScript gate and verify, the four provider modules, budgets, demo mode.
*Done means:* a clean Chrome profile with no backend running goes from install to a real
verdict on a live stream, with each of the four keys. Optional afterwards: `llm_openai.py`
and a restored `llm_gemini.py` so the companion offers the same four.

**Phase 5 — hosted speech fallback (about a week).** OpenAI and Gemini transcription for
machines that fail the benchmark.

**Phase 6 — store (1 week of work, plus review time).** `LICENSE`, privacy policy,
build script, CI, listing assets, unlisted submission, then AMO and Edge.

Start the developer account, `LICENSE` and privacy policy during Phase 1 — they are
cheap, and the first review is the slow one.

---

## 12. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Local speech too slow on typical viewer hardware | medium (was high) | S1 showed the Fast tier at 11× on a single CPU thread with no GPU; slower laptops are still unmeasured |
| A GPU runs a model fast but wrong | high | Seen twice in S1 on 16-bit paths. Mandatory self-test that checks correctness before speed; verdict cached per GPU, model and browser version |
| Only one real GPU has been tested | high | Benchmark page and runner produce a shareable report; collect NVIDIA, AMD and Apple results before Phase 3 ends |
| WebGPU missing or flaky on a viewer's machine (Linux rollout, old drivers) | medium | Probe every session, software-adapter check, `device.lost` recovery, crash guard, CPU fallback |
| Gemini grounded JSON unreliable | medium | Fallback chain is built and reports which path it took; still needs a live run |
| OpenAI and Gemini modules have never made a real call | medium | One command once keys exist; do it before Phase 4 builds on them |
| A provider withdraws the default model (happened: OpenRouter's Mercury) | medium | Check the model catalogue before use; clear "model unavailable" message; defaults easy to change |
| Free-tier or unfunded keys that validate but cannot verify | medium | Grounded test call during setup with a specific fix-it message per provider |
| Two pipelines drift (Python vs JS) | high | Single-sourced prompts and tier table; shared golden fixtures; v1 scope limited to gate + verify |
| Store rejection over `tabCapture` + audio | medium | Wizard disclosure, optional host permissions, demo mode, precise justifications |
| Viewer runs up a bill | medium | Budgets on by default; cost on the home screen; per-query search billing modelled |
| Key theft from a compromised page | medium | Trusted-context storage, no keys in content scripts or URLs, optional hosts |
| A per-channel "accuracy" table gets screenshotted as a smear | medium | n ≥ 30 floor, adjudicated-only percentages, no ranking language |
| Large model download abandoned on first run | medium | Size shown up front, resumable streamed download, demo mode works before it finishes |

---

## 13. Sources

Providers:
[Gemini — Grounding with Google Search](https://ai.google.dev/gemini-api/docs/google-search) ·
[Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing) ·
[Gemini structured outputs](https://ai.google.dev/gemini-api/docs/interactions/structured-output) ·
[Report: empty grounding metadata with structured output](https://discuss.ai.google.dev/t/grounding-metadata-grounding-chunks-grounding-supports-empty-when-using-structured-output-with-google-search-tool/113240) ·
[Report: truncated grounded JSON on Gemini 3.5/3.6 Flash](https://discuss.ai.google.dev/t/google-search-grounding-drops-the-beginning-of-the-response-text-gemini-3-5-3-6-flash-json-output-starts-mid-sentence/176967) ·
[OpenAI API pricing](https://openai.com/api/pricing/) ·
[GPT-4o mini Transcribe](https://developers.openai.com/api/docs/models/gpt-4o-mini-transcribe.md) ·
[Anthropic Usage and Cost API](https://platform.claude.com/docs/en/manage-claude/usage-cost-api) ·
[OpenRouter OAuth PKCE](https://openrouter.ai/docs/use-cases/oauth-pkce)

Dropped options (§3.7):
[Anthropic — authentication and credential use](https://code.claude.com/docs/en/legal-and-compliance) ·
[Anthropic CLI authentication](https://platform.claude.com/docs/en/cli-sdks-libraries/cli/authentication) ·
[Sign in with ChatGPT — quickstart](https://developers.openai.com/siwc/quickstart) ·
[Sign in with ChatGPT — preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations.md) ·
[Ollama CORS from a Chrome extension (community)](https://www.mellow.tel/blog/how-to-solve-cors-ollama-chrome-extension)

GPU speech in the browser:
[What's new in WebGPU, Chrome 144 (Linux rollout)](https://developer.chrome.com/blog/new-in-webgpu-144) ·
[WebGPU supported in major browsers](https://web.dev/blog/webgpu-supported-major-browsers) ·
[Transformers.js in a Chrome extension](https://huggingface.co/blog/transformersjs-chrome-extension) ·
[Transformers.js v4](https://huggingface.co/blog/transformersjs-v4) ·
[Moonshine base (ONNX)](https://huggingface.co/onnx-community/moonshine-base-ONNX) ·
[Parakeet in the browser with WebGPU](https://dev.to/maikidev/client-side-audio-transcription-using-parakeet-v3-and-webgpu-1916) ·
[Large model downloads running out of memory in extensions](https://groups.google.com/a/chromium.org/g/chromium-extensions/c/-4CNcu5bUGA)

Stores:
[Register a developer account](https://developer.chrome.com/webstore/register) ·
[Privacy practices tab](https://developer.chrome.com/docs/webstore/cws-dashboard-privacy) ·
[Review process](https://developer.chrome.com/docs/webstore/review-process) ·
[Listing images](https://developer.chrome.com/docs/webstore/images) ·
[Chrome Web Store API](https://developer.chrome.com/docs/webstore/using-api) ·
[Policy updates, July 2026](https://developer.chrome.com/blog/cws-policy-updates-2026) ·
[Review and publishing-limit updates, August 2026](https://developer.chrome.com/blog/cws-review-updates-2026) ·
[Firefox data-collection consent](https://extensionworkshop.com/documentation/develop/firefox-builtin-data-consent/)

Not independently verified, flagged in the text: OpenAI web-search and transcription
prices (third-party trackers; check the pricing page), the $5 registration fee (not
stated on Google's registration page), the Anthropic browser-access header and the
extension cross-origin-isolation manifest keys (from prior knowledge; confirm in S2 and
S3), and whether WebGPU on Linux is default-on for every Intel Gen12+ user (it is on the
dev machine).

Repo measurements (this codebase, 2026-11-11): no `LICENSE`, no `.github/` ·
`options_ui.open_in_tab` is `false` and there is no `onInstalled` handler · YouTube and
Rumble sessions record `channel = null` (`deriveSessionIdentity`) · cost is a flat
per-verify constant; token usage is logged but not stored · providers implemented in the
backend: Anthropic, OpenRouter, Ollama (gate only); no direct OpenAI provider; Gemini
removed in `06ec287`.
