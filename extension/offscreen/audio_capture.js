/**
 * Tab-audio capture and PCM framing.
 *
 * Audio graph (all standard nodes — no hand-written DSP):
 *
 *   source ─→ ctx.destination                      (loopback: the captured tab
 *                                                   stays audible — non-negotiable)
 *   source ─→ Biquad(lowpass 7 kHz, Q 0.54)
 *          ─→ Biquad(lowpass 7 kHz, Q 1.31)        (two cascaded biquads = a
 *                                                   4th-order Butterworth
 *                                                   anti-alias filter)
 *          ─→ AudioWorkletNode("pcm-capture")      (decimates 48 k → 16 k)
 *
 * The AudioContext is created at 48 kHz EXPLICITLY: Chrome resamples the
 * media stream to the context rate, so decimate-by-3 in the worklet is
 * always exact regardless of the tab's native rate.
 *
 * The worklet posts 512-sample Float32 blocks; this class batches them to
 * 4000 samples (250 ms at 16 kHz), converts to Int16 little-endian, and
 * hands one 8000-byte ArrayBuffer per 250 ms to `onFrame`.
 */

const WORKLET_MODULE_PATH = "pcm_worklet.js"; // relative to offscreen.html
const CONTEXT_SAMPLE_RATE = 48000;
const FRAME_SAMPLES = 4000; // 250 ms at 16 kHz — one binary WebSocket frame
const ANTI_ALIAS_CUTOFF_HZ = 7000;
// Butterworth 4th-order = two cascaded 2nd-order sections with these Qs.
const BUTTERWORTH_Q_STAGE_ONE = 0.54;
const BUTTERWORTH_Q_STAGE_TWO = 1.31;

// Opt-in video sampling (vision-assisted verification): one small JPEG
// every 5 s, scaled to <=480p. Frames are sent to the local backend and
// held only in its in-memory ring — never persisted anywhere.
const VIDEO_FRAME_INTERVAL_MS = 5000;
const VIDEO_MAX_HEIGHT = 480;
const VIDEO_JPEG_QUALITY = 0.7;

/**
 * Base64-encode an ArrayBuffer in chunks (String.fromCharCode has an
 * argument-count limit well below a full frame's byte length).
 *
 * @param {ArrayBuffer} buffer
 * @returns {string}
 */
const encodeBase64 = (buffer) => {
  const bytes = new Uint8Array(buffer);
  const chunks = [];
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    chunks.push(String.fromCharCode(...bytes.subarray(i, i + CHUNK)));
  }
  return btoa(chunks.join(""));
};

/**
 * Convert Float32 samples in [-1, 1] to an Int16 little-endian ArrayBuffer.
 * DataView is used so the byte order is little-endian by construction,
 * not by platform accident.
 *
 * @param {Float32Array} samples
 * @returns {ArrayBuffer}
 */
const encodeInt16Le = (samples) => {
  const buffer = new ArrayBuffer(samples.length * 2);
  const view = new DataView(buffer);
  for (let i = 0; i < samples.length; i += 1) {
    const clamped = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(i * 2, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true);
  }
  return buffer;
};

export class AudioCapture {
  /**
   * @param {object} callbacks
   * @param {(pcmFrame: ArrayBuffer) => void} callbacks.onFrame
   * @param {(reason: string) => void} callbacks.onEnded - track ended
   *   unexpectedly (not fired for a deliberate stop()).
   * @param {(frame: {imageB64: string, capturedAtMs: number}) => void}
   *   [callbacks.onVideoFrame] - one scaled JPEG per sampling tick (only
   *   when video capture was requested in start()).
   * @param {() => boolean} [callbacks.shouldSendFrame] - cheap pre-check;
   *   returning false skips the encode entirely (e.g. socket down).
   */
  constructor({onFrame, onEnded, onVideoFrame, shouldSendFrame}) {
    this.onFrame = onFrame;
    this.onEnded = onEnded;
    this.onVideoFrame = onVideoFrame ?? null;
    this.shouldSendFrame = shouldSendFrame ?? null;
    this.mediaStream = null;
    this.audioContext = null;
    this.workletNode = null;
    this.frameBuffer = new Float32Array(FRAME_SAMPLES);
    this.frameOffset = 0;
    this.stopped = false;
    this.videoElement = null;
    this.frameTimerId = null;
    this.frameCanvas = null;
    this.frameInFlight = false;
  }

