/**
 * Unit tests for the viewer's ledger: record shapes, channel identity,
 * pricing, the session recorder's state machine, the numbers behind the
 * History and Usage screens, and exports.
 *
 * Run from the repository root:
 *
 *     node --test "extension/tests/*.test.mjs"
 *
 * The IndexedDB layer itself (ledger/ledger.mjs) needs a browser and is
 * exercised by scripts/run_app_e2e.mjs.
 */

import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {test} from "node:test";
import vm from "node:vm";

import {importFromBackend} from "../ledger/backend_import.mjs";
import {csvCell, exportFileName, ledgerToJson, verdictsToCsv} from "../ledger/export.mjs";
import {COST_SOURCE, PRICING_VERSION, findPrice, priceCall} from "../ledger/pricing.mjs";
import {
  MIN_ADJUDICATED_SAMPLE,
  PERIODS,
  adjudicatedShares,
  channelOptions,
  countLabels,
  dailySpend,
  filterVerdicts,
  periodRange,
  sessionDurationSeconds,
  sortRows,
  summarizeChannels,
  summarizeSessions,
  summarizeUsage,
} from "../ledger/queries.mjs";
import {
  buildClaimRecord,
  buildFeedbackRecord,
  buildSessionRecord,
  buildUsageRecord,
  buildVerdictRecord,
  channelKey,
  isoSeconds,
  normalizeChannelId,
  normalizeIdentity,
  recordsFromBackendSession,
  sourceDomain,
} from "../ledger/records.mjs";
import {createSessionRecorder} from "../ledger/session_recorder.mjs";
import {
  channelFromOwnerHref,
  channelFromPath,
  cleanStreamTitle,
  cleanStreamUrl,
  identityFromPage,
  platformFromUrl,
} from "../ledger/stream_identity.mjs";

const approx = (actual, expected, tolerance = 1e-9) =>
  assert.ok(Math.abs(actual - expected) <= tolerance, `${actual} is not ≈ ${expected}`);

/* ------------------------------------------------------------------ */
/* Channel identity                                                     */
/* ------------------------------------------------------------------ */

test("platformFromUrl recognises the four sites and nothing else", () => {
  assert.equal(platformFromUrl("https://www.twitch.tv/somechannel"), "twitch");
  assert.equal(platformFromUrl("https://www.youtube.com/watch?v=abc"), "youtube");
  assert.equal(platformFromUrl("https://m.youtube.com/watch?v=abc"), "youtube");
  assert.equal(platformFromUrl("https://kick.com/somechannel"), "kick");
  assert.equal(platformFromUrl("https://rumble.com/v1abc-title.html"), "rumble");
  assert.equal(platformFromUrl("https://nottwitch.tv/x"), null);
  assert.equal(platformFromUrl("https://example.com/twitch.tv"), null);
  assert.equal(platformFromUrl("not a url"), null);
});

test("Twitch: the channel is the first path segment, lower-cased", () => {
  assert.equal(channelFromPath("twitch", "/SomeChannel"), "somechannel");
  assert.equal(channelFromPath("twitch", "/somechannel/videos"), "somechannel");
  assert.equal(channelFromPath("twitch", "/somechannel/clip/FunnyClip"), "somechannel");
});

test("Twitch: site sections are not channels", () => {
  // Before this, a VOD at /videos/123 was recorded as the channel "videos".
  assert.equal(channelFromPath("twitch", "/videos/2012345678"), null);
  assert.equal(channelFromPath("twitch", "/directory/category/just-chatting"), null);
  assert.equal(channelFromPath("twitch", "/"), null);
  assert.equal(channelFromPath("twitch", "/settings/profile"), null);
});

test("Twitch: popout, moderator and embed views name the channel second", () => {
  assert.equal(channelFromPath("twitch", "/popout/SomeChannel/chat"), "somechannel");
  assert.equal(channelFromPath("twitch", "/moderator/somechannel"), "somechannel");
  assert.equal(channelFromPath("twitch", "/popout"), null);
});

test("Kick: first segment, except site sections", () => {
  assert.equal(channelFromPath("kick", "/SomeStreamer"), "somestreamer");
  assert.equal(channelFromPath("kick", "/somestreamer/videos/abc"), "somestreamer");
  assert.equal(channelFromPath("kick", "/browse"), null);
  assert.equal(channelFromPath("kick", "/video/0b1c"), null);
});

test("YouTube: handles and channel ids in the address", () => {
  assert.equal(channelFromPath("youtube", "/@SomeCreator/live"), "@somecreator");
  assert.equal(channelFromPath("youtube", "/@somecreator"), "@somecreator");
  const id = "UCabcdefghijklmnopqrstuv";
  // Channel ids are case-sensitive and must survive untouched.
  assert.equal(channelFromPath("youtube", `/channel/${id}/live`), id);
  assert.equal(channelFromPath("youtube", "/channel/not-an-id"), null);
  assert.equal(channelFromPath("youtube", "/c/LegacyName"), "c/legacyname");
});

test("YouTube: a watch address names a video, not a channel", () => {
  assert.equal(channelFromPath("youtube", "/watch"), null);
  assert.equal(channelFromPath("youtube", "/live/abc123"), null);
  assert.equal(channelFromPath("youtube", "/shorts/abc123"), null);
});

test("Rumble: channel and user pages", () => {
  assert.equal(channelFromPath("rumble", "/c/SomeShow"), "someshow");
  assert.equal(channelFromPath("rumble", "/user/SomePerson"), "user/someperson");
  assert.equal(channelFromPath("rumble", "/v5abc-some-title.html"), null);
});

test("owner links resolve relative to the platform and reject other sites", () => {
  assert.equal(channelFromOwnerHref("youtube", "/@SomeCreator"), "@somecreator");
  assert.equal(
    channelFromOwnerHref("youtube", "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv"),
    "UCabcdefghijklmnopqrstuv"
  );
  assert.equal(channelFromOwnerHref("rumble", "/c/SomeShow"), "someshow");
  // A creator's off-site link under the player is not their channel.
  assert.equal(channelFromOwnerHref("youtube", "https://example.com/@SomeCreator"), null);
  assert.equal(channelFromOwnerHref("youtube", null), null);
  assert.equal(channelFromOwnerHref(null, "/@x"), null);
});

test("cleanStreamUrl keeps the video and drops per-visit noise", () => {
  assert.equal(
    cleanStreamUrl("https://www.youtube.com/watch?v=abc123&t=42s&list=PLx&pp=ygU"),
    "https://www.youtube.com/watch?v=abc123"
  );
  assert.equal(
    cleanStreamUrl("https://www.twitch.tv/somechannel?referrer=raid#chat"),
    "https://www.twitch.tv/somechannel"
  );
  assert.equal(cleanStreamUrl("chrome://extensions"), null);
  assert.equal(cleanStreamUrl(""), null);
});

test("cleanStreamTitle strips the site suffix and YouTube's unread counter", () => {
  assert.equal(cleanStreamTitle("(3) Debate night LIVE - YouTube"), "Debate night LIVE");
  assert.equal(cleanStreamTitle("somechannel - Twitch"), "somechannel");
  assert.equal(cleanStreamTitle("xQc Live Stream - Watch Live on KICK"), "xQc Live Stream");
  assert.equal(cleanStreamTitle("  "), null);
  assert.equal(cleanStreamTitle(undefined), null);
});

test("identityFromPage: a YouTube watch page gets its channel from the owner link", () => {
  assert.deepEqual(
    identityFromPage({
      url: "https://www.youtube.com/watch?v=abc123&t=10s",
      title: "(1) Budget speech reaction - YouTube",
      ownerHref: "/@SomeCreator",
      ownerName: "  Some\n Creator ",
    }),
    {
      platform: "youtube",
      channelId: "@somecreator",
      channelName: "Some Creator",
      streamUrl: "https://www.youtube.com/watch?v=abc123",
      streamTitle: "Budget speech reaction",
    }
  );
});

