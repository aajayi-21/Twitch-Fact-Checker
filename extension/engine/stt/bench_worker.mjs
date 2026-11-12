/**
 * Benchmark worker: loads ONE speech model in ONE configuration, transcribes
 * the given audio segments, and reports timings.
 *
 * It runs in a dedicated module Worker — the same kind of context the
 * production speech engine will use inside the offscreen document — and the
 * orchestrator (benchmark.mjs) starts a fresh worker per configuration, so
 * every run begins with a clean WebAssembly heap and a clean GPU device, and
 * a crash or hang in one configuration cannot poison the next.
 *
 * Protocol:
 *   in   {type: "run", job}                      (see BenchJob in benchmark.mjs)
 *   out  {type: "progress", stage, detail}       any number of times
 *   out  {type: "result", result}                exactly once; never throws
 */

import {env, pipeline} from "../../vendor/transformers/transformers.js";

import {openGpuDevice} from "./gpu_probe.mjs";

const RUNTIME_DIR = new URL("../../vendor/transformers/", import.meta.url);

// Manifest V3: all executable code ships in the package. Point ONNX Runtime
// at the vendored WebAssembly module instead of its default CDN, and turn off
// the library's WASM cache — it re-serves the loader from a blob: URL, which
// the extension's `script-src 'self'` policy would block.
env.allowLocalModels = false;
env.useWasmCache = false;
env.backends.onnx.wasm.wasmPaths = {
  mjs: new URL("ort-wasm-simd-threaded.asyncify.mjs", RUNTIME_DIR).href,
  wasm: new URL("ort-wasm-simd-threaded.asyncify.wasm", RUNTIME_DIR).href,
};

const SAMPLE_RATE = 16000;

/** @param {string} stage @param {object} [detail] */
const reportProgress = (stage, detail = {}) => {
  self.postMessage({type: "progress", stage, detail});
};

/**
 * Collapse the library's per-file download events into one overall fraction.
 *
 * @returns {(event: object) => void}
 */
const makeDownloadReporter = () => {
  const files = new Map();
  let lastReportedPercent = -1;
  return (event) => {
    if (event.status !== "progress" || !event.file || !event.total) {
      return;
    }
    files.set(event.file, {loaded: event.loaded, total: event.total});
    let loaded = 0;
    let total = 0;
    for (const file of files.values()) {
      loaded += file.loaded;
      total += file.total;
    }
    const percent = Math.floor((loaded / total) * 100);
    if (percent !== lastReportedPercent) {
      lastReportedPercent = percent;
      reportProgress("download", {percent, loadedMb: loaded / 1e6, totalMb: total / 1e6});
    }
  };
};

/**
 * Identity of the adapter behind a device, as the device itself reports it —
 * recorded so a report can prove which GPU a run really used.
 *
 * @param {GPUDevice} device
 * @returns {object}
 */
const describeSessionGpu = (device) => {
  const info = device.adapterInfo ?? {};
  return {
    vendor: info.vendor ?? "",
    architecture: info.architecture ?? "",
    description: info.description ?? "",
    isFallbackAdapter: info.isFallbackAdapter === true,
    shaderF16: device.features?.has("shader-f16") ?? false,
  };
};

/**
 * @param {import("./benchmark.mjs").BenchJob} job
 * @returns {Promise<object>} the run result (ok: true)
 */
const runJob = async (job) => {
  env.backends.onnx.wasm.numThreads = job.numThreads;

  // On the GPU path this worker opens the device itself and hands it to ONNX
  // Runtime through the execution-provider options. Left to itself the runtime
  // picks its own adapter and ignores `forceFallbackAdapter`, so the GPU that
  // was probed and the GPU that ran the model could differ.
  let gpuDevice = null;
  let workerProbe = null;
  let deviceLost = null;
  const pipelineOptions = {
    device: job.device,
    dtype: job.dtype,
    progress_callback: makeDownloadReporter(),
  };
  if (job.device === "webgpu") {
    reportProgress("probe");
    const opened = await openGpuDevice({
      powerPreference: job.powerPreference,
      forceFallbackAdapter: job.forceFallbackAdapter,
    });
    workerProbe = opened.probe;
    gpuDevice = opened.device;
    if (!gpuDevice) {
      throw new Error(
        `No GPU device (${workerProbe.reason}${workerProbe.error ? `: ${workerProbe.error}` : ""})`
      );
    }
    if (job.useOwnDevice) {
      gpuDevice.lost.then((lostInfo) => {
        deviceLost = `${lostInfo.reason || "unknown"}: ${lostInfo.message || ""}`.trim();
      });
      pipelineOptions.session_options = {
        executionProviders: [{name: "webgpu", device: gpuDevice}],
      };
    } else {
      // Diagnostic A/B only: let ONNX Runtime create its own device.
      gpuDevice.destroy();
      gpuDevice = null;
    }
  }

  reportProgress("load");
  const loadStartedAt = performance.now();
  const transcriber = await pipeline("automatic-speech-recognition", job.repo, pipelineOptions);
  const loadMs = performance.now() - loadStartedAt;

  // First inference pays one-off costs (WebGPU shader compilation, WASM JIT
  // warm-up) that a live session pays once, not per utterance. Time it
  // separately so it does not distort the steady-state real-time factor.
  reportProgress("warmup");
  const warmupSamples = job.segments[0].slice(0, SAMPLE_RATE * 2);
  const warmupStartedAt = performance.now();
  await transcriber(warmupSamples);
  const warmupMs = performance.now() - warmupStartedAt;

  const segmentResults = [];
  let inferenceMs = 0;
  let audioSeconds = 0;
  for (const [index, samples] of job.segments.entries()) {
    reportProgress("transcribe", {segment: index + 1, of: job.segments.length});
    const startedAt = performance.now();
    const output = await transcriber(samples);
    const elapsedMs = performance.now() - startedAt;
    const seconds = samples.length / SAMPLE_RATE;
    inferenceMs += elapsedMs;
    audioSeconds += seconds;
    segmentResults.push({
      audioSeconds: seconds,
      inferenceMs: elapsedMs,
      text: String(output?.text ?? "").trim(),
    });
  }
  if (deviceLost) {
    throw new Error(`GPU device lost during the run (${deviceLost})`);
  }

  const sessionGpu = gpuDevice ? describeSessionGpu(gpuDevice) : null;
  await transcriber.dispose?.();
  gpuDevice?.destroy();
  return {
    ok: true,
    error: null,
    loadMs,
    warmupMs,
    inferenceMs,
    audioSeconds,
    realTimeFactor: inferenceMs / (audioSeconds * 1000),
    slowestSegmentRtf: Math.max(
      ...segmentResults.map((segment) => segment.inferenceMs / (segment.audioSeconds * 1000))
    ),
    text: segmentResults
      .map((segment) => segment.text)
      .filter(Boolean)
      .join(" "),
    segments: segmentResults,
    numThreads: env.backends.onnx.wasm.numThreads,
    crossOriginIsolated: self.crossOriginIsolated === true,
    workerProbe,
    sessionGpu,
  };
};

self.addEventListener("message", async (event) => {
  if (event.data?.type !== "run") {
    return;
  }
  let result;
  try {
    result = await runJob(event.data.job);
  } catch (error) {
    // A failed configuration is an expected, reportable outcome (unsupported
    // precision, out of memory, lost device) — the ladder moves to the next.
    result = {ok: false, error: String(error?.message ?? error)};
  }
  self.postMessage({type: "result", result});
});
