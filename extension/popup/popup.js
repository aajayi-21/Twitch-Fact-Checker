/**
 * Toolbar popup — the user gesture that starts capture, plus live status.
 *
 * - Preflights GET /healthz (1.5 s AbortSignal.timeout) against the http(s)
 *   origin derived from the configured ws:// backendUrl; while offline the
 *   Start button is disabled.
 * - Status is never messaged: it is read from
 *   chrome.storage.session["captureSession"] and kept live via
 *   chrome.storage.onChanged (persisted by the service worker on every
 *   transition, relayed from the offscreen document via SESSION_UPDATE).
 */

import {ERR, MSG, TARGET, makeMsg} from "../shared/messages.js";
import {
  TOPIC_SLUGS,
  getEnabledTopicSlugs,
  loadSettings,
} from "../shared/settings.js";

const CAPTURE_SESSION_KEY = "captureSession";
const HEALTH_TIMEOUT_MS = 1500;
const HEALTH_POLL_INTERVAL_MS = 3000;
const ACTIVE_STATUSES = new Set(["starting", "capturing", "reconnecting"]);

const IDLE_SESSION = Object.freeze({
  status: "idle",
  tabId: null,
  startedAt: null,
  wsState: "closed",
  reconnectAttempt: 0,
  lastError: null,
});

/** One line of copy per error code, each with an action hint. */
const ERROR_COPY = {
  [ERR.ERR_BACKEND_DOWN]:
    "Backend unreachable for 5 minutes — capture stopped. Restart it, then click Start.",
  [ERR.ERR_STREAM_ID_EXPIRED]: "The capture handshake expired. Click Start again.",
  [ERR.ERR_CAPTURE_FAILED]:
    "Could not capture this tab's audio. Reload the tab and click Start again.",
  [ERR.ERR_CAPTURE_LOST]:
    "Tab audio capture ended unexpectedly. Click Start to resume.",
  [ERR.ERR_TAB_CLOSED]: "The captured tab was closed. Open a stream and click Start.",
  [ERR.ERR_NOT_CONFIGURED]: "Backend has no API key — add one in settings.",
  [ERR.ERR_CREDENTIALS_UPDATED]:
    "Session restarted after key update — press Start again.",
};

const STATUS_LABEL = {
  idle: "Idle — not capturing",
  starting: "Starting capture…",
  capturing: "Live — fact-checking this stream",
  reconnecting: "Reconnecting to backend…",
  stopping: "Stopping…",
  error: "Stopped",
};

const elements = {
  backendLine: document.getElementById("backend-line"),
  statusDot: document.getElementById("status-dot"),
  statusText: document.getElementById("status-text"),
  errorLine: document.getElementById("error-line"),
  hintLine: document.getElementById("hint-line"),
  offlineHint: document.getElementById("offline-hint"),
  startButton: document.getElementById("start-button"),
  stopButton: document.getElementById("stop-button"),
  topicsSummary: document.getElementById("topics-summary"),
  topicsEdit: document.getElementById("topics-edit"),
  setupCard: document.getElementById("setup-card"),
  openSettingsButton: document.getElementById("open-settings-button"),
  costLine: document.getElementById("cost-line"),
};

const state = {
  backendOnline: false,
  backendChecked: false,
  // healthz "configured" flag: false while the backend has no API key yet.
  // Defaults to true so the setup card never flashes before the first check.
  backendConfigured: true,
  session: {...IDLE_SESSION},
  activeTab: null,
  healthUrl: null,
  // Today's verification-attempt count + estimated cost from healthz.
  // null = field absent (older backend) -> the cost line stays hidden.
  checksToday: null,
  estCostTodayUsd: null,
};

/**
 * Derive the healthz URL from the ws:// backendUrl setting:
 * ws://host:port/ws/audio -> http://host:port/healthz (wss -> https).
 *
 * @param {string} backendUrl
 * @returns {string|null}
 */
