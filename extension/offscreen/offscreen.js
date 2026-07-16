/**
 * Offscreen document — the session owner and MV3 lifetime linchpin.
 *
 * Owns everything long-lived (MediaStream, AudioContext, WebSocket,
 * reconnect timers) and runs the capture-status state machine:
 *
 *   idle ──Start──> starting ──ready frame──> capturing
 *   starting ──getUserMedia failure──> error(code) ──> idle
 *   capturing ──WS drop──> reconnecting (attempt n, backoff 0.5→15 s)
 *   reconnecting ──open + ready──> capturing
 *   reconnecting ──5 min elapsed──> error(ERR_BACKEND_DOWN) ──> idle
 *   capturing ──Stop / tab closed / track ended──> stopping ──cleanup──> idle
 *
 * The single source of truth for the popup is
 * chrome.storage.session["captureSession"]. Offscreen documents can only use
 * chrome.runtime messaging (no other chrome.* API exists here), so every
 * transition is emitted as a SESSION_UPDATE message carrying the FULL session
 * object; the service worker persists it. sendMessage wakes a dead service
 * worker, so popup status survives service-worker death with zero keepalive
 * hacks.
 */

import {ERR, MSG, TARGET, makeMsg} from "../shared/messages.js";
import {AudioCapture} from "./audio_capture.js";
import {BackendSocket} from "./ws_client.js";

const AUDIO_SAMPLE_RATE = 16000;

let capture = null;
let socket = null;
let sessionSettings = null;

let session = {
  status: "idle",
  tabId: null,
  startedAt: null,
  wsState: "closed",
  reconnectAttempt: 0,
  lastError: null,
};

/**
 * Merge a patch into the in-memory session and ask the service worker to
 * persist the FULL object to chrome.storage.session (offscreen documents
 * cannot access chrome.storage). The in-memory merge is synchronous, so
 * overlapping writers always emit a state at least as new as their own
 * patch, and full-object updates are last-writer-wins in message order.
 */
const writeSession = async (patch) => {
  session = {...session, ...patch};
  try {
    await chrome.runtime.sendMessage(
      makeMsg(TARGET.BACKGROUND, MSG.SESSION_UPDATE, {session})
    );
  } catch (error) {
    console.error("[fact-checker] persisting captureSession failed:", error);
  }
};

const sendCaptureEnded = async (reason) => {
  try {
    await chrome.runtime.sendMessage(
      makeMsg(TARGET.BACKGROUND, MSG.CAPTURE_ENDED, {reason})
    );
  } catch (error) {
    console.warn("[fact-checker] CAPTURE_ENDED delivery failed:", error);
  }
};

const forwardBackendEvent = (frame) => {
  if (session.tabId === null) {
    return;
  }
  chrome.runtime
    .sendMessage(
      makeMsg(TARGET.BACKGROUND, MSG.BACKEND_EVENT, {
        tabId: session.tabId,
        event: frame,
      })
    )
    .catch((error) => {
      console.debug("[fact-checker] BACKEND_EVENT relay failed:", error);
    });
};

/**
 * Detach callbacks and release capture + socket. Callbacks are nulled FIRST
 * so teardown-triggered close events cannot re-enter the state machine.
 */
const teardown = async () => {
  const activeSocket = socket;
  const activeCapture = capture;
  socket = null;
  capture = null;
  if (activeSocket) {
    activeSocket.onEvent = null;
    activeSocket.onStateChange = null;
    activeSocket.onGiveUp = null;
    activeSocket.destroy();
  }
  if (activeCapture) {
    activeCapture.onFrame = null;
    activeCapture.onEnded = null;
    try {
      await activeCapture.stop();
    } catch (error) {
      console.warn("[fact-checker] capture teardown failed:", error);
    }
  }
};

/** Session-fatal path: tear down, record the error, tell the service worker. */
const endSessionWithError = async (lastError, reason) => {
  await teardown();
  await writeSession({
    status: "error",
    wsState: "closed",
    reconnectAttempt: 0,
    lastError,
  });
  await sendCaptureEnded(reason);
};

const handleAudioFrame = (pcmFrame) => {
  socket?.sendAudio(pcmFrame);
};

const handleCaptureLost = (reason) => {
  console.error(`[fact-checker] tab audio track ended (${reason})`);
  endSessionWithError(ERR.ERR_CAPTURE_LOST, "capture_lost").catch((error) => {
    console.error("[fact-checker] capture-lost handling failed:", error);
  });
};

const handleSocketGiveUp = (reason) => {
  console.error(`[fact-checker] backend given up: ${reason}`);
  endSessionWithError(ERR.ERR_BACKEND_DOWN, "backend_down").catch((error) => {
    console.error("[fact-checker] give-up handling failed:", error);
  });
};

const handleSocketState = (wsState, reconnectAttempt) => {
  const patch = {wsState, reconnectAttempt};
  if (
    wsState === "reconnecting" &&
    (session.status === "capturing" || session.status === "starting")
  ) {
    patch.status = "reconnecting";
  }
  writeSession(patch).catch((error) => {
    console.error("[fact-checker] ws-state persistence failed:", error);
  });
};

