// Design 1b — Decisions: every verdict the pipeline produced, and why it
// did or didn't post. One limit=200 fetch, client-side filter; "Older" uses
// the before= cursor for honest pagination.
import { useEffect, useState } from "preact/hooks";
import { html } from "../html.mjs";
import * as api from "../api.mjs";
import {
  decisions,
  consoleStats,
  refreshDecisions,
  retract,
  flash,
} from "../store.mjs";
import { Seg, Tag } from "../components/ui.mjs";
import { wallClock } from "../format.mjs";

const DECISION_COPY = {
  posted: (row) => `posted${row.handle ? ` · fc#${row.handle}` : ""}`,
  dry_run: () => "dry run",
  queued: () => "in review queue",
  suppressed: (row) => decorateReason(row.reason),
  failed: (row) => `failed · ${row.reason}`,
  expired: () => "expired in queue",
  skipped: () => "skipped in review",
};

function decorateReason(reason) {
  const copy = {
    label_not_postable: "blocked · never posts",
    label_disabled: "skipped · label off",
    topic_disabled: "skipped · topic off",
    burst_guard: "held · min gap",
    hourly_cap: "held · hourly cap",
    ten_minute_cap: "held · 10-min guard",
    stale: "held · too old",
    recently_posted: "held · repeat claim",
    topic_cooldown: "held · topic cooldown",
  };
  return copy[reason] || `held · ${reason}`;
}

export function Decisions() {
  const [filter, setFilter] = useState("all");
  const [olderCursor, setOlderCursor] = useState(null);
  useEffect(() => {
    refreshDecisions({ limit: 200 });
  }, []);

  const rows = decisions.value.filter((row) => {
    if (filter === "posted") return row.status === "posted";
    if (filter === "held") return row.status !== "posted";
    return true;
  });
  const funnel = consoleStats.value?.funnel_today;

  const loadOlder = async () => {
    const last = decisions.value[decisions.value.length - 1];
    if (!last) return;
    try {
      const older = await api.getDecisions({
        limit: 200,
        before: last.created_at,
      });
      decisions.value = [...decisions.value, ...older];
      setOlderCursor(older.length);
    } catch (error) {
      flash("error", String(error.message || error));
    }
  };

  return html`
    <div class="view-head">
      <h4>Decisions</h4>
      <span class="text-muted" style="font-size:12.5px"
        >every verdict the pipeline produced, and why it did or didn't
        post</span
      >
      <span style="margin-left:auto">
        <${Seg}
          compact
          name="decision-filter"
          value=${filter}
          onChange=${setFilter}
          options=${[
            ["all", "All"],
            ["posted", "Posted"],
            ["held", "Held back"],
          ]}
        />
      </span>
    </div>
    ${funnel &&
    html`<div class="stat-strip">
      <span><span class="big">${funnel.checked}</span>checked</span>
      <span><span class="big">${funnel.passed_policy}</span>passed policy</span>
      <span><span class="big">${funnel.posted}</span>posted</span>
      <span><span class="big">${funnel.retracted}</span>retracted</span>
    </div>`}
    <table class="table">
      <thead>
        <tr>
          <th style="width:64px">When</th>
          <th style="width:110px">Label</th>
          <th style="width:190px">Decision</th>
          <th>Message / claim</th>
          <th style="width:70px"></th>
        </tr>
      </thead>
      <tbody>
        ${rows.map(
          (row) => html`
            <tr key=${row.id}>
              <td class="text-muted">${wallClock(row.created_at)}</td>
              <td>${row.label && html`<${Tag} label=${row.label} />`}</td>
              <td>
                <${Tag}
                  className="tag tag-neutral"
                  decision=${row.status === "posted" || row.status === "dry_run"
                    ? row.status
                    : undefined}
                >
                  ${(DECISION_COPY[row.status] || ((r) => r.status))(row)}
                <//>
                ${row.retracted_at &&
                html`<span class="tag tag-neutral" style="margin-left:6px"
                  >retracted</span
                >`}
              </td>
              <td
                style="font-size:13px;color:var(--color-neutral-300);word-break:break-word"
              >
                ${row.message_text || row.reason}
              </td>
              <td>
                ${row.status === "posted" &&
                !row.retracted_at &&
                row.handle &&
                html`<button
                  class="btn btn-ghost"
                  style="font-size:12px"
                  onClick=${() => {
                    if (
                      confirm(
                        `Publicly retract check ${row.handle}? This posts a ` +
                          "retraction message and records the verdict as wrong."
                      )
                    )
                      retract(row.handle).then(() =>
                        refreshDecisions({ limit: 200 })
                      );
                  }}
                >
                  Retract
                </button>`}
              </td>
            </tr>
          `
        )}
      </tbody>
    </table>
    <div class="pager">
      showing ${rows.length} of ${decisions.value.length} loaded
      ${olderCursor === 0 && html`<span>· no older rows</span>`}
      <button
        class="btn btn-ghost"
        style="font-size:12px;margin-left:auto"
        onClick=${loadOlder}
      >
        Older →
      </button>
    </div>
  `;
}
