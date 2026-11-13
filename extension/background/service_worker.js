/**
 * Background message router — a service worker in Chrome, an event page in
 * Firefox (the manifest declares both; each browser picks its own).
 *
 * TWO CAPTURE PATHS, chosen by `HAS_OFFSCREEN_CAPTURE`:
 *
 *  - Chrome: tabCapture mints a stream id and the OFFSCREEN DOCUMENT owns
 *    the session (MediaStream + AudioContext + WebSocket). This file stays a
 *    stateless router — MV3 service workers die after ~30 s idle, so the
 *    captured tab id lives in chrome.storage.session and is re-read on every
 *    event.
 *  - Firefox: neither tabCapture nor offscreen documents exist, so the
 *    CONTENT SCRIPT taps the page's <video> and streams base64 PCM here,
 *    where `page_session.js` owns the WebSocket. Firefox's background page
 *    has a DOM and stays alive under the 4 msg/s audio cadence.
 *    See shared/capabilities.js for the full rationale.
 *
 * Responsibilities:
 *  - offscreen document lifecycle (Chrome) / page_session wiring (Firefox),
 *  - the capture start sequence (offscreen doc FIRST, then getMediaStreamId,
 *    then OFFSCREEN_START in the same tick — the stream id expires in seconds),
 *  - one automatic full-sequence retry on stream-id failure,
 *  - tabs.onRemoved guard for the captured tab,
 *  - relaying BACKEND_EVENT frames to the captured tab's content script,
 *  - persisting the session owner's SESSION_UPDATE state to
 *    chrome.storage.session (offscreen docs cannot access chrome.storage),
 *  - forwarding sync-settings changes to the session owner,
 *  - opening the app tab on install, and for content scripts (OPEN_OPTIONS),
 *  - routing the captured tab's ad-break reports (AD_STATE) to the session
 *    owner, which drops audio while an ad plays (shared/ad_pause.js),
 *  - choosing the engine: with "This browser" chosen for checking, the
 *    start settings carry the provider keys and spending limits and the
 *    offscreen document runs the whole pipeline with no backend
 *    (engine/engine_setup.mjs); removing a key in use ends the session,
 *  - choosing where speech is transcribed: when the viewer picked "This
 *    browser", the start settings carry the tested model configuration
 *    (engine/stt/speech_setup.mjs) and the offscreen document sends text
 *    instead of audio; this worker also keeps the GPU crash guard,
 *  - recording the session in the viewer's ledger (ledger/): every verdict,
 *    claim and usage frame, attributed to the channel on screen, plus the
 *    daily retention sweep,
 *  - closing the offscreen document on CAPTURE_ENDED.
 */

import {
  ENGINE_MODE,
  KEYS_STORAGE_KEY,
  normalizeKeys,
  planBrowserEngine,
} from "../engine/engine_setup.mjs";
import {
  loadEngineState,
  loadProviderKeys,
  protectStorage,
} from "../engine/engine_store.mjs";
import {
  SPEECH_ENGINE,
  beginGpuAttempt,
  blockGpu,
  endGpuAttempt,
  planBrowserSpeech,
} from "../engine/stt/speech_setup.mjs";
import {
  browserMajorVersion,
  loadGuardedSpeechState,
  loadRuntimeVersion,
  updateSpeechState,
} from "../engine/stt/speech_store.mjs";
import * as ledger from "../ledger/ledger.mjs";
import {currentSpend, monthStartIso} from "../ledger/queries.mjs";
import {createSessionRecorder} from "../ledger/session_recorder.mjs";
import {identityFromPage} from "../ledger/stream_identity.mjs";
import {APP_ROUTE, openAppTab} from "../shared/app_tab.js";
import {HAS_OFFSCREEN_CAPTURE} from "../shared/capabilities.js";
import {ERR, MSG, TARGET, makeMsg} from "../shared/messages.js";
import {
  deriveBackendHttpOrigin,
  getEnabledTopicSlugs,
  loadSettings,
} from "../shared/settings.js";
import * as pageSession from "./page_session.js";

