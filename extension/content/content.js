/**
 * Content script for supported stream pages (Twitch, YouTube, Kick, Rumble)
 * — mounts the FactCheckOverlay inside the host page's player and routes
 * messages between the service worker and the overlay.
 *
 * CLASSIC (non-module) script, loaded AFTER content/overlay.js via manifest
 * order, so the global FactCheckOverlay class is already defined. By the
 * shared convention, message `type` values equal their MSG.* constant names
 * ("OVERLAY_EVENT", "SESSION_STATE", "CONTENT_READY"), so they are compared
 * as string literals here — no imports needed.
 *
 * Responsibilities:
 *  - mount the shadow host inside the platform's player element (per-host
 *    selector chain in PLATFORM_ADAPTERS, falling back to document.body) so
 *    the overlay survives fullscreen — on player pages (each adapter's
 *    isPlayerPage predicate) and, while a capture session is RUNNING, on any
 *    page (chrome.tabCapture is tab-scoped and survives SPA navigation, so
 *    the UI must too); when idle on non-player pages the script stays
 *    dormant and the URL poller re-checks after SPA navigation;
 *  - follow fullscreen: if the fullscreened element does not contain the
 *    shadow host, re-parent the host into it; if a bare <video> element is
 *    fullscreened (no DOM overlay possible), suppress toasts while verdicts
 *    keep accumulating in the history panel;
 *  - re-attach across SPA navigations (debounced MutationObserver) and
 *    resync session state on URL changes;
 *  - CONTENT_READY handshake with the service worker on mount;
 *  - handle OVERLAY_EVENT (verbatim backend frames: verdict / status /
 *    error / transcript / ready) and SESSION_STATE;
 *  - own the in-memory session verdict history (cleared on page reload —
 *    accepted and documented in the plan), including user-initiated removal:
 *    the overlay's per-row "×" and header "Clear all" buttons call back into
 *    onRemoveVerdict / onClearHistory here, which mutate the array and
 *    re-render (the topic-skip footer counter is unaffected).
 */

