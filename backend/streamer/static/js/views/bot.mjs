// Bot settings — the full posting policy, explicit-Apply (these knobs are
// money- and safety-shaped). Clamp violations come back as a 400 whose exact
// reason is shown verbatim: refused loudly, never rewritten client-side.
import { useState } from "preact/hooks";
import { html } from "../html.mjs";
import {
  bot,
  topics,
  saveBotSettings,
  setTrusted,
  dryRun,
  setDryRun,
} from "../store.mjs";
import { Seg } from "../components/ui.mjs";

const POST_LABELS = ["FALSE", "MISLEADING", "TRUE"];

function NumberField({ label, hint, value, min, max, step, onInput }) {
  return html`
    <label class="field">
      <span style="font-size:12px" class="text-muted">${label}</span>
      <input
        class="input"
        type="number"
        min=${min}
        max=${max}
        step=${step || 1}
        value=${value}
        onInput=${(event) => onInput(Number(event.target.value))}
      />
      ${hint && html`<span class="card-meta">${hint}</span>`}
    </label>
  `;
}

export function Bot() {
  const currentBot = bot.value;
  const [draft, setDraft] = useState(null);
  if (!currentBot?.settings) {
    return html`<div class="view-head"><h4>Bot settings</h4></div>
      <div class="card">
        <span class="card-meta"
          >Connect Twitch first (Setup) — settings apply to the running
          bot.</span
        >
      </div>`;
  }
  const config = draft ?? currentBot.settings;
  const edit = (patch) => setDraft({ ...config, ...patch });
  const apply = async () => {
    if (await saveBotSettings(config)) setDraft(null);
  };
  const probation = currentBot.probation || {};
  return html`
    <div class="view-head">
      <h4>Bot settings</h4>
      <span class="text-muted" style="font-size:12.5px"
        >what may post, how often, how confident — hard clamps are refused
        with the reason, never silently rewritten</span
      >
    </div>
    <div class="two-col">
      <div class="card">
        <span class="card-kicker">Labels that may post</span>
        <div style="display:flex;gap:16px;font-size:13.5px">
          ${POST_LABELS.map(
            (label) => html`
              <label
                key=${label}
                style="display:flex;gap:6px;align-items:center;cursor:pointer"
              >
                <input
                  type="checkbox"
                  checked=${config.labels.includes(label)}
                  onChange=${(event) =>
                    edit({
                      labels: event.target.checked
                        ? [...config.labels, label]
                        : config.labels.filter((entry) => entry !== label),
                    })}
                />
                ${label}
              </label>
            `
          )}
        </div>
        <span class="card-meta"
          >UNVERIFIED can never post — that is not a setting. TRUE is off by
          default: corrections are the format.</span
        >
        <span class="card-kicker" style="margin-top:8px">Message shape</span>
        <div style="display:flex;gap:10px;align-items:center;font-size:13px">
          <span class="text-muted" style="width:74px">Template</span>
          <${Seg}
            compact
            name="template"
            value=${config.template}
            onChange=${(template) => edit({ template })}
            options=${[
              ["standard", "Standard"],
              ["compact", "Compact"],
              ["verbose", "Verbose"],
            ]}
          />
        </div>
        <div style="display:flex;gap:10px;align-items:center;font-size:13px">
          <span class="text-muted" style="width:74px">Sources</span>
          <${Seg}
            compact
            name="sources-style"
            value=${config.sources_style}
            onChange=${(style) => edit({ sources_style: style })}
            options=${[
              ["domain", "Domains"],
              ["url", "Full URL"],
            ]}
          />
        </div>
        <span class="card-kicker" style="margin-top:8px">Topics that may POST</span>
        <div style="display:flex;flex-wrap:wrap;gap:8px 16px;font-size:13px">
          ${topics.value.map(
            (topic) => html`
              <label
                key=${topic.slug}
                style="display:flex;gap:6px;align-items:center;cursor:pointer"
              >
                <input
                  type="checkbox"
                  checked=${config.topics.includes(topic.slug)}
                  onChange=${(event) =>
                    edit({
                      topics: event.target.checked
                        ? [...config.topics, topic.slug]
                        : config.topics.filter((slug) => slug !== topic.slug),
                    })}
                />
                <span
                  class="topic-dot"
                  style=${`background:${topic.color}`}
                ></span>
                ${topic.label}
              </label>
            `
          )}
        </div>
      </div>
      <div class="card">
        <span class="card-kicker">Pace</span>
        <div class="form-grid">
          <${NumberField}
            label="Posts per hour (1–12)"
            value=${config.posts_per_hour}
            min="1"
            max="12"
            onInput=${(value) => edit({ posts_per_hour: value })}
          />
          <${NumberField}
            label="Min gap between posts, s (≥45)"
            value=${config.min_gap_s}
            min="45"
            max="1800"
            onInput=${(value) => edit({ min_gap_s: value })}
          />
          <${NumberField}
            label="Same-claim cooldown, s (≥600)"
            value=${config.claim_cooldown_s}
            min="600"
            max="86400"
            onInput=${(value) => edit({ claim_cooldown_s: value })}
          />
          <${NumberField}
            label="Same-topic cooldown, s"
            value=${config.topic_cooldown_s}
            min="0"
            max="3600"
            onInput=${(value) => edit({ topic_cooldown_s: value })}
          />
        </div>
        <span class="card-meta"
          >an un-configurable 3-posts-per-10-minutes guard sits under all of
          this</span
        >
        <span class="card-kicker" style="margin-top:8px">Confidence bar</span>
        <div class="form-grid">
          <${NumberField}
            label="Min check-worthiness (0.60–0.95)"
            value=${config.min_check_worthiness}
            min="0.60"
            max="0.95"
            step="0.01"
            onInput=${(value) => edit({ min_check_worthiness: value })}
          />
          <${NumberField}
            label="Min citations (2–5)"
            value=${config.min_sources}
            min="2"
            max="5"
            onInput=${(value) => edit({ min_sources: value })}
          />
          <${NumberField}
            label="Max claim age, s (20–180)"
            value=${config.max_claim_age_s}
            min="20"
            max="180"
            onInput=${(value) => edit({ max_claim_age_s: value })}
          />
        </div>
        <span class="card-meta"
          >floors that always hold: ≥2 distinct domains, best source tier
          A/B, no tier-D source, never fallback-parsed verdicts, tier-A for
          politics/health</span
        >
      </div>
    </div>
    <div class="q-actions" style="margin-top:14px">
      <button class="btn btn-primary" disabled=${!draft} onClick=${apply}>
        Apply settings
      </button>
      ${draft &&
      html`<button class="btn btn-ghost" onClick=${() => setDraft(null)}>
        Discard changes
      </button>`}
    </div>
    <div class="card" style="margin-top:14px">
      <span class="card-kicker">Probation & go-live</span>
      <div style="display:flex;gap:12px;align-items:center;font-size:13px;flex-wrap:wrap">
        <span class="text-muted">
          ${probation.active
            ? `Review-mode probation: ${probation.approved_posts || 0}/10 approvals` +
              ` (${probation.retractions || 0} retractions)`
            : "Probation passed — auto mode available"}
        </span>
        <button
          class="btn btn-secondary"
          onClick=${() => {
            const message = probation.active
              ? "End probation? The bot may then AUTO-POST without human " +
                "approval (in auto mode, once dry run is off)."
              : "Reinstate probation? New posts queue for approval again.";
            if (confirm(message)) setTrusted(Boolean(probation.active));
          }}
        >
          ${probation.active ? "End probation" : "Reinstate probation"}
        </button>
        <span style="margin-left:auto"></span>
        <button
          class="btn btn-secondary"
          onClick=${() => {
            if (dryRun.value) {
              if (
                confirm(
                  "Turn DRY RUN off? The bot will actually post to chat."
                )
              )
                setDryRun(false);
            } else setDryRun(true);
          }}
        >
          ${dryRun.value ? "Go live (dry run is ON)" : "Back to dry run"}
        </button>
      </div>
    </div>
  `;
}
