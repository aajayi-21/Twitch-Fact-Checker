/**
 * AI provider, for the browser engine: add or remove API keys kept in this
 * browser, choose which provider does what, and set spending limits.
 *
 * One key is enough. Each key is checked with its provider (a free call)
 * before it is stored, and is never shown again — only its last four
 * characters.
 */

import {STAGES, modelFor} from "../../engine/engine_setup.mjs";
import {DEFAULT_BUDGETS} from "../../engine/pipeline/budget.mjs";
import {
  Button,
  Card,
  ConfirmButton,
  ExternalLink,
  Notice,
  SettingRow,
  StatusPill,
} from "../components.mjs";
import {
  chooseStageProvider,
  connectProvider,
  disconnectProvider,
  setBudgets,
  setStageModel,
  useEngine,
} from "../lib/engine_actions.mjs";
import {BROWSER_PROVIDERS, browserProvider, describeProviderError} from "../lib/engine_model.mjs";
import {html, useEffect, useState} from "../lib/html.mjs";
import {EngineCard} from "./engine_card.mjs";
import {KeyForm} from "./key_form.mjs";

const STAGE_LABELS = Object.freeze({gate: "Finding claims", verify: "Checking claims"});

/** What happens to a key typed into the form, for the browser engine. */
export const browserKeyHelp = (provider) =>
  `Checked with ${provider.label}, then kept in this browser on this device only. It is sent nowhere else, and you can remove it at any time.`;

/** One provider as a row: what it is, how it stands, what you can do. */
const ProviderRow = ({provider, hint, inUse}) => {
  const [editing, setEditing] = useState(false);
  const [justSaved, setJustSaved] = useState(false);
  const connected = Boolean(hint);

  return html`
    <li class="provider">
      <div class="provider-head">
        <div class="provider-name">
          <h2 class="card-title">${provider.label}</h2>
          ${inUse ? html`<span class="in-use">In use</span>` : null}
        </div>
        <p class="muted">${provider.blurb}</p>
      </div>
      <div class="provider-status">
        <${StatusPill} flat state=${connected ? "ok" : "idle"}>
          ${connected ? "Connected" : "Not connected"}
        <//>
        <span class="provider-key">${connected ? `Key ending ${hint}` : "No key added"}</span>
      </div>
      ${editing
        ? null
        : html`<div class="form-actions provider-actions">
            <${Button}
              variant=${connected ? "secondary" : "primary"}
              onClick=${() => {
                setJustSaved(false);
                setEditing(true);
              }}
            >
              ${connected ? "Replace key" : "Add a key"}
            <//>
            ${connected
              ? html`<${ConfirmButton}
                  confirmLabel="Remove this key?"
                  onConfirm=${() => disconnectProvider(provider.id)}
                  aria-label=${`Remove the ${provider.label} key`}
                >
                  Remove key
                <//>`
              : html`<${ExternalLink} href=${provider.keyUrl}>Get a key<//>`}
          </div>`}
      ${provider.verifiedLive && !justSaved && !editing
        ? null
        : html`<div class="provider-extra">
      ${provider.verifiedLive
        ? null
        : html`<${Notice} tone="info">
            Not yet tested against ${provider.label}'s live service from this extension. It
            follows their published API; if it fails, the message below will say why.
          <//>`}
      ${justSaved ? html`<${Notice} tone="success">Key checked and saved.<//>` : null}
      ${editing
        ? html`<${KeyForm}
            provider=${provider}
            save=${connectProvider}
            describeError=${describeProviderError}
            help=${browserKeyHelp(provider)}
            onSaved=${() => {
              setEditing(false);
              setJustSaved(true);
            }}
            onCancel=${() => setEditing(false)}
          />`
        : null}
          </div>`}
    </li>
  `;
};

/** A model field that saves when you leave it; empty means the default. */
const ModelField = ({providerId, stage, state}) => {
  const stored = state.models[providerId]?.[stage] ?? "";
  const [value, setValue] = useState(stored);
  useEffect(() => setValue(stored), [stored]);
  const id = `${stage}-model`;
  return html`
    <input
      id=${id}
      type="text"
      spellcheck=${false}
      autocomplete="off"
      value=${value}
      placeholder=${browserProvider(providerId).defaultModels[stage]}
      aria-label=${`${STAGE_LABELS[stage]} model`}
      onInput=${(event) => setValue(event.currentTarget.value)}
      onBlur=${(event) => {
        const next = event.currentTarget.value.trim();
        if (next !== stored) {
          setStageModel(providerId, stage, next);
        }
      }}
      onKeyDown=${(event) => event.key === "Enter" && event.currentTarget.blur()}
    />
  `;
};