test("identityFromPage: the address wins over the owner link when it names a channel", () => {
  const identity = identityFromPage({
    url: "https://www.twitch.tv/somechannel",
    title: "somechannel - Twitch",
    ownerHref: "/someoneelse",
  });
  assert.equal(identity.platform, "twitch");
  assert.equal(identity.channelId, "somechannel");
});

test("identityFromPage: no channel means no channel name either", () => {
  const identity = identityFromPage({
    url: "https://www.youtube.com/watch?v=abc123",
    title: "x - YouTube",
    ownerName: "Left over from the previous video",
  });
  assert.equal(identity.channelId, null);
  assert.equal(identity.channelName, null);
});

test("identityFromPage: an unsupported page is all nulls", () => {
  assert.deepEqual(identityFromPage({url: "https://example.com/", title: ""}), {
    platform: null,
    channelId: null,
    channelName: null,
    streamUrl: "https://example.com/",
    streamTitle: null,
  });
  assert.equal(identityFromPage().platform, null);
});

/** The content script's owner reader, run against a minimal fake page. */
const loadOwnerReader = (hostname, elementsBySelector) => {
  const makeElement = (description) =>
    description && {
      getAttribute: (name) => description[name] ?? null,
      textContent: description.text ?? "",
      querySelector: (selector) => makeElement(description.children?.[selector]),
    };
  const sandbox = {
    location: {hostname},
    document: {querySelector: (selector) => makeElement(elementsBySelector[selector])},
  };
  sandbox.self = sandbox;
  vm.runInNewContext(
    readFileSync(new URL("../content/page_identity.js", import.meta.url), "utf8"),
    sandbox
  );
  return sandbox.readStreamOwner;
};

test("content script: reads the YouTube owner link and its name", () => {
  const read = loadOwnerReader("www.youtube.com", {
    "ytd-watch-metadata ytd-video-owner-renderer ytd-channel-name a[href]": {
      href: "/@SomeCreator",
      text: "\n  Some Creator\n",
    },
  });
  assert.deepEqual({...read()}, {ownerHref: "/@SomeCreator", ownerName: "Some Creator"});
});

test("content script: falls back through the selector list", () => {
  const read = loadOwnerReader("www.youtube.com", {
    "#owner a[href^='/channel/']": {href: "/channel/UCabcdefghijklmnopqrstuv", text: "Name"},
  });
  assert.equal(read().ownerHref, "/channel/UCabcdefghijklmnopqrstuv");
});

test("content script: Rumble's name sits in a child element", () => {
  const read = loadOwnerReader("rumble.com", {
    "a.media-by--a[href]": {
      href: "/c/SomeShow",
      text: "Some Show 1.2M followers",
      children: {".media-heading-name": {text: "Some Show"}},
    },
  });
  assert.deepEqual({...read()}, {ownerHref: "/c/SomeShow", ownerName: "Some Show"});
});

test("content script: no owner link (Twitch, or not rendered yet) is nulls", () => {
  assert.deepEqual({...loadOwnerReader("www.twitch.tv", {})()}, {ownerHref: null, ownerName: null});
  assert.deepEqual({...loadOwnerReader("www.youtube.com", {})()}, {ownerHref: null, ownerName: null});
});

/* ------------------------------------------------------------------ */
/* Records                                                              */
/* ------------------------------------------------------------------ */

const SESSION = Object.freeze({
  ...buildSessionRecord({
    id: "s1",
    backendSessionId: "b1",
    identity: normalizeIdentity({
      platform: "youtube",
      channelId: "@SomeCreator",
      channelName: "Some Creator",
      streamUrl: "https://www.youtube.com/watch?v=abc",
      streamTitle: "Budget speech",
    }),
    startedAt: "2026-11-12T20:00:00Z",
  }),
});

const VERDICT_FRAME = Object.freeze({
  type: "verdict",
  id: "v1",
  claim: " The Eiffel Tower is 450 meters tall. ",
  topic: "history",
  label: "FALSE",
  explanation: "It is about 330 meters tall.",
  sources: [
    {url: "https://www.toureiffel.paris/en/the-monument/key-figures", title: "Key figures"},
    {url: "not a url", title: null},
    {title: "no url at all"},
  ],
  checked_at: "2026-11-12T20:01:11Z",
  used_fallback: false,
  claim_id: "c1",
  latency_ms: 2300,
  provider: "anthropic",
  model: "claude-haiku-5-5",
});

test("isoSeconds matches the backend's timestamp format", () => {
  assert.equal(isoSeconds(Date.UTC(2026, 10, 12, 20, 1, 11, 987)), "2026-11-12T20:01:11Z");
});

test("normalizeChannelId folds case, except YouTube channel ids", () => {
  assert.equal(normalizeChannelId("twitch", " SomeChannel "), "somechannel");
  assert.equal(normalizeChannelId("youtube", "@SomeCreator"), "@somecreator");
  assert.equal(
    normalizeChannelId("youtube", "UCabcdefghijklmnopqrstuv"),
    "UCabcdefghijklmnopqrstuv"
  );
  assert.equal(normalizeChannelId("kick", ""), null);
  assert.equal(normalizeChannelId("kick", null), null);
});

test("normalizeIdentity: the name falls back to the id, and unknown platforms are dropped", () => {
  assert.deepEqual(normalizeIdentity({platform: "twitch", channelId: "SomeChannel"}), {
    platform: "twitch",
    channel_id: "somechannel",
    channel_name: "somechannel",
    stream_url: null,
    stream_title: null,
  });
  assert.equal(normalizeIdentity({platform: "dailymotion", channelId: "x"}).platform, null);
  assert.equal(normalizeIdentity().channel_id, null);
});

test("channelKey separates platforms: the same name on two sites is two channels", () => {
  assert.notEqual(channelKey("twitch", "somechannel"), channelKey("kick", "somechannel"));
  assert.equal(channelKey("twitch", null), null);
});

test("sourceDomain", () => {
  assert.equal(sourceDomain("https://www.bbc.co.uk/news/1"), "bbc.co.uk");
  assert.equal(sourceDomain("nope"), null);
});

test("buildVerdictRecord carries the session's channel and stream", () => {
  const verdict = buildVerdictRecord(VERDICT_FRAME, SESSION);
  assert.equal(verdict.session_id, "s1");
  assert.equal(verdict.claim_text, "The Eiffel Tower is 450 meters tall.");
  assert.equal(verdict.platform, "youtube");
  assert.equal(verdict.channel_id, "@somecreator");
  assert.equal(verdict.channel_name, "Some Creator");
  assert.equal(verdict.stream_url, "https://www.youtube.com/watch?v=abc");
  assert.equal(verdict.latency_ms, 2300);
  assert.equal(verdict.model, "claude-haiku-5-5");
  // Sources without a URL are dropped; each kept one gets its domain.
  assert.deepEqual(verdict.sources, [
    {
      url: "https://www.toureiffel.paris/en/the-monument/key-figures",
      title: "Key figures",
      domain: "toureiffel.paris",
    },
    {url: "not a url", title: null, domain: null},
  ]);
});

test("buildVerdictRecord tolerates an older backend's frame", () => {
  const {claim_id: _c, latency_ms: _l, provider: _p, model: _m, ...oldFrame} = VERDICT_FRAME;
  const verdict = buildVerdictRecord(oldFrame, SESSION);
  assert.equal(verdict.claim_id, null);
  assert.equal(verdict.latency_ms, null);
  assert.equal(verdict.provider, null);
});

