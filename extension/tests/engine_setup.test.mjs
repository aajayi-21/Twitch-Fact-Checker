/**
 * Unit tests for the engine choice and provider keys: what is stored, how a
 * browser-engine session is planned from it, what the UI may show of a key,
 * the wording of provider failures, and the spend the limits are checked
 * against.
 *
 * Run from the repository root:
 *
 *     node --test "extension/tests/*.test.mjs"
 */

import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import {test} from "node:test";

import {
  BROWSER_PROVIDERS,
  PermissionDeclined,
  describeProviderError,
} from "../app/lib/engine_model.mjs";
import {
  ENGINE_MODE,
  ENGINE_NOT_READY,
  describeEnginePlan,
  keyHint,
  modelFor,
  normalizeEngineState,
  normalizeKeys,
  planBrowserEngine,
  providerFor,
} from "../engine/engine_setup.mjs";
import {DEFAULT_BUDGETS} from "../engine/pipeline/budget.mjs";
import {ERROR_KIND, ProviderError} from "../engine/providers/common.mjs";
import {PROVIDERS} from "../engine/providers/index.mjs";
import {COST_SOURCE} from "../ledger/pricing.mjs";
import {currentSpend, monthStartIso} from "../ledger/queries.mjs";
import {buildUsageRecord} from "../ledger/records.mjs";
import {createSessionRecorder} from "../ledger/session_recorder.mjs";

/* ------------------------------------------------------------------ */
/* The stored records                                                   */
/* ------------------------------------------------------------------ */

test("nothing stored means the local backend, default limits, no routing", () => {
  for (const stored of [undefined, null, "junk", {}]) {
    assert.deepEqual(normalizeEngineState(stored), {
      // Every install that predates the browser engine uses the backend.
      mode: ENGINE_MODE.COMPANION,
      gateProvider: null,
      verifyProvider: null,
      models: {},
      budgets: {...DEFAULT_BUDGETS},
    });
  }
});

test("unknown providers, blank models and bad limits are dropped one by one", () => {
  const state = normalizeEngineState({
    mode: "browser",
    gateProvider: "anthropic",
    verifyProvider: "skynet",
    models: {
      anthropic: {gate: "  claude-sonnet-5-5 ", verify: ""},
      skynet: {gate: "t-800"},
      openrouter: {verify: 42},
    },
    budgets: {dailyChecks: 40, monthlyUsd: "all of it"},
  });
  assert.equal(state.mode, ENGINE_MODE.BROWSER);
  assert.equal(state.gateProvider, "anthropic");
  assert.equal(state.verifyProvider, null);
  assert.deepEqual(state.models, {anthropic: {gate: "claude-sonnet-5-5"}});
  assert.deepEqual(state.budgets, {dailyChecks: 40, monthlyUsd: DEFAULT_BUDGETS.monthlyUsd});
});

test("keys: only known providers, trimmed, never empty", () => {
  assert.deepEqual(
    normalizeKeys({anthropic: "  sk-ant-abc  ", openai: "", gemini: 7, skynet: "x"}),
    {anthropic: "sk-ant-abc"}
  );
  assert.deepEqual(normalizeKeys(null), {});
});

test("keyHint shows the last four characters, and nothing of a short value", () => {
  assert.equal(keyHint("sk-ant-api03-abcdefgh1234"), "…1234");
  // Four characters of an eight-character secret is half of it.
  assert.equal(keyHint("short123"), "…");
  assert.equal(keyHint(""), null);
  assert.equal(keyHint(undefined), null);
});

/* ------------------------------------------------------------------ */
/* Planning a session                                                   */
/* ------------------------------------------------------------------ */

const state = (overrides = {}) => normalizeEngineState({mode: "browser", ...overrides});

test("plan: no key, no session", () => {
  assert.deepEqual(planBrowserEngine(state(), {}), {
    ready: false,
    reason: ENGINE_NOT_READY.NO_KEY,
    gate: null,
    verify: null,
  });
});

test("plan: one key runs both steps on that provider's default models", () => {
  const plan = planBrowserEngine(state(), {anthropic: "sk-ant-key"});
  assert.equal(plan.ready, true);
  const expected = {
    providerId: "anthropic",
    model: PROVIDERS.anthropic.defaultModels.gate,
    apiKey: "sk-ant-key",
  };
  assert.deepEqual(plan.gate, expected);
  assert.deepEqual(plan.verify, {...expected, model: PROVIDERS.anthropic.defaultModels.verify});
  assert.equal(describeEnginePlan(plan), `Anthropic (${expected.model})`);
});