// Promise-safe API namespace (see shared/capabilities.js § namespace idiom).
const chrome = globalThis.browser ?? globalThis.chrome;

const OFFSCREEN_DOCUMENT_PATH = "offscreen/offscreen.html";
const CAPTURED_TAB_KEY = "capturedTabId";
const CAPTURE_SESSION_KEY = "captureSession";
// The provider ids a running browser-engine session is using, so removing
// one of their keys can end it.
const SESSION_PROVIDERS_KEY = "sessionProviders";
const LEDGER_RECORDER_KEY = "ledgerRecorder";
const RETENTION_ALARM = "ledger-retention";
// How long capture start waits for the page to say who owns the stream.
// Identity is a nicety; a slow page must not delay the capture.
const PAGE_IDENTITY_TIMEOUT_MS = 400;

const ACTIVE_ICON_PATHS = {
  16: "icons/icon16.png",
  32: "icons/icon32.png",
  48: "icons/icon48.png",
  128: "icons/icon128.png",
};

const OFF_ICON_PATHS = {
  16: "icons/icon16_off.png",
  32: "icons/icon32_off.png",
  48: "icons/icon48_off.png",
  128: "icons/icon128_off.png",
};

const IDLE_SESSION = Object.freeze({
  status: "idle",
  tabId: null,
  startedAt: null,
  wsState: "closed",
  reconnectAttempt: 0,
  lastError: null,
  adPaused: false,
  speech: null,
});

/**
 * Writes the session into the viewer's history. Its small running state
 * (which ledger session is open, what the tab shows) lives in
 * chrome.storage.session because this worker is restarted at will.
 */
const recorder = createSessionRecorder({
  ledger,
  memory: {
    get: async () =>
      (await chrome.storage.session.get(LEDGER_RECORDER_KEY))[LEDGER_RECORDER_KEY] ?? null,
    set: async (state) => {
      if (state === null) {
        await chrome.storage.session.remove(LEDGER_RECORDER_KEY);
      } else {
        await chrome.storage.session.set({[LEDGER_RECORDER_KEY]: state});
      }
    },
  },
});

/**
 * History is a record, not part of the capture path: a failed write is
 * logged and the session carries on.
 */
const recordSafely = (what, work) => {
  work.catch((error) => {
    console.error(`[fact-checker] history: ${what} failed:`, error);
  });
};

const getCapturedTabId = async () => {
  const stored = await chrome.storage.session.get(CAPTURED_TAB_KEY);
  return stored[CAPTURED_TAB_KEY] ?? null;
};

const setCapturedTabId = async (tabId) => {
  if (tabId === null) {
    await chrome.storage.session.remove(CAPTURED_TAB_KEY);
  } else {
    await chrome.storage.session.set({[CAPTURED_TAB_KEY]: tabId});
  }
};

const readCaptureSession = async () => {
  const stored = await chrome.storage.session.get(CAPTURE_SESSION_KEY);
  return stored[CAPTURE_SESSION_KEY] ?? {...IDLE_SESSION};
};

const writeCaptureSession = async (patch) => {
  const current = await readCaptureSession();
  await chrome.storage.session.set({
    [CAPTURE_SESSION_KEY]: {...current, ...patch},
  });
};

/**
 * Persist the offscreen document's authoritative session state on its
 * behalf (offscreen documents cannot access chrome.storage). Deliberately a
 * plain full-object set, NOT the read-modify-write writeCaptureSession
 * helper: message handlers run concurrently, so merging two rapid
 * SESSION_UPDATEs could interleave read/read/write/write and lose one. The
 * offscreen doc always sends the complete session object, so plain sets are
 * last-writer-wins in message order.
 */
const persistSessionUpdate = async (session) => {
  if (!session || typeof session !== "object") {
    console.error("[fact-checker] SESSION_UPDATE without a session payload");
    return;
  }
  await chrome.storage.session.set({[CAPTURE_SESSION_KEY]: session});
};

const setToolbarIcon = async (active) => {
  try {
    await chrome.action.setIcon({
      path: active ? ACTIVE_ICON_PATHS : OFF_ICON_PATHS,
    });
  } catch (error) {
    console.warn("[fact-checker] setIcon failed:", error);
  }
};