test("buildVerdictRecord rejects frames that are not usable verdicts", () => {
  assert.equal(buildVerdictRecord({...VERDICT_FRAME, label: "MAYBE"}, SESSION), null);
  assert.equal(buildVerdictRecord({...VERDICT_FRAME, id: ""}, SESSION), null);
  assert.equal(buildVerdictRecord({...VERDICT_FRAME, claim: "  "}, SESSION), null);
  assert.equal(buildVerdictRecord(null, SESSION), null);
});

test("buildClaimRecord", () => {
  const claim = buildClaimRecord(
    {id: "c1", text: "A claim.", topic: "politics", check_worthiness: 0.2, outcome: "below_threshold"},
    SESSION,
    "2026-11-12T20:00:30Z"
  );
  assert.equal(claim.outcome, "below_threshold");
  assert.equal(claim.gated_at, "2026-11-12T20:00:30Z");
  assert.equal(claim.channel_id, "@somecreator");
  assert.equal(buildClaimRecord({id: "c1", text: "x", outcome: "exploded"}, SESSION, "t"), null);
});

test("buildFeedbackRecord validates the rating and the corrected label", () => {
  assert.deepEqual(
    buildFeedbackRecord(
      {verdictId: "v1", rating: "down", correctedLabel: "TRUE", note: " wrong year "},
      "2026-11-12T20:05:00Z"
    ),
    {
      verdict_id: "v1",
      rating: "down",
      corrected_label: "TRUE",
      note: "wrong year",
      created_at: "2026-11-12T20:05:00Z",
    }
  );
  assert.equal(buildFeedbackRecord({verdictId: "v1", rating: "meh"}, "t"), null);
  assert.equal(
    buildFeedbackRecord({verdictId: "v1", rating: "up", correctedLabel: "SORT OF"}, "t")
      .corrected_label,
    null
  );
});

/* ------------------------------------------------------------------ */
/* Pricing                                                              */
/* ------------------------------------------------------------------ */

test("a provider-reported cost is used exactly", () => {
  assert.deepEqual(
    priceCall({provider: "openrouter", model: "anything/at-all", reportedCostUsd: 0.0078}),
    {costUsd: 0.0078, costSource: COST_SOURCE.PROVIDER, pricingVersion: null}
  );
});

test("an Anthropic call is estimated from tokens and searches", () => {
  const priced = priceCall({
    provider: "anthropic",
    model: "claude-haiku-5-5",
    inputTokens: 8000,
    cachedInputTokens: 3000,
    outputTokens: 200,
    webSearches: 1,
  });
  // 8000 × $0.10/M + 3000 × $0.01/M + 200 × $0.50/M + one $0.01 search.
  approx(priced.costUsd, 0.0008 + 0.00003 + 0.0001 + 0.01);
  assert.equal(priced.costSource, COST_SOURCE.ESTIMATE);
  assert.equal(priced.pricingVersion, PRICING_VERSION);
});

test("dated and family model ids find their price; Opus 5 is not Opus 5.5", () => {
  assert.equal(findPrice("anthropic", "claude-haiku-5-5-20261001").input, 0.1);
  assert.equal(findPrice("anthropic", "claude-opus-5-5").input, 4);
  assert.equal(findPrice("anthropic", "claude-opus-5").input, 5);
  assert.equal(findPrice("anthropic", "claude-unknown-9"), null);
  assert.equal(findPrice("anthropic", null), null);
});

test("a local model is free; an unpriced one is unknown, never zero", () => {
  assert.deepEqual(priceCall({provider: "ollama", model: "llama3", inputTokens: 5000}), {
    costUsd: 0,
    costSource: COST_SOURCE.FREE,
    pricingVersion: null,
  });
  const unknown = priceCall({provider: "openrouter", model: "some/model", inputTokens: 5000});
  assert.equal(unknown.costUsd, null);
  assert.equal(unknown.costSource, COST_SOURCE.UNKNOWN);
});

test("buildUsageRecord prices the call and keeps who it was for", () => {
  const usage = buildUsageRecord(
    {
      type: "usage",
      stage: "verify",
      provider: "anthropic",
      model: "claude-haiku-5-5",
      input_tokens: 8000,
      cached_input_tokens: 0,
      output_tokens: 200,
      web_searches: 1,
      verdict_id: "v1",
    },
    SESSION,
    "2026-11-12T20:01:12Z"
  );
  assert.equal(usage.session_id, "s1");
  assert.equal(usage.calls, 1);
  assert.equal(usage.cost_source, COST_SOURCE.ESTIMATE);
  approx(usage.cost_usd, 0.0008 + 0.0001 + 0.01);
  assert.equal(usage.verdict_id, "v1");
  assert.equal(usage.platform, "youtube");
  assert.equal(usage.channel_id, "@somecreator");
});

test("buildUsageRecord keeps an exact cost and ignores junk", () => {
  const exact = buildUsageRecord(
    {stage: "gate", provider: "openrouter", model: "x/y", input_tokens: 10, cost_usd: 0.0004},
    SESSION,
    "t"
  );
  assert.equal(exact.cost_usd, 0.0004);
  assert.equal(exact.cost_source, COST_SOURCE.PROVIDER);
  assert.equal(buildUsageRecord({stage: "gate"}, SESSION, "t"), null);
  assert.equal(buildUsageRecord({stage: "vibes", provider: "x"}, SESSION, "t"), null);
  // Negative or non-numeric counts never reach a total.
  const junk = buildUsageRecord(
    {stage: "gate", provider: "ollama", model: "m", input_tokens: -5, output_tokens: "many"},
    SESSION,
    "t"
  );
  assert.equal(junk.input_tokens, 0);
  assert.equal(junk.output_tokens, 0);
});

/* ------------------------------------------------------------------ */
/* Import from the companion backend                                    */
/* ------------------------------------------------------------------ */

const BACKEND_DETAIL = Object.freeze({
  session: {
    id: "b-1",
    platform: "twitch",
    channel: "SomeChannel",
    title: "Reacting to news!",
    started_at: "2026-10-01T18:00:00Z",
    ended_at: "2026-10-01T19:30:00Z",
    speech_seconds: 4200.5,
    audio_seconds: 5400,
    gate_calls: 90,
    verify_calls: 3,
    est_cost_usd: 0.033,
    stt_drop_counts: "{}",
  },
  claims: [
    {id: "c-1", session_id: "b-1", text: "Claim one.", topic: "politics", check_worthiness: 0.9,
      gated_at: "2026-10-01T18:05:00Z", outcome: "verified"},
    {id: "c-2", session_id: "b-1", text: "Claim two.", topic: "money", check_worthiness: 0.3,
      gated_at: "2026-10-01T18:06:00Z", outcome: "below_threshold"},
  ],
  verdicts: [
    {
      id: "v-1", claim_id: "c-1", session_id: "b-1", label: "MISLEADING",
      explanation: "Partly right.", checked_at: "2026-10-01T18:05:04Z", used_fallback: 0,
      latency_ms: 3900, provider: "anthropic", model: "claude-haiku-5-5",
      sources: [{rank: 0, url: "https://example.org/a", domain: "example.org", title: "A"}],
      feedback: {rating: "down", corrected_label: "TRUE", note: null, created_at: "2026-10-01T18:06:00Z"},
    },
    // A verdict whose claim row is missing cannot be shown and is skipped.
    {id: "v-2", claim_id: "c-gone", session_id: "b-1", label: "TRUE", explanation: "x",
      checked_at: "2026-10-01T18:07:00Z", used_fallback: 0, sources: [], feedback: null},
  ],
});