test("plan: with several keys and no choice, the first in display order is used", () => {
  const keys = {gemini: "g", anthropic: "a", openrouter: "o"};
  assert.equal(providerFor(state(), keys, "gate"), "openrouter");
  assert.equal(providerFor(state(), keys, "verify"), "openrouter");
});

test("plan: each step can have its own provider and model", () => {
  const plan = planBrowserEngine(
    state({
      gateProvider: "openrouter",
      verifyProvider: "anthropic",
      models: {anthropic: {verify: "claude-sonnet-5-5"}},
    }),
    {anthropic: "a-key", openrouter: "o-key"}
  );
  assert.equal(plan.gate.providerId, "openrouter");
  assert.equal(plan.gate.apiKey, "o-key");
  assert.equal(plan.verify.providerId, "anthropic");
  assert.equal(plan.verify.model, "claude-sonnet-5-5");
  assert.equal(describeEnginePlan(plan), "OpenRouter finds claims · Anthropic checks them");
});

test("plan: a chosen provider whose key was removed falls back to one that has a key", () => {
  const plan = planBrowserEngine(state({verifyProvider: "openai"}), {anthropic: "a-key"});
  assert.equal(plan.ready, true);
  assert.equal(plan.verify.providerId, "anthropic");
});

test("modelFor: the override, else the provider's default", () => {
  const engine = state({models: {openrouter: {gate: "openai/gpt-6-luna"}}});
  assert.equal(modelFor(engine, "openrouter", "gate"), "openai/gpt-6-luna");
  assert.equal(modelFor(engine, "openrouter", "verify"), PROVIDERS.openrouter.defaultModels.verify);
});

test("describeEnginePlan: one provider with two models names both; not ready is null", () => {
  const plan = planBrowserEngine(
    state({models: {anthropic: {verify: "claude-sonnet-5-5"}}}),
    {anthropic: "a-key"}
  );
  assert.equal(
    describeEnginePlan(plan),
    `Anthropic (${PROVIDERS.anthropic.defaultModels.gate} finds claims, claude-sonnet-5-5 checks them)`
  );
  assert.equal(describeEnginePlan(planBrowserEngine(state(), {})), null);
});

test("OpenRouter's default is a model that exists, not the backend's withdrawn one", () => {
  for (const stage of ["gate", "verify"]) {
    assert.ok(!PROVIDERS.openrouter.defaultModels[stage].includes("mercury"));
  }
});

/* ------------------------------------------------------------------ */
/* Presentation                                                         */
/* ------------------------------------------------------------------ */

