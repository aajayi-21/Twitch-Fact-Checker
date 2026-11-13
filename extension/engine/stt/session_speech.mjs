/**
 * In-browser speech for one capture session.
 *
 * Wraps the live transcriber with the two things a real session needs that
 * a benchmark does not:
 *
 *   - the crash guard: the caller is told just before the GPU is first used
 *     and again when that use returns, so a browser that dies in between is
 *     noticed at the next start (speech_setup.mjs applyCrashGuard);
 *   - recovery: if the model cannot be loaded on the GPU, or the GPU is lost
 *     mid-session, transcription moves to the model's CPU build and carries
 *     on, and the GPU is reported as failed so it is not tried again.
 *
 * Audio may be pushed from the moment `start()` is called: frames that
 * arrive while the model is still loading wait in the worker's queue (and
 * the oldest are dropped if the wait grows too long — see LiveTranscriber).
 *
 * No chrome.* or DOM APIs: the transcriber is injected, so the recovery
 * logic is unit-tested under Node.
 */

import {cpuFallbackConfig} from "./speech_setup.mjs";

export const SPEECH_STATUS = Object.freeze({
  LOADING: "loading", // downloading or loading the model
  READY: "ready",
  DEGRADED: "degraded", // running on the CPU after the GPU failed
  FAILED: "failed", // nothing left to try; the session cannot transcribe
});

/**
 * @typedef {object} SpeechStatus
 * @property {string} status - SPEECH_STATUS.*
 * @property {string} modelLabel
 * @property {"webgpu"|"wasm"} device
 * @property {number|null} downloadPercent - while loading, when known
 * @property {string|null} message - why it is degraded or failed
 */

export class SessionSpeech {
  /**
   * @param {object} options
   * @param {object} options.config - from speech_setup.mjs planBrowserSpeech
   * @param {(callbacks: object, options: object) => object} options.createTranscriber
   *   builds a LiveTranscriber (injected for tests)
   * @param {(transcript: object) => void} options.onTranscript
   * @param {(status: SpeechStatus) => void} [options.onStatus]
   * @param {(attempt: {phase: "begin"|"end", variantId: string}) => Promise<void>|void} [options.onGpuAttempt]
   *   awaited for "begin": the marker must be stored before the GPU is touched
   * @param {(reason: string) => void} [options.onGpuFailed] - the GPU must
   *   not be used again until the viewer re-tests
   * @param {(reason: string) => void} [options.onFailed] - unrecoverable
   */
  constructor({
    config,
    createTranscriber,
    onTranscript,
    onStatus = () => {},
    onGpuAttempt = () => {},
    onGpuFailed = () => {},
    onFailed = () => {},
  }) {
    this.config = config;
    this.createTranscriber = createTranscriber;
    this.onTranscript = onTranscript;
    this.onStatus = onStatus;
    this.onGpuAttempt = onGpuAttempt;
    this.onGpuFailed = onGpuFailed;
    this.onFailed = onFailed;
    this.transcriber = null;
    this.disposed = false;
    this.status = SPEECH_STATUS.LOADING;
    /** Totals across every transcriber this session has used. */
    this.retired = {segmentsDropped: 0, droppedSeconds: 0, segmentsTranscribed: 0};
  }

  /**
   * Load the model. Resolves when speech is ready (or degraded, or failed) —
   * it never rejects; the outcome arrives through the callbacks.
   *
   * @returns {Promise<void>}
   */
  start() {
    return this.#load({degradedBecause: null});
  }

  /**
   * One capture frame: 16-bit mono PCM at 16 kHz.
   *
   * @param {ArrayBuffer} pcmFrame
   */
  pushFrame(pcmFrame) {
    if (!this.disposed && this.status !== SPEECH_STATUS.FAILED) {
      this.transcriber?.pushPcm16(pcmFrame);
    }
  }

