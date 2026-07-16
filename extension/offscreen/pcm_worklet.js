/**
 * AudioWorkletProcessor that decimates 48 kHz audio to 16 kHz by keeping
 * every 3rd sample. Anti-alias filtering happens BEFORE this node (two
 * cascaded BiquadFilterNodes forming a 4th-order Butterworth lowpass at
 * 7 kHz), so plain decimation is safe here.
 *
 * The decimation phase is carried across process() callbacks: render quanta
 * are 128 samples (not divisible by 3), so resetting the phase per callback
 * would drift the effective output rate. Output is posted to the main thread
 * as 512-sample Float32Array blocks with a transferred buffer (zero-copy).
 *
 * Classic worklet script — no import/export (loaded via
 * audioWorklet.addModule, which does not share module scope with the page).
 */

const DECIMATION_FACTOR = 3; // 48000 / 3 = 16000
const BLOCK_SAMPLES = 512;

class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.decimationPhase = 0; // survives across 128-sample callbacks
    this.block = new Float32Array(BLOCK_SAMPLES);
    this.blockOffset = 0;
  }

  /**
   * @param {Float32Array[][]} inputs
   * @returns {boolean} true — keep the processor alive for the node's lifetime
   */
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel) {
      return true; // input not connected yet (or momentarily silent-muted)
    }
    for (let i = 0; i < channel.length; i += 1) {
      if (this.decimationPhase === 0) {
        this.block[this.blockOffset] = channel[i];
        this.blockOffset += 1;
        if (this.blockOffset === BLOCK_SAMPLES) {
          this.port.postMessage(this.block, [this.block.buffer]);
          this.block = new Float32Array(BLOCK_SAMPLES);
          this.blockOffset = 0;
        }
      }
      this.decimationPhase = (this.decimationPhase + 1) % DECIMATION_FACTOR;
    }
    return true;
  }
}

registerProcessor("pcm-capture", PcmCaptureProcessor);