const ensureOffscreenDocument = async () => {
  if (await chrome.offscreen.hasDocument()) {
    return;
  }
  await chrome.offscreen.createDocument({
    url: OFFSCREEN_DOCUMENT_PATH,
    reasons: ["USER_MEDIA"],
    justification:
      "Capture tab audio and stream it to the local fact-checking backend.",
  });
};

const closeOffscreenDocument = async () => {
  if (!HAS_OFFSCREEN_CAPTURE) {
    return; // Firefox: no offscreen documents exist to close
  }
  try {
    if (await chrome.offscreen.hasDocument()) {
      await chrome.offscreen.closeDocument();
    }
  } catch (error) {
    console.warn("[fact-checker] closing offscreen document failed:", error);
  }
};

/**
 * Notify the captured tab's content script of session start/stop. The tab
 * may not be a supported stream page (no content script) or may have
 * navigated, so delivery failure is expected and non-fatal.
 */
const notifySessionState = async (tabId, running) => {
  try {
    await chrome.tabs.sendMessage(tabId, {
      type: MSG.SESSION_STATE,
      payload: {running},
    });
  } catch (error) {
    console.debug("[fact-checker] SESSION_STATE not delivered:", error);
  }
};

/**
 * Ask the tab's content script who owns the stream (the channel link under
 * the player). Null when there is no content script, it predates this
 * message, or it does not answer in time — all normal.
 *
 * @param {number} tabId
 * @returns {Promise<{ownerHref: string|null, ownerName: string|null}|null>}
 */
const askPageForOwner = async (tabId) => {
  try {
    const reply = await Promise.race([
      chrome.tabs.sendMessage(tabId, {type: MSG.GET_PAGE_IDENTITY, payload: {}}),
      new Promise((resolve) => setTimeout(() => resolve(null), PAGE_IDENTITY_TIMEOUT_MS)),
    ]);
    return reply && typeof reply === "object" ? reply : null;
  } catch (error) {
    console.debug("[fact-checker] page identity not available:", error);
    return null;
  }
};

/**
 * What a tab is showing: platform, channel, stream URL and title. Every
 * field is best-effort and may be null. No "tabs" permission needed —
 * activeTab, granted by the popup interaction that triggers CAPTURE_START,
 * exposes the tab's url/title.
 *
 * @param {chrome.tabs.Tab|null} tab
 */
const resolveTabIdentity = async (tab) => {
  if (!tab) {
    return identityFromPage();
  }
  const owner = await askPageForOwner(tab.id);
  return identityFromPage({
    url: tab.url ?? "",
    title: tab.title ?? "",
    ownerHref: owner?.ownerHref ?? null,
    ownerName: owner?.ownerName ?? null,
  });
};

/**
 * One full start pass. Order is load-bearing: the offscreen document must
 * exist BEFORE the stream id is requested, and OFFSCREEN_START must go out
 * in the same tick the id resolves — getMediaStreamId ids expire in seconds.
 */
const runStartSequence = async (tabId, offscreenSettings) => {
  await ensureOffscreenDocument();
  const streamId = await chrome.tabCapture.getMediaStreamId({
    targetTabId: tabId,
  });
  await chrome.runtime.sendMessage(
    makeMsg(TARGET.OFFSCREEN, MSG.OFFSCREEN_START, {
      streamId,
      tabId,
      settings: offscreenSettings,
    })
  );
};

