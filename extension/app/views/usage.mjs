/**
 * Usage: how much the checker ran and what it cost.
 *
 * Every figure comes from the ledger's usage rows — one per provider call.
 * A cost is exact when the provider reported it (OpenRouter does) and an
 * estimate from the shipped price table otherwise; estimates are always
 * marked "≈", and calls with no known price are counted separately rather
 * than quietly treated as free.
 */

import {COST_SOURCE, PRICING_VERSION} from "../../ledger/pricing.mjs";
import {PERIODS, dailySpend, periodRange, summarizeUsage} from "../../ledger/queries.mjs";
import {Card, Notice, Segmented} from "../components.mjs";
import {
  formatCost,
  formatDay,
  formatHours,
  formatTokens,
  formatUsd,
  platformLabel,
  plural,
} from "../lib/format.mjs";
import {html, useMemo, useState} from "../lib/html.mjs";
import {useLedger} from "../lib/ledger_store.mjs";

const PERIOD_OPTIONS = Object.freeze([
  {value: PERIODS.TODAY, label: "Today"},
  {value: PERIODS.WEEK, label: "7 days"},
  {value: PERIODS.MONTH, label: "30 days"},
  {value: PERIODS.ALL, label: "All time"},
]);

const DAY_MS = 86_400_000;
const MAX_CHART_DAYS = 90;

/** How many daily bars a period gets; 0 means "no chart" (a single day). */
const chartDays = (period, usage, now = new Date()) => {
  if (period === PERIODS.WEEK) {
    return 7;
  }
  if (period === PERIODS.MONTH) {
    return 30;
  }
  if (period !== PERIODS.ALL || usage.length === 0) {
    return 0;
  }
  const first = Math.min(...usage.map((row) => Date.parse(row.ts)).filter(Number.isFinite));
  const span = Math.ceil((now.getTime() - first) / DAY_MS) + 1;
  return Math.min(MAX_CHART_DAYS, Math.max(7, span));
};

/* ------------------------------------------------------------------ */
/* Daily spend chart                                                    */
/* ------------------------------------------------------------------ */

// Drawn close to the width of the page column, so its text is not scaled up.
const CHART = Object.freeze({width: 900, height: 210, left: 52, right: 8, top: 16, bottom: 24});
const MAX_BAR_WIDTH = 24;
const BAR_GAP = 2;
const BAR_RADIUS = 4;

/** A round number at or above `value`, so the top gridline reads cleanly. */
const niceCeiling = (value) => {
  if (!(value > 0)) {
    return 1;
  }
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const step = [1, 2, 2.5, 5, 10].find((candidate) => candidate * magnitude >= value);
  return step * magnitude;
};

/** A column with a rounded top and a square foot on the baseline. */
const columnPath = (x, top, width, baseline) => {
  const radius = Math.min(BAR_RADIUS, width / 2, baseline - top);
  return [
    `M${x},${baseline}`,
    `V${top + radius}`,
    `Q${x},${top} ${x + radius},${top}`,
    `H${x + width - radius}`,
    `Q${x + width},${top} ${x + width},${top + radius}`,
    `V${baseline}`,
    "Z",
  ].join(" ");
};

