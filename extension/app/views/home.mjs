/**
 * Home: is everything ready, what has it done today, and what to do next.
 */

import {Button, Card, ExternalLink, Notice, StatusPill} from "../components.mjs";
import {deriveBackendHttpOrigin} from "../../shared/settings.js";
import {refreshBackend, useAppState} from "../lib/app_store.mjs";
import {html} from "../lib/html.mjs";
import {ROUTES} from "../lib/routes.mjs";
import {describeStages} from "../lib/setup_model.mjs";

const LABEL_ORDER = Object.freeze(["TRUE", "FALSE", "MISLEADING", "UNVERIFIED"]);

const formatMoney = (amount) =>
  Number.isFinite(amount) ? `$${amount < 1 ? amount.toFixed(3) : amount.toFixed(2)}` : "—";

/** How the speech engine is doing, from /healthz. */
const describeSpeech = (health) => {
  if (!health) {
    return null;
  }
  const model = health.whisper_model ?? "speech model";
  if (health.status === "ok") {
    return {state: "ok", text: `Ready · ${model}`};
  }
  if (health.status === "degraded") {
    return {state: "warn", text: `Running on the CPU · ${model}`};
  }
  return {state: "bad", text: "Speech engine needs a backend restart"};
};

const Stat = ({value, label}) => html`
  <div class="stat">
    <span class="stat-value">${value}</span>
    <span class="stat-label">${label}</span>
  </div>
`;

const LabelBreakdown = ({labels}) => {
  const total = LABEL_ORDER.reduce((sum, label) => sum + (labels?.[label] ?? 0), 0);
  if (total === 0) {
    return html`<p class="muted">No verdicts yet. They will appear here once you have checked a stream.</p>`;
  }
  return html`
    <ul class="label-bars">
      ${LABEL_ORDER.map((label) => {
        const count = labels?.[label] ?? 0;
        return html`
          <li key=${label} class="label-bar" data-label=${label}>
            <span class="label-bar-name">${label[0] + label.slice(1).toLowerCase()}</span>
            <span class="label-bar-track"
              ><span class="label-bar-fill" style=${`width: ${(count / total) * 100}%`}></span
            ></span>
            <span class="label-bar-count">${count}</span>
          </li>
        `;
      })}
    </ul>
  `;
};

export const HomeView = ({navigate}) => {
  const {backendState, backendMessage, health, setup, summary, settings} = useAppState();
  const up = backendState === "up";
  const configured = Boolean(setup?.configured);
  const speech = describeSpeech(health);
  const origin = settings ? deriveBackendHttpOrigin(settings.backendUrl) : null;
  const today = summary?.today;
  const totals = summary?.totals;

  return html`
    <div class="page">
      <header class="page-header">
        <h1 class="page-title">Home</h1>
        <p class="page-subtitle">Open a stream, click the toolbar icon, and press Start.</p>
      </header>

      ${up
        ? null
        : html`<${Notice} tone=${backendState === "checking" ? "info" : "warn"}>
            ${backendState === "checking"
              ? "Looking for the local backend…"
              : html`${backendMessage ?? "The local backend is not running."} Start it with${" "}
                  <code>./backend/run.sh</code> in the project folder.${" "}
                  <button type="button" class="link-button" onClick=${() => refreshBackend()}>
                    Check again
                  </button>`}
          <//>`}
      ${up && !configured
        ? html`<${Notice} tone="warn">
            No AI provider is connected yet, so nothing can be checked.${" "}
            <button type="button" class="link-button" onClick=${() => navigate(ROUTES.PROVIDERS)}>
              Add an API key
            </button>
          <//>`
        : null}

      <div class="card-grid">
        <${Card}
          title="Local backend"
          aside=${html`<${StatusPill} state=${up ? "ok" : backendState === "checking" ? "busy" : "bad"}>
            ${up ? "Running" : backendState === "checking" ? "Checking…" : "Not running"}
          <//>`}
        >
          <p class="card-line">${up ? settings?.backendUrl : "Speech recognition and checking run here."}</p>
          ${speech
            ? html`<p class="card-line"><${StatusPill} state=${speech.state}>${speech.text}<//></p>`
            : null}
        <//>
        <${Card}
          title="AI provider"
          aside=${html`<${StatusPill} state=${configured ? "ok" : up ? "warn" : "idle"}>
            ${configured ? "Connected" : up ? "Not connected" : "Unknown"}
          <//>`}
        >
          <p class="card-line">${describeStages(setup) ?? "Add an API key to start checking claims."}</p>
          <div class="form-actions">
            <${Button} onClick=${() => navigate(ROUTES.PROVIDERS)}>
              ${configured ? "Manage" : "Add a key"}
            <//>
          </div>
        <//>
      </div>

      <${Card} title="Today">
        <div class="stat-row">
          <${Stat} value=${health?.checks_today ?? today?.verify_calls ?? "—"} label="fact-checks" />
          <${Stat} value=${today?.claims ?? "—"} label="claims heard" />
          <${Stat} value=${today?.sessions ?? "—"} label="sessions" />
          <${Stat}
            value=${`≈ ${formatMoney(health?.est_cost_today_usd ?? today?.est_cost_usd)}`}
            label="estimated spend"
          />
        </div>
      <//>

      <${Card}
        title="All time"
        aside=${totals
          ? html`<span class="muted">${totals.verify_calls} checks · ≈ ${formatMoney(totals.est_cost_usd)}</span>`
          : null}
      >
        <${LabelBreakdown} labels=${totals?.labels} />
        ${up && origin
          ? html`<p class="card-line">
              <${ExternalLink} href=${`${origin}/dashboard`}>Open the full analytics page<//>
              <span class="muted"> — per-channel results and past sessions.</span>
            </p>`
          : null}
      <//>
    </div>
  `;
};
