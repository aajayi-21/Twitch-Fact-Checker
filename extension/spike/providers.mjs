/**
 * Spike S2 — controller page for the provider test.
 *
 * Answers: from inside the extension, does each provider return (a) a claim
 * list and (b) a web-grounded verdict, both as schema-valid JSON, and what
 * does each call cost in time, tokens and searches?
 *
 * For every provider it was given a key for, it runs — through an offscreen
 * document — a key check, a deliberately wrong key (to see the error
 * mapping), one gate call on the fixture transcript, and one verify call per
 * probe claim. Gemini's verify runs in both of its modes, to settle whether
 * search grounding and strict JSON work in a single call.
 *
 * Driven by scripts/run_extension_spike.mjs, which calls
 * `window.runProviderSpike(config)` and watches document.title:
 *   "providers:done"   window.__providerSpikeReport is ready
 *   "providers:error"  window.__providerSpikeReport.error says why
 *
 * `config.keys` holds real API keys. They are forwarded to the offscreen
 * document for the calls and appear nowhere else: not in the report, not in
 * the page, not in the console.
 */

import {PROVIDERS} from "../engine/providers/index.mjs";

const OFFSCREEN_URL = "spike/offscreen_providers.html";
const REFERENCE_URL = new URL("./assets/claims_reference.txt", import.meta.url);
const CALL_TIMEOUT_MS = 120_000;
const WRONG_KEY = "sk-this-key-is-deliberately-invalid";

// Claims with a known right answer, one per label the product can show.
const PROBE_CLAIMS = Object.freeze([
  {claim: "The Eiffel Tower is 450 meters tall.", expected: "FALSE"},
  {claim: "The speed of light is approximately 300,000 kilometers per second.", expected: "TRUE"},
  {
    claim: "Police are arresting protesters at the port of Dover right now.",
    expected: "UNVERIFIED",
  },
]);

// Substrings the gate should and should not extract from the fixture.
const EXPECTED_CLAIM_TOPICS = Object.freeze(["eiffel", "great wall", "ballon", "speed of light"]);
const EXCLUDED_CLAIM_TOPICS = Object.freeze(["patch", "left lane", "tournament", "follow", "code stream"]);

const channel = new BroadcastChannel("provider-spike");
const statusElement = document.getElementById("status");
const logElement = document.getElementById("log");
const setStatus = (text) => {
  statusElement.textContent = text;
  logElement.textContent += `${text}\n`;
};

let nextCallId = 1;

/**
 * Run one provider operation in the offscreen document.
 *
 * @returns {Promise<{ok: boolean, value?: any, error?: object, ms: number}>}
 */
const callOffscreen = (providerId, operation, args) =>
  new Promise((resolve) => {
    const id = nextCallId;
    nextCallId += 1;
    const timer = setTimeout(() => {
      channel.removeEventListener("message", onMessage);
      resolve({ok: false, error: {kind: "timeout", message: "no reply from the offscreen document"}, ms: CALL_TIMEOUT_MS});
    }, CALL_TIMEOUT_MS);
    const onMessage = (event) => {
      if (event.data?.type === "result" && event.data.id === id) {
        clearTimeout(timer);
        channel.removeEventListener("message", onMessage);
        resolve(event.data);
      }
    };
    channel.addEventListener("message", onMessage);
    channel.postMessage({type: "call", id, providerId, operation, args});
  });

const summarizeGate = (result) => {
  if (!result.ok) {
    return {ok: false, error: result.error, ms: result.ms};
  }
  const texts = result.value.claims.map((claim) => claim.claim_text.toLowerCase());
  return {
    ok: true,
    ms: result.ms,
    usage: result.value.usage,
    claims: result.value.claims,
    expectedFound: EXPECTED_CLAIM_TOPICS.filter((topic) => texts.some((text) => text.includes(topic))),
    excludedLeaked: EXCLUDED_CLAIM_TOPICS.filter((topic) => texts.some((text) => text.includes(topic))),
  };
};

