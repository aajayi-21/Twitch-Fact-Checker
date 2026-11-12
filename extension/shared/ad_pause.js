/**
 * Ad-break pause gate, shared by both session owners (the Chrome offscreen
 * document and the Firefox background page).
 *
 * While the captured tab plays an ad, the tab's audio IS the ad, so feeding
 * it to the backend would transcribe and fact-check advertising copy. The
 * content script detects ads from the player DOM and reports them; the
 * session owner drops audio and video frames for as long as this gate is
 * paused. The backend needs no protocol change — it simply sees no new audio.
 *
 * FAIL-OPEN BY DESIGN: a pause must be re-asserted (the content script sends
 * a heartbeat while the ad lasts) or it lapses after AD_PAUSE_TTL_MS. If the
 * content script disappears mid-ad — the tab navigates to an unsupported
 * page, the extension reloads — fact-checking resumes on its own instead of
 * staying deaf for the rest of the session.
 */

/** A pause not re-asserted within this window lapses. */
export const AD_PAUSE_TTL_MS = 20000;

const PCM_FRAME_BYTES = 8000; // 4000 Int16 samples = 250 ms at 16 kHz
const BOUNDARY_SILENCE_FRAMES = 4; // 1 s

/**
 * Zero-filled PCM frames to send once when a pause begins. Dropping frames
 * splices the pre-ad audio directly onto the post-ad audio; a second of
 * silence in between lets the backend's VAD close the streamer's last
 * utterance instead of gluing it to whatever is said after the break.
 *
 * @returns {ArrayBuffer[]}
 */
export const buildBoundarySilenceFrames = () =>
  Array.from(
    {length: BOUNDARY_SILENCE_FRAMES},
    () => new ArrayBuffer(PCM_FRAME_BYTES)
  );

export class AdPauseGate {
  #assertedAtMs = null;

  /**
   * Assert (or refresh) the pause.
   *
   * @param {number} [nowMs]
   * @returns {boolean} true when this call STARTED a pause (as opposed to
   *   refreshing one that was still in force).
   */
  pause(nowMs = Date.now()) {
    const wasPaused = this.isPaused(nowMs);
    this.#assertedAtMs = nowMs;
    return !wasPaused;
  }

  /** End the pause immediately. */
  resume() {
    this.#assertedAtMs = null;
  }

  /**
   * @param {number} [nowMs]
   * @returns {boolean} true while a pause is in force and has not lapsed.
   */
  isPaused(nowMs = Date.now()) {
    return (
      this.#assertedAtMs !== null && nowMs - this.#assertedAtMs < AD_PAUSE_TTL_MS
    );
  }
}