test("import: a backend session becomes ledger records", () => {
  const records = recordsFromBackendSession(BACKEND_DETAIL);
  assert.equal(records.session.id, "b-1");
  // Its own id doubles as the backend id, which is what makes re-import a no-op.
  assert.equal(records.session.backend_session_id, "b-1");
  assert.equal(records.session.channel_id, "somechannel");
  assert.equal(records.session.imported, true);
  assert.equal(records.session.ended_at, "2026-10-01T19:30:00Z");
  assert.equal(sessionDurationSeconds(records.session), 5400);

  assert.equal(records.claims.length, 2);
  assert.equal(records.claims[1].outcome, "below_threshold");
  assert.equal(records.claims[0].gated_at, "2026-10-01T18:05:00Z");

  assert.equal(records.verdicts.length, 1);
  const [verdict] = records.verdicts;
  assert.equal(verdict.claim_text, "Claim one.");
  assert.equal(verdict.topic, "politics");
  assert.equal(verdict.channel_id, "somechannel");
  assert.equal(verdict.used_fallback, false);
  assert.equal(verdict.sources[0].domain, "example.org");

  assert.deepEqual(records.feedback, [
    {verdict_id: "v-1", rating: "down", corrected_label: "TRUE", note: null,
      created_at: "2026-10-01T18:06:00Z"},
  ]);
});

test("import: the session's flat-rate estimate becomes one usage row", () => {
  const [usage, ...rest] = recordsFromBackendSession(BACKEND_DETAIL).usage;
  assert.equal(rest.length, 0);
  assert.equal(usage.calls, 3);
  assert.equal(usage.cost_usd, 0.033);
  assert.equal(usage.cost_source, COST_SOURCE.ESTIMATE);
  assert.equal(usage.imported, true);
  assert.equal(usage.provider, "anthropic");
  assert.equal(usage.ts, "2026-10-01T19:30:00Z");
});

test("import: a session with no checks adds no usage; junk is rejected", () => {
  const quiet = recordsFromBackendSession({
    session: {id: "b-2", started_at: "2026-10-02T10:00:00Z", verify_calls: 0, est_cost_usd: 0},
    claims: [],
    verdicts: [],
  });
  assert.deepEqual(quiet.usage, []);
  assert.equal(quiet.session.channel_id, null);
  assert.equal(recordsFromBackendSession({session: {id: "b-3"}}), null);
  assert.equal(recordsFromBackendSession(null), null);
});

/* ------------------------------------------------------------------ */
/* Session recorder                                                     */
/* ------------------------------------------------------------------ */

/** An in-memory ledger with the same surface the recorder uses. */
const makeFakeLedger = () => {
  const data = {sessions: new Map(), verdicts: [], claims: new Map(), usage: [], feedback: [],
    retentionCutoffs: []};
  return {
    data,
    putSession: async (session) => void data.sessions.set(session.id, {...session}),
    updateSession: async (id, patch) => {
      const next = {...data.sessions.get(id), ...patch};
      data.sessions.set(id, next);
      return next;
    },
    recordVerdict: async (verdict) => void data.verdicts.push(verdict),
    recordClaim: async (claim) => void data.claims.set(claim.id, claim),
    recordUsage: async (usage) => void data.usage.push(usage),
    saveFeedback: async (feedback) => void data.feedback.push(feedback),
    fillInSessionChannel: async (id, channel) => {
      const fields = {platform: channel.platform, channel_id: channel.channel_id,
        channel_name: channel.channel_name};
      data.sessions.set(id, {...data.sessions.get(id), ...fields});
      for (const rows of [data.verdicts, data.usage, [...data.claims.values()]]) {
        rows.filter((row) => row.session_id === id && !row.channel_id)
          .forEach((row) => Object.assign(row, fields));
      }
    },
    deleteOlderThan: async (cutoff) => {
      data.retentionCutoffs.push(cutoff);
      return 2;
    },
  };
};

const makeRecorder = () => {
  const ledger = makeFakeLedger();
  let stored = null;
  let clock = Date.UTC(2026, 10, 12, 20, 0, 0);
  let nextId = 0;
  const recorder = createSessionRecorder({
    ledger,
    memory: {
      get: async () => (stored ? structuredClone(stored) : null),
      set: async (state) => {
        stored = state ? structuredClone(state) : null;
      },
    },
    now: () => clock,
    newId: () => `s${(nextId += 1)}`,
  });
  return {
    recorder,
    ledger,
    advance: (seconds) => {
      clock += seconds * 1000;
    },
    stored: () => stored,
    sessions: () => [...ledger.data.sessions.values()],
  };
};

const TAB = 7;
const TWITCH_A = {platform: "twitch", channelId: "alpha", streamUrl: "https://www.twitch.tv/alpha",
  streamTitle: "Alpha live"};
const YT_VIDEO = {platform: "youtube", channelId: null, streamUrl: "https://www.youtube.com/watch?v=one",
  streamTitle: "Video one"};

test("recorder: ready opens a session for the captured channel", async () => {
  const {recorder, sessions} = makeRecorder();
  await recorder.captureStarting(TAB, TWITCH_A);
  assert.equal(sessions().length, 0, "nothing is recorded until the engine is ready");
  await recorder.engineEvent(TAB, {type: "ready", session_id: "backend-1"});
  const [session] = sessions();
  assert.equal(session.backend_session_id, "backend-1");
  assert.equal(session.platform, "twitch");
  assert.equal(session.channel_id, "alpha");
  assert.equal(session.started_at, "2026-11-12T20:00:00Z");
  assert.equal(session.ended_at, null);
});

test("recorder: verdicts, claims and usage land in the open session", async () => {
  const {recorder, ledger, advance} = makeRecorder();
  await recorder.captureStarting(TAB, TWITCH_A);
  await recorder.engineEvent(TAB, {type: "ready", session_id: "backend-1"});
  advance(30);
  await recorder.engineEvent(TAB, {type: "claim", id: "c1", text: "A claim.", topic: "other",
    check_worthiness: 0.8, outcome: "pending"});
  await recorder.engineEvent(TAB, VERDICT_FRAME);
  await recorder.engineEvent(TAB, {type: "usage", stage: "verify", provider: "anthropic",
    model: "claude-haiku-5-5", input_tokens: 100, cached_input_tokens: 0, output_tokens: 10,
    web_searches: 1, verdict_id: "v1"});
  assert.equal(ledger.data.verdicts.length, 1);
  assert.equal(ledger.data.verdicts[0].session_id, "s1");
  assert.equal(ledger.data.verdicts[0].channel_id, "alpha");
  assert.equal(ledger.data.claims.get("c1").gated_at, "2026-11-12T20:00:30Z");
  assert.equal(ledger.data.usage[0].channel_id, "alpha");
  assert.equal(ledger.data.usage[0].ts, "2026-11-12T20:00:30Z");
});

test("recorder: frames it does not keep are ignored", async () => {
  const {recorder, ledger, sessions} = makeRecorder();
  await recorder.captureStarting(TAB, TWITCH_A);
  await recorder.engineEvent(TAB, {type: "transcript", text: "hello"});
  await recorder.engineEvent(TAB, {type: "status", stage: "verifying", claim: "x"});
  await recorder.engineEvent(TAB, {type: "error", code: "llm_failure"});
  await recorder.engineEvent(TAB, null);
  assert.equal(sessions().length, 0);
  assert.equal(ledger.data.verdicts.length, 0);
});

test("recorder: frames arriving together are applied in order", async () => {
  const {recorder, ledger} = makeRecorder();
  // Not awaited one by one: this is how concurrent message handlers call it.
  await Promise.all([
    recorder.captureStarting(TAB, TWITCH_A),
    recorder.engineEvent(TAB, {type: "ready", session_id: "backend-1"}),
    recorder.engineEvent(TAB, VERDICT_FRAME),
  ]);
  assert.equal(ledger.data.sessions.size, 1);
  assert.equal(ledger.data.verdicts[0].session_id, "s1");
});

