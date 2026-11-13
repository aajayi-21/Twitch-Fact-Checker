/**
 * Reading and writing this device's speech record (speech_setup.mjs), for
 * the contexts that have chrome.storage: the service worker and the app.
 *
 * Also the two facts about the running browser that the pure rules need —
 * its major version and the version of the vendored speech runtime — and
 * the lock that says "a GPU speech job is running right now".
 *
 * Why a lock as well as the stored crash-guard marker: the marker alone
 * cannot tell "the browser died while the GPU was in use" from "a test is
 * still running in another tab". A Web Lock is released by the browser the
 * moment its holder goes away, so a marker with nobody holding the lock is
 * a crash, and a marker with the lock held is just work in progress.
 */

import {
  SPEECH_STORAGE_KEY,
  applyCrashGuard,
  normalizeSpeechState,
} from "./speech_setup.mjs";

// Promise-safe API namespace (see shared/capabilities.js § namespace idiom).
const chrome = globalThis.browser ?? globalThis.chrome;

export const GPU_LOCK_NAME = "factchecker-gpu-speech";

const RUNTIME_VERSIONS_PATH = "vendor/transformers/VERSIONS.txt";

/** @returns {Promise<import("./speech_setup.mjs").SpeechState>} */
export const loadSpeechState = async () =>
  normalizeSpeechState((await chrome.storage.local.get(SPEECH_STORAGE_KEY))[SPEECH_STORAGE_KEY]);

// Updates within one context are applied one after another, so two quick
// changes (a GPU attempt ending as a setting is saved) cannot overwrite
// each other. Different contexts rarely write at the same moment, and each
// write is a small merge of fresh state.
let writeQueue = Promise.resolve();

/**
 * Read, change and store the record.
 *
 * @param {(state: import("./speech_setup.mjs").SpeechState) => import("./speech_setup.mjs").SpeechState} change
 * @returns {Promise<import("./speech_setup.mjs").SpeechState>} the stored state
 */
export const updateSpeechState = (change) => {
  const run = writeQueue.then(async () => {
    const next = change(await loadSpeechState());
    await chrome.storage.local.set({[SPEECH_STORAGE_KEY]: next});
    return next;
  });
  writeQueue = run.catch(() => {});
  return run;
};

/** @returns {Promise<boolean>} whether any context holds the GPU speech lock */
export const isGpuSpeechRunning = async () => {
  try {
    const {held = []} = await navigator.locks.query();
    return held.some((lock) => lock.name === GPU_LOCK_NAME);
  } catch {
    // No Web Locks: assume nothing is running, which errs toward blocking
    // the GPU after a crash rather than toward a crash loop.
    return false;
  }
};

/**
 * Run `work` while holding the GPU speech lock.
 *
 * @template T
 * @param {() => Promise<T>} work
 * @returns {Promise<T>}
 */
export const withGpuSpeechLock = (work) =>
  navigator.locks ? navigator.locks.request(GPU_LOCK_NAME, work) : work();

/**
 * The record with the crash guard applied: if a GPU attempt was left
 * unfinished and nothing is running now, the GPU is blocked and that is
 * stored. Call at every start.
 *
 * @returns {Promise<{state: import("./speech_setup.mjs").SpeechState, tripped: boolean}>}
 */
export const loadGuardedSpeechState = async () => {
  const state = await loadSpeechState();
  if (!state.gpuAttempt || (await isGpuSpeechRunning())) {
    return {state, tripped: false};
  }
  let tripped = false;
  const stored = await updateSpeechState((fresh) => {
    const guarded = applyCrashGuard(fresh, new Date().toISOString());
    tripped = guarded.tripped;
    return guarded.state;
  });
  return {state: stored, tripped};
};

/**
 * @param {string} [userAgent]
 * @returns {string} the browser's major version, or "unknown"
 */
export const browserMajorVersion = (userAgent = navigator.userAgent) =>
  /(?:Chrome|Chromium|Edg)\/(\d+)/.exec(userAgent)?.[1] ?? "unknown";

let runtimeVersionPromise = null;

/**
 * The vendored speech runtime's versions, as one string — or null when the
 * runtime is not in this copy of the extension (it is fetched by
 * scripts/vendor_stt_runtime.py, not checked in).
 *
 * @returns {Promise<string|null>}
 */
export const loadRuntimeVersion = () => {
  runtimeVersionPromise ??= fetch(chrome.runtime.getURL(RUNTIME_VERSIONS_PATH))
    .then(async (response) => {
      if (!response.ok) {
        return null;
      }
      // Keep the "package@version" lines; the file also carries a header.
      const packages = (await response.text())
        .split("\n")
        .map((line) => line.trim())
        .filter((line) => /@\d/.test(line));
      return packages.join(";") || null;
    })
    .catch(() => null);
  return runtimeVersionPromise;
};
