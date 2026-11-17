# Live Settings and the Speech Model's Lifecycle

*Spec, 2026-11-16. Written from an audit of `master` plus the release-plan branches,
before fixing. It says what each setting must do when it changes, and when the
in-browser speech model may load. Rows marked **fix** were wrong when this was written;
the rest already behaved as described. **All the fixes are implemented** on branch
`fix/model-loading-and-live-settings`; §3 says how each is checked.*

## 1. The speech model loads once

**Rule.** In normal use the speech model is loaded once and then reused: stopping a
session and starting another (on the same stream or a different one) must not load it
again. A new load is expected only when:

- the model or the device changes (another model chosen, the GPU blocked, a power
  preference changed), since a different model has to be built;
- the GPU fails mid-session and transcription moves to the CPU build (the existing
  recovery path);
- the model has been idle for longer than the warm period (below), and is released to
  give its memory back.

**Where the model lives.** Sessions run the model in the offscreen document. Before
this spec that document was closed whenever a session ended, so every session rebuilt
the model: the files come from the browser's cache, but building it, compiling GPU
shaders and the warm-up pass take seconds and show as "Loading speech model" each time.
**Fix:**

| Part | Behaviour |
|---|---|
| Offscreen document | When a session ends, a loaded model is **parked** (stopped, emptied of any half-heard speech, kept in memory) instead of disposed. The next session reuses it if its configuration (model revision, device, dtype, threads, power preference) is identical, and reports "ready" at once; otherwise the parked model is released and the new one loaded. A model that failed is never parked |
| Service worker | After a session that parked a model, the offscreen document is kept open for a **warm period of 10 minutes**, then closed (a `chrome.alarms` alarm, since a service worker's timers do not survive it sleeping). Starting a session cancels the alarm |
| Speech self-test | Releases a parked model first, so two copies of a large model never compete for GPU memory |

**Loads that are separate on purpose**, and stay:

- The speech **self-test** (Settings → Speech recognition, and the welcome flow) loads
  each configuration it tries in a fresh worker, so that a configuration that crashes
  the GPU cannot affect the next one.
- The welcome flow's **recorded example** runs in the app page, once. Moving it into
  the offscreen document, so that the first real session could reuse it, is possible
  but not part of this change.

## 2. Settings apply as soon as they change

**Rule.** A setting changed while a session runs takes effect in that session at once,
unless it cannot without restarting capture or loading a model. Those apply at the next
Start, and the screen says so while a session is running.

"Live" below means: within a second, in the running session, without pressing Stop.

### Synced settings (`chrome.storage.sync` "settings")

| Setting | Browser engine | Local backend | Status |
|---|---|---|---|
| Sensitivity | Live: `OFFSCREEN_CONFIG` → pipeline | Live: `config` frame | already live |
| Topics | Live, as above | Live, as above | already live |
| **Show what is heard (live caption)** | Live: the offscreen document starts or stops forwarding its transcripts | Live: `config` frame with `send_transcripts`, which the backend already applies (`apply_live_config`) but the wire frame did not carry | **fix** (both said "applies the next time you press Start") |
| Overlay position, how long verdicts stay | Live in every open stream tab (content script watches storage) | same | already live |
| On-screen claims (video frames) | Next Start (capture must be restarted to include video); local backend only | same | already labelled |
| Backend address | Next Start; the app re-checks the backend at once | same | already labelled |
| **Keep history for N days** | Applied at once: the service worker runs the clean-up when the setting changes, not only at the daily alarm | same | **fix** |

### Engine settings (`chrome.storage.local`)

| Setting | Running browser-engine session | Status |
|---|---|---|
| **Provider and model per step** | Live: the service worker recomputes the plan and sends it (`OFFSCREEN_ENGINE`); the pipeline's next gate or verify call uses it. Its transcript buffer, duplicate memory and spending counts carry over | **fix** (used the plan from when the session started) |
| **An API key added or replaced** | Live, as above: the next call uses the new key | **fix** |
| An API key in use removed | Ends the session at once | already |
| Spending limits | Live (`OFFSCREEN_BUDGETS`) | already |
| **Where it runs** (this browser / local backend) | Next Start; while a session runs, the setting shows "applies when you next press Start" | **fix** (notice) |

### Speech settings (`chrome.storage.local` speech record)

| Setting | Running session | Status |
|---|---|---|
| **Engine, model, device, power preference** | Next Start (a model has to load); while a session runs the Speech card says so. At that Start a parked model with a different configuration is released and the new one loaded | **fix** (notice) |

### Local backend settings (stored by the backend)

| Setting | Behaviour | Status |
|---|---|---|
| **Provider key, provider and model per step** | Saving them rebuilds the backend's provider stack, which ends live sessions (`credentials_updated`) because each session holds the old clients. **The extension now starts the session again on the same tab at once**, reusing a parked speech model, so the change applies with a pause of a second or two. If Chrome refuses the restart (no capture permission without a new click), the popup says "press Start again" as before | **fix** |

## 3. How this is checked

- Unit tests: parking and reuse (same configuration reused, a different one released,
  a failed model never parked); the pipeline switching stages mid-session with its
  state intact; the live caption on and off; the backend accepting `send_transcripts`
  in a `config` frame.
- In Chrome (`scripts/run_browser_speech_e2e.mjs`, which runs the real model against a
  stand-in backend): after Stop the offscreen document stays open with a 10-minute
  alarm; a second session reports `reused` and is ready within a fraction of a second;
  turning the live caption on and raising sensitivity mid-session reach the backend as
  `config` frames at once; the reused model transcribes the new session; when the warm
  alarm fires the offscreen document closes; and after the backend sends
  `credentials_updated` the session reconnects by itself on the warm model.
