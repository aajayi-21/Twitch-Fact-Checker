/**
 * Live transcriber: the main-thread side of local speech-to-text.
 *
 * Owns the transcription worker and the streaming segmenter. Feed it the
 * 16 kHz PCM frames that offscreen/audio_capture.js produces and it calls
 * back with a transcript per utterance:
 *
 *   capture frame ─▶ segmenter ─▶ queue ─▶ worker (model) ─▶ onTranscript
 *
 * A live session cannot fall behind forever, so the queue is bounded in
 * seconds of audio. When the model cannot keep up, the OLDEST waiting
 * utterance is dropped and counted — a fact-check that arrives a minute late
 * is worthless, and the backend's pipeline makes the same trade
 * (stt_drop_counts).
 */

import {StreamSegmenter} from "./stream_segmenter.mjs";

const WORKER_URL = new URL("./stt_worker.mjs", import.meta.url);

const INT16_SCALE = 32768;

// Bound on audio waiting for the model. Roughly one long utterance plus one
// in flight; beyond this the session is not keeping up.
const DEFAULT_MAX_BACKLOG_S = 20;

/**
 * @typedef {object} LiveTranscript
 * @property {string} text
 * @property {number} startS - utterance start, seconds into the stream
 * @property {number} endS
 * @property {number} inferenceMs - model time
 * @property {number} waitMs - time spent queued behind earlier utterances
 * @property {number} lagMs - from the utterance closing to its transcript
 * @property {boolean} forced - cut by the length cap rather than a pause
 */

export class LiveTranscriber {
  /**
   * @param {object} callbacks
   * @param {(transcript: LiveTranscript) => void} callbacks.onTranscript
   * @param {(progress: object) => void} [callbacks.onDownload]
   * @param {(message: string) => void} [callbacks.onFatal] - the worker died
   *   or lost its GPU; the session must be restarted (on another rung)
   * @param {object} [options]
   * @param {number} [options.maxBacklogS]
   * @param {object} [options.segmenter] - StreamSegmenter options
   */
  constructor({onTranscript, onDownload, onFatal}, {maxBacklogS = DEFAULT_MAX_BACKLOG_S, segmenter} = {}) {
    this.onTranscript = onTranscript;
    this.onDownload = onDownload ?? (() => {});
    this.onFatal = onFatal ?? (() => {});
    this.maxBacklogS = maxBacklogS;
    this.segmenter = new StreamSegmenter(segmenter);
    this.worker = null;
    /** Utterances waiting for the model, oldest first. */
    this.waiting = [];
    /** The utterance the worker is transcribing, if any. */
    this.inFlight = null;
    this.nextId = 1;
    this.idleResolvers = [];
    this.stats = {
      framesReceived: 0,
      samplesReceived: 0,
      segmentsClosed: 0,
      segmentsTranscribed: 0,
      segmentsDropped: 0,
      droppedSeconds: 0,
      utteranceErrors: 0,
      inferenceMs: 0,
      transcribedSeconds: 0,
      maxLagMs: 0,
      maxBacklogS: 0,
    };
  }

  /**
   * Start the worker and load the model.
   *
   * @param {import("./stt_runtime.mjs").TranscriberConfig} config
   * @returns {Promise<object>} load info (timings, threads, GPU used)
   * @throws {Error} when the model cannot be loaded in this configuration
   */
  load(config) {
    return new Promise((resolve, reject) => {
      this.worker = new Worker(WORKER_URL, {type: "module"});
      let loaded = false;
      this.worker.addEventListener("message", (event) => {
        const message = event.data;
        if (message.type === "download") {
          this.onDownload(message.progress);
        } else if (message.type === "ready") {
          loaded = true;
          resolve(message.info);
        } else if (message.type === "transcript") {
          this.#handleTranscript(message);
        } else if (message.type === "error") {
          this.#handleWorkerError(message, loaded, reject);
        }
      });
      this.worker.addEventListener("error", (event) => {
        const text = `speech worker error: ${event.message || "failed to start"}`;
        if (loaded) {
          this.onFatal(text);
        } else {
          reject(new Error(text));
        }
      });
      this.worker.postMessage({type: "load", config});
    });
  }