const SpendChart = ({days}) => {
  const [activeIndex, setActiveIndex] = useState(null);
  const peak = Math.max(...days.map((day) => day.usd));
  if (!(peak > 0)) {
    return html`<p class="muted">No spend was recorded on these days.</p>`;
  }
  const ceiling = niceCeiling(peak);
  const plotWidth = CHART.width - CHART.left - CHART.right;
  const baseline = CHART.height - CHART.bottom;
  const plotHeight = baseline - CHART.top;
  const band = plotWidth / days.length;
  const barWidth = Math.max(1, Math.min(MAX_BAR_WIDTH, band - BAR_GAP));
  const yOf = (usd) => baseline - (usd / ceiling) * plotHeight;
  const centerOf = (index) => CHART.left + band * (index + 0.5);
  // First, middle and last day only: a date under every bar would collide.
  const labelled = new Set([0, Math.floor((days.length - 1) / 2), days.length - 1]);
  const peakIndex = days.findIndex((day) => day.usd === peak);
  const active = activeIndex === null ? null : days[activeIndex];

  return html`
    <figure class="chart">
      <div class="chart-plot">
        <svg
          viewBox=${`0 0 ${CHART.width} ${CHART.height}`}
          role="group"
          aria-label=${`Spend per day over ${days.length} days. Highest: ${formatUsd(peak)} on ${formatDay(days[peakIndex].dayMs)}.`}
        >
          ${[0, 0.5, 1].map((fraction) => {
            const y = yOf(ceiling * fraction);
            return html`
              <g key=${fraction}>
                <line class="chart-grid" x1=${CHART.left} x2=${CHART.width - CHART.right} y1=${y} y2=${y} />
                <text class="chart-axis" x=${CHART.left - 8} y=${y + 4} text-anchor="end">
                  ${formatUsd(ceiling * fraction)}
                </text>
              </g>
            `;
          })}
          ${days.map((day, index) => {
            const center = centerOf(index);
            return html`
              <g key=${day.dayMs}>
                ${day.usd > 0
                  ? html`<path
                      class="chart-bar"
                      data-active=${index === activeIndex ? "true" : undefined}
                      data-today=${index === days.length - 1 ? "true" : undefined}
                      d=${columnPath(center - barWidth / 2, yOf(day.usd), barWidth, baseline)}
                    />`
                  : null}
                ${labelled.has(index)
                  ? html`<text
                      class="chart-axis"
                      data-today=${index === days.length - 1 ? "true" : undefined}
                      x=${center}
                      y=${CHART.height - 6}
                      text-anchor="middle"
                    >
                      ${index === days.length - 1 ? "Today" : formatDay(day.dayMs)}
                    </text>`
                  : null}
                <rect
                  class="chart-hit"
                  x=${CHART.left + band * index}
                  y=${CHART.top}
                  width=${band}
                  height=${plotHeight}
                  data-index=${index}
                  tabindex=${index === (activeIndex ?? days.length - 1) ? 0 : -1}
                  role="img"
                  onKeyDown=${(event) => {
                    // One tab stop for the chart; arrows move between days.
                    const step = {ArrowRight: 1, ArrowLeft: -1}[event.key];
                    const next = index + (step ?? 0);
                    if (step && next >= 0 && next < days.length) {
                      event.preventDefault();
                      event.currentTarget
                        .closest("svg")
                        .querySelector(`[data-index="${next}"]`)
                        ?.focus();
                    }
                  }}
                  aria-label=${`${formatDay(day.dayMs)}: ${day.estimated ? "about " : ""}${formatUsd(day.usd)}, ${plural(day.calls, "call")}`}
                  onPointerEnter=${() => setActiveIndex(index)}
                  onPointerLeave=${() => setActiveIndex(null)}
                  onFocus=${() => setActiveIndex(index)}
                  onBlur=${() => setActiveIndex(null)}
                />
              </g>
            `;
          })}
          ${activeIndex === null
            ? html`<text
                class="chart-value"
                x=${centerOf(peakIndex)}
                y=${yOf(peak) - 6}
                text-anchor=${peakIndex < 2 ? "start" : peakIndex > days.length - 3 ? "end" : "middle"}
              >
                ${formatUsd(peak)}
              </text>`
            : null}
        </svg>
        ${active
          ? html`<div
              class="chart-tooltip"
              style=${`left: ${(centerOf(activeIndex) / CHART.width) * 100}%; top: ${(yOf(active.usd) / CHART.height) * 100}%`}
            >
              <strong>${active.estimated ? "≈ " : ""}${formatUsd(active.usd)}</strong>
              <span>${formatDay(active.dayMs)} · ${plural(active.calls, "call")}</span>
            </div>`
          : null}
      </div>
      <details class="chart-table">
        <summary>Show as a table</summary>
        <div class="table-scroll">
          <table class="data-table">
            <thead>
              <tr>
                <th scope="col">Day</th>
                <th scope="col" class="numeric">Spend</th>
                <th scope="col" class="numeric">Provider calls</th>
              </tr>
            </thead>
            <tbody>
              ${days
                .filter((day) => day.calls > 0)
                .reverse()
                .map(
                  (day) => html`
                    <tr key=${day.dayMs}>
                      <th scope="row">${formatDay(day.dayMs)}</th>
                      <td class="numeric">${day.estimated ? "≈ " : ""}${formatUsd(day.usd)}</td>
                      <td class="numeric">${day.calls}</td>
                    </tr>
                  `
                )}
            </tbody>
          </table>
        </div>
      </details>
    </figure>
  `;
};

