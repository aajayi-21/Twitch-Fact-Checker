/**
 * Unit tests for in-browser speech in a real session: what this device has
 * decided (speech_setup.mjs) and how a session recovers when the GPU lets it
 * down (session_speech.mjs).
 *
 * Run from the repository root:
 *
 *     node --test "extension/tests/*.test.mjs"
 */

import assert from "node:assert/strict";
import {test} from "node:test";

import {SPEECH_TIERS, getSttModel} from "../engine/stt/models.mjs";
import {SPEECH_STATUS, SessionSpeech} from "../engine/stt/session_speech.mjs";
import {
  DEFAULT_SPEECH_SETTINGS,
  NOT_READY,
  SPEECH_ENGINE,
  applyCrashGuard,
  beginGpuAttempt,
  blockGpu,
  clearForRetest,
  cpuFallbackConfig,
  describeSpeechState,
  endGpuAttempt,
  environmentKey,
  environmentKeyWithoutGpu,
  normalizeSpeechState,
  planBrowserSpeech,
  resultFromBenchmark,
  speedLabel,
} from "../engine/stt/speech_setup.mjs";

const NOW = {browserMajor: "157", runtimeVersion: "transformers@4.3.2;ort@1.31.0"};
const GPU_KEY = "hardware|intel|xe-lpg|0x7d55|Intel Arc|f16|high-performance";

const keyFor = (modelId, overrides = {}) =>
  environmentKey({
    model: getSttModel(modelId),
    gpuCacheKey: GPU_KEY,
    devicePreference: "auto",
    powerPreference: "high-performance",
    ...NOW,
    ...overrides,
  });

/** A stored result as the self-test would leave it. */
const passingResult = (modelId, variantId, device, overrides = {}) => ({
  testedAt: "2026-11-12T20:00:00Z",
  environmentKey: keyFor(modelId),
  gpuLabel: "Intel Arc (xe-lpg)",
  selected: {variantId, device, realTimeFactor: 0.058, wordErrorRate: 0, numThreads: 1},
  runs: [{variantId, device, pass: true, reason: null, realTimeFactor: 0.058, wordErrorRate: 0, error: null}],
  skipped: [],
  ...overrides,
});

const browserState = (modelId, result, extra = {}) =>
  normalizeSpeechState({
    settings: {engine: SPEECH_ENGINE.BROWSER, modelId},
    results: result ? {[modelId]: result} : {},
    ...extra,
  });

/* ------------------------------------------------------------------ */
/* The stored record                                                    */
/* ------------------------------------------------------------------ */

test("a missing or damaged record becomes safe defaults", () => {
  for (const stored of [undefined, null, "junk", 7, {}]) {
    assert.deepEqual(normalizeSpeechState(stored), {
      settings: {...DEFAULT_SPEECH_SETTINGS},
      results: {},
      gpuAttempt: null,
      gpuBlocked: null,
    });
  }
  // The default is the engine every existing user already has.
  assert.equal(DEFAULT_SPEECH_SETTINGS.engine, SPEECH_ENGINE.BACKEND);
});

test("unknown choices fall back one by one, keeping the valid ones", () => {
  const state = normalizeSpeechState({
    settings: {
      engine: "cloud",
      modelId: "whisper-enormous",
      devicePreference: "quantum",
      powerPreference: "low-power",
    },
  });
  assert.equal(state.settings.engine, SPEECH_ENGINE.BACKEND);
  assert.equal(state.settings.modelId, DEFAULT_SPEECH_SETTINGS.modelId);
  assert.equal(state.settings.devicePreference, "auto");
  assert.equal(state.settings.powerPreference, "low-power");
});

test("every tier offered to viewers names a pinned model", () => {
  assert.deepEqual(SPEECH_TIERS.map((tier) => tier.tier), ["fast", "balanced", "accurate"]);
  for (const tier of SPEECH_TIERS) {
    const model = getSttModel(tier.modelId);
    // Weights are downloaded at first use; an unpinned model could change
    // under a tested configuration.
    assert.match(model.revision ?? "", /^[0-9a-f]{40}$/, `${model.id} must pin a commit`);
  }
});

