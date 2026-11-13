/**
 * Everything the History and Usage screens compute from ledger rows.
 *
 * Pure functions over plain arrays: the app loads the ledger into memory
 * (a heavy viewer accumulates a few thousand verdicts a year) and filters and
 * totals it here, so every number on screen is unit-tested under Node.
 *
 * Honest-metrics rule, carried over from backend/app/stats.py: a channel's
 * TRUE / FALSE / MISLEADING shares are computed over ADJUDICATED verdicts
 * only, the sample size is always returned, and callers must not show a
 * share below MIN_ADJUDICATED_SAMPLE. UNVERIFIED measures how often the
 * pipeline could not find evidence — it says nothing about the channel.
 */

import {COST_SOURCE} from "./pricing.mjs";
import {LABELS, channelKey} from "./records.mjs";

export const ADJUDICATED_LABELS = Object.freeze(["TRUE", "FALSE", "MISLEADING"]);

/** Below this many adjudicated verdicts, shares are noise and are not shown. */
export const MIN_ADJUDICATED_SAMPLE = 30;

export const PERIODS = Object.freeze({
  TODAY: "today",
  WEEK: "7d",
  MONTH: "30d",
  ALL: "all",
});

const DAY_MS = 86_400_000;

const timeMs = (iso) => {
  const parsed = Date.parse(iso ?? "");
  return Number.isFinite(parsed) ? parsed : null;
};

const startOfLocalDay = (date) =>
  new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();

/**
 * The time window a period covers, in the viewer's own time zone.
 *
 * @param {string} period - PERIODS.*
 * @param {Date} [now]
 * @returns {{fromMs: number|null, toMs: number|null}} null = unbounded
 */
export const periodRange = (period, now = new Date()) => {
  const today = startOfLocalDay(now);
  if (period === PERIODS.TODAY) {
    return {fromMs: today, toMs: null};
  }
  if (period === PERIODS.WEEK) {
    return {fromMs: today - 6 * DAY_MS, toMs: null};
  }
  if (period === PERIODS.MONTH) {
    return {fromMs: today - 29 * DAY_MS, toMs: null};
  }
  return {fromMs: null, toMs: null};
};

const inRange = (ms, {fromMs = null, toMs = null} = {}) =>
  ms !== null && (fromMs === null || ms >= fromMs) && (toMs === null || ms < toMs);

/**
 * How long a session lasted. A session whose end was never recorded (the
 * browser closed mid-stream) is measured to its last recorded activity.
 *
 * @param {object} session
 * @returns {number} seconds, never negative
 */
export const sessionDurationSeconds = (session) => {
  const start = timeMs(session.started_at);
  const end = timeMs(session.ended_at) ?? timeMs(session.last_activity_at);
  if (start === null || end === null) {
    return 0;
  }
  return Math.max(0, (end - start) / 1000);
};

/* ------------------------------------------------------------------ */
/* Cost totals                                                          */
/* ------------------------------------------------------------------ */

/**
 * @typedef {object} CostTotal
 * @property {number} usd - sum of every known cost
 * @property {boolean} estimated - part of the sum is an estimate ("≈")
 * @property {number} unpricedCalls - calls with no known cost, NOT in `usd`
 */

/** @returns {CostTotal} */
export const emptyCost = () => ({usd: 0, estimated: false, unpricedCalls: 0});

/**
 * @param {CostTotal} total
 * @param {object} usageRow
 * @returns {CostTotal} the same object, updated
 */
const addCost = (total, usageRow) => {
  if (typeof usageRow.cost_usd === "number" && Number.isFinite(usageRow.cost_usd)) {
    total.usd += usageRow.cost_usd;
    if (usageRow.cost_source === COST_SOURCE.ESTIMATE) {
      total.estimated = true;
    }
  } else {
    total.unpricedCalls += usageRow.calls ?? 1;
  }
  return total;
};

/* ------------------------------------------------------------------ */
/* Verdict timeline                                                     */
/* ------------------------------------------------------------------ */

/**
 * @typedef {object} VerdictFilters
 * @property {string} [platform] - "" for any
 * @property {string} [channel] - a channelKey, "" for any
 * @property {string[]} [labels] - empty for any
 * @property {string} [topic] - "" for any
 * @property {string} [sessionId] - "" for any
 * @property {""|"up"|"down"|"unrated"} [feedback]
 * @property {string} [search] - matched against claim and explanation
 * @property {number|null} [fromMs]
 * @property {number|null} [toMs]
 */

/**
 * Filter and sort verdicts, newest first.
 *
 * @param {object[]} verdicts
 * @param {VerdictFilters} [filters]
 * @param {Map<string, object>} [feedbackByVerdict]
 * @returns {object[]}
 */
