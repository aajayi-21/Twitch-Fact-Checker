// Design 1d — the OBS Custom Browser Dock: 340px, big targets, queue-first.
// Same modules as the cockpit, denser chrome, no sidebar.
import { html } from "../html.mjs";
import { botStatus, bot, dryRun, setMode, setMute } from "../store.mjs";
import { Key, Seg, Dot } from "../components/ui.mjs";
import { ReviewQueue } from "../components/queue.mjs";

export function Dock() {
  const currentBot = bot.value;
  const status = botStatus.value;
  const muted = Boolean(currentBot?.muted);
  return html`
    <div class="dock">
      <div
        style="display:flex;align-items:center;gap:8px;font-size:11.5px;color:var(--color-neutral-500)"
      >
        <${Dot} on=${(status?.live_sessions || []).length > 0}>live<//>
        ${dryRun.value &&
        html`<span style="color:var(--verdict-misleading)">DRY RUN</span>`}
        ${currentBot &&
        html`<span
          style="margin-left:auto;font-variant-numeric:tabular-nums"
          >${currentBot.posts_this_hour}/${currentBot.posts_per_hour} this
          hour</span
        >`}
      </div>
      <button
        class="btn btn-primary mute-btn"
        data-muted=${String(muted)}
        disabled=${!currentBot}
        onClick=${() => setMute(!muted)}
      >
        ${muted ? "MUTED" : "MUTE"}  <${Key}>M<//>
      </button>
      <${Seg}
        name="dock-mode"
        value=${currentBot?.mode}
        onChange=${setMode}
        options=${[
          ["auto", "Auto"],
          ["review", "Review"],
          ["off", "Off"],
        ]}
      />
      <${ReviewQueue} compact />
    </div>
  `;
}
