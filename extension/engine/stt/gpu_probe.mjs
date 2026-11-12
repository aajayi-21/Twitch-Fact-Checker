/**
 * GPU capability probe for in-browser speech-to-text.
 *
 * WebGPU exposes very different hardware behind one API: integrated and
 * discrete GPUs from four vendors, software rasterisers pretending to be
 * GPUs, and machines with no usable adapter at all. This module reduces
 * whatever the browser reports to one plain, serialisable description so the
 * rest of the speech engine (and the benchmark report) never touches a
 * GPUAdapter directly.
 *
 * It only DESCRIBES the machine. Whether a given GPU actually runs a model
 * correctly and fast enough is decided by measurement — see
 * stt_plan.mjs (which configurations to try) and benchmark.mjs.
 *
 * It also OPENS the device (openGpuDevice). The speech engine creates the
 * GPUDevice itself and hands it to ONNX Runtime, instead of letting the
 * runtime pick one: measured on 2026-11-11, the runtime's own adapter
 * selection ignored `forceFallbackAdapter`, so a session could silently run on
 * a different GPU than the one that was probed. Owning the device makes
 * "the GPU we checked" and "the GPU we use" the same object, and gives direct
 * access to `device.lost`.
 *
 * Works in a window, a dedicated worker, and an offscreen document; pass a
 * fake `gpu` object to unit-test it under Node.
 */

/** Why no usable GPU was found (GpuProbe.reason). */
export const PROBE_REASON = Object.freeze({
  WEBGPU_UNAVAILABLE: "webgpu-unavailable", // navigator.gpu is missing
  NO_ADAPTER: "no-adapter", // requestAdapter() resolved null (blocklisted GPU/driver)
  REQUEST_FAILED: "request-failed", // requestAdapter() threw
  SOFTWARE_ADAPTER: "software-adapter", // CPU rasteriser (SwiftShader, llvmpipe, WARP)
  DEVICE_FAILED: "device-request-failed", // adapter found, requestDevice() threw
});

/** Coarse hardware class, for reports and for picking a default model tier. */
export const GPU_KIND = Object.freeze({
  NONE: "none",
  SOFTWARE: "software",
  HARDWARE: "hardware",
});

// Adapter limits worth recording: they bound how large a single model tensor
// or binding can be, and they differ by an order of magnitude across devices.
const RECORDED_LIMITS = Object.freeze([
  "maxBufferSize",
  "maxStorageBufferBindingSize",
  "maxComputeInvocationsPerWorkgroup",
  "maxComputeWorkgroupStorageSize",
  "maxStorageBuffersPerShaderStage",
]);

// Limits raised from the WebGPU defaults to the adapter's maximum when opening
// a device. The defaults (256 MB buffers, 128 MB storage bindings) are too
// small for speech-model weights.
const RAISED_LIMITS = Object.freeze([
  "maxBufferSize",
  "maxStorageBufferBindingSize",
  "maxComputeInvocationsPerWorkgroup",
  "maxComputeWorkgroupStorageSize",
  "maxComputeWorkgroupSizeX",
  "maxComputeWorkgroupSizeY",
  "maxComputeWorkgroupSizeZ",
  "maxComputeWorkgroupsPerDimension",
  "maxStorageBuffersPerShaderStage",
]);

// Optional features to enable when the adapter has them.
const WANTED_FEATURES = Object.freeze(["shader-f16"]);

// Software rasterisers do not always set isFallbackAdapter (Mesa's llvmpipe
// reaches Chrome as an ordinary Vulkan device), so also match by name.
const SOFTWARE_ADAPTER_PATTERN =
  /swiftshader|llvmpipe|lavapipe|softpipe|software|microsoft basic render|warp/i;

