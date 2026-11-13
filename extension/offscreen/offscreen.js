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
 *
 * Ad breaks: the content script reports ads on the captured tab
 * (OFFSCREEN_AD_STATE, relayed by the service worker). While one lasts the
 * session stays "capturing" but every audio and video frame is dropped
 * instead of sent — see shared/ad_pause.js. `session.adPaused` mirrors the
 * gate for the popup.
 *
 * Speech: by default every audio frame goes to the backend, which
 * transcribes it. When the viewer chose "This browser" (the start settings
 * then carry a tested `speech` config), frames go to a model running in a
 * worker of this document instead, and only the resulting TEXT is sent —
 * the session handshakes with format "transcript". `session.speech` mirrors
 * the model's state for the popup.
 */

import {LiveTranscriber} from "../engine/stt/live_transcriber.mjs";
import {SPEECH_STATUS, SessionSpeech} from "../engine/stt/session_speech.mjs";
import {GPU_LOCK_NAME} from "../engine/stt/speech_store.mjs";
import {AdPauseGate, buildBoundarySilenceFrames} from "../shared/ad_pause.js";
import {ERR, MSG, TARGET, makeMsg} from "../shared/messages.js";
import {AudioCapture} from "./audio_capture.js";
import {BackendSocket} from "./ws_client.js";

const AUDIO_SAMPLE_RATE = 16000;
// On Stop, how long to wait for the model to finish the last utterance.
const SPEECH_FLUSH_TIMEOUT_MS = 4000;

let capture = null;
let socket = null;
/** @type {SessionSpeech|null} in-browser speech, when the viewer chose it */
let speech = null;
let sessionSettings = null;
const adPause = new AdPauseGate();

