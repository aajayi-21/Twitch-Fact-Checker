/**
 * Background message router (the MV3 service worker).
 *
 * tabCapture mints a stream id and the OFFSCREEN DOCUMENT owns the session
 * (MediaStream + AudioContext + WebSocket or in-browser pipeline). This file
 * stays a stateless router — MV3 service workers die after ~30 s idle, so the
 * captured tab id lives in chrome.storage.session and is re-read on every
 * event.
 *
 * Responsibilities:
 *  - offscreen document lifecycle,
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
  ENGINE_STORAGE_KEY,
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
import {ERR, MSG, TARGET, makeMsg} from "../shared/messages.js";
import {
  deriveBackendHttpOrigin,
  getEnabledTopicSlugs,
  loadSettings,
} from "../shared/settings.js";

const OFFSCREEN_DOCUMENT_PATH = "offscreen/offscreen.html";
const CAPTURED_TAB_KEY = "capturedTabId";
const CAPTURE_SESSION_KEY = "captureSession";
// The provider ids a running browser-engine session is using, so removing
// one of their keys can end it.
const SESSION_PROVIDERS_KEY = "sessionProviders";
const LEDGER_RECORDER_KEY = "ledgerRecorder";
const RETENTION_ALARM = "ledger-retention";
// After a session that parked a speech model, the offscreen document (and
// the model in it) is kept for this long, so the next session does not
// rebuild it; then the alarm closes it (docs/live-settings-and-model-lifecycle.md).
const WARM_SPEECH_ALARM = "warm-speech-idle";
const WARM_SPEECH_MINUTES = 10;
// Set while verdicts cannot reach the captured tab's overlay (read by the
// popup). Kept apart from captureSession, which only the offscreen document
// writes: a full-object update from it would erase a field set here.
const OVERLAY_PROBLEM_KEY = "overlayProblem";
// How long overlay deliveries must keep failing before the popup says so.
const OVERLAY_PROBLEM_AFTER_MS = 10_000;
// Every page of this extension (popup, app, offscreen document) lives here.
const EXTENSION_ORIGIN = chrome.runtime.getURL("");
// Messages that only the extension's own pages send. A content script runs
// inside a web page, so one that sends these is refused: it could otherwise
// forge history records, session state or a capture start.
const EXTENSION_PAGE_MESSAGES = new Set([
  MSG.CAPTURE_START,
  MSG.CAPTURE_STOP,
  MSG.BACKEND_EVENT,
  MSG.SESSION_UPDATE,
  MSG.CAPTURE_ENDED,
  MSG.SPEECH_GPU_ATTEMPT,
  MSG.SPEECH_GPU_FAILED,
  MSG.RELEASE_WARM_SPEECH,
]);
// The stream sites the content scripts run on (manifest content_scripts).
const CONTENT_SCRIPT = chrome.runtime.getManifest().content_scripts?.[0] ?? {matches: [], js: []};
const STREAM_PAGE_PATTERNS = CONTENT_SCRIPT.matches.map(
  (pattern) =>
    new RegExp(`^${pattern.replace(/[.+?^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*")}$`)
);
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
 * @param {string|undefined} url
 * @returns {boolean} whether the content scripts are meant to run on it
 */
const isStreamPage = (url) =>
  typeof url === "string" && STREAM_PAGE_PATTERNS.some((pattern) => pattern.test(url));

/**
 * Make sure the stream tab has a working content script before a session
 * starts. A tab opened before the extension was installed, updated or
 * reloaded has none (or only an orphaned one that can no longer receive
 * messages): the session would run and spend money with no overlay. Inject
 * the content scripts then. Start comes from the toolbar popup, so
 * `activeTab` covers the injection; where it does not (no permission, not a
 * stream page), the session goes ahead and the popup reports the missing
 * overlay instead (see relayBackendEvent).
 *
 * @param {chrome.tabs.Tab|null} tab
 * @returns {Promise<"present"|"injected"|"not_a_stream_page"|"failed">}
 */