/**
 * @typedef {object} GpuProbe
 * @property {boolean} webgpuAvailable - navigator.gpu exists
 * @property {"none"|"software"|"hardware"} kind
 * @property {boolean} usable - true only for a real hardware adapter
 * @property {string|null} reason - PROBE_REASON.* when not usable
 * @property {string|null} error - message when the adapter request threw
 * @property {string} vendor
 * @property {string} architecture
 * @property {string} device
 * @property {string} description
 * @property {boolean} isFallbackAdapter
 * @property {boolean} shaderF16 - 16-bit float shaders (needed for fp16 weights)
 * @property {string[]} features - every adapter feature, sorted
 * @property {Record<string, number>} limits - the RECORDED_LIMITS subset
 * @property {string} powerPreference - what was requested
 */

/**
 * @param {string} powerPreference
 * @returns {GpuProbe}
 */
const emptyProbe = (powerPreference) => ({
  webgpuAvailable: false,
  kind: GPU_KIND.NONE,
  usable: false,
  reason: null,
  error: null,
  vendor: "",
  architecture: "",
  device: "",
  description: "",
  isFallbackAdapter: false,
  shaderF16: false,
  features: [],
  limits: {},
  powerPreference,
});

/**
 * Adapter identity moved between API revisions: current browsers expose a
 * synchronous `adapter.info`, older ones only `requestAdapterInfo()`.
 *
 * @param {GPUAdapter} adapter
 * @returns {Promise<object>}
 */
const readAdapterInfo = async (adapter) => {
  if (adapter.info) {
    return adapter.info;
  }
  if (typeof adapter.requestAdapterInfo === "function") {
    return adapter.requestAdapterInfo();
  }
  return {};
};

/**
 * True when the adapter is a CPU rasteriser rather than real GPU hardware.
 * Emulated WebGPU is slower than the WebAssembly CPU path, so it is treated
 * as "no GPU".
 *
 * @param {object} info - GPUAdapterInfo-like
 * @param {GPUAdapter} adapter
 * @returns {boolean}
 */
const isSoftwareAdapter = (info, adapter) => {
  if (info.isFallbackAdapter === true || adapter.isFallbackAdapter === true) {
    return true;
  }
  if (info.type === "CPU" || info.architecture === "swiftshader") {
    return true;
  }
  const identity = `${info.vendor ?? ""} ${info.device ?? ""} ${info.description ?? ""}`;
  return SOFTWARE_ADAPTER_PATTERN.test(identity);
};

/**
 * Ask the browser for an adapter. Never throws.
 *
 * @param {GPU|null|undefined} gpu
 * @param {string} powerPreference
 * @param {boolean} forceFallbackAdapter
 * @returns {Promise<{adapter: GPUAdapter|null, probe: GpuProbe}>} `probe` is
 *   complete when `adapter` is null, and a starting point otherwise
 */
const requestAdapter = async (gpu, powerPreference, forceFallbackAdapter) => {
  const probe = emptyProbe(powerPreference);
  if (!gpu) {
    return {adapter: null, probe: {...probe, reason: PROBE_REASON.WEBGPU_UNAVAILABLE}};
  }
  probe.webgpuAvailable = true;
  try {
    const adapter = await gpu.requestAdapter({powerPreference, forceFallbackAdapter});
    if (!adapter) {
      return {adapter: null, probe: {...probe, reason: PROBE_REASON.NO_ADAPTER}};
    }
    return {adapter, probe};
  } catch (error) {
    return {
      adapter: null,
      probe: {
        ...probe,
        reason: PROBE_REASON.REQUEST_FAILED,
        error: String(error?.message ?? error),
      },
    };
  }
};

/**
 * Reduce an adapter to a plain description.
 *
 * @param {GPUAdapter} adapter
 * @param {GpuProbe} baseProbe - from requestAdapter()
 * @returns {Promise<GpuProbe>}
 */
const describeAdapter = async (adapter, baseProbe) => {
  const info = await readAdapterInfo(adapter);
  const features = [...(adapter.features ?? [])].sort();
  const limits = {};
  for (const name of RECORDED_LIMITS) {
    const value = adapter.limits?.[name];
    if (typeof value === "number") {
      limits[name] = value;
    }
  }
  const software = isSoftwareAdapter(info, adapter);
  return {
    ...baseProbe,
    kind: software ? GPU_KIND.SOFTWARE : GPU_KIND.HARDWARE,
    usable: !software,
    reason: software ? PROBE_REASON.SOFTWARE_ADAPTER : null,
    vendor: info.vendor ?? "",
    architecture: info.architecture ?? "",
    device: info.device ?? "",
    description: info.description ?? "",
    isFallbackAdapter:
      info.isFallbackAdapter === true || adapter.isFallbackAdapter === true,
    shaderF16: features.includes("shader-f16"),
    features,
    limits,
  };
};