export const filterVerdicts = (verdicts, filters = {}, feedbackByVerdict = new Map()) => {
  const labels = new Set(filters.labels ?? []);
  const words = (filters.search ?? "")
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean);
  return verdicts
    .filter((verdict) => {
      if (filters.platform && verdict.platform !== filters.platform) {
        return false;
      }
      if (filters.channel && channelKey(verdict.platform, verdict.channel_id) !== filters.channel) {
        return false;
      }
      if (labels.size > 0 && !labels.has(verdict.label)) {
        return false;
      }
      if (filters.topic && verdict.topic !== filters.topic) {
        return false;
      }
      if (filters.sessionId && verdict.session_id !== filters.sessionId) {
        return false;
      }
      if (filters.feedback) {
        const rating = feedbackByVerdict.get(verdict.id)?.rating ?? "unrated";
        if (rating !== filters.feedback) {
          return false;
        }
      }
      const dated = (filters.fromMs ?? null) !== null || (filters.toMs ?? null) !== null;
      if (dated && !inRange(timeMs(verdict.checked_at), filters)) {
        return false;
      }
      if (words.length > 0) {
        const haystack = `${verdict.claim_text} ${verdict.explanation}`.toLowerCase();
        return words.every((word) => haystack.includes(word));
      }
      return true;
    })
    .sort((first, second) => (first.checked_at < second.checked_at ? 1 : -1));
};

/** @returns {Record<string, number>} a zero for every label */
const emptyLabelCounts = () => Object.fromEntries(LABELS.map((label) => [label, 0]));

/**
 * @param {object[]} verdicts
 * @returns {Record<string, number>}
 */
export const countLabels = (verdicts) => {
  const counts = emptyLabelCounts();
  for (const verdict of verdicts) {
    if (verdict.label in counts) {
      counts[verdict.label] += 1;
    }
  }
  return counts;
};

/**
 * Shares over adjudicated verdicts, with the sample size and whether it is
 * large enough to show.
 *
 * @param {Record<string, number>} labelCounts
 * @returns {{n: number, reliable: boolean, truePct: number|null,
 *   falsePct: number|null, misleadingPct: number|null, unverifiedPct: number|null}}
 */
export const adjudicatedShares = (labelCounts) => {
  const n = ADJUDICATED_LABELS.reduce((sum, label) => sum + (labelCounts[label] ?? 0), 0);
  const total = n + (labelCounts.UNVERIFIED ?? 0);
  const share = (label) => (n > 0 ? (100 * (labelCounts[label] ?? 0)) / n : null);
  return {
    n,
    reliable: n >= MIN_ADJUDICATED_SAMPLE,
    truePct: share("TRUE"),
    falsePct: share("FALSE"),
    misleadingPct: share("MISLEADING"),
    // Coverage of the pipeline, over ALL verdicts.
    unverifiedPct: total > 0 ? (100 * (labelCounts.UNVERIFIED ?? 0)) / total : null,
  };
};

/* ------------------------------------------------------------------ */
/* Channels and sessions                                                */
/* ------------------------------------------------------------------ */

/**
 * One row per (platform, channel). Rows without a channel are left out:
 * there is nothing to attribute them to.
 *
 * @param {object} ledger
 * @param {object[]} ledger.sessions
 * @param {object[]} ledger.verdicts
 * @param {object[]} [ledger.claims]
 * @param {object[]} [ledger.usage]
 * @returns {object[]} most-watched first
 */
export const summarizeChannels = ({sessions, verdicts, claims = [], usage = []}) => {
  const rows = new Map();
  const rowFor = (record) => {
    const key = channelKey(record.platform, record.channel_id);
    if (!key) {
      return null;
    }
    if (!rows.has(key)) {
      rows.set(key, {
        key,
        platform: record.platform,
        channelId: record.channel_id,
        channelName: record.channel_name ?? record.channel_id,
        sessions: 0,
        watchSeconds: 0,
        claims: 0,
        checks: 0,
        labels: emptyLabelCounts(),
        cost: emptyCost(),
        lastWatchedAt: null,
      });
    }
    const row = rows.get(key);
    // A later record may carry the display name an earlier one lacked.
    if (record.channel_name && row.channelName === row.channelId) {
      row.channelName = record.channel_name;
    }
    return row;
  };

  for (const session of sessions) {
    const row = rowFor(session);
    if (!row) {
      continue;
    }
    row.sessions += 1;
    row.watchSeconds += sessionDurationSeconds(session);
    if (!row.lastWatchedAt || session.started_at > row.lastWatchedAt) {
      row.lastWatchedAt = session.started_at;
    }
  }
  for (const claim of claims) {
    // One row per claim heard, whatever became of it (rows are upserted by
    // claim id, so a claim that was queued and then checked counts once).
    const row = rowFor(claim);
    if (row) {
      row.claims += 1;
    }
  }
  for (const verdict of verdicts) {
    const row = rowFor(verdict);
    if (row && verdict.label in row.labels) {
      row.checks += 1;
      row.labels[verdict.label] += 1;
    }
  }
  for (const usageRow of usage) {
    const row = rowFor(usageRow);
    if (row) {
      addCost(row.cost, usageRow);
    }
  }

  return [...rows.values()]
    .map((row) => ({...row, adjudicated: adjudicatedShares(row.labels)}))
    .sort((first, second) => second.watchSeconds - first.watchSeconds);
};