let session = {
  status: "idle",
  tabId: null,
  startedAt: null,
  wsState: "closed",
  reconnectAttempt: 0,
  lastError: null,
  adPaused: false,
  speech: null,
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
  speech?.dispose();
  speech = null;
  if (activeSocket) {
    activeSocket.onEvent = null;
    activeSocket.onStateChange = null;
    activeSocket.onGiveUp = null;
    activeSocket.destroy();
  }
  if (activeCapture) {
    activeCapture.onFrame = null;
    activeCapture.onEnded = null;
    // Null the video callbacks too: a late sampling tick must never touch
    // a destroyed socket.
    activeCapture.onVideoFrame = null;
    activeCapture.shouldSendFrame = null;
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
  adPause.resume();
  await writeSession({
    status: "error",
    wsState: "closed",
    reconnectAttempt: 0,
    lastError,
    adPaused: false,
    speech: null,
  });
  await sendCaptureEnded(reason);
};

/** One frame of stream audio to whichever engine transcribes this session. */
const transcribeFrame = (pcmFrame) => {
  if (speech) {
    speech.pushFrame(pcmFrame);
  } else {
    socket?.sendAudio(pcmFrame);
  }
};

/**
 * An utterance transcribed in this browser: send the text to the backend
 * and, when the viewer wants the live caption, to the overlay as well (the
 * backend does not echo text it was sent).
 */
const handleLocalTranscript = (transcript) => {
  const text = transcript.text.trim();
  if (!text) {
    return;
  }
  const frame = {type: "transcript", text, start: transcript.startS, end: transcript.endS};
  socket?.sendJson(frame);
  if (sessionSettings?.sendTranscripts) {
    forwardBackendEvent(frame);
  }
};

const handleSpeechStatus = (status) => {
  if (status.status === SPEECH_STATUS.DEGRADED) {
    // Same notice the backend sends when ITS engine falls back to the CPU.
    forwardBackendEvent({
      type: "error",
      code: "stt_degraded",
      message: "Speech recognition moved to the CPU after a GPU problem; captions may lag.",
      fatal: false,
    });
  }
  writeSession({speech: {engine: "browser", ...status}}).catch((error) => {
    console.error("[fact-checker] speech-state persistence failed:", error);
  });
};

// Held while the GPU is being brought up, so another context can tell a
// stored "attempt in progress" marker that is live from one a crash left
// behind (engine/stt/speech_store.mjs).
let releaseGpuLock = null;

const holdGpuLock = () =>
  new Promise((acquired) => {
    if (!navigator.locks) {
      acquired();
      return;
    }
    navigator.locks
      .request(GPU_LOCK_NAME, () => {
        acquired();
        return new Promise((release) => {
          releaseGpuLock = release;
        });
      })
      .catch((error) => {
        console.warn("[fact-checker] GPU lock unavailable:", error);
        acquired();
      });
  });

/** Tell the service worker about GPU use; resolves once it is stored. */
const reportGpuAttempt = async (attempt) => {
  if (attempt.phase === "begin") {
    await holdGpuLock();
  }
  try {
    await chrome.runtime.sendMessage(
      makeMsg(TARGET.BACKGROUND, MSG.SPEECH_GPU_ATTEMPT, attempt)
    );
  } catch (error) {
    console.warn("[fact-checker] GPU attempt not recorded:", error);
  }
  if (attempt.phase === "end") {
    releaseGpuLock?.();
    releaseGpuLock = null;
  }
};

const startBrowserSpeech = (config) => {
  speech = new SessionSpeech({
    config,
    createTranscriber: (callbacks, options) => new LiveTranscriber(callbacks, options),
    onTranscript: handleLocalTranscript,
    onStatus: handleSpeechStatus,
    onGpuAttempt: reportGpuAttempt,
    onGpuFailed: (reason) => {
      console.error(`[fact-checker] GPU speech failed: ${reason}`);
      chrome.runtime
        .sendMessage(makeMsg(TARGET.BACKGROUND, MSG.SPEECH_GPU_FAILED, {reason}))
        .catch((error) => {
          console.warn("[fact-checker] GPU failure not recorded:", error);
        });
    },
    onFailed: (reason) => {
      console.error(`[fact-checker] in-browser speech failed: ${reason}`);
      endSessionWithError(ERR.ERR_SPEECH_FAILED, "speech_failed").catch((error) => {
        console.error("[fact-checker] speech-failure handling failed:", error);
      });
    },
  });
  // Not awaited: audio is already flowing, and frames wait in the worker's
  // queue while the model loads.
  speech.start().catch((error) => {
    console.error("[fact-checker] speech start failed:", error);
  });
};

const handleAudioFrame = (pcmFrame) => {
  if (adPause.isPaused()) {
    return; // ad break: the tab's audio is the ad, not the stream
  }
  if (session.adPaused) {
    // The pause lapsed without an explicit "ad over" — the content script
    // stopped re-asserting it (tab navigated away, extension reloaded).
    console.warn("[fact-checker] ad pause lapsed; resuming fact-checking");
    writeSession({adPaused: false}).catch((error) => {
      console.error("[fact-checker] ad-state persistence failed:", error);
    });
  }
  transcribeFrame(pcmFrame);
};

/**
 * OFFSCREEN_AD_STATE: the captured tab started or finished an ad break.
 * Repeated `active: true` messages are heartbeats that keep the pause alive
 * (it lapses on its own otherwise — see shared/ad_pause.js). The first one
 * also sends a second of silence so the transcriber's segmenter closes the
 * streamer's last utterance rather than splicing it onto post-ad speech.
 */
const handleAdState = ({active} = {}) => {
  if (!capture) {
    return; // no session — nothing to pause
  }
  const paused = Boolean(active);
  if (!paused) {
    adPause.resume();
  } else if (adPause.pause()) {
    for (const silenceFrame of buildBoundarySilenceFrames()) {
      transcribeFrame(silenceFrame);
    }
  }
  if (session.adPaused === paused) {
    return;
  }
  console.info(
    `[fact-checker] ad break ${paused ? "started; pausing" : "ended; resuming"}`
  );
  writeSession({adPaused: paused}).catch((error) => {
    console.error("[fact-checker] ad-state persistence failed:", error);
  });
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
    if (frame.code === "not_configured") {
      // Backend is up but has no API key yet — a distinct, user-fixable
      // state with its own popup copy and settings call-to-action.
      console.error(`[fact-checker] backend not configured: ${frame.message}`);
      endSessionWithError(ERR.ERR_NOT_CONFIGURED, ERR.ERR_NOT_CONFIGURED).catch(
        (error) => {
          console.error("[fact-checker] not-configured handling failed:", error);
        }
      );
      return;
    }
    if (frame.code === "credentials_updated") {
      // The user saved a new API key; the backend ended this session so the
      // new provider takes effect on the next start. Expected, not a fault —
      // distinct popup copy tells the user to simply press Start again.
      console.info(`[fact-checker] backend credentials updated: ${frame.message}`);
      endSessionWithError(
        ERR.ERR_CREDENTIALS_UPDATED,
        "credentials_updated"
      ).catch((error) => {
        console.error(
          "[fact-checker] credentials-updated handling failed:",
          error
        );
      });
      return;
    }
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
  // A new session never inherits a pause; the content script re-reports the
  // tab's ad state as soon as it learns the session is running.
  adPause.resume();
  capture = new AudioCapture({
    onFrame: handleAudioFrame,
    onEnded: handleCaptureLost,
    // Opt-in video frames: pushed straight to the backend's in-memory
    // ring over the live socket; dropped silently when the socket is down.
    onVideoFrame: ({imageB64, capturedAtMs}) => {
      socket?.sendJson({
        type: "frame",
        image_b64: imageB64,
        captured_at_ms: capturedAtMs,
      });
    },
    shouldSendFrame: () => socket?.isOpen === true && !adPause.isPaused(),
  });
  // Issue getUserMedia in this tick; persist the transition while it runs.
  const captureStarted = capture.start(streamId, {
    captureVideo: Boolean(sessionSettings.captureVideo),
  });
  await writeSession({
    status: "starting",
    tabId,
    startedAt: Date.now(),
    wsState: "connecting",
    reconnectAttempt: 0,
    lastError: null,
    adPaused: false,
    speech: null,
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
      // "transcript": this document transcribes and sends text, not audio.
      format: sessionSettings.speech ? "transcript" : "pcm_s16le",
      sample_rate: AUDIO_SAMPLE_RATE,
      channels: 1,
      sensitivity: sessionSettings.sensitivity,
      // Enabled-topic slugs; null (or omitted) means all topics enabled.
      enabled_topics: Array.isArray(sessionSettings.topics)
        ? [...sessionSettings.topics]
        : null,
      send_transcripts: Boolean(sessionSettings.sendTranscripts),
      // Ask for per-claim outcome frames, which the service worker
      // records in the viewer's history (older backends ignore this).
      send_claims: true,
      // Session identity for backend analytics (optional fields; the hello
      // object is held by reference, so reconnects carry them too).
      platform: sessionSettings.platform ?? null,
      channel: sessionSettings.channel ?? null,
      stream_title: sessionSettings.streamTitle ?? null,
    },
    onEvent: handleServerFrame,
    onStateChange: handleSocketState,
    onGiveUp: handleSocketGiveUp,
  });
  socket.connect();
  if (sessionSettings.speech) {
    startBrowserSpeech(sessionSettings.speech);
  }
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
  const activeSpeech = speech;
  socket = null;
  capture = null;
  speech = null;
  adPause.resume();
  if (activeCapture) {
    activeCapture.onFrame = null;
    activeCapture.onEnded = null;
    activeCapture.onVideoFrame = null;
    activeCapture.shouldSendFrame = null;
    try {
      await activeCapture.stop();
    } catch (error) {
      console.warn("[fact-checker] capture stop failed:", error);
    }
  }
  if (activeSpeech) {
    // The last utterance is still in the model: let it finish (bounded) so
    // its text reaches the backend before the final gate pass. `socket` is
    // already null, so send through the captured reference.
    activeSpeech.onTranscript = (transcript) => {
      const text = transcript.text.trim();
      if (text) {
        activeSocket?.sendJson({
          type: "transcript",
          text,
          start: transcript.startS,
          end: transcript.endS,
        });
      }
    };
    await activeSpeech.finish(SPEECH_FLUSH_TIMEOUT_MS);
    const {segmentsDropped, droppedSeconds} = activeSpeech.stats;
    if (segmentsDropped > 0) {
      console.info(
        `[fact-checker] speech fell behind: dropped ${segmentsDropped} utterance(s), ${droppedSeconds.toFixed(1)} s`
      );
    }
    activeSpeech.dispose();
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
    adPaused: false,
    speech: null,
  });
  await sendCaptureEnded(reason);
};

/**
 * OFFSCREEN_CONFIG: live sensitivity/topic changes, relayed by the service
 * worker (offscreen documents cannot observe chrome.storage.onChanged
 * themselves). Each field is optional and applied independently. Forward as
 * one WS config frame ({sensitivity?, enabled_topics?}) mid-session;
 * BackendSocket also folds the values into the hello so a later reconnect
 * handshakes with the current ones. (backendUrl changes take effect on the
 * next start — documented behavior.)
 */
const handleConfigChange = ({sensitivity, topics} = {}) => {
  // Guard socket === null: a config message can race session teardown.
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
  const changedFields = Object.keys(configFrame).join(", ");
  console.info(
    `[fact-checker] config change (${changedFields}) ${
      delivered ? "sent now" : "will apply via reconnect hello"
    }`
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
    case MSG.OFFSCREEN_AD_STATE:
      handleAdState(message.payload ?? {});
      break;
    default:
      break;
  }
  return false;
});
