/**
 * Options page logic (ES module — extension pages may use modules, unlike
 * content scripts). Reads and writes the single chrome.storage.sync
 * "settings" key exclusively through shared/settings.js so partial writes
 * always merge instead of clobbering.
 *
 * Live propagation is handled elsewhere: the content script re-reads
 * position/duration/transcript via storage.onChanged, and the offscreen
 * document forwards sensitivity and topic-filter changes to the backend as
 * WS config frames. backendUrl only applies on the next capture start
 * (noted in the UI).
 *
 * The "Topics to fact-check" section is instant-apply (saved on every
 * change, no Save button involvement); the rest of the form keeps its
 * explicit Save behavior.
 */

import {
  DEFAULT_SETTINGS,
  TOPIC_COLORS,
  TOPIC_LABELS,
  TOPIC_SLUGS,
  loadSettings,
  saveSettings,
} from "../shared/settings.js";

const SAVE_CONFIRMATION_MS = 2200;
const DURATION_MIN_S = 4;
const DURATION_MAX_S = 60;

const SETUP_STATUS_TIMEOUT_MS = 2500;
// Saving validates the key live against the provider — allow it time.
const SETUP_SAVE_TIMEOUT_MS = 30000;
// How long the "verified" status stays visible before the card collapses.
const SETUP_SUCCESS_COLLAPSE_MS = 1400;
const PROVIDER_LABELS = Object.freeze({
  openrouter: "OpenRouter",
  gemini: "Gemini",
});
const BACKEND_DOWN_COPY =
  "Backend isn't running — start it first (./backend/run.sh in the project folder)";

const OTHER_TOPIC_SUBLABEL = "claims that don't fit a category above";

const formElement = document.getElementById("settings-form");
const providerForm = document.getElementById("provider-form");
const providerFormBody = document.getElementById("provider-form-body");
const providerConnected = document.getElementById("provider-connected");
const providerConnectedSummary = document.getElementById(
  "provider-connected-summary"
);
const changeKeyButton = document.getElementById("change-key-button");
const cancelChangeButton = document.getElementById("cancel-change-button");
const apiKeyInput = document.getElementById("api-key-input");
const keyVisibilityToggle = document.getElementById("key-visibility-toggle");
const saveKeyButton = document.getElementById("save-key-button");
const providerStatus = document.getElementById("provider-status");
const backendUrlInput = document.getElementById("backend-url");
const backendUrlError = document.getElementById("backend-url-error");
const durationInput = document.getElementById("popup-duration");
const transcriptCheckbox = document.getElementById("show-transcript");
const saveConfirmation = document.getElementById("save-confirmation");
const topicRowsContainer = document.getElementById("topic-rows");
const topicsMasterCheckbox = document.getElementById("topics-master");

/** slug -> <input type="checkbox">, filled by buildTopicRows(). */
const topicCheckboxes = new Map();

let confirmationTimerId = null;

const getRadioValue = (name) =>
  formElement.querySelector(`input[name="${name}"]:checked`)?.value ?? null;

const setRadioValue = (name, value) => {
  const radio = formElement.querySelector(
    `input[name="${name}"][value="${value}"]`
  );
  if (radio) {
    radio.checked = true;
  }
};

/** A backend URL is valid when it parses and uses the ws:// or wss:// scheme. */
const isValidBackendUrl = (value) => {
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    return false;
  }
  return (
    (parsed.protocol === "ws:" || parsed.protocol === "wss:") &&
    parsed.host.length > 0
  );
};

const clampDurationSeconds = (rawValue) => {
  const seconds = Number(rawValue);
  if (!Number.isFinite(seconds)) {
    return DEFAULT_SETTINGS.popupDurationS;
  }
  return Math.min(DURATION_MAX_S, Math.max(DURATION_MIN_S, Math.round(seconds)));
};

const setBackendUrlError = (message) => {
  if (message) {
    backendUrlError.textContent = message;
    backendUrlError.hidden = false;
    backendUrlInput.setAttribute("aria-invalid", "true");
  } else {
    backendUrlError.hidden = true;
    backendUrlInput.removeAttribute("aria-invalid");
  }
};

