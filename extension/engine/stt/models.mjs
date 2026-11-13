/**
 * Catalogue of in-browser speech-to-text models and the ways each can run.
 *
 * A "variant" is one (execution device, weight precision) pair. The same
 * model is published in several precisions, and which one a machine can use
 * depends on its GPU:
 *
 *   webgpu-mixed GPU, 16-bit encoder + 32-bit decoder. The encoder is where
 *                the GPU time goes, so this keeps most of the fp16 benefit.
 *                Needs the `shader-f16` WebGPU feature.
 *   webgpu-fp32  GPU, 32-bit weights. Works on every WebGPU adapter; the
 *                safe GPU choice and the largest download.
 *   webgpu-fp16  GPU, 16-bit weights throughout. NOT on any ladder today: the
 *                published fp16 merged-decoder files are rejected at load by
 *                the pinned ONNX Runtime ("invalid model … subgraph output
 *                (logits) is an outer scope value") — a model-export defect
 *                that fails identically on every GPU (measured 2026-11-11).
 *                Re-test when bumping the runtime or the model revisions.
 *   webgpu-q4f16 GPU, 4-bit quantised weights with 16-bit activations. For
 *                models too large to ship at fp16 (needs `shader-f16`).
 *   webgpu-q4    GPU, 4-bit quantised weights with 32-bit activations: the
 *                large-model fallback for GPUs where 16-bit maths is absent
 *                or wrong.
 *   wasm-q8      CPU via WebAssembly, 8-bit quantised weights. The universal
 *                fallback: runs anywhere, no GPU involved.
 *
 * `variants` is in PREFERENCE order — stt_plan.mjs walks it top to bottom and
 * keeps the first one that passes the on-device self-test. The order is set
 * from measurements (docs/spike-results/stt-gpu-benchmark.md), not assumed:
 *
 *   - Moonshine is CPU-first. On an Intel Arc integrated GPU its CPU build was
 *     as fast as or faster than its GPU build, at a quarter of the download,
 *     and its 16-bit encoder produced garbage on that GPU — so the GPU is its
 *     fallback (for machines with a weak CPU), and only at fp32.
 *   - The Whisper family is GPU-first. Its encoder always processes a fixed
 *     30 s window, which the CPU cannot do fast enough for short utterances;
 *     the GPU is 3–4x faster there.
 *
 * Download sizes are the sum of the ONNX files Transformers.js fetches for
 * that precision, read from the Hugging Face repositories on 2026-11-11.
 */

export const STT_VARIANTS = Object.freeze({
  "webgpu-mixed": Object.freeze({
    device: "webgpu",
    // Per-file precision, keyed by ONNX file stem (Transformers.js `dtype`).
    dtype: Object.freeze({encoder_model: "fp16", decoder_model_merged: "fp32"}),
    requiresShaderF16: true,
  }),
  "webgpu-fp16": Object.freeze({
    device: "webgpu",
    dtype: "fp16",
    requiresShaderF16: true,
  }),
  "webgpu-fp32": Object.freeze({
    device: "webgpu",
    dtype: "fp32",
    requiresShaderF16: false,
  }),
  "webgpu-q4f16": Object.freeze({
    device: "webgpu",
    dtype: "q4f16",
    requiresShaderF16: true,
  }),
  "webgpu-q4": Object.freeze({
    device: "webgpu",
    dtype: "q4",
    requiresShaderF16: false,
  }),
  "wasm-q8": Object.freeze({
    device: "wasm",
    dtype: "q8",
    requiresShaderF16: false,
  }),
});

/**
 * @typedef {object} SttModel
 * @property {string} id
 * @property {string} label
 * @property {string} repo - Hugging Face repository id
 * @property {string} [revision] - the exact commit of `repo` to download.
 *   Model weights are fetched at first use, not shipped in the package, so
 *   the commit is pinned here: a git commit id names its content, which
 *   makes the download immutable. Set on every model offered to viewers;
 *   bump it deliberately and re-run the benchmark when you do.
 * @property {"fast"|"balanced"|"accurate"} tier
 * @property {string[]} variants - STT_VARIANTS keys, preference order
 * @property {Record<string, number>} downloadMb - per variant
 * @property {number} maxSegmentS - longest clip to hand the model in one call
 *   (10 s everywhere, matching the backend's STT_VAD_MAX_SEGMENT_S: longer
 *   segments mean later verdicts)
 */

