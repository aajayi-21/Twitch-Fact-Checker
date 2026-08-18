# Streamer Product — Stage 2 Plan & UX Guide

*Written 2026-08-17 on `feat/streamer-chat-bot`, after stage 1 (session
registry, event hub, source tiers, outbound formatter — commits `9ee102f`,
`d040521`, `2c7f486`). Stage 2 is everything between "safe strings exist" and
"a streamer runs this for a whole broadcast."*

---

## 1. Research: how streamers actually use tools like this

Four findings from looking at the incumbent tooling (Nightbot, StreamElements,
OBS, Stream Deck ecosystems), each of which changed a design decision below.

### 1.1 Bot onboarding has a muscle-memory convention

Nightbot and StreamElements both onboard the same way: **one "Activate/Join"
action → the bot appears in chat → the streamer mods it** (StreamElements
grants mod automatically; Nightbot tells you to `/mod nightbot`). Streamers
have done this dance before. Our setup should present as the same checklist —
*connect account, bot joins, mod the bot* — rather than inventing a novel flow.
Conveniently, "mod the bot" is **also our consent proof** (only the broadcaster
can grant it), so the conventional step and the safety requirement are the same
step.

### 1.2 The control surface streamers actually look at mid-stream is OBS itself

The established pattern for "a panel I interact with during the stream but
viewers don't see" is an **OBS custom browser dock** (Docks → Custom Browser
Docks → paste URL). The canonical power setup is explicitly *both halves from
one tool*: **dock the control panel inside OBS, add the display output as a
browser source**. That is exactly our shape — `/control` as a dock, `/overlay`
as a source — so:

- `control.html` must have a **compact dock layout** (narrow, vertical,
  big-target buttons) — triggered by `?dock=1` or just responsive breakpoints.
- Onboarding should offer both URLs side by side: "add the overlay as a Browser
  Source; add the panel as a Custom Browser Dock."

### 1.3 Mid-stream attention is a glance and one click — budget for exactly that

Streamers build Stream Decks and chat-command macros specifically to avoid
tabbing out; anything that needs a browser tab and a mouse mid-game will not be
used. So the mid-stream controls are tiered by attention cost:

| Cost | Surface | What lives there |
|---|---|---|
| zero (already watching it) | **their own chat**: `!fc mute`, `!fc off`, `!fc wrong <id>` | the panic path — works from a phone, works for mods, no context switch |
| one glance | **OBS dock** of `/control` | MUTE button, review queue (J/K/Enter/X keys), posts-budget meter, three status dots |
| between games | full `/control` tab | settings, template editor, Twitch connection |

Review mode is only viable because of the dock: a queue item must be
approvable in one keystroke, and the tab-title badge (`(2) Fact-Checker`)
covers the second-monitor case. **No sounds ever** — desktop audio is often
captured into the broadcast.

### 1.4 Token expiry: DCF is viable and solves it; paste-token is the fallback

Verified against Twitch's auth docs and dev forums:

- Twitch user access tokens live ~4 h. Expiry does **not** drop a live IRC
  connection — it breaks the *next* reconnect, i.e. a routine network blip
  hours into a stream. This is the #1 silent-failure mode to design out.
- **Device Code Flow works for public clients with no client secret**, and
  public clients can refresh without one. Refresh tokens are **one-time-use
  (rotate on every refresh)** and expire after **30 days of inactivity** — so
  we must persist the rotated pair on every refresh, and a streamer who goes
  live at least monthly never re-authenticates.
- App registration is free (requires 2FA on the Twitch account). Client IDs
  are public by design, but Twitch forbids sharing one client ID across
  *different* applications — and this repo has no registered app to ship. So:
  the operator either registers their own public app once (~2 min, gives the
  "connect with Twitch" experience + auto-refresh) or skips OAuth entirely and
  pastes a token (simplest, but they re-paste when it expires).

**Decisions locked by this research** (previously open):

