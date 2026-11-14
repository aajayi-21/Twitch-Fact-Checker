/**
 * History: every past result, with the controls to find, judge, export and
 * delete them.
 *
 * Three views of the same ledger:
 *   Results   every verdict, newest first, filterable and searchable
 *   Channels  one row per channel on each site
 *   Sessions  one row per time the checker was running
 *
 * The channel table follows the honest-metrics rule (ledger/queries.mjs): a
 * channel's True / False / Misleading shares are shown only from 30
 * adjudicated checks, and always beside the sample size. It is deliberately
 * not a ranking — the default order is by watch time.
 */

import {TOPIC_LABELS, TOPIC_SLUGS} from "../../shared/settings.js";
import {
  MIN_ADJUDICATED_SAMPLE,
  PERIODS,
  channelOptions,
  filterVerdicts,
  periodRange,
  sortRows,
  summarizeChannels,
  summarizeSessions,
} from "../../ledger/queries.mjs";
import {LABELS, PLATFORMS, channelKey} from "../../ledger/records.mjs";
import {Button, Card, ConfirmButton, LabelChip, Notice, Tabs} from "../components.mjs";
import {backendApi, useAppState} from "../lib/app_store.mjs";
import {
  formatCost,
  formatDateTime,
  formatDuration,
  formatPercent,
  labelName,
  platformLabel,
  plural,
} from "../lib/format.mjs";
import {html, useMemo, useState} from "../lib/html.mjs";
import {
  exportJson,
  exportVerdictsCsv,
  rateVerdict,
  removeChannel,
  removeSession,
  removeVerdict,
  useLedger,
} from "../lib/ledger_store.mjs";
import {ImportHistory} from "./import_history.mjs";

const TAB = Object.freeze({RESULTS: "results", CHANNELS: "channels", SESSIONS: "sessions"});

const PAGE_SIZE = 100;
const CUSTOM_PERIOD = "custom";

const EMPTY_FILTERS = Object.freeze({
  search: "",
  platform: "",
  channel: "",
  topic: "",
  feedback: "",
  labels: Object.freeze([]),
  period: PERIODS.ALL,
  fromDate: "",
  toDate: "",
  sessionId: "",
});

const PERIOD_OPTIONS = Object.freeze([
  [PERIODS.ALL, "Any time"],
  [PERIODS.TODAY, "Today"],
  [PERIODS.WEEK, "Last 7 days"],
  [PERIODS.MONTH, "Last 30 days"],
  [CUSTOM_PERIOD, "Between two dates…"],
]);

/** Local midnight at the start of a "YYYY-MM-DD" date input's day. */
const dateInputMs = (value, dayOffset = 0) => {
  const [year, month, day] = String(value).split("-").map(Number);
  return year && month && day ? new Date(year, month - 1, day + dayOffset).getTime() : null;
};

/** The time window the date controls describe. */
const rangeOf = (filters) =>
  filters.period === CUSTOM_PERIOD
    ? {fromMs: dateInputMs(filters.fromDate), toMs: dateInputMs(filters.toDate, 1)}
    : periodRange(filters.period);

const isFiltered = (filters) =>
  Object.keys(EMPTY_FILTERS).some((key) =>
    key === "labels" ? filters.labels.length > 0 : filters[key] !== EMPTY_FILTERS[key]
  );

const verdictAsText = (verdict) =>
  [
    `${verdict.label}: ${verdict.claim_text}`,
    verdict.explanation,
    ...verdict.sources.map((source) => source.url),
  ]
    .filter(Boolean)
    .join("\n");

/* ------------------------------------------------------------------ */
/* Results                                                              */
/* ------------------------------------------------------------------ */