/* ------------------------------------------------------------------ */
/* What a result is valid for                                           */
/* ------------------------------------------------------------------ */

test("the environment key changes with anything that could change the outcome", () => {
  const base = keyFor("moonshine-base");
  assert.notEqual(base, keyFor("moonshine-base", {browserMajor: "158"}));
  assert.notEqual(base, keyFor("moonshine-base", {runtimeVersion: "transformers@4.4.0"}));
  assert.notEqual(base, keyFor("moonshine-base", {devicePreference: "cpu"}));
  assert.notEqual(base, keyFor("moonshine-base", {powerPreference: "low-power"}));
  assert.notEqual(base, keyFor("moonshine-base", {gpuCacheKey: "hardware|nvidia|x|y|RTX|f16|hp"}));
  assert.notEqual(base, keyFor("whisper-base.en"));
  assert.equal(base, keyFor("moonshine-base"));
});

test("the GPU part can be left out without disturbing the rest", () => {
  const withIntel = environmentKeyWithoutGpu(keyFor("moonshine-base"));
  const withNvidia = environmentKeyWithoutGpu(
    keyFor("moonshine-base", {gpuCacheKey: "hardware|nvidia|x|y|RTX 4070|f16|hp"})
  );
  assert.equal(withIntel, withNvidia);
  // The probe's own key contains "|": none of its fields may leak through.
  assert.ok(!withIntel.includes("intel") && !withIntel.includes("xe-lpg"));
  assert.notEqual(withIntel, environmentKeyWithoutGpu(keyFor("moonshine-base", {browserMajor: "158"})));
});

/* ------------------------------------------------------------------ */
/* Planning a session                                                   */
/* ------------------------------------------------------------------ */

test("plan: a model that passed its test is ready, with its exact configuration", () => {
  const state = browserState("moonshine-base", passingResult("moonshine-base", "wasm-q8", "wasm"));
  const plan = planBrowserSpeech(state, NOW);
  assert.equal(plan.ready, true);
  assert.deepEqual(plan.config, {
    modelId: "moonshine-base",
    modelLabel: "Moonshine Base",
    variantId: "wasm-q8",
    repo: "onnx-community/moonshine-base-ONNX",
    revision: getSttModel("moonshine-base").revision,
    device: "wasm",
    dtype: "q8",
    numThreads: 1,
    powerPreference: "high-performance",
    maxSegmentS: 10,
  });
});

test("plan: a GPU winner carries the per-file precision map", () => {
  const state = browserState(
    "whisper-base.en",
    passingResult("whisper-base.en", "webgpu-mixed", "webgpu")
  );
  const {config} = planBrowserSpeech(state, NOW);
  assert.equal(config.device, "webgpu");
  assert.deepEqual({...config.dtype}, {encoder_model: "fp16", decoder_model_merged: "fp32"});
});

test("plan: never tested is not ready", () => {
  const plan = planBrowserSpeech(browserState("moonshine-base", null), NOW);
  assert.deepEqual(plan, {ready: false, reason: NOT_READY.NOT_TESTED, config: null});
});

test("plan: a test for another model does not count", () => {
  const state = normalizeSpeechState({
    settings: {engine: SPEECH_ENGINE.BROWSER, modelId: "whisper-base.en"},
    results: {"moonshine-base": passingResult("moonshine-base", "wasm-q8", "wasm")},
  });
  assert.equal(planBrowserSpeech(state, NOW).reason, NOT_READY.NOT_TESTED);
});

test("plan: a browser or runtime update makes the result stale", () => {
  const state = browserState("moonshine-base", passingResult("moonshine-base", "wasm-q8", "wasm"));
  assert.equal(planBrowserSpeech(state, {...NOW, browserMajor: "158"}).reason, NOT_READY.STALE);
  assert.equal(
    planBrowserSpeech(state, {...NOW, runtimeVersion: "missing"}).reason,
    NOT_READY.STALE
  );
});

