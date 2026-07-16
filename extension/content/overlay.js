/**
 * FactCheckOverlay — the on-stream UI for the Live Stream Fact-Checker.
 *
 * CLASSIC (non-module) content script, loaded BEFORE content/content.js via
 * manifest order; it defines the global FactCheckOverlay class that
 * content.js constructs and drives. All UI lives inside an open Shadow DOM
 * so host-page CSS (Twitch, YouTube, Kick, Rumble) can never restyle it
 * (and our CSS never leaks out). Styles are fetched from content/overlay.css
 * (a web-accessible resource) and injected as a <style> element inside the
 * shadow root.
 *
 * Components:
 *  - Verdict toasts: max 2 stacked (a third evicts the oldest — stale
 *    verdicts are noise on a live stream), auto-dismiss after
 *    settings.popupDurationS, hover pauses the countdown.
 *  - "Checking claim…" chip: shown on backend status frames with stage
 *    "verifying"; hidden when a verdict lands or after a safety timeout.
 *  - History panel: corner pill "Fact-check · N" with a connection status
 *    dot, toggling a scrollable list of this session's verdicts (entries
 *    expand on click to show explanation + sources). Each verdict carries a
 *    topic color dot; a footer aggregates claims skipped by the topic filter
 *    with an "Edit topics" link (opens the options page via content.js).
 *  - Transient extras: non-fatal backend notice chip, optional live
 *    transcript line.
 *
 * All dynamic text is set via textContent (never innerHTML) and source URLs
 * are validated as http(s) before an anchor is created.
 */

// Mirrors shared/settings.js TOPIC_COLORS / TOPIC_LABELS (classic scripts
// cannot import modules; the topic slugs are the shared wire contract) —
// keep in sync. Unknown or missing slugs render with the "other" entry.
const TOPIC_COLORS = Object.freeze({
  politics: "#e74c3c",
  health: "#2ecc71",
  science_tech: "#3498db",
  money: "#f1c40f",
  history: "#9b59b6",
  sports: "#e67e22",
  gaming: "#1abc9c",
  entertainment: "#e84393",
  other: "#95a5a6",
});

const TOPIC_LABELS = Object.freeze({
  politics: "Politics & current events",
  health: "Health & medicine",
  science_tech: "Science & technology",
  money: "Money & economy",
  history: "History",
  sports: "Sports",
  gaming: "Gaming",
  entertainment: "Entertainment & pop culture",
  other: "Everything else",
});

class FactCheckOverlay {
  static MAX_STACKED_TOASTS = 2;
  static TOAST_SOURCE_LIMIT = 3;
  static PANEL_SOURCE_LIMIT = 5;
  static CHECKING_SAFETY_TIMEOUT_MS = 20000;
  static NOTICE_DURATION_MS = 6000;
  static FATAL_NOTICE_DURATION_MS = 10000;
  static TRANSCRIPT_DURATION_MS = 5000;
  static TOAST_EXIT_MS = 220;
  static MIN_RESUME_MS = 800;
  static DEFAULT_DURATION_MS = 12000;
  static VALID_LABELS = new Set(["TRUE", "FALSE", "MISLEADING", "UNVERIFIED"]);
  static VALID_POSITIONS = new Set([
    "top-left",
    "top-right",
    "bottom-left",
    "bottom-right",
  ]);
  static NOTICE_COPY = Object.freeze({
    quota_cooldown: "Fact-checks paused: API quota is cooling down",
    rate_limited: "Fact-checks throttled: rate limit reached",
    stt_overload: "Transcription is falling behind — some audio was dropped",
    llm_failure: "A fact-check failed — that claim was skipped",
  });

  #settings;
  #host = null;
  #shadow = null;
  #root = null;
  #refs = null;
  #activeToasts = [];
  #historyEntries = [];
  #expandedEntryKeys = new Set();
  #panelOpen = false;
  #running = false;
  #chipTimerId = null;
  #noticeTimerId = null;
  #transcriptTimerId = null;
  #topicSkipCount = 0;
  #toastsSuppressed = false;

