/**
 * User settings, persisted under the single chrome.storage.sync key
 * "settings". Consumers that need live updates subscribe to
 * chrome.storage.onChanged (area "sync") and watch changes.settings.
 */

/**
 * Claim-topic wire contract shared with the backend: canonical slug order,
 * display labels, and category colors. content/overlay.js keeps a mirror of
 * the label/color maps (classic content scripts cannot import modules) —
 * keep them in sync.
 */
export const TOPIC_SLUGS = Object.freeze([
  "politics",
  "health",
  "science_tech",
  "money",
  "history",
  "sports",
  "gaming",
  "entertainment",
  "other",
]);

export const TOPIC_LABELS = Object.freeze({
  politics: "Politics & current events",
  health: "Health & medicine",
  science_tech: "Science & technology",
  money: "Money & economy",
  history: "History",
  sports: "Sports",
  gaming: "Gaming",
  entertainment: "Entertainment & pop culture",
  other: "Everything else",
});

export const TOPIC_COLORS = Object.freeze({
  politics: "#e74c3c",
  health: "#2ecc71",
  science_tech: "#3498db",
  money: "#f1c40f",
  history: "#9b59b6",
  sports: "#e67e22",
  gaming: "#1abc9c",
  entertainment: "#e84393",
  other: "#95a5a6",
});

export const DEFAULT_SETTINGS = Object.freeze({
  backendUrl: "ws://127.0.0.1:8710/ws/audio",
  sensitivity: "medium", // low | medium | high
  popupPosition: "top-right", // top-left | top-right | bottom-left | bottom-right
  popupDurationS: 12,
  showTranscript: false,
  topics: Object.freeze({
    politics: true,
    health: true,
    science_tech: true,
    money: true,
    history: true,
    sports: true,
    gaming: true,
    entertainment: true,
    other: true, // catch-all — always enabled, the UI never unchecks it
  }),
});

/**
 * Convert the per-topic settings object into the enabled-slug array sent on
 * the wire (hello "enabled_topics" / config frame). Canonical slug order;
 * missing keys count as enabled (new topics default on); "other" is the
 * catch-all and is always included.
 *
 * @param {object|undefined} topics - settings.topics shape ({slug: boolean})
 * @returns {string[]} enabled topic slugs
 */
export const getEnabledTopicSlugs = (topics) => {
  const topicMap = topics && typeof topics === "object" ? topics : {};
  return TOPIC_SLUGS.filter(
    (slug) => slug === "other" || topicMap[slug] !== false
  );
};

/**
 * Load settings, filling any missing keys from DEFAULT_SETTINGS so callers
 * always receive a complete object even after future settings are added.
 *
 * @returns {Promise<object>}
 */
export const loadSettings = async () => {
  const {settings = {}} = await chrome.storage.sync.get("settings");
  return {...DEFAULT_SETTINGS, ...settings};
};

/**
 * Merge a partial update into the stored settings and persist the result.
 *
 * @param {object} partialSettings - subset of DEFAULT_SETTINGS keys
 * @returns {Promise<object>} the merged settings that were written
 */
export const saveSettings = async (partialSettings) => {
  const merged = {...(await loadSettings()), ...partialSettings};
  await chrome.storage.sync.set({settings: merged});
  return merged;
};