  /**
   * End of the stream: transcribe what is left, but do not wait forever —
   * the viewer pressed Stop.
   *
   * @param {number} timeoutMs
   * @returns {Promise<void>}
   */
  async finish(timeoutMs) {
    if (!this.transcriber || this.status === SPEECH_STATUS.FAILED) {
      return;
    }
    let timer;
    await Promise.race([
      this.transcriber.finish(),
      new Promise((resolve) => {
        timer = setTimeout(resolve, timeoutMs);
      }),
    ]);
    clearTimeout(timer);
  }

  /** Release the model and the worker. Safe to call more than once. */
  dispose() {
    this.disposed = true;
    this.#retire();
  }

  /** @returns {{segmentsDropped: number, droppedSeconds: number, segmentsTranscribed: number}} */
  get stats() {
    const live = this.transcriber?.stats ?? {};
    return {
      segmentsDropped: this.retired.segmentsDropped + (live.segmentsDropped ?? 0),
      droppedSeconds: this.retired.droppedSeconds + (live.droppedSeconds ?? 0),
      segmentsTranscribed: this.retired.segmentsTranscribed + (live.segmentsTranscribed ?? 0),
    };
  }

  #retire() {
    if (!this.transcriber) {
      return;
    }
    const {stats} = this;
    this.retired = stats;
    this.transcriber.dispose();
    this.transcriber = null;
  }

  #report(status, {downloadPercent = null, message = null} = {}) {
    this.status = status;
    this.onStatus({
      status,
      modelLabel: this.config.modelLabel,
      device: this.config.device,
      downloadPercent,
      message,
    });
  }

  async #load({degradedBecause}) {
    const config = this.config;
    const usesGpu = config.device === "webgpu";
    this.#report(SPEECH_STATUS.LOADING, {message: degradedBecause});
    if (usesGpu) {
      // Stored BEFORE the GPU is touched: this is what survives a crash.
      await this.onGpuAttempt({phase: "begin", variantId: config.variantId});
    }
    if (this.disposed) {
      return;
    }
    const transcriber = this.createTranscriber(
      {
        onTranscript: (transcript) => {
          if (!this.disposed && this.transcriber === transcriber) {
            this.onTranscript(transcript);
          }
        },
        onDownload: (progress) => {
          if (!this.disposed && this.status === SPEECH_STATUS.LOADING) {
            this.#report(SPEECH_STATUS.LOADING, {
              downloadPercent: progress?.percent ?? null,
              message: degradedBecause,
            });
          }
        },
        onFatal: (reason) => {
          if (this.transcriber === transcriber) {
            this.#recover(reason);
          }
        },
      },
      {segmenter: {maxSegmentS: config.maxSegmentS}}
    );
    this.transcriber = transcriber;
    let loadError = null;
    try {
      await transcriber.load({
        repo: config.repo,
        revision: config.revision,
        device: config.device,
        dtype: config.dtype,
        numThreads: config.numThreads,
        powerPreference: config.powerPreference,
      });
    } catch (error) {
      loadError = String(error?.message ?? error);
    }
    if (usesGpu) {
      // It returned — cleanly or with an error — so the browser survived.
      await this.onGpuAttempt({phase: "end", variantId: config.variantId});
    }
    if (this.disposed || this.transcriber !== transcriber) {
      return;
    }
    if (loadError) {
      await this.#recover(`The speech model could not be loaded: ${loadError}`);
      return;
    }
    this.#report(degradedBecause ? SPEECH_STATUS.DEGRADED : SPEECH_STATUS.READY, {
      message: degradedBecause,
    });
  }

  async #recover(reason) {
    if (this.disposed || this.status === SPEECH_STATUS.FAILED) {
      return;
    }
    const failedOnGpu = this.config.device === "webgpu";
    const fallback = cpuFallbackConfig(this.config);
    this.#retire();
    if (failedOnGpu) {
      this.onGpuFailed(reason);
    }
    if (!fallback) {
      this.#report(SPEECH_STATUS.FAILED, {message: reason});
      this.onFailed(reason);
      return;
    }
    this.config = fallback;
    await this.#load({degradedBecause: reason});
  }
}