const VerdictDetail = ({verdict, feedback}) => {
  const [copied, setCopied] = useState(false);
  const up = useAppState((app) => app.backendState === "up");
  const api = up ? backendApi : null;
  const rating = feedback?.rating ?? null;

  const rate = (nextRating, correctedLabel = null) =>
    rateVerdict({verdictId: verdict.id, rating: nextRating, correctedLabel}, api);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(verdictAsText(verdict));
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (error) {
      console.warn("[fact-checker] copy failed:", error);
    }
  };

  return html`
    <div class="result-detail" id=${`detail-${verdict.id}`}>
      <div class="result-detail-main">
      <div class="result-body">
      <p class="result-explanation">${verdict.explanation || "No explanation was recorded."}</p>
      ${verdict.sources.length > 0
        ? html`<ul class="source-list">
            ${verdict.sources.map(
              (source) => html`
                <li key=${source.url}>
                  <a href=${source.url} target="_blank" rel="noopener noreferrer">
                    ${source.title ?? source.domain ?? source.url}
                  </a>
                  ${source.title && source.domain
                    ? html`<span class="muted"> · ${source.domain}</span>`
                    : null}
                </li>
              `
            )}
          </ul>`
        : html`<p class="muted">No sources — which is why a result like this is left Unverified.</p>`}
      </div>
      <dl class="facts">
        ${verdict.stream_url
          ? html`<dt>Said in</dt>
              <dd>
                <a href=${verdict.stream_url} target="_blank" rel="noopener noreferrer">
                  ${verdict.stream_title ?? verdict.stream_url}
                </a>
              </dd>`
          : verdict.stream_title
            ? html`<dt>Said in</dt>
                <dd>${verdict.stream_title}</dd>`
            : null}
        <dt>Topic</dt>
        <dd>${TOPIC_LABELS[verdict.topic] ?? verdict.topic}</dd>
        ${verdict.model
          ? html`<dt>Checked by</dt>
              <dd>${verdict.provider} · ${verdict.model}</dd>`
          : null}
        ${Number.isFinite(verdict.latency_ms)
          ? html`<dt>Took</dt>
              <dd>${(verdict.latency_ms / 1000).toFixed(1)} s</dd>`
          : null}
        ${verdict.used_fallback
          ? html`<dt>Note</dt>
              <dd>The provider's structured answer failed; this came from its fallback path.</dd>`
          : null}
      </dl>
      </div>
      <div class="result-actions">
        <span class="rating" role="group" aria-label="Was this result right?">
          <button
            type="button"
            class="rating-button"
            aria-pressed=${rating === "up" ? "true" : "false"}
            onClick=${() => rate("up")}
          >
            <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12l5 5 9-10" /></svg>Right
          </button>
          <button
            type="button"
            class="rating-button"
            aria-pressed=${rating === "down" ? "true" : "false"}
            onClick=${() => rate("down", feedback?.corrected_label ?? null)}
          >
            <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" /></svg>Wrong
          </button>
        </span>
        ${rating === "down"
          ? html`<label class="inline-field">
              <span>It should have been</span>
              <select
                value=${feedback?.corrected_label ?? ""}
                onChange=${(event) => rate("down", event.currentTarget.value || null)}
              >
                <option value="">not sure</option>
                ${LABELS.filter((label) => label !== verdict.label).map(
                  (label) => html`<option key=${label} value=${label}>${labelName(label)}</option>`
                )}
              </select>
            </label>`
          : null}
        <span class="result-actions-end">
          <${Button} variant="ghost" onClick=${copy}>${copied ? "Copied" : "Copy"}<//>
          <${ConfirmButton}
            confirmLabel="Delete this result?"
            onConfirm=${() => removeVerdict(verdict.id)}
          >
            Delete
          <//>
        </span>
      </div>
    </div>
  `;
};

const VerdictRow = ({verdict, feedback, expanded, onToggle}) => html`
  <li class="result" data-label=${verdict.label} data-expanded=${expanded ? "true" : undefined}>
    <button
      type="button"
      class="result-summary"
      aria-expanded=${expanded ? "true" : "false"}
      aria-controls=${`detail-${verdict.id}`}
      onClick=${onToggle}
    >
      <${LabelChip} label=${verdict.label} />
      <span class="result-claim">${verdict.claim_text}</span>
      <span class="result-meta">
        ${verdict.channel_id
          ? `${verdict.channel_name ?? verdict.channel_id} · ${platformLabel(verdict.platform)}`
          : platformLabel(verdict.platform)}
        ${" · "}${formatDateTime(verdict.checked_at)}
        ${feedback ? (feedback.rating === "up" ? " · rated right" : " · rated wrong") : ""}
      </span>
      <svg
        class="result-chevron"
        viewBox="0 0 24 24"
        width="18"
        height="18"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <path d=${expanded ? "M6 15l6-6 6 6" : "M6 9l6 6 6-6"} />
      </svg>
    </button>
    ${expanded ? html`<${VerdictDetail} verdict=${verdict} feedback=${feedback} />` : null}
  </li>