/* ------------------------------------------------------------------ */
/* Breakdowns                                                           */
/* ------------------------------------------------------------------ */

/**
 * Ranked horizontal bars, largest first: one row per group with its name, a
 * bar on a shared baseline, and the figures. Bars measure spend, or calls
 * when nothing in the list has a price.
 */
const Breakdown = ({title, groups, labelOf = (group) => group.label}) => {
  if (groups.length === 0) {
    return null;
  }
  const bySpend = groups.some((group) => group.cost.usd > 0);
  const measure = (group) => (bySpend ? group.cost.usd : group.calls);
  const largest = Math.max(...groups.map(measure));
  return html`
    <${Card} title=${title}>
      <ul class="meter-list">
        ${groups.map(
          (group) => html`
            <li key=${group.key} class="meter">
              <span class="meter-name">${labelOf(group)}</span>
              <span class="meter-track" aria-hidden="true"
                ><span
                  class="meter-fill"
                  style=${`width: ${largest > 0 ? (measure(group) / largest) * 100 : 0}%`}
                ></span
              ></span>
              <span class="meter-figure">${formatCost(group.cost)}</span>
              <span class="meter-sub">${plural(group.calls, "call")}</span>
            </li>
          `
        )}
      </ul>
    <//>
  `;
};

const TokenTable = ({groups}) => html`
  <details class="advanced">
    <summary>Tokens and searches by model</summary>
    <div class="table-scroll">
      <table class="data-table">
        <thead>
          <tr>
            <th scope="col">Model</th>
            <th scope="col" class="numeric">Calls</th>
            <th scope="col" class="numeric">Input</th>
            <th scope="col" class="numeric" title="Input read back from the provider's prompt cache, billed at a fraction of the normal rate">
              Cached input
            </th>
            <th scope="col" class="numeric">Output</th>
            <th scope="col" class="numeric">Web searches</th>
            <th scope="col" class="numeric">Spend</th>
          </tr>
        </thead>
        <tbody>
          ${groups.map(
            (group) => html`
              <tr key=${group.key}>
                <th scope="row">${group.label}</th>
                <td class="numeric">${group.calls}</td>
                <td class="numeric">${formatTokens(group.inputTokens)}</td>
                <td class="numeric">${formatTokens(group.cachedInputTokens)}</td>
                <td class="numeric">${formatTokens(group.outputTokens)}</td>
                <td class="numeric">${group.webSearches}</td>
                <td class="numeric">${formatCost(group.cost)}</td>
              </tr>
            `
          )}
        </tbody>
      </table>
    </div>
    <p class="table-note">
      "≈" marks an estimate from token counts and list prices (price table of ${PRICING_VERSION}).
      Figures without it were reported by the provider. Your provider's own billing page is the
      final word.
    </p>
  </details>
`;

const Stat = ({value, label, hint}) => html`
  <div class="stat" title=${hint}>
    <span class="stat-value">${value}</span>
    <span class="stat-label">${label}</span>
  </div>
`;

/* ------------------------------------------------------------------ */
/* Screen                                                               */
/* ------------------------------------------------------------------ */

