/**
 * Unit tests for the browser speech engine's pure logic.
 *
 * Run from the repo root (Node 20+, no dependencies):
 *
 *     node --test "extension/tests/*.test.mjs"
 */

import assert from "node:assert/strict";
import {describe, it} from "node:test";

import {
  decodeWavPcm16,
  splitOnSilence,
  totalDurationS,
} from "../engine/stt/audio_utils.mjs";
import {
  GPU_KIND,
  PROBE_REASON,
  describeGpu,
  gpuCacheKey,
  openGpuDevice,
  probeGpu,
} from "../engine/stt/gpu_probe.mjs";
import {STT_MODELS, STT_VARIANTS, getSttModel} from "../engine/stt/models.mjs";
import {
  DEFAULT_THRESHOLDS,
  DEVICE_PREFERENCE,
  FAIL_REASON,
  SKIP_REASON,
  buildLadder,
  evaluateRun,
  selectVariant,
} from "../engine/stt/stt_plan.mjs";
import {normalizeWords, numberToWords, wordErrorRate} from "../engine/stt/wer.mjs";

/**
 * Build a fake `navigator.gpu` whose adapter looks like the given hardware.
 */
const fakeGpu = ({info = {}, features = [], limits = {}, legacyInfo = false} = {}) => ({
  requestAdapter: async () => ({
    ...(legacyInfo ? {requestAdapterInfo: async () => info} : {info}),
    features: new Set(features),
    limits,
  }),
});

const INTEL_ARC = {
  info: {vendor: "intel", architecture: "xe-2lpg", device: "", description: "Intel(R) Graphics (LNL)"},
  features: ["shader-f16", "timestamp-query"],
  limits: {maxBufferSize: 2 ** 31, maxStorageBufferBindingSize: 2 ** 30},
};

describe("probeGpu", () => {
  it("reports a missing navigator.gpu", async () => {
    const probe = await probeGpu({gpu: null});
    assert.equal(probe.webgpuAvailable, false);
    assert.equal(probe.kind, GPU_KIND.NONE);
    assert.equal(probe.usable, false);
    assert.equal(probe.reason, PROBE_REASON.WEBGPU_UNAVAILABLE);
  });

  it("reports a null adapter (blocklisted GPU or driver)", async () => {
    const probe = await probeGpu({gpu: {requestAdapter: async () => null}});
    assert.equal(probe.webgpuAvailable, true);
    assert.equal(probe.usable, false);
    assert.equal(probe.reason, PROBE_REASON.NO_ADAPTER);
  });

  it("never throws when the adapter request rejects", async () => {
    const gpu = {
      requestAdapter: async () => {
        throw new Error("GPU process crashed");
      },
    };
    const probe = await probeGpu({gpu});
    assert.equal(probe.reason, PROBE_REASON.REQUEST_FAILED);
    assert.equal(probe.error, "GPU process crashed");
  });

  it("describes a hardware adapter with shader-f16", async () => {
    const probe = await probeGpu({gpu: fakeGpu(INTEL_ARC)});
    assert.equal(probe.kind, GPU_KIND.HARDWARE);
    assert.equal(probe.usable, true);
    assert.equal(probe.reason, null);
    assert.equal(probe.vendor, "intel");
    assert.equal(probe.shaderF16, true);
    assert.equal(probe.limits.maxBufferSize, 2 ** 31);
    assert.deepEqual(probe.features, ["shader-f16", "timestamp-query"]);
  });

  it("reads adapter identity through the legacy requestAdapterInfo()", async () => {
    const probe = await probeGpu({gpu: fakeGpu({...INTEL_ARC, legacyInfo: true})});
    assert.equal(probe.vendor, "intel");
    assert.equal(probe.usable, true);
  });

  it("treats a flagged fallback adapter as software", async () => {
    const gpu = fakeGpu({info: {vendor: "google", isFallbackAdapter: true}});
    const probe = await probeGpu({gpu});
    assert.equal(probe.kind, GPU_KIND.SOFTWARE);
    assert.equal(probe.usable, false);
    assert.equal(probe.reason, PROBE_REASON.SOFTWARE_ADAPTER);
  });

  for (const description of [
    "llvmpipe (LLVM 21.1.8, 256 bits)",
    "SwiftShader Device (Subzero)",
    "Microsoft Basic Render Driver",
  ]) {
    it(`recognises "${description}" as software even when unflagged`, async () => {
      const probe = await probeGpu({gpu: fakeGpu({info: {vendor: "x", description}})});
      assert.equal(probe.kind, GPU_KIND.SOFTWARE);
    });
  }

  it("does not mistake real vendors for software", async () => {
    for (const info of [
      {vendor: "nvidia", architecture: "lovelace", description: "NVIDIA GeForce RTX 4070"},
      {vendor: "amd", architecture: "rdna-3", description: "AMD Radeon RX 7800 XT"},
      {vendor: "apple", architecture: "metal-3", description: "Apple M3"},
      {vendor: "qualcomm", architecture: "adreno-7xx", description: "Adreno X1-85"},
    ]) {
      const probe = await probeGpu({gpu: fakeGpu({info})});
      assert.equal(probe.kind, GPU_KIND.HARDWARE, info.description);
    }
  });

  it("passes the power preference and fallback flag to the browser", async () => {
    let received = null;
    const gpu = {
      requestAdapter: async (options) => {
        received = options;
        return null;
      },
    };
    await probeGpu({gpu, powerPreference: "low-power", forceFallbackAdapter: true});
    assert.deepEqual(received, {powerPreference: "low-power", forceFallbackAdapter: true});
  });
});

