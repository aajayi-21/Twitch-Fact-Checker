// The review queue — shared verbatim between the cockpit and the OBS dock.
// TTL mechanics: /bot/status carries review_ttl_s and each item's age_s at
// fetch time; the store stamps fetchedAtMs on every assignment, and the 1 Hz
// nowMs tick re-renders the countdowns. The server owns actual expiry — an
// expired card renders as such and the next refetch removes it; the client
// never fakes removal.
import { html } from "../html.mjs";
import {
  queue,
  selectedIdx,
  bot,
  nowMs,
  fetchedAtMs,
  approve,
  skip,
  topicColors,
  topicLabels,
} from "../store.mjs";
import { mmss, sourceDomains } from "../format.mjs";
import { Key, Tag } from "./ui.mjs";

function remainingSeconds(item) {
  nowMs.value; // subscribe to the 1 Hz tick
  const ttl = bot.value?.review_ttl_s ?? 180;
  const age = item.age_s + (performance.now() - fetchedAtMs.value) / 1000;
  return { left: Math.max(0, ttl - age), ttl };
}

function QueueCard({ item, index, compact }) {
  const selected = selectedIdx.value === index;
  const { left, ttl } = remainingSeconds(item);
  const urgent = left < 60;
  const expired = left <= 0;
  const verdict = item; // queue rows carry label/claim/message
  return html`
    <article
      class="queue-card"
      data-label=${item.label}
      data-selected=${selected ? "" : undefined}
      onClick=${() => (selectedIdx.value = index)}
    >
      <div class="q-row">
        <${Tag} label=${item.label} />
        <span
          class="topic-dot"
          title=${topicLabels.value[item.topic] || ""}
          style=${topicColors.value[item.topic]
            ? `background:${topicColors.value[item.topic]}`
            : ""}
        ></span>
        ${!compact &&
        html`<span class="text-muted" style="font-size:12px">
          ${(() => {
            const { shown, extra } = sourceDomains(verdict.sources, 2);
            if (!shown.length) return "";
            return `${shown.length + extra} source${
              shown.length + extra === 1 ? "" : "s"
            } · ${shown.join(", ")}${extra > 0 ? ` +${extra}` : ""}`;
          })()}
        </span>`}
        <span class="q-ttl" data-urgent=${urgent ? "" : undefined}>
          ${expired ? "expired" : `${mmss(left)} left`}
        </span>
      </div>
      <div class="ttl-bar">
        <div
          style=${`transform:scaleX(${ttl ? left / ttl : 0});background:var(--verdict-${(
            item.label || "unverified"
          ).toLowerCase()})`}
        ></div>
      </div>
      <p class="q-claim">${item.claim}</p>
      ${!compact && item.message && html`<pre class="chatmsg">${item.message}</pre>`}
      <div class="q-actions">
        <button
          class=${selected ? "btn btn-primary" : "btn btn-secondary"}
          disabled=${expired}
          onClick=${(event) => {
            event.stopPropagation();
            approve(item.post_id);
          }}
        >
          ${compact ? "Post" : "Post to chat"} <${Key}>↵<//>
        </button>
        <button
          class=${selected ? "btn btn-secondary" : "btn btn-ghost"}
          onClick=${(event) => {
            event.stopPropagation();
            skip(item.post_id);
          }}
        >
          Skip <${Key}>X<//>
        </button>
        ${selected &&
        !compact &&
        html`<span class="q-note">selected — keys act on this card</span>`}
      </div>
    </article>
  `;
}

export function ReviewQueue({ compact = false }) {
  const items = queue.value;
  const ttl = bot.value?.review_ttl_s ?? 180;
  return html`
    <div style="display:flex;flex-direction:column;gap:12px">
      <div class="q-head">
        <h6>${compact ? "Queue" : "Review queue"}</h6>
        ${items.length > 0 &&
        html`<span class="tag tag-accent">${items.length} waiting</span>`}
        ${!compact &&
        html`<span class="q-hint">
          items expire in ${mmss(ttl)} — a late fact-check is noise
        </span>`}
      </div>
      ${items.length === 0 &&
      html`<p class="text-muted" style="font-size:13px;margin:0">
        Nothing waiting. In review mode, verdicts land here for one-keystroke
        approval.
      </p>`}
      ${items.map(
        (item, index) =>
          html`<${QueueCard}
            key=${item.post_id}
            item=${item}
            index=${index}
            compact=${compact}
          />`
      )}
      <div class="keys-legend">
        <span><${Key}>J<//>/<${Key}>K<//> select</span>
        <span><${Key}>↵<//> post</span>
        <span><${Key}>X<//> skip</span>
        <span><${Key}>M<//> mute</span>
        ${!compact &&
        html`<span style="margin-left:auto"
          >works while this tab or the OBS dock has focus</span
        >`}
      </div>
    </div>
  `;
}
