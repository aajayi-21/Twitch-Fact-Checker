# Twitch Live Fact-Checker

A Chrome extension (Manifest V3) plus a local Python backend that fact-checks a live
Twitch stream in real time. The extension captures the tab's audio and streams it to a
local FastAPI server, which transcribes it with `faster-whisper`, extracts verifiable
claims with an LLM "claim gate", verifies them with a web-search-grounded LLM call, and
pushes **TRUE / FALSE / MISLEADING / UNVERIFIED** verdicts (with sources) back to a
Shadow-DOM overlay rendered over the Twitch player.

The LLM layer is provider-switchable: **OpenRouter** (default — free model + the `web`
search plugin) or **Gemini** (requires a paid-tier key for search grounding). Set
`LLM_PROVIDER` in `backend/.env`.

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
│ content script (twitch.tv)      │           │  POST /debug/text (test path)│
│   Shadow-DOM toast + history    │           │  GET  /healthz               │
└─────────────────────────────────┘           └───────────────────────────────┘
```

No database, no build step, no auth — everything runs locally, single user.

## Prerequisites

- Python 3.11+
- Google Chrome 116+
- An OpenRouter API key: https://openrouter.ai/keys (default provider), **or** a
  paid-tier Gemini API key (https://aistudio.google.com/apikey) if you switch
  `LLM_PROVIDER=gemini` — Gemini search grounding is not available on the free tier.

## Backend setup

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env and set OPENROUTER_API_KEY=<your key>   (from https://openrouter.ai/keys)

uvicorn app.main:app --host 127.0.0.1 --port 8710
```

`.env` holds your real API key — it is gitignored and must never be committed.

Check it is up: `curl http://127.0.0.1:8710/healthz` (also echoes the active
`llm_provider`, `gate_model`, and `verify_model`).

**First run:** the Whisper model (`distil-small.en`, int8, ~170 MB) is downloaded at
startup — expect a one-time delay. Startup fails loudly if the active provider's API
key (`OPENROUTER_API_KEY`, or `GEMINI_API_KEY` with `LLM_PROVIDER=gemini`) is missing
or the model download fails. On slow machines, set `WHISPER_MODEL=base` in `.env`.

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

**Switching back to Gemini:** set `LLM_PROVIDER=gemini` and `GEMINI_API_KEY` in `.env`
(models: `GEMINI_GATE_MODEL` / `GEMINI_VERIFY_MODEL`). Search grounding requires a
paid-tier Gemini key, which is why OpenRouter is the default.

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

## Extension install

1. Open `chrome://extensions/` and enable **Developer mode** (top right).
2. Click **Load unpacked** and select `/home/ade/misc/twitch_fact_checker/extension`.
3. After code changes, click the refresh icon on the extension card to reload.

## Usage

1. Start the backend, then open a Twitch live stream (`https://www.twitch.tv/...`).
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
list of this session's verdicts plus a connection status dot. History is in-memory —
a page reload clears it.

**Options** (right-click icon → Options): backend URL (takes effect on next Start),
sensitivity (low/medium/high — applied live), popup position (4 corners), popup
duration, and an optional live-transcript toggle.

## Testing recipes

All commands from `backend/` with the venv active.

```bash
# Unit/integration tests (slow real-Whisper tests are excluded by default
# via addopts = "-m 'not slow'"; run them with: pytest -m slow)
pytest -m "not slow"
```

**No-audio end-to-end** — exercise gate → dedupe → grounded verify; if a WS session is
open, the verdict also pops on the Twitch tab:

```bash
curl -X POST http://127.0.0.1:8710/debug/text \
  -H "Content-Type: application/json" \
  -d '{"text": "The Great Wall of China is visible from space with the naked eye"}'
```

**Audio pipeline without a browser** — build a TTS fixture (requires `espeak-ng`) and
stream it over the real WS protocol:

```bash
python scripts/make_fixture_wav.py          # writes tests/fixtures/claims_16k.wav
python scripts/stream_wav.py tests/fixtures/claims_16k.wav   # prints server frames
python scripts/stream_wav.py tests/fixtures/claims_16k.wav --speed 3   # backpressure
```

## Troubleshooting

- **"Backend offline" in the popup** — start the server:
  `uvicorn app.main:app --host 127.0.0.1 --port 8710` from `backend/`.
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