describe("openGpuDevice", () => {
  /** Fake GPU whose adapter records the descriptor passed to requestDevice. */
  const deviceGpu = ({features = [], limits = {}, failWith = null} = {}) => {
    const calls = [];
    const gpu = {
      requestAdapter: async () => ({
        info: {vendor: "amd", architecture: "rdna-3", description: "AMD Radeon"},
        features: new Set(features),
        limits,
        requestDevice: async (descriptor) => {
          calls.push(descriptor);
          if (failWith) {
            throw new Error(failWith);
          }
          return {label: "fake-device"};
        },
      }),
    };
    return {gpu, calls};
  };

  it("returns no device and the probe reason when WebGPU is missing", async () => {
    const {probe, device} = await openGpuDevice({gpu: null});
    assert.equal(device, null);
    assert.equal(probe.reason, PROBE_REASON.WEBGPU_UNAVAILABLE);
  });

  it("raises limits to the adapter's maximum and enables shader-f16 when present", async () => {
    const {gpu, calls} = deviceGpu({
      features: ["shader-f16", "timestamp-query"],
      limits: {maxBufferSize: 4_000_000_000, maxStorageBufferBindingSize: 2_000_000_000, maxTextureDimension2D: 16384},
    });
    const {probe, device} = await openGpuDevice({gpu});
    assert.deepEqual(device, {label: "fake-device"});
    assert.equal(probe.usable, true);
    assert.deepEqual(calls[0].requiredFeatures, ["shader-f16"]);
    assert.equal(calls[0].requiredLimits.maxBufferSize, 4_000_000_000);
    assert.equal(calls[0].requiredLimits.maxStorageBufferBindingSize, 2_000_000_000);
    assert.equal("maxTextureDimension2D" in calls[0].requiredLimits, false);
  });

  it("does not request shader-f16 from an adapter that lacks it", async () => {
    const {gpu, calls} = deviceGpu({features: ["timestamp-query"]});
    await openGpuDevice({gpu});
    assert.deepEqual(calls[0].requiredFeatures, []);
  });

  it("reports a failed device request instead of throwing", async () => {
    const {gpu} = deviceGpu({failWith: "out of memory"});
    const {probe, device} = await openGpuDevice({gpu});
    assert.equal(device, null);
    assert.equal(probe.usable, false);
    assert.equal(probe.reason, PROBE_REASON.DEVICE_FAILED);
    assert.equal(probe.error, "out of memory");
  });
});

