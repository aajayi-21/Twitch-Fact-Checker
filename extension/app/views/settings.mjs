/**
 * Settings: how sensitive the checker is, which topics it covers, how
 * verdicts appear over the stream, and what happens to the recorded history.
 *
 * Changes save as they are made (there is no Save button to forget) and
 * reach a running session through chrome.storage, exactly as before.
 */

import {
  Button,
  Card,
  ConfirmButton,
  Notice,
  Segmented,
  SettingRow,
  Toggle,
} from "../components.mjs";
import {
  DEFAULT_SETTINGS,
  TOPIC_COLORS,
  TOPIC_LABELS,
  TOPIC_SLUGS,
} from "../../shared/settings.js";
import {updateSettings, useAppState} from "../lib/app_store.mjs";
import {plural} from "../lib/format.mjs";
import {html, useEffect, useRef, useState} from "../lib/html.mjs";
import {exportJson, removeEverything, useLedger} from "../lib/ledger_store.mjs";
import {
  DURATION_MAX_S,
  DURATION_MIN_S,
  clampDurationSeconds,
  isValidBackendUrl,
  setAllTopics,
  setTopicEnabled,
  topicsMasterState,
} from "../lib/settings_rules.mjs";
import {ImportHistory} from "./import_history.mjs";

const SENSITIVITY_OPTIONS = Object.freeze([
  {value: "low", label: "Low", hint: "Only clear, significant claims"},
  {value: "medium", label: "Medium", hint: "A balanced default"},
  {value: "high", label: "High", hint: "More borderline claims, more cost"},
]);

const POSITIONS = Object.freeze([
  {value: "top-left", label: "Top left"},
  {value: "top-right", label: "Top right"},
  {value: "bottom-left", label: "Bottom left"},
  {value: "bottom-right", label: "Bottom right"},
]);

const RETENTION_OPTIONS = Object.freeze([
  [0, "Until I delete it"],
  [365, "1 year"],
  [90, "90 days"],
  [30, "30 days"],
]);

const SAVED_NOTE_MS = 1800;

