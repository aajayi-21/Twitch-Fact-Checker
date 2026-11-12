/**
 * Validation rules for the settings screen. Pure, so they are unit-tested
 * under Node.
 */

export const DURATION_MIN_S = 4;
export const DURATION_MAX_S = 60;

/**
 * A backend address is usable when it parses and uses ws:// or wss://.
 *
 * @param {string} value
 * @returns {boolean}
 */
export const isValidBackendUrl = (value) => {
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    return false;
  }
  return (parsed.protocol === "ws:" || parsed.protocol === "wss:") && parsed.host.length > 0;
};

/**
 * @param {unknown} rawValue - what was typed
 * @param {number} fallback - used when the input is not a number
 * @returns {number} whole seconds within the allowed range
 */
export const clampDurationSeconds = (rawValue, fallback) => {
  const seconds = Number(rawValue);
  if (rawValue === "" || rawValue === null || !Number.isFinite(seconds)) {
    return fallback;
  }
  return Math.min(DURATION_MAX_S, Math.max(DURATION_MIN_S, Math.round(seconds)));
};

/**
 * Update one topic in the settings' topic map. "other" is the catch-all and
 * can never be switched off.
 *
 * @param {Record<string, boolean>} topics
 * @param {string} slug
 * @param {boolean} enabled
 * @returns {Record<string, boolean>} a new map
 */
export const setTopicEnabled = (topics, slug, enabled) => ({
  ...topics,
  [slug]: slug === "other" ? true : enabled,
});

/**
 * Switch every optional topic on or off at once.
 *
 * @param {readonly string[]} slugs
 * @param {boolean} enabled
 * @returns {Record<string, boolean>}
 */
export const setAllTopics = (slugs, enabled) =>
  Object.fromEntries(slugs.map((slug) => [slug, slug === "other" ? true : enabled]));

/**
 * @param {Record<string, boolean>} topics
 * @param {readonly string[]} slugs
 * @returns {"all"|"some"|"none"} state of the optional topics, for the
 *   "all topics" checkbox
 */
export const topicsMasterState = (topics, slugs) => {
  const optional = slugs.filter((slug) => slug !== "other");
  const enabled = optional.filter((slug) => topics?.[slug] !== false).length;
  if (enabled === optional.length) {
    return "all";
  }
  return enabled === 0 ? "none" : "some";
};