const showConfirmation = (text, isError = false) => {
  saveConfirmation.textContent = text;
  saveConfirmation.classList.toggle("error", isError);
  saveConfirmation.classList.add("visible");
  if (confirmationTimerId !== null) {
    clearTimeout(confirmationTimerId);
  }
  confirmationTimerId = setTimeout(() => {
    saveConfirmation.classList.remove("visible");
    confirmationTimerId = null;
  }, SAVE_CONFIRMATION_MS);
};

/* ----------------------------------------------------- provider setup -- */

/**
 * The "Connect your AI provider" card talks to the local backend's
 * /setup/status and /setup/credentials endpoints. The API key exists ONLY
 * as the transient DOM input value and the one POST body sent to the
 * localhost backend — it is never written to chrome.storage or persisted
 * client-side in any form.
 */

/**
 * Derive the backend's http(s) origin from the ws:// backendUrl setting,
 * exactly like popup.js derives its healthz URL:
 * ws://host:port/ws/audio -> http://host:port (wss -> https).
 *
 * @param {string} backendUrl
 * @returns {string|null}
 */
const deriveBackendHttpOrigin = (backendUrl) => {
  try {
    const wsUrl = new URL(backendUrl);
    const httpProtocol = wsUrl.protocol === "wss:" ? "https:" : "http:";
    return `${httpProtocol}//${wsUrl.host}`;
  } catch (error) {
    console.error(`[fact-checker] invalid backendUrl "${backendUrl}":`, error);
    return null;
  }
};

const getSelectedProvider = () =>
  providerForm.querySelector('input[name="apiProvider"]:checked')?.value ??
  "openrouter";

const setSelectedProvider = (provider) => {
  const radio = providerForm.querySelector(
    `input[name="apiProvider"][value="${provider}"]`
  );
  if (radio) {
    radio.checked = true;
  }
};

/** @param {"checking"|"setup"|"pending"|"connected"|"error"} stateName */
const setProviderStatus = (text, stateName) => {
  providerStatus.textContent = text;
  providerStatus.dataset.state = stateName;
};

/**
 * Provider/key/credits tail shared by the transient status line and the
 * compact connected row, e.g. "OpenRouter · key …abcd · $4.97 credits"
 * (credits are only present for OpenRouter; remaining = total - usage).
 *
 * @param {object} status - /setup/status (or POST /setup/credentials) body
 * @returns {string}
 */
const describeConnectedDetails = (status) => {
  const parts = [PROVIDER_LABELS[status.provider] ?? status.provider];
  if (status.key_hint) {
    parts.push(`key ${status.key_hint}`);
  }
  const credits = status.credits;
  if (
    credits &&
    Number.isFinite(credits.total) &&
    Number.isFinite(credits.usage)
  ) {
    parts.push(`$${(credits.total - credits.usage).toFixed(2)} credits`);
  }
  return parts.join(" · ");
};

/**
 * Last known-configured /setup/status body — provider, key_hint, credits
 * only, NEVER any key material. Lets "Cancel" restore the collapsed summary
 * without a network round-trip. null until the backend reports configured.
 */
let lastConnectedStatus = null;
/** Pending "show success briefly, then collapse" timer, or null. */
let collapseTimerId = null;

const cancelPendingCollapse = () => {
  if (collapseTimerId !== null) {
    clearTimeout(collapseTimerId);
    collapseTimerId = null;
  }
};

/**
 * Collapse the card to the compact connected summary: green check, one-line
 * provider/key/credits description, "Change key" button. Hides the form
 * body and always clears the key input — the key never survives a collapse.
 *
 * @param {object} status - configured /setup/status-shaped body
 */
const collapseToConnectedSummary = (status) => {
  cancelPendingCollapse();
  lastConnectedStatus = status;
  if (status.provider) {
    setSelectedProvider(status.provider);
  }
  apiKeyInput.value = "";
  resetKeyVisibility();
  providerConnectedSummary.textContent = `Connected — ${describeConnectedDetails(
    status
  )}`;
  providerFormBody.hidden = true;
  cancelChangeButton.hidden = true;
  providerConnected.hidden = false;
};

/**
 * "Change key": swap the summary back for the full form with the current
 * provider preselected, an empty focused key input, and a Cancel link that
 * returns to the summary without saving.
 */
