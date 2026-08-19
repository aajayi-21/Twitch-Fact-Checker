# Distribution, Marketing & Monetization Plan

*Written 2026-08-18. Companion to [`business-analysis.md`](business-analysis.md), which
answered "is there a business?" (yes, a niche one, sold to streamers). This document
answers the three questions that follow from that: **should it be open source, how does
it get distributed, and how does it make money?** Repo facts are measured from this
codebase; market and legal facts are cited in §9 and should be re-checked before any
irreversible decision.*

---

## 1. Verdict up front

**Yes — open-source it, and monetize the hosting, not the code.**

For this specific product the usual open-source objection ("someone will take it and not
pay") barely applies, because **the people who can run it were never going to pay, and the
people who would pay cannot run it.** The install is a 7.5 GB virtualenv, a `uv`
toolchain, ffmpeg, a speech model download, and two API keys. A talk-show streamer with an
Elgato and a Stream Deck is not doing that. A developer who *would* do that is not a
customer at any price.

What open source buys, for this product specifically, is the one thing a fact-checking bot
cannot buy any other way: **auditability**. Nobody should trust a black box that publishes
"❌ FALSE" to their audience under their channel's name. `chat/policy.py`, the prompts,
the source-tier table and the four consent proofs *are* the marketing material.

The plan in one line: **AGPL-3.0 backend + MIT extension + a closed multi-tenant control
plane + a hosted service metered in fact-checks, priced $7–29/mo.**

| Question | Answer | Where |
|---|---|---|
| Open source? | Yes — AGPL-3.0 backend, MIT extension, sole copyright retained for dual-licensing | §2, §3 |
| Split the repos? | **No.** Split the licences and the release artifacts | §3.4, §4 |
| What's closed? | The multi-tenant control plane, billing, ops — nothing a self-hoster needs | §3.3 |
| How does it earn? | Hosted service, metered in checks, not hours or seats | §5 |
| First paid tier | Cloud BYO-key at $7/mo — near-zero COGS, tests willingness-to-pay honestly | §5.3 |
| Biggest legal item | EU AI Act Art. 50 disclosure — **enforceable since 2 Aug 2026** | §7.1 |
| Biggest structural risk | Hosted mode makes *you* the publisher of every verdict | §7.2 |

---

## 2. The open-source decision, argued properly

### 2.1 The asymmetry that decides it

Open-core arguments usually turn on how much revenue leaks to self-hosters. Measure the
leak here:

| Barrier to self-hosting | Measured in this repo |
|---|---|
| Python environment | 7.5 GB `.venv` with the GPU extra; `uv` required |
| System binaries | ffmpeg + streamlink on PATH |
| First-run download | ~170 MB speech model |
| Secrets | An LLM provider key **and** a Twitch bot token the user must mint |
| Ops | A process that must stay up for the whole broadcast, plus OBS wiring |
| Multi-tenancy | None — one channel per process by design (`SESSION_PREEMPT_SCOPE=global`, one `twitch_channel` in `.env`) |

That is a developer-grade install. The leak is confined to developers, and developers who
self-host a niche creator tool are a rounding error in revenue but a meaningful fraction of
your contributors, bug reports, and — critically — the friends of streamers who install it
*for* a streamer.

### 2.2 Trust is the product, and closed source undermines it

This is not a chat bot that posts a link. It publishes **judgements about truth, in
someone else's chat, under their name, to their audience**. The single biggest risk in
`business-analysis.md` §6 is "one confidently wrong FALSE is a clip against the product."

Everything that mitigates that risk is a *rule*, and rules are only credible if they can be
read:

- UNVERIFIED can never post — not a setting, a constant.
- ≥2 distinct domains, best source tier A/B, never a tier-D source, never a
  fallback-parsed verdict, tier-A required for politics/health.
- Four independent consent proofs, checked on every post, fail-closed.
- Dry run defaults **on**; new channels sit in review-mode probation until 10 approvals.

A competitor can claim all of that. Only you can let a skeptical streamer's mod read the
file. "Here is the code that decides what your bot is allowed to say" is a marketing asset
no closed competitor will match, and it converts exactly the audience you need first
(technically-minded, mod-team-having, debate-genre channels).

### 2.3 Open source is the cheapest distribution channel you have

You have no marketing budget and no audience. Developer-tool GTM research is consistent
that discovery happens through open-source projects, communities and documentation rather
than ads. A public repo gets you: GitHub search, `awesome-twitch` lists, HN/Reddit launch
posts that only work if the code is public, and the self-hosted crowd (r/selfhosted) who
are prolific sharers. The extension already exists to be free forever; making the whole
thing public is the same move with more surface area.

### 2.4 What open source does *not* protect against — and the fix

The realistic threat is not a streamer self-hosting. It is **a funded competitor
(StreamElements/Streamlabs-class, or a fast solo dev) lifting `backend/streamer` and
shipping "fact-check bot" as a hosted feature next quarter.** That is precisely the
free-riding case AGPL-3.0 and the newer source-available licences were written for.

AGPL-3.0 handles it well enough: a hosted derivative must offer its users the complete
corresponding source, including modifications. For a competitor that means either shipping
their improvements back into the commons (fine — you can merge them) or not doing it. It
is not airtight (see §3.5), but it is OSI-approved, which preserves the "genuinely open
source" claim that §2.2 and §2.3 depend on.

### 2.5 What would change this answer

Flip to source-available (FSL/BUSL) if, and only if:

- A commercial hosted clone actually appears and AGPL doesn't deter it, **or**
- A platform (Twitch/Kick/StreamElements) starts negotiating an acquisition or an
  integration where exclusivity has real value, **or**
- The hosted service crosses ~$5k MRR and the code, not the ops, becomes the moat.

Until one of those happens, the openness is worth more than the protection.

---

## 3. The licensing plan

### 3.1 File layout (no repo split needed)

| Path | Licence | Reasoning |
|---|---|---|
| `extension/` | **MIT** | Pure funnel. Useless without a backend, so zero revenue leak. Maximum spread, zero friction for anyone vendoring or forking it. |
| `backend/app/`, `backend/streamer/`, `backend/tests/` | **AGPL-3.0-only** | The commercial surface. Self-hosters comply trivially (they distribute nothing); a hosted competitor cannot close their fork. |
| `docs/` | **CC BY 4.0** | Let people quote the methodology; that's the point. |
| `cloud/` (future, separate private repo) | Proprietary | Multi-tenant control plane — see §3.3 |

Put a `LICENSE` at the root (AGPL-3.0), a `LICENSE` in `extension/`, and a short
`LICENSING.md` explaining the split in plain language. **There is currently no `LICENSE`
file anywhere in this repo, which means nobody may legally redistribute any of it — this
blocks store submission, pilots, and Docker publishing today.** It is the single highest
priority item in this document.

### 3.2 Keep the right to relicense

AGPL-outbound only stays a *choice* if you own all the copyright. From the first outside
contribution onward, either require a lightweight CLA (CLA-assistant bot, ~10 minutes to
set up) or a DCO plus an explicit "contributions are licensed to the project under terms
permitting relicensing" note in `CONTRIBUTING.md`. Without it, §2.5's escape hatch closes
permanently the day you merge someone else's PR.

### 3.3 What stays closed, and why that line is honest

Closed: **the multi-tenant control plane** — tenant/billing tables, per-channel secret
storage, the fleet orchestrator that runs N single-tenant pipelines, the Stripe wiring, the
ops dashboards. None of it is a *feature* a self-hoster is missing; it is scaffolding that
only means anything if you're running other people's channels. That is the cleanest
possible open-core line: nobody feels cheated, because nothing they'd want is behind it.

Deliberately **not** closed: the posting policy, prompts, source-tier table, consent logic,
overlay styles, console. Crippling any of those turns §2.2 into a lie.

### 3.4 Repo structure (unchanged, and that's the point)

The coupling is one-directional and heavy: `streamer/` imports `app/` **41 times across 12
files** (`app.events`, `app.sessions`, `app.models`, `app.db`, `app.config`, `app.setup`,
`app.ws`, `app.stats`); `app/` imports `streamer/` **zero times**. The streamer product is
a superset that mounts the viewer's routers and subclasses its `Settings`. Splitting means
either versioning a 9k-line core you change weekly, or forking it and drifting — in exactly
the files that carry the consent and policy invariants. One repo, one 1030-test suite,
separate release workflows keyed by path filter.

### 3.5 Known limits of AGPL here

Be clear-eyed: AGPL's network clause is famously under-tested in court, and a determined
competitor can put a thin proprietary orchestration layer around an unmodified AGPL core
and argue compliance by publishing nothing but the untouched upstream. AGPL raises the
cost and the reputational risk of cloning; it does not make it impossible. If that
scenario materialises, §2.5 says relicense **future versions** to FSL-1.1-Apache-2.0
(all features open, converts to Apache 2.0 after two years) — which needs §3.2 to be in
place today.

### 3.6 Trademark: the actual moat

Licences protect code; **trademarks protect the business.** The name, logo and the bot's
chat identity are what streamers recommend to each other, and they are cheap to defend.
Pick a distinctive name, register the domain and the Twitch/Kick/YouTube handles on day
one, and note the hard constraint: **Twitch's guidelines forbid using the Twitch name,
logo or Glitch in your product branding.** "TwitchFactCheck" is not an available name.
Reserve `.com` + the bot account + a Discord before launching anything public.

---

## 4. Distribution

Four audiences, four artifacts, one repo.

### 4.1 The ladder (ship in this order)

| # | Artifact | Audience | Effort | Gate to start |
|---|---|---|---|---|
| 1 | **Tagged GitHub release** + zipped extension | developers, self-hosters | hours | LICENSE + CI |
| 2 | **`uvx` one-command install** of both servers | technical streamers, pilots | days | 2 console-script entry points |
| 3 | **Extension in CWS (unlisted) + AMO (signed)** | viewers — the funnel | ~1 week incl. review | privacy disclosures |
| 4 | **Desktop bundle** (Tauri/PyInstaller + tray) | *actual* streamers | 2–4 weeks + signing costs | pilot demand proven |
| 5 | **Hosted service** | paying streamers | 4–8 weeks | §8 kill criteria passed |

### 4.2 Notes that matter per rung

**Rung 2 — `uvx` is nearly free.** You already have hatchling packaging,
`packages = ["app", "streamer"]`, and a `fact-checker-ingest` console script. Add
`fact-checker` and `fact-checker-streamer` entry points and the install becomes one
command from a git URL — no PyPI account required to start. Biggest ratio of friction
removed to work done in this entire document.

**Rung 3 — the extension is store-ready in the ways that usually fail.** Manifest V3 bans
remotely-hosted code, and enforcement of the 2026 Chrome Web Store policy updates began
**1 August 2026**. Your no-build vendored-runtime policy means there is nothing to fix:
zero external fetches, verified by a test. Registration is a one-time $5. Publish
**unlisted** first (installable by link, still reviewed) so pilots can install without a
public listing to defend. `tabCapture`/`offscreen` will draw scrutiny — the disclosure is
easy and true: audio goes to a server on the user's own machine, nothing leaves it.
Firefox: AMO signs unlisted XPIs for self-distribution, so the same folder ships to both.

**Rung 4 — this is the real gap, and it costs money.** OBS users do not run `uv`. The
console is already a zero-build offline webapp, so the wrapper is thin: supervise the
FastAPI process, open `/control`, add a tray icon. Budget honestly: 300 MB–1 GB installers
with the speech model, ~$99/yr Apple signing, and Windows SmartScreen warnings without an
EV certificate. Never bundle the `gpu` extra (that's what makes the venv 7.5 GB) — keep
`install_stt_gpu.sh` as opt-in.

**Rung 5 — see §7.3 before building it.** There is a platform-terms question about pulling
streams server-side that changes the architecture.

### 4.3 Blockers to clear first

1. **No `LICENSE` file** — blocks everything above (§3.1).
2. **No CI** — `.github/` does not exist; 1030 tests run only when you remember.
3. **`TWITCH_CLIENT_ID` has no default**, by design: every operator registers their own
   app. Correct for a self-hosted tool, wrong for a distributed one. A shipped product
   registers one client id (they're public) and deletes a whole step from setup.
4. **First-run model download** needs a progress UI in a packaged app, or pre-bundling.
5. **Privacy policy page** — required for CWS listing and for any hosted tier.

---

## 5. Monetization

### 5.1 The competitive price ceiling is brutal, and it sets the design

| Product | Price | Note |
|---|---|---|
| Nightbot, Fossabot, StreamElements, Cloudbot | **$0** | the incumbent price of "a chat bot" |
| Firebot | **$0**, open source | proves free + OSS is normal in this niche |
| Lumia Stream Premium | **$5.99/mo**, $59.99/yr | full-featured closed tool, single-purpose-ish |
| Alesha AI Pro (AI co-host) | **$12.99/mo** | AI-per-stream pricing anchor |
| StreamChat AI | ~**£5–10/mo** | AI chat, closest comparable |
| Streamlabs Ultra | **$27/mo**, $189/yr | a *bundle* of ~6 products, not a bot |

Two conclusions. First, `business-analysis.md` §5's "$15–25/mo for unlimited" is at the top
of the observed band for a **single-feature** tool and should be revised down at entry.
Second, you cannot sell "bot-ness" at all — Nightbot gives that away. You sell **verified
checks**, which have a real marginal cost, and you should price the thing that costs money.

### 5.2 Meter fact-checks, not hours or seats

Measured COGS: **~$0.005–0.006 per check** (web-search fee dominates; tokens are noise),
~$0.15 per streamed hour at default sensitivity, ~7 s latency. Hours are the wrong unit —
a quiet gaming stream and a debate podcast differ 10× in checks per hour, so hour-pricing
overcharges the cheap user and loses money on the valuable one. Seats are meaningless
(one channel = one seat).

**Checks are the honest unit**: they map 1:1 to COGS, they're legible to a streamer
("300 checks ≈ 8–10 hours of talk stream"), and the console's sensitivity control already
lets the customer trade spend against coverage. You are also *already* recording the funnel
(`heard → gate passed → checked → passed policy → posted`), so metering needs no new
instrumentation.

### 5.3 Proposed tiers

| Tier | Price | Included | Who runs it | Gross margin |
|---|---|---|---|---|
| **Self-host** | $0 (AGPL) | everything, forever, BYO keys | streamer/dev | n/a — this is marketing |
| **Cloud BYO-key** | **$7/mo** / $70/yr | hosted bot + STT + overlay; *your* LLM key | us | ~90% (no API cost) |
| **Cloud Starter** | **$15/mo** / $150/yr | 600 checks/mo, all keys included | us | ~70% |
| **Cloud Pro** | **$29/mo** / $290/yr | 2,000 checks/mo, post-stream reports, clip cards, multi-platform | us | ~65% |
| Overage | $0.02/check | auto-billed or hard-stop, streamer's choice | | 3× COGS |

Ship **Cloud BYO-key first.** It is the only tier that can be launched without solving
metering, without float risk on API spend, and without you becoming the party paying for a
runaway sensitivity setting. It also produces the cleanest possible signal: someone paying
$7/mo purely for *not having to run it* has told you the value is real and the packaging
is the product.

Free-to-paid conversion for open-source-led products realistically lands **1–5% of active
users**; plan the funnel arithmetic on 2%, not on the Cursor-style outliers.

### 5.4 Upsells worth building later, in order

1. **Post-stream fact-check report** (a shareable page per broadcast). Nearly free to build
   — the data is already timestamped and source-cited — and it converts the tool from a
   live gimmick into an artifact the streamer posts to Discord after every stream. This is
   the retention answer to the novelty-half-life risk.
2. **Clip cards** — a rendered verdict card image per posted verdict, watermarked. This is
   a marketing engine disguised as a feature (§6.2).
3. **Multi-platform simulcast** (Kick/YouTube chat) — ingest already handles all three.
4. **B2B artifacts** (newsroom/researcher monitoring, brand-safety signals). Highest
   ceiling, worst fit for a solo launch. Keep the data model ready; don't build it first.

### 5.5 What not to sell

- **Don't sell accuracy guarantees.** Ever. See §7.2.
- **Don't gate safety features** (rate caps, dry run, retraction) behind a paid tier —
  that's the one open-core line that would be indefensible.
- **Don't do a viewer subscription.** `business-analysis.md` settled this: 50 viewers on
  one channel = 50× the same transcription cost. The physics don't change.
- **Don't take sponsorships in chat.** Twitch extension policy forbids ad content and the
  bot's credibility is the whole asset.

---

## 6. Marketing

### 6.1 Positioning: a referee, not a truth ministry

Sell **"give your chat a referee"** — a format element with receipts. Do not sell
"combat misinformation": it is politically coded, it attracts drama-tourism and
adversarial testing, and it repels the neutral talk/react channels that are the largest
serviceable segment. The bot's own copy should be light: it corrects claims, it never
characterises people, and it says so.

Corollary for the tagline: lead with what a viewer *sees* ("❌ FALSE — the Eiffel Tower is
330 m, not 450 · toureiffel.paris"), not with what the pipeline does.

### 6.2 The growth loop is the clip, and it's native to the product

Every posted verdict is a potential clip. That is unusually lucky: the product manufactures
its own marketing inventory. Make it deliberate —

1. Render a **clip card** for each posted verdict (verdict, claim, one source, small
   watermark, channel name).
2. Give the streamer a one-click "post to Discord/X" from the console.
3. Track which verdicts get clipped, and surface it in Analytics ("this check was clipped
   3×") — that number is the retention hook and the renewal argument in one.

Word-of-mouth is how streamer tools actually spread; the loop above is what makes the mouth
have something to show.

### 6.3 Channels, ranked by payoff per unit of effort

| Rank | Channel | Why it works here | Effort |
|---|---|---|---|
| 1 | **3–5 white-glove pilot streamers** (talk/debate/react, 100–2k CCV) | their clips are the ads; their mods are the QA team | high touch, low cost |
| 2 | **Open-source launch** (HN, r/selfhosted, r/opensource) | only possible *because* of §2; recruits devs who install it for streamer friends | one weekend |
| 3 | **Streaming-tool Discords** (OBS, Streamer.bot, Firebot, StreamElements communities) | where technical streamers already are; adjacency, not competition | ongoing, cheap |
| 4 | **r/Twitch, r/streaming, r/Twitch_Startup** | high intent, hostile to ads — post as a builder, not a vendor | ongoing |
| 5 | **YouTube setup tutorial** ("how to add a fact-check bot to your stream") | search-durable, converts for years, doubles as documentation | 1–2 days |
| 6 | **Directory listings** (StreamScheme, alternativeto, awesome-twitch lists) | free, permanent, drives the long tail | hours |
| 7 | **The viewer extension itself** | a viewer sees a verdict → "tell your streamer" — the wedge, working as designed | already built |

Deliberately absent: paid ads (nobody searches for this yet), influencer sponsorships
(too expensive pre-PMF), and TikTok as a *primary* channel (it's an output of rank 1, not
an input).

### 6.4 Trust marketing — the differentiator nobody else will copy

Publish, on a public page, updated per release:

- A **verdict accuracy eval** against a fixed claim set, with the methodology.
- The **retraction rate** across all pilot channels.
- The **policy floors** in plain English, linked to the exact source file.
- An **incident log**: every disputed verdict, what happened, what changed.

This does three jobs at once: it is the honest answer to "why should I let a bot speak for
me", it pre-commits you to the correction format that defuses the wrong-verdict clip risk,
and it is unmatched content for the rank-2 and rank-5 channels above. It is also the
strongest possible argument that the open-source posture is a business decision, not a
concession.

### 6.5 Launch sequence

**Do not launch publicly until the pilot has run.** A cold public launch of a
fact-checking bot invites adversarial testing before the policy has been tuned on real
streams, and the first viral moment will be someone breaking it. Order: pilot (§8) →
fix what the pilot breaks → publish the accuracy page → *then* rank-2 launch.

---

## 7. Legal & platform compliance

*Not legal advice; these are the items to take to someone who gives it.*

### 7.1 EU AI Act Article 50 — live now, cheap to satisfy

Article 50's transparency obligations became **enforceable on 2 August 2026** — sixteen
days before this document. Two limbs plausibly touch this product: people must be informed
when they are interacting with an AI system, and deployers publishing AI-generated **text
that informs the public on matters of public interest** must disclose that it is
artificially generated. A bot posting fact-checks about news and politics into a public
chat is close enough to that description that arguing about it costs more than complying.
Penalties for transparency breaches run to €15M / 3% of turnover — irrelevant at your
scale, but the reputational version is not.

Compliance is nearly free, and most of it is copy:

- Mark every posted message as automated (the format already leads with an emoji marker;
  add an explicit "🤖 automated check" or equivalent to the standard template).
- Disclose in the bot account's Twitch bio/panel and in `!fc about`.
- State it in the overlay's first-run watermark and in the hosted ToS.
- Keep the disclosure in the *first* thing a viewer sees, not in a footer.

Assign this to the message templates in `chat/format.py` and treat it as a launch blocker
for the EU-facing hosted tier.

### 7.2 Defamation: the structure decides who's exposed

The 2025 Georgia decision in *Walters v. OpenAI* granted the developer summary judgment on
a hallucination-based defamation claim, resting substantially on disclaimers, the absence
of actual malice, and demonstrable effort to reduce hallucination. The other half of the
current commentary matters more to you: **a person who takes an AI output and conveys it
onward is generally treated as the publisher of it**, and Section 230's application to
AI-generated output is unsettled.

Consequences for this plan:

- **Self-hosted mode is structurally safer for you**: the streamer runs the software,
  presses go-live, and publishes. You are a tool vendor.
- **Hosted mode moves you much closer to publisher** of every verdict on every channel.
  That is a real reason to sequence hosted *last*, and to write a ToS with a clear
  allocation of responsibility, an indemnity, and a documented takedown/retraction path.
- The existing guards are the defence, so never weaken them: UNVERIFIED never posts, ≥2
  distinct sources, tier gating for politics/health, never fallback-parsed verdicts,
  probation, dry-run default, one-click retraction.
- Keep the "**claims, never people**" rule absolute, and never let the bot check a claim
  about a private individual. That is where defamation exposure concentrates.
- Log everything you already log (claim text, sources, timestamps, decisions) — the audit
  trail is the evidence of non-negligence that *Walters* rewarded.

### 7.3 Twitch platform terms

- **Charging is permitted**, and the market proves it (Moobot, Wizebot, Lumia all charge).
  The Developer Services Agreement's restriction to watch is on acting as a *marketplace*
  for third-party services via the Twitch APIs — don't build a plugin bazaar.
- **No Twitch branding in the product name or logo** (§3.6).
- **Rate limits are a non-issue by construction.** Normal accounts get ~20 messages /
  30 s, known bots ~50, and moderators are exempt from the per-channel restrictions; your
  policy hard-caps at ≤12 posts/hour. Also useful for hosted mode: the 100-channel
  concurrent-join limit **does not count channels where the bot is a moderator** — and the
  bot must be a moderator anyway, as consent proof #2. The consent design removes the
  scaling ceiling as a side effect.
- Apply for **known/verified bot status** before the hosted tier, not after.

### 7.4 The ingest question that changes hosted architecture

Twitch's ToS grants a limited licence for personal or internal business use and does not
permit commercial use or resale of Twitch materials, and third-party stream-downloading
tools are generally treated as violating it. Server-side `streamlink` pulls are *exactly*
that pattern, at scale, for money.

**Design the hosted service so the streamer's own machine pushes audio to you** (the
existing `--source device` path, or a thin desktop agent), rather than you pulling their
HLS. Benefits stack up: it's defensible under the ToS, it's lower-latency, it removes
egress cost, and it works identically for Kick/YouTube. Keep `streamlink` pulls for
self-hosted mode, where the operator is the broadcaster and "personal use" is
uncontroversial.

---

## 8. Sequenced roadmap

| When | Do | Done means |
|---|---|---|
| **Week 0** (hours) | `LICENSE` files + `LICENSING.md` + `CONTRIBUTING.md` with CLA; GitHub Actions running the 1030 tests; name + domain + handles reserved | The repo is legally redistributable |
| **Week 0–1** | Two console-script entry points; `uvx` install documented in README; tagged v0.1 release with zipped extension | A stranger installs it in one command |
| **Week 1** | Article 50 disclosure in message templates + bot bio + overlay; privacy policy page | EU-safe to run publicly |
| **Week 1–2** | Extension → CWS unlisted + AMO signed | Pilots can install without dev mode |
| **Week 2–5** | **Pilot: 3–5 consenting talk/debate streamers**, free, white-glove. Instrument: clips containing bot messages, chat reply rate in the 60 s after a post, approvals vs retractions, unprompted disable events | The three questions from `business-analysis.md` §7 are answered with data |
| **Week 5** | **Decision gate** — continue only if ≥3 pilots keep it enabled unprompted past week 2, verdicts visibly move chat, ≤1 disputed-wrong public verdict per 10 streams | Go / reshape / kill |
| **Week 6–9** | Publish accuracy + retraction page; open-source launch (HN/Reddit); YouTube setup video; desktop bundle | Inbound exists |
| **Week 10+** | Cloud BYO-key at $7/mo, streamer-push ingest, Stripe, ToS with indemnity | First revenue, minimal exposure |
| **Later, gated on demand** | Metered Starter/Pro tiers, post-stream reports, clip cards, multi-platform | Revenue that scales with value |

Total spend to the decision gate: the pilot's API cost is roughly **$11/week across 5
channels**, plus a small VPS, plus $5 for the CWS. Everything expensive (desktop signing,
hosted infra, metering) sits *after* the gate on purpose.

---

## 9. Risks specific to this plan

| Risk | Severity | Mitigation |
|---|---|---|
| A funded competitor ships the feature first | high | Speed + the trust page (§6.4) + AGPL friction; the moat is reputation, not code |
| Hosted mode makes you the publisher | high | Sequence hosted last; ToS + indemnity; never weaken the policy floors (§7.2) |
| Nobody pays because bots are free | high | Don't sell bot-ness; sell checks + hosting. BYO-key tier tests it for $0 of API float |
| Novelty decay | medium | Post-stream reports + clip cards turn a gimmick into a recurring format element |
| AGPL scares a would-be acquirer or partner | medium | Retain sole copyright (§3.2) so relicensing stays available |
| One viral wrong verdict | medium | Probation, dry-run default, retraction path, published incident log |
| Twitch changes API/ToS terms | medium | Multi-platform ingest already exists; keep the transport behind its protocol |
| Solo-founder bandwidth | high | The ladder in §4.1 is ordered so every rung is independently useful if you stop |

---

## 10. Sources

Licensing and open-source business models:
[Goodwin — source-available licensing trends](https://www.goodwinlaw.com/en/insights/publications/2024/09/insights-practices-moving-away-from-open-source-trends-in-licensing) ·
[Functional Source License](https://fsl.software/) ·
[Sentry — introducing the FSL](https://blog.sentry.io/introducing-the-functional-source-license-freedom-without-free-riding/) ·
[FSL explained (TLDRLegal)](https://www.tldrlegal.com/license/functional-source-license-fsl) ·
[n8n Sustainable Use License](https://docs.n8n.io/sustainable-use-license/) ·
[Open-source license guide 2026](https://www.opensourcealternatives.to/blog/open-source-license-guide) ·
[Open-source monetization models](https://earnifyhub.com/blog/open-source-monetization-making-money-from-free-software.php) ·
[Free-to-paid conversion benchmarks](https://www.getmonetizely.com/articles/whats-the-optimal-conversion-rate-from-free-to-paid-in-open-source-saas) ·
[Dev-tool PLG and discovery channels](https://business.daily.dev/resources/product-led-growth-marketing-for-dev-tools-from-free-tier-to-enterprise/)

Market and pricing:
[Best Twitch bots 2026 (StreamScheme)](https://www.streamscheme.com/best-twitch-bots/) ·
[Chatbot comparison (StreamingEquip)](https://streamingequip.com/en/blog/best-streaming-chatbot/) ·
[Lumia Stream pricing](https://lumiastream.com/pricing) ·
[Streamlabs pricing 2026](https://checkthat.ai/brands/streamlabs/pricing) ·
[Firebot](https://firebot.app/) ·
[AI co-host tools and pricing](https://aleshaai.com/features/ai-co-host) ·
[StreamChat AI comparison](https://streamchatai.com/blog/best-twitch-bots-2025-complete-guide)

Distribution:
[Chrome Web Store program policies](https://developer.chrome.com/docs/webstore/program-policies) ·
[CWS 2026 policy updates](https://developer.chrome.com/blog/cws-policy-updates-2026) ·
[Remotely-hosted code rules](https://developer.chrome.com/docs/extensions/develop/migrate/remote-hosted-code) ·
[CWS registration fee](https://developer.chrome.com/docs/webstore/register) ·
[CWS distribution/visibility options](https://developer.chrome.com/docs/webstore/cws-dashboard-distribution) ·
[Firefox self-distribution](https://extensionworkshop.com/documentation/publish/self-distribution/)

Platform and legal:
[Twitch Developer Services Agreement](https://legal.twitch.com/legal/developer-agreement/) ·
[Twitch Extensions guidelines & policies](https://dev.twitch.tv/docs/extensions/guidelines-and-policies/) ·
[Twitch Terms of Service](https://legal.twitch.com/en/legal/terms-of-service/) ·
[Twitch chat & chatbots docs (rate limits)](https://dev.twitch.tv/docs/chat/) ·
[EU AI Act Article 50](https://artificialintelligenceact.eu/article/50/) ·
[Orrick — Article 50 transparency obligations](https://www.orrick.com/en/Insights/2026/08/EU-AI-Act-Transparency-Obligations-for-AI-Generated-Content-Article-50) ·
[EC FAQ — Article 50 transparency](https://digital-strategy.ec.europa.eu/en/faqs/transparency-obligations-under-article-50-ai-act) ·
[Quinn Emanuel — defamation in the AI era](https://www.quinnemanuel.com/the-firm/publications/client-alert-defamation-in-the-ai-era/) ·
[CRS — Section 230 and generative AI](https://www.congress.gov/crs-product/LSB11097)

Repo measurements (this codebase, 2026-08-18): 41 `app.*` imports across 12 `streamer/`
files, 0 reverse imports · 7.5 GB `.venv` with the `gpu` extra · no `LICENSE`, no
`.github/` · unit economics per [`business-analysis.md`](business-analysis.md) §2.
