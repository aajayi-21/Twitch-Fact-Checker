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
 *     on, and the GPU is reported as failed so it is not tried again;
 *   - a download that fails (or stalls) is not the GPU's fault: it is tried
 *     once more on the same device, then reported, and the GPU stays usable.
 *
 * A loaded model outlives its session: `park()` keeps it in memory between
 * sessions and `adopt()` hands it to the next one, so stopping and starting
 * again does not rebuild it (docs/live-settings-and-model-lifecycle.md §1).
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
 * @property {boolean} reused - this session took over a model loaded by an
 *   earlier one, so nothing was loaded for it
 */

/**
 * Load errors that come from fetching the model files rather than from the
 * device: a dropped connection, a missing file, a stalled download. The
 * wording comes from the browser's fetch, transformers.js and LiveTranscriber.
 */
const DOWNLOAD_FAILURE =
  /failed to fetch|networkerror|network error|load failed|could not locate file|err_internet|err_network|http(s)? error|status( code)? \d{3}|no progress loading/i;

/** @param {string} reason @returns {boolean} */
export const isDownloadFailure = (reason) => DOWNLOAD_FAILURE.test(String(reason ?? ""));

/** How many times a failed download is retried before the session gives up. */
const DOWNLOAD_RETRIES = 1;

/**
 * What identifies a loaded model: two configurations with the same key run
 * the same model in the same way, so a loaded one can be reused.
 *
 * @param {object} config - from speech_setup.mjs planBrowserSpeech
 * @returns {string}
 */
export const speechConfigKey = (config) =>
  JSON.stringify([
    config?.repo,
    config?.revision,
    config?.device,
    config?.dtype,
    config?.numThreads,
    config?.powerPreference,
    config?.maxSegmentS,
  ]);

const ignore = () => {};

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
    this.downloadRetries = 0;
    this.status = SPEECH_STATUS.LOADING;
    /** The message of the last status report (why it is degraded). */
    this.statusMessage = null;
    /** Whether the current session adopted an already-loaded model. */
    this.reused = false;
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

  /** Whether the loaded model can be parked and handed to another session. */
  get reusable() {
    return (
      !this.disposed &&
      this.transcriber !== null &&
      (this.status === SPEECH_STATUS.READY || this.status === SPEECH_STATUS.DEGRADED)
    );
  }

  /** @returns {string} see speechConfigKey; the configuration now loaded */
  get configKey() {
    return speechConfigKey(this.config);
  }

  /**
   * The session ended: keep the model loaded, but stop reporting to it and
   * forget whatever it was in the middle of hearing.
   */
  park() {
    this.onTranscript = ignore;
    this.onStatus = ignore;
    this.onFailed = ignore;
    this.transcriber?.resetStream();
    this.retired = {segmentsDropped: 0, droppedSeconds: 0, segmentsTranscribed: 0};
  }

  /**
   * A new session takes over the parked model: its callbacks, a fresh
   * stream, and an immediate status report (no load).
   *
   * @param {object} callbacks - as for the constructor
   */
  adopt({onTranscript, onStatus = ignore, onGpuAttempt = ignore, onGpuFailed = ignore, onFailed = ignore}) {
    this.onTranscript = onTranscript;
    this.onStatus = onStatus;
    this.onGpuAttempt = onGpuAttempt;
    this.onGpuFailed = onGpuFailed;
    this.onFailed = onFailed;
    this.downloadRetries = 0;
    this.reused = true;
    this.transcriber.resetStream();
    this.#report(this.status, {message: this.statusMessage});
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
    this.statusMessage = message;
    this.onStatus({
      status,
      modelLabel: this.config.modelLabel,
      device: this.config.device,
      downloadPercent,
      message,
      reused: this.reused,
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
    if (loadError && isDownloadFailure(loadError)) {
      await this.#downloadFailed(loadError, degradedBecause);
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

  /** Try the same download again; after that, say so and stop. */
  async #downloadFailed(loadError, degradedBecause) {
    this.#retire();
    if (this.downloadRetries < DOWNLOAD_RETRIES) {
      this.downloadRetries += 1;
      await this.#load({degradedBecause});
      return;
    }
    const reason =
      `The speech model could not be downloaded (${loadError}). ` +
      "Check the internet connection, then press Start again.";
    this.#report(SPEECH_STATUS.FAILED, {message: reason});
    this.onFailed(reason);
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
