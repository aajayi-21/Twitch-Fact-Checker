/**
 * What this device has decided about transcribing speech in the browser,
 * and the rules for acting on it.
 *
 * One record, kept in chrome.storage.local (it describes THIS machine, so it
 * must not sync to the viewer's other devices):
 *
 *   settings    what the viewer chose: which engine, which model, which device
 *   results     per model, what the on-device self-test found
 *   gpuAttempt  crash guard: set just before the GPU is first used, cleared
 *               when that use returns. Still set at the next start means the
 *               browser died in between.
 *   gpuBlocked  the GPU is not to be used until the viewer re-runs the test
 *
 * The rule behind `results`: a configuration is used only if it PASSED the
 * self-test on this machine (see stt_plan.mjs) — never because the GPU
 * claims to support it.
 *
 * Pure (no browser APIs), so every decision here is unit-tested under Node.
 */

import {STT_VARIANTS, getSttModel} from "./models.mjs";

export const SPEECH_STORAGE_KEY = "speech";

export const SPEECH_ENGINE = Object.freeze({
  BACKEND: "backend", // the local backend transcribes (the default)
  BROWSER: "browser", // this extension transcribes, on this device
});

export const DEFAULT_SPEECH_SETTINGS = Object.freeze({
  engine: SPEECH_ENGINE.BACKEND,
  modelId: "moonshine-base",
  devicePreference: "auto", // auto | gpu | cpu
  powerPreference: "high-performance", // or "low-power", for dual-GPU laptops
});

/** Why the browser engine cannot be used right now (SpeechPlan.reason). */
export const NOT_READY = Object.freeze({
  NOT_TESTED: "not-tested", // the self-test has never run for this model
  STALE: "stale", // it ran under another browser version, runtime or setting
  NOTHING_PASSED: "nothing-passed", // no configuration works on this machine
  GPU_BLOCKED: "gpu-blocked", // the passing configuration needs the GPU, which is blocked
});

/**
 * @typedef {object} SpeechState
 * @property {typeof DEFAULT_SPEECH_SETTINGS} settings
 * @property {Record<string, SpeechResult>} results - by model id
 * @property {{startedAt: string, context: string, variantId: string}|null} gpuAttempt
 * @property {{at: string, reason: string}|null} gpuBlocked
 */

/**
 * @typedef {object} SpeechResult
 * @property {string} testedAt - ISO time
 * @property {string} environmentKey - what the test is valid for
 * @property {string} gpuLabel - e.g. "Intel (xe-lpg) — hardware"
 * @property {{variantId: string, device: "webgpu"|"wasm", realTimeFactor: number,
 *   wordErrorRate: number|null, numThreads: number}|null} selected
 * @property {{variantId: string, device: string, pass: boolean, reason: string|null,
 *   realTimeFactor: number|null, wordErrorRate: number|null, error: string|null}[]} runs
 * @property {{variantId: string, reason: string}[]} skipped
 */

/**
 * The stored record with every field present, whatever was (or was not) on
 * disk.
 *
 * @param {unknown} stored
 * @returns {SpeechState}
 */
export const normalizeSpeechState = (stored) => {
  const record = stored && typeof stored === "object" ? stored : {};
  const settings = {...DEFAULT_SPEECH_SETTINGS, ...(record.settings ?? {})};
  if (!Object.values(SPEECH_ENGINE).includes(settings.engine)) {
    settings.engine = DEFAULT_SPEECH_SETTINGS.engine;
  }
  if (!["auto", "gpu", "cpu"].includes(settings.devicePreference)) {
    settings.devicePreference = DEFAULT_SPEECH_SETTINGS.devicePreference;
  }
  if (!["high-performance", "low-power"].includes(settings.powerPreference)) {
    settings.powerPreference = DEFAULT_SPEECH_SETTINGS.powerPreference;
  }
  try {
    getSttModel(settings.modelId);
  } catch {
    settings.modelId = DEFAULT_SPEECH_SETTINGS.modelId;
  }
  return {
    settings,
    results: record.results && typeof record.results === "object" ? {...record.results} : {},
    gpuAttempt: record.gpuAttempt ?? null,
    gpuBlocked: record.gpuBlocked ?? null,
  };
};

/**
 * Everything a self-test result depends on. When any part changes, the
 * result no longer describes this machine and the test must run again.
 *
 * The GPU's identity is deliberately part of it: a different adapter (an
 * external GPU, a driver that now reports another name) is a different
 * machine as far as correctness goes.
 *
 * @param {object} parts
 * @param {import("./models.mjs").SttModel} parts.model
 * @param {string} parts.gpuCacheKey - from gpu_probe.mjs gpuCacheKey
 * @param {string|number} parts.browserMajor
 * @param {string} parts.runtimeVersion - the vendored speech runtime
 * @param {string} parts.devicePreference
 * @param {string} parts.powerPreference
 * @returns {string}
 */