describe("gpuCacheKey / describeGpu", () => {
  it("changes when the adapter or its capabilities change", async () => {
    const withF16 = await probeGpu({gpu: fakeGpu(INTEL_ARC)});
    const withoutF16 = await probeGpu({gpu: fakeGpu({...INTEL_ARC, features: []})});
    const lowPower = await probeGpu({gpu: fakeGpu(INTEL_ARC), powerPreference: "low-power"});
    assert.notEqual(gpuCacheKey(withF16), gpuCacheKey(withoutF16));
    assert.notEqual(gpuCacheKey(withF16), gpuCacheKey(lowPower));
    assert.equal(gpuCacheKey(withF16), gpuCacheKey({...withF16}));
  });

  it("labels each kind readably", async () => {
    assert.match(describeGpu(await probeGpu({gpu: null})), /No GPU/);
    assert.equal(
      describeGpu(await probeGpu({gpu: fakeGpu(INTEL_ARC)})),
      "Intel(R) Graphics (LNL) (xe-2lpg)"
    );
    const software = await probeGpu({gpu: fakeGpu({info: {description: "llvmpipe"}})});
    assert.match(describeGpu(software), /software renderer/);
  });
});

describe("model catalogue", () => {
  it("only references known variants, each with a download size", () => {
    for (const model of STT_MODELS) {
      assert.ok(model.variants.length > 0, model.id);
      for (const variantId of model.variants) {
        assert.ok(STT_VARIANTS[variantId], `${model.id}: ${variantId}`);
        assert.ok(model.downloadMb[variantId] > 0, `${model.id}: ${variantId} size`);
      }
    }
  });

  it("gives every model a CPU configuration, so no machine is left without one", () => {
    for (const model of STT_MODELS) {
      assert.ok(
        model.variants.some((variantId) => STT_VARIANTS[variantId].device === "wasm"),
        model.id
      );
    }
  });

  it("rejects unknown ids with the list of known ones", () => {
    assert.throws(() => getSttModel("nope"), /Known models: moonshine-tiny/);
  });
});

describe("buildLadder", () => {
  const whisper = getSttModel("whisper-base.en");
  const moonshine = getSttModel("moonshine-base");
  const hardware = {kind: GPU_KIND.HARDWARE, shaderF16: true};
  const ids = (ladder) => ladder.rungs.map((rung) => rung.variantId);

  it("tries a GPU-first model at mixed precision, then fp32, then CPU", () => {
    const ladder = buildLadder(whisper, hardware);
    assert.deepEqual(ids(ladder), ["webgpu-mixed", "webgpu-fp32", "wasm-q8"]);
    assert.deepEqual(ladder.skipped, []);
  });

  it("tries a CPU-first model on the CPU before the GPU", () => {
    assert.deepEqual(ids(buildLadder(moonshine, hardware)), ["wasm-q8", "webgpu-fp32"]);
  });

  it("skips the 16-bit encoder on a GPU without shader-f16", () => {
    const ladder = buildLadder(whisper, {...hardware, shaderF16: false});
    assert.deepEqual(ids(ladder), ["webgpu-fp32", "wasm-q8"]);
    assert.deepEqual(ladder.skipped, [
      {variantId: "webgpu-mixed", reason: SKIP_REASON.NO_SHADER_F16},
    ]);
  });

  it("goes straight to CPU with no GPU", () => {
    const ladder = buildLadder(whisper, {kind: GPU_KIND.NONE, shaderF16: false});
    assert.deepEqual(ladder.rungs, [{variantId: "wasm-q8", device: "wasm", dtype: "q8"}]);
    assert.equal(ladder.skipped.length, 2);
    assert.ok(ladder.skipped.every((skip) => skip.reason === SKIP_REASON.NO_GPU));
  });

  it("goes straight to CPU on a software adapter unless testing", () => {
    const software = {kind: GPU_KIND.SOFTWARE, shaderF16: false};
    const ladder = buildLadder(whisper, software);
    assert.deepEqual(ids(ladder), ["wasm-q8"]);
    assert.ok(ladder.skipped.every((skip) => skip.reason === SKIP_REASON.SOFTWARE_GPU));
    assert.deepEqual(ids(buildLadder(whisper, software, {allowSoftwareGpu: true})), [
      "webgpu-fp32",
      "wasm-q8",
    ]);
  });

  it("drops only the 16-bit rung of a large model when f16 is missing", () => {
    const parakeet = getSttModel("parakeet-ctc-0.6b");
    assert.deepEqual(ids(buildLadder(parakeet, {...hardware, shaderF16: false})), [
      "webgpu-q4",
      "wasm-q8",
    ]);
  });

  it("restricts the ladder to the GPU when the viewer forces it", () => {
    const ladder = buildLadder(moonshine, hardware, {devicePreference: DEVICE_PREFERENCE.GPU});
    assert.deepEqual(ids(ladder), ["webgpu-fp32"]);
    assert.deepEqual(ladder.skipped, [
      {variantId: "wasm-q8", reason: SKIP_REASON.USER_PREFERENCE},
    ]);
  });

  it("restricts the ladder to the CPU when the viewer forces it", () => {
    const ladder = buildLadder(whisper, hardware, {devicePreference: DEVICE_PREFERENCE.CPU});
    assert.deepEqual(ids(ladder), ["wasm-q8"]);
  });

  it("can end up empty when the GPU is forced on a machine without one", () => {
    const ladder = buildLadder(whisper, {kind: GPU_KIND.NONE, shaderF16: false}, {
      devicePreference: DEVICE_PREFERENCE.GPU,
    });
    assert.deepEqual(ladder.rungs, []);
  });
});

