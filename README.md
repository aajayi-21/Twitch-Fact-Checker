# Live Stream Fact-Checker

A Chrome extension (Manifest V3, named **"Live Stream Fact-Checker"**) plus a local
Python backend that fact-checks a live stream in real time. Supported sites: **Twitch,
YouTube (watch pages and live), Kick, and Rumble**. The extension captures the tab's
audio and streams it to a local FastAPI server, which transcribes it with
`faster-whisper`, extracts verifiable claims with an LLM "claim gate", verifies them
with a web-search-grounded LLM call, and pushes
**TRUE / FALSE / MISLEADING / UNVERIFIED** verdicts (with sources) back to a
Shadow-DOM overlay rendered over the player.

The LLM layer is provider-switchable: **OpenRouter** (default — free model + the `web`
search plugin) or **Gemini** (requires a paid-tier key for search grounding). Pick the
provider and paste your key on the extension's options page — no file editing needed.

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
   settings** → paste your **OpenRouter** key (https://openrouter.ai/keys,
   recommended — free models) or **Gemini** key
   (https://aistudio.google.com/apikey, requires a paid-tier key for search
   grounding) → **Save & verify**. The key is validated live against the provider
   and stored only in `backend/.env` on your machine.

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
│   → Int16 PCM ─────────────────────WebSocket──→ claim gate (LLM, ungrounded)│
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
- **First inference on an accelerator is slow** — SYCL/CUDA kernels JIT-compile
  on first use. Measured on an Intel Arc 140V (Lunar Lake), 11 s of speech,
  steady state after warm-up: `whisper-small.en` runs **16.6× realtime on XPU vs
  2.0× on CPU**, and `tiny.en` 51× vs 16×. The first window after startup takes
  several seconds regardless; that is warm-up, not a hang.

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
`--source device --device <loopback>` for zero-delay local capture.

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

**Default: OpenRouter.** Both pipeline stages (claim gate + verification) run on
`google/gemma-4-26b-a4b-it:free` — chosen by a live pipeline eval (2026-07-16) where it
went 4/4 with zero errors: claim extracted, opinion rejected, and both known-answer
verdicts correct with 5 web sources each in strict JSON mode. No expiration date.
Change models via `OPENROUTER_GATE_MODEL` / `OPENROUTER_VERIFY_MODEL` in `.env`.
Documented alternates (full rationale in `.env.example`):

- **`openai/gpt-oss-120b` (paid) — the recommended upgrade** once the account holds
  credits: 4/4 on the live eval, verification ~7 s flat (vs 5–18 s variable on the free
  default), no daily request cap, and ~$0.02–0.05 of tokens per streaming hour on top of
  the ~$0.10/hr web-search fees.
- `openai/gpt-oss-20b:free` — fastest gate call (3.5 s) and accurate verification, but
  produced an empty completion on opinion-only input during the eval.
- `nvidia/nemotron-3-super-120b-a12b:free` — good quality when reachable, but its free
  endpoint returned upstream 429s on half the eval calls — too flaky for the gate cadence.
- `tencent/hy3:free` — technically excellent fit but **its free variant expires 2026-07-21**.
- `nvidia/nemotron-3-ultra-550b-a55b:free` — not recommended (no structured outputs on
  the free endpoint, slow high-effort reasoning by default, worst measured uptime).

**Choosing specific models.** Each stage's OpenRouter model is a slug you can
set from the options page (Gate model slug / Verify model slug) or in `.env`
(`OPENROUTER_GATE_MODEL` / `OPENROUTER_VERIFY_MODEL`). Slugs are validated
against OpenRouter's live catalogue on Apply, so a typo is rejected immediately
rather than surfacing as a runtime failure mid-stream — and a model works the
day it launches. Note that a paid model and its `:free` variant are distinct
slugs; the error message points that out when you hit it. Browse the catalogue
at <https://openrouter.ai/models>.

**Switching to Gemini:** pick Gemini on the extension's options page and paste your
key (or manually set `LLM_PROVIDER=gemini` and `GEMINI_API_KEY` in `.env`; models:
`GEMINI_GATE_MODEL` / `GEMINI_VERIFY_MODEL`). Search grounding requires a paid-tier
Gemini key, which is why OpenRouter is the default.

**Per-stage providers & Ollama (local gate).** The two pipeline stages can run on
different providers: the claim gate makes ~300 cheap ungrounded calls/hour (this is
what burns OpenRouter's free-tier daily cap), while verification makes 5–20
grounded calls/hour. Routing the gate to a local **Ollama** model eliminates ~95%
of hosted API calls with hard grammar-constrained JSON output — the recommended
setup for anyone with a GPU:

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

- Inference on `:free` models costs $0, but **web search is billed to your credit
  balance even on `:free` models** — roughly $0.005 per fact-check (Exa engine, up to
  10 results). A $0-credit account gets 402 errors on verification, so hold a small
  credit balance.
- Free-variant rate limits: **20 requests/min**, and **50 requests/day** with under $10
  in lifetime credit purchases vs **1,000/day** once you have bought $10+. The gate
  alone makes ~5 calls/min while a stream runs, so the 50/day cap dies in minutes —
  **a one-time $10 top-up is the practical minimum** (the credits themselves barely
  deplete: only web searches consume them).
- At the app's throttled rate (gate every 12 s, verifications capped by `VERIFY_RPM=8`
  and deduped), expect pennies per multi-hour stream.

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