test("plan: changing the device or GPU preference makes the result stale", () => {
  const result = passingResult("moonshine-base", "wasm-q8", "wasm");
  const cpuOnly = normalizeSpeechState({
    settings: {engine: SPEECH_ENGINE.BROWSER, modelId: "moonshine-base", devicePreference: "cpu"},
    results: {"moonshine-base": result},
  });
  assert.equal(planBrowserSpeech(cpuOnly, NOW).reason, NOT_READY.STALE);
});

test("plan: a test in which nothing passed is not ready", () => {
  const state = browserState(
    "whisper-base.en",
    passingResult("whisper-base.en", "webgpu-mixed", "webgpu", {selected: null})
  );
  assert.equal(planBrowserSpeech(state, NOW).reason, NOT_READY.NOTHING_PASSED);
});

test("plan: a blocked GPU stops a GPU configuration, not a CPU one", () => {
  const blocked = {gpuBlocked: {at: "2026-11-12T21:00:00Z", reason: "device lost"}};
  const onGpu = browserState(
    "whisper-base.en",
    passingResult("whisper-base.en", "webgpu-mixed", "webgpu"),
    blocked
  );
  assert.equal(planBrowserSpeech(onGpu, NOW).reason, NOT_READY.GPU_BLOCKED);
  const onCpu = browserState(
    "moonshine-base",
    passingResult("moonshine-base", "wasm-q8", "wasm"),
    blocked
  );
  assert.equal(planBrowserSpeech(onCpu, NOW).ready, true);
});

test("plan: an unfinished GPU attempt is treated like a block", () => {
  const state = browserState(
    "whisper-base.en",
    passingResult("whisper-base.en", "webgpu-mixed", "webgpu"),
    {gpuAttempt: {startedAt: "2026-11-12T21:00:00Z", context: "session", variantId: "webgpu-mixed"}}
  );
  assert.equal(planBrowserSpeech(state, NOW).reason, NOT_READY.GPU_BLOCKED);
});

test("cpuFallbackConfig: the model's CPU build, or nothing when already on the CPU", () => {
  const gpu = planBrowserSpeech(
    browserState("whisper-base.en", passingResult("whisper-base.en", "webgpu-mixed", "webgpu")),
    NOW
  ).config;
  const fallback = cpuFallbackConfig(gpu);
  assert.equal(fallback.device, "wasm");
  assert.equal(fallback.variantId, "wasm-q8");
  assert.equal(fallback.dtype, "q8");
  assert.equal(fallback.repo, gpu.repo);
  assert.equal(fallback.revision, gpu.revision);
  assert.equal(cpuFallbackConfig(fallback), null);
});

/* ------------------------------------------------------------------ */
/* Storing a test                                                       */
/* ------------------------------------------------------------------ */

test("resultFromBenchmark keeps the winner and why the others lost", () => {
  const report = {
    gpu: {label: "Intel Arc (xe-lpg)"},
    skipped: [{variantId: "webgpu-q4f16", reason: "gpu-lacks-shader-f16"}],
    selected: {variantId: "webgpu-fp32", device: "webgpu", dtype: "fp32"},
    runs: [
      {variantId: "webgpu-mixed", device: "webgpu", ok: true, realTimeFactor: 0.1, wordErrorRate: 1.4,
        verdict: {pass: false, reason: "wrong-output"}},
      {variantId: "webgpu-fp32", device: "webgpu", ok: true, realTimeFactor: 0.21, wordErrorRate: 0.05,
        numThreads: 1, verdict: {pass: true, reason: null}},
      {variantId: "wasm-q8", device: "wasm", ok: false, error: "timed out after 600 s",
        verdict: {pass: false, reason: "error"}},
    ],
  };
  const result = resultFromBenchmark(report, "the-key", "2026-11-12T20:00:00Z");
  assert.equal(result.environmentKey, "the-key");
  assert.deepEqual(result.selected, {
    variantId: "webgpu-fp32", device: "webgpu", realTimeFactor: 0.21, wordErrorRate: 0.05, numThreads: 1,
  });
  assert.deepEqual(result.runs.map((run) => [run.variantId, run.pass, run.reason, run.error]), [
    ["webgpu-mixed", false, "wrong-output", null],
    ["webgpu-fp32", true, null, null],
    ["wasm-q8", false, "error", "timed out after 600 s"],
  ]);
  assert.equal(result.skipped.length, 1);
  assert.equal(resultFromBenchmark({...report, selected: null}, "k", "t").selected, null);
});