const expandProviderForm = () => {
  cancelPendingCollapse();
  providerConnected.hidden = true;
  providerFormBody.hidden = false;
  if (lastConnectedStatus?.provider) {
    setSelectedProvider(lastConnectedStatus.provider);
  }
  apiKeyInput.value = "";
  resetKeyVisibility();
  cancelChangeButton.hidden = lastConnectedStatus === null;
  setProviderStatus("Paste a new API key, then Save & verify.", "setup");
  apiKeyInput.focus();
};

/** Cancel a key change: back to the last connected summary, nothing saved. */
const cancelKeyChange = () => {
  if (lastConnectedStatus !== null) {
    collapseToConnectedSummary(lastConnectedStatus);
  }
};

const renderSetupStatus = (status) => {
  if (status?.configured) {
    // Don't yank the form away from a user who already started typing a new
    // key while the on-open status probe was in flight.
    if (
      apiKeyInput.value !== "" ||
      apiKeyInput === document.activeElement
    ) {
      lastConnectedStatus = status;
      return;
    }
    collapseToConnectedSummary(status);
  } else {
    setProviderStatus(
      "Not connected — choose a provider, paste an API key, then Save & verify.",
      "setup"
    );
  }
};

/** On-open probe: GET /setup/status and render connected vs. setup prompt. */
const refreshSetupStatus = async () => {
  let settings;
  try {
    settings = await loadSettings();
  } catch (error) {
    console.error("[fact-checker] loading settings for setup failed:", error);
    settings = {...DEFAULT_SETTINGS};
  }
  const origin = deriveBackendHttpOrigin(settings.backendUrl);
  if (!origin) {
    setProviderStatus("Backend URL below is invalid — fix it first.", "error");
    return;
  }
  try {
    const response = await fetch(`${origin}/setup/status`, {
      signal: AbortSignal.timeout(SETUP_STATUS_TIMEOUT_MS),
      cache: "no-store",
    });
    if (!response.ok) {
      throw new Error(`GET /setup/status returned ${response.status}`);
    }
    renderSetupStatus(await response.json());
  } catch (error) {
    console.warn("[fact-checker] setup status unavailable:", error);
    setProviderStatus(BACKEND_DOWN_COPY, "error");
  }
};

const resetKeyVisibility = () => {
  apiKeyInput.type = "password";
  keyVisibilityToggle.textContent = "Show";
  keyVisibilityToggle.setAttribute("aria-pressed", "false");
};

/**
 * POST /setup/credentials: the backend validates the key live with the
 * provider (free probe), persists it into its .env, and hot-swaps. Renders
 * the response's connected summary, or the error detail verbatim.
 */
const handleProviderSubmit = async (event) => {
  event.preventDefault();
  const provider = getSelectedProvider();
  const apiKey = apiKeyInput.value.trim();
  if (!apiKey) {
    // Deliberately BEFORE cancelPendingCollapse(): an empty re-submit during
    // the post-save success beat must not cancel the scheduled collapse.
    setProviderStatus("Paste an API key first.", "error");
    apiKeyInput.focus();
    return;
  }
  cancelPendingCollapse();
  let settings;
  try {
    settings = await loadSettings();
  } catch (error) {
    console.error("[fact-checker] loading settings for setup failed:", error);
    settings = {...DEFAULT_SETTINGS};
  }
  const origin = deriveBackendHttpOrigin(settings.backendUrl);
  if (!origin) {
    setProviderStatus("Backend URL below is invalid — fix it first.", "error");
    return;
  }
  saveKeyButton.disabled = true;
  setProviderStatus(
    `Verifying key with ${PROVIDER_LABELS[provider] ?? provider}…`,
    "pending"
  );
  try {
    let response;
    try {
      response = await fetch(`${origin}/setup/credentials`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({provider, api_key: apiKey}),
        signal: AbortSignal.timeout(SETUP_SAVE_TIMEOUT_MS),
      });
    } catch (networkError) {
      console.warn("[fact-checker] credentials POST failed:", networkError);
      setProviderStatus(BACKEND_DOWN_COPY, "error");
      return;
    }
    let body = null;
    try {
      body = await response.json();
    } catch (parseError) {
      console.warn("[fact-checker] unparseable setup response:", parseError);
    }
    if (response.ok) {
      apiKeyInput.value = ""; // the key never outlives the request
      resetKeyVisibility();
      const status = body?.configured ? body : {configured: true, provider};
      // Refresh Cancel's snapshot right away so a click during the brief
      // success beat still collapses to the NEW key's summary.
      lastConnectedStatus = status;
      setProviderStatus(
        `Key verified ✓ ${describeConnectedDetails(status)}`,
        "connected"
      );
      collapseTimerId = setTimeout(() => {
        collapseTimerId = null;
        collapseToConnectedSummary(status);
      }, SETUP_SUCCESS_COLLAPSE_MS);
      return;
    }
    const detail =
      typeof body?.detail === "string" && body.detail.length > 0
        ? body.detail
        : `Setup failed (HTTP ${response.status}).`;
    setProviderStatus(
      response.status === 401 ? `Key rejected: ${detail}` : detail,
      "error"
    );
  } finally {
    saveKeyButton.disabled = false;
  }
};