test("the four providers are offered, each with what the UI needs", () => {
  assert.deepEqual(
    BROWSER_PROVIDERS.map((provider) => provider.id),
    ["openrouter", "anthropic", "openai", "gemini"]
  );
  for (const provider of BROWSER_PROVIDERS) {
    assert.ok(provider.label && provider.blurb && provider.keyPlaceholder, provider.id);
    assert.match(provider.keyUrl, /^https:\/\//);
    assert.ok(provider.defaultModels.gate && provider.defaultModels.verify, provider.id);
  }
  // The two never called live from this extension say so.
  assert.deepEqual(
    BROWSER_PROVIDERS.filter((provider) => !provider.verifiedLive).map((provider) => provider.id),
    ["openai", "gemini"]
  );
});

test("every provider that needs a site permission has it declared as optional", async () => {
  const manifest = JSON.parse(
    await readFile(new URL("../manifest.json", import.meta.url), "utf8")
  );
  const needing = BROWSER_PROVIDERS.filter((provider) => provider.needsPermission);
  assert.deepEqual(needing.map((provider) => provider.id), ["gemini"]);
  for (const provider of needing) {
    assert.ok(
      manifest.optional_host_permissions.includes(provider.hostPermission),
      `${provider.hostPermission} must be in optional_host_permissions`
    );
    // Optional, so installing asks for nothing extra.
    assert.ok(!manifest.host_permissions.includes(provider.hostPermission));
  }
});

test("describeProviderError: each kind of failure gets its own plain sentence", () => {
  const say = (kind, message = "raw detail") =>
    describeProviderError(new ProviderError(kind, message), "Anthropic");
  assert.match(say(ERROR_KIND.AUTH), /did not accept that key/);
  assert.match(say(ERROR_KIND.BILLING), /no credit or billing/);
  assert.match(say(ERROR_KIND.RATE_LIMIT), /rate-limiting/);
  assert.match(say(ERROR_KIND.NETWORK), /could not be reached/);
  assert.match(say(ERROR_KIND.TIMEOUT), /took too long/);
  assert.equal(say(ERROR_KIND.BAD_REQUEST, "unknown model"), "Anthropic rejected the request: unknown model");
  assert.match(say(ERROR_KIND.SERVER), /having problems/);
  assert.match(
    describeProviderError(new PermissionDeclined("Gemini"), "Gemini"),
    /allow the extension to reach it/
  );
  assert.equal(describeProviderError(new Error("boom"), "X"), "boom");
  // The provider's own text is not echoed for an auth failure (it can quote
  // the key back).
  assert.ok(!say(ERROR_KIND.AUTH, "sk-ant-secret is invalid").includes("sk-ant"));
});

/* ------------------------------------------------------------------ */
/* Spend the limits are checked against                                 */
/* ------------------------------------------------------------------ */

test("currentSpend: verify calls today, dollars this month, in local time", () => {
  const now = new Date(2026, 10, 13, 15, 0);
  const at = (day, hour, month = 10) => new Date(2026, month, day, hour).toISOString();
  const row = (ts, overrides = {}) => ({ts, stage: "verify", calls: 1, cost_usd: 0.01, ...overrides});
  const spent = currentSpend(
    [
      row(at(13, 9)),
      row(at(13, 14), {calls: 2, cost_usd: 0.02}),
      row(at(13, 10), {stage: "gate", cost_usd: 0.001}), // spend, but not a check
      row(at(12, 23)), // yesterday: this month's spend, not today's checks
      row(at(13, 11), {cost_usd: null}), // unpriced: a check, no dollars
      row(at(31, 12, 9)), // last month
    ],
    now
  );
  assert.equal(spent.checksToday, 4);
  assert.ok(Math.abs(spent.spendThisMonthUsd - 0.041) < 1e-9);
  assert.deepEqual(currentSpend([], now), {checksToday: 0, spendThisMonthUsd: 0});
  assert.equal(new Date(monthStartIso(now)).getTime(), new Date(2026, 10, 1).getTime());
});

/* ------------------------------------------------------------------ */
/* History of a browser-engine session                                  */
/* ------------------------------------------------------------------ */

test("a usage frame covering two calls is recorded as two", () => {
  const session = {id: "s1", engine: "browser", platform: "twitch", channel_id: "alpha"};
  const record = buildUsageRecord(
    {stage: "verify", provider: "gemini", model: "gemini-3.5-flash-lite", calls: 2, input_tokens: 10},
    session,
    "2026-11-13T12:00:00Z"
  );
  assert.equal(record.calls, 2);
  assert.equal(record.engine, "browser");
  // No list price for this model yet: unpriced, never free.
  assert.equal(record.cost_source, COST_SOURCE.UNKNOWN);
  assert.equal(buildUsageRecord({stage: "gate", provider: "ollama"}, session, "t").calls, 1);
});

test("the history records which engine ran a session, and keeps it across a channel change", async () => {
  const sessions = new Map();
  let stored = null;
  let nextId = 0;
  const recorder = createSessionRecorder({
    ledger: {
      putSession: async (session) => void sessions.set(session.id, {...session}),
      updateSession: async (id, patch) => void sessions.set(id, {...sessions.get(id), ...patch}),
    },
    memory: {
      get: async () => (stored ? structuredClone(stored) : null),
      set: async (next) => {
        stored = next ? structuredClone(next) : null;
      },
    },
    newId: () => `s${(nextId += 1)}`,
  });
  await recorder.captureStarting(1, {platform: "twitch", channelId: "alpha"});
  await recorder.engineEvent(1, {type: "ready", engine: "browser"});
  assert.equal(sessions.get("s1").engine, "browser");
  assert.equal(sessions.get("s1").backend_session_id, null);
  await recorder.identityChanged(1, {platform: "twitch", channelId: "beta"});
  assert.equal(sessions.get("s2").engine, "browser");

  // A backend's ready frame carries no engine field.
  await recorder.captureStarting(1, {platform: "twitch", channelId: "alpha"});
  await recorder.engineEvent(1, {type: "ready", session_id: "b-1"});
  assert.equal(sessions.get("s3").engine, "companion");
});
