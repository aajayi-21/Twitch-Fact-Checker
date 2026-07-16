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
   */
  constructor({onFrame, onEnded}) {
    this.onFrame = onFrame;
    this.onEnded = onEnded;
    this.mediaStream = null;
    this.audioContext = null;
    this.workletNode = null;
    this.frameBuffer = new Float32Array(FRAME_SAMPLES);
    this.frameOffset = 0;
    this.stopped = false;
  }

  /**
   * Redeem the tabCapture stream id and build the audio graph. The
   * getUserMedia call is issued synchronously on entry — the stream id
   * expires within seconds of being minted.
   *
   * @param {string} streamId - from chrome.tabCapture.getMediaStreamId
   */
  async start(streamId) {
    this.mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        mandatory: {
          chromeMediaSource: "tab",
          chromeMediaSourceId: streamId,
        },
      },
    });
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
