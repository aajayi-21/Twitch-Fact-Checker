/**
 * Speech benchmark orchestrator.
 *
 * Runs one model through its ladder of configurations (stt_plan.mjs) on a
 * clip with a known transcript, one fresh worker per configuration, and
 * returns everything needed to decide — and to explain — how this machine
 * should transcribe: the GPU description, what was skipped and why, each
 * run's speed and accuracy, and the configuration that won.
 *
 * The same code path serves two purposes:
 *   - the first-run self-test in the onboarding wizard (short bundled clip);
 *   - the developer benchmark page (extension/spike/stt_bench.html), whose
 *     JSON report is how results are collected from other people's GPUs.
 */

import {TARGET_SAMPLE_RATE, splitOnSilence} from "./audio_utils.mjs";
import {describeGpu, gpuCacheKey, probeGpu} from "./gpu_probe.mjs";
import {STT_VARIANTS} from "./models.mjs";
import {DEFAULT_THRESHOLDS, buildLadder, evaluateRun, selectVariant} from "./stt_plan.mjs";
import {wordErrorRate} from "./wer.mjs";

const WORKER_URL = new URL("./bench_worker.mjs", import.meta.url);

// Generous: the first run of a large model includes its download.
const DEFAULT_RUN_TIMEOUT_MS = 10 * 60 * 1000;

// More threads stop helping (and start fighting the page) beyond a few cores.
const MAX_WASM_THREADS = 4;

/**
 * @typedef {object} BenchJob
 * @property {string} repo - Hugging Face model repository
 * @property {string} [revision] - exact commit to download
 * @property {"webgpu"|"wasm"} device
 * @property {string} dtype
 * @property {Float32Array[]} segments - 16 kHz mono clips
 * @property {number} numThreads - WASM threads (1 unless cross-origin isolated)
 * @property {"high-performance"|"low-power"} powerPreference
 * @property {boolean} forceFallbackAdapter
 * @property {boolean} useOwnDevice - hand ONNX Runtime the device this code
 *   opened (the production behaviour); false is a diagnostic A/B switch
 */

/**
 * Threads need SharedArrayBuffer, which needs a cross-origin isolated page.
 *
 * @returns {number}
 */
export const defaultThreadCount = () =>
  globalThis.crossOriginIsolated
    ? Math.max(1, Math.min(MAX_WASM_THREADS, navigator.hardwareConcurrency ?? 1))
    : 1;

/**
 * Run one job in a fresh worker. Resolves with the worker's result object;
 * a crash, an error event or a timeout resolves as `{ok: false, error}`.
 *
 * @param {BenchJob} job
 * @param {object} [options]
 * @param {(stage: string, detail: object) => void} [options.onProgress]
 * @param {number} [options.timeoutMs]
 * @returns {Promise<object>}
 */
export const runJobInWorker = (job, {onProgress, timeoutMs = DEFAULT_RUN_TIMEOUT_MS} = {}) =>
  new Promise((resolve) => {
    const worker = new Worker(WORKER_URL, {type: "module"});
    let settled = false;
    const finish = (result) => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timer);
      worker.terminate();
      resolve(result);
    };
    const timer = setTimeout(
      () => finish({ok: false, error: `timed out after ${Math.round(timeoutMs / 1000)} s`}),
      timeoutMs
    );
    worker.addEventListener("message", (event) => {
      if (event.data?.type === "progress") {
        onProgress?.(event.data.stage, event.data.detail);
      } else if (event.data?.type === "result") {
        finish(event.data.result);
      }
    });
    worker.addEventListener("error", (event) => {
      finish({ok: false, error: `worker error: ${event.message || "failed to start"}`});
    });
    worker.postMessage({type: "run", job});
  });

/**
 * @typedef {object} BenchmarkOptions
 * @property {string} [referenceText] - known transcript; without it runs are
 *   timed but cannot be checked for correctness
 * @property {"high-performance"|"low-power"} [powerPreference]
 * @property {boolean} [forceFallbackAdapter] - testing: use the software GPU
 * @property {boolean} [allowSoftwareGpu] - testing: benchmark WebGPU on it
 * @property {"auto"|"gpu"|"cpu"} [devicePreference] - the viewer's device setting
 * @property {boolean} [useOwnDevice] - diagnostic: false lets the runtime
 *   create its own GPU device
 * @property {string[]} [onlyVariants] - restrict to these variant ids
 * @property {boolean} [stopAtFirstPass] - production behaviour: stop climbing
 *   down the ladder once a configuration passes
 * @property {number} [numThreads]
 * @property {number} [timeoutMs]
 * @property {object} [thresholds]
 * @property {(update: object) => void} [onProgress]
 */

