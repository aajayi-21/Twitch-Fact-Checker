/**
 * The engine choice and provider keys, as the app's screens see them, and
 * the actions that change them.
 *
 * Screens never hold a key: they get a last-four hint and whether a provider
 * is connected. A key passes through here once, on its way from the form to
 * a validation call and then to chrome.storage.local.
 */

import {
  ENGINE_MODE,
  ENGINE_STORAGE_KEY,
  KEYS_STORAGE_KEY,
  describeEnginePlan,
  keyHint,
  normalizeEngineState,
  planBrowserEngine,
} from "../../engine/engine_setup.mjs";
import {
  loadEngineState,
  loadProviderKeys,
  removeProviderKey,
  saveProviderKey,
  updateEngineState,
} from "../../engine/engine_store.mjs";
import {usageFrame} from "../../engine/pipeline/browser_pipeline.mjs";
import {getProvider} from "../../engine/providers/index.mjs";
import {recordUsage} from "../../ledger/ledger.mjs";
import {ENGINE, buildUsageRecord, isoSeconds} from "../../ledger/records.mjs";
import {PermissionDeclined, browserProvider} from "./engine_model.mjs";
import {useEffect, useState} from "./html.mjs";

// Promise-safe API namespace (see shared/capabilities.js § namespace idiom).
const chrome = globalThis.browser ?? globalThis.chrome;

/**
 * @typedef {object} EngineView
 * @property {boolean} loaded
 * @property {import("../../engine/engine_setup.mjs").EngineState} state
 * @property {Record<string, string>} hints - connected providers: id -> "…abcd"
 * @property {{ready: boolean, summary: string|null,
 *   gate: {providerId: string, model: string}|null,
 *   verify: {providerId: string, model: string}|null}} plan - without keys
 */

/** @type {EngineView} */
let view = {
  loaded: false,
  state: normalizeEngineState(null),
  hints: {},
  plan: {ready: false, summary: null, gate: null, verify: null},
};

const listeners = new Set();

/** Rebuild the view from what is stored. The keys do not leave this function. */
const refresh = async () => {
  const [state, keys] = await Promise.all([loadEngineState(), loadProviderKeys()]);
  const plan = planBrowserEngine(state, keys);
  const withoutKey = (stage) => stage && {providerId: stage.providerId, model: stage.model};
  view = {
    loaded: true,
    state,
    hints: Object.fromEntries(
      Object.entries(keys).map(([providerId, apiKey]) => [providerId, keyHint(apiKey)])
    ),
    plan: {
      ready: plan.ready,
      summary: describeEnginePlan(plan),
      gate: withoutKey(plan.gate),
      verify: withoutKey(plan.verify),
    },
  };
  listeners.forEach((listener) => listener());
};

/** Subscribe a component to the engine record. */
export const useEngine = () => {
  const [, forceRender] = useState(0);
  const rendered = view;
  useEffect(() => {
    const listener = () => forceRender((count) => count + 1);
    listeners.add(listener);
    if (view !== rendered) {
      listener(); // changed between render and effect (see app_store.mjs)
    }
    return () => listeners.delete(listener);
  }, []);
  return view;
};

export const getEngineView = () => view;

/** Load the record and keep it fresh. Call once at startup. */
export const initEngine = async () => {
  await refresh();
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && (changes[ENGINE_STORAGE_KEY] || changes[KEYS_STORAGE_KEY])) {
      refresh().catch((error) => {
        console.error("[fact-checker] engine settings failed to reload:", error);
      });
    }
  });
};

const change = async (update) => {
  await updateEngineState(update);
  await refresh();
};

/** @param {"companion"|"browser"} mode - takes effect at the next Start */
export const setEngineMode = (mode) => change((state) => ({...state, mode}));

/**
 * @param {"gate"|"verify"} stage
 * @param {string|null} providerId - null: whichever provider has a key
 */
export const chooseStageProvider = (stage, providerId) =>
  change((state) => ({...state, [`${stage}Provider`]: providerId}));