/**
 * @typedef {object} GpuRequestOptions
 * @property {"high-performance"|"low-power"} [powerPreference] - which GPU to
 *   prefer on dual-GPU laptops
 * @property {boolean} [forceFallbackAdapter] - ask for the software adapter
 *   (testing only: simulates a machine without a usable GPU)
 * @property {GPU} [gpu] - injection point for tests
 */

/**
 * Describe the GPU the browser offers, without opening a device.
 *
 * Never throws: every failure mode is a normal, reportable outcome.
 *
 * @param {GpuRequestOptions} [options]
 * @returns {Promise<GpuProbe>}
 */
export const probeGpu = async ({
  powerPreference = "high-performance",
  forceFallbackAdapter = false,
  gpu = globalThis.navigator?.gpu,
} = {}) => {
  const {adapter, probe} = await requestAdapter(gpu, powerPreference, forceFallbackAdapter);
  return adapter ? describeAdapter(adapter, probe) : probe;
};

/**
 * Open a GPUDevice for speech inference, with its limits raised to what the
 * adapter supports, and describe the adapter it came from.
 *
 * `device` is null when there is no adapter or the device could not be
 * created; `probe.reason` says which. A software adapter still yields a
 * device — whether to use it is the caller's decision (stt_plan.mjs).
 *
 * Never throws.
 *
 * @param {GpuRequestOptions} [options]
 * @returns {Promise<{probe: GpuProbe, device: GPUDevice|null}>}
 */
export const openGpuDevice = async ({
  powerPreference = "high-performance",
  forceFallbackAdapter = false,
  gpu = globalThis.navigator?.gpu,
} = {}) => {
  const requested = await requestAdapter(gpu, powerPreference, forceFallbackAdapter);
  if (!requested.adapter) {
    return {probe: requested.probe, device: null};
  }
  const {adapter} = requested;
  const probe = await describeAdapter(adapter, requested.probe);

  const requiredLimits = {};
  for (const name of RAISED_LIMITS) {
    const value = adapter.limits?.[name];
    if (typeof value === "number") {
      requiredLimits[name] = value;
    }
  }
  const requiredFeatures = WANTED_FEATURES.filter((feature) =>
    adapter.features?.has(feature)
  );
  try {
    const device = await adapter.requestDevice({requiredFeatures, requiredLimits});
    return {probe, device};
  } catch (error) {
    return {
      probe: {
        ...probe,
        usable: false,
        reason: PROBE_REASON.DEVICE_FAILED,
        error: String(error?.message ?? error),
      },
      device: null,
    };
  }
};

/**
 * Stable identity for caching a benchmark verdict: a result measured on one
 * adapter must not be reused after the user plugs in an eGPU, updates a
 * driver that changes the reported device, or switches power preference.
 *
 * @param {GpuProbe} probe
 * @returns {string}
 */
export const gpuCacheKey = (probe) =>
  [
    probe.kind,
    probe.vendor,
    probe.architecture,
    probe.device,
    probe.description,
    probe.shaderF16 ? "f16" : "no-f16",
    probe.powerPreference,
  ].join("|");

/**
 * Short human label for the UI and reports, e.g. "Intel (gen-12lp)".
 *
 * @param {GpuProbe} probe
 * @returns {string}
 */
export const describeGpu = (probe) => {
  if (probe.kind === GPU_KIND.NONE) {
    return "No GPU available to the browser";
  }
  const name = probe.description || probe.device || probe.vendor || "Unknown GPU";
  const detail = probe.architecture ? ` (${probe.architecture})` : "";
  const software = probe.kind === GPU_KIND.SOFTWARE ? " — software renderer" : "";
  return `${name}${detail}${software}`;
};