describe("evaluateRun / selectVariant", () => {
  const good = {variantId: "webgpu-fp16", ok: true, wordErrorRate: 0.02, realTimeFactor: 0.1};

  it("passes a correct, fast run", () => {
    assert.deepEqual(evaluateRun(good), {pass: true, reason: null});
  });

  it("fails a crashed run", () => {
    assert.equal(evaluateRun({...good, ok: false, error: "device lost"}).reason, FAIL_REASON.ERROR);
  });

  it("fails a fast run with a wrong transcript — correctness before speed", () => {
    const garbage = {...good, wordErrorRate: 1.4, realTimeFactor: 0.01};
    assert.equal(evaluateRun(garbage).reason, FAIL_REASON.WRONG_OUTPUT);
  });

  it("fails a correct run that cannot keep up", () => {
    assert.equal(evaluateRun({...good, realTimeFactor: 0.9}).reason, FAIL_REASON.TOO_SLOW);
  });

  it("fails a run with missing measurements", () => {
    assert.equal(evaluateRun({...good, wordErrorRate: null}).reason, FAIL_REASON.WRONG_OUTPUT);
    assert.equal(evaluateRun({...good, realTimeFactor: NaN}).reason, FAIL_REASON.TOO_SLOW);
  });

  it("accepts values exactly at the thresholds", () => {
    const atLimit = {
      ...good,
      wordErrorRate: DEFAULT_THRESHOLDS.maxWordErrorRate,
      realTimeFactor: DEFAULT_THRESHOLDS.maxRealTimeFactor,
    };
    assert.equal(evaluateRun(atLimit).pass, true);
  });

  const rungs = [
    {variantId: "webgpu-fp16", device: "webgpu", dtype: "fp16"},
    {variantId: "webgpu-fp32", device: "webgpu", dtype: "fp32"},
    {variantId: "wasm-q8", device: "wasm", dtype: "q8"},
  ];

  it("picks the first passing rung in ladder order, not run order", () => {
    const runs = [
      {variantId: "wasm-q8", ok: true, wordErrorRate: 0.03, realTimeFactor: 0.3},
      {variantId: "webgpu-fp32", ok: true, wordErrorRate: 0.02, realTimeFactor: 0.1},
      {variantId: "webgpu-fp16", ok: true, wordErrorRate: 1.2, realTimeFactor: 0.05},
    ];
    assert.equal(selectVariant(rungs, runs).variantId, "webgpu-fp32");
  });

  it("falls through to CPU when both GPU rungs are broken", () => {
    const runs = [
      {variantId: "webgpu-fp16", ok: false, error: "out of memory"},
      {variantId: "webgpu-fp32", ok: true, wordErrorRate: 0.9, realTimeFactor: 0.1},
      {variantId: "wasm-q8", ok: true, wordErrorRate: 0.04, realTimeFactor: 0.4},
    ];
    assert.equal(selectVariant(rungs, runs).variantId, "wasm-q8");
  });

  it("returns null when nothing passes or nothing ran", () => {
    const slow = rungs.map((rung) => ({
      variantId: rung.variantId,
      ok: true,
      wordErrorRate: 0.02,
      realTimeFactor: 2,
    }));
    assert.equal(selectVariant(rungs, slow), null);
    assert.equal(selectVariant(rungs, []), null);
  });
});