const wireProviderCard = () => {
  changeKeyButton.addEventListener("click", expandProviderForm);
  cancelChangeButton.addEventListener("click", cancelKeyChange);
  keyVisibilityToggle.addEventListener("click", () => {
    const reveal = apiKeyInput.type === "password";
    apiKeyInput.type = reveal ? "text" : "password";
    keyVisibilityToggle.textContent = reveal ? "Hide" : "Show";
    keyVisibilityToggle.setAttribute("aria-pressed", String(reveal));
  });
  providerForm.addEventListener("submit", (event) => {
    handleProviderSubmit(event).catch((error) => {
      console.error("[fact-checker] saving credentials failed:", error);
      setProviderStatus("Saving the key failed — see console for details.", "error");
      saveKeyButton.disabled = false;
    });
  });
};

/* ------------------------------------------------------------- topics -- */

/** The topics the user may toggle — everything except the "other" catch-all. */
const togglableTopicSlugs = TOPIC_SLUGS.filter((slug) => slug !== "other");

/**
 * Build one color-dotted checkbox row per topic slug (canonical contract
 * order). The "other" catch-all row is rendered checked + disabled with an
 * explanatory sublabel: the backend always treats it as enabled.
 */
const buildTopicRows = () => {
  for (const slug of TOPIC_SLUGS) {
    const isOther = slug === "other";
    const row = document.createElement("label");
    row.className = isOther ? "topic-row topic-row-disabled" : "topic-row";

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.name = "topic";
    checkbox.value = slug;
    if (isOther) {
      checkbox.checked = true;
      checkbox.disabled = true;
    }
    topicCheckboxes.set(slug, checkbox);
    row.appendChild(checkbox);

    const dot = document.createElement("span");
    dot.className = "topic-dot";
    dot.style.background = TOPIC_COLORS[slug];
    dot.setAttribute("aria-hidden", "true");
    row.appendChild(dot);

    const text = document.createElement("span");
    text.className = "topic-text";
    const name = document.createElement("span");
    name.className = "topic-name";
    name.textContent = TOPIC_LABELS[slug];
    text.appendChild(name);
    if (isOther) {
      const sublabel = document.createElement("span");
      sublabel.className = "topic-sublabel";
      sublabel.textContent = OTHER_TOPIC_SUBLABEL;
      text.appendChild(sublabel);
    }
    row.appendChild(text);

    topicRowsContainer.appendChild(row);
  }
};

/**
 * Tri-state master: checked when every togglable topic is on, unchecked when
 * every togglable topic is off, indeterminate for a mix. The always-on
 * "other" catch-all is excluded so unchecking the master reads as unchecked,
 * not mixed.
 */
const updateTopicsMasterState = () => {
  const checkedCount = togglableTopicSlugs.filter(
    (slug) => topicCheckboxes.get(slug).checked
  ).length;
  topicsMasterCheckbox.checked = checkedCount === togglableTopicSlugs.length;
  topicsMasterCheckbox.indeterminate =
    checkedCount > 0 && checkedCount < togglableTopicSlugs.length;
};

/** Collect the checkbox states into the settings.topics shape ("other" forced on). */
const collectTopics = () => {
  const topics = {};
  for (const slug of TOPIC_SLUGS) {
    topics[slug] = slug === "other" ? true : topicCheckboxes.get(slug).checked;
  }
  return topics;
};

