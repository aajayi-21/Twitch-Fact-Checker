# Live Stream Fact-Checker — Business Viability Analysis

*Written 2026-07-16. Unit-economics figures are measured from this codebase's live testing;
market figures are cited estimates and should be treated as directional.*

## 1. Verdict up front

**The product as it exists today (viewer extension + self-hosted backend) is not a
business — it is a demo.** Install friction filters the audience to people who can run a
Python server, and those people self-host rather than subscribe.

**The plausible business is the chat-bot reshaping: a per-channel service sold to
streamers, positioned as a content/credibility layer, not as truth infrastructure.**
Realistic ceiling: a niche product in the low thousands of paying channels — a healthy
indie/lifestyle business, not a venture-scale one. The fastest way to find out costs
roughly a weekend of engineering and under $50 of API spend (see §7, MVP).

## 2. What building it taught us (measured, not estimated)

| Fact | Value | Why it matters |
|---|---|---|
| Cost per fact-check (web search fee) | ~$0.005 | The dominant marginal cost; token costs are noise |
| LLM tokens per streaming hour (paid `gpt-oss-120b`) | ~$0.02–0.05 | Effectively free at any price point |
| All-in cost per **viewer**-hour (current architecture) | ~$0.12–0.15 | Kills viewer-side SaaS: a 4 h/day viewer ≈ $15–18/mo COGS |
| All-in cost per **channel**-hour (bot architecture) | same ~$0.15 | Amortized over the channel's whole audience — the economics invert |
| Verification latency (claim spoken → verdict) | ~7 s flat | Fast enough to feel live in chat |
| STT compute | local `faster-whisper`, ~0.3–0.6× realtime on CPU | One modest 4-core VPS (~$20–40/mo) plausibly serves 4–8 concurrent channels |
| Accuracy posture | UNVERIFIED-by-default, no-citation ⇒ downgrade | The right defaults exist, but public verdicts raise the stakes (§6) |

The single most important line: **50 viewers watching the same channel with the extension
means paying to transcribe and verify the same audio 50 times.** One bot per channel does
it once. Any monetization plan that doesn't move to per-channel economics fights physics.

## 3. Is there a market?

### Demand signals (for)