export const UsageView = () => {
  const ledger = useLedger();
  const [period, setPeriod] = useState(PERIODS.WEEK);

  const summary = useMemo(() => summarizeUsage(ledger, periodRange(period)), [ledger, period]);
  const days = useMemo(() => {
    const count = chartDays(period, ledger.usage);
    return count > 0 ? dailySpend(ledger.usage, {days: count}) : [];
  }, [ledger.usage, period]);
  const hasEstimate = ledger.usage.some((row) => row.cost_source === COST_SOURCE.ESTIMATE);
  const {total} = summary;

  return html`
    <div class="page">
      <header class="page-header page-header-split">
        <div>
          <h1 class="page-title">Usage</h1>
          <p class="page-subtitle">What the checker did and what it cost you at your AI provider.</p>
        </div>
        <${Segmented}
          compact
          name="usage-period"
          label="Period"
          options=${PERIOD_OPTIONS}
          value=${period}
          onChange=${setPeriod}
        />
      </header>

      ${ledger.error
        ? html`<${Notice} tone="error">The history could not be read: ${ledger.error}<//>`
        : null}

      <section class="card stat-card" aria-label="Totals for this period">
        <div class="stat-row">
          <${Stat} value=${summary.checks} label="fact-checks" />
          <${Stat}
            value=${formatHours(summary.watchSeconds)}
            label="hours listened"
            hint=${`${plural(summary.sessions, "session")}`}
          />
          <${Stat} value=${formatCost(total.cost)} label="spend" />
          <${Stat}
            value=${summary.costPerHour === null
              ? "—"
              : `${total.cost.estimated ? "≈ " : ""}${formatUsd(summary.costPerHour)}`}
            label="per hour listened"
          />
        </div>
        ${total.calls === 0
          ? html`<p class="stat-note">
              ${ledger.loaded
                ? "No provider calls in this period. Usage is recorded while a stream is being checked."
                : "Loading…"}
            </p>`
          : html`<p class="stat-note">
              ${[
                plural(total.calls, "provider call"),
                plural(total.webSearches, "web search", "web searches"),
                `${formatTokens(total.inputTokens + total.cachedInputTokens)} tokens in, ${formatTokens(total.outputTokens)} out`,
              ].join(" · ")}
            </p>`}
      </section>

      ${total.cost.unpricedCalls > 0
        ? html`<${Notice} tone="warn">
            ${plural(total.cost.unpricedCalls, "call")} had no cost reported and no list price in
            this app's table, so ${total.cost.unpricedCalls === 1 ? "it is" : "they are"} not in
            the spend above.
          <//>`
        : null}
      ${summary.includesImported
        ? html`<${Notice} tone="info">
            Part of this period was imported from the backend, which recorded only a flat-rate
            estimate per check — no tokens. Sessions recorded from now on are priced per call.
          <//>`
        : null}

      ${days.length > 0
        ? html`<${Card}
            title="Spend per day"
            aside=${hasEstimate ? html`<span class="muted">≈ includes estimates</span>` : null}
          >
            <${SpendChart} days=${days} />
          <//>`
        : null}

      ${total.calls > 0
        ? html`
            <div class="card-grid">
              <${Breakdown} title="By model" groups=${summary.byModel} />
              <${Breakdown} title="By step" groups=${summary.byStage} />
              <${Breakdown}
                title="By site"
                groups=${summary.byPlatform}
                labelOf=${(group) => platformLabel(group.key === "unknown" ? null : group.key)}
              />
              <${Breakdown}
                title="By channel"
                groups=${summary.byChannel.slice(0, 8)}
                labelOf=${(group) =>
                  // The same name on two sites is two channels: say which.
                  group.key === "unknown"
                    ? group.label
                    : `${group.label} · ${platformLabel(group.key.split(":")[0])}`}
              />
            </div>
            <${TokenTable} groups=${summary.byModel} />
          `
        : null}
    </div>
  `;
};