1. **Probation by default.** A newly armed channel starts in `review` mode and
   auto-graduates to `auto` after 10 approved posts with ≤1 retraction — or
   immediately when the broadcaster types `!fc trust`. Rationale: the kill
   criterion ("≤1 disputed-wrong public verdict per 10 streams") is currently
   unmeasured; the first streams are the highest-variance moment and the only
   source of the approve/reject data that makes the predicate tunable.
2. **DCF primary, paste-token fallback**, both writing the same `.env` keys via
   the existing `upsert_env_values`. `TWITCH_CLIENT_ID` has no default;
   without it the panel simply shows only the paste path.

---

## 2. The streamer journey (the spec the UI is built against)

### Moment 1 — First-run setup (once, ~5 minutes)

`./backend/run-streamer.sh` prints the panel URL. `/control` renders a
checklist; every item is either done (green) or shows exactly one action:

1. **AI provider** — reuses the existing `/setup/status` + `/setup/credentials`
   flow (already built for the extension options page).
2. **Connect Twitch** — "Connect with Twitch" (DCF: big `user_code`, link,
   poll) or "Paste a token instead" (validated via `GET /oauth2/validate`;
   `oauth:` prefix stripped server-side; scopes `chat:read chat:edit` checked
   with a specific error naming the missing scope).
3. **Channel to watch** — text input, prefilled from the live ingest session
   when one exists.
4. **Mod the bot** — shows `/mod <botlogin>` to copy; a live check flips green
   when the bot's `USERSTATE` in that room carries `mod=1`. Copy explains the
   why: consent proof + link permissions + the 100 msgs/30 s tier.
5. **Arm it** — the *broadcaster* types `!fc enable` in their own chat
   (user-id must equal room-id). Records `armed_at` / `armed_by_user_id` —
   the auditable consent row. Panel flips to "armed · review mode (probation)".
6. **OBS** — two copy buttons: overlay URL (Browser Source, with the two
   settings to disable: *shutdown when not visible*, *refresh on activate*)
   and panel dock URL (Custom Browser Docks). **Send test verdict** button
   fires a synthetic verdict through the hub so they can position the overlay
   before ever going live.

### Moment 2 — Pre-stream ritual (every stream, <1 minute)

One command: `uv run fact-checker-ingest twitch.tv/<me>` (or
`--source device` for zero-latency local capture). The panel's empty state
*teaches* this command with a copy button. Three status dots confirm:
`backend ● ingest ● chat ●`. Bot presence in chat is exactly coextensive with
a live ingest session (join on session start, part after a linger window) —
no session, no bot.

### Moment 3 — Mid-stream (glances)

Per §1.3. Everything posted or suppressed appears in the live feed with a
reason chip (`posted` / `capped` / `label filtered` / `stale` / …) so "why
didn't it post that?" never requires reading logs. `!fc wrong <id>` retracts
publicly AND writes a 👎 feedback row — the appeal path and the eval set are
the same mechanism.

### Moment 4 — Post-stream (optional)

Dashboard session report; disputed verdicts to adjudicate; the
wrong-per-10-streams metric accumulating against the kill criterion.

---

## 3. Implementation phases

Order chosen so every phase is independently testable and the dangerous parts
land before the parts that make them reachable.

### Phase A — Rate-limit primitives *(small, shared-core touch)*
- `TokenBucket.try_acquire() -> bool` (non-blocking; chat drops, never queues).
- Fix the latent token-steal bug in `acquire()` (sleeps holding the lock, then
  decrements unconditionally — must re-check in a loop once `try_acquire`
  exists). Ships with its own test.
- New `streamer/chat/limits.py`: `SlidingWindowCap` (deque of timestamps — a
  fixed bucket would allow 2× the cap across a boundary), `PostingLatch`
  (QuotaCooldown-shaped, tripped by Twitch `NOTICE` msg-ids: `msg_ratelimit` →
  1 h + operator alert; `msg_banned`/`msg_channel_suspended` → hard-disable the
  channel). Every limiter takes injected `now` — a 60-minute window cannot be
  tested with real sleeps.

