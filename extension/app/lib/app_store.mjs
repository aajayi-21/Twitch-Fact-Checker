/**
 * App state and the actions that change it.
 *
 * One small observable store instead of a framework: screens read a slice
 * with `useAppState`, and every write goes through an action here, so there
 * is a single place that talks to chrome.storage and to the backend.
 *
 * What lives where:
 *   chrome.storage.sync   display and capture settings (shared/settings.js)
 *   chrome.storage.local  onboarding progress on this device
 *   the backend           provider keys and stage routing — never the browser
 */

import {deriveBackendHttpOrigin, loadSettings, saveSettings} from "../../shared/settings.js";
import {BACKEND_ERROR, BackendError, createBackendApi} from "./backend_api.mjs";
import {ENGINE_MODE, getEngineView} from "./engine_actions.mjs";
import {useEffect, useState} from "./html.mjs";

// Promise-safe API namespace (see shared/capabilities.js § namespace idiom).
const chrome = globalThis.browser ?? globalThis.chrome;

const ONBOARDING_KEY = "onboarding";

// Poll quickly while the backend is down (the person is probably starting
// it), slowly once it is up.
const POLL_DOWN_MS = 3000;
const POLL_UP_MS = 15_000;

/**
 * @typedef {object} AppState
 * @property {boolean} ready - settings and onboarding have loaded
 * @property {object|null} settings
 * @property {{consentAt: string|null, completedAt: string|null}} onboarding
 * @property {"checking"|"up"|"down"} backendState
 * @property {string|null} backendMessage - why it is down, when known
 * @property {object|null} health - /healthz body
 * @property {object|null} setup - /setup/status body
 * @property {object|null} summary - /stats/summary body
 */

/** @type {AppState} */
let state = {
  ready: false,
  settings: null,
  onboarding: {consentAt: null, completedAt: null},
  backendState: "checking",
  backendMessage: null,
  health: null,
  setup: null,
  summary: null,
};

const listeners = new Set();

const setState = (patch) => {
  state = {...state, ...patch};
  listeners.forEach((listener) => listener());
};

export const getAppState = () => state;

/**
 * Subscribe a component to the store.
 *
 * @template T
 * @param {(state: AppState) => T} [selector]
 * @returns {T}
 */
export const useAppState = (selector = (current) => current) => {
  const [, forceRender] = useState(0);
  const renderedState = state;
  useEffect(() => {
    const listener = () => forceRender((count) => count + 1);
    listeners.add(listener);
    // Effects run after paint — up to 100 ms later in a background tab. A
    // fast backend can finish loading before then, and without this check
    // those updates would be missed and the screen would stay on "Loading…".
    if (state !== renderedState) {
      listener();
    }
    return () => listeners.delete(listener);
  }, []);
  return selector(state);
};

const api = createBackendApi({
  getOrigin: () => (state.settings ? deriveBackendHttpOrigin(state.settings.backendUrl) : null),
});

/** The backend client, for screens that call it directly (history import). */
export const backendApi = api;

let pollTimer = null;

const schedulePoll = () => {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(
    () => refreshBackend().catch(() => {}),
    state.backendState === "up" ? POLL_UP_MS : POLL_DOWN_MS
  );
};

/**
 * Ask the backend how it is. Never throws: an unreachable backend is a
 * state, shown on screen, not an error.
 */
export const refreshBackend = async () => {
  // With the browser engine there is no backend to look for: do not knock on
  // a port nothing listens on every few seconds.
  if (getEngineView().state.mode === ENGINE_MODE.BROWSER) {
    clearTimeout(pollTimer);
    setState({backendState: "down", backendMessage: null, health: null});
    return;
  }
  try {
    const health = await api.health();
    // Status and totals are secondary: a hiccup there must not flip the
    // whole app to "backend down".
    const [setup, summary] = await Promise.all([
      api.setupStatus().catch(() => state.setup),
      api.statsSummary().catch(() => state.summary),
    ]);
    setState({backendState: "up", backendMessage: null, health, setup, summary});
  } catch (error) {
    setState({
      backendState: "down",
      backendMessage: error instanceof BackendError ? error.message : String(error),
      health: null,
    });
  } finally {
    if (document.visibilityState === "visible") {
      schedulePoll();
    }
  }
};

/** Load everything once and keep it fresh. Call once at startup. */
export const initApp = async () => {
  const [settings, stored] = await Promise.all([
    loadSettings(),
    chrome.storage.local.get(ONBOARDING_KEY),
  ]);
  setState({
    ready: true,
    settings,
    onboarding: {consentAt: null, completedAt: null, ...(stored[ONBOARDING_KEY] ?? {})},
  });

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "sync" && changes.settings) {
      // Another surface (or another device) changed the settings.
      loadSettings().then((latest) => setState({settings: latest}));
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      refreshBackend().catch(() => {});
    } else {
      clearTimeout(pollTimer);
    }
  });
  await refreshBackend();
};

/**
 * Change settings. The overlay and a running session pick the change up
 * through chrome.storage.onChanged, exactly as with the old settings page.
 *
 * @param {object} partialSettings
 */
export const updateSettings = async (partialSettings) => {
  const previousUrl = state.settings?.backendUrl;
  const settings = await saveSettings(partialSettings);
  setState({settings});
  if (settings.backendUrl !== previousUrl) {
    setState({backendState: "checking"});
    await refreshBackend();
  }
};

const saveOnboarding = async (patch) => {
  const onboarding = {...state.onboarding, ...patch};
  await chrome.storage.local.set({[ONBOARDING_KEY]: onboarding});
  setState({onboarding});
};

export const recordConsent = () => saveOnboarding({consentAt: new Date().toISOString()});

export const completeOnboarding = () => saveOnboarding({completedAt: new Date().toISOString()});

/**
 * Send a key to the backend, which validates it live and stores it.
 *
 * @param {string} provider
 * @param {string} apiKey
 * @throws {BackendError}
 */
export const saveProviderKey = async (provider, apiKey) => {
  const setup = await api.saveCredentials({provider, apiKey});
  setState({setup});
  refreshBackend().catch(() => {});
};

/**
 * @param {object} stageRequest - from setup_model.mjs stageRequestFromForm
 * @throws {BackendError}
 */
export const applyStages = async (stageRequest) => {
  const setup = await api.saveStages(stageRequest);
  setState({setup});
  refreshBackend().catch(() => {});
};

/**
 * Run a sample sentence through the real pipeline.
 *
 * @param {string} text
 * @returns {Promise<{claims: object[], verdicts: object[]}>}
 * @throws {BackendError}
 */
export const runTestCheck = (text) => api.runTestCheck(text);

export {BACKEND_ERROR, BackendError};