const handleCaptureStart = async (tabId) => {
  if (typeof tabId !== "number") {
    console.error("[fact-checker] CAPTURE_START without a numeric tabId");
    return;
  }
  await writeCaptureSession({
    status: "starting",
    tabId,
    startedAt: Date.now(),
    wsState: "connecting",
    reconnectAttempt: 0,
    lastError: null,
    adPaused: false,
  });
  const settings = await loadSettings();
  // Identity is best-effort: a tabs.get failure must not abort the start.
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  // Which engine checks claims. The browser engine is Chrome-only (it needs
  // the offscreen document); Firefox always uses the backend.
  let browserEngine = null;
  if (HAS_OFFSCREEN_CAPTURE) {
    const engineState = await loadEngineState();
    if (engineState.mode === ENGINE_MODE.BROWSER) {
      const plan = planBrowserEngine(engineState, await loadProviderKeys());
      if (!plan.ready) {
        console.error(`[fact-checker] browser engine not ready: ${plan.reason}`);
        await writeCaptureSession({
          status: "error",
          tabId: null,
          wsState: "closed",
          lastError: ERR.ERR_NOT_CONFIGURED,
        });
        return;
      }
      // Where the viewer stands against the spending limits. A history that
      // cannot be read must not stop the session; it starts from zero.
      const spent = await ledger
        .loadUsageSince(monthStartIso())
        .then((usage) => currentSpend(usage))
        .catch((error) => {
          console.warn("[fact-checker] could not read this month's usage:", error);
          return {checksToday: 0, spendThisMonthUsd: 0};
        });
      browserEngine = {
        gate: plan.gate,
        verify: plan.verify,
        budgets: engineState.budgets,
        spent,
      };
    }
  }
  await chrome.storage.session.set({
    [SESSION_PROVIDERS_KEY]: browserEngine
      ? [browserEngine.gate.providerId, browserEngine.verify.providerId]
      : [],
  });

  // Where speech is transcribed. With the browser engine there is no backend
  // to send audio to, so speech is in the browser whatever the setting says.
  let speechConfig = null;
  if (HAS_OFFSCREEN_CAPTURE) {
    const {state: speechState, tripped} = await loadGuardedSpeechState();
    if (tripped) {
      console.warn("[fact-checker] GPU speech blocked after an unfinished attempt");
    }
    if (speechState.settings.engine === SPEECH_ENGINE.BROWSER || browserEngine) {
      const plan = planBrowserSpeech(speechState, {
        browserMajor: browserMajorVersion(),
        runtimeVersion: (await loadRuntimeVersion()) ?? "missing",
      });
      if (!plan.ready) {
        // The viewer asked for in-browser speech and it cannot run. Say so
        // rather than quietly sending their audio somewhere else.
        console.error(`[fact-checker] in-browser speech not ready: ${plan.reason}`);
        await writeCaptureSession({
          status: "error",
          tabId: null,
          wsState: "closed",
          lastError: ERR.ERR_SPEECH_NOT_READY,
        });
        return;
      }
      speechConfig = plan.config;
    }
  }
  const identity = await resolveTabIdentity(tab);
  recordSafely("capture start", recorder.captureStarting(tabId, identity));
  const offscreenSettings = {
    // Non-null: transcribe in the offscreen document and send text.
    speech: speechConfig,
    // Non-null: check claims in the offscreen document, with no backend.
    engine: browserEngine,
    appVersion: chrome.runtime.getManifest().version,
    backendUrl: settings.backendUrl,
    sensitivity: settings.sensitivity,
    topics: getEnabledTopicSlugs(settings.topics),
    sendTranscripts: settings.showTranscript,
    // Opt-in frame capture; next-start-only (same precedent as backendUrl).
    captureVideo: Boolean(settings.captureVideo),
    // Session identity for the backend's own analytics (hello frame).
    platform: identity.platform,
    channel: identity.channelId,
    streamTitle: identity.streamTitle,
  };
  if (!HAS_OFFSCREEN_CAPTURE) {
    // Firefox: open the socket here, then ask the content script to start
    // tapping the page's <video>. Socket first so the hello handshake is
    // already in flight when the first PCM frame lands.
    try {
      await pageSession.start(tabId, offscreenSettings);
      await chrome.tabs.sendMessage(tabId, {
        type: MSG.CONTENT_CAPTURE_START,
        payload: {captureVideo: offscreenSettings.captureVideo},
      });
    } catch (error) {
      console.error("[fact-checker] content capture start failed:", error);
      await pageSession.captureFailed();
      recordSafely("capture end", recorder.captureEnded());
      return;
    }
    await setCapturedTabId(tabId);
    await setToolbarIcon(true);
    await notifySessionState(tabId, true);
    return;
  }
  try {
    await runStartSequence(tabId, offscreenSettings);
  } catch (firstError) {
    console.warn(
      "[fact-checker] start sequence failed; retrying once with a fresh stream id:",
      firstError
    );
    try {
      await runStartSequence(tabId, offscreenSettings);
    } catch (secondError) {
      console.error("[fact-checker] start sequence failed twice:", secondError);
      recordSafely("capture end", recorder.captureEnded());
      await closeOffscreenDocument();
      await writeCaptureSession({
        status: "error",
        tabId: null,
        wsState: "closed",
        lastError: ERR.ERR_STREAM_ID_EXPIRED,
      });
      return;
    }
  }
  await setCapturedTabId(tabId);
  await setToolbarIcon(true);
  await notifySessionState(tabId, true);
};