const ensureOverlay = async (tab) => {
  if (!tab || !isStreamPage(tab.url)) {
    return "not_a_stream_page";
  }
  const answered = await chrome.tabs
    .sendMessage(tab.id, {type: MSG.GET_PAGE_IDENTITY})
    .then(
      () => true,
      () => false
    );
  if (answered) {
    return "present";
  }
  try {
    await chrome.scripting.executeScript({target: {tabId: tab.id}, files: CONTENT_SCRIPT.js});
    console.info("[fact-checker] the stream tab predates this version; overlay injected");
    return "injected";
  } catch (error) {
    console.warn("[fact-checker] could not inject the overlay into the stream tab:", error);
    return "failed";
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

/**
 * @param {number} tabId
 * @param {object} [options]
 * @param {boolean} [options.afterSettingsChange] - an automatic restart after
 *   the backend ended the session for a settings change; if it fails, the
 *   popup keeps saying to press Start
 */
const handleCaptureStart = async (tabId, {afterSettingsChange = false} = {}) => {
  if (typeof tabId !== "number") {
    console.error("[fact-checker] CAPTURE_START without a numeric tabId");
    return;
  }
  // A warm offscreen document is about to be used again.
  await chrome.alarms.clear(WARM_SPEECH_ALARM);
  await writeCaptureSession({
    status: "starting",
    tabId,
    startedAt: Date.now(),
    wsState: "connecting",
    reconnectAttempt: 0,
    lastError: null,
    errorDetail: null,
    warning: null,
    adPaused: false,
  });
  const settings = await loadSettings();
  // Identity is best-effort: a tabs.get failure must not abort the start.
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  await clearOverlayProblem();
  await ensureOverlay(tab);
  // Which engine checks claims: the backend, or the offscreen document with
  // the viewer's own provider keys.
  let browserEngine = null;
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
    // Where the viewer stands against the spending limits. Without it the
    // limits cannot be applied, so the session does not start: assuming
    // nothing has been spent could run straight past them.
    let spent;
    try {
      spent = currentSpend(await ledger.loadUsageSince(monthStartIso()));
    } catch (error) {
      console.error("[fact-checker] could not read this month's usage:", error);
      await writeCaptureSession({
        status: "error",
        tabId: null,
        wsState: "closed",
        lastError: ERR.ERR_SPEND_UNKNOWN,
      });
      return;
    }
    browserEngine = {
      gate: plan.gate,
      verify: plan.verify,
      budgets: engineState.budgets,
      spent,
    };
  }
  await chrome.storage.session.set({
    [SESSION_PROVIDERS_KEY]: browserEngine
      ? [browserEngine.gate.providerId, browserEngine.verify.providerId]
      : [],
  });

  // Where speech is transcribed. With the browser engine there is no backend
  // to send audio to, so speech is in the browser whatever the setting says.
  let speechConfig = null;
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
        lastError: afterSettingsChange ? ERR.ERR_CREDENTIALS_UPDATED : ERR.ERR_STREAM_ID_EXPIRED,
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
/**
 * @param {string|undefined} reason
 * @param {object} [options]
 * @param {boolean} [options.keepWarm] - the offscreen document parked a
 *   loaded speech model: keep it for the warm period instead of closing it
 */
const handleCaptureEnded = async (reason, {keepWarm = false} = {}) => {
  console.info(`[fact-checker] capture ended (reason: ${reason ?? "unknown"})`);
  recordSafely("capture end", recorder.captureEnded());
  const capturedTabId = await getCapturedTabId();
  await setCapturedTabId(null);
  await setToolbarIcon(false);
  await clearOverlayProblem();
  await chrome.storage.session.set({[SESSION_PROVIDERS_KEY]: []});
  if (capturedTabId !== null) {
    await notifySessionState(capturedTabId, false);
  }
  if (keepWarm) {
    await chrome.alarms.create(WARM_SPEECH_ALARM, {delayInMinutes: WARM_SPEECH_MINUTES});
  } else {
    await closeOffscreenDocument();
  }
  if (reason === "credentials_updated" && capturedTabId !== null) {
    // The local backend rebuilt its provider stack after a key or model was
    // saved, which ends every live session. Start again on the same tab, so
    // the change applies without the viewer pressing Start.
    console.info("[fact-checker] backend settings changed; restarting the session");
    await handleCaptureStart(capturedTabId, {afterSettingsChange: true});
  }
};

/**
 * Overlay deliveries that have kept failing, for the popup. In memory: a
 * worker that restarts mid-outage simply starts counting again.
 * @type {{tabId: number, sinceMs: number, reported: boolean}|null}
 */
let overlayOutage = null;

const clearOverlayProblem = async () => {
  overlayOutage = null;
  await chrome.storage.session.remove(OVERLAY_PROBLEM_KEY);
};

/** What the viewer would see in the overlay; other frames do not count. */
const SHOWN_IN_OVERLAY = new Set(["verdict", "status", "error"]);

const noteOverlayDelivery = async (tabId, event, delivered) => {
  if (delivered) {
    if (overlayOutage) {
      const wasReported = overlayOutage.reported;
      overlayOutage = null;
      if (wasReported) {
        await chrome.storage.session.remove(OVERLAY_PROBLEM_KEY);
      }
    }
    return;
  }
  if (!SHOWN_IN_OVERLAY.has(event?.type)) {
    return;
  }
  const nowMs = Date.now();
  if (!overlayOutage || overlayOutage.tabId !== tabId) {
    overlayOutage = {tabId, sinceMs: nowMs, reported: false};
    return;
  }
  if (!overlayOutage.reported && nowMs - overlayOutage.sinceMs >= OVERLAY_PROBLEM_AFTER_MS) {
    overlayOutage.reported = true;
    await chrome.storage.session.set({
      [OVERLAY_PROBLEM_KEY]:
        "Results cannot be shown on this tab. Reload the stream page, then press Start again " +
        "(they are still saved in History).",
    });
  }
};

const relayBackendEvent = async (payload) => {
  const tabId = payload?.tabId;
  if (typeof tabId !== "number" || !payload.event) {
    return;
  }
  // Not awaited: the overlay must not wait on a database write.
  recordSafely("engine event", recorder.engineEvent(tabId, payload.event));
  let delivered = false;
  try {
    await chrome.tabs.sendMessage(tabId, {
      type: MSG.OVERLAY_EVENT,
      payload: {event: payload.event},
    });
    delivered = true;
  } catch (error) {
    // No content script (not a stream page, or one that predates this
    // version and could not be injected), or the tab navigated.
    console.debug("[fact-checker] OVERLAY_EVENT not delivered:", error);
  }
  await noteOverlayDelivery(tabId, payload.event, delivered);
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

/** @returns {boolean} whether the message came from one of this extension's pages */
const fromExtensionPage = (sender) =>
  sender?.id === chrome.runtime.id &&
  typeof sender.url === "string" &&
  sender.url.startsWith(EXTENSION_ORIGIN);

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || message.target !== TARGET.BACKGROUND) {
    return false;
  }
  if (EXTENSION_PAGE_MESSAGES.has(message.type) && !fromExtensionPage(sender)) {
    console.warn(`[fact-checker] refused ${message.type} from ${sender?.url ?? "an unknown sender"}`);
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
        await handleCaptureEnded(message.payload?.reason, {
          keepWarm: Boolean(message.payload?.keepWarm),
        });
        break;
      case MSG.RELEASE_WARM_SPEECH:
        // A speech self-test is starting in the app: an idle, parked model
        // would only compete with it for GPU memory.
        if ((await getCapturedTabId()) === null) {
          await chrome.alarms.clear(WARM_SPEECH_ALARM);
          await closeOffscreenDocument();
        }
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
 * setting (0 = keep until deleted by hand).
 *
 * Created only when missing. `alarms.create` REPLACES an alarm of the same
 * name, so creating it on every worker start re-armed it one minute out;
 * its firing woke the worker, which re-armed it again: a wake-up every
 * minute, forever. Checked at every start because Chrome may drop alarms
 * on an update or a browser restart.
 */
const ensureRetentionAlarm = async () => {
  if (!(await chrome.alarms.get(RETENTION_ALARM))) {
    await chrome.alarms.create(RETENTION_ALARM, {delayInMinutes: 1, periodInMinutes: 24 * 60});
  }
};
ensureRetentionAlarm().catch((error) => {
  console.error("[fact-checker] scheduling the history clean-up failed:", error);
});
/** Delete history older than the viewer's retention setting. */
const runRetentionSweep = async () => {
  const settings = await loadSettings();
  const removed = await recorder.applyRetention(settings.historyRetentionDays);
  if (removed > 0) {
    console.info(`[fact-checker] history retention removed ${removed} session(s)`);
  }
};

/** The warm period is over: release the parked model, unless a session is using it. */
const closeWarmOffscreenDocument = async () => {
  if ((await getCapturedTabId()) === null) {
    console.info("[fact-checker] speech model idle for the warm period; releasing it");
    await closeOffscreenDocument();
  }
};

chrome.alarms?.onAlarm.addListener((alarm) => {
  const task =
    alarm.name === RETENTION_ALARM
      ? runRetentionSweep()
      : alarm.name === WARM_SPEECH_ALARM
        ? closeWarmOffscreenDocument()
        : null;
  task?.catch((error) => {
    console.error(`[fact-checker] alarm ${alarm.name} failed:`, error);
  });
});

chrome.tabs.onRemoved.addListener((tabId) => {
  handleTabRemoved(tabId).catch((error) => {
    console.error("[fact-checker] tabs.onRemoved handling failed:", error);
  });
});

/**
 * Relay live sensitivity, topic-filter and live-caption changes from the
 * app's Settings to the offscreen document (offscreen docs cannot observe
 * chrome.storage.onChanged), which applies them or forwards them as a WS
 * config frame. Topics travel as the enabled-slug array (the
 * OFFSCREEN_CONFIG payload shape: {sensitivity?, topics?, sendTranscripts?}).
 * No offscreen document — i.e. no active session — is the common case for
 * options changes, not an error.
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
  if (Boolean(newSettings.showTranscript) !== Boolean(oldSettings.showTranscript)) {
    configPatch.sendTranscripts = Boolean(newSettings.showTranscript);
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

/**
 * A provider key was removed in the app. If a running session is using that
 * provider, end it now: "remove key" has to be an off-switch that works.
 */
/** @returns {Promise<boolean>} whether it stopped the session */
const stopSessionIfKeyRemoved = async (change) => {
  const before = normalizeKeys(change.oldValue);
  const after = normalizeKeys(change.newValue);
  const removed = Object.keys(before).filter((providerId) => !after[providerId]);
  if (removed.length === 0 || (await getCapturedTabId()) === null) {
    return false;
  }
  const stored = await chrome.storage.session.get(SESSION_PROVIDERS_KEY);
  const inUse = stored[SESSION_PROVIDERS_KEY] ?? [];
  if (!removed.some((providerId) => inUse.includes(providerId))) {
    return false;
  }
  console.info("[fact-checker] a provider key in use was removed; stopping the session");
  // The offscreen document records the reason when it writes the final state.
  await handleCaptureStop("key_removed");
  return true;
};

/** @returns {Promise<boolean>} whether a browser-engine session is running now */
const browserEngineSessionRunning = async () => {
  const stored = await chrome.storage.session.get(SESSION_PROVIDERS_KEY);
  return (
    (stored[SESSION_PROVIDERS_KEY] ?? []).length > 0 &&
    (await getCapturedTabId()) !== null &&
    (await chrome.offscreen.hasDocument())
  );
};

/**
 * Spending limits, a step's provider or model, or an API key changed in the
 * app. A running browser-engine session applies them at once (offscreen
 * documents cannot watch storage): limits through OFFSCREEN_BUDGETS, the
 * recomputed plan through OFFSCREEN_ENGINE, so the next provider call uses
 * the new provider, model and key. Switching between this browser and the
 * local backend applies at the next Start; a removed key in use is handled
 * by stopSessionIfKeyRemoved, which ends the session instead.
 */
const relayEngineChange = async () => {
  if (!(await browserEngineSessionRunning())) {
    return;
  }
  const engineState = await loadEngineState();
  const send = (type, payload) =>
    chrome.runtime.sendMessage(makeMsg(TARGET.OFFSCREEN, type, payload)).catch((error) => {
      // The document can close between the check and delivery.
      console.debug(`[fact-checker] ${type} not delivered:`, error);
    });
  await send(MSG.OFFSCREEN_BUDGETS, {budgets: engineState.budgets});
  if (engineState.mode !== ENGINE_MODE.BROWSER) {
    return;
  }
  const plan = planBrowserEngine(engineState, await loadProviderKeys());
  if (!plan.ready) {
    return;
  }
  await chrome.storage.session.set({
    [SESSION_PROVIDERS_KEY]: [plan.gate.providerId, plan.verify.providerId],
  });
  await send(MSG.OFFSCREEN_ENGINE, {gate: plan.gate, verify: plan.verify});
};

// Content scripts run inside web pages: keep them out of storage.local,
// where the provider keys are.
protectStorage();

chrome.storage.onChanged.addListener((changes, areaName) => {
  if (areaName === "local" && changes[ENGINE_STORAGE_KEY]) {
    relayEngineChange().catch((error) => {
      console.error("[fact-checker] engine settings relay failed:", error);
    });
  }
  if (areaName === "local" && changes[KEYS_STORAGE_KEY]) {
    stopSessionIfKeyRemoved(changes[KEYS_STORAGE_KEY])
      .then((stopped) => (stopped ? null : relayEngineChange()))
      .catch((error) => {
        console.error("[fact-checker] key change handling failed:", error);
      });
    return;
  }
  if (areaName !== "sync" || !changes.settings) {
    return;
  }
  relaySettingsChange(changes).catch((error) => {
    console.error("[fact-checker] settings relay failed:", error);
  });
  const retentionBefore = changes.settings.oldValue?.historyRetentionDays;
  const retentionAfter = changes.settings.newValue?.historyRetentionDays;
  if (retentionAfter !== retentionBefore) {
    // Applied now, not at the next daily alarm: History shows the result.
    runRetentionSweep().catch((error) => {
      console.error("[fact-checker] history retention sweep failed:", error);
    });
  }
});
