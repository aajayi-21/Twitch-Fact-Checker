/**
 * AI provider: add or replace an API key, and (for people who want it)
 * choose which provider and model run each stage.
 *
 * The main path is one key. Everything the old settings page could do —
 * separate providers per stage, model overrides, a local Ollama gate, the
 * Jev pre-screen, an existing `ant auth login` sign-in — is kept under
 * "Advanced", because those are features of the local backend.
 */

import {Button, Card, ExternalLink, Notice, SettingRow, StatusPill} from "../components.mjs";
import {BACKEND_ERROR, applyStages, saveProviderKey, useAppState} from "../lib/app_store.mjs";
import {html, useEffect, useState} from "../lib/html.mjs";
import {
  KEY_PROVIDERS,
  PROVIDER_LABELS,
  describeConnection,
  describeStages,
  isStageFormDirty,
  stageFormFields,
  stageFormFromStatus,
  stageFormProblem,
  stageRequestFromForm,
  withStageProvider,
} from "../lib/setup_model.mjs";
import {ENGINE_MODE, useEngine} from "../lib/engine_actions.mjs";
import {EngineCard} from "./engine_card.mjs";
import {KeyForm} from "./key_form.mjs";
import {BrowserProvidersView} from "./providers_browser.mjs";

const GATE_PROVIDERS = Object.freeze(["anthropic", "openrouter", "ollama"]);
// Local verification has no web search, so every verdict would be UNVERIFIED.
const VERIFY_PROVIDERS = Object.freeze(["anthropic", "openrouter"]);

const MODEL_PLACEHOLDERS = Object.freeze({
  anthropic: "e.g. claude-haiku-5-5",
  openrouter: "e.g. google/gemini-3.5-flash-lite",
});

const JEV_MODES = Object.freeze([
  {value: "off", label: "Off"},
  {value: "shadow", label: "Shadow (measure only)"},
  {value: "screen", label: "Screen (skip unlikely batches)"},
]);

