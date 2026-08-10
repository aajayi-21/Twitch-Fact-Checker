/**
 * Firefox session owner — the offscreen document's counterpart.
 *
 * Firefox MV3 keeps a DOM-bearing background EVENT PAGE rather than a
 * service worker, so the WebSocket, its reconnect timers, and the session
 * state machine can live right here and reuse `BackendSocket` unchanged.
 * Only the audio source differs: PCM arrives as base64 messages from
 * content/capture.js instead of from a MediaStream (see
 * shared/capabilities.js for why the split falls this way).
 *
 * State machine matches offscreen/offscreen.js exactly, so the popup sees
 * identical `captureSession` transitions in both browsers:
 *
 *   idle ──Start──> starting ──ready frame──> capturing
 *   starting ──capture failure──> error(code) ──> idle
 *   capturing ──WS drop──> reconnecting (attempt n, backoff 0.5→15 s)
 *   reconnecting ──5 min elapsed──> error(ERR_BACKEND_DOWN) ──> idle
 *   capturing ──Stop / tab closed──> stopping ──cleanup──> idle
 *
 * The event page stays alive for the session's duration because the content
 * script messages it four times a second; no keepalive hack needed.
 */

import { ERR } from "../shared/messages.js";
import { BackendSocket } from "../offscreen/ws_client.js";

const AUDIO_SAMPLE_RATE = 16000;

let socket = null;
let sessionSettings = null;
let activeTabId = null;
/** Hooks into the service worker's session/tab plumbing (see configure). */
let hooks = {
  onSessionPatch: async () => {},
  onBackendEvent: () => {},
  onEnded: async () => {},
};

/**
 * Install the service worker's callbacks. Passed in rather than imported so
 * this module never depends on service_worker.js (which imports it).
 *
 * @param {object} callbacks
 * @param {(patch: object) => Promise<void>} callbacks.onSessionPatch - merge
 *   into chrome.storage.session["captureSession"].
 * @param {(tabId: number, frame: object) => void} callbacks.onBackendEvent -
 *   relay one server frame to the captured tab's overlay.
 * @param {(reason: string) => Promise<void>} callbacks.onEnded - the session
 *   finished (same contract as the offscreen doc's CAPTURE_ENDED).
 */
export const configure = (callbacks) => {
  hooks = {...hooks, ...callbacks};
};

/** True while a Firefox-path session is running. */
export const isActive = () => socket !== null;

const forwardBackendEvent = (frame) => {
  if (activeTabId !== null) {
    hooks.onBackendEvent(activeTabId, frame);
  }
};

/** Detach callbacks first so teardown cannot re-enter the state machine. */
const teardown = () => {
  const activeSocket = socket;
  socket = null;
  if (activeSocket) {
    activeSocket.onEvent = null;
    activeSocket.onStateChange = null;
    activeSocket.onGiveUp = null;
    activeSocket.destroy();
  }
};

const endSessionWithError = async (lastError, reason) => {
  teardown();
  await hooks.onSessionPatch({
    status: "error",
    wsState: "closed",
    reconnectAttempt: 0,
    lastError,
  });
  activeTabId = null;
  sessionSettings = null;
  await hooks.onEnded(reason);
};

const handleSocketState = (wsState, reconnectAttempt) => {
  const patch = {wsState, reconnectAttempt};
  if (wsState === "reconnecting") {
    patch.status = "reconnecting";
  }
  hooks.onSessionPatch(patch).catch((error) => {
    console.error("[fact-checker] ws-state persistence failed:", error);
  });
};

const handleSocketGiveUp = (reason) => {
  console.error(`[fact-checker] backend given up: ${reason}`);
  endSessionWithError(ERR.ERR_BACKEND_DOWN, "backend_down").catch((error) => {
    console.error("[fact-checker] give-up handling failed:", error);
  });
};

const handleServerFrame = (frame) => {
  forwardBackendEvent(frame);
  if (frame.type === "ready") {
    hooks
      .onSessionPatch({status: "capturing", wsState: "open", reconnectAttempt: 0})
      .catch((error) => {
        console.error("[fact-checker] ready-state persistence failed:", error);
      });
    return;
  }
  if (frame.type === "error" && frame.fatal === true) {
    if (frame.code === "not_configured") {
      console.error(`[fact-checker] backend not configured: ${frame.message}`);
      endSessionWithError(ERR.ERR_NOT_CONFIGURED, ERR.ERR_NOT_CONFIGURED).catch(
        (error) => {
          console.error("[fact-checker] not-configured handling failed:", error);
        }
      );
      return;
    }
    if (frame.code === "credentials_updated") {
      console.info(`[fact-checker] backend credentials updated: ${frame.message}`);
      endSessionWithError(
        ERR.ERR_CREDENTIALS_UPDATED,
        "credentials_updated"
      ).catch((error) => {
        console.error("[fact-checker] credentials-updated handling failed:", error);
      });
      return;
    }
    console.error(
      `[fact-checker] fatal backend error (${frame.code}): ${frame.message}`
    );
    endSessionWithError(`backend:${frame.code}`, frame.code).catch((error) => {
      console.error("[fact-checker] fatal-frame handling failed:", error);
    });
  }
};

