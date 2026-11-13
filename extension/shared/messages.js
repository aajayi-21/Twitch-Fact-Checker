/**
 * Shared message vocabulary for every extension context.
 *
 * Convention: every message type value EQUALS its constant name
 * (MSG.OVERLAY_EVENT === "OVERLAY_EVENT"), so classic (non-module) content
 * scripts can compare against string literals without importing this file.
 *
 * Envelope for chrome.runtime.sendMessage: {target, type, payload}.
 * Runtime messages broadcast to all extension contexts; receivers ignore
 * envelopes whose `target` does not match their own context name.
 * Service worker -> content script uses chrome.tabs.sendMessage instead
 * (no `target` field needed — tab delivery is already addressed).
 */

export const TARGET = Object.freeze({
  BACKGROUND: "background",
  OFFSCREEN: "offscreen",
});

export const MSG = Object.freeze({
  // popup -> service worker
  CAPTURE_START: "CAPTURE_START",
  CAPTURE_STOP: "CAPTURE_STOP",
  // service worker -> offscreen
  OFFSCREEN_START: "OFFSCREEN_START",
  OFFSCREEN_STOP: "OFFSCREEN_STOP",
  OFFSCREEN_CONFIG: "OFFSCREEN_CONFIG",
  // Ad break started/ended on the captured tab ({active}); the offscreen
  // document drops audio while it lasts (see shared/ad_pause.js).
  OFFSCREEN_AD_STATE: "OFFSCREEN_AD_STATE",
  // offscreen -> service worker
  BACKEND_EVENT: "BACKEND_EVENT",
  // In-browser speech crash guard ({phase: "begin"|"end", variantId}): sent
  // just before the GPU is first used and when that use returns. ANSWERED
  // (sendResponse) once stored, because "begin" must be on disk before the
  // GPU is touched. See engine/stt/speech_setup.mjs.
  SPEECH_GPU_ATTEMPT: "SPEECH_GPU_ATTEMPT",
  // The GPU failed during a session ({reason}); do not use it again until
  // the viewer re-runs the speech test.
  SPEECH_GPU_FAILED: "SPEECH_GPU_FAILED",
  CAPTURE_ENDED: "CAPTURE_ENDED",
  SESSION_UPDATE: "SESSION_UPDATE",
  // service worker -> content script (via chrome.tabs.sendMessage)
  OVERLAY_EVENT: "OVERLAY_EVENT",
  SESSION_STATE: "SESSION_STATE",
  // Ask the page who owns the stream; the content script answers through
  // sendResponse with {ownerHref, ownerName} (content/page_identity.js).
  GET_PAGE_IDENTITY: "GET_PAGE_IDENTITY",
  // Firefox capture path only (no tabCapture/offscreen there): the content
  // script taps the page's own <video> and streams PCM back to the
  // background page. See shared/capabilities.js.
  CONTENT_CAPTURE_START: "CONTENT_CAPTURE_START",
  CONTENT_CAPTURE_STOP: "CONTENT_CAPTURE_STOP",
  // content script -> service worker
  CONTENT_READY: "CONTENT_READY",
  OPEN_OPTIONS: "OPEN_OPTIONS",
  // content script -> service worker: 👍/👎 on a verdict; the SW records it
  // in the ledger and POSTs it to the backend's /feedback endpoint
  // (fire-and-forget).
  SUBMIT_FEEDBACK: "SUBMIT_FEEDBACK",
  // content script -> service worker: the page moved to another stream, or
  // its channel link appeared ({url, title, ownerHref, ownerName}). The
  // ledger uses it to attribute what follows to the right channel.
  PAGE_IDENTITY: "PAGE_IDENTITY",
  // content script -> service worker: the player is showing an ad ({active}).
  // Sent on every change and re-sent as a heartbeat while an ad lasts; the
  // service worker routes it to whichever context owns the session.
  AD_STATE: "AD_STATE",
  // Firefox capture path: audio/video payloads from the content script to
  // the background page's WebSocket. PCM travels base64-encoded because
  // runtime messaging is JSON-serialized in both browsers (an ArrayBuffer
  // would arrive as {}).
  AUDIO_CHUNK: "AUDIO_CHUNK",
  VIDEO_FRAME: "VIDEO_FRAME",
  CONTENT_CAPTURE_FAILED: "CONTENT_CAPTURE_FAILED",
});

/**
 * Session error codes. Each maps to exactly one line of popup copy
 * (see popup/popup.js ERROR_COPY) plus an action hint.
 */
export const ERR = Object.freeze({
  ERR_BACKEND_DOWN: "ERR_BACKEND_DOWN",
  ERR_STREAM_ID_EXPIRED: "ERR_STREAM_ID_EXPIRED",
  ERR_CAPTURE_FAILED: "ERR_CAPTURE_FAILED",
  ERR_CAPTURE_LOST: "ERR_CAPTURE_LOST",
  ERR_TAB_CLOSED: "ERR_TAB_CLOSED",
  // Backend is up but has no API key yet (healthz configured:false / WS
  // fatal error frame {"code":"not_configured"}).
  ERR_NOT_CONFIGURED: "ERR_NOT_CONFIGURED",
  // The backend hot-swapped to a freshly saved API key/provider and closed
  // the session (WS fatal error frame {"code":"credentials_updated"}).
  ERR_CREDENTIALS_UPDATED: "ERR_CREDENTIALS_UPDATED",
  // "This browser" is the chosen speech engine but it has no tested, usable
  // configuration (never set up, stale after an update, or GPU blocked).
  ERR_SPEECH_NOT_READY: "ERR_SPEECH_NOT_READY",
  // In-browser speech stopped working mid-session with nothing to fall
  // back to.
  ERR_SPEECH_FAILED: "ERR_SPEECH_FAILED",
});

/**
 * Build a runtime message envelope.
 *
 * @param {string} target - TARGET.BACKGROUND | TARGET.OFFSCREEN
 * @param {string} type - one of MSG.*
 * @param {object} [payload]
 * @returns {{target: string, type: string, payload: object}}
 */
export const makeMsg = (target, type, payload = {}) => ({target, type, payload});