- **The claim-dense content category is enormous.** Twitch averages ~2.55M concurrent
  viewers, and Just Chatting — talk, debate, news reaction, politics — is the largest
  category at roughly 13–16% of all watch hours (~310–320k concurrent viewers), more than
  double the #1 game ([Twitch statistics roundup](https://www.notta.ai/en/blog/twitch-statistics),
  [Just Chatting statistics](https://www.amraandelma.com/just-chatting-statistics/),
  [category share](https://blog.99coupons.ai/twitch-stats)). This is exactly where
  checkable claims concentrate.
- **Streamers pay for tools that make content.** TTS, alerts, minigames, AI gimmicks —
  the willingness-to-pay is proven when a tool produces *moments* (clips, chat
  engagement). A bot dropping "❌ FALSE — the Eiffel Tower is 330 m, not 450
  (toureiffel.paris)" mid-rant is a moment.
- **Cultural tailwind.** Community-Notes-style public correction is a mainstream format
  now; "chat will fact-check you" is already a live meme. This product automates
  something audiences visibly enjoy doing manually.
- **No direct competitor.** Nothing does live, in-chat, source-cited fact-checking today.
  Adjacent tools are either offline (article checkers) or generic (chat bots).

### Headwinds (against)

- **Fact-checking monetizes poorly, historically.** Snopes/PolitiFact live on
  donations/ads/grants; NewsGuard's consumer subscription failed and it pivoted B2B.
  Consumers claim to value truth and rarely pay for it. **Do not build the "truth
  utility" pitch; build the "show element with receipts" pitch.**
- **The incumbent price for chat bots is $0.** Nightbot and StreamElements are free;
  Streamlabs monetizes via a ~$19/mo premium tier and fees
  ([bot comparison](https://www.streamscheme.com/best-twitch-bots/),
  [StreamElements](https://streamelements.com/),
  [Streamlabs vs StreamElements](https://earnifyhub.com/blog/streamlabs-vs-streamelements)).
  A paid bot must justify itself on *content value*, not bot-ness. The $19/mo Prime tier
  proves streamers will pay that much when the value is legible.
- **Novelty half-life.** Stream gimmicks decay. Retention will depend on the bot becoming
  part of a channel's format (recurring segments, "bot said it" callbacks), not on the
  first-week wow.
- **Public wrongness is expensive.** One confidently wrong FALSE against the streamer in
  their own chat is a clip *against the product* (§6).

### Market size (napkin, flagged as such)

Of Twitch's ~90–105k average concurrent channels, the serviceable niche — talk/debate/
politics/news-reaction channels large enough to care about production value (say 100+
concurrent viewers) — is plausibly a few thousand channels at any time, more across
YouTube Live/Kick (both already supported by this codebase). Capture 300–1,000 paying
channels at $15–25/mo → **$5k–25k MRR**. That is the honest ceiling estimate for the core
product: a real business for one person, not a rocket. Upside beyond it lives in B2B
(§4c) and platform expansion.

## 4. Who is the customer, and what exactly is the service?

Pick one primary customer. The analysis says: **the streamer.**

### 4a. Primary: the streamer (or their mod team) — "the fact-check bot"

- **Service:** an opt-in Twitch/Kick/YouTube chat bot that watches the broadcaster's own
  stream and posts conservative, source-cited verdicts in chat. Configurable topics
  (already built), sensitivity (already built), posting style (FALSE/MISLEADING only by
  default), cooldowns, and a per-stream cap.
- **Why the streamer and not the viewer:** (1) consent is structurally required anyway —
  an uninvited fact-check bot is a ban plus a Twitch-policy problem, so the person who
  must say yes is the natural buyer; (2) one sale covers the entire audience — per-channel
  economics; (3) the value proposition is strongest for them: differentiation
  ("fact-checked stream" as format), credibility signaling in debate content, and
  clippable moments that market the channel.
- **Positioning:** *entertainment and format first, accuracy second.* "Give your chat a
  referee" sells; "combat misinformation" doesn't.

### 4b. Secondary (free): the viewer extension — the wedge

Keep the existing extension free and BYO-key forever. It needs nobody's permission, works
on any channel, demos the pipeline, and is the top of the funnel ("get this in YOUR chat
→ tell your streamer"). It will never be revenue; it doesn't need to be.

### 4c. Tertiary (later, likely the real money if it works): B2B artifacts

The pipeline produces a byproduct with institutional value: a timestamped, source-cited
record of claims made on live streams. Products: post-stream fact-check reports
(shareable content for the streamer, but also) misinformation monitoring for newsrooms
and researchers, brand-safety signals for sponsors. This is where fact-checking
economics have historically actually worked (NewsGuard's pivot). Do not build it first;
keep the data model ready for it (verdicts are already structured + timestamped).

## 5. Pricing hypothesis (to test, not to ship blind)

| Tier | Price | Contents |
|---|---|---|
| Free | $0 | Viewer extension (BYO key); bot trial: N checks/stream or 2 streams/mo |
| Channel | ~$15–25/mo | Unlimited bot streams on one channel, topic/sensitivity config, session history page |
| Pro / later | ~$50+/mo | Post-stream reports, clip-ready verdict cards, API access, multi-platform simulcast |

COGS per active channel ≈ $0.15/streamed-hour + amortized VPS ⇒ a 60 h/mo channel costs
~$10–12 to serve. Margin exists at $19 but is thin for heavy streamers — a soft cap or
usage-based component above ~80 h/mo protects the downside.

## 6. Risks and honest counterarguments

1. **A wrong public verdict is a product-killing clip.** Mitigations already in the
   architecture: UNVERIFIED-by-default, no-citations ⇒ downgrade, sensitivity threshold.
   Bot-specific additions needed: post only FALSE/MISLEADING at high confidence, never
   post UNVERIFIED, easy `!fc mute` for mods, and an appeals/correction message format.
2. **Weaponization in drama.** Restrict to broadcaster-authorized channels only; the bot
   speaks about *claims*, never about *people*; rate-cap posts.
3. **Platform policy.** Chat bots are normal on Twitch, but verdict posting must respect
   rate limits and channel link permissions; the bot account should be registered/known.
   Kick's ToS anti-automation wording is the broadest of the platforms — get explicit
   broadcaster consent everywhere and this is the same category as Nightbot.
4. **Model dependency.** Verdict quality rides on one model + one search plugin. The
   provider seam (OpenRouter/Gemini, model via `.env`) already hedges this.
5. **The chilling scenario: nobody cares.** Possible! Fact-check fatigue is real. This is
   exactly what the MVP exists to measure cheaply, with predefined kill criteria (§7).

## 7. What the MVP entails

**Goal: answer three questions in ~4 weeks for <$100:** (1) will streamers turn it on,
(2) does chat engage with verdicts, (3) is public accuracy good enough?

### Scope (builds on what exists — the backend needs no changes)

The backend already speaks "PCM in → verdict frames out" over a WebSocket and doesn't
know a browser exists. The MVP adds two small clients:

1. **Headless ingest** (~150 lines): `streamlink <channel> audio` → ffmpeg → 16 kHz PCM
   into the existing `/ws/audio` endpoint. (`scripts/stream_wav.py` is already 80% of
   this client.)
2. **Chat bot sink** (~200 lines, `twitchio`): consumes verdict frames → formats chat
   messages (`❌ FALSE — <claim>. <one-line explanation> (source.domain)`); posts only
   FALSE/MISLEADING; per-stream cap (e.g. 6 posts/hour); `!fc on|off|topics|mute`
   commands for broadcaster/mods.
3. **Per-channel config**: a JSON file per channel reusing the existing topic +
   sensitivity settings. No dashboard, no database.
4. **Measurement**: log every posted verdict + chat messages in the 60 s after it
   (reply/emote rate), count clips containing bot messages, weekly streamer check-in.

### Explicit non-goals for the MVP

No payments, no multi-tenant SaaS, no web dashboard, no auto-scaling (one VPS, ≤5
channels), no YouTube/Kick chat (Twitch chat only — the *ingest* already works for all
platforms via streamlink), no UNVERIFIED/TRUE posting.

### Pilot plan

Recruit 3–5 consenting mid-size (100–2,000 CCV) talk/debate/react streamers — free,
white-glove, in exchange for feedback and permission to measure. Run 2–4 weeks.

### Success / kill criteria (decide before launch)

- **Continue** if: ≥3 pilots keep it enabled unprompted after week 2; verdicts visibly
  drive chat activity; ≤1 disputed-wrong public verdict per 10 streams.
- **Kill or reshape** if: streamers quietly disable it (the strongest possible signal),
  chat ignores it, or accuracy disputes dominate the feedback.

### Cost of the experiment

Engineering: roughly a weekend for the two clients + a week of polish/ops. Running: ~5
channels × ~15 streamed hours/week × $0.15/h ≈ **$11/week API spend** + one small VPS.

## 8. Bottom line

There is no consumer business in selling truth to viewers, and history says so loudly.
There is a plausible niche business in selling *a fact-check character* to talk-genre
streamers at StreamElements-premium price points, with per-channel economics that work
(~$10 COGS against ~$20 revenue) and a B2B data byproduct as the long-term upside. The
architecture built so far happens to be exactly the right substrate for testing this: the
bot is just a second consumer of the same verdict stream. The MVP is cheap enough that
the correct move is to run it rather than debate it.

*Sources: [Twitch statistics](https://www.notta.ai/en/blog/twitch-statistics) ·
[Just Chatting statistics](https://www.amraandelma.com/just-chatting-statistics/) ·
[Category share Q1 2026](https://blog.99coupons.ai/twitch-stats) ·
[Twitch bot landscape](https://www.streamscheme.com/best-twitch-bots/) ·
[StreamElements](https://streamelements.com/) ·
[Streamlabs vs StreamElements pricing](https://earnifyhub.com/blog/streamlabs-vs-streamelements) ·
unit economics measured in this repo (see README "Costs & limits").*
