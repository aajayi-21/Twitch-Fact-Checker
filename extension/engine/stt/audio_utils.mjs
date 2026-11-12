/**
 * Audio helpers for the speech benchmark: WAV decoding and a simple
 * silence-based segmenter.
 *
 * The segmenter is NOT the production voice-activity detector (Phase 3 uses a
 * neural VAD, matching the backend's Silero segmentation). It exists so the
 * benchmark feeds models utterance-shaped clips cut at pauses, the way a live
 * session will, instead of arbitrary fixed windows that slice words in half
 * and inflate the error rate. Its defaults mirror the backend's VAD settings
 * (STT_VAD_MIN_SILENCE_MS=500, STT_VAD_MAX_SEGMENT_S=10, 200 ms speech pad)
 * so browser and backend numbers are comparable.
 */

export const TARGET_SAMPLE_RATE = 16000;

const RIFF_HEADER_BYTES = 12;
const PCM_FORMAT_TAG = 1;

/**
 * Decode a 16 kHz mono 16-bit PCM WAV — the exact format the extension's
 * capture path produces — into float samples in [-1, 1).
 *
 * @param {ArrayBuffer} buffer
 * @returns {Float32Array}
 * @throws {Error} when the file is not 16 kHz mono PCM16
 */
export const decodeWavPcm16 = (buffer) => {
  const view = new DataView(buffer);
  const readTag = (offset) =>
    String.fromCharCode(
      view.getUint8(offset),
      view.getUint8(offset + 1),
      view.getUint8(offset + 2),
      view.getUint8(offset + 3)
    );
  if (buffer.byteLength < RIFF_HEADER_BYTES || readTag(0) !== "RIFF" || readTag(8) !== "WAVE") {
    throw new Error("Not a RIFF/WAVE file.");
  }

  let format = null;
  let offset = RIFF_HEADER_BYTES;
  while (offset + 8 <= buffer.byteLength) {
    const chunkId = readTag(offset);
    const chunkSize = view.getUint32(offset + 4, true);
    const bodyOffset = offset + 8;
    if (chunkId === "fmt ") {
      format = {
        tag: view.getUint16(bodyOffset, true),
        channels: view.getUint16(bodyOffset + 2, true),
        sampleRate: view.getUint32(bodyOffset + 4, true),
        bitsPerSample: view.getUint16(bodyOffset + 14, true),
      };
    } else if (chunkId === "data") {
      if (!format) {
        throw new Error("WAV data chunk precedes the fmt chunk.");
      }
      const {tag, channels, sampleRate, bitsPerSample} = format;
      if (
        tag !== PCM_FORMAT_TAG ||
        channels !== 1 ||
        sampleRate !== TARGET_SAMPLE_RATE ||
        bitsPerSample !== 16
      ) {
        throw new Error(
          `Need 16 kHz mono 16-bit PCM; got ${sampleRate} Hz, ${channels} channel(s), ` +
            `${bitsPerSample}-bit, format tag ${tag}.`
        );
      }
      // Clamp to the bytes actually present (some writers overstate the size).
      const byteLength = Math.min(chunkSize, buffer.byteLength - bodyOffset);
      const sampleCount = Math.floor(byteLength / 2);
      const samples = new Float32Array(sampleCount);
      for (let index = 0; index < sampleCount; index += 1) {
        samples[index] = view.getInt16(bodyOffset + index * 2, true) / 32768;
      }
      return samples;
    }
    // Chunks are word-aligned: an odd size is followed by one pad byte.
    offset = bodyOffset + chunkSize + (chunkSize % 2);
  }
  throw new Error("WAV file has no data chunk.");
};

/**
 * @typedef {object} SegmentOptions
 * @property {number} [sampleRate]
 * @property {number} [frameMs] - analysis frame length
 * @property {number} [minSilenceMs] - a pause at least this long ends a segment
 * @property {number} [maxSegmentS] - hard cap; a longer utterance is cut at
 *   its quietest recent frame
 * @property {number} [minSegmentS] - shorter fragments are merged or dropped
 * @property {number} [silenceRatio] - a frame is silent when its RMS is below
 *   this fraction of the clip's peak frame RMS
 * @property {number} [padMs] - context kept on each side of a segment
 */