test("recorder: a reconnect closes the session and opens another", async () => {
  const {recorder, sessions, advance} = makeRecorder();
  await recorder.captureStarting(TAB, TWITCH_A);
  await recorder.engineEvent(TAB, {type: "ready", session_id: "backend-1"});
  advance(600);
  await recorder.engineEvent(TAB, {type: "ready", session_id: "backend-2"});
  const [first, second] = sessions();
  assert.equal(first.ended_at, "2026-11-12T20:10:00Z");
  assert.equal(second.backend_session_id, "backend-2");
  assert.equal(second.channel_id, "alpha");
  assert.equal(second.ended_at, null);
});

test("recorder: capture ending closes the session and forgets the tab", async () => {
  const {recorder, sessions, advance, stored} = makeRecorder();
  await recorder.captureStarting(TAB, TWITCH_A);
  await recorder.engineEvent(TAB, {type: "ready", session_id: "backend-1"});
  advance(90);
  await recorder.captureEnded();
  assert.equal(sessions()[0].ended_at, "2026-11-12T20:01:30Z");
  assert.equal(stored(), null);
  // Ending twice is harmless.
  await recorder.captureEnded();
});

test("recorder: an old backend's ready (no session id) still records", async () => {
  const {recorder, sessions} = makeRecorder();
  await recorder.captureStarting(TAB, TWITCH_A);
  await recorder.engineEvent(TAB, {type: "ready", server_version: "0.1.0"});
  assert.equal(sessions()[0].backend_session_id, null);
});

test("recorder: a frame with no session (worker lost its state) opens one", async () => {
  const {recorder, ledger, sessions} = makeRecorder();
  await recorder.engineEvent(TAB, VERDICT_FRAME);
  assert.equal(sessions().length, 1);
  assert.equal(ledger.data.verdicts[0].session_id, sessions()[0].id);
});

test("recorder: YouTube's channel arriving late fills in the same session", async () => {
  const {recorder, ledger, sessions} = makeRecorder();
  await recorder.captureStarting(TAB, YT_VIDEO);
  await recorder.engineEvent(TAB, {type: "ready", session_id: "backend-1"});
  await recorder.engineEvent(TAB, VERDICT_FRAME);
  assert.equal(ledger.data.verdicts[0].channel_id, null);

  await recorder.identityChanged(TAB, {...YT_VIDEO, channelId: "@Creator", channelName: "Creator"});
  assert.equal(sessions().length, 1, "still one session");
  assert.equal(sessions()[0].channel_id, "@creator");
  // The verdict recorded before the channel was known is attributed too.
  assert.equal(ledger.data.verdicts[0].channel_id, "@creator");
  assert.equal(ledger.data.verdicts[0].channel_name, "Creator");

  // And what follows carries the channel from the start.
  await recorder.engineEvent(TAB, {...VERDICT_FRAME, id: "v2"});
  assert.equal(ledger.data.verdicts[1].channel_id, "@creator");
});

test("recorder: moving to another channel splits the session", async () => {
  const {recorder, ledger, sessions, advance} = makeRecorder();
  await recorder.captureStarting(TAB, {...YT_VIDEO, channelId: "@first"});
  await recorder.engineEvent(TAB, {type: "ready", session_id: "backend-1"});
  await recorder.engineEvent(TAB, VERDICT_FRAME);
  advance(1200);
  await recorder.identityChanged(TAB, {platform: "youtube", channelId: "@second",
    streamUrl: "https://www.youtube.com/watch?v=two", streamTitle: "Video two"});
  await recorder.engineEvent(TAB, {...VERDICT_FRAME, id: "v2"});

  const [first, second] = sessions();
  assert.equal(first.channel_id, "@first");
  assert.equal(first.ended_at, "2026-11-12T20:20:00Z");
  assert.equal(second.channel_id, "@second");
  assert.equal(second.started_at, "2026-11-12T20:20:00Z");
  // Both halves belong to the same engine connection.
  assert.equal(second.backend_session_id, "backend-1");
  assert.deepEqual(
    ledger.data.verdicts.map((verdict) => [verdict.id, verdict.channel_id, verdict.session_id]),
    [["v1", "@first", first.id], ["v2", "@second", second.id]]
  );
});

test("recorder: another video on the same channel updates the stream, not the session", async () => {
  const {recorder, ledger, sessions} = makeRecorder();
  await recorder.captureStarting(TAB, {...YT_VIDEO, channelId: "@first"});
  await recorder.engineEvent(TAB, {type: "ready", session_id: "backend-1"});
  await recorder.identityChanged(TAB, {platform: "youtube", channelId: "@first",
    streamUrl: "https://www.youtube.com/watch?v=two", streamTitle: "Video two"});
  assert.equal(sessions().length, 1);
  assert.equal(sessions()[0].stream_url, "https://www.youtube.com/watch?v=two");
  await recorder.engineEvent(TAB, VERDICT_FRAME);
  assert.equal(ledger.data.verdicts[0].stream_title, "Video two");
});

test("recorder: the owner link flickering away on the same page changes nothing", async () => {
  const {recorder, ledger, sessions} = makeRecorder();
  const known = {...YT_VIDEO, channelId: "@first"};
  await recorder.captureStarting(TAB, known);
  await recorder.engineEvent(TAB, {type: "ready", session_id: "backend-1"});
  await recorder.identityChanged(TAB, YT_VIDEO); // same URL, link momentarily missing
  await recorder.engineEvent(TAB, VERDICT_FRAME);
  assert.equal(sessions().length, 1);
  assert.equal(ledger.data.verdicts[0].channel_id, "@first");
});

test("recorder: identity reports from another tab are ignored", async () => {
  const {recorder, sessions} = makeRecorder();
  await recorder.captureStarting(TAB, TWITCH_A);
  await recorder.engineEvent(TAB, {type: "ready", session_id: "backend-1"});
  await recorder.identityChanged(TAB + 1, {platform: "kick", channelId: "someoneelse"});
  assert.equal(sessions().length, 1);
  assert.equal(sessions()[0].channel_id, "alpha");
});

test("recorder: an identity report before ready only updates what will be recorded", async () => {
  const {recorder, sessions} = makeRecorder();
  await recorder.captureStarting(TAB, YT_VIDEO);
  await recorder.identityChanged(TAB, {...YT_VIDEO, channelId: "@creator"});
  assert.equal(sessions().length, 0);
  await recorder.engineEvent(TAB, {type: "ready", session_id: "backend-1"});
  assert.equal(sessions()[0].channel_id, "@creator");
});

test("recorder: feedback is validated and stored", async () => {
  const {recorder, ledger} = makeRecorder();
  await recorder.feedback({verdictId: "v1", rating: "up"});
  await recorder.feedback({verdictId: "v1", rating: "sideways"});
  assert.deepEqual(ledger.data.feedback, [
    {verdict_id: "v1", rating: "up", corrected_label: null, note: null,
      created_at: "2026-11-12T20:00:00Z"},
  ]);
});

test("recorder: retention deletes before the cutoff, and 0 keeps everything", async () => {
  const {recorder, ledger} = makeRecorder();
  assert.equal(await recorder.applyRetention(0), 0);
  assert.equal(await recorder.applyRetention(undefined), 0);
  assert.deepEqual(ledger.data.retentionCutoffs, []);
  assert.equal(await recorder.applyRetention(30), 2);
  assert.deepEqual(ledger.data.retentionCutoffs, ["2026-10-13T20:00:00Z"]);
});

