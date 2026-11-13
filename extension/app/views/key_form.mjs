/**
 * The "add an API key" form, used by the welcome flow and the providers
 * screens, for both engines.
 *
 * With the local backend (the defaults below) the key goes to the backend
 * once, which checks it live with the provider and stores it in its own
 * `.env`; it is never kept in the browser. With the browser engine the
 * caller passes its own `save`, `help` and `describeError`: the key is
 * checked from here and stored in this browser. Either way the field is
 * cleared as soon as the key is accepted.
 */

import {Button, ExternalLink, Notice} from "../components.mjs";
import {BACKEND_ERROR, saveProviderKey} from "../lib/app_store.mjs";
import {html, useState} from "../lib/html.mjs";

/** What to say for each way a save can fail. */
const describeSaveError = (error, providerLabel) => {
  switch (error?.kind) {
    case BACKEND_ERROR.KEY_REJECTED:
      return `${providerLabel} did not accept that key. Check it was copied in full and try again.`;
    case BACKEND_ERROR.UNREACHABLE:
      return "The local backend is not running, so the key could not be checked.";
    case BACKEND_ERROR.TIMEOUT:
      return "Checking the key took too long. Try again in a moment.";
    case BACKEND_ERROR.PROVIDER_DOWN:
      return `The backend could not reach ${providerLabel} to check the key. Check your connection and try again.`;
    default:
      return error?.message || "The key could not be saved.";
  }
};

/**
 * @param {object} props
 * @param {object} props.provider - an entry of setup_model.mjs KEY_PROVIDERS
 * @param {() => void} [props.onSaved]
 * @param {() => void} [props.onCancel] - shows a Cancel button when given
 * @param {(providerId: string, apiKey: string) => Promise<void>} [props.save]
 * @param {(error: unknown, providerLabel: string) => string} [props.describeError]
 * @param {any} [props.help] - what happens to the key, in a sentence
 */
export const KeyForm = ({
  provider,
  onSaved,
  onCancel,
  save = saveProviderKey,
  describeError = describeSaveError,
  help = null,
}) => {
  const [apiKey, setApiKey] = useState("");
  const [revealed, setRevealed] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const inputId = `api-key-${provider.id}`;

  const submit = async (event) => {
    event.preventDefault();
    if (!apiKey.trim() || saving) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await save(provider.id, apiKey.trim());
      setApiKey("");
      setRevealed(false);
      onSaved?.();
    } catch (saveError) {
      setError(describeError(saveError, provider.label));
    } finally {
      setSaving(false);
    }
  };

  return html`
    <form class="key-form" onSubmit=${submit} novalidate>
      <label class="field-label" for=${inputId}>${provider.label} API key</label>
      <div class="key-input">
        <input
          id=${inputId}
          type=${revealed ? "text" : "password"}
          value=${apiKey}
          placeholder=${provider.keyPlaceholder}
          autocomplete="off"
          spellcheck=${false}
          aria-invalid=${error ? "true" : undefined}
          aria-describedby=${`${inputId}-help`}
          onInput=${(event) => setApiKey(event.currentTarget.value)}
        />
        <button
          type="button"
          class="key-reveal"
          aria-pressed=${revealed ? "true" : "false"}
          onClick=${() => setRevealed(!revealed)}
        >
          ${revealed ? "Hide" : "Show"}
        </button>
      </div>
      <p class="field-help" id=${`${inputId}-help`}>
        ${help ??
        `Sent once to the backend on this computer, which checks it with ${provider.label} and stores it there. It is not kept in the browser.`}${" "}
        <${ExternalLink} href=${provider.keyUrl}>Get a key<//>
      </p>
      ${error ? html`<${Notice} tone="error">${error}<//>` : null}
      <div class="form-actions">
        <button type="submit" class="button button-primary" disabled=${!apiKey.trim() || saving} aria-busy=${saving ? "true" : undefined}>
          ${saving ? html`<span class="spinner" aria-hidden="true"></span>` : null}
          ${saving ? "Checking the key…" : "Save and check key"}
        </button>
        ${onCancel ? html`<${Button} variant="ghost" onClick=${onCancel} disabled=${saving}>Cancel<//>` : null}
      </div>
    </form>
  `;
};
