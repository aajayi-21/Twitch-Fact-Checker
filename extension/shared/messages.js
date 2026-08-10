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
  // offscreen -> service worker
  BACKEND_EVENT: "BACKEND_EVENT",
  CAPTURE_ENDED: "CAPTURE_ENDED",
  SESSION_UPDATE: "SESSION_UPDATE",
  // service worker -> content script (via chrome.tabs.sendMessage)
  OVERLAY_EVENT: "OVERLAY_EVENT",
  SESSION_STATE: "SESSION_STATE",
  // Firefox capture path only (no tabCapture/offscreen there): the content
  // script taps the page's own <video> and streams PCM back to the
  // background page. See shared/capabilities.js.
  CONTENT_CAPTURE_START: "CONTENT_CAPTURE_START",
  CONTENT_CAPTURE_STOP: "CONTENT_CAPTURE_STOP",
  // content script -> service worker
  CONTENT_READY: "CONTENT_READY",
  OPEN_OPTIONS: "OPEN_OPTIONS",
  // content script -> service worker: 👍/👎 on a verdict; the SW POSTs it
  // to the backend's /feedback endpoint (fire-and-forget).
  SUBMIT_FEEDBACK: "SUBMIT_FEEDBACK",
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