const ProviderCard = ({provider, setup}) => {
  const [editing, setEditing] = useState(false);
  const [justSaved, setJustSaved] = useState(false);
  const connection = describeConnection(setup, provider.id);
  const connected = connection.state === "connected";

  return html`
    <${Card}
      title=${provider.label}
      aside=${html`<${StatusPill} state=${connected ? "ok" : "idle"}>
        ${connected ? "Connected" : "Not connected"}
      <//>`}
    >
      <p class="card-line">${provider.blurb}</p>
      <p class="card-line muted">${connection.detail}</p>
      ${justSaved ? html`<${Notice} tone="success">Key checked and saved.<//>` : null}
      ${editing
        ? html`<${KeyForm}
            provider=${provider}
            onSaved=${() => {
              setEditing(false);
              setJustSaved(true);
            }}
            onCancel=${() => setEditing(false)}
          />`
        : html`<div class="form-actions">
            <${Button}
              variant=${connected ? "secondary" : "primary"}
              onClick=${() => {
                setJustSaved(false);
                setEditing(true);
              }}
            >
              ${connection.method === "api_key" ? "Replace key" : "Add a key"}
            <//>
          </div>`}
    <//>
  `;
};

/** End backend-supplied text with a full stop, so more can follow it. */
const asSentence = (text) => (/[.!?]$/.test(text.trim()) ? text.trim() : `${text.trim()}.`);

const describeStageError = (error) => {
  switch (error?.kind) {
    case BACKEND_ERROR.NOT_READY:
      return `${asSentence(error.message)} Connect that provider first.`;
    case BACKEND_ERROR.UNREACHABLE:
      return "The local backend is not running, so nothing was changed.";
    case BACKEND_ERROR.PROVIDER_DOWN:
      return "The backend could not reach the provider to check that model. Nothing was changed.";
    default:
      return error?.message || "The change could not be applied.";
  }
};

const StageRouting = ({setup}) => {
  const [form, setForm] = useState(() => stageFormFromStatus(setup));
  const [applying, setApplying] = useState(false);
  const [message, setMessage] = useState(null);

  // Follow the backend when it changes underneath (a key was just saved).
  useEffect(() => {
    setForm(stageFormFromStatus(setup));
  }, [setup?.gate?.provider, setup?.verify?.provider, setup?.gate?.model, setup?.verify?.model]);

  const fields = stageFormFields(form);
  const problem = stageFormProblem(form);
  const dirty = isStageFormDirty(form, setup);

  const apply = async () => {
    setApplying(true);
    setMessage(null);
    try {
      await applyStages(stageRequestFromForm(form));
      setMessage({tone: "success", text: "Applied. New sessions use this routing."});
    } catch (error) {
      setMessage({tone: "error", text: describeStageError(error)});
    } finally {
      setApplying(false);
    }
  };

  const providerSelect = (stage, providers) => html`
    <select
      id=${`${stage}-provider`}
      value=${form[`${stage}Provider`]}
      onChange=${(event) => setForm(withStageProvider(form, stage, event.currentTarget.value, setup))}
    >
      ${providers.map(
        (providerId) => html`<option key=${providerId} value=${providerId}>${PROVIDER_LABELS[providerId]}</option>`
      )}
    </select>
  `;

  const modelInput = (stage) => html`
    <input
      id=${`${stage}-model`}
      type="text"
      spellcheck=${false}
      autocomplete="off"
      value=${form[`${stage}Model`]}
      placeholder=${MODEL_PLACEHOLDERS[form[`${stage}Provider`]] ?? ""}
      onInput=${(event) => setForm({...form, [`${stage}Model`]: event.currentTarget.value})}
    />
  `;

  const ollama = describeConnection(setup, "ollama");
  const anthropic = describeConnection(setup, "anthropic");

  // Sending Anthropic an EMPTY key tells the backend to drop any stored key
  // and use this computer's `ant auth login` sign-in instead.
  const useSignIn = async () => {
    setApplying(true);
    setMessage(null);
    try {
      await saveProviderKey("anthropic", "");
      setMessage({tone: "success", text: "Now using the Anthropic sign-in on this computer."});
    } catch (error) {
      setMessage({
        tone: "error",
        text:
          error?.kind === BACKEND_ERROR.KEY_REJECTED
            ? "No usable Anthropic sign-in was found on this computer. Run `ant auth login` in a terminal, then try again."
            : describeStageError(error),
      });
    } finally {
      setApplying(false);
    }
  };

  return html`
    <details class="advanced">
      <summary>Advanced: providers and models per stage</summary>
      <p class="field-help">
        Finding claims ("the gate") runs a few hundred cheap calls an hour; checking one needs
        web search. They can use different providers. Models are verified with the provider
        when you apply: Anthropic ids against its model list, OpenRouter slugs against its${" "}
        <${ExternalLink} href="https://openrouter.ai/models">catalogue<//>.
      </p>
      <${SettingRow} label="Finding claims" htmlFor="gate-provider">${providerSelect("gate", GATE_PROVIDERS)}<//>
      ${fields.gateModel
        ? html`<${SettingRow} label="Model for finding claims" htmlFor="gate-model">${modelInput("gate")}<//>`
        : null}
      ${fields.jevMode
        ? html`<${SettingRow}
            label="Jev pre-screen"
            htmlFor="jev-mode"
            help="A small model that decides whether a batch is worth sending to the gate at all."
          >
            <select
              id="jev-mode"
              value=${form.jevMode}
              onChange=${(event) => setForm({...form, jevMode: event.currentTarget.value})}
            >
              ${JEV_MODES.map((mode) => html`<option key=${mode.value} value=${mode.value}>${mode.label}</option>`)}
            </select>
          <//>`
        : null}
      <${SettingRow} label="Checking claims" htmlFor="verify-provider">
        ${providerSelect("verify", VERIFY_PROVIDERS)}
      <//>
      ${fields.verifyModel
        ? html`<${SettingRow} label="Model for checking claims" htmlFor="verify-model">${modelInput("verify")}<//>`
        : null}
      ${problem ? html`<${Notice} tone="error">${problem}<//>` : null}
      ${message ? html`<${Notice} tone=${message.tone}>${message.text}<//>` : null}
      <div class="form-actions">
        <${Button} variant="primary" onClick=${apply} busy=${applying} disabled=${!dirty || Boolean(problem)}>
          Apply
        <//>
      </div>
      <dl class="facts">
        <dt>Ollama (local gate)</dt>
        <dd>${ollama.detail}</dd>
        <dt>Anthropic sign-in</dt>
        <dd>
          ${anthropic.method === "sign_in"
            ? "In use. Adding a key above replaces it."
            : html`For people who ran <code>ant auth login</code> on this computer.${" "}
                <button type="button" class="link-button" onClick=${useSignIn} disabled=${applying}>
                  Use the sign-in instead of a key
                </button>`}
        </dd>
      </dl>
    </details>
  `;
};

/**
 * The AI provider screen for whichever engine is chosen: keys kept in this
 * browser (providers_browser.mjs), or keys held by the local backend (below).
 */
export const ProvidersView = () => {
  const engine = useEngine();
  return engine.state.mode === ENGINE_MODE.BROWSER
    ? html`<${BrowserProvidersView} />`
    : html`<${BackendProvidersView} />`;
};

const BackendProvidersView = () => {
  const {backendState, setup} = useAppState();
  if (backendState !== "up" || !setup) {
    return html`
      <div class="page">
        <header class="page-header">
          <h1 class="page-title">AI provider</h1>
        </header>
        <${Notice} tone=${backendState === "checking" ? "info" : "warn"}>
          ${backendState === "checking"
            ? "Looking for the local backend…"
            : html`Keys are stored by the local backend, which is not running. Start it with${" "}
                <code>./backend/run.sh</code> and this page will update.`}
        <//>
        <${EngineCard} />
      </div>
    `;
  }
  const summary = describeStages(setup);
  return html`
    <div class="page">
      <header class="page-header">
        <h1 class="page-title">AI provider</h1>
        <p class="page-subtitle">
          ${summary ? `In use: ${summary}.` : "Add one API key to start fact-checking."}
        </p>
      </header>
      <div class="card-grid">
        ${KEY_PROVIDERS.map(
          (provider) => html`<${ProviderCard} key=${provider.id} provider=${provider} setup=${setup} />`
        )}
      </div>
      <p class="muted">
        OpenAI and Gemini keys work with the built-in engine — choose “This browser” below.
      </p>
      <${StageRouting} setup=${setup} />
      <${EngineCard} />
    </div>
  `;
};