/* ------------------------------------------------------------------ */
/* Crash guard                                                          */
/* ------------------------------------------------------------------ */

test("crash guard: an attempt that began and ended leaves no trace", () => {
  const begun = beginGpuAttempt(normalizeSpeechState(null), {
    context: "session", variantId: "webgpu-mixed", at: "2026-11-12T21:00:00Z",
  });
  assert.deepEqual(begun.gpuAttempt, {
    startedAt: "2026-11-12T21:00:00Z", context: "session", variantId: "webgpu-mixed",
  });
  const ended = endGpuAttempt(begun);
  assert.equal(ended.gpuAttempt, null);
  assert.deepEqual(applyCrashGuard(ended, "2026-11-12T22:00:00Z"), {state: ended, tripped: false});
});

test("crash guard: an attempt still open at the next start blocks the GPU", () => {
  const begun = beginGpuAttempt(normalizeSpeechState(null), {
    context: "selftest", variantId: "whisper-base.en", at: "2026-11-12T21:00:00Z",
  });
  const {state, tripped} = applyCrashGuard(begun, "2026-11-12T22:00:00Z");
  assert.equal(tripped, true);
  assert.equal(state.gpuAttempt, null);
  assert.equal(state.gpuBlocked.at, "2026-11-12T22:00:00Z");
  assert.match(state.gpuBlocked.reason, /closed unexpectedly/);
  // Applying it again changes nothing.
  assert.equal(applyCrashGuard(state, "2026-11-12T23:00:00Z").tripped, false);
});

test("re-testing lifts the block and forgets only that model's result", () => {
  const state = {
    ...normalizeSpeechState(null),
    results: {
      "moonshine-base": passingResult("moonshine-base", "wasm-q8", "wasm"),
      "whisper-base.en": passingResult("whisper-base.en", "webgpu-mixed", "webgpu"),
    },
    gpuBlocked: {at: "t", reason: "device lost"},
  };
  const cleared = clearForRetest(state, "whisper-base.en");
  assert.equal(cleared.gpuBlocked, null);
  assert.deepEqual(Object.keys(cleared.results), ["moonshine-base"]);
  assert.equal(blockGpu(cleared, {at: "t2", reason: "again"}).gpuBlocked.reason, "again");
});

/* ------------------------------------------------------------------ */
/* Wording                                                              */
/* ------------------------------------------------------------------ */

test("speedLabel turns a real-time factor into a multiple", () => {
  assert.equal(speedLabel(0.058), "17× real time");
  assert.equal(speedLabel(0.21), "4.8× real time");
  assert.equal(speedLabel(0), null);
  assert.equal(speedLabel(undefined), null);
});

test("describeSpeechState: one line for Home", () => {
  const backend = normalizeSpeechState(null);
  assert.deepEqual(describeSpeechState(backend, planBrowserSpeech(backend, NOW)), {
    state: "idle",
    text: "Transcribed by the local backend",
  });

  const ready = browserState("moonshine-base", passingResult("moonshine-base", "wasm-q8", "wasm"));
  assert.deepEqual(describeSpeechState(ready, planBrowserSpeech(ready, NOW)), {
    state: "ok",
    text: "In this browser · Moonshine Base · CPU · 17× real time",
  });

  const untested = browserState("whisper-base.en", null);
  assert.deepEqual(describeSpeechState(untested, planBrowserSpeech(untested, NOW)), {
    state: "warn",
    text: "In this browser · Whisper Base (English) · needs its one-time setup",
  });
});

