/**
 * The shapes the ledger stores, and how they are built.
 *
 * Two sources produce the same records: live frames from the engine that is
 * running a session, and a one-off import of the companion backend's
 * `fact_checker.db`. Keeping both mappings here — and pure — means the
 * History and Usage screens never need to know where a row came from.
 *
 * Field names are snake_case on purpose: they mirror the backend's SQLite
 * columns (backend/app/db.py), so an import is a near copy and an export
 * reads the same whichever side produced it.
 *
 * Verdicts and usage rows repeat the session's platform, channel and stream
 * (denormalised). That keeps filtering a single pass over one store, and it
 * keeps each verdict attributed to the video it was said in even when the
 * session row is later updated.
 */

import {COST_SOURCE, priceCall} from "./pricing.mjs";

export const ENGINE = Object.freeze({
  COMPANION: "companion", // the local Python backend
  BROWSER: "browser", // the in-extension pipeline (build guide, phase 4)
});

export const PLATFORMS = Object.freeze(["twitch", "youtube", "kick", "rumble"]);

export const LABELS = Object.freeze(["TRUE", "FALSE", "MISLEADING", "UNVERIFIED"]);

const CLAIM_OUTCOMES = new Set([
  "pending",
  "below_threshold",
  "topic_skipped",
  "duplicate",
  "queue_dropped",
  "verify_failed",
  "verified",
]);

const text = (value) => (typeof value === "string" && value.trim() ? value.trim() : null);

const count = (value) => (Number.isFinite(value) && value > 0 ? Math.round(value) : 0);

/**
 * "2026-11-12T20:01:11Z" — second precision, the backend's own format, so
 * rows from both sources sort together as plain strings.
 *
 * @param {Date|number} [when]
 * @returns {string}
 */
export const isoSeconds = (when = new Date()) =>
  new Date(when).toISOString().replace(/\.\d{3}Z$/, "Z");

/**
 * A channel's stable id within its platform.
 *
 * Twitch logins, Kick slugs, Rumble slugs and YouTube @handles are
 * case-insensitive, so they are folded to lower case; YouTube's "UC…"
 * channel ids are case-sensitive and kept as they are.
 *
 * @param {string|null} platform
 * @param {string|null|undefined} rawChannelId
 * @returns {string|null}
 */
export const normalizeChannelId = (platform, rawChannelId) => {
  const channelId = text(rawChannelId);
  if (!channelId) {
    return null;
  }
  if (platform === "youtube" && /^UC[\w-]{22}$/.test(channelId)) {
    return channelId;
  }
  return channelId.toLowerCase();
};

/**
 * Identity of what is being watched. `(platform, channel_id)` is the key;
 * the same creator on two platforms is two channels, never merged.
 *
 * @param {object} [identity] - camelCase, as the capture code passes it
 * @returns {{platform: string|null, channel_id: string|null, channel_name: string|null,
 *   stream_url: string|null, stream_title: string|null}}
 */
export const normalizeIdentity = (identity = {}) => {
  const platform = PLATFORMS.includes(identity.platform) ? identity.platform : null;
  const channelId = normalizeChannelId(platform, identity.channelId);
  return {
    platform,
    channel_id: channelId,
    channel_name: channelId ? (text(identity.channelName) ?? channelId) : null,
    stream_url: text(identity.streamUrl),
    stream_title: text(identity.streamTitle),
  };
};

/**
 * @param {string|null} platform
 * @param {string|null} channelId
 * @returns {string|null} e.g. "youtube:@somehandle"; null without a channel
 */
export const channelKey = (platform, channelId) =>
  channelId ? `${platform ?? "unknown"}:${channelId}` : null;

/** @returns {boolean} whether two identities are the same channel */
export const isSameChannel = (first, second) =>
  (first?.platform ?? null) === (second?.platform ?? null) &&
  (first?.channel_id ?? null) === (second?.channel_id ?? null);

/**
 * @param {string} url
 * @returns {string|null} host without "www.", or null for a non-URL
 */
export const sourceDomain = (url) => {
  try {
    return new URL(url).hostname.replace(/^www\./, "") || null;
  } catch {
    return null;
  }
};

const identityFields = (session) => ({
  platform: session?.platform ?? null,
  channel_id: session?.channel_id ?? null,
  channel_name: session?.channel_name ?? null,
  stream_url: session?.stream_url ?? null,
  stream_title: session?.stream_title ?? null,
});

