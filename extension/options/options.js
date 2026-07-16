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

const OTHER_TOPIC_SUBLABEL = "claims that don't fit a category above";

const formElement = document.getElementById("settings-form");
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

populateForm().catch((error) => {
  console.error("[fact-checker] options init failed:", error);
});