### Phase B — IRC transport (`streamer/chat/transport.py`)
- `ChatTransport` Protocol: `connect() / send(text) -> str | None /
  messages() -> AsyncIterator[ChatMessage] / close()`. `send` returns an id
  (not a bool) so a future Helix implementation supports deletion without a
  protocol change.
- `TwitchIRCTransport`: `CAP REQ :twitch.tv/tags twitch.tv/commands` → `PASS`
  → `NICK` → `JOIN`; wait for `001`; a WS frame may carry several `\r\n` lines
  (the classic parsing bug); PING→PONG echo; `RECONNECT`; `NOTICE …
  authentication failed` → distinct `AuthFailed`; USERSTATE → own mod status;
  tag unescaping (`\s \: \\ \r \n`) that never reintroduces a newline.
- **`PASS` redaction is a hard rule**: any wire logging prints
  `PASS oauth:…abcd`. A naive "log every line sent" logs the token on line 1.
- **If tags capability is NAKed, refuse to run** — no tags means no
  authorization means no posting.
- Send path: sanitize → min-gap 1.2 s → `try_acquire` on an 18/30 s budget →
  PRIVMSG. The budget protects the *account* (exceeding Twitch's cap = ignored
  for one hour); the product cap lives in policy, not here.
- Tests against a **local fake IRC server** (`websockets.serve` speaking the
  real handshake) — the suite stays fully offline.

### Phase C — Policy + consent + persistence
- `streamer/chat/policy.py`: pure `decide(event, policy, state, now) ->
  Decision(action, reason)` with ordered gates — authorization → operational
  state → label → verdict integrity (sources ≥2 distinct domains, best tier
  ∈ {A,B}, no tier D, `used_fallback` never posts, politics/health require
  tier A) → claim shape (already in `format.py`) → topic → freshness
  (`claim_age_s` ≤ 90 s) & repetition → **rate caps last**, so a `hourly_cap`
  drop is known to have been otherwise postable — the only honest input to
  "should the cap be 6 or 8?".
- Clamps live in the policy module, not the config layer: UNVERIFIED/fallback/
  zero-source never post; ≤12 posts/hour; ≥45 s between posts; label set
  ⊆ {FALSE, MISLEADING} (+TRUE only explicitly); named template variants
  instead of free-form format strings (an editable format string is an
  injection surface and can delete the sources).