/* ------------------------------------------------------------------ */
/* Queries                                                              */
/* ------------------------------------------------------------------ */

const verdict = (id, overrides = {}) => ({
  id,
  session_id: "s1",
  claim_text: `Claim ${id}`,
  explanation: "Because.",
  topic: "politics",
  label: "TRUE",
  checked_at: "2026-11-10T12:00:00Z",
  platform: "twitch",
  channel_id: "alpha",
  channel_name: "Alpha",
  sources: [],
  ...overrides,
});

const VERDICTS = Object.freeze([
  verdict("v1", {label: "FALSE", checked_at: "2026-11-10T12:00:00Z",
    claim_text: "Inflation hit 9 percent last year."}),
  verdict("v2", {label: "TRUE", checked_at: "2026-11-11T12:00:00Z", topic: "health",
    explanation: "The trial enrolled 30,000 people."}),
  verdict("v3", {label: "UNVERIFIED", checked_at: "2026-11-12T12:00:00Z", platform: "youtube",
    channel_id: "@beta", channel_name: "Beta", session_id: "s2"}),
  verdict("v4", {label: "MISLEADING", checked_at: "2026-11-09T12:00:00Z", platform: "kick",
    channel_id: "alpha", channel_name: "alpha", session_id: "s3"}),
]);

test("filterVerdicts: newest first, and no filters means everything", () => {
  assert.deepEqual(filterVerdicts(VERDICTS).map((row) => row.id), ["v3", "v2", "v1", "v4"]);
  assert.deepEqual(filterVerdicts(VERDICTS, {labels: [], platform: "", search: ""}).length, 4);
});

test("filterVerdicts: platform, channel, label, topic, session", () => {
  const ids = (filters) => filterVerdicts(VERDICTS, filters).map((row) => row.id);
  assert.deepEqual(ids({platform: "twitch"}), ["v2", "v1"]);
  // "alpha" on Twitch and "alpha" on Kick are different channels.
  assert.deepEqual(ids({channel: channelKey("kick", "alpha")}), ["v4"]);
  assert.deepEqual(ids({channel: channelKey("twitch", "alpha")}), ["v2", "v1"]);
  assert.deepEqual(ids({labels: ["FALSE", "MISLEADING"]}), ["v1", "v4"]);
  assert.deepEqual(ids({topic: "health"}), ["v2"]);
  assert.deepEqual(ids({sessionId: "s2"}), ["v3"]);
  assert.deepEqual(ids({platform: "twitch", labels: ["TRUE"]}), ["v2"]);
});

test("filterVerdicts: search covers claim and explanation, all words, any case", () => {
  const ids = (search) => filterVerdicts(VERDICTS, {search}).map((row) => row.id);
  assert.deepEqual(ids("INFLATION"), ["v1"]);
  assert.deepEqual(ids("trial 30,000"), ["v2"]);
  assert.deepEqual(ids("inflation trial"), []);
  assert.deepEqual(ids("   "), ["v3", "v2", "v1", "v4"]);
});

test("filterVerdicts: date range and feedback state", () => {
  const from = Date.parse("2026-11-10T00:00:00Z");
  const to = Date.parse("2026-11-12T00:00:00Z");
  assert.deepEqual(
    filterVerdicts(VERDICTS, {fromMs: from, toMs: to}).map((row) => row.id),
    ["v2", "v1"]
  );
  const feedback = new Map([["v1", {rating: "down"}], ["v2", {rating: "up"}]]);
  const ids = (state) => filterVerdicts(VERDICTS, {feedback: state}, feedback).map((row) => row.id);
  assert.deepEqual(ids("down"), ["v1"]);
  assert.deepEqual(ids("up"), ["v2"]);
  assert.deepEqual(ids("unrated"), ["v3", "v4"]);
});

test("adjudicatedShares: UNVERIFIED is coverage, not part of the shares", () => {
  const shares = adjudicatedShares({TRUE: 30, FALSE: 10, MISLEADING: 10, UNVERIFIED: 50});
  assert.equal(shares.n, 50);
  assert.equal(shares.reliable, true);
  assert.equal(shares.truePct, 60);
  assert.equal(shares.falsePct, 20);
  assert.equal(shares.misleadingPct, 20);
  assert.equal(shares.unverifiedPct, 50);
});

test("adjudicatedShares: below the floor the sample is flagged unreliable", () => {
  const few = adjudicatedShares({TRUE: 3, FALSE: 3, MISLEADING: 0, UNVERIFIED: 0});
  assert.equal(few.n, 6);
  assert.equal(few.reliable, false);
  const edge = adjudicatedShares({TRUE: MIN_ADJUDICATED_SAMPLE, FALSE: 0, MISLEADING: 0});
  assert.equal(edge.reliable, true);
  const none = adjudicatedShares({TRUE: 0, FALSE: 0, MISLEADING: 0, UNVERIFIED: 4});
  assert.equal(none.falsePct, null);
  assert.equal(none.unverifiedPct, 100);
  assert.equal(adjudicatedShares({}).unverifiedPct, null);
});

test("sessionDurationSeconds: ended, never ended, and nonsense", () => {
  assert.equal(
    sessionDurationSeconds({started_at: "2026-11-10T12:00:00Z", ended_at: "2026-11-10T13:30:00Z"}),
    5400
  );
  // The browser closed mid-stream: measured to the last recorded activity.
  assert.equal(
    sessionDurationSeconds({started_at: "2026-11-10T12:00:00Z", ended_at: null,
      last_activity_at: "2026-11-10T12:10:00Z"}),
    600
  );
  assert.equal(sessionDurationSeconds({started_at: "2026-11-10T12:00:00Z", ended_at: null}), 0);
  assert.equal(
    sessionDurationSeconds({started_at: "2026-11-10T12:00:00Z", ended_at: "2026-11-10T11:00:00Z"}),
    0
  );
});

const SESSIONS = Object.freeze([
  {id: "s1", platform: "twitch", channel_id: "alpha", channel_name: "Alpha",
    started_at: "2026-11-10T11:00:00Z", ended_at: "2026-11-10T13:00:00Z"},
  {id: "s2", platform: "youtube", channel_id: "@beta", channel_name: "Beta",
    started_at: "2026-11-12T11:00:00Z", ended_at: "2026-11-12T12:00:00Z"},
  {id: "s3", platform: "kick", channel_id: "alpha", channel_name: "alpha",
    started_at: "2026-11-09T11:00:00Z", ended_at: "2026-11-09T11:30:00Z"},
  {id: "s4", platform: "youtube", channel_id: null, channel_name: null,
    started_at: "2026-11-08T11:00:00Z", ended_at: "2026-11-08T11:10:00Z"},
]);

const usageRow = (overrides) => ({
  ts: "2026-11-10T12:00:00Z", session_id: "s1", stage: "verify", provider: "anthropic",
  model: "claude-haiku-5-5", calls: 1, input_tokens: 1000, cached_input_tokens: 0,
  output_tokens: 100, web_searches: 1, cost_usd: 0.011, cost_source: COST_SOURCE.ESTIMATE,
  platform: "twitch", channel_id: "alpha", channel_name: "Alpha", ...overrides,
});

const USAGE = Object.freeze([
  usageRow({}),
  usageRow({stage: "gate", web_searches: 0, cost_usd: 0.001}),
  usageRow({ts: "2026-11-12T11:30:00Z", session_id: "s2", provider: "openrouter",
    model: "google/gemini-3.5-flash-lite", cost_usd: 0.0078, cost_source: COST_SOURCE.PROVIDER,
    platform: "youtube", channel_id: "@beta", channel_name: "Beta"}),
  usageRow({ts: "2026-11-12T11:40:00Z", session_id: "s2", provider: "openrouter",
    model: "some/unpriced", cost_usd: null, cost_source: COST_SOURCE.UNKNOWN,
    platform: "youtube", channel_id: "@beta", channel_name: "Beta"}),
]);