const deriveHealthUrl = (backendUrl) => {
  try {
    const wsUrl = new URL(backendUrl);
    const httpProtocol = wsUrl.protocol === "wss:" ? "https:" : "http:";
    return `${httpProtocol}//${wsUrl.host}/healthz`;
  } catch (error) {
    console.error(`[fact-checker] invalid backendUrl "${backendUrl}":`, error);
    return null;
  }
};

const isCapturableTab = () => {
  const url = state.activeTab?.url;
  if (!url) {
    return true; // URL unreadable — let the start sequence surface any error
  }
  return url.startsWith("http://") || url.startsWith("https://");
};

const render = () => {
  const {session, backendOnline, backendChecked} = state;
  const active = ACTIVE_STATUSES.has(session.status);
  const capturingElsewhere =
    active &&
    state.activeTab?.id != null &&
    session.tabId !== null &&
    session.tabId !== state.activeTab.id;

  // Backend line.
  if (!backendChecked) {
    elements.backendLine.textContent = "Checking backend…";
    elements.backendLine.dataset.state = "checking";
  } else if (backendOnline) {
    elements.backendLine.textContent = "Backend online";
    elements.backendLine.dataset.state = "online";
  } else {
    elements.backendLine.textContent = "Backend offline";
    elements.backendLine.dataset.state = "offline";
  }

  // Status dot + text.
  elements.statusDot.dataset.state = session.status;
  let statusText = STATUS_LABEL[session.status] ?? session.status;
  if (session.status === "reconnecting" && session.reconnectAttempt > 0) {
    statusText = `Reconnecting to backend… (attempt ${session.reconnectAttempt})`;
  }
  if (capturingElsewhere) {
    statusText = "Capturing in another tab";
  }
  elements.statusText.textContent = statusText;

  // One-line error copy (only when the session is not running).
  const errorCopy =
    !active && session.lastError
      ? ERROR_COPY[session.lastError] ?? `Stopped: ${session.lastError}`
      : null;
  elements.errorLine.hidden = !errorCopy;
  if (errorCopy) {
    elements.errorLine.textContent = errorCopy;
  }

  // Today's usage readout (feature-detected: hidden on older backends).
  const showCost = backendOnline && state.checksToday !== null;
  elements.costLine.hidden = !showCost;
  if (showCost) {
    const costPart = Number.isFinite(state.estCostTodayUsd)
      ? ` · ~$${state.estCostTodayUsd.toFixed(2)}`
      : "";
    elements.costLine.textContent =
      `Checks today: ${state.checksToday}${costPart}`;
  }

  // Offline hint (with the port the popup actually polls) while stopped and
  // unreachable.
  elements.offlineHint.hidden = !(backendChecked && !backendOnline && !active);

  // Setup state: backend reachable but no API key configured yet. Shown
  // above the start controls; Start stays disabled until a key is added.
  const needsSetup =
    backendChecked && backendOnline && !state.backendConfigured;
  elements.setupCard.hidden = !(needsSetup && !active);

  // Buttons.
  elements.stopButton.hidden = !active;
  elements.startButton.hidden = active;
  const capturable = isCapturableTab();
  elements.startButton.disabled = !backendOnline || !capturable || needsSetup;
  if (!active && backendOnline && !capturable) {
    elements.hintLine.hidden = false;
    elements.hintLine.textContent =
      "This page can't be captured — open a stream on Twitch, YouTube, Kick, " +
      "or Rumble (or any audible tab).";
  } else {
    elements.hintLine.hidden = true;
  }
};

