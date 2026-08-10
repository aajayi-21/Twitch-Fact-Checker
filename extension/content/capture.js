/**
 * Firefox capture engine — taps the page's own <video> element.
 *
 * Firefox implements neither chrome.tabCapture nor the offscreen API, so the
 * Chrome path (stream id -> getUserMedia in an offscreen document) has no
 * equivalent. Instead this content script routes the page's media element
 * through an AudioContext, which needs no permission at all because the
 * element is already in our document.
 *
 * Audio graph (mirrors offscreen/audio_capture.js so both browsers feed the
 * backend byte-identical PCM):
 *
 *   source ─→ ctx.destination                 (loopback: MANDATORY — see below)
 *   source ─→ Biquad(lowpass 7 kHz, Q 0.54)
 *          ─→ Biquad(lowpass 7 kHz, Q 1.31)   (4th-order Butterworth anti-alias)
 *          ─→ pcm-capture worklet             (decimates 48 k → 16 k)
 *
 * Three constraints drive the unusual bits:
 *
 * 1. `createMediaElementSource()` REROUTES the element's audio into the
 *    graph. If the source is not connected to `ctx.destination`, the page
 *    goes silent — permanently, because the rerouting cannot be undone. So
 *    the AudioContext and the source node are created ONCE per element and
 *    deliberately never torn down; stopping only detaches the analysis
 *    branch.
 * 2. `createMediaElementSource()` throws if called twice for one element, so
 *    source nodes are memoized per element.
 * 3. The AudioWorklet module is an extension URL loaded into a page-context
 *    AudioContext, which some sites' CSP can refuse. A ScriptProcessorNode
 *    fallback keeps capture working there (deprecated, but it is the only
 *    universally available option and this branch is Firefox-only).
 *
 * Cross-origin note: MSE players (Twitch, YouTube, Kick, Rumble) feed the
 * element from a same-origin `blob:` MediaSource, so the graph receives real
 * audio. A plain cross-origin <video> without CORS headers would yield
 * silence — that is a browser rule we cannot bypass, and it does not apply
 * to the supported sites.
 *
 * Classic content script: defines `self.PageAudioCapture` (no imports).
 */

"use strict";