/**
 * Ask the offscreen document to stop gracefully. If it is already gone
 * (sendMessage rejects with "receiving end does not exist"), finish the
 * cleanup here so the popup never shows a zombie session.
 */
const handleCaptureStop = async (reason) => {
  if (!HAS_OFFSCREEN_CAPTURE) {
    // Firefox: silence the content script first (a late PCM frame must not
    // outlive the socket), then flush and close it here.
    const capturedTabId = await getCapturedTabId();
    if (capturedTabId !== null) {
      await chrome.tabs
        .sendMessage(capturedTabId, {type: MSG.CONTENT_CAPTURE_STOP, payload: {}})
        .catch((error) => {
          console.debug("[fact-checker] CONTENT_CAPTURE_STOP not delivered:", error);
        });
    }
    await pageSession.stop(reason);
    return;
  }
  try {
    await chrome.runtime.sendMessage(
      makeMsg(TARGET.OFFSCREEN, MSG.OFFSCREEN_STOP, {reason})
    );
  } catch (error) {
    console.warn(
      "[fact-checker] offscreen unreachable during stop; cleaning up directly:",
      error
    );
    recordSafely("capture end", recorder.captureEnded());
    await closeOffscreenDocument();
    await setCapturedTabId(null);
    await setToolbarIcon(false);
    await writeCaptureSession({
      ...IDLE_SESSION,
      lastError: reason === "tab_closed" ? ERR.ERR_TAB_CLOSED : null,
    });
  }
};

/**
 * The offscreen document has fully torn down (it writes its final
 * captureSession state BEFORE sending CAPTURE_ENDED), so the only work left
 * is releasing the document and clearing the routing state.
 */
const handleCaptureEnded = async (reason) => {
  console.info(`[fact-checker] capture ended (reason: ${reason ?? "unknown"})`);
  recordSafely("capture end", recorder.captureEnded());
  const capturedTabId = await getCapturedTabId();
  await setCapturedTabId(null);
  await setToolbarIcon(false);
  if (capturedTabId !== null) {
    await notifySessionState(capturedTabId, false);
  }
  await closeOffscreenDocument();
};

const relayBackendEvent = async (payload) => {
  const tabId = payload?.tabId;
  if (typeof tabId !== "number" || !payload.event) {
    return;
  }
  // Not awaited: the overlay must not wait on a database write.
  recordSafely("engine event", recorder.engineEvent(tabId, payload.event));
  try {
    await chrome.tabs.sendMessage(tabId, {
      type: MSG.OVERLAY_EVENT,
      payload: {event: payload.event},
    });
  } catch (error) {
    // Expected when the captured tab has no content script (unsupported
    // page) or has navigated.
    console.debug("[fact-checker] OVERLAY_EVENT not delivered:", error);
  }
};

/**
 * Record one 👍/👎 in the viewer's history and POST it to the backend, which
 * keeps its own copy for prompt evaluation. The POST is fire-and-forget by
 * contract: failures are logged, never surfaced — a lost rating is not
 * worth interrupting the viewer.
 */