/** @type {readonly SttModel[]} */
export const STT_MODELS = Object.freeze([
  {
    id: "moonshine-tiny",
    label: "Moonshine Tiny",
    repo: "onnx-community/moonshine-tiny-ONNX",
    tier: "fast",
    variants: ["wasm-q8", "webgpu-fp32"],
    downloadMb: {"wasm-q8": 28, "webgpu-fp32": 109},
    maxSegmentS: 10,
  },
  {
    id: "moonshine-base",
    label: "Moonshine Base",
    repo: "onnx-community/moonshine-base-ONNX",
    revision: "b1e9b6aae3c3c7298f10c3798393fdf38e8fbbad",
    tier: "fast",
    variants: ["wasm-q8", "webgpu-fp32"],
    downloadMb: {"wasm-q8": 63, "webgpu-fp32": 247},
    maxSegmentS: 10,
  },
  {
    id: "whisper-base.en",
    label: "Whisper Base (English)",
    repo: "onnx-community/whisper-base.en",
    revision: "51eefc0af78b103839eda9e7e4f4186acc6517fe",
    tier: "balanced",
    variants: ["webgpu-mixed", "webgpu-fp32", "wasm-q8"],
    downloadMb: {"webgpu-mixed": 250, "webgpu-fp32": 291, "wasm-q8": 77},
    maxSegmentS: 10,
  },
  {
    // The same model the Python backend defaults to (WHISPER_MODEL), so this
    // row is the like-for-like quality comparison with today's product.
    id: "distil-small.en",
    label: "Distil-Whisper Small (English)",
    repo: "onnx-community/distil-small.en",
    tier: "accurate",
    variants: ["webgpu-mixed", "webgpu-fp32", "wasm-q8"],
    downloadMb: {"webgpu-mixed": 489, "webgpu-fp32": 665, "wasm-q8": 172},
    maxSegmentS: 10,
  },
  {
    // Closest in-browser relative of the backend's best engine (Parakeet
    // TDT). Large: only worth offering on a capable GPU.
    id: "parakeet-ctc-0.6b",
    label: "Parakeet CTC 0.6B",
    repo: "onnx-community/parakeet-ctc-0.6b-ONNX",
    revision: "7df2cab7aed886b8b7f80d68a8214007e4847601",
    tier: "accurate",
    variants: ["webgpu-q4f16", "webgpu-q4", "wasm-q8"],
    downloadMb: {"webgpu-q4f16": 455, "webgpu-q4": 643, "wasm-q8": 612},
    maxSegmentS: 10,
  },
]);

/**
 * The three choices a viewer is offered, from the measurements in
 * docs/spike-results/stt-gpu-benchmark.md. The other catalogue entries stay
 * for the developer benchmark.
 */
export const SPEECH_TIERS = Object.freeze([
  Object.freeze({
    tier: "fast",
    modelId: "moonshine-base",
    label: "Fast",
    summary: "Runs on any computer, no GPU needed. The smallest download.",
  }),
  Object.freeze({
    tier: "balanced",
    modelId: "whisper-base.en",
    label: "Balanced",
    summary: "English only. Needs a GPU to keep up with live speech.",
  }),
  Object.freeze({
    tier: "accurate",
    modelId: "parakeet-ctc-0.6b",
    label: "Accurate",
    summary: "The most accurate, and the fastest on a capable GPU. A large download.",
  }),
]);

/**
 * @param {string} modelId
 * @returns {SttModel}
 * @throws {Error} on an unknown id
 */
export const getSttModel = (modelId) => {
  const model = STT_MODELS.find((candidate) => candidate.id === modelId);
  if (!model) {
    const known = STT_MODELS.map((candidate) => candidate.id).join(", ");
    throw new Error(`Unknown speech model "${modelId}". Known models: ${known}.`);
  }
  return model;
};