(() => {
  // Promise-safe API namespace (see shared/capabilities.js § namespace idiom).
  const chrome = globalThis.browser ?? globalThis.chrome;

  const CONTEXT_SAMPLE_RATE = 48000;
  const TARGET_SAMPLE_RATE = 16000;
  const DECIMATION_FACTOR = CONTEXT_SAMPLE_RATE / TARGET_SAMPLE_RATE; // 3
  const FRAME_SAMPLES = 4000; // 250 ms at 16 kHz — one backend frame
  const ANTI_ALIAS_CUTOFF_HZ = 7000;
  const BUTTERWORTH_Q_STAGE_ONE = 0.54;
  const BUTTERWORTH_Q_STAGE_TWO = 1.31;
  const WORKLET_BLOCK_SAMPLES = 512;
  const SCRIPT_PROCESSOR_BUFFER = 4096;
  // How often to re-check that the captured element is still in the DOM
  // (SPA navigation swaps the player wholesale on these sites).
  const REATTACH_INTERVAL_MS = 3000;

  const VIDEO_FRAME_INTERVAL_MS = 5000;
  const VIDEO_MAX_HEIGHT = 480;
  const VIDEO_JPEG_QUALITY = 0.7;

  /**
   * ONE AudioContext for the page lifetime. Closing it would strand every
   * element whose audio we rerouted (constraint 1 above), so it is created
   * lazily and then kept forever.
   */
  let sharedContext = null;
  /** element -> MediaElementAudioSourceNode (constraint 2 above). */
  const sourceNodes = new WeakMap();

  const getSharedContext = () => {
    if (sharedContext === null) {
      sharedContext = new AudioContext({sampleRate: CONTEXT_SAMPLE_RATE});
    }
    return sharedContext;
  };

  /**
   * The element's source node, created on first use and reconnected to the
   * speakers every time (idempotent: repeat connects to the same destination
   * are no-ops).
   *
   * @param {HTMLMediaElement} element
   * @returns {MediaElementAudioSourceNode}
   */
  const getSourceNode = (element) => {
    const context = getSharedContext();
    let source = sourceNodes.get(element);
    if (!source) {
      source = context.createMediaElementSource(element);
      sourceNodes.set(element, source);
    }
    source.connect(context.destination); // keep the page audible
    return source;
  };

  /**
   * Chunked base64 — String.fromCharCode has an argument-count limit well
   * below a full frame, and runtime messaging is JSON-serialized so binary
   * cannot travel as-is.
   *
   * @param {ArrayBuffer} buffer
   * @returns {string}
   */
  const encodeBase64 = (buffer) => {
    const bytes = new Uint8Array(buffer);
    const chunks = [];
    const CHUNK = 0x8000;
    for (let index = 0; index < bytes.length; index += CHUNK) {
      chunks.push(String.fromCharCode(...bytes.subarray(index, index + CHUNK)));
    }
    return btoa(chunks.join(""));
  };

  /**
   * Convert Float32 samples in [-1, 1] to Int16 little-endian, matching the
   * Chrome path's encoder exactly (DataView keeps the byte order explicit
   * rather than platform-dependent).
   *
   * @param {Float32Array} samples
   * @returns {ArrayBuffer}
   */
  const encodeInt16Le = (samples) => {
    const buffer = new ArrayBuffer(samples.length * 2);
    const view = new DataView(buffer);
    for (let index = 0; index < samples.length; index += 1) {
      const clamped = Math.max(-1, Math.min(1, samples[index]));
      view.setInt16(
        index * 2,
        clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff,
        true
      );
    }
    return buffer;
  };

  /**
   * The page's main media element: the largest <video> that has real
   * dimensions, preferring one that is actually playing.
   *
   * @returns {HTMLVideoElement|null}
   */
  const findVideoElement = () => {
    const videos = [...document.querySelectorAll("video")].filter(
      (video) => video.videoWidth > 0 || video.readyState > 0
    );
    if (videos.length === 0) {
      return null;
    }
    const score = (video) => {
      const area = (video.clientWidth || 0) * (video.clientHeight || 0);
      return (video.paused ? 0 : 1e9) + area;
    };
    return videos.reduce((best, video) => (score(video) > score(best) ? video : best));
  };

  class PageAudioCapture {
    /**
     * @param {object} callbacks
     * @param {(pcmB64: string) => void} callbacks.onFrame - one 250 ms
     *   base64 Int16LE PCM frame.
     * @param {(frame: {imageB64: string, capturedAtMs: number}) => void}
     *   [callbacks.onVideoFrame]
     * @param {() => boolean} [callbacks.shouldSendFrame] - cheap pre-check;
     *   false skips the JPEG encode entirely.
     */
    constructor({onFrame, onVideoFrame, shouldSendFrame}) {
      this.onFrame = onFrame;
      this.onVideoFrame = onVideoFrame ?? null;
      this.shouldSendFrame = shouldSendFrame ?? null;
      this.videoElement = null;
      this.source = null;
      this.filterHead = null;
      this.captureNode = null;
      this.silentSink = null;
      this.analysisNodes = [];
      this.frameBuffer = new Float32Array(FRAME_SAMPLES);
      this.frameOffset = 0;
      this.decimationPhase = 0;
      this.stopped = false;
      this.frameTimerId = null;
      this.reattachTimerId = null;
      this.frameCanvas = null;
      this.frameInFlight = false;
    }

    /**
     * Attach to the page's media element and start emitting PCM frames.
     *
     * @param {{captureVideo?: boolean}} [options]
     * @throws {Error} when no usable <video> exists or the context cannot
     *   run at 48 kHz (the decimation assumes it).
     */
    async start({captureVideo = false} = {}) {
      const video = findVideoElement();
      if (!video) {
        throw new Error("no <video> element found on this page");
      }
      const context = getSharedContext();
      if (context.sampleRate !== CONTEXT_SAMPLE_RATE) {
        throw new Error(
          `AudioContext runs at ${context.sampleRate} Hz, expected ` +
            `${CONTEXT_SAMPLE_RATE} Hz`
        );
      }
      // Autoplay policy can hand back a suspended context. This check MUST
      // happen before the source node exists: createMediaElementSource
      // reroutes the element's audio into this context, so attaching to a
      // suspended one would silence the stream the user is watching.
      if (context.state === "suspended") {
        await context.resume().catch(() => {});
      }
      if (context.state !== "running") {
        throw new Error(
          `AudioContext is ${context.state}; refusing to attach (it would ` +
            "mute the page)"
        );
      }
      await this.attachTo(video);
      this.reattachTimerId = setInterval(() => {
        this.ensureAttached().catch((error) => {
          console.warn("[fact-checker] re-attach failed:", error);
        });
      }, REATTACH_INTERVAL_MS);
      if (captureVideo) {
        this.frameTimerId = setInterval(() => {
          this.captureVideoFrame().catch((error) => {
            console.warn("[fact-checker] frame capture failed:", error);
          });
        }, VIDEO_FRAME_INTERVAL_MS);
      }
    }

    /** Build the analysis branch for one element. */
    async attachTo(video) {
      const context = getSharedContext();
      this.videoElement = video;
      this.source = getSourceNode(video);

      const stageOne = new BiquadFilterNode(context, {
        type: "lowpass",
        frequency: ANTI_ALIAS_CUTOFF_HZ,
        Q: BUTTERWORTH_Q_STAGE_ONE,
      });
      const stageTwo = new BiquadFilterNode(context, {
        type: "lowpass",
        frequency: ANTI_ALIAS_CUTOFF_HZ,
        Q: BUTTERWORTH_Q_STAGE_TWO,
      });
      this.captureNode = await this.createCaptureNode(context);
      this.source.connect(stageOne);
      stageOne.connect(stageTwo);
      stageTwo.connect(this.captureNode);
      this.filterHead = stageOne;
      // Everything the analysis branch owns, so teardown can unwind it
      // exactly (and never touch source -> destination).
      this.analysisNodes = [stageOne, stageTwo, this.captureNode, this.silentSink];
    }

    /**
     * An AudioWorkletNode when the page lets us load the worklet module,
     * otherwise a ScriptProcessorNode. Both deliver already-decimated
     * 16 kHz Float32 samples to `appendSamples`.
     */
    async createCaptureNode(context) {
      try {
        await context.audioWorklet.addModule(
          chrome.runtime.getURL("offscreen/pcm_worklet.js")
        );
        const node = new AudioWorkletNode(context, "pcm-capture", {
          numberOfInputs: 1,
          numberOfOutputs: 0,
          channelCount: 1,
          channelCountMode: "explicit",
        });
        node.port.onmessage = (event) => this.appendSamples(event.data);
        return node;
      } catch (error) {
        console.warn(
          "[fact-checker] AudioWorklet unavailable (page CSP?); falling back " +
            "to ScriptProcessorNode:",
          error
        );
      }
      const node = context.createScriptProcessor(SCRIPT_PROCESSOR_BUFFER, 1, 1);
      node.onaudioprocess = (event) => {
        this.appendDecimated(event.inputBuffer.getChannelData(0));
      };
      // A ScriptProcessorNode is only pulled while it reaches a destination;
      // a muted gain node keeps it running without being audible.
      this.silentSink = new GainNode(context, {gain: 0});
      node.connect(this.silentSink);
      this.silentSink.connect(context.destination);
      return node;
    }

    /**
     * Decimate context-rate samples to 16 kHz, carrying the phase across
     * callbacks (buffer sizes are not multiples of 3, so resetting per call
     * would drift the effective rate). The worklet does this internally.
     *
     * @param {Float32Array} samples
     */
    appendDecimated(samples) {
      if (this.stopped) {
        return;
      }
      const decimated = new Float32Array(
        Math.ceil((samples.length - this.decimationPhase) / DECIMATION_FACTOR)
      );
      let written = 0;
      for (let index = 0; index < samples.length; index += 1) {
        if (this.decimationPhase === 0) {
          decimated[written] = samples[index];
          written += 1;
        }
        this.decimationPhase = (this.decimationPhase + 1) % DECIMATION_FACTOR;
      }
      this.appendSamples(decimated.subarray(0, written));
    }

    /**
     * Accumulate 16 kHz samples into the 250 ms frame buffer, emitting a
     * base64 Int16LE frame whenever it fills.
     *
     * @param {Float32Array} block
     */
    appendSamples(block) {
      if (this.stopped || !(block instanceof Float32Array)) {
        return;
      }
      let readOffset = 0;
      while (readOffset < block.length) {
        const copyCount = Math.min(
          block.length - readOffset,
          FRAME_SAMPLES - this.frameOffset
        );
        this.frameBuffer.set(
          block.subarray(readOffset, readOffset + copyCount),
          this.frameOffset
        );
        this.frameOffset += copyCount;
        readOffset += copyCount;
        if (this.frameOffset === FRAME_SAMPLES) {
          this.frameOffset = 0;
          this.onFrame?.(encodeBase64(encodeInt16Le(this.frameBuffer)));
        }
      }
    }

    /**
     * SPA navigation replaces the whole player; when that happens the old
     * source node feeds a detached element (silence), so re-point the
     * analysis branch at the new one.
     */
    async ensureAttached() {
      if (this.stopped) {
        return;
      }
      if (this.videoElement && this.videoElement.isConnected) {
        return;
      }
      const replacement = findVideoElement();
      if (!replacement || replacement === this.videoElement) {
        return;
      }
      console.info("[fact-checker] player element replaced; re-attaching");
      this.disconnectAnalysisBranch();
      await this.attachTo(replacement);
    }

    /** Grab one frame of the captured element, scaled to <=480p JPEG. */
    async captureVideoFrame() {
      const video = this.videoElement;
      if (this.stopped || this.frameInFlight || !video) {
        return;
      }
      if (this.shouldSendFrame && this.shouldSendFrame() === false) {
        return;
      }
      const {videoWidth, videoHeight, readyState} = video;
      if (readyState < 2 || !videoWidth || !videoHeight) {
        return;
      }
      this.frameInFlight = true;
      try {
        const scale = Math.min(1, VIDEO_MAX_HEIGHT / videoHeight);
        const width = Math.round(videoWidth * scale);
        const height = Math.round(videoHeight * scale);
        if (
          !this.frameCanvas ||
          this.frameCanvas.width !== width ||
          this.frameCanvas.height !== height
        ) {
          this.frameCanvas = new OffscreenCanvas(width, height);
        }
        this.frameCanvas.getContext("2d").drawImage(video, 0, 0, width, height);
        const blob = await this.frameCanvas.convertToBlob({
          type: "image/jpeg",
          quality: VIDEO_JPEG_QUALITY,
        });
        const imageB64 = encodeBase64(await blob.arrayBuffer());
        this.onVideoFrame?.({imageB64, capturedAtMs: Date.now()});
      } finally {
        this.frameInFlight = false;
      }
    }

    /**
     * Drop the analysis branch. The source -> destination link and the
     * AudioContext deliberately SURVIVE: tearing them down would silence
     * the page for good (constraint 1).
     */
    disconnectAnalysisBranch() {
      // TARGETED disconnect: `source.disconnect()` with no argument would
      // also drop source -> destination and mute the page for good.
      if (this.source && this.filterHead) {
        try {
          this.source.disconnect(this.filterHead);
        } catch (error) {
          console.debug("[fact-checker] source detach failed:", error);
        }
      }
      for (const node of this.analysisNodes ?? []) {
        try {
          node?.disconnect();
        } catch (error) {
          console.debug("[fact-checker] node disconnect failed:", error);
        }
      }
      if (this.captureNode) {
        if (this.captureNode.port) {
          this.captureNode.port.onmessage = null;
        }
        this.captureNode.onaudioprocess = null;
      }
      this.analysisNodes = [];
      this.filterHead = null;
      this.captureNode = null;
      this.silentSink = null;
    }

    /** Stop capturing; the page keeps playing exactly as before. */
    stop() {
      this.stopped = true;
      for (const timerId of [this.frameTimerId, this.reattachTimerId]) {
        if (timerId !== null) {
          clearInterval(timerId);
        }
      }
      this.frameTimerId = null;
      this.reattachTimerId = null;
      this.disconnectAnalysisBranch();
      this.frameCanvas = null;
      this.frameOffset = 0;
      this.videoElement = null;
      this.source = null;
    }
  }

  self.PageAudioCapture = PageAudioCapture;
})();