const handleSubmitFeedback = async (payload) => {
  const verdictId = payload?.verdictId;
  const rating = payload?.rating;
  if (typeof verdictId !== "string" || !["up", "down"].includes(rating)) {
    console.warn("[fact-checker] SUBMIT_FEEDBACK with bad payload:", payload);
    return;
  }
  recordSafely(
    "feedback",
    recorder.feedback({
      verdictId,
      rating,
      correctedLabel: payload.correctedLabel ?? null,
      note: payload.note ?? null,
    })
  );
  // Only the companion backend keeps its own copy of ratings.
  if ((await loadEngineState()).mode === ENGINE_MODE.BROWSER) {
    return;
  }
  const settings = await loadSettings();
  const origin = deriveBackendHttpOrigin(settings.backendUrl);
  if (!origin) {
    return;
  }
  try {
    const response = await fetch(`${origin}/feedback`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        verdict_id: verdictId,
        rating,
        corrected_label: payload.correctedLabel ?? null,
        note: payload.note ?? null,
      }),
      signal: AbortSignal.timeout(5000),
    });
    if (!response.ok) {
      console.warn(`[fact-checker] feedback POST got HTTP ${response.status}`);
    }
  } catch (error) {
    console.warn("[fact-checker] feedback POST failed:", error);
  }
};

/**
 * AD_STATE from a content script: forward to the session owner, but only
 * for the tab actually being captured — every supported tab runs the
 * content script, and an ad in some other tab says nothing about this
 * session's audio.
 */
const handleAdState = async (sender, payload) => {
  const capturedTabId = await getCapturedTabId();
  if (capturedTabId === null || sender.tab?.id !== capturedTabId) {
    return;
  }
  const active = Boolean(payload?.active);
  if (!HAS_OFFSCREEN_CAPTURE) {
    pageSession.setAdActive(active);
    return;
  }
  if (!(await chrome.offscreen.hasDocument())) {
    return;
  }
  try {
    await chrome.runtime.sendMessage(
      makeMsg(TARGET.OFFSCREEN, MSG.OFFSCREEN_AD_STATE, {active})
    );
  } catch (error) {
    // The document can close between hasDocument() and delivery.
    console.debug("[fact-checker] OFFSCREEN_AD_STATE not delivered:", error);
  }
};

/**
 * SPEECH_GPU_ATTEMPT from the offscreen document: store the crash-guard
 * marker ("begin") or clear it ("end"). The caller waits for this.
 */
const handleGpuAttempt = async ({phase, variantId} = {}) => {
  await updateSpeechState((state) =>
    phase === "begin"
      ? beginGpuAttempt(state, {
          context: "session",
          variantId: String(variantId ?? "unknown"),
          at: new Date().toISOString(),
        })
      : endGpuAttempt(state)
  );
};

/**
 * PAGE_IDENTITY from a content script: the page moved to another stream, or
 * its channel link appeared. Only the captured tab matters.
 */
const handlePageIdentity = async (sender, payload) => {
  const capturedTabId = await getCapturedTabId();
  if (capturedTabId === null || sender.tab?.id !== capturedTabId) {
    return;
  }
  await recorder.identityChanged(
    capturedTabId,
    identityFromPage({
      // The content script reports its own location: sender.tab.url is only
      // filled in for hosts the extension holds a permission for, and the
      // stream sites are reached through content-script matches alone.
      url: payload?.url ?? sender.url ?? "",
      title: payload?.title ?? "",
      ownerHref: payload?.ownerHref ?? null,
      ownerName: payload?.ownerName ?? null,
    })
  );
};

const buildSessionStateReply = async (sender) => {
  const capturedTabId = await getCapturedTabId();
  const session = await readCaptureSession();
  const running =
    sender.tab?.id != null &&
    sender.tab.id === capturedTabId &&
    ["starting", "capturing", "reconnecting"].includes(session.status);
  return {type: MSG.SESSION_STATE, payload: {running}};
};

