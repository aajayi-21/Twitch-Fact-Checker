/**
 * Streaming segmenter: turns a live audio stream into utterance-sized clips
 * for the speech model, cut at pauses.
 *
 * The live counterpart of audio_utils.mjs `splitOnSilence`, with the same
 * defaults (which mirror the backend's STT_VAD_MIN_SILENCE_MS=500,
 * STT_VAD_MAX_SEGMENT_S=10 and 200 ms speech pad). Audio arrives in arbitrary
 * chunks; a segment is emitted as soon as a long-enough pause follows speech,
 * or when unbroken speech reaches the length cap.
 *
 * This is an ENERGY-based detector — good enough to measure the pipeline,
 * not the production voice-activity detector. Music, crowd noise and game
 * audio will defeat it; Phase 3 replaces the `isSpeech` decision with a
 * neural VAD and keeps everything else.
 *
 * Pure (no browser APIs): unit-tested under Node.
 */

import {TARGET_SAMPLE_RATE} from "./audio_utils.mjs";

/**
 * @typedef {object} StreamSegment
 * @property {Float32Array} samples
 * @property {number} startS - position of the first sample in the stream
 * @property {number} endS - position just past the last sample
 * @property {boolean} forced - true when cut by the length cap, not a pause
 */

export class StreamSegmenter {
  /**
   * @param {object} [options]
   * @param {number} [options.sampleRate]
   * @param {number} [options.frameMs] - analysis frame length
   * @param {number} [options.minSilenceMs] - a pause this long ends a segment
   * @param {number} [options.maxSegmentS] - cap; longer speech is cut at its
   *   quietest recent frame
   * @param {number} [options.minSegmentS] - shorter bursts are discarded
   * @param {number} [options.padMs] - context kept on each side of a segment
   * @param {number} [options.silenceFloor] - frames below this RMS are always
   *   silence (digital silence and line noise)
   * @param {number} [options.silenceRatio] - otherwise a frame is silence when
   *   below this fraction of the recent loudest frame
   * @param {number} [options.peakHalfLifeS] - how quickly the "recent
   *   loudest" level forgets, so one shout does not mute a quiet speaker
   */
  constructor({
    sampleRate = TARGET_SAMPLE_RATE,
    frameMs = 30,
    minSilenceMs = 500,
    maxSegmentS = 10,
    minSegmentS = 0.3,
    padMs = 200,
    silenceFloor = 0.003,
    silenceRatio = 0.05,
    peakHalfLifeS = 30,
  } = {}) {
    this.sampleRate = sampleRate;
    this.frameLength = Math.round((sampleRate * frameMs) / 1000);
    this.minSilenceFrames = Math.max(1, Math.round(minSilenceMs / frameMs));
    this.maxSegmentFrames = Math.max(1, Math.floor((maxSegmentS * 1000) / frameMs));
    this.minSegmentFrames = Math.max(1, Math.round((minSegmentS * 1000) / frameMs));
    this.padFrames = Math.round(padMs / frameMs);
    this.silenceFloor = silenceFloor;
    this.silenceRatio = silenceRatio;
    this.peakDecay = Math.pow(0.5, frameMs / 1000 / peakHalfLifeS);

    /** Samples not yet forming a whole frame. */
    this.pending = new Float32Array(0);
    /** Frames consumed so far (stream clock). */
    this.frameCount = 0;
    this.peakRms = 0;

    /** Most recent frames while idle — the leading pad of the next segment. */
    this.preRoll = [];
    /** Frames of the segment being built (speech plus interior pauses). */
    this.segmentFrames = [];
    this.segmentRms = [];
    this.segmentStartFrame = 0;
    /** How many of the segment's first frames are leading pad, not speech. */
    this.leadPadFrames = 0;
    this.inSpeech = false;
    this.silentRun = 0;
  }

  /**
   * Feed audio; returns the segments that closed as a result (often none).
   *
   * @param {Float32Array} samples
   * @returns {StreamSegment[]}
   */
  push(samples) {
    const closed = [];
    let buffer = samples;
    if (this.pending.length > 0) {
      buffer = new Float32Array(this.pending.length + samples.length);
      buffer.set(this.pending);
      buffer.set(samples, this.pending.length);
    }
    let offset = 0;
    while (offset + this.frameLength <= buffer.length) {
      const frame = buffer.slice(offset, offset + this.frameLength);
      offset += this.frameLength;
      const segment = this.#consumeFrame(frame);
      if (segment) {
        closed.push(segment);
      }
    }
    this.pending = buffer.slice(offset);
    return closed;
  }

