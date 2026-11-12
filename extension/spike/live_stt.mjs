/**
 * Spike S3 — controller page for the live speech test.
 *
 * An ordinary extension page (so it has chrome.offscreen and
 * chrome.tabCapture). It creates the spike offscreen document, has it load a
 * speech model, captures another tab's audio into it, and collects the
 * transcripts and timing into one report.
 *
 * URL parameters:
 *   model=<id>        engine/stt/models.mjs id (default moonshine-base)
 *   variant=<id>      configuration to run (default: the model's first)
 *   target=<prefix>   URL prefix of the tab to capture
 *   threads=N         WASM threads (default: 4 if isolated, else 1)
 *
 * Driven by scripts/run_extension_spike.mjs, which watches document.title:
 *   "live:capturing"  capture is running — start the audio now
 *   "live:done"       window.__liveSttReport is ready
 *   "live:error"      window.__liveSttReport.error says why
 * and calls window.finishLiveStt() when the audio has finished playing.
 */

import {STT_VARIANTS, getSttModel} from "../engine/stt/models.mjs";
import {wordErrorRate} from "../engine/stt/wer.mjs";

const params = new URLSearchParams(location.search);
const channel = new BroadcastChannel("stt-spike");
const OFFSCREEN_URL = "spike/offscreen_stt.html";
const REFERENCE_URL = new URL("./assets/claims_reference.txt", import.meta.url);
const MAX_WASM_THREADS = 4;

const statusElement = document.getElementById("status");
const logElement = document.getElementById("log");
const setStatus = (text) => {
  statusElement.textContent = text;
  logElement.textContent += `${text}\n`;
};

const transcripts = [];
let loaded = null;

/**
 * Wait for the next message of one of `types` from the offscreen document;
 * an "error" message rejects.
 */
const waitFor = (types, timeoutMs) =>
  new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      channel.removeEventListener("message", onMessage);
      reject(new Error(`timed out waiting for ${types.join("/")}`));
    }, timeoutMs);
    const onMessage = (event) => {
      const message = event.data;
      if (message.type === "error") {
        clearTimeout(timer);
        channel.removeEventListener("message", onMessage);
        reject(new Error(`${message.stage}: ${message.message}`));
      } else if (types.includes(message.type)) {
        clearTimeout(timer);
        channel.removeEventListener("message", onMessage);
        resolve(message);
      }
    };
    channel.addEventListener("message", onMessage);
  });

channel.addEventListener("message", (event) => {
  const message = event.data;
  if (message.type === "transcript") {
    transcripts.push(message.transcript);
    setStatus(
      `[${message.transcript.startS.toFixed(1)}s] ${message.transcript.text} ` +
        `(lag ${Math.round(message.transcript.lagMs)} ms)`
    );
  } else if (message.type === "download") {
    statusElement.textContent = `downloading model ${message.progress.percent}%`;
  }
});

const fail = (error) => {
  console.error("[stt-spike] live test failed:", error);
  window.__liveSttReport = {schema: 1, error: String(error?.message ?? error)};
  setStatus(`error: ${error?.message ?? error}`);
  document.title = "live:error";
};

const median = (values) => {
  if (values.length === 0) {
    return null;
  }
  const sorted = [...values].sort((left, right) => left - right);
  return sorted[Math.floor(sorted.length / 2)];
};

const run = async () => {
  const model = getSttModel(params.get("model") ?? "moonshine-base");
  const variantId = params.get("variant") ?? model.variants[0];
  const variant = STT_VARIANTS[variantId];
  if (!variant || !model.variants.includes(variantId)) {
    throw new Error(`Model ${model.id} has no variant "${variantId}".`);
  }

  // One offscreen document per extension: replace any that is open.
  if (await chrome.offscreen.hasDocument()) {
    await chrome.offscreen.closeDocument();
  }
  const offscreenReady = waitFor(["offscreen-ready"], 15000);
  await chrome.offscreen.createDocument({
    url: OFFSCREEN_URL,
    reasons: ["USER_MEDIA"],
    justification: "Spike: capture tab audio and transcribe it locally.",
  });
  await offscreenReady;

  setStatus(`loading ${model.label} (${variantId})…`);
  const requestedThreads = Number(params.get("threads")) || MAX_WASM_THREADS;
  const loadedPromise = waitFor(["loaded"], 10 * 60 * 1000);
  channel.postMessage({
    type: "load",
    config: {
      repo: model.repo,
      device: variant.device,
      dtype: variant.dtype,
      // The worker clamps this to 1 when it is not cross-origin isolated.
      numThreads: requestedThreads,
    },
  });
  loaded = await loadedPromise;
  setStatus(
    `model ready in ${(loaded.info.loadMs / 1000).toFixed(1)} s — ` +
      `offscreen isolated: ${loaded.environment.crossOriginIsolated}, ` +
      `threads: ${loaded.info.numThreads}, GPU: ${loaded.environment.gpu.label}`
  );

  const targetPrefix = params.get("target") ?? "http://127.0.0.1";
  const tabs = await chrome.tabs.query({});
  const targetTab = tabs.find((tab) => tab.url?.startsWith(targetPrefix));
  if (!targetTab) {
    throw new Error(`No open tab starts with ${targetPrefix}.`);
  }
  const streamId = await chrome.tabCapture.getMediaStreamId({targetTabId: targetTab.id});
  const capturing = waitFor(["capturing"], 15000);
  channel.postMessage({type: "capture", streamId});
  await capturing;
  setStatus(`capturing tab ${targetTab.id} (${targetTab.url})`);
  document.title = "live:capturing";

  window.finishLiveStt = async () => {
    try {
      const stoppedPromise = waitFor(["stopped"], 5 * 60 * 1000);
      channel.postMessage({type: "stop"});
      const stopped = await stoppedPromise;
      const reference = (await (await fetch(REFERENCE_URL)).text()).trim();
      const text = transcripts.map((transcript) => transcript.text).filter(Boolean).join(" ");
      const {stats} = stopped;
      window.__liveSttReport = {
        schema: 1,
        modelId: model.id,
        variantId,
        device: variant.device,
        extensionId: chrome.runtime.id,
        controllerIsolated: globalThis.crossOriginIsolated === true,
        environment: loaded.environment,
        load: loaded.info,
        capture: stopped.capture,
        stats,
        realTimeFactor: stats.transcribedSeconds
          ? stats.inferenceMs / (stats.transcribedSeconds * 1000)
          : null,
        medianLagMs: median(transcripts.map((transcript) => transcript.lagMs)),
        maxLagMs: stats.maxLagMs,
        wordErrorRate: wordErrorRate(reference, text),
        text,
        transcripts,
      };
      await chrome.offscreen.closeDocument();
      setStatus("finished");
      document.title = "live:done";
    } catch (error) {
      fail(error);
    }
  };
};

run().catch(fail);