- `streamer/chat/consent.py`: `assert_postable_channel(...)` — four proofs,
  checked **on every post**, fail-closed: env allowlist (operator intent), bot
  is mod in the room (broadcaster grant, Twitch-asserted), `armed_at` row
  (broadcaster's `!fc enable`, id-verified), and a **live ingest session whose
  (platform, channel) matches the chat channel** — which makes cross-channel
  weaponization structurally impossible and stops posting when the stream ends.
- Streamer DB tables (`streamer.db`, NOT the viewer's): `chat_channels`
  (mode, armed/disarmed, `muted_until` as absolute UTC so a restart cannot
  resurrect posting, probation counters, config JSON), `chat_posts` (one row
  per verdict per decision — posted/queued/suppressed/failed + reason + the
  message text + `stream_time_s` VOD locator), `chat_commands` (verb only,
  never args), `chat_engagement` (counts only, never chat text).

### Phase D — Commands + the bot orchestrator
- `commands.py`: `!fc` grammar; auth from tags with **numeric user-id only**
  (display names are homoglyph-able); viewer verbs (`why`, `dispute`, `about`,
  `status`), mod verbs (`on/off/mute/unmute/cap/topics/labels/retract`),
  broadcaster verbs (`enable/disable/correct/trust`). Silent denial for
  viewers (loud denial is a spam amplifier anyone can trigger). **No user
  text is ever interpolated into an outbound message** — handles re-validated
  against `^[0-9a-f]{4,6}$`, topics against the fixed tuple.
- `bot.py`: `ChannelBot` (consume hub → decide → send; connect loop with
  jittered backoff owns reconnection) + `ChatBotSupervisor` keyed by
  `channel_key`, refcounted acquire/release following the ingest session with
  a linger window (no JOIN/PART churn on reconnects).
- **Dry-run mode is the default until armed**: connects, evaluates, logs the
  exact message it *would* post, sends nothing. The pre-launch gate from the
  safety checklist.
- Probation graduation logic; disclosure post on join and every N posts;
  hourly token re-validate + T-15 min refresh (persist the rotated pair);
  auto-mute (never auto-retract — that hands chat a brigade button) at ≥3
  distinct disputers on one handle.

### Phase E — The streamer app (`streamer/main.py`, `routes.py`, `config.py`)
- Own FastAPI app, port **8711**, `streamer.db`, own lifespan (reuses the
  shared transcriber/LLM/db machinery; builds hub + registry + supervisor).
  Mounts the shared routers (`ws`, `setup`, `stats`, `feedback`, `debug`) so
  it is self-contained — the viewer app remains untouched.
- New routes: `GET /overlay`, `GET /control` (FileResponse pattern),
  `GET /ws/events` (read-only hub subscriber; **explicit Origin check** — CORS
  does not apply to WebSockets, and this closes a pre-existing hole on
  `/ws/audio` too; optional `EVENTS_TOKEN` query param because OBS cannot set
  headers — never the Twitch token, tokens in URLs land in logs),
  `GET/POST /setup/twitch` (validate → upsert `.env` → hint-only status),
  `POST /setup/twitch/device` + poll (DCF), `GET /bot/status` +
  `POST /bot/{mode,mute,settings}` + `POST /bot/queue/{id}/{post,skip}`
  (every mutation returns the same `BotStatus` shape — WS pushes are
  idempotent applies keyed by id; `GET /bot/status` poll every 15 s is the
  drift reconciler), `POST /events/test` (the synthetic verdict).
- `run-streamer.sh` mirroring `run.sh`.

### Phase F — The two pages (single-file, zero build step, like `dashboard.html`)
- `overlay.html`: transparent body, `pointer-events: none`, transform/opacity
  animation only (no backdrop-filter — nothing behind it to blur, and it costs
  encoder headroom in CEF), sources as **spans not anchors** (a click would
  navigate the browser source away mid-broadcast), `?preview=1` checkerboard,
  params `position/labels/topics/duration/scale/max/margin` — all **narrowing
  only** (a URL param must never widen what the server policy allows), dwell
  bar, never-give-up reconnect (an OBS source runs 8 hours; giving up kills it
  silently), disconnect badge only in preview mode.
- `control.html`: command bar (MUTE, mode segmented control, budget meter,
  three dots, token-expiry chip) → setup checklist → review queue (J/K/Enter/X,
  TTL countdown, exact message preview with live 500-char count, edit-and-post)
  → live feed with reason chips + 👍/👎 → recent posts → settings
  (`<details>` sections; template gets explicit Apply — highest blast radius).
  **Dock layout** at narrow widths per §1.2. Topic colors fetched from a new
  `GET /meta/topics` (backend-canonical, stops the color quadruplication).

### Phase G — Ingest CLI (`streamer/ingest/`)
- `fact-checker-ingest [TARGET]`, sources `streamlink | device | wav` behind
  one `AudioSource` async-iterator interface (respawn inside the source,
  reconnect inside the socket, the CLI is a dumb pump).
- streamlink+ffmpeg via `os.pipe()` — **never a shell**; channel validated
  against `^[A-Za-z0-9_]{1,25}$` before argv; `-nostdin`; both parent fds
  closed; stderr drained to a ring printed on death; `--retry-streams 30
  --retry-max 0` so "streamer is offline" waits inside streamlink;
  quality chain `audio_only,worst` (not all streams transcode audio_only).
- Hello carries `platform/channel/stream_title` (the `stream_wav.py` gap that
  makes sessions invisible to `/stats/channels`); pure `classify_error()`:
  `superseded` is **terminal** (prevents preempt ping-pong with a browser
  tab), `not_configured` points at `/control`, transient codes rate-limited
  to one warning per 30 s. `stream_wav.py` collapses to a thin wrapper.
- `streamlink`/`ffmpeg` stay system binaries probed with `shutil.which` +
  per-OS install hints; `sounddevice` an optional extra; `websockets` moves
  dev → main dependencies (the CLI is a product surface).

### Phase H — Docs + launch gate
- README "Streamer mode" section (the journey in §2, the two OBS setup
  gotchas, the `!fc` table); `.env.example` blocks; mark improvement-report
  §6.2 resolved.
- The pre-launch checklist as `docs/streamer-launch-checklist.md`: green
  suite; injection/echo/consent tests cited by name; bot account (bot-obvious
  name, verified email+phone, scopes exactly `chat:read chat:edit`, **no
  API-level moderation scopes** — mod status in-channel only); ≥2 h dry-run
  reviewed message-by-message; `!fc mute`/`!fc off` demonstrated by the
  broadcaster AND a mod; operator kill switch reachable in <30 s.

---

## 4. Test map for stage 2

| Module | File | The tests that matter most |
|---|---|---|
| limits | `test_chat_limits.py` | sliding-vs-fixed boundary case; `try_acquire` under a sleeping `acquire`; latch trip table from NOTICE msg-ids; FakeClock throughout |
| transport | `test_chat_transport.py` | multi-line frames; tag-escape never yields a newline; PASS redaction (assert on captured log text); NAKed tags refuses to run; auth-failure classification; budget drop not delay |
| policy | `test_chat_policy.py` | the parametrized table (~40 rows of signal combos → action+reason); one test per clamp proving config cannot cross it; purity (monkeypatch `time.monotonic` to raise) |
| consent | `test_chat_consent.py` | each of the four proofs failing individually; demod mid-session stops the next post; session end stops posting; restart honors `mode=off` and unexpired mutes |
| commands | `test_chat_commands.py` | role matrix; homoglyph display-name rejected (id wins); `!fc dispute <id> /ban everyone` echoes neither; non-hex handle → no reply |
| bot e2e | `test_chat_bot_e2e.py` | `/debug/text` → exactly one PRIVMSG on the fake transport (golden string); unarmed/unmodded/mismatched-channel → nothing sent + the right `chat_posts.reason`; `!fc wrong` → retraction string + feedback row; hourly cap: 7 in → 6 out |
| app split | `test_streamer_app.py` | streamer app serves `/overlay` `/control` `/ws/events` on its own state; viewer app has none of them; two apps, two DBs, zero cross-talk |
| pages | `test_pages.py` | 200 + text/html; regex proves zero external asset references (enforces no-build-step permanently) |
| ingest | `test_ingest.py` | identity derivation table; hello carries channel; streamlink argv as a golden list (proves no shell); `classify_error` policy table |

Everything offline: fake IRC server, `httpx.MockTransport` for Twitch
endpoints, FakeClock for every limiter, the existing conftest fakes for the
LLM/Whisper layers.

---

## 5. What stage 2 explicitly does NOT include

Held to keep the blast radius bounded — each is a deliberate later decision,
not an oversight:

- **Contradictions in chat** (overlay/panel only; no citations, double
  paraphrase, no external ground truth — a human-clicked "callback" template
  is the only future path).
- **EventSub/Helix transport** (the `ChatTransport` seam exists for it).
- **Kick/YouTube chat posting** (ingest is already multi-platform; posting is
  Twitch-only per the MVP non-goals).
- **Wiring source tiers into `_enforce_invariants`** (changes every existing
  overlay user's verdicts; needs its own eval against the feedback table).
- **Message deletion via Helix on retract** (needs different scopes; the
  retraction message stands alone in v1).
- **Multi-channel in one process** (one process per channel; the registry
  supports `channel` scope when that changes).