/* ------------------------------------------------------------------ */
/* Session speech: loading, recovery, crash-guard calls                 */
/* ------------------------------------------------------------------ */

const GPU_CONFIG = Object.freeze(
  planBrowserSpeech(
    browserState("whisper-base.en", passingResult("whisper-base.en", "webgpu-mixed", "webgpu")),
    NOW
  ).config
);
const CPU_CONFIG = Object.freeze(
  planBrowserSpeech(
    browserState("moonshine-base", passingResult("moonshine-base", "wasm-q8", "wasm")),
    NOW
  ).config
);

/**
 * A harness around SessionSpeech with scripted transcribers.
 * `loadOutcomes` gives, per transcriber created, "ok" or an error message.
 */
const makeSession = (config, loadOutcomes = ["ok"]) => {
  const log = [];
  const transcribers = [];
  const session = new SessionSpeech({
    config,
    createTranscriber: (callbacks, options) => {
      const index = transcribers.length;
      const transcriber = {
        callbacks,
        options,
        loadedWith: null,
        frames: 0,
        disposed: false,
        stats: {segmentsDropped: 0, droppedSeconds: 0, segmentsTranscribed: 0},
        load: async (loadConfig) => {
          transcriber.loadedWith = loadConfig;
          const outcome = loadOutcomes[index] ?? "ok";
          if (outcome !== "ok") {
            throw new Error(outcome);
          }
          return {};
        },
        pushPcm16: () => {
          transcriber.frames += 1;
        },
        finish: async () => {},
        dispose: () => {
          transcriber.disposed = true;
        },
      };
      transcribers.push(transcriber);
      return transcriber;
    },
    onTranscript: (transcript) => log.push(["transcript", transcript.text]),
    onStatus: (status) => log.push(["status", status.status, status.device]),
    onGpuAttempt: async (attempt) => void log.push(["gpu", attempt.phase]),
    onGpuFailed: (reason) => log.push(["gpu-failed", reason]),
    onFailed: (reason) => log.push(["failed", reason]),
  });
  return {session, log, transcribers};
};

test("session: a CPU model loads without touching the crash guard", async () => {
  const {session, log, transcribers} = makeSession(CPU_CONFIG);
  await session.start();
  assert.deepEqual(log, [["status", "loading", "wasm"], ["status", "ready", "wasm"]]);
  assert.deepEqual(transcribers[0].loadedWith, {
    repo: CPU_CONFIG.repo,
    revision: CPU_CONFIG.revision,
    device: "wasm",
    dtype: "q8",
    numThreads: 1,
    powerPreference: "high-performance",
  });
  assert.deepEqual(transcribers[0].options, {segmenter: {maxSegmentS: 10}});
});

test("session: a GPU model brackets its load with the crash-guard marker", async () => {
  const {session, log} = makeSession(GPU_CONFIG);
  await session.start();
  assert.deepEqual(log, [
    ["status", "loading", "webgpu"],
    ["gpu", "begin"],
    ["gpu", "end"],
    ["status", "ready", "webgpu"],
  ]);
});

test("session: the marker is stored before the transcriber even exists", async () => {
  const {session, transcribers} = makeSession(GPU_CONFIG);
  let createdWhenBeginReported = null;
  session.onGpuAttempt = async ({phase}) => {
    if (phase === "begin") {
      createdWhenBeginReported = transcribers.length;
    }
  };
  await session.start();
  assert.equal(createdWhenBeginReported, 0);
});

test("session: a GPU load failure falls back to the CPU and reports the GPU", async () => {
  const {session, log, transcribers} = makeSession(GPU_CONFIG, ["out of memory", "ok"]);
  await session.start();
  assert.deepEqual(log.map((entry) => entry.slice(0, 3)), [
    ["status", "loading", "webgpu"],
    ["gpu", "begin"],
    // It failed cleanly: the browser survived, so the marker is cleared.
    ["gpu", "end"],
    ["gpu-failed", "The speech model could not be loaded: out of memory"],
    ["status", "loading", "wasm"],
    ["status", "degraded", "wasm"],
  ]);
  assert.equal(transcribers[0].disposed, true);
  assert.equal(transcribers[1].loadedWith.device, "wasm");
  assert.equal(session.status, SPEECH_STATUS.DEGRADED);
});