  /**
   * Called when the user clicks "Edit topics" in the history-panel footer.
   * Assigned by content.js (which relays OPEN_OPTIONS to the service worker
   * — content scripts cannot open the options page themselves).
   * @type {(() => void)|null}
   */
  onEditTopics = null;

  /**
   * @param {object} settings - shape of shared/settings.js DEFAULT_SETTINGS;
   *   only popupPosition, popupDurationS and showTranscript are used here.
   */
  constructor(settings = {}) {
    this.#settings = {...settings};
  }

  /** The shadow host element (null before the first mount). */
  get hostElement() {
    return this.#host;
  }

  /**
   * Create the shadow host on first call and attach it under parentElement.
   * Safe to call again after an SPA navigation replaces the player (or a
   * fullscreen re-parent): the existing host — with live toasts, panel state
   * and history — is simply re-appended (appendChild moves the node).
   * Positioning is `absolute` inside the player element and `fixed` on the
   * document.body fallback. If the player container is not itself positioned
   * (computed position "static", possible on non-Twitch platforms), it is
   * guardedly promoted to position:relative so the absolute host anchors to
   * it; if that promotion does not stick (e.g. an !important stylesheet
   * rule), the host falls back to viewport-fixed positioning.
   *
   * @param {Element} parentElement
   */
  mount(parentElement) {
    if (!(parentElement instanceof Element)) {
      throw new Error("FactCheckOverlay.mount: parentElement must be an Element");
    }
    if (!this.#host) {
      this.#host = document.createElement("div");
      this.#host.id = "stream-fact-checker-overlay";
      this.#shadow = this.#host.attachShadow({mode: "open"});
      this.#injectStyles();
      this.#buildSkeleton();
    }
    const useFixedPositioning =
      parentElement === document.body ||
      !FactCheckOverlay.#ensurePositionedAncestor(parentElement);
    Object.assign(this.#host.style, {
      position: useFixedPositioning ? "fixed" : "absolute",
      inset: "0",
      zIndex: "2147483646",
      pointerEvents: "none",
    });
    if (this.#host.parentElement !== parentElement) {
      parentElement.appendChild(this.#host);
    }
  }