  /**
   * Feed one capture frame: 16-bit little-endian mono PCM at 16 kHz, the
   * exact buffer AudioCapture hands to `onFrame`.
   *
   * @param {ArrayBuffer} pcmFrame
   */
  pushPcm16(pcmFrame) {
    const int16 = new Int16Array(pcmFrame);
    const samples = new Float32Array(int16.length);
    for (let index = 0; index < int16.length; index += 1) {
      samples[index] = int16[index] / INT16_SCALE;
    }
    this.stats.framesReceived += 1;
    this.stats.samplesReceived += samples.length;
    this.#enqueue(this.segmenter.push(samples));
  }

  /**
   * End of stream: transcribe whatever is left and wait for it.
   *
   * @returns {Promise<void>} resolves when nothing is queued or in flight
   */
  finish() {
    this.#enqueue(this.segmenter.flush());
    if (!this.inFlight && this.waiting.length === 0) {
      return Promise.resolve();
    }
    return new Promise((resolve) => this.idleResolvers.push(resolve));
  }

  /** Stop immediately and release the worker (and its GPU device). */
  dispose() {
    this.worker?.postMessage({type: "dispose"});
    // Give the worker a moment to free the model, then make sure it is gone.
    const worker = this.worker;
    setTimeout(() => worker?.terminate(), 500);
    this.worker = null;
    this.waiting = [];
    this.inFlight = null;
  }

  /** @param {import("./stream_segmenter.mjs").StreamSegment[]} segments */
  #enqueue(segments) {
    for (const segment of segments) {
      this.stats.segmentsClosed += 1;
      this.waiting.push({
        id: this.nextId,
        segment,
        seconds: segment.endS - segment.startS,
        closedAt: performance.now(),
      });
      this.nextId += 1;
    }
    let backlogS = this.waiting.reduce((sum, item) => sum + item.seconds, 0);
    this.stats.maxBacklogS = Math.max(this.stats.maxBacklogS, backlogS);
    while (backlogS > this.maxBacklogS && this.waiting.length > 1) {
      const dropped = this.waiting.shift();
      backlogS -= dropped.seconds;
      this.stats.segmentsDropped += 1;
      this.stats.droppedSeconds += dropped.seconds;
    }
    this.#pump();
  }

  #pump() {
    if (this.inFlight || !this.worker) {
      return;
    }
    const next = this.waiting.shift();
    if (!next) {
      this.idleResolvers.splice(0).forEach((resolve) => resolve());
      return;
    }
    this.inFlight = {...next, sentAt: performance.now()};
    const {samples} = next.segment;
    this.worker.postMessage({type: "transcribe", id: next.id, samples}, [samples.buffer]);
  }

  /** @param {{id: number, text: string, inferenceMs: number}} message */
  #handleTranscript({id, text, inferenceMs}) {
    const item = this.inFlight;
    if (!item || item.id !== id) {
      return; // stale reply after dispose()
    }
    this.inFlight = null;
    const lagMs = performance.now() - item.closedAt;
    this.stats.segmentsTranscribed += 1;
    this.stats.inferenceMs += inferenceMs;
    this.stats.transcribedSeconds += item.seconds;
    this.stats.maxLagMs = Math.max(this.stats.maxLagMs, lagMs);
    this.onTranscript({
      text,
      startS: item.segment.startS,
      endS: item.segment.endS,
      inferenceMs,
      waitMs: item.sentAt - item.closedAt,
      lagMs,
      forced: item.segment.forced,
    });
    this.#pump();
  }

  /**
   * @param {{id?: number, fatal: boolean, message: string}} message
   * @param {boolean} loaded
   * @param {(error: Error) => void} rejectLoad
   */
  #handleWorkerError(message, loaded, rejectLoad) {
    if (!loaded) {
      rejectLoad(new Error(message.message));
      return;
    }
    if (message.fatal) {
      this.onFatal(message.message);
      return;
    }
    // One utterance failed; skip it and keep the session alive.
    console.warn("[fact-checker] utterance transcription failed:", message.message);
    this.stats.utteranceErrors += 1;
    this.inFlight = null;
    this.#pump();
  }
}
