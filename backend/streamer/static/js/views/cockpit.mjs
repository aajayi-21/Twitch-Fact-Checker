// Design 1a — the live cockpit: mute, mode and the review queue ARE the
// page; everything else is a rail.
import { html } from "../html.mjs";
import {
  health,
  botStatus,
  bot,
  dryRun,
  sessionConfig,
  consoleStats,
  decisions,
  transcriptTail,
  nowMs,
  setMode,
  setMute,
  setDryRun,
  savePipeline,
  refreshDecisions,
} from "../store.mjs";
import { navigate } from "../router.mjs";
import { hms, wallClock } from "../format.mjs";
import { Key, Seg, Toggle, Pips, Dot, Tag } from "../components/ui.mjs";
import { ReviewQueue } from "../components/queue.mjs";
import { useEffect } from "preact/hooks";

function IdentityBar() {
  const status = botStatus.value;
  const currentBot = bot.value;
  const liveSession = (consoleStats.value?.live || [])[0];
  let elapsed = null;
  if (liveSession?.started_at) {
    nowMs.value; // 1 Hz repaint
    elapsed = hms((Date.now() - Date.parse(liveSession.started_at)) / 1000);
  }
  return html`
    <div class="identity">
      <span class="brand"
        ><span class="brand-mark">✓</span>Fact-Checker
        <span class="text-muted" style="font-size:13px">streamer console</span>
      </span>
      <span class="who">
        <${Dot} on=${Boolean(health.value)}>backend<//>
        <${Dot} on=${(status?.live_sessions || []).length > 0}>ingest<//>
        <${Dot} on=${Boolean(currentBot?.connected)}
          >chat${status?.bot_login ? ` · ${status.bot_login}` : ""}<//
        >
      </span>
      <span style="font-size:12px;color:var(--color-neutral-500)">
        ${status?.channel
          ? html`twitch.tv/<span style="color:var(--color-text)"
                >${status.channel}</span
              >`
          : "no channel yet"}
        ${elapsed ? ` · live ${elapsed}` : ""}
      </span>
    </div>
    <div class="fade-rule"></div>
  `;
}

export function CommandRow() {
  const currentBot = bot.value;
  const muted = Boolean(currentBot?.muted);
  return html`
    <div class="command">
      <button
        class="btn btn-primary mute-btn"
        data-muted=${String(muted)}
        disabled=${!currentBot}
        onClick=${() => setMute(!muted)}
      >
        ${muted ? "MUTED — unmute" : "MUTE"} <${Key}>M<//>
      </button>
      <${Seg}
        name="mode"
        value=${currentBot?.mode}
        onChange=${setMode}
        options=${[
          ["auto", "Auto"],
          ["review", "Review"],
          ["off", "Off"],
        ]}
      />
      ${currentBot &&
      html`<span
        style="display:flex;align-items:center;gap:8px;font-size:12px;color:var(--color-neutral-500)"
      >
        posts this hour
        <${Pips}
          used=${currentBot.posts_this_hour}
          cap=${currentBot.posts_per_hour}
        />
        <span
          style="font-variant-numeric:tabular-nums;color:var(--color-text)"
          >${currentBot.posts_this_hour}/${currentBot.posts_per_hour}</span
        >
        ${currentBot.latched && html`<${Tag} className="tag" label="MISLEADING">LATCHED<//>`}
      </span>`}
      ${dryRun.value &&
      html`<span class="dry-banner">
        DRY RUN — evaluating, posting nothing
        <button
          class="btn btn-secondary"
          style="font-size:12px;padding:4px 12px"
          onClick=${() => {
            if (
              confirm(
                "Turn DRY RUN off? The bot will actually post to chat.\n\n" +
                  "Only do this after reading a full stream's worth of " +
                  "would-have-posted messages in Decisions."
              )
            )
              setDryRun(false);
          }}
        >
          Go live
        </button>
      </span>`}
    </div>
  `;
}