/**
 * One row per watch session, newest first.
 *
 * @param {object} ledger - as summarizeChannels
 * @returns {object[]}
 */
export const summarizeSessions = ({sessions, verdicts, claims = [], usage = []}) => {
  const rows = new Map(
    sessions.map((session) => [
      session.id,
      {
        session,
        durationSeconds: sessionDurationSeconds(session),
        claims: 0,
        checks: 0,
        labels: emptyLabelCounts(),
        cost: emptyCost(),
      },
    ])
  );
  for (const claim of claims) {
    const row = rows.get(claim.session_id);
    if (row) {
      row.claims += 1;
    }
  }
  for (const verdict of verdicts) {
    const row = rows.get(verdict.session_id);
    if (row && verdict.label in row.labels) {
      row.checks += 1;
      row.labels[verdict.label] += 1;
    }
  }
  for (const usageRow of usage) {
    const row = rows.get(usageRow.session_id);
    if (row) {
      addCost(row.cost, usageRow);
    }
  }
  return [...rows.values()].sort((first, second) =>
    first.session.started_at < second.session.started_at ? 1 : -1
  );
};

/**
 * Sort rows by a column. Rows whose value is null sort last in either
 * direction — a channel with no shares yet should never top a "highest
 * FALSE share" sort.
 *
 * @template T
 * @param {T[]} rows
 * @param {(row: T) => number|string|null} valueOf
 * @param {"asc"|"desc"} direction
 * @returns {T[]} a new array
 */
export const sortRows = (rows, valueOf, direction = "desc") => {
  const sign = direction === "asc" ? 1 : -1;
  return [...rows].sort((first, second) => {
    const a = valueOf(first);
    const b = valueOf(second);
    if (a === null || a === undefined) {
      return b === null || b === undefined ? 0 : 1;
    }
    if (b === null || b === undefined) {
      return -1;
    }
    if (typeof a === "string" || typeof b === "string") {
      return sign * String(a).localeCompare(String(b));
    }
    return sign * (a - b);
  });
};

/* ------------------------------------------------------------------ */
/* Usage                                                                */
/* ------------------------------------------------------------------ */

const emptyUsageGroup = (key, label) => ({
  key,
  label,
  calls: 0,
  inputTokens: 0,
  cachedInputTokens: 0,
  outputTokens: 0,
  webSearches: 0,
  cost: emptyCost(),
});

const addUsageRow = (group, usageRow) => {
  group.calls += usageRow.calls ?? 1;
  group.inputTokens += usageRow.input_tokens ?? 0;
  group.cachedInputTokens += usageRow.cached_input_tokens ?? 0;
  group.outputTokens += usageRow.output_tokens ?? 0;
  group.webSearches += usageRow.web_searches ?? 0;
  addCost(group.cost, usageRow);
};

const groupUsage = (usage, keyOf, labelOf) => {
  const groups = new Map();
  for (const usageRow of usage) {
    const key = keyOf(usageRow);
    if (!groups.has(key)) {
      groups.set(key, emptyUsageGroup(key, labelOf(usageRow)));
    }
    addUsageRow(groups.get(key), usageRow);
  }
  return [...groups.values()].sort((first, second) => second.cost.usd - first.cost.usd);
};

const STAGE_LABELS = Object.freeze({
  gate: "Finding claims",
  verify: "Checking claims",
  stt: "Speech to text",
});

/**
 * Totals and breakdowns for the Usage screen.
 *
 * @param {object} ledger
 * @param {object[]} ledger.usage
 * @param {object[]} ledger.sessions
 * @param {object[]} ledger.verdicts
 * @param {{fromMs: number|null, toMs: number|null}} [range]
 * @returns {object}
 */