describe("word error rate", () => {
  it("spells out numbers the way the backend scorer does", () => {
    assert.deepEqual(numberToWords(0), ["zero"]);
    assert.deepEqual(numberToWords(450), ["four", "hundred", "fifty"]);
    assert.deepEqual(numberToWords(300000), ["three", "hundred", "thousand"]);
    assert.deepEqual(numberToWords(1001), ["one", "thousand", "one"]);
  });

  it("treats digits and spelled-out numbers as equal", () => {
    const reference = "The Eiffel Tower is four hundred and fifty meters tall";
    assert.equal(wordErrorRate(reference, "the Eiffel Tower is 450 meters tall."), 0);
    assert.equal(wordErrorRate("about 300,000 kilometers", "about three hundred thousand kilometers"), 0);
  });

  it("normalises case, punctuation, percent signs and stray apostrophes", () => {
    assert.deepEqual(normalizeWords("That's 10% off, 'chat'!"), [
      "that's",
      "ten",
      "percent",
      "off",
      "chat",
    ]);
  });

  it("keeps a meaningful 'and' outside numbers", () => {
    assert.deepEqual(normalizeWords("push and take"), ["push", "and", "take"]);
  });

  it("counts substitutions, deletions and insertions", () => {
    assert.equal(wordErrorRate("one two three four", "one two three four"), 0);
    assert.equal(wordErrorRate("one two three four", "one too three four"), 0.25);
    assert.equal(wordErrorRate("one two three four", "one three four"), 0.25);
    assert.equal(wordErrorRate("one two three four", "one two two three four"), 0.25);
    assert.equal(wordErrorRate("one two", ""), 1);
  });

  it("can exceed 1 for hallucinated output", () => {
    assert.ok(wordErrorRate("hello", "completely unrelated words here") > 1);
  });
});

/** Minimal 16-bit PCM WAV writer for the decoder tests. */
const makeWav = (samples, {sampleRate = 16000, channels = 1, extraChunk = false} = {}) => {
  const extraBytes = extraChunk ? 8 + 3 + 1 : 0; // odd-sized chunk + pad byte
  const buffer = new ArrayBuffer(44 + extraBytes + samples.length * 2);
  const view = new DataView(buffer);
  const writeTag = (offset, tag) =>
    [...tag].forEach((char, index) => view.setUint8(offset + index, char.charCodeAt(0)));
  writeTag(0, "RIFF");
  view.setUint32(4, buffer.byteLength - 8, true);
  writeTag(8, "WAVE");
  writeTag(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, channels, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * channels * 2, true);
  view.setUint16(32, channels * 2, true);
  view.setUint16(34, 16, true);
  let offset = 36;
  if (extraChunk) {
    writeTag(offset, "LIST");
    view.setUint32(offset + 4, 3, true);
    offset += 8 + 3 + 1;
  }
  writeTag(offset, "data");
  view.setUint32(offset + 4, samples.length * 2, true);
  samples.forEach((sample, index) => view.setInt16(offset + 8 + index * 2, sample, true));
  return buffer;
};