const CLAIMS = Object.freeze([
  {id: "c1", session_id: "s1", platform: "twitch", channel_id: "alpha"},
  {id: "c2", session_id: "s1", platform: "twitch", channel_id: "alpha"},
  {id: "c3", session_id: "s1", platform: "twitch", channel_id: "alpha"},
  {id: "c4", session_id: "s2", platform: "youtube", channel_id: "@beta"},
]);

const LEDGER = Object.freeze({sessions: SESSIONS, verdicts: VERDICTS, claims: CLAIMS, usage: USAGE});

test("summarizeChannels: one row per platform+channel, most-watched first", () => {
  const rows = summarizeChannels(LEDGER);
  // The channel-less session s4 has nowhere to be attributed.
  assert.deepEqual(rows.map((row) => row.key), ["twitch:alpha", "youtube:@beta", "kick:alpha"]);
  const [alpha, beta, kickAlpha] = rows;
  assert.equal(alpha.sessions, 1);
  assert.equal(alpha.watchSeconds, 7200);
  assert.equal(alpha.claims, 3);
  assert.equal(alpha.checks, 2);
  assert.deepEqual(alpha.labels, {TRUE: 1, FALSE: 1, MISLEADING: 0, UNVERIFIED: 0});
  assert.equal(alpha.adjudicated.n, 2);
  assert.equal(alpha.adjudicated.reliable, false);
  approx(alpha.cost.usd, 0.012);
  assert.equal(alpha.cost.estimated, true);

  assert.equal(beta.channelName, "Beta");
  approx(beta.cost.usd, 0.0078);
  // An exact provider figure is not an estimate; the unpriced call is counted apart.
  assert.equal(beta.cost.estimated, false);
  assert.equal(beta.cost.unpricedCalls, 1);
  assert.equal(beta.adjudicated.n, 0);
  assert.equal(beta.adjudicated.unverifiedPct, 100);

  assert.equal(kickAlpha.checks, 1);
  assert.equal(kickAlpha.cost.usd, 0);
});

test("summarizeChannels: a later record supplies a missing display name", () => {
  const rows = summarizeChannels({
    sessions: [{id: "s", platform: "youtube", channel_id: "@x", channel_name: "@x",
      started_at: "2026-11-10T11:00:00Z", ended_at: "2026-11-10T12:00:00Z"}],
    verdicts: [verdict("v", {platform: "youtube", channel_id: "@x", channel_name: "Proper Name"})],
  });
  assert.equal(rows[0].channelName, "Proper Name");
});

test("summarizeSessions: newest first with checks, claims and cost", () => {
  const rows = summarizeSessions(LEDGER);
  assert.deepEqual(rows.map((row) => row.session.id), ["s2", "s1", "s3", "s4"]);
  const s1 = rows[1];
  assert.equal(s1.durationSeconds, 7200);
  assert.equal(s1.checks, 2);
  assert.equal(s1.claims, 3);
  approx(s1.cost.usd, 0.012);
  assert.equal(rows[3].checks, 0);
});

test("sortRows: either direction, with empty values always last", () => {
  const rows = [{n: 2, name: "b"}, {n: null, name: "c"}, {n: 9, name: "a"}];
  assert.deepEqual(sortRows(rows, (row) => row.n, "desc").map((row) => row.n), [9, 2, null]);
  assert.deepEqual(sortRows(rows, (row) => row.n, "asc").map((row) => row.n), [2, 9, null]);
  assert.deepEqual(sortRows(rows, (row) => row.name, "asc").map((row) => row.name), ["a", "b", "c"]);
  assert.equal(rows[0].n, 2, "the input is not reordered");
});

test("summarizeUsage: totals, breakdowns and spend per hour", () => {
  const summary = summarizeUsage(LEDGER);
  assert.equal(summary.checks, 4);
  assert.equal(summary.sessions, 4);
  assert.equal(summary.watchSeconds, 7200 + 3600 + 1800 + 600);
  assert.equal(summary.total.calls, 4);
  assert.equal(summary.total.webSearches, 3);
  approx(summary.total.cost.usd, 0.0198);
  assert.equal(summary.total.cost.estimated, true);
  assert.equal(summary.total.cost.unpricedCalls, 1);
  approx(summary.costPerHour, 0.0198 / (13200 / 3600));

  assert.deepEqual(summary.byStage.map((group) => [group.key, group.calls]), [
    ["verify", 3],
    ["gate", 1],
  ]);
  assert.equal(summary.byStage[0].label, "Checking claims");
  assert.deepEqual(summary.byPlatform.map((group) => group.key), ["twitch", "youtube"]);
  assert.equal(summary.byModel[0].label, "anthropic · claude-haiku-5-5");
  assert.deepEqual(summary.byChannel.map((group) => group.label), ["Alpha", "Beta"]);
});

test("summarizeUsage: a range limits every figure", () => {
  const range = {fromMs: Date.parse("2026-11-12T00:00:00Z"), toMs: null};
  const summary = summarizeUsage(LEDGER, range);
  assert.equal(summary.checks, 1);
  assert.equal(summary.sessions, 1);
  assert.equal(summary.total.calls, 2);
  approx(summary.total.cost.usd, 0.0078);
  assert.equal(summary.total.cost.estimated, false);
});

test("summarizeUsage: nothing recorded gives zeros, not NaN", () => {
  const summary = summarizeUsage({usage: [], sessions: [], verdicts: []});
  assert.equal(summary.total.cost.usd, 0);
  assert.equal(summary.costPerHour, null);
  assert.deepEqual(summary.byModel, []);
  assert.equal(summary.includesImported, false);
});

test("summarizeUsage: an imported session row counts all its calls", () => {
  const summary = summarizeUsage({
    usage: [usageRow({calls: 12, cost_usd: 0.132, imported: true})],
    sessions: [],
    verdicts: [],
  });
  assert.equal(summary.total.calls, 12);
  assert.equal(summary.includesImported, true);
});

test("periodRange: local-day boundaries", () => {
  const now = new Date(2026, 10, 12, 15, 30);
  const midnight = new Date(2026, 10, 12).getTime();
  assert.deepEqual(periodRange(PERIODS.TODAY, now), {fromMs: midnight, toMs: null});
  assert.equal(periodRange(PERIODS.WEEK, now).fromMs, midnight - 6 * 86_400_000);
  assert.equal(periodRange(PERIODS.MONTH, now).fromMs, midnight - 29 * 86_400_000);
  assert.deepEqual(periodRange(PERIODS.ALL, now), {fromMs: null, toMs: null});
});

test("dailySpend: one bucket per day, empty days included, oldest first", () => {
  const now = new Date(2026, 10, 12, 15, 30);
  const at = (day, hour) => new Date(2026, 10, day, hour).toISOString();
  const days = dailySpend(
    [
      usageRow({ts: at(12, 9), cost_usd: 0.01}),
      usageRow({ts: at(12, 14), cost_usd: 0.02, cost_source: COST_SOURCE.PROVIDER}),
      usageRow({ts: at(10, 23), cost_usd: 0.005, cost_source: COST_SOURCE.PROVIDER}),
      usageRow({ts: at(1, 12), cost_usd: 9}), // outside the window
      usageRow({ts: at(11, 12), cost_usd: null, cost_source: COST_SOURCE.UNKNOWN}),
    ],
    {days: 3, now}
  );
  assert.equal(days.length, 3);
  assert.deepEqual(days.map((day) => new Date(day.dayMs).getDate()), [10, 11, 12]);
  approx(days[0].usd, 0.005);
  assert.equal(days[0].estimated, false);
  assert.equal(days[1].usd, 0);
  assert.equal(days[1].calls, 1);
  approx(days[2].usd, 0.03);
  assert.equal(days[2].estimated, true);
});

