# Live Stream Fact-Checker

A Chrome extension (Manifest V3, named **"Live Stream Fact-Checker"**) plus a local
Python backend that fact-checks a live stream in real time. Supported sites: **Twitch,
YouTube (watch pages and live), Kick, and Rumble**. The extension captures the tab's
audio and streams it to a local FastAPI server, which transcribes it with
`faster-whisper`, screens transcript batches with Jev, extracts verifiable claims
from approved batches, verifies them with a web-search-grounded LLM, and pushes
**TRUE / FALSE / MISLEADING / UNVERIFIED** verdicts (with sources) back to a
Shadow-DOM overlay rendered over the player.

The LLM layer runs on **OpenRouter** (the primary provider: one key, any model, the
`web` search plugin for grounding); **Gemini** is an optional secondary provider
(requires a paid-tier key for search grounding). Pick the provider and paste your key
on the extension's options page — no file editing needed.

## Quickstart

1. **Start the backend** (needs Python 3.11+; first run downloads a ~170 MB speech
   model):

   ```bash
   ./backend/run.sh
   ```

   The server starts key-less in a "needs setup" state — the extension will walk you
   through adding a key.

2. **Load the extension** — the same `extension/` folder works in both browsers:

   - **Chrome 116+**: open `chrome://extensions/`, enable **Developer mode**
     (top right), click **Load unpacked**, select `extension/`.
   - **Firefox 128+**: open `about:debugging#/runtime/this-firefox`, click
     **Load Temporary Add-on…**, and select `extension/manifest.json`.
     (Temporary add-ons are cleared when Firefox restarts.)

   Each browser warns about the other's manifest keys — Chrome about
   `background.scripts` and `browser_specific_settings`, Firefox about the
   `tabCapture`/`offscreen` permissions. Both are expected: the manifest
   deliberately carries both browsers' keys so there is no build step.

3. **Connect your AI provider**: click the extension's toolbar icon → **Open
   settings** → paste your **OpenRouter** key (https://openrouter.ai/keys —
   the primary provider; hold a few dollars of credit for web search) or,
   optionally, a **Gemini** key (https://aistudio.google.com/apikey, requires a
   paid-tier key for search grounding) → **Save & verify**. The key is validated
   live against the provider and stored only in `backend/.env` on your machine.

That's it — open a stream on a supported site, click the toolbar icon, and press
**Start**. See [Usage](#usage) for details.

## Architecture

```
Chrome (extension)                              Local backend (127.0.0.1:8710)
┌─────────────────────────────────┐
│ popup ── Start/Stop (gesture)   │
│   │                             │
│ service worker (stateless)      │
│   │ streamId                    │
│ offscreen document              │           ┌───────────────────────────────┐
│   tabCapture → AudioContext     │  16 kHz   │ FastAPI  /ws/audio            │
│   → lowpass ×2 → worklet        │  PCM over │  ring buffer → faster-whisper │
│   → Int16 PCM ─────────────────────WebSocket──→ Jev → claim extraction    │
│   ← JSON verdict frames ────────────────────←─ grounded verify (LLM + web  │
│   │                             │           │     search) → verdict        │
│ content script (supported sites)│           │  POST /debug/text (test path)│
│   Shadow-DOM toast + history    │           │  GET  /healthz               │
└─────────────────────────────────┘           └───────────────────────────────┘
```

No build step, no auth — everything runs locally, single user. Analytics live in
one SQLite file (`backend/fact_checker.db`; delete it to reset).

**Firefox uses a different front half.** Firefox implements neither
`chrome.tabCapture` nor the offscreen-document API, so there is no way to
capture a tab's audio and nowhere Chrome-shaped to put the session. The
extension detects this at load (`extension/shared/capabilities.js`) and swaps
in a second capture path; the backend never notices the difference.

```
Firefox (extension)                             Local backend (127.0.0.1:8710)
┌─────────────────────────────────┐
│ popup ── Start/Stop (gesture)   │
│   │                             │
│ content script                  │
│   page's own <video>            │
│   → AudioContext (+ loopback)   │           ┌───────────────────────────────┐
│   → lowpass ×2 → worklet        │           │ FastAPI  /ws/audio            │
│   → Int16 PCM → base64          │  16 kHz   │  ring buffer → faster-whisper │
│   │ runtime messages            │  PCM over │                               │
│ background EVENT PAGE (has DOM) │  WebSocket│                               │
│   WebSocket ───────────────────────────────→│  (identical protocol)         │
│   ← JSON verdict frames ────────────────────←─                              │
│   │ relayed to the same overlay │           └───────────────────────────────┘
└─────────────────────────────────┘
```

Why it is shaped that way: a content script's network requests run in the
*page's* context under MV3, and Twitch/YouTube ship a `connect-src` CSP that
would block `ws://127.0.0.1` — so the WebSocket has to live in an extension
page. Firefox's MV3 background is an event page with a real DOM (not a service
worker), so it can hold the socket and reuse the same `BackendSocket` client.
PCM crosses as base64 because runtime messaging is JSON-serialized in both
browsers (an `ArrayBuffer` would arrive as `{}`).