/**
 * @param {object} options
 * @param {string} options.id - the ledger's own id for this watch session
 * @param {string|null} [options.backendSessionId] - the companion's id for
 *   the same session, which is what lets an import skip it later
 * @param {object} options.identity - from normalizeIdentity
 * @param {string} options.startedAt - ISO time
 * @param {string} [options.engine]
 */
export const buildSessionRecord = ({
  id,
  backendSessionId = null,
  identity,
  startedAt,
  engine = ENGINE.COMPANION,
}) => ({
  id,
  backend_session_id: backendSessionId,
  engine,
  ...identityFields(identity),
  started_at: startedAt,
  ended_at: null,
  // Moves forward with every row written, so a session whose end was never
  // recorded (browser crash) still has a believable duration.
  last_activity_at: startedAt,
  imported: false,
});

/**
 * @param {object} frame - a "verdict" frame (backend/app/models.py VerdictFrame)
 * @param {object} session - the ledger session it belongs to
 * @returns {object|null} null when the frame is not a usable verdict
 */
export const buildVerdictRecord = (frame, session) => {
  if (!text(frame?.id) || !LABELS.includes(frame.label) || !text(frame.claim)) {
    return null;
  }
  return {
    id: frame.id,
    session_id: session.id,
    claim_id: text(frame.claim_id),
    claim_text: frame.claim.trim(),
    topic: text(frame.topic) ?? "other",
    label: frame.label,
    explanation: typeof frame.explanation === "string" ? frame.explanation : "",
    sources: (Array.isArray(frame.sources) ? frame.sources : [])
      .filter((source) => text(source?.url))
      .map((source) => ({
        url: source.url,
        title: text(source.title),
        domain: sourceDomain(source.url),
      })),
    checked_at: text(frame.checked_at) ?? isoSeconds(),
    used_fallback: Boolean(frame.used_fallback),
    latency_ms: Number.isFinite(frame.latency_ms) ? frame.latency_ms : null,
    provider: text(frame.provider),
    model: text(frame.model),
    engine: session.engine ?? ENGINE.COMPANION,
    ...identityFields(session),
  };
};

/**
 * @param {object} frame - a "claim" frame (backend ClaimFrame)
 * @param {object} session
 * @param {string} nowIso
 * @returns {object|null}
 */
export const buildClaimRecord = (frame, session, nowIso) => {
  if (!text(frame?.id) || !text(frame.text) || !CLAIM_OUTCOMES.has(frame.outcome)) {
    return null;
  }
  return {
    id: frame.id,
    session_id: session.id,
    text: frame.text.trim(),
    topic: text(frame.topic) ?? "other",
    check_worthiness: Number.isFinite(frame.check_worthiness) ? frame.check_worthiness : null,
    outcome: frame.outcome,
    gated_at: nowIso,
    ...identityFields(session),
  };
};

/**
 * One provider call, priced.
 *
 * @param {object} frame - a "usage" frame (backend UsageFrame)
 * @param {object} session
 * @param {string} nowIso
 * @returns {object|null}
 */
export const buildUsageRecord = (frame, session, nowIso) => {
  if (!text(frame?.provider) || !["gate", "verify", "stt"].includes(frame.stage)) {
    return null;
  }
  const tokens = {
    input_tokens: count(frame.input_tokens),
    cached_input_tokens: count(frame.cached_input_tokens),
    output_tokens: count(frame.output_tokens),
    web_searches: count(frame.web_searches),
  };
  const priced = priceCall({
    provider: frame.provider,
    model: frame.model,
    inputTokens: tokens.input_tokens,
    cachedInputTokens: tokens.cached_input_tokens,
    outputTokens: tokens.output_tokens,
    webSearches: tokens.web_searches,
    reportedCostUsd: frame.cost_usd ?? null,
  });
  return {
    ts: nowIso,
    session_id: session.id,
    engine: session.engine ?? ENGINE.COMPANION,
    stage: frame.stage,
    provider: frame.provider,
    model: text(frame.model),
    calls: 1,
    ...tokens,
    audio_seconds: 0,
    cost_usd: priced.costUsd,
    cost_source: priced.costSource,
    pricing_version: priced.pricingVersion,
    verdict_id: text(frame.verdict_id),
    platform: session.platform ?? null,
    channel_id: session.channel_id ?? null,
    channel_name: session.channel_name ?? null,
  };
};

/**
 * @param {object} feedback
 * @param {string} feedback.verdictId
 * @param {"up"|"down"} feedback.rating
 * @param {string|null} [feedback.correctedLabel]
 * @param {string|null} [feedback.note]
 * @param {string} nowIso
 * @returns {object|null}
 */