  /**
   * Redeem the tabCapture stream id and build the audio graph. The
   * getUserMedia call is issued synchronously on entry — the stream id
   * expires within seconds of being minted. When video capture is enabled
   * the video track MUST be requested in this SAME call: a stream id is
   * single-redemption, so there is no second chance at it.
   *
   * @param {string} streamId - from chrome.tabCapture.getMediaStreamId
   * @param {{captureVideo?: boolean}} [options]
   */
  async start(streamId, {captureVideo = false} = {}) {
    const constraints = {
      audio: {
        mandatory: {
          chromeMediaSource: "tab",
          chromeMediaSourceId: streamId,
        },
      },
    };
    if (captureVideo) {
      constraints.video = {
        mandatory: {
          chromeMediaSource: "tab",
          chromeMediaSourceId: streamId,
          maxWidth: 854,
          maxHeight: VIDEO_MAX_HEIGHT,
          maxFrameRate: 5,
        },
      };
    }
    this.mediaStream = await navigator.mediaDevices.getUserMedia(constraints);
    const [audioTrack] = this.mediaStream.getAudioTracks();
    if (!audioTrack) {
      this.releaseStream();
      throw new Error("captured MediaStream has no audio track");
    }
    audioTrack.addEventListener("ended", () => {
      if (!this.stopped) {
        this.onEnded?.("capture_lost");
      }
    });

    this.audioContext = new AudioContext({sampleRate: CONTEXT_SAMPLE_RATE});
    await this.audioContext.audioWorklet.addModule(WORKLET_MODULE_PATH);

    const source = this.audioContext.createMediaStreamSource(this.mediaStream);
    // Loopback FIRST — a captured tab goes silent unless its audio is
    // routed back to the context destination.
    source.connect(this.audioContext.destination);

    const antiAliasStageOne = new BiquadFilterNode(this.audioContext, {
      type: "lowpass",
      frequency: ANTI_ALIAS_CUTOFF_HZ,
      Q: BUTTERWORTH_Q_STAGE_ONE,
    });
    const antiAliasStageTwo = new BiquadFilterNode(this.audioContext, {
      type: "lowpass",
      frequency: ANTI_ALIAS_CUTOFF_HZ,
      Q: BUTTERWORTH_Q_STAGE_TWO,
    });
    // channelCount 1 + explicit mode: Chrome downmixes the (usually stereo)
    // tab audio to mono at the worklet input — better than dropping a channel.
    this.workletNode = new AudioWorkletNode(this.audioContext, "pcm-capture", {
      numberOfInputs: 1,
      numberOfOutputs: 0,
      channelCount: 1,
      channelCountMode: "explicit",
    });
    source.connect(antiAliasStageOne);
    antiAliasStageOne.connect(antiAliasStageTwo);
    antiAliasStageTwo.connect(this.workletNode);
    this.workletNode.port.onmessage = (event) => {
      this.appendBlock(event.data);
    };

    const [videoTrack] = this.mediaStream.getVideoTracks();
    if (videoTrack) {
      this.startVideoSampling(videoTrack);
    }
  }

  /**
   * Play the video track in a hidden element and sample a scaled JPEG on an
   * interval. The element gets its OWN MediaStream so the audio graph's
   * consumers (createMediaStreamSource + the loopback) are untouched, and
   * it is muted so nothing double-plays.
   *
   * A video track ending is NOT fatal — frames simply stop while the audio
   * session continues (unlike the audio track's onEnded path).
   *
   * @param {MediaStreamTrack} videoTrack
   */
  startVideoSampling(videoTrack) {
    this.videoElement = document.createElement("video");
    this.videoElement.muted = true;
    this.videoElement.playsInline = true;
    this.videoElement.srcObject = new MediaStream([videoTrack]);
    this.videoElement.style.display = "none";
    document.body.append(this.videoElement);
    this.videoElement.play().catch((error) => {
      console.warn("[fact-checker] video element play failed:", error);
    });
    videoTrack.addEventListener("ended", () => {
      console.info("[fact-checker] video track ended; frames stop");
      this.stopVideoSampling();
    });
    this.frameTimerId = setInterval(() => {
      this.captureVideoFrame().catch((error) => {
        console.warn("[fact-checker] frame capture failed:", error);
      });
    }, VIDEO_FRAME_INTERVAL_MS);
  }

  /** Grab one frame, scale to <=480p, encode JPEG, hand off as base64. */
  async captureVideoFrame() {
    if (this.stopped || this.frameInFlight || !this.videoElement) {
      return;
    }
    if (this.shouldSendFrame && this.shouldSendFrame() === false) {
      return; // skip the encode entirely while the socket is down
    }
    const {videoWidth, videoHeight, readyState} = this.videoElement;
    if (readyState < 2 || !videoWidth || !videoHeight) {
      return; // no decodable frame yet
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
      const context = this.frameCanvas.getContext("2d");
      context.drawImage(this.videoElement, 0, 0, width, height);
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

  /** Tear down the sampling loop and the hidden video element. */
  stopVideoSampling() {
    if (this.frameTimerId !== null) {
      clearInterval(this.frameTimerId);
      this.frameTimerId = null;
    }
    if (this.videoElement) {
      this.videoElement.pause();
      this.videoElement.srcObject = null;
      this.videoElement.remove();
      this.videoElement = null;
    }
    this.frameCanvas = null;
  }

  /**
   * Accumulate a 512-sample worklet block into the 4000-sample frame buffer,
   * emitting a complete Int16LE frame whenever it fills.
   *
   * @param {Float32Array} block
   */
  appendBlock(block) {
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
        this.onFrame?.(encodeInt16Le(this.frameBuffer));
      }
    }
  }

  /** Tear down the graph, close the context, and release the MediaStream. */
  async stop() {
    this.stopped = true;
    this.stopVideoSampling();
    if (this.workletNode) {
      this.workletNode.port.onmessage = null;
      try {
        this.workletNode.disconnect();
      } catch (error) {
        console.debug("[fact-checker] worklet disconnect failed:", error);
      }
      this.workletNode = null;
    }
    if (this.audioContext) {
      try {
        await this.audioContext.close();
      } catch (error) {
        console.warn("[fact-checker] AudioContext close failed:", error);
      }
      this.audioContext = null;
    }
    this.releaseStream();
    this.frameOffset = 0;
  }

  releaseStream() {
    if (!this.mediaStream) {
      return;
    }
    for (const track of this.mediaStream.getTracks()) {
      track.stop();
    }
    this.mediaStream = null;
  }
}