const checkBackendHealth = async () => {
  if (!state.healthUrl) {
    state.backendOnline = false;
    state.backendChecked = true;
    render();
    return;
  }
  try {
    const response = await fetch(state.healthUrl, {
      signal: AbortSignal.timeout(HEALTH_TIMEOUT_MS),
      cache: "no-store",
    });
    state.backendOnline = response.ok;
    if (response.ok) {
      try {
        const health = await response.json();
        // Missing field (older backend) counts as configured.
        state.backendConfigured = health?.configured !== false;
        state.checksToday = Number.isFinite(health?.checks_today)
          ? health.checks_today
          : null;
        state.estCostTodayUsd = Number.isFinite(health?.est_cost_today_usd)
          ? health.est_cost_today_usd
          : null;
      } catch (parseError) {
        console.warn("[fact-checker] unparseable healthz body:", parseError);
        state.backendConfigured = true;
      }
    }
  } catch (error) {
    state.backendOnline = false;
  }
  state.backendChecked = true;
  render();
};

const handleStartClick = async () => {
  try {
    const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
    if (!tab?.id) {
      console.error("[fact-checker] no active tab to capture");
      return;
    }
    state.activeTab = tab;
    elements.startButton.disabled = true; // storage.onChanged re-renders shortly
    await chrome.runtime.sendMessage(
      makeMsg(TARGET.BACKGROUND, MSG.CAPTURE_START, {tabId: tab.id})
    );
  } catch (error) {
    console.error("[fact-checker] CAPTURE_START failed:", error);
    render();
  }
};

/** Read-only "Topics: N of 9 on" summary; editing lives on the options page. */
const renderTopicsSummary = (settings) => {
  const enabledCount = getEnabledTopicSlugs(settings.topics).length;
  elements.topicsSummary.textContent =
    `Topics: ${enabledCount} of ${TOPIC_SLUGS.length} on`;
};

/** Shared by the topics "Edit" link and the setup card's "Open settings". */
const handleOpenOptionsClick = async () => {
  try {
    await chrome.runtime.openOptionsPage();
  } catch (error) {
    console.error("[fact-checker] opening options page failed:", error);
  }
};

const handleStopClick = async () => {
  try {
    elements.stopButton.disabled = true;
    await chrome.runtime.sendMessage(
      makeMsg(TARGET.BACKGROUND, MSG.CAPTURE_STOP, {})
    );
  } catch (error) {
    console.error("[fact-checker] CAPTURE_STOP failed:", error);
  } finally {
    elements.stopButton.disabled = false;
  }
};

chrome.storage.onChanged.addListener((changes, areaName) => {
  if (areaName !== "session" || !changes[CAPTURE_SESSION_KEY]) {
    return;
  }
  state.session = changes[CAPTURE_SESSION_KEY].newValue ?? {...IDLE_SESSION};
  render();
});

elements.startButton.addEventListener("click", handleStartClick);
elements.stopButton.addEventListener("click", handleStopClick);
elements.topicsEdit.addEventListener("click", () => {
  handleOpenOptionsClick().catch((error) => {
    console.error("[fact-checker] edit-topics click failed:", error);
  });
});
elements.openSettingsButton.addEventListener("click", () => {
  handleOpenOptionsClick().catch((error) => {
    console.error("[fact-checker] open-settings click failed:", error);
  });
});

const init = async () => {
  const settings = await loadSettings();
  state.healthUrl = deriveHealthUrl(settings.backendUrl);
  renderTopicsSummary(settings);
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  state.activeTab = tab ?? null;
  const stored = await chrome.storage.session.get(CAPTURE_SESSION_KEY);
  if (stored[CAPTURE_SESSION_KEY]) {
    state.session = stored[CAPTURE_SESSION_KEY];
  }
  render();
  await checkBackendHealth();
  // Keep polling while the popup is open so Start enables the moment the
  // backend comes up; the interval dies with the popup document.
  setInterval(checkBackendHealth, HEALTH_POLL_INTERVAL_MS);
};

init().catch((error) => {
  console.error("[fact-checker] popup init failed:", error);
  elements.statusText.textContent = "Popup failed to initialize";
  elements.statusDot.dataset.state = "error";
});
