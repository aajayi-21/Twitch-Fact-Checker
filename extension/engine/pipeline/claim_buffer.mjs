/**
 * Transcript buffering for the claim gate — a port of the provider-neutral
 * half of backend/app/claim_gate.py.
 *
 * Text is batched and sent to the gate model in ONE call per interval, not
 * one per utterance: claim detection needs no web search, and a call every
 * few words would cost far more and split sentences across calls. A short
 * tail of earlier text travels with each batch so the model can resolve
 * "he" and "that bill".
 *
 * Pure (the clock is passed in): unit-tested under Node.
 */

export const GATE_DEFAULTS = Object.freeze({
  intervalS: 12, // backend GATE_INTERVAL_S
  minNewWords: 8, // backend ClaimGate.MIN_NEW_WORDS
  contextTailWords: 40, // backend ClaimGate.CONTEXT_TAIL_WORDS
});

const words = (text) => text.split(/\s+/).filter(Boolean);

export class ClaimBuffer {
  /**
   * @param {object} [options]
   * @param {number} [options.intervalS] - at most one gate call per this long
   * @param {number} [options.minNewWords] - and only once this much is new
   * @param {number} [options.contextTailWords]
   */
  constructor({
    intervalS = GATE_DEFAULTS.intervalS,
    minNewWords = GATE_DEFAULTS.minNewWords,
    contextTailWords = GATE_DEFAULTS.contextTailWords,
  } = {}) {
    this.intervalMs = intervalS * 1000;
    this.minNewWords = minNewWords;
    this.contextTailWords = contextTailWords;
    this.pendingTexts = [];
    this.pendingWordCount = 0;
    this.contextWords = [];
    // -Infinity so the very first batch only waits for minNewWords.
    this.lastDrainedAtMs = Number.NEGATIVE_INFINITY;
  }

  /** @param {string} text - one transcribed utterance */
  add(text) {
    const trimmed = String(text ?? "").trim();
    if (!trimmed) {
      return;
    }
    this.pendingTexts.push(trimmed);
    this.pendingWordCount += words(trimmed).length;
  }

  /** @returns {boolean} whether there is anything not yet sent to the gate */
  get hasPending() {
    return this.pendingWordCount > 0;
  }

  /**
   * @param {number} nowMs
   * @returns {boolean} the interval has passed AND enough new words arrived
   */
  shouldRun(nowMs) {
    return (
      nowMs - this.lastDrainedAtMs >= this.intervalMs && this.pendingWordCount >= this.minNewWords
    );
  }

  /**
   * Take everything buffered as one batch. The buffer is emptied and the
   * context tail advanced BEFORE the model is called: if the call fails the
   * batch is lost, which is acceptable; retrying it forever is not.
   *
   * @param {number} nowMs
   * @returns {{context: string, transcript: string}|null} null when empty
   */
  drain(nowMs) {
    const transcript = this.pendingTexts.join(" ").trim();
    const context = this.contextWords.join(" ");
    this.pendingTexts = [];
    this.pendingWordCount = 0;
    this.lastDrainedAtMs = nowMs;
    if (!transcript) {
      return null;
    }
    this.contextWords = [...this.contextWords, ...words(transcript)].slice(
      -this.contextTailWords
    );
    return {context, transcript};
  }
}
