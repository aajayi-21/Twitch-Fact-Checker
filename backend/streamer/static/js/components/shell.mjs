// The design-1b sidebar shell: brand, nav with live badges, footer status.
import { html } from "../html.mjs";
import { route, navigate } from "../router.mjs";
import {
  health,
  botStatus,
  bot,
  queue,
  dryRun,
  twitchStatus,
  setupStatus,
  wsConnected,
} from "../store.mjs";

const NAV = [
  ["cockpit", "Cockpit"],
  ["setup", "Setup"],
  ["pipeline", "Pipeline"],
  ["bot", "Bot settings"],
  ["decisions", "Decisions"],
  ["analytics", "Analytics"],
];

function setupProgress() {
  // The checklist steps the Setup view renders; the ✓ mirrors them.
  const llm = Boolean(setupStatus.value?.configured);
  const twitch = Boolean(twitchStatus.value?.configured);
  const modded = Boolean(bot.value?.is_moderator);
  const armed = Boolean(bot.value?.armed);
  const live = (botStatus.value?.live_sessions || []).length > 0;
  const done = [llm, twitch, modded, armed, live].filter(Boolean).length;
  return { done, total: 5 };
}

function NavLink({ view, label }) {
  const active = route.value === view;
  const badges = [];
  if (view === "cockpit" && queue.value.length > 0) {
    badges.push(
      html`<span class="tag tag-accent nav-badge">${queue.value.length}</span>`
    );
  }
  if (view === "setup") {
    const { done, total } = setupProgress();
    badges.push(
      done === total
        ? html`<span class="nav-ok">${done}/${total} ✓</span>`
        : html`<span class="nav-badge text-muted">${done}/${total}</span>`
    );
  }
  return html`<a
    href=${`#/${view}`}
    aria-current=${active ? "page" : undefined}
    onClick=${(event) => {
      event.preventDefault();
      navigate(view);
    }}
    >${label}${badges}</a
  >`;
}

export function Shell({ children }) {
  const currentBot = bot.value;
  const status = botStatus.value;
  return html`
    <div class="shell">
      <aside class="sidebar">
        <span class="brand"><span class="brand-mark">✓</span>Fact-Checker</span>
        <nav class="sidenav">
          ${NAV.map(
            ([view, label]) =>
              html`<${NavLink} key=${view} view=${view} label=${label} />`
          )}
        </nav>
        <div class="side-foot">
          <span class="dot" data-on=${health.value ? "" : undefined}
            >backend</span
          >
          <span
            class="dot"
            data-on=${(status?.live_sessions || []).length ? "" : undefined}
            >ingest</span
          >
          <span class="dot" data-on=${currentBot?.connected ? "" : undefined}
            >chat${status?.bot_login ? ` · ${status.bot_login}` : ""}</span
          >
          ${dryRun.value &&
          html`<span class="dot" data-warn="">DRY RUN</span>`}
          ${!wsConnected.value &&
          html`<span class="text-muted">events reconnecting…</span>`}
          <span
            >${status?.bot_login || "no bot"} @
            ${status?.channel || "no channel"}</span
          >
        </div>
      </aside>
      <main class="main">${children}</main>
    </div>
  `;
}
