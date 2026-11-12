/**
 * Deciding HOW to run speech-to-text on this particular machine.
 *
 * The rule that makes a variety of GPUs supportable: **never trust a GPU
 * because of what it claims — measure it.** A driver can advertise WebGPU and
 * 16-bit shaders and still be too slow, run out of memory, or (worst) return
 * confident garbage. So selection is a ladder:
 *
 *   1. buildLadder()      — from the GPU probe, list the variants worth
 *                           trying, best first, and record why the others
 *                           were skipped.
 *   2. (benchmark.mjs)    — run each rung on a short clip with a known
 *                           transcript.
 *   3. evaluateRun()      — a rung passes only if it finished, the transcript
 *                           is right, and it is comfortably faster than real
 *                           time.
 *   4. selectVariant()    — the first passing rung wins.
 *
 * Everything here is pure (no browser APIs), so it is unit-tested under Node.
 */

import {GPU_KIND} from "./gpu_probe.mjs";
import {STT_VARIANTS} from "./models.mjs";

/** Why a variant was left off the ladder (LadderSkip.reason). */
export const SKIP_REASON = Object.freeze({
  NO_GPU: "no-usable-gpu",
  SOFTWARE_GPU: "software-gpu",
  NO_SHADER_F16: "gpu-lacks-shader-f16",
  USER_PREFERENCE: "excluded-by-device-setting",
});

/** The viewer's "Speech device" setting. */
export const DEVICE_PREFERENCE = Object.freeze({
  AUTO: "auto", // the model's measured preference order
  GPU: "gpu", // GPU configurations only
  CPU: "cpu", // CPU configurations only
});

/** Why a benchmark run failed (RunVerdict.reason). */
export const FAIL_REASON = Object.freeze({
  ERROR: "error", // crashed, out of memory, device lost, timed out
  WRONG_OUTPUT: "wrong-output", // ran, but the transcript is not the reference
  TOO_SLOW: "too-slow", // right, but cannot keep up with live audio
});

/**
 * Pass marks for the on-device self-test.
 *
 * - `maxRealTimeFactor` 0.5: transcription must take at most half the audio's
 *   duration. Live captioning needs headroom — the GPU is shared with video
 *   decoding, and speech arrives in bursts.
 * - `maxWordErrorRate` 0.25: deliberately loose. This is not a quality bar
 *   (healthy runs score well under 0.1 on the self-test clip); it only
 *   separates "working" from "broken", and a broken precision path produces
 *   error rates near or above 1.0.
 */
export const DEFAULT_THRESHOLDS = Object.freeze({
  maxRealTimeFactor: 0.5,
  maxWordErrorRate: 0.25,
});

/**
 * @typedef {object} LadderRung
 * @property {string} variantId
 * @property {"webgpu"|"wasm"} device
 * @property {string} dtype
 */

/**
 * @typedef {object} LadderSkip
 * @property {string} variantId
 * @property {string} reason - SKIP_REASON.*
 */

/**
 * List the variants of `model` worth trying on the probed machine, in
 * preference order.
 *
 * @param {import("./models.mjs").SttModel} model
 * @param {import("./gpu_probe.mjs").GpuProbe} probe
 * @param {object} [options]
 * @param {"auto"|"gpu"|"cpu"} [options.devicePreference] - the viewer's
 *   device setting; "gpu" and "cpu" restrict the ladder to that device
 * @param {boolean} [options.allowSoftwareGpu] - testing only: also try WebGPU
 *   on a software adapter, to exercise the "bad GPU" path end to end
 * @returns {{rungs: LadderRung[], skipped: LadderSkip[]}}
 */
export const buildLadder = (
  model,
  probe,
  {devicePreference = DEVICE_PREFERENCE.AUTO, allowSoftwareGpu = false} = {}
) => {
  const rungs = [];
  const skipped = [];
  const excludedDevice = {
    [DEVICE_PREFERENCE.GPU]: "wasm",
    [DEVICE_PREFERENCE.CPU]: "webgpu",
  }[devicePreference];
  for (const variantId of model.variants) {
    const variant = STT_VARIANTS[variantId];
    if (!variant) {
      throw new Error(`Model "${model.id}" lists unknown variant "${variantId}".`);
    }
    if (variant.device === excludedDevice) {
      skipped.push({variantId, reason: SKIP_REASON.USER_PREFERENCE});
      continue;
    }
    if (variant.device === "webgpu") {
      if (probe.kind === GPU_KIND.NONE) {
        skipped.push({variantId, reason: SKIP_REASON.NO_GPU});
        continue;
      }
      if (probe.kind === GPU_KIND.SOFTWARE && !allowSoftwareGpu) {
        skipped.push({variantId, reason: SKIP_REASON.SOFTWARE_GPU});
        continue;
      }
      if (variant.requiresShaderF16 && !probe.shaderF16) {
        skipped.push({variantId, reason: SKIP_REASON.NO_SHADER_F16});
        continue;
      }
    }
    rungs.push({variantId, device: variant.device, dtype: variant.dtype});
  }
  return {rungs, skipped};
};

/**
 * @typedef {object} BenchmarkRun
 * @property {string} variantId
 * @property {boolean} ok - the run completed without throwing
 * @property {string|null} [error]
 * @property {number|null} [realTimeFactor] - processing time / audio duration
 * @property {number|null} [wordErrorRate] - against the reference transcript
 */

/**
 * @typedef {object} RunVerdict
 * @property {boolean} pass
 * @property {string|null} reason - FAIL_REASON.* when not passing
 */

/**
 * Judge one benchmark run. Order matters: correctness is checked before
 * speed, because a fast wrong answer is the dangerous outcome.
 *
 * @param {BenchmarkRun} run
 * @param {{maxRealTimeFactor: number, maxWordErrorRate: number}} [thresholds]
 * @returns {RunVerdict}
 */
export const evaluateRun = (run, thresholds = DEFAULT_THRESHOLDS) => {
  if (!run.ok) {
    return {pass: false, reason: FAIL_REASON.ERROR};
  }
  const {wordErrorRate, realTimeFactor} = run;
  if (!Number.isFinite(wordErrorRate) || wordErrorRate > thresholds.maxWordErrorRate) {
    return {pass: false, reason: FAIL_REASON.WRONG_OUTPUT};
  }
  if (
    !Number.isFinite(realTimeFactor) ||
    realTimeFactor > thresholds.maxRealTimeFactor
  ) {
    return {pass: false, reason: FAIL_REASON.TOO_SLOW};
  }
  return {pass: true, reason: null};
};

/**
 * Pick the configuration to use: the first rung, in ladder order, whose run
 * passed. Returns null when nothing passed — the caller then offers hosted
 * transcription or the companion backend.
 *
 * @param {LadderRung[]} rungs
 * @param {BenchmarkRun[]} runs - any order; matched to rungs by variantId
 * @param {{maxRealTimeFactor: number, maxWordErrorRate: number}} [thresholds]
 * @returns {LadderRung|null}
 */
export const selectVariant = (rungs, runs, thresholds = DEFAULT_THRESHOLDS) => {
  const runByVariant = new Map(runs.map((run) => [run.variantId, run]));
  for (const rung of rungs) {
    const run = runByVariant.get(rung.variantId);
    if (run && evaluateRun(run, thresholds).pass) {
      return rung;
    }
  }
  return null;
};
