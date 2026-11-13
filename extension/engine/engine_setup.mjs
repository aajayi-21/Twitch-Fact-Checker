/**
 * Which engine checks claims on this device, and with whose key.
 *
 * Two records in chrome.storage.local (never storage.sync — these describe
 * this machine, and one of them is secret):
 *
 *   engine        mode ("companion": the local backend; "browser": this
 *                 extension), which provider runs each stage, model
 *                 overrides, and the spending limits
 *   providerKeys  the viewer's API keys, by provider id
 *
 * The keys are kept apart from everything else so that nothing which reads
 * settings ever has a key in its hands by accident, and so "remove key" is
 * one delete.
 *
 * Pure (no browser APIs): unit-tested under Node.
 */

import {normalizeBudgets} from "./pipeline/budget.mjs";
import {PROVIDERS} from "./providers/index.mjs";

export const ENGINE_STORAGE_KEY = "engine";
export const KEYS_STORAGE_KEY = "providerKeys";

export const ENGINE_MODE = Object.freeze({
  COMPANION: "companion",
  BROWSER: "browser",
});

export const STAGES = Object.freeze(["gate", "verify"]);

/** Why the browser engine cannot start (EnginePlan.reason). */
export const ENGINE_NOT_READY = Object.freeze({
  NO_KEY: "no-key", // no provider key has been added
});

const PROVIDER_IDS = Object.freeze(Object.keys(PROVIDERS));

const text = (value) => (typeof value === "string" ? value.trim() : "");

/**
 * @typedef {object} EngineState
 * @property {"companion"|"browser"} mode
 * @property {string|null} gateProvider - null: whichever provider has a key
 * @property {string|null} verifyProvider
 * @property {Record<string, {gate?: string, verify?: string}>} models -
 *   per-provider model overrides; absent means the provider's default
 * @property {{dailyChecks: number, monthlyUsd: number}} budgets
 */

/**
 * @param {unknown} stored
 * @returns {EngineState}
 */
export const normalizeEngineState = (stored) => {
  const record = stored && typeof stored === "object" ? stored : {};
  const provider = (value) => (PROVIDER_IDS.includes(value) ? value : null);
  const models = {};
  for (const providerId of PROVIDER_IDS) {
    const override = record.models?.[providerId];
    const cleaned = {};
    for (const stage of STAGES) {
      if (text(override?.[stage])) {
        cleaned[stage] = text(override[stage]);
      }
    }
    if (Object.keys(cleaned).length > 0) {
      models[providerId] = cleaned;
    }
  }
  return {
    // The companion is what every install before the browser engine uses.
    mode: record.mode === ENGINE_MODE.BROWSER ? ENGINE_MODE.BROWSER : ENGINE_MODE.COMPANION,
    gateProvider: provider(record.gateProvider),
    verifyProvider: provider(record.verifyProvider),
    models,
    budgets: normalizeBudgets(record.budgets),
  };
};

/**
 * @param {unknown} stored
 * @returns {Record<string, string>} keys for known providers only
 */
export const normalizeKeys = (stored) => {
  const record = stored && typeof stored === "object" ? stored : {};
  const keys = {};
  for (const providerId of PROVIDER_IDS) {
    if (text(record[providerId])) {
      keys[providerId] = text(record[providerId]);
    }
  }
  return keys;
};

/**
 * What the UI may show of a key: its last four characters.
 *
 * @param {string|undefined} apiKey
 * @returns {string|null}
 */
export const keyHint = (apiKey) => {
  const key = text(apiKey);
  if (!key) {
    return null;
  }
  // A very short "key" would be shown almost whole; show nothing of it.
  return key.length >= 12 ? `…${key.slice(-4)}` : "…";
};

/**
 * @param {EngineState} state
 * @param {string} providerId
 * @param {"gate"|"verify"} stage
 * @returns {string} the override, or the provider's default
 */
export const modelFor = (state, providerId, stage) =>
  state.models[providerId]?.[stage] ?? PROVIDERS[providerId].defaultModels[stage];

/**
 * Which provider runs a stage: the one chosen for it if it has a key,
 * otherwise the first provider (in display order) that does.
 *
 * @param {EngineState} state
 * @param {Record<string, string>} keys
 * @param {"gate"|"verify"} stage
 * @returns {string|null}
 */
export const providerFor = (state, keys, stage) => {
  const chosen = state[`${stage}Provider`];
  if (chosen && keys[chosen]) {
    return chosen;
  }
  return PROVIDER_IDS.find((providerId) => keys[providerId]) ?? null;
};

/**
 * @typedef {object} EnginePlan
 * @property {boolean} ready
 * @property {string|null} reason - ENGINE_NOT_READY.* when not ready
 * @property {{providerId: string, model: string, apiKey: string}|null} gate
 * @property {{providerId: string, model: string, apiKey: string}|null} verify
 */

/**
 * Decide how a browser-engine session starting now should run.
 *
 * @param {EngineState} state
 * @param {Record<string, string>} keys
 * @returns {EnginePlan}
 */
export const planBrowserEngine = (state, keys) => {
  const stage = (name) => {
    const providerId = providerFor(state, keys, name);
    return providerId
      ? {providerId, model: modelFor(state, providerId, name), apiKey: keys[providerId]}
      : null;
  };
  const gate = stage("gate");
  const verify = stage("verify");
  if (!gate || !verify) {
    return {ready: false, reason: ENGINE_NOT_READY.NO_KEY, gate: null, verify: null};
  }
  return {ready: true, reason: null, gate, verify};
};

/**
 * "Anthropic (claude-haiku-5-5)" or, with two providers,
 * "OpenRouter finds claims · Anthropic checks them".
 *
 * @param {EnginePlan} plan
 * @returns {string|null} null when no provider is connected
 */
export const describeEnginePlan = (plan) => {
  if (!plan.ready) {
    return null;
  }
  const name = (stage) => PROVIDERS[stage.providerId].label;
  if (plan.gate.providerId === plan.verify.providerId) {
    const models =
      plan.gate.model === plan.verify.model
        ? plan.gate.model
        : `${plan.gate.model} finds claims, ${plan.verify.model} checks them`;
    return `${name(plan.gate)} (${models})`;
  }
  return `${name(plan.gate)} finds claims · ${name(plan.verify)} checks them`;
};