test("session: a GPU lost mid-session moves to the CPU and keeps transcribing", async () => {
  const {session, log, transcribers} = makeSession(GPU_CONFIG);
  await session.start();
  transcribers[0].callbacks.onTranscript({text: "before"});
  transcribers[0].callbacks.onFatal("GPU device lost (destroyed)");
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(session.status, SPEECH_STATUS.DEGRADED);
  assert.ok(log.some((entry) => entry[0] === "gpu-failed"));
  // The old transcriber is retired: a late result from it is ignored.
  transcribers[0].callbacks.onTranscript({text: "late"});
  transcribers[1].callbacks.onTranscript({text: "after"});
  assert.deepEqual(
    log.filter((entry) => entry[0] === "transcript").map((entry) => entry[1]),
    ["before", "after"]
  );
  session.pushFrame(new ArrayBuffer(8));
  assert.equal(transcribers[1].frames, 1);
});

test("session: when the CPU fails too, the session is told once and stops", async () => {
  const {session, log, transcribers} = makeSession(GPU_CONFIG, ["no gpu", "no wasm either"]);
  await session.start();
  assert.equal(session.status, SPEECH_STATUS.FAILED);
  assert.equal(log.filter((entry) => entry[0] === "failed").length, 1);
  assert.equal(transcribers.length, 2);
  session.pushFrame(new ArrayBuffer(8)); // ignored, not an error
  await session.finish(10); // nothing to wait for
});

test("session: a CPU-only model that fails has nowhere to fall", async () => {
  const {session, log} = makeSession(CPU_CONFIG, ["corrupt download"]);
  await session.start();
  assert.equal(session.status, SPEECH_STATUS.FAILED);
  assert.ok(!log.some((entry) => entry[0] === "gpu-failed"), "the GPU was never involved");
  assert.equal(log.at(-1)[0], "failed");
});

test("session: frames pushed while loading reach the transcriber", async () => {
  const {session, transcribers} = makeSession(CPU_CONFIG);
  const started = session.start();
  await Promise.resolve();
  session.pushFrame(new ArrayBuffer(8));
  session.pushFrame(new ArrayBuffer(8));
  await started;
  assert.equal(transcribers[0].frames, 2);
});

test("session: disposing during a load abandons it quietly", async () => {
  const {session, log, transcribers} = makeSession(CPU_CONFIG);
  const started = session.start();
  session.dispose();
  await started;
  assert.equal(transcribers[0]?.disposed ?? true, true);
  assert.ok(!log.some((entry) => entry[1] === "ready"));
  session.dispose(); // twice is fine
});

test("session: finish waits for the model, but not past its timeout", async () => {
  const {session, transcribers} = makeSession(CPU_CONFIG);
  await session.start();
  transcribers[0].finish = () => new Promise(() => {}); // never resolves
  const startedAt = Date.now();
  await session.finish(30);
  assert.ok(Date.now() - startedAt < 500);
});

test("session: dropped audio is totalled across a fallback", async () => {
  const {session, transcribers} = makeSession(GPU_CONFIG);
  await session.start();
  transcribers[0].stats = {segmentsDropped: 2, droppedSeconds: 9.5, segmentsTranscribed: 4};
  transcribers[0].callbacks.onFatal("GPU device lost");
  await new Promise((resolve) => setTimeout(resolve, 0));
  transcribers[1].stats = {segmentsDropped: 1, droppedSeconds: 3, segmentsTranscribed: 6};
  assert.deepEqual(session.stats, {segmentsDropped: 3, droppedSeconds: 12.5, segmentsTranscribed: 10});
});
