/**
 * User settings, persisted under the single chrome.storage.sync key
 * "settings". Consumers that need live updates subscribe to
 * chrome.storage.onChanged (area "sync") and watch changes.settings.
 */

export const DEFAULT_SETTINGS = Object.freeze({
  backendUrl: "ws://127.0.0.1:8710/ws/audio",
  sensitivity: "medium", // low | medium | high
  popupPosition: "top-right", // top-left | top-right | bottom-left | bottom-right
  popupDurationS: 12,
  showTranscript: false,
});

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
