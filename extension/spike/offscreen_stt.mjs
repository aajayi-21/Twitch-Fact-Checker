/**
 * Spike S3 — offscreen document: production tab capture + local speech.
 *
 * Answers whether a WebGPU (or WASM) speech session can live in the same
 * offscreen document as the tabCapture MediaStream and AudioContext without
 * either starving the other. It reuses offscreen/audio_capture.js unchanged
 * and feeds its PCM frames to engine/stt/live_transcriber.mjs instead of the
 * backend WebSocket.
 *
 * Controlled by spike/live_stt.mjs over BroadcastChannel "stt-spike" (kept
 * off chrome.runtime messaging so the production service worker never sees
 * spike traffic):
 *
 *   in   {type: "load", config}      -> {type: "loaded", info, environment}
 *   in   {type: "capture", streamId} -> {type: "capturing"}
 *   in   {type: "stop"}              -> {type: "stopped", stats, capture}
 *   out  {type: "transcript", transcript} / {type: "download", progress}
 *   out  {type: "error", stage, message}
 */

import {LiveTranscriber} from "../engine/stt/live_transcriber.mjs";
import {describeGpu, probeGpu} from "../engine/stt/gpu_probe.mjs";
import {AudioCapture} from "../offscreen/audio_capture.js";

const channel = new BroadcastChannel("stt-spike");
const post = (message) => channel.postMessage(message);

/** @type {LiveTranscriber|null} */
let transcriber = null;
/** @type {AudioCapture|null} */
let capture = null;

// Capture-continuity bookkeeping: frames should arrive every 250 ms. A gap
// well beyond that means the document was starved while the model ran.
const FRAME_INTERVAL_MS = 250;
let captureStartedAt = 0;
let lastFrameAt = 0;
let maxFrameGapMs = 0;
let lateFrames = 0;
let captureEndReason = null;

const handleFrame = (pcmFrame) => {
  const now = performance.now();
  if (lastFrameAt > 0) {
    const gap = now - lastFrameAt;
    maxFrameGapMs = Math.max(maxFrameGapMs, gap);
    if (gap > FRAME_INTERVAL_MS * 2) {
      lateFrames += 1;
    }
  }
  lastFrameAt = now;
  transcriber?.pushPcm16(pcmFrame);
};

const handleLoad = async (config) => {
  transcriber = new LiveTranscriber({
    onTranscript: (transcript) => post({type: "transcript", transcript}),
    onDownload: (progress) => post({type: "download", progress}),
    onFatal: (message) => post({type: "error", stage: "session", message}),
  });
  const info = await transcriber.load(config);
  const gpu = await probeGpu();
  post({
    type: "loaded",
    info,
    environment: {
      crossOriginIsolated: globalThis.crossOriginIsolated === true,
      hardwareConcurrency: navigator.hardwareConcurrency ?? null,
      gpu: {...gpu, label: describeGpu(gpu)},
      userAgent: navigator.userAgent,
    },
  });
};

const handleCapture = async (streamId) => {
  capture = new AudioCapture({
    onFrame: handleFrame,
    onEnded: (reason) => {
      captureEndReason = reason;
      post({type: "error", stage: "capture", message: `tab audio ended (${reason})`});
    },
    onVideoFrame: () => {},
    shouldSendFrame: () => false,
  });
  await capture.start(streamId, {captureVideo: false});
  captureStartedAt = performance.now();
  post({type: "capturing"});
};

const handleStop = async () => {
  const capturedMs = performance.now() - captureStartedAt;
  await capture?.stop();
  capture = null;
  await transcriber?.finish();
  const stats = {...transcriber.stats};
  transcriber.dispose();
  transcriber = null;
  post({
    type: "stopped",
    stats,
    capture: {
      capturedSeconds: capturedMs / 1000,
      receivedSeconds: stats.samplesReceived / 16000,
      maxFrameGapMs,
      lateFrames,
      endReason: captureEndReason,
    },
  });
};

channel.addEventListener("message", async (event) => {
  const message = event.data;
  const handlers = {
    load: () => handleLoad(message.config),
    capture: () => handleCapture(message.streamId),
    stop: handleStop,
  };
  const handler = handlers[message.type];
  if (!handler) {
    return;
  }
  try {
    await handler();
  } catch (error) {
    console.error(`[stt-spike] ${message.type} failed:`, error);
    post({type: "error", stage: message.type, message: String(error?.message ?? error)});
  }
});

post({type: "offscreen-ready"});