function SessionCard() {
  const funnel = consoleStats.value?.funnel_today;
  const spend = health.value?.est_cost_today_usd;
  return html`
    <div class="card">
      <span class="card-kicker">Session</span>
      <div class="kv">
        <span class="text-muted">Claims heard</span>
        <span class="num">${funnel?.heard ?? "–"}</span>
        <span class="text-muted">Gate passed</span>
        <span class="num">${funnel?.gate_passed ?? "–"}</span>
        <span class="text-muted">Checked</span>
        <span class="num">${funnel?.checked ?? "–"}</span>
        <span class="text-muted">Posted</span>
        <span class="num">${funnel?.posted ?? "–"}</span>
        <span class="text-muted">Est. spend</span>
        <span class="num">${spend != null ? `$${spend.toFixed(2)}` : "–"}</span>
      </div>
    </div>
  `;
}

function PipelineCard() {
  const config = sessionConfig.value;
  const session = (config?.sessions || [])[0];
  return html`
    <div class="card">
      <span class="card-kicker">Pipeline</span>
      <div style="display:flex;flex-direction:column;gap:10px;font-size:13px">
        <div style="display:flex;align-items:center;gap:10px">
          <span class="text-muted" style="width:74px">Sensitivity</span>
          <${Seg}
            compact
            name="sens"
            value=${session?.sensitivity}
            onChange=${(sensitivity) => savePipeline({ sensitivity })}
            options=${[
              ["low", "Low"],
              ["medium", "Med"],
              ["high", "High"],
            ]}
          />
        </div>
        <div style="display:flex;align-items:center;gap:10px">
          <span class="text-muted" style="flex:1"
            >Vision — use on-screen frames</span
          >
          <${Toggle}
            pressed=${Boolean(config?.vision_enabled)}
            onToggle=${(on) => savePipeline({ vision_enabled: on })}
          />
        </div>
        <div style="display:flex;align-items:center;gap:10px">
          <span class="text-muted" style="flex:1"
            >Live transcript (panel only)</span
          >
          <${Toggle}
            pressed=${Boolean(session?.send_transcripts)}
            onToggle=${(on) => savePipeline({ send_transcripts: on })}
          />
        </div>
      </div>
      ${session?.send_transcripts &&
      transcriptTail.value.length > 0 &&
      html`<div class="transcript-box">
        …${transcriptTail.value.slice(-3).join(" ")}
      </div>`}
      ${!session &&
      html`<span class="card-meta"
        >no live session — start the ingest CLI</span
      >`}
    </div>
  `;
}

function LastPostedCard() {
  const posted = decisions.value
    .filter((row) => row.status === "posted")
    .slice(0, 2);
  return html`
    <div class="card">
      <span class="card-kicker">Last posted</span>
      ${posted.length === 0 &&
      html`<span class="card-meta">nothing posted yet</span>`}
      <div style="display:flex;flex-direction:column;gap:8px;font-size:12.5px">
        ${posted.map(
          (row) => html`
            <div
              key=${row.id}
              style="display:flex;gap:8px;align-items:center"
            >
              <${Tag} label=${row.label} className="tag" />
              <span
                class="text-muted"
                style="flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis"
                >${(row.message_text || "").replace(/^[^"“]*[""]?/, "")}</span
              >
              <span style="color:var(--color-neutral-600)"
                >${wallClock(row.posted_at || row.created_at)}</span
              >
            </div>
          `
        )}
      </div>
      <a
        href="#/decisions"
        style="font-size:12px"
        onClick=${(event) => {
          event.preventDefault();
          navigate("decisions");
        }}
        >All decisions →</a
      >
    </div>
  `;
}

export function Cockpit() {
  useEffect(() => {
    refreshDecisions({ limit: 50 });
  }, []);
  return html`
    <${IdentityBar} />
    <${CommandRow} />
    <div class="cockpit">
      <${ReviewQueue} />
      <div class="rail">
        <${SessionCard} />
        <${PipelineCard} />
        <${LastPostedCard} />
      </div>
    </div>
  `;
}