describe("decodeWavPcm16", () => {
  it("decodes samples to floats in [-1, 1)", () => {
    const decoded = decodeWavPcm16(makeWav([0, 16384, -32768, 32767]));
    assert.equal(decoded.length, 4);
    assert.equal(decoded[0], 0);
    assert.equal(decoded[1], 0.5);
    assert.equal(decoded[2], -1);
    assert.ok(decoded[3] < 1);
  });

  it("skips unrelated chunks, including odd-sized ones", () => {
    const decoded = decodeWavPcm16(makeWav([100, 200], {extraChunk: true}));
    assert.equal(decoded.length, 2);
  });

  it("rejects the wrong sample rate or channel count with the actual format", () => {
    assert.throws(() => decodeWavPcm16(makeWav([0], {sampleRate: 44100})), /got 44100 Hz/);
    assert.throws(() => decodeWavPcm16(makeWav([0, 0], {channels: 2})), /2 channel/);
  });

  it("rejects non-WAV data", () => {
    assert.throws(() => decodeWavPcm16(new ArrayBuffer(64)), /Not a RIFF/);
  });
});

describe("splitOnSilence", () => {
  const SAMPLE_RATE = 16000;
  /** Build audio from [seconds, amplitude] spans of a 220 Hz tone. */
  const makeAudio = (spans) => {
    const total = spans.reduce((sum, [seconds]) => sum + Math.round(seconds * SAMPLE_RATE), 0);
    const samples = new Float32Array(total);
    let cursor = 0;
    for (const [seconds, amplitude] of spans) {
      const count = Math.round(seconds * SAMPLE_RATE);
      for (let index = 0; index < count; index += 1) {
        samples[cursor + index] = amplitude * Math.sin((2 * Math.PI * 220 * index) / SAMPLE_RATE);
      }
      cursor += count;
    }
    return samples;
  };

  it("returns nothing for silence or empty input", () => {
    assert.deepEqual(splitOnSilence(new Float32Array(SAMPLE_RATE)), []);
    assert.deepEqual(splitOnSilence(new Float32Array(0)), []);
  });

  it("cuts at pauses and keeps each utterance whole", () => {
    const audio = makeAudio([
      [0.5, 0],
      [2, 0.5],
      [0.6, 0],
      [3, 0.5],
      [0.5, 0],
    ]);
    const ranges = splitOnSilence(audio, {padMs: 0});
    assert.equal(ranges.length, 2);
    const seconds = ranges.map((range) => (range.end - range.start) / SAMPLE_RATE);
    assert.ok(Math.abs(seconds[0] - 2) < 0.3, `first segment ${seconds[0]}s`);
    assert.ok(Math.abs(seconds[1] - 3) < 0.3, `second segment ${seconds[1]}s`);
    assert.ok(ranges[0].end <= ranges[1].start);
  });

  it("does not split on a pause shorter than minSilenceMs", () => {
    const audio = makeAudio([
      [1, 0.5],
      [0.12, 0],
      [1, 0.5],
    ]);
    assert.equal(splitOnSilence(audio).length, 1);
  });

  it("caps unbroken speech at maxSegmentS", () => {
    const audio = makeAudio([[20, 0.5]]);
    const ranges = splitOnSilence(audio, {maxSegmentS: 6, padMs: 0});
    assert.ok(ranges.length >= 3);
    for (const range of ranges) {
      assert.ok((range.end - range.start) / SAMPLE_RATE <= 6.01);
    }
    assert.ok(Math.abs(totalDurationS(ranges) - 20) < 0.1);
  });

  it("drops fragments shorter than minSegmentS", () => {
    const audio = makeAudio([
      [0.5, 0],
      [0.09, 0.5],
      [1, 0],
    ]);
    assert.deepEqual(splitOnSilence(audio), []);
  });

  it("never returns ranges outside the buffer", () => {
    const audio = makeAudio([[1.5, 0.5]]);
    const [range] = splitOnSilence(audio, {padMs: 500});
    assert.ok(range.start >= 0 && range.end <= audio.length);
  });
});