const summarizeVerify = (probe, result, verifyMode) => {
  const base = {claim: probe.claim, expected: probe.expected, requestedMode: verifyMode, ms: result.ms};
  if (!result.ok) {
    return {...base, ok: false, error: result.error};
  }
  const {verdict, usage, mode} = result.value;
  return {
    ...base,
    ok: true,
    mode,
    label: verdict.label,
    matchesExpected: verdict.label === probe.expected,
    evidence: verdict.evidence,
    explanation: verdict.explanation,
    sources: verdict.sources,
    usage,
  };
};

const testProvider = async (provider, apiKey, models) => {
  const report = {
    providerId: provider.id,
    models: {gate: models?.gate ?? provider.defaultModels.gate, verify: models?.verify ?? provider.defaultModels.verify},
    hostPermissionGranted: await chrome.permissions.contains({origins: [provider.hostPermission]}),
  };

  setStatus(`${provider.label}: checking the key`);
  const valid = await callOffscreen(provider.id, "validateKey", {apiKey});
  report.validateKey = {ok: valid.ok, ms: valid.ms, error: valid.error ?? null};

  const wrong = await callOffscreen(provider.id, "validateKey", {apiKey: WRONG_KEY});
  report.wrongKey = {rejected: !wrong.ok, kind: wrong.error?.kind ?? null, status: wrong.error?.status ?? null};

  if (!valid.ok) {
    return report; // nothing else can work
  }

  setStatus(`${provider.label}: extracting claims`);
  const transcript = (await (await fetch(REFERENCE_URL)).text()).trim();
  report.gate = summarizeGate(
    await callOffscreen(provider.id, "runGate", {apiKey, context: "", transcript, model: report.models.gate})
  );

  // Gemini is the provider with a real question about single-call grounding.
  const verifyModes = provider.id === "gemini" ? ["strict", "prompted"] : [undefined];
  report.verify = [];
  for (const verifyMode of verifyModes) {
    for (const probe of PROBE_CLAIMS) {
      setStatus(`${provider.label}: checking "${probe.claim}"${verifyMode ? ` (${verifyMode})` : ""}`);
      const result = await callOffscreen(provider.id, "runVerify", {
        apiKey,
        claim: probe.claim,
        model: report.models.verify,
        ...(verifyMode ? {verifyMode} : {}),
      });
      report.verify.push(summarizeVerify(probe, result, verifyMode ?? null));
    }
  }
  return report;
};

/**
 * @param {object} config
 * @param {Record<string, string>} config.keys - providerId -> API key
 * @param {Record<string, {gate?: string, verify?: string}>} [config.models]
 */
window.runProviderSpike = async (config) => {
  try {
    if (await chrome.offscreen.hasDocument()) {
      await chrome.offscreen.closeDocument();
    }
    const ready = new Promise((resolve) => {
      const onMessage = (event) => {
        if (event.data?.type === "offscreen-ready") {
          channel.removeEventListener("message", onMessage);
          resolve();
        }
      };
      channel.addEventListener("message", onMessage);
    });
    await chrome.offscreen.createDocument({
      url: OFFSCREEN_URL,
      // "TESTING" needs a launch switch; any ordinary reason will do here.
      reasons: ["WORKERS"],
      justification: "Spike: call AI provider APIs from an offscreen document.",
    });
    await ready;

    const providers = [];
    for (const provider of Object.values(PROVIDERS)) {
      const apiKey = config.keys?.[provider.id];
      if (!apiKey) {
        providers.push({providerId: provider.id, skipped: "no key supplied"});
        continue;
      }
      providers.push(await testProvider(provider, apiKey, config.models?.[provider.id]));
    }
    await chrome.offscreen.closeDocument();

    window.__providerSpikeReport = {
      schema: 1,
      extensionId: chrome.runtime.id,
      userAgent: navigator.userAgent,
      timestamp: new Date().toISOString(),
      providers,
    };
    setStatus("finished");
    document.title = "providers:done";
  } catch (error) {
    // Never include `config` here: it holds the keys.
    console.error("[provider-spike] failed:", error?.message ?? error);
    window.__providerSpikeReport = {schema: 1, error: String(error?.message ?? error)};
    setStatus(`error: ${error?.message ?? error}`);
    document.title = "providers:error";
  }
};

document.title = "providers:ready";
