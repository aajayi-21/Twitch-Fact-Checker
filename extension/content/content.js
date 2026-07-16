/**
 * Content script for twitch.tv pages — mounts the FactCheckOverlay inside
 * the Twitch player and routes messages between the service worker and the
 * overlay.
 *
 * CLASSIC (non-module) script, loaded AFTER content/overlay.js via manifest
 * order, so the global FactCheckOverlay class is already defined. By the
 * shared convention, message `type` values equal their MSG.* constant names
 * ("OVERLAY_EVENT", "SESSION_STATE", "CONTENT_READY"), so they are compared
 * as string literals here — no imports needed.
 *
 * Responsibilities:
 *  - mount the shadow host inside '[data-a-target="video-player"]' (falls
 *    back to document.body) so the overlay survives fullscreen;
 *  - re-attach across Twitch SPA navigations (debounced MutationObserver)
 *    and resync session state on URL changes;
 *  - CONTENT_READY handshake with the service worker on mount;
 *  - handle OVERLAY_EVENT (verbatim backend frames: verdict / status /
 *    error / transcript / ready) and SESSION_STATE;
 *  - own the in-memory session verdict history (cleared on page reload —
 *    accepted and documented in the plan).
 */

(() => {
  "use strict";

  const PLAYER_SELECTOR = '[data-a-target="video-player"]';
  const HISTORY_LIMIT = 100;
  const REMOUNT_DEBOUNCE_MS = 500;
  const URL_POLL_INTERVAL_MS = 1000;

  // Mirrors shared/settings.js DEFAULT_SETTINGS (classic scripts cannot
  // import modules; the "settings" storage key is the shared contract).
  const DEFAULT_SETTINGS = Object.freeze({
    backendUrl: "ws://127.0.0.1:8710/ws/audio",
    sensitivity: "medium",
    popupPosition: "top-right",
    popupDurationS: 12,
    showTranscript: false,
  });

  const state = {
    overlay: null,
    settings: {...DEFAULT_SETTINGS},
    history: [],
    running: false,
    lastUrl: location.href,
    remountScheduled: false,
  };

  const loadStoredSettings = async () => {
    try {
      const stored = await chrome.storage.sync.get("settings");
      return {...DEFAULT_SETTINGS, ...(stored.settings ?? {})};
    } catch (error) {
      console.error(
        "[fact-checker] failed to load settings; using defaults:",
        error
      );
      return {...DEFAULT_SETTINGS};
    }
  };

  const findMountTarget = () =>
    document.querySelector(PLAYER_SELECTOR) ?? document.body;

  /**
   * (Re-)attach the shadow host. Twitch SPA navigations replace the player
   * element, orphaning the host; mount() moves the existing host (with all
   * live UI state) under the new target.
   */
  const ensureMounted = () => {
    const target = findMountTarget();
    const host = state.overlay.hostElement;
    if (host && host.isConnected && host.parentElement === target) {
      return;
    }
    try {
      state.overlay.mount(target);
    } catch (error) {
      console.error("[fact-checker] overlay mount failed:", error);
    }
  };

  const scheduleRemountCheck = () => {
    if (state.remountScheduled) {
      return;
    }
    state.remountScheduled = true;
    setTimeout(() => {
      state.remountScheduled = false;
      ensureMounted();
    }, REMOUNT_DEBOUNCE_MS);
  };

  const setRunning = (running) => {
    state.running = Boolean(running);
    state.overlay.setConnectionState(state.running);
  };

  /**
   * CONTENT_READY handshake: the service worker replies (via sendResponse)
   * with {type: "SESSION_STATE", payload: {running}} for THIS tab. Called on
   * mount and again after SPA navigation to resync the status dot.
   */
  const announceReady = async () => {
    try {
      const reply = await chrome.runtime.sendMessage({
        target: "background",
        type: "CONTENT_READY",
        payload: {},
      });
      if (reply?.type === "SESSION_STATE") {
        setRunning(reply.payload?.running);
      }
    } catch (error) {
      // Expected right after an extension reload; non-fatal.
      console.debug("[fact-checker] CONTENT_READY handshake failed:", error);
    }
  };

  const recordVerdict = (verdictFrame) => {
    state.history.push(verdictFrame);
    if (state.history.length > HISTORY_LIMIT) {
      state.history.splice(0, state.history.length - HISTORY_LIMIT);
    }
    state.overlay.addVerdict(verdictFrame);
    state.overlay.renderHistory(state.history);
  };

  /**
   * Backend WebSocket frames arrive verbatim (offscreen → service worker →
   * here, §2.1 of the plan). Unknown frame types are ignored by design so
   * future backend versions stay compatible.
   */
  const handleBackendEvent = (event) => {
    if (!event || typeof event.type !== "string") {
      return;
    }
    switch (event.type) {
      case "verdict":
        recordVerdict(event);
        break;
      case "status":
        if (event.stage === "verifying") {
          state.overlay.showChecking(event.claim);
        }
        break;
      case "error":
        state.overlay.showBackendNotice(event);
        break;
      case "transcript":
        if (state.settings.showTranscript) {
          state.overlay.showTranscript(event);
        }
        break;
      case "ready":
        setRunning(true);
        break;
      default:
        break;
    }
  };

  const listenForMessages = () => {
    chrome.runtime.onMessage.addListener((message) => {
      if (!message || typeof message.type !== "string") {
        return false;
      }
      if (message.type === "OVERLAY_EVENT") {
        handleBackendEvent(message.payload?.event);
      } else if (message.type === "SESSION_STATE") {
        setRunning(message.payload?.running);
      }
      return false;
    });
  };

  const listenForSettingsChanges = () => {
    chrome.storage.onChanged.addListener((changes, areaName) => {
      if (areaName !== "sync" || !changes.settings) {
        return;
      }
      state.settings = {
        ...DEFAULT_SETTINGS,
        ...(changes.settings.newValue ?? {}),
      };
      state.overlay.applySettings(state.settings);
    });
  };

  /**
   * Twitch is an SPA: navigation replaces player DOM without a page load.
   * A debounced MutationObserver re-attaches the host when it is orphaned,
   * and a URL poller resyncs session state on channel changes.
   */
  const watchForNavigation = () => {
    const observer = new MutationObserver(scheduleRemountCheck);
    observer.observe(document.documentElement, {
      childList: true,
      subtree: true,
    });
    setInterval(() => {
      if (location.href === state.lastUrl) {
        return;
      }
      state.lastUrl = location.href;
      scheduleRemountCheck();
      announceReady();
    }, URL_POLL_INTERVAL_MS);
  };

  const init = async () => {
    state.settings = await loadStoredSettings();
    state.overlay = new FactCheckOverlay(state.settings);
    ensureMounted();
    listenForMessages();
    listenForSettingsChanges();
    watchForNavigation();
    await announceReady();
  };

  init().catch((error) => {
    console.error("[fact-checker] content script init failed:", error);
  });
})();
