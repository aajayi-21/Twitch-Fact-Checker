/**
 * Benchmark worker: loads ONE speech model in ONE configuration, transcribes
 * the given audio segments, and reports timings.
 *
 * It runs in a dedicated module Worker — the same kind of context the live
 * speech engine uses inside the offscreen document — and the orchestrator
 * (benchmark.mjs) starts a fresh worker per configuration, so every run
 * begins with a clean WebAssembly heap and a clean GPU device, and a crash
 * or hang in one configuration cannot poison the next.
 *
 * Protocol:
 *   in   {type: "run", job}                      (see BenchJob in benchmark.mjs)
 *   out  {type: "progress", stage, detail}       any number of times
 *   out  {type: "result", result}                exactly once; never throws
 */

import {SAMPLE_RATE, activeThreadCount, loadTranscriber} from "./stt_runtime.mjs";

/** @param {string} stage @param {object} [detail] */
const reportProgress = (stage, detail = {}) => {
  self.postMessage({type: "progress", stage, detail});
};

/**
 * @param {import("./benchmark.mjs").BenchJob} job
 * @returns {Promise<object>} the run result (ok: true)
 */
const runJob = async (job) => {
  reportProgress("load");
  const transcriber = await loadTranscriber({
    repo: job.repo,
    device: job.device,
    dtype: job.dtype,
    numThreads: job.numThreads,
    powerPreference: job.powerPreference,
    forceFallbackAdapter: job.forceFallbackAdapter,
    useOwnDevice: job.useOwnDevice,
    onDownloadProgress: (progress) => reportProgress("download", progress),
  });

  // First inference pays one-off costs (WebGPU shader compilation, WASM JIT
  // warm-up) that a live session pays once, not per utterance. Time it
  // separately so it does not distort the steady-state real-time factor.
  reportProgress("warmup");
  const warmupSamples = job.segments[0].slice(0, SAMPLE_RATE * 2);
  const warmupStartedAt = performance.now();
  await transcriber.transcribe(warmupSamples);
  const warmupMs = performance.now() - warmupStartedAt;

  const segmentResults = [];
  let inferenceMs = 0;
  let audioSeconds = 0;
  for (const [index, samples] of job.segments.entries()) {
    reportProgress("transcribe", {segment: index + 1, of: job.segments.length});
    const startedAt = performance.now();
    const text = await transcriber.transcribe(samples);
    const elapsedMs = performance.now() - startedAt;
    const seconds = samples.length / SAMPLE_RATE;
    inferenceMs += elapsedMs;
    audioSeconds += seconds;
    segmentResults.push({audioSeconds: seconds, inferenceMs: elapsedMs, text});
  }
  const deviceLost = transcriber.deviceLost();
  if (deviceLost) {
    throw new Error(`GPU device lost during the run (${deviceLost})`);
  }

  const result = {
    ok: true,
    error: null,
    loadMs: transcriber.loadMs,
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
    numThreads: activeThreadCount(),
    crossOriginIsolated: self.crossOriginIsolated === true,
    workerProbe: transcriber.workerProbe,
    sessionGpu: transcriber.sessionGpu,
  };
  await transcriber.dispose();
  return result;
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