export const environmentKey = ({
  model,
  gpuCacheKey,
  browserMajor,
  runtimeVersion,
  devicePreference,
  powerPreference,
}) =>
  [
    model.id,
    model.revision ?? "head",
    // The probe's own key uses "|" between its fields; keep this key's
    // separator unambiguous.
    `gpu=${String(gpuCacheKey).replaceAll("|", "/")}`,
    `browser=${browserMajor}`,
    `runtime=${runtimeVersion}`,
    `device=${devicePreference}`,
    `power=${powerPreference}`,
  ].join("|");

/**
 * The part of an environment key the service worker can check without
 * probing the GPU: everything except the adapter's identity.
 *
 * @param {string} key
 * @returns {string}
 */
export const environmentKeyWithoutGpu = (key) =>
  String(key ?? "")
    .split("|")
    .filter((part) => !part.startsWith("gpu="))
    .join("|");

/**
 * Reduce a benchmark report (benchmark.mjs benchmarkModel) to what is worth
 * keeping.
 *
 * @param {object} report
 * @param {string} key - environmentKey for this run
 * @param {string} testedAt - ISO time
 * @returns {SpeechResult}
 */
export const resultFromBenchmark = (report, key, testedAt) => {
  const winner = report.selected
    ? report.runs.find((run) => run.variantId === report.selected.variantId)
    : null;
  return {
    testedAt,
    environmentKey: key,
    gpuLabel: report.gpu?.label ?? "",
    selected: winner
      ? {
          variantId: winner.variantId,
          device: winner.device,
          realTimeFactor: winner.realTimeFactor,
          wordErrorRate: winner.wordErrorRate ?? null,
          numThreads: winner.numThreads ?? 1,
        }
      : null,
    runs: report.runs.map((run) => ({
      variantId: run.variantId,
      device: run.device,
      pass: Boolean(run.verdict?.pass),
      reason: run.verdict?.reason ?? null,
      realTimeFactor: Number.isFinite(run.realTimeFactor) ? run.realTimeFactor : null,
      wordErrorRate: Number.isFinite(run.wordErrorRate) ? run.wordErrorRate : null,
      error: run.ok ? null : String(run.error ?? "failed"),
    })),
    skipped: report.skipped ?? [],
  };
};

/**
 * @typedef {object} SpeechPlan
 * @property {boolean} ready
 * @property {string|null} reason - NOT_READY.* when not ready
 * @property {object|null} config - a TranscriberConfig plus what to show
 */

/**
 * Decide how a session starting now should transcribe in the browser.
 *
 * @param {SpeechState} state
 * @param {object} now
 * @param {string|number} now.browserMajor
 * @param {string} now.runtimeVersion
 * @returns {SpeechPlan}
 */
export const planBrowserSpeech = (state, {browserMajor, runtimeVersion}) => {
  const {settings} = state;
  const model = getSttModel(settings.modelId);
  const result = state.results[model.id];
  const notReady = (reason) => ({ready: false, reason, config: null});
  if (!result) {
    return notReady(NOT_READY.NOT_TESTED);
  }
  const expected = environmentKeyWithoutGpu(
    environmentKey({
      model,
      gpuCacheKey: "",
      browserMajor,
      runtimeVersion,
      devicePreference: settings.devicePreference,
      powerPreference: settings.powerPreference,
    })
  );
  if (environmentKeyWithoutGpu(result.environmentKey) !== expected) {
    return notReady(NOT_READY.STALE);
  }
  if (!result.selected) {
    return notReady(NOT_READY.NOTHING_PASSED);
  }
  if (result.selected.device === "webgpu" && (state.gpuBlocked || state.gpuAttempt)) {
    // A GPU configuration won the test, and the GPU has since crashed (or a
    // previous use of it never returned). Do not walk back into it.
    return notReady(NOT_READY.GPU_BLOCKED);
  }
  const variant = STT_VARIANTS[result.selected.variantId];
  return {
    ready: true,
    reason: null,
    config: {
      modelId: model.id,
      modelLabel: model.label,
      variantId: result.selected.variantId,
      repo: model.repo,
      revision: model.revision,
      device: variant.device,
      dtype: variant.dtype,
      numThreads: result.selected.numThreads,
      powerPreference: settings.powerPreference,
      maxSegmentS: model.maxSegmentS,
    },
  };
};

/**
 * The CPU configuration to drop to when the GPU fails in the middle of a
 * session. It has not necessarily passed the self-test — the ladder stops at
 * the first pass — so it is a best effort, announced as degraded.
 *
 * @param {object} config - the session's current config (planBrowserSpeech)
 * @returns {object|null} null when already on the CPU or the model has no
 *   CPU build
 */
