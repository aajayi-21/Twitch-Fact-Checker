// Design 1c — Analytics: stat tiles, verdict mix, claim funnel, per-channel
// cards with the honest-metrics doctrine (rates only at n>=30 adjudicated).
import { useEffect } from "preact/hooks";
import { html } from "../html.mjs";
import {
  health,
  consoleStats,
  summary,
  channels,
  botStatus,
  refreshAnalytics,
  refreshConsoleStats,
} from "../store.mjs";

const MIX = [
  ["TRUE", "var(--verdict-true)"],
  ["FALSE", "var(--verdict-false)"],
  ["MISLEADING", "var(--verdict-misleading)"],
  ["UNVERIFIED", "var(--color-neutral-700)"],
];

function Tile({ kicker, value, meta }) {
  return html`<div class="card">
    <span class="card-kicker">${kicker}</span>
    <span class="tile-num">${value}</span>
    <span class="card-meta">${meta}</span>
  </div>`;
}

function VerdictMix() {
  const labels = summary.value?.today?.labels || {};
  const total = Object.values(labels).reduce((sum, count) => sum + count, 0);
  return html`
    <div class="card">
      <span class="card-kicker">Verdict mix</span>
      ${total === 0
        ? html`<span class="card-meta">no verdicts today yet</span>`
        : html`
            <div class="mix-bar">
              ${MIX.map(
                ([label, color]) =>
                  html`<span
                    key=${label}
                    style=${`width:${((labels[label] || 0) / total) * 100}%;background:${color}`}
                  ></span>`
              )}
            </div>
            <div class="mix-legend">
              ${MIX.map(
                ([label, color]) => html`
                  <span key=${label}
                    ><span class="swatch" style=${`background:${color}`}></span
                    >${label} ${labels[label] || 0}</span
                  >
                `
              )}
            </div>
          `}
    </div>
  `;
}

function Funnel() {
  const funnel = consoleStats.value?.funnel_today;
  if (!funnel) return null;
  const steps = [
    ["heard", funnel.heard],
    ["gate passed", funnel.gate_passed],
    ["checked", funnel.checked],
    ["passed policy", funnel.passed_policy],
    ["posted", funnel.posted],
  ];
  const top = Math.max(funnel.heard, 1);
  return html`
    <div class="card">
      <span class="card-kicker">Claim funnel</span>
      <div class="funnel">
        ${steps.map(
          ([name, count]) => html`
            <span key=${name}>${name}</span>
            <span
              ><span
                class="bar"
                style=${`width:${Math.max((count / top) * 100, count > 0 ? 2 : 0)}%`}
              ></span
            ></span>
            <span class="num">${count.toLocaleString()}</span>
          `
        )}
      </div>
      <span class="card-meta"
        >deliberately much less than what gets checked — the policy is the
        product</span
      >
    </div>
  `;
}

function ChannelCards() {
  const rows = channels.value || [];
  return html`
    <h6 style="color:var(--color-neutral-500);margin:0 0 10px">Channels</h6>
    <div class="channels-grid">
      ${rows.map((row) => {
        const adjudicated = row.adjudicated || {};
        const hasRates = (adjudicated.n || 0) >= 30;
        return html`
          <div class="card elev-sm" key=${`${row.platform}/${row.channel}`}>
            <span class="card-title">${row.channel}</span>
            <div class="kv" style="font-size:12.5px">
              <span class="text-muted">sessions</span>
              <span class="num">${row.sessions}</span>
              <span class="text-muted">claims</span>
              <span class="num">${row.claims}</span>
              <span class="text-muted">FALSE rate</span>
              <span
                class="num"
                style=${hasRates ? "" : "color:var(--color-neutral-600)"}
                >${hasRates ? `${adjudicated.false_pct}%` : "—"}</span
              >
            </div>
            <span class="card-meta">
              ${hasRates
                ? `${adjudicated.n} adjudicated verdicts`
                : "rates appear at ≥30 adjudicated verdicts"}
            </span>
          </div>
        `;
      })}
      <div class="card connect-card">+ connect another channel</div>
    </div>
  `;
}

export function Analytics() {
  useEffect(() => {
    refreshAnalytics();
    refreshConsoleStats();
    const timer = setInterval(refreshAnalytics, 30000);
    return () => clearInterval(timer);
  }, []);
  const stats = consoleStats.value;
  const approval = stats?.approval_7d;
  const latency = stats?.latency_today;
  const funnel = stats?.funnel_today;
  return html`
    <div class="view-head">
      <h4>Analytics</h4>
      <span class="text-muted" style="font-size:12.5px"
        >today · resets with the database</span
      >
      <span
        style="margin-left:auto;font-size:12px;color:var(--color-neutral-500)"
        >${botStatus.value?.channel || ""}</span
      >
    </div>
    <div class="tiles">
      <${Tile}
        kicker="Checks today"
        value=${health.value?.checks_today ?? "–"}
        meta=${health.value
          ? `~$${(health.value.est_cost_today_usd ?? 0).toFixed(2)} web-search spend`
          : ""}
      />
      <${Tile}
        kicker="Posted"
        value=${funnel?.posted ?? "–"}
        meta=${funnel ? `of ${funnel.passed_policy} that passed policy` : ""}
      />
      <${Tile}
        kicker="Approval rate"
        value=${approval?.rate != null
          ? `${Math.round(approval.rate * 100)}%`
          : "—"}
        meta="review-mode decisions, 7 days"
      />
      <${Tile}
        kicker="Median latency"
        value=${latency?.median_ms != null
          ? `${(latency.median_ms / 1000).toFixed(1)}s`
          : "—"}
        meta="claim heard → verdict"
      />
    </div>
    <div class="two-col">
      <${VerdictMix} />
      <${Funnel} />
    </div>
    <${ChannelCards} />
  `;
}
