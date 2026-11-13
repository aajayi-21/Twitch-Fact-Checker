/**
 * The no-key demo: play a bundled 43-second clip through the real speech
 * model on this computer, and through the real claim pipeline with recorded
 * answers in place of the two paid model calls.
 *
 * What is real: the speech-to-text (the model the viewer just downloaded and
 * tested), and every pipeline rule. What is recorded: which claims the gate
 * model found and what the verify model concluded — captured from a real run
 * (engine/demo/recording.mjs). The screen says so.
 *
 * Nothing is sent anywhere, nothing is written to the history, and no key is
 * needed. It exists so that someone without a key — a first-time viewer, or
 * a store reviewer — can see the product work.
 */

import {createDemoProvider} from "../../engine/demo/demo_provider.mjs";
import {DEMO_RECORDING} from "../../engine/demo/recording.mjs";
import {BrowserPipeline} from "../../engine/pipeline/browser_pipeline.mjs";
import {LiveTranscriber} from "../../engine/stt/live_transcriber.mjs";
import {SPEECH_STATUS, SessionSpeech} from "../../engine/stt/session_speech.mjs";
import {beginGpuAttempt, blockGpu, endGpuAttempt} from "../../engine/stt/speech_setup.mjs";
import {updateSpeechState} from "../../engine/stt/speech_store.mjs";

const DEMO_CLIP_URL = new URL("../../engine/demo/demo_16k.wav", import.meta.url);

const SAMPLE_RATE = 16000;
const FRAME_SAMPLES = SAMPLE_RATE / 4; // 250 ms, as the capture path sends
// Feed the clip faster than real time: nobody should wait 43 s for a demo,
// and the model keeps up at several times real time or it failed its test.
const FRAME_INTERVAL_MS = 25;
const WAV_HEADER_BYTES = 44;

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export {DEMO_RECORDING};

/**
 * Run the demo once.
 *
 * @param {object} options
 * @param {object} options.config - the tested speech configuration
 *   (speech_setup.mjs planBrowserSpeech)
 * @param {(frame: object) => void} options.onFrame - receives "transcript",
 *   "status", "claim" and "verdict" frames as they happen
 * @returns {Promise<void>} resolves when the clip has been fully processed
 * @throws {Error} when the clip is missing or speech cannot run
 */
export const runDemo = async ({config, onFrame}) => {
  const response = await fetch(DEMO_CLIP_URL);
  if (!response.ok) {
    throw new Error("The demo clip is missing from this copy of the extension.");
  }
  const pcm = (await response.arrayBuffer()).slice(WAV_HEADER_BYTES);

  const provider = createDemoProvider(DEMO_RECORDING);
  const stage = {provider, apiKey: "", model: DEMO_RECORDING.model};
  const pipeline = new BrowserPipeline({
    gate: stage,
    verify: stage,
    // Usage frames are all zeros here (nothing was called); keep them out.
    emit: (frame) => frame.type !== "usage" && onFrame(frame),
    budgets: {dailyChecks: 0, monthlyUsd: 0},
    spent: {checksToday: 0, spendThisMonthUsd: 0},
    // The rate limit protects a provider; there is none here.
    tuning: {verifyPerMinute: 6000, verifyBurst: 10},
  });

  let failure = null;
  const speech = new SessionSpeech({
    config,
    // The whole clip is queued within a second, so the "falling behind"
    // drop policy must not apply.
    createTranscriber: (callbacks, options) =>
      new LiveTranscriber(callbacks, {...options, maxBacklogS: 600}),
    onTranscript: (transcript) => {
      const text = transcript.text.trim();
      if (text) {
        onFrame({type: "transcript", text, start: transcript.startS, end: transcript.endS});
        pipeline.addTranscript(text);
      }
    },
    onGpuAttempt: ({phase, variantId}) =>
      updateSpeechState((state) =>
        phase === "begin"
          ? beginGpuAttempt(state, {context: "demo", variantId, at: new Date().toISOString()})
          : endGpuAttempt(state)
      ),
    onGpuFailed: (reason) => {
      updateSpeechState((state) => blockGpu(state, {reason, at: new Date().toISOString()}));
    },
    onFailed: (reason) => {
      failure = reason;
    },
  });

  try {
    await speech.start();
    if (speech.status === SPEECH_STATUS.FAILED) {
      throw new Error(failure ?? "Speech recognition could not start.");
    }
    pipeline.start();
    const frameBytes = FRAME_SAMPLES * 2;
    for (let offset = 0; offset + frameBytes <= pcm.byteLength; offset += frameBytes) {
      speech.pushFrame(pcm.slice(offset, offset + frameBytes));
      await pause(FRAME_INTERVAL_MS);
    }
    await speech.finish(120_000);
    if (failure) {
      throw new Error(failure);
    }
    await pipeline.stop();
  } finally {
    speech.dispose();
    pipeline.dispose();
  }
};
