/**
 * Loading a speech model inside a worker — shared by the benchmark worker
 * and the live transcription worker, so both exercise the same code path.
 *
 * Importing this module configures the vendored runtime for Manifest V3:
 * all executable code ships in the package, so ONNX Runtime is pointed at
 * the vendored WebAssembly module instead of its default CDN, and the
 * library's WASM cache is turned off — it re-serves the loader from a
 * `blob:` URL, which the extension's `script-src 'self'` policy blocks.
 *
 * Worker-only: it imports the ~1.4 MB Transformers.js bundle.
 */

import {env, pipeline} from "../../vendor/transformers/transformers.js";

import {openGpuDevice} from "./gpu_probe.mjs";

const RUNTIME_DIR = new URL("../../vendor/transformers/", import.meta.url);

env.allowLocalModels = false;
env.useWasmCache = false;
env.backends.onnx.wasm.wasmPaths = {
  mjs: new URL("ort-wasm-simd-threaded.asyncify.mjs", RUNTIME_DIR).href,
  wasm: new URL("ort-wasm-simd-threaded.asyncify.wasm", RUNTIME_DIR).href,
};

export const SAMPLE_RATE = 16000;

/**
 * Collapse the library's per-file download events into one overall fraction.
 *
 * @param {(progress: {percent: number, loadedMb: number, totalMb: number}) => void} onProgress
 * @returns {(event: object) => void} a Transformers.js `progress_callback`
 */
const makeDownloadReporter = (onProgress) => {
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
      onProgress({percent, loadedMb: loaded / 1e6, totalMb: total / 1e6});
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
 * @typedef {object} TranscriberConfig
 * @property {string} repo - Hugging Face model repository
 * @property {string} [revision] - exact commit to download (models.mjs);
 *   omitted, the repository's current head is used
 * @property {"webgpu"|"wasm"} device
 * @property {string|object} dtype - precision, or a per-file precision map
 * @property {number} numThreads - WASM threads (1 unless cross-origin isolated)
 * @property {"high-performance"|"low-power"} [powerPreference]
 * @property {boolean} [forceFallbackAdapter] - testing: use the software GPU
 * @property {boolean} [useOwnDevice] - hand ONNX Runtime the device opened
 *   here (the production behaviour); false is a diagnostic A/B switch
 * @property {(progress: object) => void} [onDownloadProgress]
 */

/**
 * @typedef {object} LoadedTranscriber
 * @property {(samples: Float32Array) => Promise<string>} transcribe
 * @property {number} loadMs
 * @property {object|null} workerProbe - GPU description seen by this worker
 * @property {object|null} sessionGpu - the GPU the session actually uses
 * @property {() => string|null} deviceLost - reason, once the device is lost
 * @property {() => Promise<void>} dispose
 */

/**
 * Load a speech model in the given configuration.
 *
 * On the GPU path this opens the GPUDevice itself and hands it to ONNX
 * Runtime through the execution-provider options. Left to itself the runtime
 * picks its own adapter and ignores `forceFallbackAdapter`, so the GPU that
 * was probed and the GPU that ran the model could differ.
 *
 * @param {TranscriberConfig} config
 * @returns {Promise<LoadedTranscriber>}
 * @throws {Error} when no GPU device is available or the model fails to load
 */
export const loadTranscriber = async ({
  repo,
  revision,
  device,
  dtype,
  numThreads,
  powerPreference = "high-performance",
  forceFallbackAdapter = false,
  useOwnDevice = true,
  onDownloadProgress = () => {},
}) => {
  env.backends.onnx.wasm.numThreads = self.crossOriginIsolated === true ? numThreads : 1;

  let gpuDevice = null;
  let workerProbe = null;
  let deviceLost = null;
  const pipelineOptions = {
    device,
    dtype,
    progress_callback: makeDownloadReporter(onDownloadProgress),
  };
  if (revision) {
    pipelineOptions.revision = revision;
  }
  if (device === "webgpu") {
    const opened = await openGpuDevice({powerPreference, forceFallbackAdapter});
    workerProbe = opened.probe;
    gpuDevice = opened.device;
    if (!gpuDevice) {
      const detail = workerProbe.error ? `: ${workerProbe.error}` : "";
      throw new Error(`No GPU device (${workerProbe.reason}${detail})`);
    }
    if (useOwnDevice) {
      gpuDevice.lost.then((lostInfo) => {
        deviceLost = `${lostInfo.reason || "unknown"}: ${lostInfo.message || ""}`.trim();
      });
      pipelineOptions.session_options = {
        executionProviders: [{name: "webgpu", device: gpuDevice}],
      };
    } else {
      gpuDevice.destroy();
      gpuDevice = null;
    }
  }

  const loadStartedAt = performance.now();
  const transcriber = await pipeline("automatic-speech-recognition", repo, pipelineOptions);
  const loadMs = performance.now() - loadStartedAt;

  return {
    loadMs,
    workerProbe,
    sessionGpu: gpuDevice ? describeSessionGpu(gpuDevice) : null,
    deviceLost: () => deviceLost,
    transcribe: async (samples) => {
      const output = await transcriber(samples);
      return String(output?.text ?? "").trim();
    },
    dispose: async () => {
      await transcriber.dispose?.();
      gpuDevice?.destroy();
    },
  };
};

/**
 * The WASM thread count actually in effect. Threads need SharedArrayBuffer,
 * so outside a cross-origin isolated context ONNX Runtime silently runs
 * single-threaded whatever was requested.
 *
 * @returns {number}
 */
export const activeThreadCount = () =>
  self.crossOriginIsolated === true ? env.backends.onnx.wasm.numThreads : 1;