/**
 * Instant apply: persist the topic selection on every change. The service
 * worker's storage.onChanged relay forwards it to a running session, so no
 * Save click is needed (or offered) for topics.
 */
const saveTopicSelection = async () => {
  try {
    await saveSettings({topics: collectTopics()});
  } catch (error) {
    console.error("[fact-checker] saving topic selection failed:", error);
    showConfirmation("Saving topics failed — see console for details", true);
  }
};

const populateTopics = (topics) => {
  for (const slug of togglableTopicSlugs) {
    topicCheckboxes.get(slug).checked = topics?.[slug] !== false;
  }
  updateTopicsMasterState();
};

const wireTopicHandlers = () => {
  topicsMasterCheckbox.addEventListener("change", () => {
    for (const slug of togglableTopicSlugs) {
      topicCheckboxes.get(slug).checked = topicsMasterCheckbox.checked;
    }
    topicsMasterCheckbox.indeterminate = false;
    saveTopicSelection().catch((error) => {
      console.error("[fact-checker] topic master apply failed:", error);
    });
  });
  for (const slug of togglableTopicSlugs) {
    topicCheckboxes.get(slug).addEventListener("change", () => {
      updateTopicsMasterState();
      saveTopicSelection().catch((error) => {
        console.error("[fact-checker] topic apply failed:", error);
      });
    });
  }
};

/* --------------------------------------------------------------- form -- */

const populateForm = async () => {
  let settings;
  try {
    settings = await loadSettings();
  } catch (error) {
    console.error("[fact-checker] loading settings failed:", error);
    settings = {...DEFAULT_SETTINGS};
    showConfirmation("Could not load saved settings — showing defaults", true);
  }
  backendUrlInput.value = settings.backendUrl;
  setRadioValue(
    "sensitivity",
    ["low", "medium", "high"].includes(settings.sensitivity)
      ? settings.sensitivity
      : DEFAULT_SETTINGS.sensitivity
  );
  setRadioValue(
    "popupPosition",
    ["top-left", "top-right", "bottom-left", "bottom-right"].includes(
      settings.popupPosition
    )
      ? settings.popupPosition
      : DEFAULT_SETTINGS.popupPosition
  );
  durationInput.value = String(clampDurationSeconds(settings.popupDurationS));
  transcriptCheckbox.checked = Boolean(settings.showTranscript);
  populateTopics(settings.topics);
};

const handleSubmit = async (event) => {
  event.preventDefault();
  const backendUrl = backendUrlInput.value.trim();
  if (!isValidBackendUrl(backendUrl)) {
    setBackendUrlError(
      "Enter a valid WebSocket URL starting with ws:// or wss:// " +
        "(e.g. ws://127.0.0.1:8710/ws/audio)."
    );
    backendUrlInput.focus();
    return;
  }
  setBackendUrlError(null);
  const updatedSettings = {
    backendUrl,
    sensitivity: getRadioValue("sensitivity") ?? DEFAULT_SETTINGS.sensitivity,
    popupPosition:
      getRadioValue("popupPosition") ?? DEFAULT_SETTINGS.popupPosition,
    popupDurationS: clampDurationSeconds(durationInput.value),
    showTranscript: transcriptCheckbox.checked,
  };
  durationInput.value = String(updatedSettings.popupDurationS); // reflect clamp
  try {
    await saveSettings(updatedSettings);
    showConfirmation("Saved");
  } catch (error) {
    console.error("[fact-checker] saving settings failed:", error);
    showConfirmation("Save failed — see console for details", true);
  }
};

formElement.addEventListener("submit", (event) => {
  handleSubmit(event).catch((error) => {
    console.error("[fact-checker] settings submit failed:", error);
    showConfirmation("Save failed — see console for details", true);
  });
});

backendUrlInput.addEventListener("input", () => setBackendUrlError(null));

buildTopicRows();
wireTopicHandlers();
wireProviderCard();

populateForm().catch((error) => {
  console.error("[fact-checker] options init failed:", error);
});

refreshSetupStatus().catch((error) => {
  console.error("[fact-checker] setup status check failed:", error);
  setProviderStatus(BACKEND_DOWN_COPY, "error");
});