(() => {
  "use strict";

  // Promise-safe API namespace (see shared/capabilities.js § namespace idiom).
  const chrome = globalThis.browser ?? globalThis.chrome;

  const HISTORY_LIMIT = 100;
  const REMOUNT_DEBOUNCE_MS = 500;
  const URL_POLL_INTERVAL_MS = 1000;

  // Kick paths that are single top-level segments but NOT channel pages.
  // VERIFIED routes only: on Kick any unreserved single segment is a real
  // channel (kick.com/privacy, /chat, /live and /main are channels), so
  // speculative entries would hide the overlay on real channel pages. The
  // list can never be exhaustive — the structural no-<video> guard on the
  // kick adapter (allowsBodyFallback) covers routes it misses.
  const KICK_NON_CHANNEL_SEGMENTS = new Set([
    "browse",
    "categories",
    "category",
    "search",
    "following",
    "transactions",
    "terms-of-service",
    "community-guidelines",
    "dashboard",
    "help",
    "support",
    "privacy-policy",
    "dmca-policy",
    "cookie-policy",
    "channel-points-terms-and-conditions",
    "kicks-usage-policy",
    "partner-terms-and-conditions",
  ]);

  /**
   * Kick's class names are obfuscated and churn; when none of the stable ids
   * match, walk up from a visible <video> to the nearest positioned ancestor
   * with player-like dimensions (the pattern current Kick extensions use).
   *
   * @returns {Element|null}
   */
  const findKickPlayerFromVideo = () => {
    for (const video of document.querySelectorAll("video")) {
      const videoRect = video.getBoundingClientRect();
      if (videoRect.width < 200 || videoRect.height < 100) {
        continue; // thumbnail/preview-sized video — not the main player
      }
      let ancestor = video.parentElement;
      while (ancestor && ancestor !== document.body) {
        const ancestorRect = ancestor.getBoundingClientRect();
        if (
          getComputedStyle(ancestor).position !== "static" &&
          ancestorRect.width >= videoRect.width * 0.9 &&
          ancestorRect.height >= videoRect.height * 0.9
        ) {
          return ancestor;
        }
        ancestor = ancestor.parentElement;
      }
    }
    return null;
  };

  /**
   * Per-host adapter table. `playerSelectors` is an ordered fallback chain
   * of mount targets; `isPlayerPage(pathname)` decides whether this URL has
   * a player at all (on non-player pages the overlay never mounts or
   * announces while idle — the URL poller re-evaluates after every SPA
   * navigation). Kick additionally gets `findFallbackTarget` (see above)
   * and `allowsBodyFallback` (structural no-player guard).
   */
  const PLATFORM_ADAPTERS = Object.freeze({
    "twitch.tv": {
      playerSelectors: ['[data-a-target="video-player"]'],
      // Any /<channel> path may host a player (Twitch even autoplays one on
      // the homepage) — mount everywhere, matching the original behavior.
      isPlayerPage: () => true,
    },
    "youtube.com": {
      playerSelectors: ["#movie_player", "ytd-player"],
      // /watch, /live/<id>, /@<channel>/live, and the channel-scoped live
      // permalinks /channel/<id>/live, /c/<name>/live, /user/<name>/live all
      // serve the watch page at their own URL (no redirect); /embed/* iframes
      // are out of scope.
      isPlayerPage: (pathname) => {
        if (pathname.startsWith("/embed/")) {
          return false;
        }
        return (
          pathname === "/watch" ||
          /^\/live\/[^/]+/.test(pathname) ||
          /^\/@[^/]+\/live\/?$/.test(pathname) ||
          /^\/(channel|c|user)\/[^/]+\/live\/?$/.test(pathname)
        );
      },
    },
    "kick.com": {
      playerSelectors: [
        "#video-player",
        "#injected-channel-player",
        "#video-holder",
      ],
      findFallbackTarget: findKickPlayerFromVideo,
      // Structural guard: KICK_NON_CHANNEL_SEGMENTS can never be exhaustive,
      // so when no selector matches, the video walk-up fails, AND the page
      // has not a single <video>, findMountTarget reports "no player" (null)
      // instead of falling back to document.body. Any <video> keeps the
      // body-fallback safety net for genuine channel pages whose obfuscated
      // DOM defeats the selectors.
      allowsBodyFallback: () => document.querySelector("video") !== null,
      // /<channel> (single segment, minus known non-channel routes) and
      // /<channel>/videos/<id> VOD pages.
      isPlayerPage: (pathname) => {
        const segments = pathname.split("/").filter(Boolean);
        if (segments.length === 1) {
          return !KICK_NON_CHANNEL_SEGMENTS.has(segments[0].toLowerCase());
        }
        return segments.length === 3 && segments[1] === "videos";
      },
    },
    "rumble.com": {
      playerSelectors: ["#videoPlayer", ".video-player"],
      // Watch pages (live streams included) are /v<id>-<slug>; /embed/*
      // iframes are out of scope.
      isPlayerPage: (pathname) => {
        if (pathname.startsWith("/embed/")) {
          return false;
        }
        return /^\/v[a-z0-9]+-/.test(pathname);
      },
    },
  });

  // Unknown host (should not happen given the manifest matches): mount to
  // document.body on every page, the platform-agnostic lenient behavior.
  const GENERIC_ADAPTER = Object.freeze({
    playerSelectors: [],
    isPlayerPage: () => true,
  });

  const adapter =
    PLATFORM_ADAPTERS[location.hostname.replace(/^www\./, "")] ??
    GENERIC_ADAPTER;

  // Mirrors shared/settings.js DEFAULT_SETTINGS (classic scripts cannot
  // import modules; the "settings" storage key is the shared contract).
  const DEFAULT_SETTINGS = Object.freeze({
    backendUrl: "ws://127.0.0.1:8710/ws/audio",
    sensitivity: "medium",
    popupPosition: "top-right",
    popupDurationS: 12,
    showTranscript: false,
    captureVideo: false,
    topics: Object.freeze({
      politics: true,
      health: true,
      science_tech: true,
      money: true,
      history: true,
      sports: true,
      gaming: true,
      entertainment: true,
      other: true,
    }),
  });

  const state = {
    overlay: null,
    settings: {...DEFAULT_SETTINGS},
    history: [],
    running: false,
    lastUrl: location.href,
    remountScheduled: false,
    // Claims dropped by the server-side topic filter, surfaced only as an
    // aggregate history-panel footer (never per-skip toasts). Page-lifetime,
    // like the verdict history.
    topicSkipCount: 0,
    // PageAudioCapture instance on the Firefox path only (Chrome captures
    // the tab from an offscreen document and leaves this null).
    capture: null,
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

  /**
   * @returns {Element|null} the mount target, or null when the adapter's
   *   structural guard says the page has no plausible player at all (Kick:
   *   no selector match, video walk-up failed, and not a single <video>).
   */
  const findMountTarget = () => {
    for (const selector of adapter.playerSelectors) {
      const candidate = document.querySelector(selector);
      if (candidate) {
        return candidate;
      }
    }
    const fallback = adapter.findFallbackTarget?.() ?? null;
    if (fallback) {
      return fallback;
    }
    if (adapter.allowsBodyFallback && !adapter.allowsBodyFallback()) {
      return null;
    }
    return document.body;
  };

  /** Detach the shadow host if it is currently in the document. */
  const detachOverlayHost = () => {
    const host = state.overlay.hostElement;
    if (host?.isConnected) {
      state.overlay.unmount();
    }
  };

  /**
   * (Re-)attach the shadow host. SPA navigations replace the player element,
   * orphaning the host; mount() moves the existing host (with all live UI
   * state) under the new target. On non-player pages the host is detached —
   * but ONLY while no capture session is running: capture is tab-scoped and
   * survives SPA navigation (YouTube miniplayer, Kick browsing), so a live
   * session keeps the overlay mounted (the player selectors still win —
   * YouTube reuses #movie_player inside the miniplayer — with document.body
   * as the fixed-position fallback). Fullscreen-aware: while an element (not
   * a bare <video>) is fullscreened, the host must live inside it or nothing
   * renders.
   */
  const ensureMounted = () => {
    if (!adapter.isPlayerPage(location.pathname) && !state.running) {
      detachOverlayHost();
      return;
    }
    const fullscreenElement = document.fullscreenElement;
    if (fullscreenElement && fullscreenElement.tagName !== "VIDEO") {
      const host = state.overlay.hostElement;
      if (host && fullscreenElement.contains(host)) {
        return;
      }
      try {
        state.overlay.mount(fullscreenElement);
      } catch (error) {
        console.error("[fact-checker] fullscreen re-parent failed:", error);
      }
      return;
    }
    let target = findMountTarget();
    if (!target) {
      // Structural guard hit (Kick): the URL looked like a channel page but
      // nothing player-like exists — treat it like a non-player page while
      // idle; a running session falls back to document.body as usual.
      if (!state.running) {
        detachOverlayHost();
        return;
      }
      target = document.body;
    }
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

  /**
   * Universal fullscreen rule: if the fullscreened element does not contain
   * the shadow host, re-parent the host into it; if the fullscreened node is
   * a bare <video> (no DOM overlay can render inside it), suppress toasts —
   * verdicts still accumulate in the history panel. Both re-parent and
   * suppression are undone on fullscreen exit.
   */
  const handleFullscreenChange = () => {
    const fullscreenElement = document.fullscreenElement;
    state.overlay.setToastsSuppressed(
      Boolean(fullscreenElement && fullscreenElement.tagName === "VIDEO")
    );
    ensureMounted();
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
        } else if (event.stage === "topic_skipped") {
          // One frame per claim dropped by the topic filter (no verdict
          // follows). Counted silently; the history panel footer shows the
          // aggregate.
          state.topicSkipCount += 1;
          state.overlay.setTopicSkipCount(state.topicSkipCount);
        }
        break;
      case "contradiction":
        // Same-session self-contradiction alert (backend enforces the
        // high-confidence threshold; the relay stays dumb).
        state.overlay.addContradiction(event);
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

  /* ------------------------------------------------------------------ */
  /* Firefox capture path (see shared/capabilities.js)                   */
  /* ------------------------------------------------------------------ */

  /**
   * Tap the page's own <video> and stream base64 PCM to the background
   * page, which owns the WebSocket. Only Firefox ever asks for this —
   * Chrome captures the tab through an offscreen document instead.
   */
  const startPageCapture = async ({captureVideo = false} = {}) => {
    if (state.capture) {
      state.capture.stop();
      state.capture = null;
    }
    const capture = new PageAudioCapture({
      onFrame: (pcmB64) => {
        chrome.runtime
          .sendMessage({
            target: "background",
            type: "AUDIO_CHUNK",
            payload: {pcmB64},
          })
          .catch((error) => {
            console.debug("[fact-checker] AUDIO_CHUNK send failed:", error);
          });
      },
      onVideoFrame: ({imageB64, capturedAtMs}) => {
        chrome.runtime
          .sendMessage({
            target: "background",
            type: "VIDEO_FRAME",
            payload: {imageB64, capturedAtMs},
          })
          .catch((error) => {
            console.debug("[fact-checker] VIDEO_FRAME send failed:", error);
          });
      },
    });
    await capture.start({captureVideo});
    state.capture = capture;
  };

  const stopPageCapture = () => {
    if (!state.capture) {
      return;
    }
    state.capture.stop();
    state.capture = null;
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
      } else if (message.type === "CONTENT_CAPTURE_START") {
        startPageCapture(message.payload ?? {}).catch((error) => {
          console.error("[fact-checker] page capture failed to start:", error);
          chrome.runtime
            .sendMessage({
              target: "background",
              type: "CONTENT_CAPTURE_FAILED",
              payload: {reason: String(error?.message ?? error)},
            })
            .catch(() => {});
        });
      } else if (message.type === "CONTENT_CAPTURE_STOP") {
        stopPageCapture();
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
   * Twitch, YouTube and Kick are SPAs: navigation replaces player DOM
   * without a page load (Rumble is an MPA, where this is simply idle). A
   * debounced MutationObserver re-attaches the host when it is orphaned,
   * and a platform-agnostic URL poller resyncs session state on channel /
   * video changes. Non-player URLs never mount or announce.
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
      if (adapter.isPlayerPage(location.pathname)) {
        announceReady();
      }
    }, URL_POLL_INTERVAL_MS);
  };

  const init = async () => {
    state.settings = await loadStoredSettings();
    state.overlay = new FactCheckOverlay(state.settings);
    // "Edit topics" in the history-panel footer: content scripts cannot call
    // chrome.runtime.openOptionsPage(), so ask the service worker.
    state.overlay.onEditTopics = () => {
      chrome.runtime
        .sendMessage({target: "background", type: "OPEN_OPTIONS", payload: {}})
        .catch((error) => {
          console.debug("[fact-checker] OPEN_OPTIONS send failed:", error);
        });
    };
    // "×" on a history row: this script owns the history array, so the
    // overlay only reports the verdict id; re-rendering from the mutated
    // array removes the row and updates the pill count in one pass.
    state.overlay.onRemoveVerdict = (verdictId) => {
      const index = state.history.findIndex(
        (verdict) => verdict.id === verdictId
      );
      if (index === -1) {
        return;
      }
      state.history.splice(index, 1);
      state.overlay.renderHistory(state.history);
    };
    // "Clear all" in the panel header: empty the session history; the
    // overlay shows "Fact-check · 0" and its existing empty state. The
    // topic-skip counter is page-lifetime and intentionally untouched.
    state.overlay.onClearHistory = () => {
      state.history.length = 0;
      state.overlay.renderHistory(state.history);
    };
    // 👍/👎 on a verdict: fire-and-forget relay to the service worker,
    // which POSTs the backend's /feedback endpoint (content scripts never
    // talk HTTP themselves).
    state.overlay.onVerdictFeedback = (verdictId, rating) => {
      chrome.runtime
        .sendMessage({
          target: "background",
          type: "SUBMIT_FEEDBACK",
          payload: {verdictId, rating},
        })
        .catch((error) => {
          console.debug("[fact-checker] SUBMIT_FEEDBACK send failed:", error);
        });
    };
    ensureMounted(); // no-ops quietly on non-player pages
    document.addEventListener("fullscreenchange", handleFullscreenChange);
    // Firefox path only: a full navigation destroys this script (and with it
    // the audio tap), so end the session rather than leave the backend
    // socket open with nothing feeding it. SPA route changes do NOT fire
    // pagehide — those are handled by PageAudioCapture's re-attach check.
    window.addEventListener("pagehide", () => {
      if (!state.capture) {
        return;
      }
      stopPageCapture();
      chrome.runtime
        .sendMessage({
          target: "background",
          type: "CAPTURE_STOP",
          payload: {reason: "navigated"},
        })
        .catch(() => {});
    });
    listenForMessages();
    listenForSettingsChanges();
    watchForNavigation();
    if (adapter.isPlayerPage(location.pathname)) {
      await announceReady();
    }
  };

  init().catch((error) => {
    console.error("[fact-checker] content script init failed:", error);
  });
})();