/** History kept on this device: how long, and getting it in and out. */
const DataCard = ({settings, save}) => {
  const ledger = useLedger();
  const [deleted, setDeleted] = useState(false);
  const empty = ledger.sessions.length === 0 && ledger.verdicts.length === 0;
  return html`
    <${Card} title="Your data">
      <p class="setting-help">
        Your history — sessions, claims, results, ratings and usage — is stored in this browser
        and nowhere else.
        ${ledger.loaded
          ? ` It holds ${plural(ledger.verdicts.length, "result")} from ${plural(ledger.sessions.length, "session")}.`
          : ""}
      </p>
      <${SettingRow}
        label="Keep history for"
        help="Sessions older than this are removed automatically, within a day of the change."
        htmlFor="history-retention"
      >
        <select
          id="history-retention"
          value=${String(settings.historyRetentionDays ?? 0)}
          onChange=${(event) => save({historyRetentionDays: Number(event.currentTarget.value)})}
        >
          ${RETENTION_OPTIONS.map(
            ([days, label]) => html`<option key=${days} value=${String(days)}>${label}</option>`
          )}
        </select>
      <//>
      <${SettingRow} label="Past results in the backend" stacked>
        <${ImportHistory} />
      <//>
      <${SettingRow}
        label="Export"
        help="One JSON file with everything above. Results can also be exported as a spreadsheet from History."
      >
        <${Button} disabled=${empty} onClick=${() => exportJson()}>Export everything<//>
      <//>
      <${SettingRow}
        label="Delete"
        help="Removes the whole history from this browser. The backend's own database is not touched."
      >
        <${ConfirmButton}
          disabled=${empty}
          confirmLabel="Delete all history for good?"
          onConfirm=${async () => {
            await removeEverything();
            setDeleted(true);
          }}
        >
          Delete all history
        <//>
      <//>
      ${deleted && empty ? html`<${Notice} tone="success">Your history has been deleted.<//>` : null}
    <//>
  `;
};

export const SettingsView = () => {
  const {settings} = useAppState();
  const [savedAt, setSavedAt] = useState(0);
  const [saveError, setSaveError] = useState(null);
  // Free-text fields are edited locally and committed when valid.
  const [backendUrl, setBackendUrl] = useState(settings.backendUrl);
  const [duration, setDuration] = useState(String(settings.popupDurationS));
  const savedTimer = useRef(null);

  useEffect(() => setBackendUrl(settings.backendUrl), [settings.backendUrl]);
  useEffect(() => setDuration(String(settings.popupDurationS)), [settings.popupDurationS]);
  useEffect(() => () => clearTimeout(savedTimer.current), []);

  const save = async (partial) => {
    try {
      await updateSettings(partial);
      setSaveError(null);
      setSavedAt(Date.now());
      clearTimeout(savedTimer.current);
      savedTimer.current = setTimeout(() => setSavedAt(0), SAVED_NOTE_MS);
    } catch (error) {
      console.error("[fact-checker] saving settings failed:", error);
      setSaveError("That change could not be saved. Chrome's sync storage may be full or offline.");
    }
  };

  const urlValid = isValidBackendUrl(backendUrl.trim());
  // Both commit from the field itself, not from state: a blur can arrive
  // before the re-render that follows the last keystroke.
  const commitBackendUrl = (event) => {
    const trimmed = event.currentTarget.value.trim();
    if (isValidBackendUrl(trimmed) && trimmed !== settings.backendUrl) {
      save({backendUrl: trimmed});
    }
  };
  const commitDuration = (event) => {
    const seconds = clampDurationSeconds(event.currentTarget.value, settings.popupDurationS);
    setDuration(String(seconds));
    if (seconds !== settings.popupDurationS) {
      save({popupDurationS: seconds});
    }
  };

  const masterState = topicsMasterState(settings.topics, TOPIC_SLUGS);

  return html`
    <div class="page">
      <header class="page-header">
        <h1 class="page-title">Settings</h1>
        <p class="page-subtitle">
          Changes save automatically and sync with your Chrome profile.
          <span class="saved-note" role="status" aria-live="polite">${savedAt ? " Saved." : ""}</span>
        </p>
      </header>
      ${saveError ? html`<${Notice} tone="error">${saveError}<//>` : null}

      <${Card} title="What gets checked">
        <${SettingRow}
          stacked
          label="Sensitivity"
          help="Higher sensitivity checks more borderline claims and uses more of your provider credit. Applies to a running session straight away."
        >
          <${Segmented}
            name="sensitivity"
            label="Sensitivity"
            options=${SENSITIVITY_OPTIONS}
            value=${settings.sensitivity}
            onChange=${(sensitivity) => save({sensitivity})}
          />
        <//>
        <${SettingRow}
          stacked
          label="Topics"
          help="Claims outside the topics you tick are heard but not checked. Applies to a running session straight away."
        >
          <div class="topics">
            <label class="topic topic-master">
              <input
                type="checkbox"
                checked=${masterState === "all"}
                indeterminate=${masterState === "some"}
                onChange=${(event) => save({topics: setAllTopics(TOPIC_SLUGS, event.currentTarget.checked)})}
              />
              <span class="topic-name">All topics</span>
            </label>
            ${TOPIC_SLUGS.map((slug) => {
              const locked = slug === "other";
              return html`
                <label class=${`topic${locked ? " topic-locked" : ""}`} key=${slug}>
                  <input
                    type="checkbox"
                    checked=${locked || settings.topics?.[slug] !== false}
                    disabled=${locked}
                    onChange=${(event) =>
                      save({topics: setTopicEnabled(settings.topics, slug, event.currentTarget.checked)})}
                  />
                  <span class="topic-dot" style=${`background: ${TOPIC_COLORS[slug]}`} aria-hidden="true"></span>
                  <span class="topic-name">${TOPIC_LABELS[slug]}</span>
                  ${locked ? html`<span class="topic-note">always on — claims that fit nowhere else</span>` : null}
                </label>
              `;
            })}
          </div>
        <//>
      <//>

      <${Card} title="How verdicts appear">
        <${SettingRow} stacked label="Position over the player" help="Applies immediately.">
          <div class="corner-grid" role="radiogroup" aria-label="Verdict position">
            ${POSITIONS.map(
              (position) => html`
                <label class="corner" key=${position.value}>
                  <input
                    type="radio"
                    name="popup-position"
                    value=${position.value}
                    checked=${settings.popupPosition === position.value}
                    onChange=${() => save({popupPosition: position.value})}
                  />
                  <span class="corner-body">
                    <span class="corner-preview" data-corner=${position.value} aria-hidden="true"></span>
                    <span>${position.label}</span>
                  </span>
                </label>
              `
            )}
          </div>
        <//>
        <${SettingRow}
          label="Seconds on screen"
          htmlFor="popup-duration"
          help=${`How long each verdict stays up (${DURATION_MIN_S}–${DURATION_MAX_S}). It stays while you hover it.`}
        >
          <input
            id="popup-duration"
            class="input-number"
            type="number"
            inputmode="numeric"
            min=${DURATION_MIN_S}
            max=${DURATION_MAX_S}
            value=${duration}
            onInput=${(event) => setDuration(event.currentTarget.value)}
            onBlur=${commitDuration}
            onKeyDown=${(event) => event.key === "Enter" && event.currentTarget.blur()}
          />
        <//>
        <${SettingRow}
          label="Show a live transcript"
          htmlFor="show-transcript"
          help="A subtle caption of what the fact-checker hears. Applies the next time you press Start."
        >
          <${Toggle}
            id="show-transcript"
            checked=${settings.showTranscript}
            onChange=${(showTranscript) => save({showTranscript})}
          />
        <//>
      <//>

      <${DataCard} settings=${settings} save=${save} />

      <${Card} title="Advanced">
        <${SettingRow}
          label="Check on-screen claims (experimental)"
          htmlFor="capture-video"
          help="While checking runs, a small screenshot of the stream (up to 480p) is taken every 5 seconds and sent to your local backend. Only when a claim seems to refer to something on screen is the latest one passed to your AI provider. Screenshots are never stored. Applies the next time you press Start."
        >
          <${Toggle}
            id="capture-video"
            checked=${settings.captureVideo}
            onChange=${(captureVideo) => save({captureVideo})}
          />
        <//>
        <${SettingRow}
          stacked
          label="Backend address"
          htmlFor="backend-url"
          help="Where the local backend listens. Change it only if you moved the backend to another port."
        >
          <input
            id="backend-url"
            type="text"
            spellcheck=${false}
            autocomplete="off"
            value=${backendUrl}
            aria-invalid=${urlValid ? undefined : "true"}
            aria-describedby="backend-url-error"
            onInput=${(event) => setBackendUrl(event.currentTarget.value)}
            onBlur=${commitBackendUrl}
            onKeyDown=${(event) => event.key === "Enter" && event.currentTarget.blur()}
          />
          <p class="field-error" id="backend-url-error" hidden=${urlValid}>
            Enter an address that starts with ws:// or wss://, for example
            ${" "}${DEFAULT_SETTINGS.backendUrl}
          </p>
          ${settings.backendUrl !== DEFAULT_SETTINGS.backendUrl
            ? html`<button
                type="button"
                class="link-button"
                onClick=${() => save({backendUrl: DEFAULT_SETTINGS.backendUrl})}
              >
                Reset to the default
              </button>`
            : null}
        <//>
      <//>
    </div>
  `;
};