  /** Detach the host and cancel every pending timer. mount() re-attaches. */
  unmount() {
    this.hideChecking();
    this.#hideNotice();
    this.#hideTranscript();
    for (const toast of [...this.#activeToasts]) {
      this.#dismissToast(toast, {immediate: true});
    }
    if (this.#host) {
      this.#host.remove();
    }
  }

  /**
   * Merge new settings. Position and transcript visibility apply
   * immediately; the dismiss duration applies to toasts created from now on
   * (already-running countdowns keep their remaining time).
   *
   * @param {object} settings
   */
  applySettings(settings = {}) {
    this.#settings = {...this.#settings, ...settings};
    if (!this.#root) {
      return;
    }
    this.#root.dataset.position = this.#normalizedPosition();
    if (!this.#settings.showTranscript) {
      this.#hideTranscript();
    }
  }

  /**
   * Drive the connection status dot (pill + panel header). Turning off also
   * clears the transient "checking" chip and transcript line.
   *
   * @param {boolean} running
   */
  setConnectionState(running) {
    this.#running = Boolean(running);
    if (!this.#refs) {
      return;
    }
    const dotState = this.#running ? "on" : "off";
    this.#refs.pillDot.dataset.state = dotState;
    this.#refs.panelDot.dataset.state = dotState;
    this.#refs.panelStatusText.textContent = this.#running ? "Live" : "Idle";
    if (!this.#running) {
      this.hideChecking();
      this.#hideTranscript();
    }
  }

  /**
   * Show a verdict toast (server `verdict` frame, verbatim). Evicts the
   * oldest visible toast beyond MAX_STACKED_TOASTS and hides the
   * "checking" chip that announced this verification.
   *
   * @param {object} verdict - {id, claim, label, explanation, sources,
   *   checked_at, used_fallback}
   */
  addVerdict(verdict) {
    if (!this.#refs || !verdict) {
      return;
    }
    this.hideChecking();
    if (this.#toastsSuppressed) {
      return; // fullscreened bare <video>: history still records the verdict
    }
    while (this.#activeToasts.length >= FactCheckOverlay.MAX_STACKED_TOASTS) {
      this.#dismissToast(this.#activeToasts[0], {immediate: true});
    }
    const toast = this.#buildToast(verdict);
    this.#refs.toasts.prepend(toast.element);
    this.#activeToasts.push(toast);
    this.#startToastTimer(toast);
  }

  /**
   * Re-render the history pill count and panel list from the caller-owned
   * session history array (oldest first; rendered newest first).
   *
   * @param {object[]} entries - verdict frames
   */
  renderHistory(entries) {
    this.#historyEntries = Array.isArray(entries) ? [...entries] : [];
    if (!this.#refs) {
      return;
    }
    this.#refs.pillLabel.textContent = `Fact-check · ${this.#historyEntries.length}`;
    const list = this.#refs.panelList;
    list.textContent = "";
    if (this.#historyEntries.length === 0) {
      const empty = document.createElement("div");
      empty.className = "fc-empty";
      empty.textContent = "No claims checked yet.";
      list.appendChild(empty);
      return;
    }
    for (const verdict of [...this.#historyEntries].reverse()) {
      list.appendChild(this.#buildHistoryEntry(verdict));
    }
  }

  /**
   * Update the history-panel footer with the aggregate number of claims the
   * backend dropped via the topic filter (server `status` frames, stage
   * "topic_skipped" — counted by content.js). Hidden while the count is 0;
   * deliberately no per-skip toasts.
   *
   * @param {number} count
   */
  setTopicSkipCount(count) {
    const skipped =
      Number.isFinite(count) && count > 0 ? Math.floor(count) : 0;
    this.#topicSkipCount = skipped;
    if (!this.#refs) {
      return;
    }
    this.#refs.panelFooter.hidden = skipped === 0;
    const noun = skipped === 1 ? "claim" : "claims";
    this.#refs.skipText.textContent =
      `${skipped} ${noun} skipped by topic filters`;
  }

  /**
   * Suppress (or re-allow) verdict toasts. content.js enables this while a
   * bare <video> element is fullscreened — no DOM overlay can render over it
   * — so verdicts accumulate silently in the history panel instead of
   * bursting as stale toasts on fullscreen exit.
   *
   * @param {boolean} suppressed
   */
  setToastsSuppressed(suppressed) {
    this.#toastsSuppressed = Boolean(suppressed);
  }

  /**
   * Show the "Checking claim…" chip (server `status` frame, stage
   * "verifying"). Auto-hides after a safety timeout in case the matching
   * verdict or error never arrives.
   *
   * @param {string} [claim] - shown as the chip tooltip
   */
  showChecking(claim) {
    if (!this.#refs) {
      return;
    }
    this.#refs.chipText.textContent = "Checking claim…";
    this.#refs.chip.title = typeof claim === "string" ? claim : "";
    this.#refs.chip.hidden = false;
    if (this.#chipTimerId !== null) {
      clearTimeout(this.#chipTimerId);
    }
    this.#chipTimerId = setTimeout(
      () => this.hideChecking(),
      FactCheckOverlay.CHECKING_SAFETY_TIMEOUT_MS
    );
  }

  /** Hide the "Checking claim…" chip and cancel its safety timeout. */
  hideChecking() {
    if (this.#chipTimerId !== null) {
      clearTimeout(this.#chipTimerId);
      this.#chipTimerId = null;
    }
    if (this.#refs) {
      this.#refs.chip.hidden = true;
    }
  }

  /**
   * Show a transient notice for a backend `error` frame. Known non-fatal
   * codes get friendly copy; anything else falls back to the frame's own
   * message. Fatal frames are styled red and shown a little longer (the
   * status dot goes grey separately via SESSION_STATE).
   *
   * @param {object} errorFrame - {type: "error", code, message, fatal}
   */
  showBackendNotice(errorFrame = {}) {
    if (!this.#refs) {
      return;
    }
    const friendlyCopy = FactCheckOverlay.NOTICE_COPY[errorFrame.code];
    const fallbackCopy =
      errorFrame.message || errorFrame.code || "backend error";
    const fatal = Boolean(errorFrame.fatal);
    const text = fatal
      ? `Fact-checking stopped: ${friendlyCopy ?? fallbackCopy}`
      : friendlyCopy ?? `Fact-checker: ${fallbackCopy}`;
    const notice = this.#refs.notice;
    notice.textContent = text;
    if (fatal) {
      notice.dataset.severity = "fatal";
    } else {
      delete notice.dataset.severity;
    }
    notice.hidden = false;
    if (this.#noticeTimerId !== null) {
      clearTimeout(this.#noticeTimerId);
    }
    this.#noticeTimerId = setTimeout(
      () => this.#hideNotice(),
      fatal
        ? FactCheckOverlay.FATAL_NOTICE_DURATION_MS
        : FactCheckOverlay.NOTICE_DURATION_MS
    );
  }

  /**
   * Show/update the live transcript line (server `transcript` frame); it
   * fades out a few seconds after the last update. content.js only calls
   * this while settings.showTranscript is enabled.
   *
   * @param {object} frame - {type: "transcript", text, start, end}
   */
  showTranscript(frame = {}) {
    if (!this.#refs) {
      return;
    }
    const text = typeof frame.text === "string" ? frame.text.trim() : "";
    if (!text) {
      return;
    }
    this.#refs.transcript.textContent = text;
    this.#refs.transcript.hidden = false;
    if (this.#transcriptTimerId !== null) {
      clearTimeout(this.#transcriptTimerId);
    }
    this.#transcriptTimerId = setTimeout(
      () => this.#hideTranscript(),
      FactCheckOverlay.TRANSCRIPT_DURATION_MS
    );
  }

  /* ------------------------------------------------------------------ */
  /* Private: skeleton + styles                                         */
  /* ------------------------------------------------------------------ */

  /**
   * Fetch overlay.css from the extension package and inject it as a
   * <style> element in the shadow root. On failure the overlay keeps
   * working unstyled — better than no verdicts at all.
   */
  #injectStyles() {
    const styleElement = document.createElement("style");
    this.#shadow.appendChild(styleElement);
    const cssUrl = chrome.runtime.getURL("content/overlay.css");
    fetch(cssUrl)
      .then((response) => {
        if (!response.ok) {
          throw new Error(`unexpected HTTP ${response.status} for ${cssUrl}`);
        }
        return response.text();
      })
      .then((cssText) => {
        styleElement.textContent = cssText;
      })
      .catch((error) => {
        console.error(
          "[fact-checker] failed to load overlay.css — overlay will be unstyled:",
          error
        );
      });
  }

  /** Build the static widget skeleton (no dynamic data → innerHTML is safe). */
  #buildSkeleton() {
    this.#root = document.createElement("div");
    this.#root.className = "fc-root";
    this.#root.dataset.position = this.#normalizedPosition();
    this.#root.innerHTML = `
      <div class="fc-anchor">
        <div class="fc-pill-row">
          <button class="fc-pill" type="button" aria-expanded="false"
                  title="Toggle fact-check history">
            <span class="fc-dot" data-state="off"></span>
            <span class="fc-pill-label">Fact-check · 0</span>
          </button>
        </div>
        <section class="fc-panel" hidden>
          <header class="fc-panel-header">
            <span>Session fact-checks</span>
            <span class="fc-panel-status">
              <span class="fc-dot" data-state="off"></span>
              <span class="fc-panel-status-text">Idle</span>
            </span>
          </header>
          <div class="fc-panel-list"></div>
          <footer class="fc-panel-footer" hidden>
            <span class="fc-skip-text"></span>
            <span aria-hidden="true">·</span>
            <button class="fc-edit-topics" type="button">Edit topics</button>
          </footer>
        </section>
        <div class="fc-chip" hidden>
          <span class="fc-spinner"></span>
          <span class="fc-chip-text">Checking claim…</span>
        </div>
        <div class="fc-notice" hidden></div>
        <div class="fc-transcript" hidden></div>
        <div class="fc-toasts"></div>
      </div>
    `;
    this.#shadow.appendChild(this.#root);
    const query = (selector) => this.#root.querySelector(selector);
    this.#refs = {
      pill: query(".fc-pill"),
      pillDot: query(".fc-pill .fc-dot"),
      pillLabel: query(".fc-pill-label"),
      panel: query(".fc-panel"),
      panelList: query(".fc-panel-list"),
      panelDot: query(".fc-panel-status .fc-dot"),
      panelStatusText: query(".fc-panel-status-text"),
      panelFooter: query(".fc-panel-footer"),
      skipText: query(".fc-skip-text"),
      editTopics: query(".fc-edit-topics"),
      chip: query(".fc-chip"),
      chipText: query(".fc-chip-text"),
      notice: query(".fc-notice"),
      transcript: query(".fc-transcript"),
      toasts: query(".fc-toasts"),
    };
    this.#refs.pill.addEventListener("click", () => this.#togglePanel());
    this.#refs.editTopics.addEventListener("click", () => {
      this.onEditTopics?.();
    });
    this.renderHistory(this.#historyEntries);
    this.setConnectionState(this.#running);
    this.setTopicSkipCount(this.#topicSkipCount);
  }

  #togglePanel() {
    this.#panelOpen = !this.#panelOpen;
    this.#refs.panel.hidden = !this.#panelOpen;
    this.#refs.pill.setAttribute("aria-expanded", String(this.#panelOpen));
  }

  /* ------------------------------------------------------------------ */
  /* Private: toasts                                                    */
  /* ------------------------------------------------------------------ */

  #buildToast(verdict) {
    const label = FactCheckOverlay.#normalizeLabel(verdict.label);
    const element = document.createElement("article");
    element.className = "fc-toast";
    element.dataset.label = label;
    element.setAttribute("role", "status");

    const head = document.createElement("div");
    head.className = "fc-toast-head";
    head.appendChild(FactCheckOverlay.#buildLabelPill(label));
    head.appendChild(FactCheckOverlay.#buildTopicDot(verdict.topic));
    const time = document.createElement("span");
    time.className = "fc-toast-time";
    time.textContent = FactCheckOverlay.#formatTime(verdict.checked_at);
    head.appendChild(time);
    const close = document.createElement("button");
    close.type = "button";
    close.className = "fc-toast-close";
    close.setAttribute("aria-label", "Dismiss");
    close.textContent = "✕";
    head.appendChild(close);
    element.appendChild(head);

    const claim = document.createElement("p");
    claim.className = "fc-claim";
    claim.textContent = typeof verdict.claim === "string" ? verdict.claim : "";
    element.appendChild(claim);

    if (typeof verdict.explanation === "string" && verdict.explanation) {
      const explanation = document.createElement("p");
      explanation.className = "fc-explanation";
      explanation.textContent = verdict.explanation;
      element.appendChild(explanation);
    }

    const sources = FactCheckOverlay.#buildSourceLinks(
      verdict.sources,
      FactCheckOverlay.TOAST_SOURCE_LIMIT
    );
    if (sources) {
      element.appendChild(sources);
    }

    const toast = {
      element,
      remainingMs: this.#durationMs(),
      timerId: null,
      startedAt: 0,
      dismissed: false,
    };
    close.addEventListener("click", () => this.#dismissToast(toast));
    element.addEventListener("mouseenter", () => this.#pauseToastTimer(toast));
    element.addEventListener("mouseleave", () => this.#resumeToastTimer(toast));
    return toast;
  }

  #startToastTimer(toast) {
    toast.startedAt = performance.now();
    toast.timerId = setTimeout(() => this.#dismissToast(toast), toast.remainingMs);
  }

  /** Hover pauses the countdown; the remaining time is banked. */
  #pauseToastTimer(toast) {
    if (toast.dismissed || toast.timerId === null) {
      return;
    }
    clearTimeout(toast.timerId);
    toast.timerId = null;
    toast.remainingMs -= performance.now() - toast.startedAt;
  }

  #resumeToastTimer(toast) {
    if (toast.dismissed || toast.timerId !== null) {
      return;
    }
    toast.remainingMs = Math.max(
      toast.remainingMs,
      FactCheckOverlay.MIN_RESUME_MS
    );
    this.#startToastTimer(toast);
  }

  #dismissToast(toast, {immediate = false} = {}) {
    if (toast.dismissed) {
      return;
    }
    toast.dismissed = true;
    if (toast.timerId !== null) {
      clearTimeout(toast.timerId);
      toast.timerId = null;
    }
    const index = this.#activeToasts.indexOf(toast);
    if (index !== -1) {
      this.#activeToasts.splice(index, 1);
    }
    if (immediate) {
      toast.element.remove();
      return;
    }
    toast.element.classList.add("fc-leaving");
    const removeElement = () => toast.element.remove();
    toast.element.addEventListener("animationend", removeElement, {once: true});
    setTimeout(removeElement, FactCheckOverlay.TOAST_EXIT_MS + 100);
  }

  /* ------------------------------------------------------------------ */
  /* Private: history panel                                             */
  /* ------------------------------------------------------------------ */

  #buildHistoryEntry(verdict) {
    const key = FactCheckOverlay.#entryKey(verdict);
    const entry = document.createElement("div");
    entry.className = "fc-entry";

    const head = document.createElement("div");
    head.className = "fc-entry-head";
    head.appendChild(
      FactCheckOverlay.#buildLabelPill(
        FactCheckOverlay.#normalizeLabel(verdict.label)
      )
    );
    head.appendChild(FactCheckOverlay.#buildTopicDot(verdict.topic));
    const time = document.createElement("span");
    time.className = "fc-entry-time";
    time.textContent = FactCheckOverlay.#formatTime(verdict.checked_at);
    head.appendChild(time);
    entry.appendChild(head);

    const claim = document.createElement("p");
    claim.className = "fc-entry-claim";
    claim.textContent = typeof verdict.claim === "string" ? verdict.claim : "";
    entry.appendChild(claim);

    const details = document.createElement("div");
    details.className = "fc-entry-details";
    details.hidden = !this.#expandedEntryKeys.has(key);
    if (typeof verdict.explanation === "string" && verdict.explanation) {
      const explanation = document.createElement("p");
      explanation.className = "fc-explanation";
      explanation.textContent = verdict.explanation;
      details.appendChild(explanation);
    }
    const sources = FactCheckOverlay.#buildSourceLinks(
      verdict.sources,
      FactCheckOverlay.PANEL_SOURCE_LIMIT
    );
    if (sources) {
      details.appendChild(sources);
    }
    entry.appendChild(details);

    entry.addEventListener("click", (event) => {
      if (event.target instanceof Element && event.target.closest("a")) {
        return; // let source links navigate without toggling
      }
      details.hidden = !details.hidden;
      if (details.hidden) {
        this.#expandedEntryKeys.delete(key);
      } else {
        this.#expandedEntryKeys.add(key);
      }
    });
    return entry;
  }

  /* ------------------------------------------------------------------ */
  /* Private: transient elements + small helpers                        */
  /* ------------------------------------------------------------------ */

  #hideNotice() {
    if (this.#noticeTimerId !== null) {
      clearTimeout(this.#noticeTimerId);
      this.#noticeTimerId = null;
    }
    if (this.#refs) {
      this.#refs.notice.hidden = true;
    }
  }

  #hideTranscript() {
    if (this.#transcriptTimerId !== null) {
      clearTimeout(this.#transcriptTimerId);
      this.#transcriptTimerId = null;
    }
    if (this.#refs) {
      this.#refs.transcript.hidden = true;
    }
  }

  #normalizedPosition() {
    return FactCheckOverlay.VALID_POSITIONS.has(this.#settings.popupPosition)
      ? this.#settings.popupPosition
      : "top-right";
  }

  #durationMs() {
    const seconds = Number(this.#settings.popupDurationS);
    return Number.isFinite(seconds) && seconds > 0
      ? seconds * 1000
      : FactCheckOverlay.DEFAULT_DURATION_MS;
  }

  /**
   * Make sure an absolute-positioned host will anchor to parentElement.
   * Twitch's player container is already positioned; on other platforms the
   * chosen container may be `static`, which would make the host anchor to
   * some distant ancestor. Guardedly promote it to position:relative and
   * verify the promotion stuck (a page stylesheet with !important can win
   * over an inline style).
   *
   * @param {Element} parentElement
   * @returns {boolean} true when parentElement is (now) a positioned
   *   ancestor; false → caller should use viewport-fixed positioning.
   */
  static #ensurePositionedAncestor(parentElement) {
    try {
      if (getComputedStyle(parentElement).position !== "static") {
        return true;
      }
      parentElement.style.position = "relative";
      return getComputedStyle(parentElement).position !== "static";
    } catch (error) {
      console.warn(
        "[fact-checker] could not verify mount-target positioning:",
        error
      );
      return false;
    }
  }

  static #normalizeLabel(label) {
    return FactCheckOverlay.VALID_LABELS.has(label) ? label : "UNVERIFIED";
  }

  static #buildLabelPill(label) {
    const pill = document.createElement("span");
    pill.className = "fc-label-pill";
    pill.dataset.label = label;
    pill.textContent = label;
    return pill;
  }

  /** Unknown or missing verdict.topic slugs coerce to the "other" catch-all. */
  static #normalizeTopic(topic) {
    return Object.prototype.hasOwnProperty.call(TOPIC_COLORS, topic)
      ? topic
      : "other";
  }

  /**
   * Small colored category dot shown beside the verdict label pill; the
   * topic's display label is exposed as the hover tooltip.
   *
   * @param {string|undefined} topic - verdict.topic slug
   */
  static #buildTopicDot(topic) {
    const slug = FactCheckOverlay.#normalizeTopic(topic);
    const dot = document.createElement("span");
    dot.className = "fc-topic-dot";
    dot.style.background = TOPIC_COLORS[slug];
    dot.title = TOPIC_LABELS[slug];
    return dot;
  }

  static #formatTime(isoTimestamp) {
    const date = isoTimestamp ? new Date(isoTimestamp) : new Date();
    if (Number.isNaN(date.getTime())) {
      return "";
    }
    return date.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
  }

  /** Stable key for remembering which history entries are expanded. */
  static #entryKey(verdict) {
    return typeof verdict.id === "string" && verdict.id
      ? verdict.id
      : `${verdict.claim ?? ""}|${verdict.checked_at ?? ""}`;
  }

  /**
   * Render up to `limit` source links as domain-name chips. URLs must parse
   * as http(s); anything else is skipped (defense against a misbehaving
   * backend injecting javascript: URLs).
   */
  static #buildSourceLinks(sources, limit) {
    if (!Array.isArray(sources) || sources.length === 0) {
      return null;
    }
    const container = document.createElement("div");
    container.className = "fc-sources";
    let rendered = 0;
    for (const source of sources) {
      if (rendered >= limit) {
        break;
      }
      const url = typeof source?.url === "string" ? source.url : "";
      const domain = FactCheckOverlay.#domainFromUrl(url);
      if (!domain) {
        continue;
      }
      const link = document.createElement("a");
      link.className = "fc-source-link";
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = domain;
      link.title = source.title || url;
      container.appendChild(link);
      rendered += 1;
    }
    return rendered > 0 ? container : null;
  }

  static #domainFromUrl(url) {
    let parsed;
    try {
      parsed = new URL(url);
    } catch {
      return null;
    }
    if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
      return null;
    }
    return parsed.hostname.replace(/^www\./, "");
  }
}

// Explicit global export: content.js (a separate classic script in the same
// isolated world) constructs this class.
self.FactCheckOverlay = FactCheckOverlay;
