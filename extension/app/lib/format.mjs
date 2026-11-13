/**
 * How numbers, times and names are worded on screen. Pure, so the wording
 * is unit-tested under Node.
 */

export const PLATFORM_LABELS = Object.freeze({
  twitch: "Twitch",
  youtube: "YouTube",
  kick: "Kick",
  rumble: "Rumble",
});

/** @returns {string} "YouTube", or "Unknown site" for a null platform */
export const platformLabel = (platform) => PLATFORM_LABELS[platform] ?? "Unknown site";

/** "TRUE" -> "True" */
export const labelName = (label) =>
  typeof label === "string" && label ? label[0] + label.slice(1).toLowerCase() : "";

/**
 * A dollar amount sized to what it is: provider calls cost fractions of a
 * cent, a month costs dollars.
 *
 * @param {number|null|undefined} usd
 * @returns {string}
 */
export const formatUsd = (usd) => {
  if (!Number.isFinite(usd)) {
    return "—";
  }
  if (usd === 0) {
    return "$0.00";
  }
  if (usd < 0.01) {
    return `$${usd.toFixed(4)}`;
  }
  return `$${usd < 1 ? usd.toFixed(3) : usd.toFixed(2)}`;
};

/**
 * A cost total, with "≈" whenever any part of it is an estimate.
 *
 * @param {{usd: number, estimated: boolean, unpricedCalls: number}|null|undefined} cost
 * @returns {string}
 */
export const formatCost = (cost) => {
  if (!cost) {
    return "—";
  }
  if (cost.usd === 0 && cost.unpricedCalls > 0) {
    return "not priced";
  }
  return `${cost.estimated ? "≈ " : ""}${formatUsd(cost.usd)}`;
};

/**
 * @param {number} seconds
 * @returns {string} "2 h 5 min", "14 min", "under a minute"
 */
export const formatDuration = (seconds) => {
  if (!Number.isFinite(seconds) || seconds <= 0) {
    return "—";
  }
  if (seconds < 60) {
    return "under a minute";
  }
  const minutes = Math.round(seconds / 60);
  const hours = Math.floor(minutes / 60);
  return hours > 0 ? `${hours} h ${minutes % 60} min` : `${minutes} min`;
};

/**
 * @param {number} seconds
 * @returns {string} hours to one decimal: "3.5"
 */
export const formatHours = (seconds) =>
  Number.isFinite(seconds) ? (seconds / 3600).toFixed(1) : "—";

/**
 * @param {number|null} percent
 * @returns {string} "42%", or "—" when there is nothing to show
 */
export const formatPercent = (percent) =>
  Number.isFinite(percent) ? `${Math.round(percent)}%` : "—";

/**
 * @param {number} tokens
 * @returns {string} "950", "12.4K", "3.1M"
 */
export const formatTokens = (tokens) => {
  if (!Number.isFinite(tokens)) {
    return "—";
  }
  if (tokens < 1000) {
    return String(Math.round(tokens));
  }
  return tokens < 1_000_000
    ? `${(tokens / 1000).toFixed(1)}K`
    : `${(tokens / 1_000_000).toFixed(1)}M`;
};

/** @returns {string} "1 check", "3 checks" */
export const plural = (count, singular, pluralForm = `${singular}s`) =>
  `${count} ${count === 1 ? singular : pluralForm}`;

const DATE_TIME = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
});
const DATE_TIME_YEAR = new Intl.DateTimeFormat(undefined, {
  year: "numeric",
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
});
const DAY = new Intl.DateTimeFormat(undefined, {month: "short", day: "numeric"});

/**
 * A moment in the viewer's own time zone; the year only when it is not this
 * one.
 *
 * @param {string} iso
 * @param {Date} [now]
 * @returns {string}
 */
export const formatDateTime = (iso, now = new Date()) => {
  const date = new Date(iso ?? "");
  if (Number.isNaN(date.getTime())) {
    return "—";
  }
  return (date.getFullYear() === now.getFullYear() ? DATE_TIME : DATE_TIME_YEAR).format(date);
};

/** @param {number} dayMs @returns {string} "Nov 12" */
export const formatDay = (dayMs) => DAY.format(new Date(dayMs));
