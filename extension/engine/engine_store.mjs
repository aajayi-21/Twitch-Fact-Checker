/**
 * Reading and writing the engine record and the provider keys
 * (engine_setup.mjs), for the contexts that have chrome.storage: the service
 * worker and the app.
 *
 * Credential rules (build guide §3.6), enforced here:
 *   - keys live in chrome.storage.local only, never storage.sync;
 *   - content scripts are locked out of storage.local (`protectStorage`),
 *     because they run inside web pages;
 *   - nothing here logs a key, and the UI is given hints, not keys.
 */

import {
  ENGINE_STORAGE_KEY,
  KEYS_STORAGE_KEY,
  normalizeEngineState,
  normalizeKeys,
} from "./engine_setup.mjs";

// Promise-safe API namespace (see shared/capabilities.js § namespace idiom).
const chrome = globalThis.browser ?? globalThis.chrome;

/**
 * Keep content scripts out of storage.local. By default they can read it;
 * a compromised page must not be one step from the viewer's API keys. The
 * content script's own settings live in storage.sync and are unaffected.
 *
 * Call at service-worker start. A browser without the API (Firefox) keeps
 * its default, which is why keys are only ever stored by the Chrome-only
 * browser engine.
 */
export const protectStorage = async () => {
  try {
    await chrome.storage.local.setAccessLevel?.({accessLevel: "TRUSTED_CONTEXTS"});
  } catch (error) {
    console.warn("[fact-checker] could not restrict storage.local:", error);
  }
};

/** @returns {Promise<import("./engine_setup.mjs").EngineState>} */
export const loadEngineState = async () =>
  normalizeEngineState((await chrome.storage.local.get(ENGINE_STORAGE_KEY))[ENGINE_STORAGE_KEY]);

/** @returns {Promise<Record<string, string>>} */
export const loadProviderKeys = async () =>
  normalizeKeys((await chrome.storage.local.get(KEYS_STORAGE_KEY))[KEYS_STORAGE_KEY]);

// One writer at a time per context (see speech_store.mjs for the reasoning).
let writeQueue = Promise.resolve();

const serialized = (work) => {
  const run = writeQueue.then(work);
  writeQueue = run.catch(() => {});
  return run;
};

/**
 * @param {(state: import("./engine_setup.mjs").EngineState) => object} change
 * @returns {Promise<import("./engine_setup.mjs").EngineState>} the stored state
 */
export const updateEngineState = (change) =>
  serialized(async () => {
    const next = normalizeEngineState(change(await loadEngineState()));
    await chrome.storage.local.set({[ENGINE_STORAGE_KEY]: next});
    return next;
  });

/**
 * Store a provider's key, replacing any earlier one.
 *
 * @param {string} providerId
 * @param {string} apiKey
 * @returns {Promise<Record<string, string>>} all keys now stored
 */
export const saveProviderKey = (providerId, apiKey) =>
  serialized(async () => {
    const keys = normalizeKeys({...(await loadProviderKeys()), [providerId]: apiKey});
    await chrome.storage.local.set({[KEYS_STORAGE_KEY]: keys});
    return keys;
  });

/**
 * Delete a provider's key. Takes effect at once for new sessions; the
 * caller stops a running one.
 *
 * @param {string} providerId
 * @returns {Promise<Record<string, string>>} the keys that remain
 */
export const removeProviderKey = (providerId) =>
  serialized(async () => {
    const keys = await loadProviderKeys();
    delete keys[providerId];
    await chrome.storage.local.set({[KEYS_STORAGE_KEY]: keys});
    return keys;
  });
