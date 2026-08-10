/**
 * Background service worker — a stateless message router.
 *
 * MV3 service workers die after ~30 s of idle, so this file keeps ZERO
 * in-memory session state: the captured tab id lives in
 * chrome.storage.session ("capturedTabId") and is re-read on every event.
 * Everything long-lived (MediaStream, AudioContext, WebSocket, timers)
 * lives in the offscreen document.
 *
 * Responsibilities:
 *  - offscreen document lifecycle,
 *  - the capture start sequence (offscreen doc FIRST, then getMediaStreamId,
 *    then OFFSCREEN_START in the same tick — the stream id expires in seconds),
 *  - one automatic full-sequence retry on stream-id failure,
 *  - tabs.onRemoved guard for the captured tab,
 *  - relaying BACKEND_EVENT frames to the captured tab's content script,
 *  - persisting the offscreen document's SESSION_UPDATE state to
 *    chrome.storage.session (offscreen docs cannot access chrome.storage),
 *  - forwarding sync-settings changes to the offscreen doc (OFFSCREEN_CONFIG),
 *  - opening the options page for content scripts (OPEN_OPTIONS),
 *  - closing the offscreen document on CAPTURE_ENDED.
 */

import {ERR, MSG, TARGET, makeMsg} from "../shared/messages.js";
import {
  deriveBackendHttpOrigin,
  getEnabledTopicSlugs,
  loadSettings,
} from "../shared/settings.js";

const OFFSCREEN_DOCUMENT_PATH = "offscreen/offscreen.html";
const CAPTURED_TAB_KEY = "capturedTabId";
const CAPTURE_SESSION_KEY = "captureSession";

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
});

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
 * Session identity (backend analytics): platform/channel/title derived from
 * the captured tab, best-effort. Keyed like content.js's PLATFORM_ADAPTERS
 * (hostname minus leading www.). No "tabs" permission needed — activeTab,
 * granted by the popup interaction that triggers CAPTURE_START, exposes the
 * tab's url/title.
 */
const PLATFORM_BY_HOSTNAME = Object.freeze({
  "twitch.tv": "twitch",
  "youtube.com": "youtube",
  "kick.com": "kick",
  "rumble.com": "rumble",
});

// Best-effort " - Twitch"-style suffix strip from tab titles.
const TITLE_SUFFIX_RE = /\s*[-–—|·]\s*(Twitch|YouTube|Kick|Rumble)\s*$/i;

/**
 * Derive {platform, channel, streamTitle} from a tab, all-null on any
 * failure — every field is optional on the wire. Channel is the first path
 * segment for twitch/kick only (YouTube/Rumble URLs carry video ids, not
 * channel slugs).
 *
 * @param {chrome.tabs.Tab|null} tab
 * @returns {{platform: string|null, channel: string|null, streamTitle: string|null}}
 */
const deriveSessionIdentity = (tab) => {
  const identity = {platform: null, channel: null, streamTitle: null};
  if (!tab) {
    return identity;
  }
  try {
    identity.streamTitle = tab.title?.replace(TITLE_SUFFIX_RE, "").trim() || null;
    const url = new URL(tab.url ?? "");
    const hostname = url.hostname.replace(/^www\./, "");
    identity.platform = PLATFORM_BY_HOSTNAME[hostname] ?? null;
    if (identity.platform === "twitch" || identity.platform === "kick") {
      identity.channel = url.pathname.split("/").filter(Boolean)[0] ?? null;
    }
  } catch (error) {
    console.debug("[fact-checker] session identity derivation failed:", error);
  }
  return identity;
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
  });
  const settings = await loadSettings();
  // Identity is best-effort: a tabs.get failure must not abort the start.
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  const offscreenSettings = {
    backendUrl: settings.backendUrl,
    sensitivity: settings.sensitivity,
    topics: getEnabledTopicSlugs(settings.topics),
    sendTranscripts: settings.showTranscript,
    // Opt-in frame capture; next-start-only (same precedent as backendUrl).
    captureVideo: Boolean(settings.captureVideo),
    ...deriveSessionIdentity(tab),
  };
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
  try {
    await chrome.runtime.sendMessage(
      makeMsg(TARGET.OFFSCREEN, MSG.OFFSCREEN_STOP, {reason})
    );
  } catch (error) {
    console.warn(
      "[fact-checker] offscreen unreachable during stop; cleaning up directly:",
      error
    );
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
 * POST one 👍/👎 to the backend. Fire-and-forget by contract: failures are
 * logged, never surfaced — the backend is the system of record and a lost
 * rating is not worth interrupting the viewer.
 */
const handleSubmitFeedback = async (payload) => {
  const verdictId = payload?.verdictId;
  const rating = payload?.rating;
  if (typeof verdictId !== "string" || !["up", "down"].includes(rating)) {
    console.warn("[fact-checker] SUBMIT_FEEDBACK with bad payload:", payload);
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
        corrected_label: null,
        note: null,
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
        // Content scripts cannot call chrome.runtime.openOptionsPage().
        await chrome.runtime.openOptionsPage();
        break;
      case MSG.SUBMIT_FEEDBACK:
        await handleSubmitFeedback(message.payload);
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

chrome.tabs.onRemoved.addListener((tabId) => {
  handleTabRemoved(tabId).catch((error) => {
    console.error("[fact-checker] tabs.onRemoved handling failed:", error);
  });
});

/**
 * Relay live sensitivity and topic-filter changes from the options page to
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

chrome.storage.onChanged.addListener((changes, areaName) => {
  if (areaName !== "sync" || !changes.settings) {
    return;
  }
  relaySettingsChange(changes).catch((error) => {
    console.error("[fact-checker] settings relay failed:", error);
  });
});