  /**
   * End of stream: close the segment in progress, if any.
   *
   * @returns {StreamSegment[]}
   */
  flush() {
    if (!this.inSpeech) {
      return [];
    }
    const segment = this.#closeSegment(this.segmentFrames.length - this.silentRun, false);
    this.#reset();
    return segment ? [segment] : [];
  }

  /** @param {Float32Array} frame @returns {StreamSegment|null} */
  #consumeFrame(frame) {
    let sumSquares = 0;
    for (let index = 0; index < frame.length; index += 1) {
      sumSquares += frame[index] * frame[index];
    }
    const rms = Math.sqrt(sumSquares / frame.length);
    this.peakRms = Math.max(rms, this.peakRms * this.peakDecay);
    const isSpeech = rms >= Math.max(this.silenceFloor, this.peakRms * this.silenceRatio);
    const frameIndex = this.frameCount;
    this.frameCount += 1;

    if (!this.inSpeech) {
      if (!isSpeech) {
        this.preRoll.push(frame);
        if (this.preRoll.length > this.padFrames) {
          this.preRoll.shift();
        }
        return null;
      }
      // Speech begins: the pre-roll becomes the segment's leading pad.
      this.inSpeech = true;
      this.silentRun = 0;
      this.segmentStartFrame = frameIndex - this.preRoll.length;
      this.leadPadFrames = this.preRoll.length;
      this.segmentFrames = [...this.preRoll, frame];
      this.segmentRms = [...this.preRoll.map(() => 0), rms];
      this.preRoll = [];
      return null;
    }

    this.segmentFrames.push(frame);
    this.segmentRms.push(rms);
    this.silentRun = isSpeech ? 0 : this.silentRun + 1;

    if (this.silentRun >= this.minSilenceFrames) {
      // A real pause: close at the end of speech, keeping a trailing pad.
      const speechEnd = this.segmentFrames.length - this.silentRun;
      const segment = this.#closeSegment(speechEnd, false);
      // The tail of the pause is the pre-roll of whatever comes next.
      this.preRoll = this.segmentFrames.slice(-this.padFrames);
      this.#reset({keepPreRoll: true});
      return segment;
    }

    if (this.segmentFrames.length >= this.maxSegmentFrames) {
      // No pause long enough: cut at the quietest frame in the last third so
      // the cut lands between words when it can, and carry the rest over.
      const searchFrom = Math.floor((this.maxSegmentFrames * 2) / 3);
      let quietest = this.segmentFrames.length - 1;
      for (let index = searchFrom; index < this.segmentFrames.length; index += 1) {
        if (this.segmentRms[index] < this.segmentRms[quietest]) {
          quietest = index;
        }
      }
      const cut = quietest + 1;
      const segment = this.#closeSegment(cut, true, {pad: false});
      const carriedFrames = this.segmentFrames.slice(cut);
      const carriedRms = this.segmentRms.slice(cut);
      this.segmentStartFrame += cut;
      this.leadPadFrames = 0;
      this.segmentFrames = carriedFrames;
      this.segmentRms = carriedRms;
      // silentRun is unchanged: any trailing silence is in the carried part.
      this.silentRun = Math.min(this.silentRun, carriedFrames.length);
      return segment;
    }
    return null;
  }

  /**
   * Build a segment from the first `speechFrames` frames of the buffer, plus
   * a trailing pad taken from the frames that follow.
   *
   * @param {number} speechFrames
   * @param {boolean} forced
   * @param {{pad?: boolean}} [options]
   * @returns {StreamSegment|null} null when too short to be worth transcribing
   */
  #closeSegment(speechFrames, forced, {pad = true} = {}) {
    // The leading pad is not speech; do not count it toward the minimum.
    if (speechFrames - this.leadPadFrames < this.minSegmentFrames) {
      return null;
    }
    const endFrame = pad
      ? Math.min(this.segmentFrames.length, speechFrames + this.padFrames)
      : speechFrames;
    const samples = new Float32Array(endFrame * this.frameLength);
    for (let index = 0; index < endFrame; index += 1) {
      samples.set(this.segmentFrames[index], index * this.frameLength);
    }
    const secondsPerFrame = this.frameLength / this.sampleRate;
    return {
      samples,
      startS: this.segmentStartFrame * secondsPerFrame,
      endS: (this.segmentStartFrame + endFrame) * secondsPerFrame,
      forced,
    };
  }

  /** @param {{keepPreRoll?: boolean}} [options] */
  #reset({keepPreRoll = false} = {}) {
    this.inSpeech = false;
    this.silentRun = 0;
    this.segmentFrames = [];
    this.segmentRms = [];
    if (!keepPreRoll) {
      this.preRoll = [];
    }
  }
}