Firefox-specific caveats:

- Audio is tapped from the page's `<video>` via `createMediaElementSource`,
  which **reroutes** that element's audio through the extension's
  AudioContext. The graph therefore keeps a permanent loopback to the
  speakers and never closes the context — and capture refuses to start if the
  context cannot leave the `suspended` state, rather than risk muting the
  stream.
- It follows that capture only works on pages with a real media element (all
  four supported sites) and not on DRM-protected video.
- A full page navigation ends the session (the content script owns the tap);
  in-page SPA route changes are handled by re-attaching to the new player.

## Backend details (advanced)

`./backend/run.sh` is idempotent: it syncs the [uv](https://docs.astral.sh/uv/)
environment and execs uvicorn. Dependencies live in `backend/pyproject.toml` and
are pinned by `backend/uv.lock`. To run the steps manually:

```bash
cd backend
uv sync --inexact                      # creates .venv from uv.lock
uv run --no-sync uvicorn app.main:app --host 127.0.0.1 --port 8710
uv run pytest -m "not slow"            # tests
```

`--inexact` and `--no-sync` matter: the optional GPU speech backend is installed
with an accelerator-specific PyTorch wheel, and a plain `uv sync`/`uv run` would
prune or downgrade it on every start.

Check it is up: `curl http://127.0.0.1:8710/healthz` (echoes `configured` plus the
active `llm_provider`, `gate_model`, and `verify_model` — all `null` until a key is
set).

**API keys:** the backend starts without one, in a "needs setup" state (fact-checking
is disabled until a key is added). The normal path is the extension's options page,
which validates the key live and writes it to `backend/.env`. Editing `.env` by hand
still works if you prefer — set `OPENROUTER_API_KEY=<key>` (or `LLM_PROVIDER=gemini`
plus `GEMINI_API_KEY=<key>`; see `.env.example`) and restart. Both paths use the same
file. `.env` holds your real key — it is gitignored and must never be committed.

**First run:** the Whisper model (`distil-small.en`, int8, ~170 MB) is downloaded at
startup — expect a one-time delay. On slow machines, set `WHISPER_MODEL=base` in
`.env`.

## Speech-to-text backends (CPU, CUDA, ROCm, XPU)

Two engines, one filter stack — `STT_BACKEND` picks which:

| | `faster-whisper` (default) | `torch` |
|---|---|---|
| Devices | cpu, cuda | cpu, **cuda**, **rocm**, **xpu** |
| Model name | ctranslate2 (`distil-small.en`) | HF repo id (`openai/whisper-small.en`) |
| Speed | fastest on CPU (int8) | needed for Intel/AMD GPUs |
| Install | included | `./backend/scripts/install_stt_gpu.sh` |

For an Intel Arc / Core Ultra iGPU, an AMD Radeon, or an NVIDIA card:

```bash
cd backend
./scripts/install_stt_gpu.sh          # auto-detects your GPU
./scripts/install_stt_gpu.sh xpu      # or force: xpu | rocm6.4 | cu128 | cpu
```

It uses uv's `--torch-backend`, which inspects the machine and fetches from the
matching PyTorch index — a lock file cannot encode "whatever GPU this machine
has". Then in `backend/.env`:

```ini
STT_BACKEND=torch
WHISPER_DEVICE=auto                   # or cuda / rocm / xpu / cpu
WHISPER_MODEL=openai/whisper-small.en
```

Notes worth knowing:

- **uv's own auto-detection has no Intel branch** — it probes for an NVIDIA
  driver and an AMD ROCm arch, so on an Intel-only machine `--torch-backend=auto`
  quietly resolves to the `+cpu` wheel and `torch.xpu.is_available()` is `False`.
  The script detects Intel itself and upgrades `auto` to `xpu`; it also verifies
  an accelerator is actually available afterwards and exits non-zero if not,
  rather than leaving you on a CPU wheel that merely looks installed.
- **Switching backends needs `--reinstall-package torch`** (the script passes it):
  a bare `torch` requirement is already satisfied by whatever variant is present,
  so re-running the install with a different `--torch-backend` is otherwise a
  no-op that reports success and changes nothing.
- **Intel also needs system packages** the wheels cannot provide — the Level Zero
  loader and compute runtime (`libze1`, `libze-intel-gpu1`, `intel-opencl-icd` on
  Debian/Ubuntu). Without them torch imports fine and silently falls back to CPU.
- **`rocm` is spelled `cuda` inside PyTorch** (HIP reuses the CUDA API). The
  backend maps it for you *and* verifies `torch.version.hip`, so a ROCm typo
  fails loudly instead of silently running on CPU at a fraction of the speed.
- The torch backend brings its own **Silero VAD** (reused from faster-whisper)
  and computes real `avg_logprob`/`no_speech_prob`, because transformers
  returns neither — without them two of the six hallucination filters would be
  silently inactive.
- Because uv hardlinks from a shared cache (`~/.cache/uv`), a PyTorch you
  already installed for another uv project costs no extra disk here.
- **Warm-up runs at startup, not in your first session.** SYCL/CUDA kernels
  JIT-compile on first use (~5 s on an Intel Arc 140V with a warm kernel cache,
  longer cold) — inside a live session that alone stalled the STT loop past
  the ring buffer's high watermark and dropped audio. The backend now pushes
  one synthetic window through the whole inference path twice before it
  listens, and logs both timings: `torch STT warm-up on xpu: pass 1 5.4s,
  pass 2 0.9s`. Pass 2 is your steady state; if it exceeds the 3.5 s hop
  budget you get a WARNING naming the fix (smaller model or faster device).
  Measured on the same Arc 140V: `whisper-small.en` takes ~0.9 s per 4 s
  window (3.7× realtime) after warm-up, ~2 s on the CPU. Set
  `STT_WARM_UP=false` to skip it.
- **A broken GPU no longer takes the session down with it.** Accelerator
  kernels report indexing faults *asynchronously* (Intel XPU:
  `IndexKernelUtils.h ... vectorized gather kernel index out of bounds`), and
  once one fires the device context is poisoned — every later window fails.
  `app/stt_supervisor.py` counts consecutive failed windows; at
  `STT_FAILURE_THRESHOLD` (3) it reloads the same model on the **CPU in
  fp32**, once, and sessions continue. The client gets one non-fatal
  `stt_degraded` notice ("switched to CPU — captions may lag"), `/healthz`
  reports `status: "degraded"` with the details under `stt`, and the log
  says `STT recovered on the CPU: ... [degraded from xpu]`. If the CPU reload
  fails too (or `STT_CPU_FALLBACK=false`), the session ends with a fatal
  `stt_failure` frame, new connections are refused with the same code, and
  `/healthz` says `unhealthy` until you restart. Rehearse the whole path
  without breaking anything: `curl -X POST 127.0.0.1:8710/debug/stt/fail
  -H 'content-type: application/json' -d '{"windows":3}'` while a capture
  is running.
- The torch path also synchronizes the device right after `generate`, so a
  fault is attributed to the window that caused it; bounds-checks token ids
  before they index anything; and pins greedy decoding and the modern
  (non-`forced_decoder_ids`) generation config that transformers 5.x
  maintains.
- **`torch`/`transformers` are pinned** (`torch>=2.13,<2.14`,
  `transformers>=5.15,<6` in pyproject's `gpu` extra). The GPU install runs
  outside `uv.lock`, so these upper bounds are the only thing stopping the
  next major from arriving silently; `install_stt_gpu.sh` reads them from
  pyproject and prints the versions it installed.

## Analytics & dashboard

Every session records its funnel to `backend/fact_checker.db`: which claims were
gated, why they were dropped (below threshold / topic filter / duplicate / queue),
verdicts with latency and sources, and your 👍/👎 feedback from the overlay (the
seed of a self-growing eval set). Open **http://127.0.0.1:8710/dashboard** for
today's stat tiles, the verdict-label distribution, the claim funnel, per-channel
cards (rates only appear at ≥30 adjudicated verdicts — below that there is no
honest signal), and a recent-sessions table with per-session detail. The popup
shows a live "Checks today: N · ~$X.XX" readout. Raw JSON: `GET /stats/summary`,
`/stats/channels`, `/stats/sessions`. Delete the `.db` file to reset everything.

## Streamer mode (separate product: bot + OBS overlay)

Everything above is the **viewer** tool: verdicts appear in a private overlay
only you see. **Streamer mode** is a separate product for broadcasters — same
pipeline, pointed at your *own* stream, with verdicts going to your whole
audience: sourced fact-checks posted in your Twitch chat by a bot you control,
and an on-stream overlay rendered through OBS. It runs side by side with the
viewer backend: its own entry point, port (**8711**), and database
(`streamer.db`).

```bash
./backend/run-streamer.sh          # then open http://127.0.0.1:8711/control
```

**Setup (once, ~5 minutes)** — the control panel walks this checklist:

1. AI provider key (same flow as the extension).
2. Connect the **bot account** to Twitch — "Connect with Twitch" (device code;
   needs a free `TWITCH_CLIENT_ID` you register once, and gives automatic
   token refresh) or paste a `chat:read chat:edit` token.
3. `/mod <yourbot>` in your chat — this is the consent proof (only you can
   grant it), and it lifts link filtering and raises the rate tier.
4. You (the broadcaster, not a mod) type `!fc enable` — recorded as the
   auditable consent row.
5. OBS: add `http://127.0.0.1:8711/overlay` as a Browser Source and
   `/control` as a Custom Browser Dock. Turn OFF *"Shutdown source when not
   visible"* and *"Refresh browser when scene becomes active"*. Press **Send
   test verdict** to position the overlay before going live.

**Every stream (<1 minute):** `uv run fact-checker-ingest twitch.tv/<you>`
(pulls your published stream via streamlink+ffmpeg), or
`--source device --device <loopback>` for zero-delay local capture. Add
`--video` to also capture a ≤480p frame every 5 s for on-screen claims ("as
you can see, this chart…") — same privacy posture as the extension's toggle:
frames go only to your local backend, live in a 3-frame memory ring, and are
never stored.

**The console** (`/control`) is a no-build Preact webapp on the Nocturne
design system — vendored runtime, zero Node, works offline. Six views:
**Cockpit** (MUTE/mode/review queue with J·K·↵·X·M keys, session stats, live
pipeline controls), **Setup** (the checklist + the overlay style picker),
**Pipeline** (sensitivity, topics-to-check, vision, live transcript —
instant-apply to the running session), **Bot settings** (the full posting
policy), **Decisions** (every verdict and why it did or didn't post, with
Retract), and **Analytics** (approval rate, median latency, verdict mix,
claim funnel, per-channel cards). `/control#/dock` is a 340px queue-first
layout for an OBS Custom Browser Dock. OBS 30+ recommended (modern CEF).

**The overlay** (`/overlay`) has four **console-selectable styles** — refined
toast, broadcast lower-third, minimal chip, verdict stamp — picked in Setup
and applied to the running overlay live (the OBS source URL never changes).
URL params (`style=`, `position=`, `duration=`, `labels=` — narrowing-only)
pin per-source overrides, so two browser sources can run different styles.

**What actually posts** — deliberately much less than what gets checked:
FALSE/MISLEADING only, ≥2 citations across ≥2 distinct reputable domains
(politics/health require a primary source), max 6 posts/hour with a
3-per-10-minutes guard, nothing older than 90 s, nothing UNVERIFIED — ever.
New channels start in **review mode** (you approve each post, one keystroke in
the dock) and graduate to auto after 10 approvals; `!fc trust` skips the
probation. **Dry run is on by default**: the bot evaluates and records the
exact message it *would* have sent — read a full stream's worth in the panel,
then flip "Go live". Every policy knob is editable in the panel's **Bot
settings** (labels, topics, pace, confidence bar, message shape, source-tier
overrides, probation) — hard safety clamps are refused with the reason, never
silently rewritten. Mid-stream control is chat-first: `!fc mute [30m]`,
`!fc off`, `!fc wrong <id>` (public retraction + a feedback row), `!fc help`
for the rest — mods can use all of them, from a phone.

Before pointing it at a real audience, work through
`docs/streamer-launch-checklist.md`.

## Self-contradiction alerts

Separately from web-grounded fact-checks, the backend remembers what was claimed
earlier in the SAME session and flags high-confidence logical contradictions
("Earlier: 'I've never been to Japan' · Just now: 'I've been to Japan twice'") as
amber two-quote toasts. Candidate pairs are retrieved with Ollama embeddings
(`nomic-embed-text`) when Ollama is running, or a built-in lexical fallback when
it isn't, and judged by the gate model. The doctrine mirrors verification:
default to NO flag — mind-changes, jokes, and restatements never count, and only
high-confidence judgements surface.

## On-screen claims (experimental, opt-in)

Enable **"Send video frames for on-screen claims"** in the options page and the
extension captures a small (≤480p) screenshot of the stream every 5 seconds
alongside the audio. When a claim references something visible ("as you can see,
this chart…"), the freshest frame is attached to that verification call so the
model can actually look at the chart. Privacy posture: frames go only to your
local backend, are held in a 3-frame in-memory ring, are forwarded to the LLM
provider only for visual-cue claims, and are **never stored anywhere**. An
attached image can never make a check fail (or a verdict stronger) than it would
have been without it — the no-citations ⇒ UNVERIFIED rule is unchanged.

## LLM provider, models, costs

**Primary provider: OpenRouter.** The default gate is `~typesafe/jev-latest`:
transcript → Jev decision → claim extraction → filters → web-grounded verification.
Jev uses the [Decisions API](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-questions-and-answers-request),
with `{context, new_transcript, current_date}` as its state. The context is the
previously processed 40-word tail; only assertions completed in the fresh text
qualify. Its `needs_fact_check` Noul answer is the probability that a checkable
assertion exists, **not the probability that a statement is true**.

`JEV_MIN_CHECK_PROBABILITY=0.35` is the initial, permissive cutoff: uncertain
batches can reach extraction, while clear negatives skip both extraction and
search. This cutoff is a starting policy, not an empirically calibrated optimum.
Jev cannot write claim text. Approved batches go to
`OPENROUTER_EXTRACTION_MODEL=inception/mercury-2.5-preview`, which resolves
references, extracts individual claims, and assigns topics and check-worthiness.
Existing sensitivity, topic, and duplicate filters still run before web search.
`OPENROUTER_VERIFY_MODEL` also defaults to `inception/mercury-2.5-preview`.
Contradiction judgments use the extraction model and only see retained claims.

`GATE_TIMEOUT_S=15` covers the entire Jev + extraction pass. A Jev timeout,
API error, or malformed answer drops the batch and logs the failure; it does
not fall back to the generative gate. Logs include probability, route, resolved
Jev version, and stage latency. `/healthz` exposes the decision/extraction setup
under `openrouter.decision_gate`. Gate-call analytics still count logical batch
passes rather than individual HTTP requests.

Set the models in `.env` or the options page. An explicitly configured older
gate model keeps using the generative gate; choose `~typesafe/jev-latest` to
switch an existing installation. Gemini and Ollama gate options remain available.
To evaluate Jev against labeled synthetic transcript examples, explicitly run
`cd backend && uv run python scripts/eval_jev.py --yes-spend-credits`.
This spends credits on decisions only; normal tests remain offline.

**Capability-aware requests.** At boot and on every Apply the backend reads each
active generative model's `supported_parameters` from OpenRouter's public catalogue
(`GET /api/v1/models`, keyless) and builds requests from it: `temperature` and
`reasoning` are only sent to models that list them, strict `json_schema` mode is only
attempted when a model lists `structured_outputs` (a model with plain
`response_format` support gets one grounded `json_object` call instead), and the
verdict is never silently degraded. This matters because every strict request also
carries `provider.require_parameters: true`, so a parameter the model's endpoints
reject fails the whole call — OpenAI's GPT-5.x endpoints, for instance, accept no
`temperature`, and before this lookup existed every one of their verdicts took the
2–3-call text fallback chain without anyone noticing. `/healthz` reports the lookup
under `openrouter.capabilities` (`"source": "catalogue"` or `"assumed"` when
openrouter.ai was unreachable) and the per-model verify modes so far
(`strict` / `json_object` / `fallback`); `/stats/summary` adds a persisted
`verify_modes` table with each model's fallback rate.

**How verification is grounded.** Verification sends a `system` message with the
instructions and a `user` message containing only the claim: OpenRouter's `web`
plugin runs a search on the user message before the model runs, so anything else
there would pollute the query. Results come back as `url_citation` annotations —
the only place sources ever come from. The model also rates `evidence`
(`strong` / `partial` / `none`, how directly the results address *this* claim) and
anything but `strong` is downgraded to UNVERIFIED: a search always returns five
results, so the source count alone cannot tell confirmation from adjacency.
`OPENROUTER_WEB_ENGINE` picks the engine: `exa` (default; works for every model,
$0.007/request, `OPENROUTER_WEB_MAX_RESULTS=5` results), `native` (the model
provider's own search — pricier, and fails on models without one), or `auto`.

**Choosing specific models.** Each stage's OpenRouter model is a slug you can set
from the options page (Gate / Claim extraction / Verify model) or in `.env`.
Documented Jev IDs (`~typesafe/jev-latest`, `typesafe/jev-1.13`) are accepted for
the gate only, independently of the chat catalogue. Generative model slugs are
validated against OpenRouter's live catalogue on Apply, so a typo is rejected
immediately rather than surfacing as a runtime failure mid-stream — and a model works
the day it launches. A paid model and its `:free` variant are distinct slugs (the
error message points that out). Documented alternates:

- `openai/gpt-5.6-luna` — a stronger verifier ($0.20/M input); no `temperature`
  support, handled automatically by the capability lookup.
- `google/gemini-3.8-flash` — strong on current events, Google-native web search
  available via `OPENROUTER_WEB_ENGINE=native` ($0.014/call); $0.75/M input.
- `google/gemma-4-26b-a4b-it:free` — $0 tokens, but its endpoint has no
  `structured_outputs` (runs in `json_object` mode) and the free tier's daily
  request cap dies within minutes at the gate's cadence.

Browse the catalogue at <https://openrouter.ai/models>.

**Gemini (optional secondary provider):** pick Gemini on the extension's options
page and paste your key (or set `LLM_PROVIDER=gemini` and `GEMINI_API_KEY` in
`.env`; models: `GEMINI_GATE_MODEL` / `GEMINI_VERIFY_MODEL`). Gemini searches with
its own Google Search tool, which requires a paid-tier key — one reason OpenRouter
is the primary provider.

**Per-stage providers & Ollama (local gate).** The two pipeline stages can run on
different providers: the claim gate makes ~300 cheap ungrounded calls/hour, while
verification makes 5–20 grounded calls/hour. Routing the gate to a local **Ollama**
model eliminates ~95% of hosted API calls with hard grammar-constrained JSON output:

```bash
ollama pull gemma3:4b          # gate model (OLLAMA_GATE_MODEL)
ollama pull nomic-embed-text   # embeddings for contradiction detection
```

Then on the options page: select **Ollama → Test connection**, and set **Claim
detection (gate)** to Ollama under stage routing (verification stays on
OpenRouter/Gemini — local verify is not supported because it has no web-search
grounding). Env equivalents: `GATE_PROVIDER=ollama`, `VERIFY_PROVIDER=openrouter`,
`OLLAMA_BASE_URL=http://127.0.0.1:11434/v1` (any OpenAI-compatible server works:
LM Studio, vLLM, llama.cpp). A cold local model's first call can exceed
`GATE_TIMEOUT_S=15` — raise it or set a longer Ollama `keep_alive` if the first
gate pass times out.

**Costs & limits (OpenRouter):**

- Web search is billed to your credit balance — $0.007 per fact-check on Exa — **even
  on `:free` models**. A $0-credit account gets 402 errors on verification, so hold a
  small credit balance. With the mercury default, tokens add roughly $0.03 per
  streaming hour on top.
- Free-variant rate limits: **20 requests/min**, and **50 requests/day** with under $10
  in lifetime credit purchases vs **1,000/day** once you have bought $10+. The gate
  alone makes ~5 calls/min while a stream runs, so a `:free` gate model dies within
  minutes — a paid gate model or a one-time $10 top-up is the practical minimum.
- At the app's throttled rate (gate every 12 s, verifications capped by `VERIFY_RPM=8`
  and deduped), expect well under a dollar per multi-hour stream.

**Checking verdict quality.** `backend/scripts/eval_verify.py` replays the claims
stored in `fact_checker.db` through the configured verifier and prints label,
evidence and fallback histograms plus latency — it spends credits, so it insists on
`--yes-spend-credits`. `POST /debug/text` runs the gate → verify path on raw text.

**On the transcription model.** `openai/whisper-small.en` stays the torch default on
an integrated Intel GPU: after the startup warm-up it runs ~0.9 s per 4 s window, and
the larger "fast" variants (`whisper-large-v3-turbo`, `distil-large-v3`) keep the full
32-layer large encoder, which is what the 3.5 s hop budget cannot afford on an iGPU.
An audio-native or hosted speech model on OpenRouter (e.g. `microsoft/mai-transcribe-2`
at ~$0.10 per audio-hour, or an audio-input LLM) would remove the GPU dependency, but
OpenRouter accepts whole clips only (a request per window), the audio would leave the
machine, and the hallucination filters would lose the `avg_logprob`/`no_speech_prob`
signals they key on. It is the natural opt-in `STT_BACKEND=openrouter` follow-up for
the hosted tier, not a replacement for local Whisper today.

## Usage

Installed via the [Quickstart](#quickstart) above. After code changes to the
extension, reload it: the refresh icon on its card in `chrome://extensions/`, or
**Reload** on `about:debugging#/runtime/this-firefox` in Firefox.

1. Start the backend, then open a stream on a supported site: Twitch
   (`https://www.twitch.tv/...`), YouTube (`/watch?v=` or `/live/...`), Kick
   (`https://kick.com/<channel>`), or Rumble (a `/v...` watch page).
2. Click the extension's toolbar icon and press **Start** (the click is the required
   user gesture for tab capture). The tab stays audible while captured.
3. Verdict toasts appear over the player; sources are clickable links. Hovering a toast
   pauses its auto-dismiss timer.

**Popup status meanings**

| Status | Meaning |
|---|---|
| Backend online / offline | `GET /healthz` preflight; Start is disabled while offline |
| Idle | Nothing captured |
| Starting… | Capture + WebSocket handshake in progress |
| Capturing | Audio streaming, pipeline live |
| Reconnecting (attempt n) | Backend WS dropped; backoff 0.5 s → 15 s, gives up after 5 min |
| Capturing in another tab | A different tab owns the session; Stop it first |
| Error(code) | e.g. `ERR_BACKEND_DOWN`, `ERR_STREAM_ID_EXPIRED` ("Click Start again"), `ERR_CAPTURE_LOST` |

**History panel:** a corner pill ("Fact-check · n") on the player toggles a scrollable
list of this session's verdicts plus a connection status dot. A muted footer counts
claims skipped by your topic filters ("N claims skipped by topic filters · Edit",
hidden while the count is 0). History is in-memory — a page reload clears it.

**Options** (right-click icon → Options): backend URL (takes effect on next Start),
sensitivity (low/medium/high — applied live), popup position (4 corners), popup
duration, and an optional live-transcript toggle.

**Topics to fact-check** (options page): a checkbox per claim category — Politics &
current events, Health & medicine, Science & technology, Money & economy, History,
Sports, Gaming, Entertainment & pop culture, and Everything else (claims that don't
fit a category above) — under a tri-state "Fact-check all topics" master checkbox.
Claims outside the checked topics are ignored. All topics are on by default;
"Everything else" is always on. Changes apply instantly — even mid-stream.

**Platform notes:** Kick's DOM churns fastest of the supported sites (obfuscated,
frequently changing class names), so expect the Kick player selectors to need
occasional maintenance. On Rumble, if the raw `<video>` element itself is fullscreened,
no overlay can render on top of it — verdicts go to the history panel instead.

## Testing recipes

All commands from `backend/` with the venv active.

```bash
# Unit/integration tests (slow real-Whisper tests are excluded by default
# via addopts = "-m 'not slow'"; run them with: pytest -m slow)
pytest -m "not slow"
```

**No-audio end-to-end** — exercise gate → dedupe → grounded verify; if a WS session is
open, the verdict also pops on the captured tab:

```bash
curl -X POST http://127.0.0.1:8710/debug/text \
  -H "Content-Type: application/json" \
  -d '{"text": "The Great Wall of China is visible from space with the naked eye"}'
```

The optional `enabled_topics` parameter (list of topic slugs) tests the topic filter
with the same semantics as the live pipeline: filtered claims still appear in the
response's `claims` but produce no verdict. E.g. a sports claim with sports disabled:

```bash
curl -X POST http://127.0.0.1:8710/debug/text \
  -H "Content-Type: application/json" \
  -d '{"text": "Brazil has won five FIFA World Cups",
       "enabled_topics": ["politics", "health", "other"]}'
```

**Audio pipeline without a browser** — build a TTS fixture (requires `espeak-ng`) and
stream it over the real WS protocol:

```bash
python scripts/make_fixture_wav.py          # writes tests/fixtures/claims_16k.wav
python scripts/stream_wav.py tests/fixtures/claims_16k.wav   # prints server frames
python scripts/stream_wav.py tests/fixtures/claims_16k.wav --speed 3   # backpressure
```

## Troubleshooting

- **"Backend offline" in the popup** — start the server: `./backend/run.sh`.
- **"Add your API key to start fact-checking" in the popup** — the backend is running
  but has no key yet; click **Open settings** and paste one (see Quickstart step 3).
- **"Fact-checks paused: API quota is cooling down"** — a provider 429 tripped the
  cooldown; checks resume automatically after the retry window. Lower `VERIFY_RPM` in
  `.env` or reduce sensitivity if it recurs. On OpenRouter free variants this is
  usually the 20/min or 50-per-day cap — see "Costs & limits" above.
- **"OpenRouter credits exhausted" / 402 errors** — web search bills credits even on
  `:free` models; top up at https://openrouter.ai (fact-checks pause for 15 minutes
  after a 402 to avoid a doomed request loop).
- **Tab goes silent when Start is pressed** — the offscreen document should loop
  captured audio back to the speakers; silence means that loopback broke. Reload the
  extension and file a bug report.
- **Slow first start / startup hang** — the ~170 MB Whisper model is downloading;
  watch the backend log. Model, device, and compute type are configurable in `.env`.
- **`/debug/text` returns 404** — set `DEBUG_ENDPOINTS=true` in `.env`.
