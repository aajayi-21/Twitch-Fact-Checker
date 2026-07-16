/**
 * Options page logic (ES module — extension pages may use modules, unlike
 * content scripts). Reads and writes the single chrome.storage.sync
 * "settings" key exclusively through shared/settings.js so partial writes
 * always merge instead of clobbering.
 *
 * Live propagation is handled elsewhere: the content script re-reads
 * position/duration/transcript via storage.onChanged, and the offscreen
 * document forwards sensitivity changes to the backend as a WS config frame.
 * backendUrl only applies on the next capture start (noted in the UI).
 */

import {DEFAULT_SETTINGS, loadSettings, saveSettings} from "../shared/settings.js";

const SAVE_CONFIRMATION_MS = 2200;
const DURATION_MIN_S = 4;
const DURATION_MAX_S = 60;

const formElement = document.getElementById("settings-form");
const backendUrlInput = document.getElementById("backend-url");
const backendUrlError = document.getElementById("backend-url-error");
const durationInput = document.getElementById("popup-duration");
const transcriptCheckbox = document.getElementById("show-transcript");
const saveConfirmation = document.getElementById("save-confirmation");

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

populateForm().catch((error) => {
  console.error("[fact-checker] options init failed:", error);
});
