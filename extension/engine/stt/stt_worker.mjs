/**
 * Live transcription worker: loads one speech model and transcribes the
 * segments it is sent, one at a time, in order.
 *
 * Runs in a dedicated module Worker inside the offscreen document, so
 * inference never blocks the document that owns audio capture.
 *
 * Protocol:
 *   in   {type: "load", config}                 TranscriberConfig (stt_runtime.mjs)
 *   out  {type: "download", progress}           while model files are fetched
 *   out  {type: "ready", info}                  model loaded and warmed up
 *   in   {type: "transcribe", id, samples}      Float32Array, transferred
 *   out  {type: "transcript", id, text, inferenceMs}
 *   in   {type: "dispose"}
 *   out  {type: "error", id?, fatal, message}   fatal: the worker is unusable
 */

import {SAMPLE_RATE, activeThreadCount, loadTranscriber} from "./stt_runtime.mjs";

/** @type {import("./stt_runtime.mjs").LoadedTranscriber|null} */
let transcriber = null;

// Messages are handled strictly one after another: a "transcribe" that
// arrives while the model is still loading waits for it.
let queue = Promise.resolve();

const handleLoad = async (config) => {
  transcriber = await loadTranscriber({
    ...config,
    onDownloadProgress: (progress) => self.postMessage({type: "download", progress}),
  });
  // Pay the one-off costs (shader compilation, JIT warm-up) now, on silence,
  // so the first real utterance is not the slow one.
  const warmupStartedAt = performance.now();
  await transcriber.transcribe(new Float32Array(SAMPLE_RATE));
  self.postMessage({
    type: "ready",
    info: {
      loadMs: transcriber.loadMs,
      warmupMs: performance.now() - warmupStartedAt,
      numThreads: activeThreadCount(),
      crossOriginIsolated: self.crossOriginIsolated === true,
      workerProbe: transcriber.workerProbe,
      sessionGpu: transcriber.sessionGpu,
    },
  });
};

const handleTranscribe = async ({id, samples}) => {
  if (!transcriber) {
    throw new Error("transcribe before load");
  }
  const startedAt = performance.now();
  const text = await transcriber.transcribe(samples);
  const deviceLost = transcriber.deviceLost();
  if (deviceLost) {
    // Results after a lost device are meaningless; the session must restart.
    self.postMessage({type: "error", id, fatal: true, message: `GPU device lost (${deviceLost})`});
    return;
  }
  self.postMessage({type: "transcript", id, text, inferenceMs: performance.now() - startedAt});
};

self.addEventListener("message", (event) => {
  const message = event.data;
  queue = queue.then(async () => {
    try {
      if (message.type === "load") {
        await handleLoad(message.config);
      } else if (message.type === "transcribe") {
        await handleTranscribe(message);
      } else if (message.type === "dispose") {
        await transcriber?.dispose();
        transcriber = null;
      }
    } catch (error) {
      self.postMessage({
        type: "error",
        id: message.id,
        // A failed load leaves nothing to use; a failed utterance is skipped.
        fatal: message.type === "load",
        message: String(error?.message ?? error),
      });
    }
  });
});