/**
 * @param {string} providerId
 * @param {"gate"|"verify"} stage
 * @param {string} model - empty restores the provider's default
 */
export const setStageModel = (providerId, stage, model) =>
  change((state) => ({
    ...state,
    models: {...state.models, [providerId]: {...state.models[providerId], [stage]: model}},
  }));

/** @param {Partial<{dailyChecks: number, monthlyUsd: number}>} patch */
export const setBudgets = (patch) =>
  change((state) => ({...state, budgets: {...state.budgets, ...patch}}));

/**
 * Check a key with its provider (a free call) and, if it is accepted, store
 * it. Call directly from the click handler: a provider that needs a site
 * permission asks for it here, and the browser only shows that prompt
 * during a user gesture.
 *
 * @param {string} providerId
 * @param {string} apiKey
 * @throws {PermissionDeclined|import("../../engine/providers/common.mjs").ProviderError}
 */
export const connectProvider = async (providerId, apiKey) => {
  const entry = browserProvider(providerId);
  if (entry.needsPermission) {
    const granted = await chrome.permissions.request({origins: [entry.hostPermission]});
    if (!granted) {
      throw new PermissionDeclined(entry.label);
    }
  }
  await getProvider(providerId).validateKey(apiKey);
  await saveProviderKey(providerId, apiKey);
  await refresh();
};

/**
 * Delete a provider's key. The service worker ends a session that is using
 * it (it watches the stored keys).
 *
 * @param {string} providerId
 */
export const disconnectProvider = async (providerId) => {
  await removeProviderKey(providerId);
  await refresh();
};

// Usage of calls made outside any capture session (the sample check) is
// filed under this id, so the money still shows on the Usage screen.
const SAMPLE_CHECK_SESSION = Object.freeze({
  id: "sample-check",
  engine: ENGINE.BROWSER,
  platform: null,
  channel_id: null,
  channel_name: null,
});

/** Put one call's usage in the history. A failure here must not fail the check. */
const recordSampleUsage = async (stage, config, usage) => {
  try {
    const record = buildUsageRecord(
      usageFrame(stage, config.providerId, config.model, usage),
      SAMPLE_CHECK_SESSION,
      isoSeconds()
    );
    if (record) {
      await recordUsage(record);
    }
  } catch (error) {
    console.warn("[fact-checker] sample-check usage not recorded:", error);
  }
};

/**
 * Run one sentence through the real gate and verify calls with the viewer's
 * own key — the same two calls a stream makes. Costs about a cent.
 *
 * Returns the backend's `/debug/text` shape, so the welcome flow shows the
 * result of either engine the same way.
 *
 * @param {string} text
 * @returns {Promise<{claims: object[], verdicts: object[]}>}
 * @throws {Error} when no provider is connected, or the provider fails
 */
export const runBrowserTestCheck = async (text) => {
  const plan = planBrowserEngine(await loadEngineState(), await loadProviderKeys());
  if (!plan.ready) {
    throw new Error("Add an API key first.");
  }
  const gate = getProvider(plan.gate.providerId);
  const verify = getProvider(plan.verify.providerId);
  const {claims, usage: gateUsage} = await gate.runGate({
    apiKey: plan.gate.apiKey,
    model: plan.gate.model,
    context: "",
    transcript: text,
  });
  await recordSampleUsage("gate", plan.gate, gateUsage);
  // One verification is enough to prove the chain, and keeps this to a cent.
  const [claim] = [...claims].sort(
    (first, second) => second.check_worthiness - first.check_worthiness
  );
  if (!claim) {
    return {claims, verdicts: []};
  }
  const {verdict, usage: verifyUsage} = await verify.runVerify({
    apiKey: plan.verify.apiKey,
    model: plan.verify.model,
    claim: claim.claim_text,
  });
  await recordSampleUsage("verify", plan.verify, verifyUsage);
  return {
    claims,
    verdicts: [{id: "sample", claim: claim.claim_text, topic: claim.topic, ...verdict}],
  };
};

export {ENGINE_MODE};