test("countLabels and channelOptions", () => {
  assert.deepEqual(countLabels(VERDICTS), {TRUE: 1, FALSE: 1, MISLEADING: 1, UNVERIFIED: 1});
  assert.deepEqual(
    channelOptions(VERDICTS).map((option) => [option.key, option.label, option.checks]),
    [["twitch:alpha", "Alpha", 2], ["youtube:@beta", "Beta", 1], ["kick:alpha", "alpha", 1]]
  );
  assert.deepEqual(channelOptions([verdict("v", {channel_id: null})]), []);
});

/* ------------------------------------------------------------------ */
/* Export                                                               */
/* ------------------------------------------------------------------ */

test("csvCell quotes what needs quoting", () => {
  assert.equal(csvCell("plain"), "plain");
  assert.equal(csvCell('He said "no", twice'), '"He said ""no"", twice"');
  assert.equal(csvCell("two\nlines"), '"two\nlines"');
  assert.equal(csvCell(null), "");
  assert.equal(csvCell(undefined), "");
  assert.equal(csvCell(2300), "2300");
  assert.equal(csvCell(Number.NaN), "");
});

test("csvCell defuses spreadsheet formulas in transcribed text", () => {
  // A claim is speech the app does not control; a cell starting with one of
  // these would be executed by Excel or Sheets.
  assert.equal(csvCell("=HYPERLINK(\"http://evil\")"), "\"'=HYPERLINK(\"\"http://evil\"\")\"");
  assert.equal(csvCell("+1 for that"), "'+1 for that");
  assert.equal(csvCell("-5 degrees"), "'-5 degrees");
  assert.equal(csvCell("@everyone"), "'@everyone");
  // A real negative number is data, not a formula.
  assert.equal(csvCell(-5), "-5");
});

test("verdictsToCsv: header, one line per verdict, ratings joined in", () => {
  const rows = [
    buildVerdictRecord(VERDICT_FRAME, SESSION),
    buildVerdictRecord({...VERDICT_FRAME, id: "v2", claim: "Short, with a comma."}, SESSION),
  ];
  const csv = verdictsToCsv(rows, new Map([["v1", {rating: "down", corrected_label: "TRUE"}]]));
  const lines = csv.trimEnd().split("\r\n");
  assert.equal(lines.length, 3);
  assert.ok(lines[0].startsWith("checked_at,platform,channel,stream_title,"));
  assert.ok(lines[1].includes(",FALSE,The Eiffel Tower is 450 meters tall.,"));
  assert.ok(lines[1].includes(",down,TRUE,anthropic,claude-haiku-5-5,2300,v1"));
  assert.ok(lines[1].includes("youtube,Some Creator,Budget speech,"));
  assert.ok(lines[2].includes('"Short, with a comma."'));
  assert.ok(csv.endsWith("\r\n"));
  assert.equal(verdictsToCsv([]).trimEnd().split("\r\n").length, 1);
});

test("ledgerToJson is a self-describing document that round-trips", () => {
  const json = ledgerToJson(
    {sessions: [SESSION], verdicts: [buildVerdictRecord(VERDICT_FRAME, SESSION)]},
    {exportedAt: "2026-11-12T21:00:00Z", appVersion: "0.1.0"}
  );
  const parsed = JSON.parse(json);
  assert.equal(parsed.format, "live-stream-fact-checker-export");
  assert.equal(parsed.format_version, 1);
  assert.equal(parsed.sessions[0].id, "s1");
  assert.equal(parsed.verdicts[0].id, "v1");
  assert.deepEqual(parsed.claims, []);
  assert.deepEqual(parsed.usage, []);
});

test("exportFileName uses the local date", () => {
  assert.equal(
    exportFileName("fact-checks", "csv", new Date(2026, 0, 5, 23, 59)),
    "fact-checks-2026-01-05.csv"
  );
});

/* ------------------------------------------------------------------ */
/* Import paging                                                        */
/* ------------------------------------------------------------------ */

/** A backend with `count` sessions, served `pageSize` at a time. */
const makeImportBackend = (count, {honoursOffset = true, broken = []} = {}) => {
  const rows = Array.from({length: count}, (unused, index) => ({
    id: `b-${index}`,
    started_at: `2026-10-${String((index % 28) + 1).padStart(2, "0")}T10:00:00Z`,
    verify_calls: 0,
    est_cost_usd: 0,
  }));
  const calls = {list: [], detail: []};
  return {
    calls,
    statsSessions: async ({limit, offset}) => {
      calls.list.push(offset);
      const start = honoursOffset ? offset : 0;
      return rows.slice(start, start + limit);
    },
    statsSession: async (id) => {
      calls.detail.push(id);
      if (broken.includes(id)) {
        throw new Error("HTTP 500");
      }
      return {session: rows.find((row) => row.id === id), claims: [], verdicts: []};
    },
  };
};

const makeImportLedger = (alreadyKnown = []) => {
  const stored = new Set(alreadyKnown);
  return {
    stored,
    knownBackendSessions: async (ids) => new Set(ids.filter((id) => stored.has(id))),
    importSessions: async (batches) => {
      batches.forEach((batch) => stored.add(batch.session.backend_session_id));
      return {sessions: batches.length, verdicts: 0, skipped: 0};
    },
  };
};

test("import: pages through every session and stores each once", async () => {
  const api = makeImportBackend(230);
  const ledger = makeImportLedger();
  const progress = [];
  const result = await importFromBackend({api, ledger, onProgress: (step) => progress.push(step)});
  assert.deepEqual(api.calls.list, [0, 100, 200]);
  assert.equal(result.found, 230);
  assert.equal(result.sessions, 230);
  assert.equal(ledger.stored.size, 230);
  assert.deepEqual(progress[0], {done: 0, total: 230});
  assert.deepEqual(progress.at(-1), {done: 230, total: 230});
});

test("import: sessions already in the ledger are not fetched again", async () => {
  const api = makeImportBackend(5);
  const ledger = makeImportLedger(["b-0", "b-1", "b-2"]);
  const result = await importFromBackend({api, ledger});
  assert.deepEqual(api.calls.detail.sort(), ["b-3", "b-4"]);
  assert.equal(result.sessions, 2);
  assert.equal(result.skipped, 3);
});

test("import: a backend that ignores paging does not loop forever", async () => {
  // Exactly one page long, so only "nothing new" can end the loop.
  const api = makeImportBackend(100, {honoursOffset: false});
  const result = await importFromBackend({api, ledger: makeImportLedger()});
  assert.deepEqual(api.calls.list, [0, 100]);
  assert.equal(result.sessions, 100);
});

test("import: one unreadable session does not cost the others", async () => {
  const api = makeImportBackend(4, {broken: ["b-2"]});
  const ledger = makeImportLedger();
  const result = await importFromBackend({api, ledger});
  assert.equal(result.sessions, 3);
  assert.equal(result.failed, 1);
  assert.equal(ledger.stored.has("b-2"), false);
});

test("import: an empty backend is a result, not an error", async () => {
  const result = await importFromBackend({api: makeImportBackend(0), ledger: makeImportLedger()});
  assert.deepEqual(result, {found: 0, sessions: 0, verdicts: 0, skipped: 0, failed: 0});
});

test("import: can be cancelled", async () => {
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(
    importFromBackend({api: makeImportBackend(3), ledger: makeImportLedger(), signal: controller.signal})
  );
});