/**
 * Open the backend socket for a content-script capture session. The content
 * script is told to start separately; audio that arrives before the socket
 * is OPEN is dropped by BackendSocket (a live stream cannot be rewound).
 *
 * @param {number} tabId
 * @param {object} settings - the same shape the offscreen path receives.
 */
export const start = async (tabId, settings) => {
  if (socket) {
    teardown();
  }
  activeTabId = tabId;
  sessionSettings = {...settings};
  await hooks.onSessionPatch({
    status: "starting",
    tabId,
    startedAt: Date.now(),
    wsState: "connecting",
    reconnectAttempt: 0,
    lastError: null,
  });
  socket = new BackendSocket({
    url: sessionSettings.backendUrl,
    hello: {
      type: "hello",
      version: 1,
      format: "pcm_s16le",
      sample_rate: AUDIO_SAMPLE_RATE,
      channels: 1,
      sensitivity: sessionSettings.sensitivity,
      enabled_topics: Array.isArray(sessionSettings.topics)
        ? [...sessionSettings.topics]
        : null,
      send_transcripts: Boolean(sessionSettings.sendTranscripts),
      platform: sessionSettings.platform ?? null,
      channel: sessionSettings.channel ?? null,
      stream_title: sessionSettings.streamTitle ?? null,
    },
    onEvent: handleServerFrame,
    onStateChange: handleSocketState,
    onGiveUp: handleSocketGiveUp,
  });
  socket.connect();
};

/**
 * Graceful stop: {"type":"stop"} lets the server flush and run a final gate
 * pass, and flush frames keep reaching the overlay until it closes 1000.
 *
 * @param {string} reason
 */
export const stop = async (reason = "user_stop") => {
  const activeSocket = socket;
  socket = null;
  await hooks.onSessionPatch({status: "stopping"});
  if (activeSocket) {
    // Forward flush frames only — no state-machine re-entry.
    activeSocket.onEvent = forwardBackendEvent;
    activeSocket.onStateChange = null;
    activeSocket.onGiveUp = null;
    try {
      await activeSocket.stop();
    } catch (error) {
      console.warn("[fact-checker] socket stop failed:", error);
    }
    activeSocket.onEvent = null;
    if (activeSocket.droppedMs > 0) {
      console.info(
        `[fact-checker] session dropped ${activeSocket.droppedMs} ms of audio ` +
          "while disconnected"
      );
    }
  }
  activeTabId = null;
  sessionSettings = null;
  await hooks.onSessionPatch({
    status: "idle",
    tabId: null,
    startedAt: null,
    wsState: "closed",
    reconnectAttempt: 0,
    lastError: reason === "tab_closed" ? ERR.ERR_TAB_CLOSED : null,
  });
  await hooks.onEnded(reason);
};

/**
 * One 250 ms PCM frame from the content script. Base64 because runtime
 * messaging is JSON-serialized; decoded straight back to the bytes the
 * backend's binary frames expect.
 *
 * @param {string} pcmB64
 */
export const submitAudio = (pcmB64) => {
  if (!socket || typeof pcmB64 !== "string") {
    return;
  }
  const binary = atob(pcmB64);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  socket.sendAudio(bytes.buffer);
};

/**
 * One captured stream frame (opt-in vision path); dropped when the socket
 * is not open, exactly like the Chrome path.
 *
 * @param {{imageB64: string, capturedAtMs: number}} frame
 */
export const submitVideoFrame = ({imageB64, capturedAtMs}) => {
  socket?.sendJson({
    type: "frame",
    image_b64: imageB64,
    captured_at_ms: capturedAtMs,
  });
};

/** True while the socket can accept frames (the content script's gate). */
export const isSocketOpen = () => socket?.isOpen === true;

/**
 * Mid-session sensitivity/topic change, relayed from the options page.
 *
 * @param {{sensitivity?: string, topics?: string[]}} patch
 */
export const sendConfig = ({sensitivity, topics} = {}) => {
  if (!socket) {
    return;
  }
  const configFrame = {};
  if (sensitivity && sensitivity !== sessionSettings?.sensitivity) {
    sessionSettings = {...sessionSettings, sensitivity};
    configFrame.sensitivity = sensitivity;
  }
  if (Array.isArray(topics)) {
    sessionSettings = {...sessionSettings, topics: [...topics]};
    configFrame.enabled_topics = [...topics];
  }
  if (Object.keys(configFrame).length === 0) {
    return;
  }
  const delivered = socket.sendConfig(configFrame);
  console.info(
    `[fact-checker] config change (${Object.keys(configFrame).join(", ")}) ${
      delivered ? "sent now" : "will apply via reconnect hello"
    }`
  );
};

/** The content script could not tap the page's audio — end the session. */
export const captureFailed = async () => {
  await endSessionWithError(ERR.ERR_CAPTURE_FAILED, "capture_failed");
};