const handleTabRemoved = async (tabId) => {
  const capturedTabId = await getCapturedTabId();
  if (capturedTabId === null || tabId !== capturedTabId) {
    return;
  }
  console.info("[fact-checker] captured tab closed; stopping capture");
  await handleCaptureStop("tab_closed");
};

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || message.target !== TARGET.BACKGROUND) {
    return false;
  }
  if (message.type === MSG.SPEECH_GPU_ATTEMPT) {
    // Answered only once the marker is stored: "begin" must be on disk
    // before the offscreen document touches the GPU.
    handleGpuAttempt(message.payload)
      .catch((error) => {
        console.error("[fact-checker] storing the GPU attempt failed:", error);
      })
      .finally(() => sendResponse({stored: true}));
    return true;
  }
  if (message.type === MSG.CONTENT_READY) {
    buildSessionStateReply(sender)
      .then(sendResponse)
      .catch((error) => {
        console.error("[fact-checker] CONTENT_READY reply failed:", error);
        sendResponse({type: MSG.SESSION_STATE, payload: {running: false}});
      });
    return true; // keep sendResponse alive for the async reply
  }
  const route = async () => {
    switch (message.type) {
      case MSG.CAPTURE_START:
        await handleCaptureStart(message.payload?.tabId);
        break;
      case MSG.CAPTURE_STOP:
        await handleCaptureStop(message.payload?.reason ?? "user_stop");
        break;
      case MSG.BACKEND_EVENT:
        await relayBackendEvent(message.payload);
        break;
      case MSG.SESSION_UPDATE:
        await persistSessionUpdate(message.payload?.session);
        break;
      case MSG.CAPTURE_ENDED:
        await handleCaptureEnded(message.payload?.reason);
        break;
      case MSG.OPEN_OPTIONS:
        // Content scripts cannot open extension pages themselves.
        await openAppTab(message.payload?.route ?? APP_ROUTE.SETTINGS);
        break;
      case MSG.SUBMIT_FEEDBACK:
        await handleSubmitFeedback(message.payload);
        break;
      case MSG.AD_STATE:
        await handleAdState(sender, message.payload);
        break;
      case MSG.PAGE_IDENTITY:
        await handlePageIdentity(sender, message.payload);
        break;
      case MSG.SPEECH_GPU_FAILED:
        await updateSpeechState((state) =>
          blockGpu(state, {
            at: new Date().toISOString(),
            reason: String(message.payload?.reason ?? "The GPU failed during a session."),
          })
        );
        break;
      // Firefox capture path (content script -> this page's WebSocket).
      case MSG.AUDIO_CHUNK:
        pageSession.submitAudio(message.payload?.pcmB64);
        break;
      case MSG.VIDEO_FRAME:
        pageSession.submitVideoFrame(message.payload ?? {});
        break;
      case MSG.CONTENT_CAPTURE_FAILED:
        console.error(
          "[fact-checker] content capture failed:",
          message.payload?.reason
        );
        await pageSession.captureFailed();
        break;
      default:
        console.warn(`[fact-checker] unhandled message type: ${message.type}`);
    }
  };
  route().catch((error) => {
    console.error(`[fact-checker] handling ${message.type} failed:`, error);
  });
  return false;
});

// First install: open the app on its welcome flow. Updates and browser
// restarts must NOT pop a tab open.
chrome.runtime.onInstalled.addListener(({reason}) => {
  if (reason !== "install") {
    return;
  }
  openAppTab(APP_ROUTE.WELCOME).catch((error) => {
    console.error("[fact-checker] opening the welcome page failed:", error);
  });
});

/**
 * History retention: once a day, delete sessions older than the viewer's
 * setting (0 = keep until deleted by hand). `alarms.create` with the same
 * name replaces the existing alarm, so calling this on every worker start
 * is safe and keeps the alarm alive across updates.
 */
chrome.alarms?.create(RETENTION_ALARM, {delayInMinutes: 1, periodInMinutes: 24 * 60});
chrome.alarms?.onAlarm.addListener((alarm) => {
  if (alarm.name !== RETENTION_ALARM) {
    return;
  }
  loadSettings()
    .then((settings) => recorder.applyRetention(settings.historyRetentionDays))
    .then((removed) => {
      if (removed > 0) {
        console.info(`[fact-checker] history retention removed ${removed} session(s)`);
      }
    })
    .catch((error) => {
      console.error("[fact-checker] history retention sweep failed:", error);
    });
});