export const summarizeUsage = ({usage, sessions, verdicts}, range = {}) => {
  const usageInRange = usage.filter((row) => inRange(timeMs(row.ts), range));
  const verdictsInRange = verdicts.filter((verdict) => inRange(timeMs(verdict.checked_at), range));
  const sessionsInRange = sessions.filter((session) => inRange(timeMs(session.started_at), range));

  const total = emptyUsageGroup("total", "Total");
  usageInRange.forEach((row) => addUsageRow(total, row));
  const watchSeconds = sessionsInRange.reduce(
    (sum, session) => sum + sessionDurationSeconds(session),
    0
  );

  return {
    checks: verdictsInRange.length,
    labels: countLabels(verdictsInRange),
    sessions: sessionsInRange.length,
    watchSeconds,
    total,
    // Spend per hour watched needs both a spend and a meaningful duration.
    costPerHour: watchSeconds >= 60 ? total.cost.usd / (watchSeconds / 3600) : null,
    includesImported: usageInRange.some((row) => row.imported),
    byModel: groupUsage(
      usageInRange,
      (row) => `${row.provider}|${row.model ?? ""}`,
      (row) => (row.model ? `${row.provider} · ${row.model}` : row.provider)
    ),
    byStage: groupUsage(
      usageInRange,
      (row) => row.stage,
      (row) => STAGE_LABELS[row.stage] ?? row.stage
    ),
    byPlatform: groupUsage(
      usageInRange,
      (row) => row.platform ?? "unknown",
      (row) => row.platform ?? "Unknown site"
    ),
    byChannel: groupUsage(
      usageInRange,
      (row) => channelKey(row.platform, row.channel_id) ?? "unknown",
      (row) => (row.channel_id ? (row.channel_name ?? row.channel_id) : "No channel")
    ),
  };
};

/**
 * Spend per local calendar day, oldest first, with empty days included so a
 * chart's bars are evenly spaced.
 *
 * @param {object[]} usage
 * @param {object} options
 * @param {number} options.days - how many days, ending today
 * @param {Date} [options.now]
 * @returns {{dayMs: number, usd: number, calls: number, estimated: boolean}[]}
 */
export const dailySpend = (usage, {days, now = new Date()}) => {
  const today = startOfLocalDay(now);
  const buckets = new Map();
  for (let offset = days - 1; offset >= 0; offset -= 1) {
    // Built from calendar fields, not by subtracting 24 h, so days stay
    // aligned across a daylight-saving change.
    const day = new Date(now.getFullYear(), now.getMonth(), now.getDate() - offset).getTime();
    buckets.set(day, {dayMs: day, usd: 0, calls: 0, estimated: false});
  }
  for (const row of usage) {
    const ms = timeMs(row.ts);
    if (ms === null || ms > today + DAY_MS) {
      continue;
    }
    const bucket = buckets.get(startOfLocalDay(new Date(ms)));
    if (!bucket) {
      continue;
    }
    bucket.calls += row.calls ?? 1;
    if (typeof row.cost_usd === "number") {
      bucket.usd += row.cost_usd;
      bucket.estimated ||= row.cost_source === COST_SOURCE.ESTIMATE;
    }
  }
  return [...buckets.values()];
};

/**
 * Where the viewer stands against the spending limits right now: provider
 * fact-check calls made today and dollars spent this calendar month, both in
 * the viewer's own time zone.
 *
 * @param {object[]} usage - rows covering at least this month so far
 * @param {Date} [now]
 * @returns {{checksToday: number, spendThisMonthUsd: number}}
 */
export const currentSpend = (usage, now = new Date()) => {
  const dayStart = startOfLocalDay(now);
  const monthStart = new Date(now.getFullYear(), now.getMonth(), 1).getTime();
  let checksToday = 0;
  let spendThisMonthUsd = 0;
  for (const row of usage) {
    const ms = timeMs(row.ts);
    if (ms === null || ms < monthStart) {
      continue;
    }
    if (typeof row.cost_usd === "number" && Number.isFinite(row.cost_usd)) {
      spendThisMonthUsd += row.cost_usd;
    }
    if (ms >= dayStart && row.stage === "verify") {
      checksToday += row.calls ?? 1;
    }
  }
  return {checksToday, spendThisMonthUsd};
};

/** @param {Date} [now] @returns {string} ISO time of the first moment of this month */
export const monthStartIso = (now = new Date()) =>
  new Date(now.getFullYear(), now.getMonth(), 1).toISOString();

/* ------------------------------------------------------------------ */
/* Filter options                                                       */
/* ------------------------------------------------------------------ */

/**
 * The channels a verdict filter can offer, most-checked first.
 *
 * @param {object[]} verdicts
 * @returns {{key: string, platform: string|null, label: string, checks: number}[]}
 */
export const channelOptions = (verdicts) => {
  const options = new Map();
  for (const verdict of verdicts) {
    const key = channelKey(verdict.platform, verdict.channel_id);
    if (!key) {
      continue;
    }
    if (!options.has(key)) {
      options.set(key, {
        key,
        platform: verdict.platform,
        label: verdict.channel_name ?? verdict.channel_id,
        checks: 0,
      });
    }
    options.get(key).checks += 1;
  }
  return [...options.values()].sort((first, second) => second.checks - first.checks);
};
