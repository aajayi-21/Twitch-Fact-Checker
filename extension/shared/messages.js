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
  // content script -> service worker
  CONTENT_READY: "CONTENT_READY",
  OPEN_OPTIONS: "OPEN_OPTIONS",
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