`;

const ResultFilters = ({filters, setFilters, channels, sessionLabel}) => {
  const set = (patch) => setFilters({...filters, ...patch});
  const toggleLabel = (label) =>
    set({
      labels: filters.labels.includes(label)
        ? filters.labels.filter((kept) => kept !== label)
        : [...filters.labels, label],
    });
  // Narrowing to a site narrows the channel list to that site.
  const channelChoices = channels.filter(
    (option) => !filters.platform || option.platform === filters.platform
  );
  return html`
    <div class="filters">
      <input
        type="search"
        class="filter-search"
        placeholder="Search claims and explanations"
        aria-label="Search claims and explanations"
        value=${filters.search}
        onInput=${(event) => set({search: event.currentTarget.value})}
      />
      <div class="filter-row">
        <select
          aria-label="Site"
          value=${filters.platform}
          onChange=${(event) => set({platform: event.currentTarget.value, channel: ""})}
        >
          <option value="">All sites</option>
          ${PLATFORMS.map(
            (platform) =>
              html`<option key=${platform} value=${platform}>${platformLabel(platform)}</option>`
          )}
        </select>
        <select
          aria-label="Channel"
          value=${filters.channel}
          onChange=${(event) => set({channel: event.currentTarget.value})}
        >
          <option value="">All channels</option>
          ${channelChoices.map(
            (option) => html`
              <option key=${option.key} value=${option.key}>
                ${option.label} (${platformLabel(option.platform)})
              </option>
            `
          )}
        </select>
        <select
          aria-label="Topic"
          value=${filters.topic}
          onChange=${(event) => set({topic: event.currentTarget.value})}
        >
          <option value="">All topics</option>
          ${TOPIC_SLUGS.map(
            (slug) => html`<option key=${slug} value=${slug}>${TOPIC_LABELS[slug]}</option>`
          )}
        </select>
        <select
          aria-label="Your rating"
          value=${filters.feedback}
          onChange=${(event) => set({feedback: event.currentTarget.value})}
        >
          <option value="">Rated or not</option>
          <option value="up">Rated right</option>
          <option value="down">Rated wrong</option>
          <option value="unrated">Not rated</option>
        </select>
        <select
          aria-label="When"
          value=${filters.period}
          onChange=${(event) => set({period: event.currentTarget.value})}
        >
          ${PERIOD_OPTIONS.map(
            ([value, label]) => html`<option key=${value} value=${value}>${label}</option>`
          )}
        </select>
      </div>
      ${filters.period === CUSTOM_PERIOD
        ? html`<div class="filter-row filter-dates">
            <label class="inline-field">
              <span>From</span>
              <input
                type="date"
                value=${filters.fromDate}
                max=${filters.toDate || undefined}
                onChange=${(event) => set({fromDate: event.currentTarget.value})}
              />
            </label>
            <label class="inline-field">
              <span>To</span>
              <input
                type="date"
                value=${filters.toDate}
                min=${filters.fromDate || undefined}
                onChange=${(event) => set({toDate: event.currentTarget.value})}
              />
            </label>
          </div>`
        : null}
      <div class="filter-row" role="group" aria-label="Verdict">
        ${LABELS.map(
          (label) => html`
            <button
              key=${label}
              type="button"
              class="label-toggle"
              data-label=${label}
              aria-pressed=${filters.labels.includes(label) ? "true" : "false"}
              onClick=${() => toggleLabel(label)}
            >
              ${labelName(label)}
            </button>
          `
        )}
        ${sessionLabel
          ? html`<button
              type="button"
              class="filter-chip"
              onClick=${() => set({sessionId: ""})}
              aria-label=${`Stop filtering by session: ${sessionLabel}`}
            >
              Session: ${sessionLabel} <span aria-hidden="true">✕</span>
            </button>`
          : null}
      </div>
    </div>
  `;
};

const ResultsPanel = ({ledger, filters, setFilters}) => {
  const [expandedId, setExpandedId] = useState(null);
  const [shown, setShown] = useState(PAGE_SIZE);

  const channels = useMemo(() => channelOptions(ledger.verdicts), [ledger.verdicts]);
  const matching = useMemo(
    () => filterVerdicts(ledger.verdicts, {...filters, ...rangeOf(filters)}, ledger.feedback),
    [ledger.verdicts, ledger.feedback, filters]
  );
  const session = filters.sessionId
    ? ledger.sessions.find((candidate) => candidate.id === filters.sessionId)
    : null;
  const sessionLabel = filters.sessionId
    ? session
      ? `${session.channel_name ?? platformLabel(session.platform)}, ${formatDateTime(session.started_at)}`
      : "deleted session"
    : null;

  const applyFilters = (next) => {
    setFilters(next);
    setShown(PAGE_SIZE);
  };

  return html`
    <div role="tabpanel" id="panel-results" aria-labelledby="tab-results" class="panel">
      <${ResultFilters}
        filters=${filters}
        setFilters=${applyFilters}
        channels=${channels}
        sessionLabel=${sessionLabel}
      />
      <div class="result-bar">
        <span class="muted" role="status">
          ${matching.length === ledger.verdicts.length
            ? plural(matching.length, "result")
            : `${matching.length} of ${plural(ledger.verdicts.length, "result")}`}
        </span>
        <span class="result-bar-actions">
          ${isFiltered(filters)
            ? html`<${Button} variant="ghost" onClick=${() => applyFilters({...EMPTY_FILTERS})}>
                Clear filters
              <//>`
            : null}
          <${Button}
            disabled=${matching.length === 0}
            onClick=${() => exportVerdictsCsv(matching)}
          >
            Export CSV
          <//>
          <${Button}
            disabled=${matching.length === 0}
            onClick=${() =>
              exportJson(
                {
                  verdicts: matching,
                  feedback: matching
                    .map((verdict) => ledger.feedback.get(verdict.id))
                    .filter(Boolean),
                },
                "fact-checks"
              )}
          >
            Export JSON
          <//>
        </span>
      </div>
      ${matching.length === 0
        ? html`<p class="empty">Nothing matches these filters.</p>`
        : html`<ul class="result-list">
            ${matching.slice(0, shown).map(
              (verdict) => html`
                <${VerdictRow}
                  key=${verdict.id}
                  verdict=${verdict}
                  feedback=${ledger.feedback.get(verdict.id)}
                  expanded=${expandedId === verdict.id}
                  onToggle=${() => setExpandedId(expandedId === verdict.id ? null : verdict.id)}
                />
              `
            )}
          </ul>`}
      ${matching.length > shown
        ? html`<div class="form-actions">
            <${Button} onClick=${() => setShown(shown + PAGE_SIZE)}>
              Show ${Math.min(PAGE_SIZE, matching.length - shown)} more
            <//>
          </div>`
        : null}
    </div>
  `;
};

/* ------------------------------------------------------------------ */
/* Sortable tables                                                      */
/* ------------------------------------------------------------------ */

/** Sort state for a table: which column, which way. */
const useSort = (initialColumn, initialDirection = "desc") => {
  const [sort, setSort] = useState({column: initialColumn, direction: initialDirection});
  const toggle = (column, firstDirection = "desc") =>
    setSort(
      sort.column === column
        ? {column, direction: sort.direction === "desc" ? "asc" : "desc"}
        : {column, direction: firstDirection}
    );
  return [sort, toggle];
};

const SortHeader = ({column, sort, onSort, numeric = false, firstDirection, title, children}) => {
  const active = sort.column === column;
  return html`
    <th
      scope="col"
      class=${numeric ? "numeric" : undefined}
      aria-sort=${active ? (sort.direction === "asc" ? "ascending" : "descending") : undefined}
    >
      <button
        type="button"
        class="sort-button"
        title=${title}
        onClick=${() => onSort(column, firstDirection)}
      >
        ${children}<span class="sort-arrow" aria-hidden="true"
          >${active ? (sort.direction === "asc" ? "▲" : "▼") : ""}</span
        >
      </button>
    </th>
  `;
};

/* ------------------------------------------------------------------ */
/* Channels                                                             */
/* ------------------------------------------------------------------ */

const CHANNEL_SORTS = Object.freeze({
  channel: (row) => row.channelName.toLowerCase(),
  watch: (row) => row.watchSeconds,
  checks: (row) => row.checks,
  sample: (row) => row.adjudicated.n,
  // Shares below the sample floor are not shown, so they do not sort either.
  truePct: (row) => (row.adjudicated.reliable ? row.adjudicated.truePct : null),
  falsePct: (row) => (row.adjudicated.reliable ? row.adjudicated.falsePct : null),
  misleadingPct: (row) => (row.adjudicated.reliable ? row.adjudicated.misleadingPct : null),
  unverifiedPct: (row) => row.adjudicated.unverifiedPct,
  cost: (row) => row.cost.usd,
});

const Share = ({adjudicated, percent}) =>
  adjudicated.reliable
    ? formatPercent(percent)
    : html`<span
        class="muted"
        title=${`Shown from ${MIN_ADJUDICATED_SAMPLE} adjudicated checks; this channel has ${adjudicated.n}.`}
        >—</span
      >`;

const ChannelsPanel = ({ledger, showResultsFor}) => {
  const [platform, setPlatform] = useState("");
  const [minimumSample, setMinimumSample] = useState(0);
  const [sort, onSort] = useSort("watch");

  const allRows = useMemo(() => summarizeChannels(ledger), [ledger]);
  const rows = sortRows(
    allRows.filter(
      (row) => (!platform || row.platform === platform) && row.adjudicated.n >= minimumSample
    ),
    CHANNEL_SORTS[sort.column],
    sort.direction
  );

  return html`
    <div role="tabpanel" id="panel-channels" aria-labelledby="tab-channels" class="panel">
      <div class="filter-row">
        <select
          aria-label="Site"
          value=${platform}
          onChange=${(event) => setPlatform(event.currentTarget.value)}
        >
          <option value="">All sites</option>
          ${PLATFORMS.map(
            (option) => html`<option key=${option} value=${option}>${platformLabel(option)}</option>`
          )}
        </select>
        <label class="inline-field">
          <span>At least</span>
          <input
            type="number"
            class="input-number"
            min="0"
            step="5"
            value=${minimumSample}
            onInput=${(event) =>
              setMinimumSample(Math.max(0, Number(event.currentTarget.value) || 0))}
          />
          <span>adjudicated checks</span>
        </label>
      </div>
      ${rows.length === 0
        ? html`<p class="empty">
            ${allRows.length === 0
              ? "No channel has been recorded yet."
              : "No channel matches these filters."}
          </p>`
        : html`<div class="table-scroll">
            <table class="data-table">
              <thead>
                <tr>
                  <${SortHeader} column="channel" sort=${sort} onSort=${onSort} firstDirection="asc">
                    Channel
                  <//>
                  <${SortHeader} column="watch" sort=${sort} onSort=${onSort} numeric>Watched<//>
                  <${SortHeader} column="checks" sort=${sort} onSort=${onSort} numeric>Checks<//>
                  <${SortHeader}
                    column="sample"
                    sort=${sort}
                    onSort=${onSort}
                    numeric
                    title="Checks that reached a True, False or Misleading verdict"
                  >
                    Adjudicated
                  <//>
                  <${SortHeader} column="truePct" sort=${sort} onSort=${onSort} numeric>True<//>
                  <${SortHeader} column="falsePct" sort=${sort} onSort=${onSort} numeric>False<//>
                  <${SortHeader} column="misleadingPct" sort=${sort} onSort=${onSort} numeric>
                    Misleading
                  <//>
                  <${SortHeader}
                    column="unverifiedPct"
                    sort=${sort}
                    onSort=${onSort}
                    numeric
                    title="How often no solid evidence was found — a limit of the checker, not a mark against the channel"
                  >
                    Unverified
                  <//>
                  <${SortHeader} column="cost" sort=${sort} onSort=${onSort} numeric>Spend<//>
                  <th scope="col"><span class="visually-hidden">Actions</span></th>
                </tr>
              </thead>
              <tbody>
                ${rows.map(
                  (row) => html`
                    <tr key=${row.key}>
                      <th scope="row">
                        ${row.channelName}
                        <span class="cell-sub">${platformLabel(row.platform)}</span>
                      </th>
                      <td class="numeric">
                        ${formatDuration(row.watchSeconds)}
                        <span class="cell-sub">${plural(row.sessions, "session")}</span>
                      </td>
                      <td class="numeric">${row.checks}</td>
                      <td class="numeric">${row.adjudicated.n}</td>
                      <td class="numeric">
                        <${Share} adjudicated=${row.adjudicated} percent=${row.adjudicated.truePct} />
                      </td>
                      <td class="numeric">
                        <${Share} adjudicated=${row.adjudicated} percent=${row.adjudicated.falsePct} />
                      </td>
                      <td class="numeric">
                        <${Share}
                          adjudicated=${row.adjudicated}
                          percent=${row.adjudicated.misleadingPct}
                        />
                      </td>
                      <td class="numeric">${formatPercent(row.adjudicated.unverifiedPct)}</td>
                      <td class="numeric">${formatCost(row.cost)}</td>
                      <td class="row-actions">
                        <${Button}
                          variant="ghost"
                          onClick=${() =>
                            showResultsFor({
                              platform: row.platform ?? "",
                              channel: row.key,
                            })}
                        >
                          Results
                        <//>
                        <${ConfirmButton}
                          confirmLabel="Delete all of it?"
                          onConfirm=${() => removeChannel(row.platform, row.channelId)}
                          aria-label=${`Delete ${row.channelName}`}
                        >
                          Delete
                        <//>
                      </td>
                    </tr>
                  `
                )}
              </tbody>
            </table>
          </div>`}
      <p class="table-note">
        True, False and Misleading are shares of <em>adjudicated</em> checks and appear once a
        channel has ${MIN_ADJUDICATED_SAMPLE} of them — below that a percentage says more about
        chance than about the channel. The same creator on two sites is listed twice.
      </p>
    </div>
  `;
};

/* ------------------------------------------------------------------ */
/* Sessions                                                             */
/* ------------------------------------------------------------------ */

const SESSION_SORTS = Object.freeze({
  started: (row) => row.session.started_at,
  channel: (row) => (row.session.channel_name ?? "").toLowerCase() || null,
  site: (row) => platformLabel(row.session.platform),
  duration: (row) => row.durationSeconds,
  claims: (row) => row.claims,
  checks: (row) => row.checks,
  cost: (row) => row.cost.usd,
});

const SessionsPanel = ({ledger, showResultsFor}) => {
  const [sort, onSort] = useSort("started");
  const [shown, setShown] = useState(PAGE_SIZE);
  const allRows = useMemo(() => summarizeSessions(ledger), [ledger]);
  const rows = sortRows(allRows, SESSION_SORTS[sort.column], sort.direction);

  if (rows.length === 0) {
    return html`
      <div role="tabpanel" id="panel-sessions" aria-labelledby="tab-sessions" class="panel">
        <p class="empty">No session has been recorded yet.</p>
      </div>
    `;
  }
  return html`
    <div role="tabpanel" id="panel-sessions" aria-labelledby="tab-sessions" class="panel">
      <div class="table-scroll">
        <table class="data-table">
          <thead>
            <tr>
              <${SortHeader} column="started" sort=${sort} onSort=${onSort}>Started<//>
              <${SortHeader} column="channel" sort=${sort} onSort=${onSort} firstDirection="asc">
                Channel
              <//>
              <${SortHeader} column="site" sort=${sort} onSort=${onSort} firstDirection="asc">
                Site
              <//>
              <${SortHeader} column="duration" sort=${sort} onSort=${onSort} numeric>Length<//>
              <${SortHeader}
                column="claims"
                sort=${sort}
                onSort=${onSort}
                numeric
                title="Checkable claims the checker picked out of the speech"
              >
                Claims heard
              <//>
              <${SortHeader} column="checks" sort=${sort} onSort=${onSort} numeric>Checks<//>
              <${SortHeader} column="cost" sort=${sort} onSort=${onSort} numeric>Spend<//>
              <th scope="col"><span class="visually-hidden">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            ${rows.slice(0, shown).map(({session, durationSeconds, claims, checks, cost}) => {
              const name = session.channel_name ?? "—";
              return html`
                <tr key=${session.id}>
                  <th scope="row">
                    ${formatDateTime(session.started_at)}
                    ${session.stream_title
                      ? html`<span class="cell-sub">${session.stream_title}</span>`
                      : null}
                  </th>
                  <td>${name}</td>
                  <td>${platformLabel(session.platform)}</td>
                  <td class="numeric">
                    ${formatDuration(durationSeconds)}${session.ended_at ? "" : " +"}
                  </td>
                  <td class="numeric">${claims}</td>
                  <td class="numeric">${checks}</td>
                  <td class="numeric">${formatCost(cost)}</td>
                  <td class="row-actions">
                    <${Button}
                      variant="ghost"
                      disabled=${checks === 0}
                      onClick=${() => showResultsFor({sessionId: session.id})}
                    >
                      Results
                    <//>
                    <${ConfirmButton}
                      confirmLabel="Delete it?"
                      onConfirm=${() => removeSession(session.id)}
                      aria-label=${`Delete the session of ${formatDateTime(session.started_at)}`}
                    >
                      Delete
                    <//>
                  </td>
                </tr>
              `;
            })}
          </tbody>
        </table>
      </div>
      ${rows.length > shown
        ? html`<div class="form-actions">
            <${Button} onClick=${() => setShown(shown + PAGE_SIZE)}>Show more<//>
          </div>`
        : null}
      <p class="table-note">
        A session is one stretch of one channel: switching channel, or the checker reconnecting,
        starts a new one. "+" marks a session whose end was not recorded.
      </p>
    </div>
  `;
};

/* ------------------------------------------------------------------ */
/* Screen                                                               */
/* ------------------------------------------------------------------ */

export const HistoryView = () => {
  const ledger = useLedger();
  const [tab, setTab] = useState(TAB.RESULTS);
  const [filters, setFilters] = useState({...EMPTY_FILTERS});

  const showResultsFor = (patch) => {
    setFilters({...EMPTY_FILTERS, ...patch});
    setTab(TAB.RESULTS);
  };
  const channelCount = useMemo(
    () =>
      new Set(
        ledger.sessions
          .map((session) => channelKey(session.platform, session.channel_id))
          .filter(Boolean)
      ).size,
    [ledger.sessions]
  );
  const empty = ledger.sessions.length === 0 && ledger.verdicts.length === 0;

  return html`
    <div class="page">
      <header class="page-header">
        <h1 class="page-title">History</h1>
        <p class="page-subtitle">
          Every result from Twitch, YouTube, Kick and Rumble, kept in this browser only.
        </p>
      </header>

      ${ledger.error
        ? html`<${Notice} tone="error">The history could not be read: ${ledger.error}<//>`
        : null}
      ${!ledger.loaded
        ? html`<p class="muted">Loading your history…</p>`
        : empty
          ? html`<${Card} title="Nothing here yet">
              <p class="lede">
                Results are recorded from the next stream you check. If you used this checker before
                this screen existed, your earlier results are still in the local backend:
              </p>
              <${ImportHistory} compact />
            <//>`
          : html`
              <${Tabs}
                label="History views"
                active=${tab}
                onSelect=${setTab}
                tabs=${[
                  {id: TAB.RESULTS, label: "Results", count: ledger.verdicts.length},
                  {id: TAB.CHANNELS, label: "Channels", count: channelCount},
                  {id: TAB.SESSIONS, label: "Sessions", count: ledger.sessions.length},
                ]}
              />
              ${tab === TAB.RESULTS
                ? html`<${ResultsPanel} ledger=${ledger} filters=${filters} setFilters=${setFilters} />`
                : tab === TAB.CHANNELS
                  ? html`<${ChannelsPanel} ledger=${ledger} showResultsFor=${showResultsFor} />`
                  : html`<${SessionsPanel} ledger=${ledger} showResultsFor=${showResultsFor} />`}
            `}
    </div>
  `;
};