chrome.tabs.onRemoved.addListener((tabId) => {
  handleTabRemoved(tabId).catch((error) => {
    console.error("[fact-checker] tabs.onRemoved handling failed:", error);
  });
});

// Firefox capture path: give page_session.js the same session/tab plumbing
// the offscreen document reaches through messages. Harmless on Chrome — the
// module is inert unless HAS_OFFSCREEN_CAPTURE is false.
pageSession.configure({
  onSessionPatch: writeCaptureSession,
  onBackendEvent: (tabId, event) => {
    relayBackendEvent({tabId, event}).catch((error) => {
      console.debug("[fact-checker] backend event relay failed:", error);
    });
  },
  onEnded: handleCaptureEnded,
});

/**
 * Relay live sensitivity and topic-filter changes from the app's Settings to
 * the offscreen document (offscreen docs cannot observe
 * chrome.storage.onChanged), which forwards them as a WS config frame.
 * Topics travel as the enabled-slug array (the OFFSCREEN_CONFIG payload
 * shape: {sensitivity?, topics?}). No offscreen document — i.e. no active
 * session — is the common case for options changes, not an error.
 */
const relaySettingsChange = async (changes) => {
  const newSettings = changes.settings.newValue ?? {};
  const oldSettings = changes.settings.oldValue ?? {};
  const configPatch = {};
  if (
    newSettings.sensitivity &&
    newSettings.sensitivity !== oldSettings.sensitivity
  ) {
    configPatch.sensitivity = newSettings.sensitivity;
  }
  if (newSettings.topics) {
    const newEnabledTopics = getEnabledTopicSlugs(newSettings.topics);
    const oldEnabledTopics = getEnabledTopicSlugs(oldSettings.topics);
    if (newEnabledTopics.join(",") !== oldEnabledTopics.join(",")) {
      configPatch.topics = newEnabledTopics;
    }
  }
  if (Object.keys(configPatch).length === 0) {
    return;
  }
  if (!HAS_OFFSCREEN_CAPTURE) {
    // Firefox: the socket lives here, so apply the change directly (a no-op
    // when no session is running).
    pageSession.sendConfig(configPatch);
    return;
  }
  if (!(await chrome.offscreen.hasDocument())) {
    return;
  }
  try {
    await chrome.runtime.sendMessage(
      makeMsg(TARGET.OFFSCREEN, MSG.OFFSCREEN_CONFIG, configPatch)
    );
  } catch (error) {
    // The document can close between hasDocument() and delivery.
    console.debug("[fact-checker] OFFSCREEN_CONFIG not delivered:", error);
  }
};

/**
 * A provider key was removed in the app. If a running session is using that
 * provider, end it now: "remove key" has to be an off-switch that works.
 */
const stopSessionIfKeyRemoved = async (change) => {
  const before = normalizeKeys(change.oldValue);
  const after = normalizeKeys(change.newValue);
  const removed = Object.keys(before).filter((providerId) => !after[providerId]);
  if (removed.length === 0 || (await getCapturedTabId()) === null) {
    return;
  }
  const stored = await chrome.storage.session.get(SESSION_PROVIDERS_KEY);
  const inUse = stored[SESSION_PROVIDERS_KEY] ?? [];
  if (!removed.some((providerId) => inUse.includes(providerId))) {
    return;
  }
  console.info("[fact-checker] a provider key in use was removed; stopping the session");
  // The offscreen document records the reason when it writes the final state.
  await handleCaptureStop("key_removed");
};

// Content scripts run inside web pages: keep them out of storage.local,
// where the provider keys are.
protectStorage();

chrome.storage.onChanged.addListener((changes, areaName) => {
  if (areaName === "local" && changes[KEYS_STORAGE_KEY]) {
    stopSessionIfKeyRemoved(changes[KEYS_STORAGE_KEY]).catch((error) => {
      console.error("[fact-checker] stopping after key removal failed:", error);
    });
    return;
  }
  if (areaName !== "sync" || !changes.settings) {
    return;
  }
  relaySettingsChange(changes).catch((error) => {
    console.error("[fact-checker] settings relay failed:", error);
  });
});
