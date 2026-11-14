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
import {SPEECH_ENGINE, describeSpeechState} from "../../engine/stt/speech_setup.mjs";
import {LABELS} from "../../ledger/records.mjs";
import {Card, LabelChip, Notice, StatusPill} from "../components.mjs";
import {refreshBackend, useAppState} from "../lib/app_store.mjs";
import {ENGINE_MODE, useEngine} from "../lib/engine_actions.mjs";
import {formatCost, formatDateTime, labelName, platformLabel, plural} from "../lib/format.mjs";
import {html, useMemo} from "../lib/html.mjs";
import {useLedger} from "../lib/ledger_store.mjs";
import {useSpeech} from "../lib/speech_actions.mjs";
import {ROUTES} from "../lib/routes.mjs";
import {describeStages} from "../lib/setup_model.mjs";
import {ImportHistory} from "./import_history.mjs";

const LATEST_RESULTS = 5;

/** What a speech status dot means, for readers who cannot see its colour. */
const SPEECH_STATE_WORDS = Object.freeze({
  ok: "Ready",
  warn: "Needs attention",
  bad: "Not working",
  busy: "Working",
  idle: "Idle",
});

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

/**
 * Every verdict as one bar split by label, with the counts listed under it.
 * The legend names each label, so the colour never has to carry it alone.
 */
const LabelBreakdown = ({labels}) => {
  const total = LABELS.reduce((sum, label) => sum + (labels?.[label] ?? 0), 0);
  if (total === 0) {
    return html`<p class="muted">
      No results yet. They will appear here once you have checked a stream.
    </p>`;
  }
  const counted = LABELS.map((label) => ({label, count: labels?.[label] ?? 0}));
  return html`
    <div
      class="label-split"
      role="img"
      aria-label=${`${plural(total, "check")}: ${counted
        .map(({label, count}) => `${count} ${labelName(label).toLowerCase()}`)
        .join(", ")}`}
    >
      ${counted
        .filter(({count}) => count > 0)
        .map(
          ({label, count}) => html`
            <span key=${label} class="label-split-part" data-label=${label} style=${`flex: ${count} 1 0`}></span>
          `
        )}
    </div>
    <ul class="label-legend">
      ${counted.map(
        ({label, count}) => html`
          <li key=${label} class="label-legend-row" data-label=${label}>
            <span class="label-legend-swatch" aria-hidden="true"></span>
            <span>${labelName(label)}</span>
            <span>${count}</span>
            <span class="label-legend-share">${Math.round((count / total) * 100)}%</span>
          </li>
        `
      )}
    </ul>
  `;
};

/** One line of the Setup card: what it is, and how it is doing. */
const SetupRow = ({name, status, children}) => html`
  <li class="fact-row">
    <span class="fact-name">${name}</span>
    ${status}
    <div class="fact-value">${children}</div>
  </li>
`;

const TODAY_FORMAT = new Intl.DateTimeFormat(undefined, {weekday: "short", month: "short", day: "numeric"});

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
  const browserSpeech = useSpeech();
  const engine = useEngine();
  // With the browser engine the backend plays no part: its state is neither
  // shown nor waited for.
  const browserEngine = engine.state.mode === ENGINE_MODE.BROWSER;
  const up = backendState === "up";
  const configured = browserEngine ? engine.plan.ready : Boolean(setup?.configured);
  // Speech is the backend's job unless it runs in the browser — by choice,
  // or because the browser engine has no backend to hand audio to.
  const speechInBrowser =
    browserEngine || browserSpeech.state.settings.engine === SPEECH_ENGINE.BROWSER;
  const speech = !browserSpeech.loaded
    ? null
    : speechInBrowser
      ? describeSpeechState(
          {
            ...browserSpeech.state,
            settings: {...browserSpeech.state.settings, engine: SPEECH_ENGINE.BROWSER},
          },
          browserSpeech.plan
        )
      : describeSpeech(health);

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

      ${up || browserEngine
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
      ${(up || browserEngine) && engine.loaded && !configured
        ? html`<${Notice} tone="warn">
            No AI provider is connected yet, so nothing can be checked.${" "}
            <button type="button" class="link-button" onClick=${() => navigate(ROUTES.PROVIDERS)}>
              Add an API key
            </button>
          <//>`
        : null}

      ${canImport
        ? html`<${Card} title="Bring your past results across">
            <p class="lede">
              The local backend holds ${plural(backendSessions, "earlier session")}. Import them to
              see them under History and Usage.
            </p>
            <${ImportHistory} compact />
          <//>`
        : null}

      <section class="card stat-card" aria-label="Today">
        <header class="stat-card-header">
          <h2 class="eyebrow">Today</h2>
          <span>${TODAY_FORMAT.format(new Date())}</span>
        </header>
        <div class="stat-row">
          <${Stat} value=${today.checks} label="fact-checks" />
          <${Stat} value=${today.claims} label="claims heard" />
          <${Stat} value=${today.sessions} label=${today.sessions === 1 ? "session" : "sessions"} />
          <${Stat} value=${formatCost(today.total.cost)} label="spend" />
        </div>
      </section>

      <div class="split">
        <div class="split-main">
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
        </div>

        <div class="split-side">
          <${Card}
            title="Setup"
            aside=${html`<button type="button" class="link-button" onClick=${() => navigate(ROUTES.PROVIDERS)}>
              ${configured ? "Manage" : "Add a key"}
            </button>`}
          >
            <ul class="fact-rows">
              <${SetupRow}
                name="Runs on"
                status=${browserEngine
                  ? html`<${StatusPill} flat state="ok">Built in<//>`
                  : html`<${StatusPill} flat state=${up ? "ok" : backendState === "checking" ? "busy" : "bad"}>
                      ${up ? "Running" : backendState === "checking" ? "Checking…" : "Not running"}
                    <//>`}
              >
                ${browserEngine ? "This browser" : "Local backend"}
                <div class="fact-sub">
                  ${browserEngine
                    ? "Speech recognition and checking run inside this extension. Nothing else to start."
                    : up
                      ? settings?.backendUrl
                      : "Speech recognition and checking run here."}
                </div>
              <//>
              ${speech
                ? html`<${SetupRow}
                    name="Speech recognition"
                    status=${html`<${StatusPill} flat state=${speech.state}>
                      <span class="visually-hidden">${SPEECH_STATE_WORDS[speech.state] ?? ""}</span>
                    <//>`}
                  >
                    ${speech.text}
                    ${speech.state === "warn" && speechInBrowser
                      ? html`${" "}<button
                            type="button"
                            class="link-button"
                            onClick=${() => navigate(ROUTES.SETTINGS)}
                          >
                            Set up
                          </button>`
                      : null}
                  <//>`
                : null}
              <${SetupRow}
                name="AI provider"
                status=${html`<${StatusPill}
                  flat
                  state=${configured ? "ok" : up || browserEngine ? "warn" : "idle"}
                >
                  ${configured ? "Connected" : up || browserEngine ? "Not connected" : "Unknown"}
                <//>`}
              >
                ${(browserEngine ? engine.plan.summary : describeStages(setup)) ??
                "Add an API key to start checking claims."}
              <//>
            </ul>
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
      </div>
    </div>
  `;
};