const handleServerFrame = (frame) => {
  forwardBackendEvent(frame);
  if (frame.type === "ready") {
    // starting/reconnecting -> capturing (open + ready, per the state machine).
    writeSession({status: "capturing", wsState: "open", reconnectAttempt: 0}).catch(
      (error) => {
        console.error("[fact-checker] ready-state persistence failed:", error);
      }
    );
    return;
  }
  if (frame.type === "error" && frame.fatal === true) {
    // e.g. "superseded" — another client preempted this session.
    console.error(
      `[fact-checker] fatal backend error (${frame.code}): ${frame.message}`
    );
    endSessionWithError(`backend:${frame.code}`, frame.code).catch((error) => {
      console.error("[fact-checker] fatal-frame handling failed:", error);
    });
  }
};

/**
 * OFFSCREEN_START: redeem the stream id IMMEDIATELY (it expires in seconds),
 * then connect the WebSocket. Capture first, WebSocket second.
 */
const handleStart = async ({streamId, tabId, settings}) => {
  if (capture || socket) {
    console.warn(
      "[fact-checker] OFFSCREEN_START during an active session; replacing it"
    );
    await teardown();
  }
  sessionSettings = {...settings};
  capture = new AudioCapture({
    onFrame: handleAudioFrame,
    onEnded: handleCaptureLost,
  });
  // Issue getUserMedia in this tick; persist the transition while it runs.
  const captureStarted = capture.start(streamId);
  await writeSession({
    status: "starting",
    tabId,
    startedAt: Date.now(),
    wsState: "connecting",
    reconnectAttempt: 0,
    lastError: null,
  });
  try {
    await captureStarted;
  } catch (error) {
    console.error("[fact-checker] stream-id redemption failed:", error);
    await endSessionWithError(ERR.ERR_CAPTURE_FAILED, "capture_failed");
    return;
  }
  socket = new BackendSocket({
    url: sessionSettings.backendUrl,
    hello: {
      type: "hello",
      version: 1,
      format: "pcm_s16le",
      sample_rate: AUDIO_SAMPLE_RATE,
      channels: 1,
      sensitivity: sessionSettings.sensitivity,
      send_transcripts: Boolean(sessionSettings.sendTranscripts),
    },
    onEvent: handleServerFrame,
    onStateChange: handleSocketState,
    onGiveUp: handleSocketGiveUp,
  });
  socket.connect();
};

/**
 * OFFSCREEN_STOP: stop feeding audio, then close the socket gracefully
 * ({"type":"stop"} lets the server flush and run a final gate pass). Flush
 * frames (status/verdict) keep flowing to the overlay until the server
 * closes 1000 (BackendSocket.stop bounds the wait) — otherwise the final
 * gate/verify results, and the grounded quota they spent, would be lost.
 * `reason` distinguishes a user stop from the captured tab being closed.
 */
const handleStop = async ({reason = "user_stop"} = {}) => {
  await writeSession({status: "stopping"});
  const activeSocket = socket;
  const activeCapture = capture;
  socket = null;
  capture = null;
  if (activeCapture) {
    activeCapture.onFrame = null;
    activeCapture.onEnded = null;
    try {
      await activeCapture.stop();
    } catch (error) {
      console.warn("[fact-checker] capture stop failed:", error);
    }
  }
  if (activeSocket) {
    // Forward flush frames only — no state-machine re-entry: "ready" cannot
    // arrive mid-flush, and a fatal frame must not clobber the stop path.
    // session.tabId stays set until the final write below, so forwarding works.
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
        `[fact-checker] session dropped ${activeSocket.droppedMs} ms of audio while disconnected`
      );
    }
  }
  await writeSession({
    status: "idle",
    tabId: null,
    startedAt: null,
    wsState: "closed",
    reconnectAttempt: 0,
    lastError: reason === "tab_closed" ? ERR.ERR_TAB_CLOSED : null,
  });
  await sendCaptureEnded(reason);
};

/**
 * OFFSCREEN_CONFIG: live sensitivity change, relayed by the service worker
 * (offscreen documents cannot observe chrome.storage.onChanged themselves).
 * Forward as a WS config frame mid-session; BackendSocket also folds it into
 * the hello so a later reconnect handshakes with the current value.
 * (backendUrl changes take effect on the next start — documented behavior.)
 */
const handleConfigChange = ({sensitivity} = {}) => {
  // Guard socket === null: a config message can race session teardown.
  if (!socket || !sensitivity || sensitivity === sessionSettings?.sensitivity) {
    return;
  }
  sessionSettings = {...sessionSettings, sensitivity};
  const delivered = socket.sendConfig({sensitivity});
  console.info(
    `[fact-checker] sensitivity -> ${sensitivity} (${
      delivered ? "sent now" : "will apply via reconnect hello"
    })`
  );
};

chrome.runtime.onMessage.addListener((message) => {
  if (!message || message.target !== TARGET.OFFSCREEN) {
    return false;
  }
  switch (message.type) {
    case MSG.OFFSCREEN_START:
      handleStart(message.payload).catch((error) => {
        console.error("[fact-checker] OFFSCREEN_START failed:", error);
        endSessionWithError(ERR.ERR_CAPTURE_FAILED, "capture_failed").catch(
          (cleanupError) => {
            console.error("[fact-checker] cleanup failed:", cleanupError);
          }
        );
      });
      break;
    case MSG.OFFSCREEN_STOP:
      handleStop(message.payload ?? {}).catch((error) => {
        console.error("[fact-checker] OFFSCREEN_STOP failed:", error);
      });
      break;
    case MSG.OFFSCREEN_CONFIG:
      handleConfigChange(message.payload ?? {});
      break;
    default:
      break;
  }
  return false;
});