const StageRouting = ({engine}) => {
  const connected = BROWSER_PROVIDERS.filter((provider) => engine.hints[provider.id]);
  if (connected.length === 0) {
    return null;
  }
  return html`
    <details class="advanced">
      <summary>Advanced: providers and models per step</summary>
      <p class="setting-help">
        Finding claims is a small, frequent call; checking one searches the web and costs about
        a cent. With two keys you can give each step to a different provider. Leave a model
        empty to use the provider's default.
      </p>
      ${STAGES.map((stage) => {
        const active = engine.plan[stage];
        return html`
          <${SettingRow} key=${stage} label=${STAGE_LABELS[stage]} htmlFor=${`${stage}-provider`} stacked>
            <div class="filter-row">
              <select
                id=${`${stage}-provider`}
                value=${active?.providerId ?? ""}
                onChange=${(event) => chooseStageProvider(stage, event.currentTarget.value)}
              >
                ${connected.map(
                  (provider) =>
                    html`<option key=${provider.id} value=${provider.id}>${provider.label}</option>`
                )}
              </select>
              ${active
                ? html`<${ModelField}
                    key=${`${stage}-${active.providerId}`}
                    providerId=${active.providerId}
                    stage=${stage}
                    state=${engine.state}
                  />`
                : null}
            </div>
            ${active
              ? html`<p class="setting-help">
                  Using <code>${modelFor(engine.state, active.providerId, stage)}</code>.
                </p>`
              : null}
          <//>
        `;
      })}
    </details>
  `;
};

/** A number field that saves when you leave it. */
const LimitField = ({id, value, step, onCommit}) => {
  const [draft, setDraft] = useState(String(value));
  useEffect(() => setDraft(String(value)), [value]);
  return html`
    <input
      id=${id}
      type="number"
      class="input-number"
      min="0"
      step=${step}
      value=${draft}
      onInput=${(event) => setDraft(event.currentTarget.value)}
      onBlur=${(event) => {
        const next = Number(event.currentTarget.value);
        if (Number.isFinite(next) && next >= 0 && next !== value) {
          onCommit(next);
        } else {
          setDraft(String(value));
        }
      }}
      onKeyDown=${(event) => event.key === "Enter" && event.currentTarget.blur()}
    />
  `;
};

export const BudgetCard = ({engine}) => {
  const {budgets} = engine.state;
  return html`
    <${Card} title="Spending limits">
      <p class="setting-help">
        You pay your provider for every call, and a stream can run for hours. When either limit
        is reached, checking pauses and says so over the stream; nothing more is sent until the
        day or month rolls over or you raise the limit. You are told at 80%. Use 0 for no limit.
      </p>
      <${SettingRow}
        label="Fact-checks per day"
        help=${`A check costs about a cent. Default: ${DEFAULT_BUDGETS.dailyChecks}.`}
        htmlFor="budget-daily"
      >
        <${LimitField}
          id="budget-daily"
          value=${budgets.dailyChecks}
          step="10"
          onCommit=${(dailyChecks) => setBudgets({dailyChecks})}
        />
      <//>
      <${SettingRow}
        label="Dollars per month"
        help=${`Counts what this app can price: exact costs from OpenRouter, estimates for Anthropic. Default: $${DEFAULT_BUDGETS.monthlyUsd}.`}
        htmlFor="budget-monthly"
      >
        <${LimitField}
          id="budget-monthly"
          value=${budgets.monthlyUsd}
          step="1"
          onCommit=${(monthlyUsd) => setBudgets({monthlyUsd})}
        />
      <//>
    <//>
  `;
};

export const BrowserProvidersView = () => {
  const engine = useEngine();
  return html`
    <div class="page">
      <header class="page-header">
        <h1 class="page-title">AI provider</h1>
        <p class="page-subtitle">
          ${engine.plan.summary ?? "Add one API key to start. The others are optional."}
        </p>
      </header>

      ${engine.plan.ready
        ? null
        : html`<${Notice} tone="warn">
            No provider is connected yet, so nothing can be checked. Add a key below.
          <//>`}

      <ul class="card provider-list">
        ${BROWSER_PROVIDERS.map(
          (provider) => html`
            <${ProviderRow}
              key=${provider.id}
              provider=${provider}
              hint=${engine.hints[provider.id]}
              inUse=${engine.plan.ready &&
              [engine.plan.gate?.providerId, engine.plan.verify?.providerId].includes(provider.id)}
            />
          `
        )}
      </ul>

      <${StageRouting} engine=${engine} />
      <${BudgetCard} engine=${engine} />
      <${EngineCard} />
    </div>
  `;
};