export const cpuFallbackConfig = (config) => {
  if (config.device !== "webgpu") {
    return null;
  }
  const model = getSttModel(config.modelId);
  const variantId = model.variants.find((id) => STT_VARIANTS[id].device === "wasm");
  if (!variantId) {
    return null;
  }
  return {
    ...config,
    variantId,
    device: "wasm",
    dtype: STT_VARIANTS[variantId].dtype,
  };
};

/* ------------------------------------------------------------------ */
/* Crash guard                                                          */
/* ------------------------------------------------------------------ */

/**
 * Record that the GPU is about to be used.
 *
 * @param {SpeechState} state
 * @param {{context: "selftest"|"session", variantId: string, at: string}} attempt
 * @returns {SpeechState}
 */
export const beginGpuAttempt = (state, {context, variantId, at}) => ({
  ...state,
  gpuAttempt: {startedAt: at, context, variantId},
});

/**
 * The GPU use returned — whether it worked or failed cleanly, the browser
 * survived it.
 *
 * @param {SpeechState} state
 * @returns {SpeechState}
 */
export const endGpuAttempt = (state) => ({...state, gpuAttempt: null});

/**
 * Stop using the GPU until the viewer re-runs the test.
 *
 * @param {SpeechState} state
 * @param {{reason: string, at: string}} block
 * @returns {SpeechState}
 */
export const blockGpu = (state, {reason, at}) => ({
  ...state,
  gpuAttempt: null,
  gpuBlocked: {at, reason},
});

/**
 * Run at every start (of a session, or of the app's speech screen). A GPU
 * attempt that never ended means the browser — or its GPU process — died
 * while the GPU was in use; repeating it automatically risks a crash loop,
 * and Chrome withholds the GPU after a few.
 *
 * @param {SpeechState} state
 * @param {string} at - ISO time
 * @returns {{state: SpeechState, tripped: boolean}}
 */
export const applyCrashGuard = (state, at) => {
  if (!state.gpuAttempt) {
    return {state, tripped: false};
  }
  const {context, variantId} = state.gpuAttempt;
  return {
    state: blockGpu(state, {
      at,
      reason: `The browser closed unexpectedly while the GPU was in use (${context}, ${variantId}).`,
    }),
    tripped: true,
  };
};

/**
 * The viewer asked for a fresh test: forget the block and the old result
 * for this model.
 *
 * @param {SpeechState} state
 * @param {string} modelId
 * @returns {SpeechState}
 */
export const clearForRetest = (state, modelId) => {
  const results = {...state.results};
  delete results[modelId];
  return {...state, results, gpuAttempt: null, gpuBlocked: null};
};

/* ------------------------------------------------------------------ */
/* Wording                                                              */
/* ------------------------------------------------------------------ */

/**
 * "17× real time" from a real-time factor (processing time ÷ audio time).
 *
 * @param {number|null|undefined} realTimeFactor
 * @returns {string|null}
 */
export const speedLabel = (realTimeFactor) => {
  if (!Number.isFinite(realTimeFactor) || realTimeFactor <= 0) {
    return null;
  }
  const speed = 1 / realTimeFactor;
  return `${speed >= 10 ? Math.round(speed) : speed.toFixed(1)}× real time`;
};

/** @param {"webgpu"|"wasm"} device @returns {string} */
export const deviceLabel = (device) => (device === "webgpu" ? "GPU" : "CPU");

/**
 * One line saying where speech is transcribed, for Home and the popup.
 *
 * @param {SpeechState} state
 * @param {SpeechPlan} plan - from planBrowserSpeech
 * @returns {{state: "ok"|"warn"|"idle", text: string}}
 */
export const describeSpeechState = (state, plan) => {
  if (state.settings.engine !== SPEECH_ENGINE.BROWSER) {
    return {state: "idle", text: "Transcribed by the local backend"};
  }
  const model = getSttModel(state.settings.modelId);
  if (!plan.ready) {
    const why = {
      [NOT_READY.NOT_TESTED]: "needs its one-time setup",
      [NOT_READY.STALE]: "needs re-testing after an update",
      [NOT_READY.NOTHING_PASSED]: "cannot run on this device",
      [NOT_READY.GPU_BLOCKED]: "GPU switched off after a crash",
    }[plan.reason];
    return {state: "warn", text: `In this browser · ${model.label} · ${why}`};
  }
  const result = state.results[model.id];
  const speed = speedLabel(result.selected.realTimeFactor);
  return {
    state: "ok",
    text: [`In this browser`, model.label, deviceLabel(plan.config.device), speed]
      .filter(Boolean)
      .join(" · "),
  };
};