/**
 * Benchmark one model on this machine.
 *
 * @param {import("./models.mjs").SttModel} model
 * @param {Float32Array} samples - 16 kHz mono audio
 * @param {BenchmarkOptions} [options]
 * @returns {Promise<object>} the model report (JSON-serialisable)
 */
export const benchmarkModel = async (
  model,
  samples,
  {
    referenceText = null,
    powerPreference = "high-performance",
    forceFallbackAdapter = false,
    allowSoftwareGpu = false,
    devicePreference = "auto",
    useOwnDevice = true,
    onlyVariants = null,
    stopAtFirstPass = false,
    numThreads = defaultThreadCount(),
    timeoutMs = DEFAULT_RUN_TIMEOUT_MS,
    thresholds = DEFAULT_THRESHOLDS,
    onProgress,
  } = {}
) => {
  const probe = await probeGpu({powerPreference, forceFallbackAdapter});
  const ladder = buildLadder(model, probe, {allowSoftwareGpu, devicePreference});
  const rungs = onlyVariants
    ? ladder.rungs.filter((rung) => onlyVariants.includes(rung.variantId))
    : ladder.rungs;

  const ranges = splitOnSilence(samples, {maxSegmentS: model.maxSegmentS});
  if (ranges.length === 0) {
    throw new Error("The benchmark clip contains no speech.");
  }

  const runs = [];
  for (const rung of rungs) {
    // Fresh copies per run: the worker receives its own buffers.
    const segments = ranges.map((range) => samples.slice(range.start, range.end));
    const result = await runJobInWorker(
      {
        repo: model.repo,
        revision: model.revision,
        device: rung.device,
        dtype: rung.dtype,
        segments,
        numThreads,
        powerPreference,
        forceFallbackAdapter,
        useOwnDevice,
      },
      {
        timeoutMs,
        onProgress: (stage, detail) =>
          onProgress?.({modelId: model.id, variantId: rung.variantId, stage, detail}),
      }
    );
    const run = {
      variantId: rung.variantId,
      device: rung.device,
      dtype: rung.dtype,
      downloadMb: model.downloadMb[rung.variantId] ?? null,
      wordErrorRate:
        result.ok && referenceText !== null ? wordErrorRate(referenceText, result.text) : null,
      ...result,
    };
    // Without a reference there is nothing to check correctness against, so
    // judge speed only.
    run.verdict =
      referenceText === null
        ? evaluateRun({...run, wordErrorRate: 0}, thresholds)
        : evaluateRun(run, thresholds);
    runs.push(run);
    onProgress?.({modelId: model.id, variantId: rung.variantId, stage: "done", detail: run});
    if (stopAtFirstPass && run.verdict.pass) {
      break;
    }
  }

  const judgedRuns =
    referenceText === null ? runs.map((run) => ({...run, wordErrorRate: 0})) : runs;
  const selected = selectVariant(rungs, judgedRuns, thresholds);
  return {
    modelId: model.id,
    repo: model.repo,
    gpu: {...probe, label: describeGpu(probe), cacheKey: gpuCacheKey(probe)},
    skipped: ladder.skipped,
    audio: {
      seconds: samples.length / TARGET_SAMPLE_RATE,
      segments: ranges.length,
      hasReference: referenceText !== null,
    },
    thresholds,
    runs,
    selected: selected && {...selected, ...STT_VARIANTS[selected.variantId]},
  };
};

/**
 * Environment facts that explain a report when it is read on another machine.
 *
 * @returns {object}
 */
export const describeEnvironment = () => ({
  userAgent: navigator.userAgent,
  platform: navigator.userAgentData?.platform ?? navigator.platform ?? "",
  hardwareConcurrency: navigator.hardwareConcurrency ?? null,
  deviceMemoryGb: navigator.deviceMemory ?? null,
  crossOriginIsolated: globalThis.crossOriginIsolated === true,
  origin: location.protocol,
  timestamp: new Date().toISOString(),
});
