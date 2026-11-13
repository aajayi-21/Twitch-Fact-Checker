/**
 * Home: is everything ready, what has it done today, and what to do next.
 *
 * The status cards describe the engine (the local backend and its AI
 * provider). Everything below them — today's numbers, the latest results,
 * the all-time split — is read from the viewer's own history.
 */

import {
  PERIODS,
  countLabels,
  filterVerdicts,
  periodRange,
  summarizeUsage,
} from "../../ledger/queries.mjs";
import {LABELS} from "../../ledger/records.mjs";
import {Button, Card, LabelChip, Notice, StatusPill} from "../components.mjs";
import {refreshBackend, useAppState} from "../lib/app_store.mjs";
import {formatCost, formatDateTime, labelName, platformLabel, plural} from "../lib/format.mjs";
import {html, useMemo} from "../lib/html.mjs";
import {useLedger} from "../lib/ledger_store.mjs";
import {ROUTES} from "../lib/routes.mjs";
import {describeStages} from "../lib/setup_model.mjs";
import {ImportHistory} from "./import_history.mjs";

const LATEST_RESULTS = 5;

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
  const total = LABELS.reduce((sum, label) => sum + (labels?.[label] ?? 0), 0);
  if (total === 0) {
    return html`<p class="muted">
      No results yet. They will appear here once you have checked a stream.
    </p>`;
  }
  return html`
    <ul class="label-bars">
      ${LABELS.map((label) => {
        const count = labels?.[label] ?? 0;
        return html`
          <li key=${label} class="label-bar" data-label=${label}>
            <span class="label-bar-name">${labelName(label)}</span>
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

/** The newest few results. */
const LatestResults = ({verdicts}) => html`
  <ul class="latest-list">
    ${verdicts.map(
      (verdict) => html`
        <li key=${verdict.id} class="latest" data-label=${verdict.label}>
          <${LabelChip} label=${verdict.label} />
          <span class="latest-claim">${verdict.claim_text}</span>
          <span class="result-meta">
            ${verdict.channel_id
              ? `${verdict.channel_name ?? verdict.channel_id} · ${platformLabel(verdict.platform)}`
              : platformLabel(verdict.platform)}
            ${" · "}${formatDateTime(verdict.checked_at)}
          </span>
        </li>
      `
    )}
  </ul>
`;

export const HomeView = ({navigate}) => {
  const {backendState, backendMessage, health, setup, summary, settings} = useAppState();
  const ledger = useLedger();
  const up = backendState === "up";
  const configured = Boolean(setup?.configured);
  const speech = describeSpeech(health);

  const today = useMemo(() => {
    const range = periodRange(PERIODS.TODAY);
    return {
      ...summarizeUsage(ledger, range),
      claims: ledger.claims.filter((claim) => Date.parse(claim.gated_at) >= range.fromMs).length,
    };
  }, [ledger]);
  const allTime = useMemo(() => summarizeUsage(ledger), [ledger]);
  const latest = useMemo(
    () => filterVerdicts(ledger.verdicts).slice(0, LATEST_RESULTS),
    [ledger.verdicts]
  );
  // Results from before this app kept its own history are still in the backend.
  const backendSessions = summary?.totals?.sessions ?? 0;
  const canImport = ledger.loaded && ledger.sessions.length === 0 && up && backendSessions > 0;

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

      ${canImport
        ? html`<${Card} title="Bring your past results across">
            <p class="lede">
              The local backend holds ${plural(backendSessions, "earlier session")}. Import them to
              see them under History and Usage.
            </p>
            <${ImportHistory} compact />
          <//>`
        : null}

      <${Card} title="Today">
        <div class="stat-row">
          <${Stat} value=${today.checks} label="fact-checks" />
          <${Stat} value=${today.claims} label="claims heard" />
          <${Stat} value=${today.sessions} label="sessions" />
          <${Stat} value=${formatCost(today.total.cost)} label="spend" />
        </div>
      <//>

      <${Card}
        title="Latest results"
        aside=${latest.length > 0
          ? html`<button type="button" class="link-button" onClick=${() => navigate(ROUTES.HISTORY)}>
              See all ${ledger.verdicts.length}
            </button>`
          : null}
      >
        ${latest.length > 0
          ? html`<${LatestResults} verdicts=${latest} />`
          : html`<p class="muted">
              ${ledger.loaded
                ? "Nothing checked yet. Results appear here as a stream is checked."
                : "Loading…"}
            </p>`}
      <//>

      <${Card}
        title="All time"
        aside=${allTime.checks > 0
          ? html`<button type="button" class="link-button" onClick=${() => navigate(ROUTES.USAGE)}>
              ${plural(allTime.checks, "check")} · ${formatCost(allTime.total.cost)}
            </button>`
          : null}
      >
        <${LabelBreakdown} labels=${countLabels(ledger.verdicts)} />
      <//>
    </div>
  `;
};