/**
 * Cut audio into utterance-like segments at pauses.
 *
 * @param {Float32Array} samples
 * @param {SegmentOptions} [options]
 * @returns {{start: number, end: number}[]} sample-index ranges, in order
 */
export const splitOnSilence = (
  samples,
  {
    sampleRate = TARGET_SAMPLE_RATE,
    frameMs = 30,
    minSilenceMs = 500,
    maxSegmentS = 10,
    minSegmentS = 0.3,
    silenceRatio = 0.05,
    padMs = 200,
  } = {}
) => {
  const frameLength = Math.round((sampleRate * frameMs) / 1000);
  const frameCount = Math.floor(samples.length / frameLength);
  if (frameCount === 0) {
    return [];
  }

  const frameRms = new Float32Array(frameCount);
  let peakRms = 0;
  for (let frame = 0; frame < frameCount; frame += 1) {
    let sumSquares = 0;
    const base = frame * frameLength;
    for (let index = 0; index < frameLength; index += 1) {
      const sample = samples[base + index];
      sumSquares += sample * sample;
    }
    frameRms[frame] = Math.sqrt(sumSquares / frameLength);
    peakRms = Math.max(peakRms, frameRms[frame]);
  }
  if (peakRms === 0) {
    return []; // digital silence
  }

  const threshold = peakRms * silenceRatio;
  const minSilenceFrames = Math.max(1, Math.round(minSilenceMs / frameMs));
  const maxSegmentFrames = Math.max(1, Math.floor((maxSegmentS * 1000) / frameMs));
  const minSegmentFrames = Math.max(1, Math.round((minSegmentS * 1000) / frameMs));
  const padFrames = Math.round(padMs / frameMs);

  /** @type {{start: number, end: number}[]} frame ranges, end exclusive */
  const frameRanges = [];
  let segmentStart = -1;
  let silentRun = 0;
  for (let frame = 0; frame < frameCount; frame += 1) {
    const isSpeech = frameRms[frame] >= threshold;
    if (segmentStart < 0) {
      if (isSpeech) {
        segmentStart = frame;
        silentRun = 0;
      }
      continue;
    }
    silentRun = isSpeech ? 0 : silentRun + 1;
    if (silentRun >= minSilenceFrames) {
      frameRanges.push({start: segmentStart, end: frame - silentRun + 1});
      segmentStart = -1;
      continue;
    }
    if (frame - segmentStart + 1 >= maxSegmentFrames) {
      // No pause long enough: cut at the quietest frame in the last third so
      // the cut lands between words when it can.
      const searchFrom = segmentStart + Math.floor((maxSegmentFrames * 2) / 3);
      let quietest = frame;
      for (let candidate = searchFrom; candidate <= frame; candidate += 1) {
        if (frameRms[candidate] < frameRms[quietest]) {
          quietest = candidate;
        }
      }
      frameRanges.push({start: segmentStart, end: quietest + 1});
      segmentStart = quietest + 1;
      silentRun = 0;
    }
  }
  if (segmentStart >= 0) {
    // End of audio: close the open segment, without its trailing silence.
    frameRanges.push({start: segmentStart, end: frameCount - silentRun});
  }

  return frameRanges
    .filter((range) => range.end - range.start >= minSegmentFrames)
    .map((range) => ({
      start: Math.max(0, range.start - padFrames) * frameLength,
      end: Math.min(frameCount, range.end + padFrames) * frameLength,
    }));
};

/**
 * @param {{start: number, end: number}[]} ranges - sample-index ranges
 * @param {number} [sampleRate]
 * @returns {number} total duration in seconds
 */
export const totalDurationS = (ranges, sampleRate = TARGET_SAMPLE_RATE) =>
  ranges.reduce((sum, range) => sum + (range.end - range.start), 0) / sampleRate;