export const buildFeedbackRecord = ({verdictId, rating, correctedLabel = null, note = null}, nowIso) => {
  if (!text(verdictId) || !["up", "down"].includes(rating)) {
    return null;
  }
  return {
    verdict_id: verdictId,
    rating,
    corrected_label: LABELS.includes(correctedLabel) ? correctedLabel : null,
    note: text(note),
    created_at: nowIso,
  };
};

/**
 * Turn one `GET /stats/sessions/{id}` body from the companion backend into
 * ledger records.
 *
 * The backend never recorded per-call token usage, only a flat estimate per
 * session, so an imported session contributes ONE usage row carrying that
 * estimate and its call count. It is marked `imported` so the Usage screen
 * can say the figure is the old flat-rate estimate.
 *
 * @param {{session: object, claims: object[], verdicts: object[]}} detail
 * @returns {{session: object, claims: object[], verdicts: object[],
 *   feedback: object[], usage: object[]}|null}
 */
export const recordsFromBackendSession = (detail) => {
  const row = detail?.session;
  if (!text(row?.id) || !text(row.started_at)) {
    return null;
  }
  const identity = normalizeIdentity({
    platform: row.platform,
    channelId: row.channel,
    channelName: row.channel,
    streamTitle: row.title,
  });
  const session = {
    ...buildSessionRecord({
      id: row.id,
      backendSessionId: row.id,
      identity,
      startedAt: row.started_at,
    }),
    ended_at: text(row.ended_at),
    last_activity_at: text(row.ended_at) ?? row.started_at,
    speech_seconds: Number.isFinite(row.speech_seconds) ? row.speech_seconds : 0,
    audio_seconds: Number.isFinite(row.audio_seconds) ? row.audio_seconds : 0,
    imported: true,
  };

  const claimRows = Array.isArray(detail.claims) ? detail.claims : [];
  const claimsById = new Map(claimRows.map((claim) => [claim.id, claim]));
  const claims = claimRows
    .map((claim) => {
      const record = buildClaimRecord(
        {
          id: claim.id,
          text: claim.text,
          topic: claim.topic,
          check_worthiness: claim.check_worthiness,
          outcome: claim.outcome,
        },
        session,
        text(claim.gated_at) ?? session.started_at
      );
      return record;
    })
    .filter(Boolean);

  const verdicts = [];
  const feedback = [];
  for (const verdict of Array.isArray(detail.verdicts) ? detail.verdicts : []) {
    const claim = claimsById.get(verdict.claim_id);
    const record = buildVerdictRecord(
      {
        id: verdict.id,
        claim_id: verdict.claim_id,
        claim: claim?.text,
        topic: claim?.topic,
        label: verdict.label,
        explanation: verdict.explanation,
        sources: verdict.sources,
        checked_at: verdict.checked_at,
        used_fallback: verdict.used_fallback,
        latency_ms: verdict.latency_ms,
        provider: verdict.provider,
        model: verdict.model,
      },
      session
    );
    if (!record) {
      continue;
    }
    verdicts.push(record);
    if (verdict.feedback) {
      const rated = buildFeedbackRecord(
        {
          verdictId: verdict.id,
          rating: verdict.feedback.rating,
          correctedLabel: verdict.feedback.corrected_label,
          note: verdict.feedback.note,
        },
        text(verdict.feedback.created_at) ?? record.checked_at
      );
      if (rated) {
        feedback.push(rated);
      }
    }
  }

  const usage = [];
  const verifyCalls = count(row.verify_calls);
  const estimatedCost = Number.isFinite(row.est_cost_usd) ? row.est_cost_usd : 0;
  if (verifyCalls > 0 || estimatedCost > 0) {
    const answered = verdicts.find((verdict) => verdict.provider);
    usage.push({
      ts: session.ended_at ?? session.started_at,
      session_id: session.id,
      engine: ENGINE.COMPANION,
      stage: "verify",
      provider: answered?.provider ?? "unknown",
      model: answered?.model ?? null,
      calls: verifyCalls,
      input_tokens: 0,
      cached_input_tokens: 0,
      output_tokens: 0,
      web_searches: 0,
      audio_seconds: 0,
      cost_usd: estimatedCost,
      cost_source: COST_SOURCE.ESTIMATE,
      pricing_version: "backend-flat-rate",
      verdict_id: null,
      platform: session.platform,
      channel_id: session.channel_id,
      channel_name: session.channel_name,
      imported: true,
    });
  }
  return {session, claims, verdicts, feedback, usage};
};
