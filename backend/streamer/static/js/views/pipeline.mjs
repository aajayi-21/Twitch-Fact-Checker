// Pipeline — the live session's knobs (the extension's options,
// console-shaped): sensitivity, topics-to-CHECK, vision, transcripts.
// Instant-apply; effective on the next gate pass; the CLI flags set the
// startup defaults. Distinct from Bot settings' topics-to-POST.
import { html } from "../html.mjs";
import {
  sessionConfig,
  topics,
  transcriptTail,
  savePipeline,
} from "../store.mjs";
import { Seg, Toggle } from "../components/ui.mjs";

export function Pipeline() {
  const config = sessionConfig.value;
  const session = (config?.sessions || [])[0];
  const enabled = new Set(session?.enabled_topics || []);
  const toggleTopic = (slug, on) => {
    const next = new Set(enabled);
    if (on) next.add(slug);
    else next.delete(slug);
    savePipeline({ enabled_topics: [...next] });
  };
  return html`
    <div class="view-head">
      <h4>Pipeline</h4>
      <span class="text-muted" style="font-size:12.5px"
        >the live session's checking behavior — instant-apply; CLI flags set
        the startup defaults</span
      >
    </div>
    ${!session &&
    html`<div class="card" style="margin-bottom:14px">
      <span class="card-meta"
        >No live session. Start the ingest CLI:
        <code style="user-select:all">uv run fact-checker-ingest twitch.tv/you</code></span
      >
    </div>`}
    <div class="two-col">
      <div class="card">
        <span class="card-kicker">Session</span>
        <div style="display:flex;align-items:center;gap:10px;font-size:13px">
          <span class="text-muted" style="width:90px">Sensitivity</span>
          <${Seg}
            compact
            name="pipe-sens"
            value=${session?.sensitivity}
            onChange=${(sensitivity) => savePipeline({ sensitivity })}
            options=${[
              ["low", "Low"],
              ["medium", "Medium"],
              ["high", "High"],
            ]}
          />
        </div>
        <span class="card-meta"
          >more sensitive = more claims checked = more spend</span
        >
        <div style="display:flex;align-items:center;gap:10px;font-size:13px">
          <span class="text-muted" style="flex:1"
            >Vision — use on-screen frames in verification</span
          >
          <${Toggle}
            pressed=${Boolean(config?.vision_enabled)}
            onToggle=${(on) => savePipeline({ vision_enabled: on })}
          />
        </div>
        <span class="card-meta"
          >frames arrive only if a client sends them (ingest --video, or the
          extension's toggle); 3-frame memory ring, never stored</span
        >
        <div style="display:flex;align-items:center;gap:10px;font-size:13px">
          <span class="text-muted" style="flex:1"
            >Live transcript (console only, never the overlay)</span
          >
          <${Toggle}
            pressed=${Boolean(session?.send_transcripts)}
            onToggle=${(on) => savePipeline({ send_transcripts: on })}
          />
        </div>
        ${session?.send_transcripts &&
        transcriptTail.value.length > 0 &&
        html`<div class="transcript-box" style="max-height:120px">
          ${transcriptTail.value.join(" ")}
        </div>`}
      </div>
      <div class="card">
        <span class="card-kicker">Topics to CHECK</span>
        <span class="card-meta"
          >gates what gets verified (spend); which of those may POST lives in
          Bot settings</span
        >
        <div style="display:flex;flex-direction:column;gap:6px;font-size:13.5px">
          ${topics.value.map((topic) => {
            const always = topic.slug === "other";
            return html`
              <label
                key=${topic.slug}
                style=${`display:flex;gap:8px;align-items:center;${
                  always ? "opacity:.6" : "cursor:pointer"
                }`}
              >
                <input
                  type="checkbox"
                  disabled=${always || !session}
                  checked=${always || enabled.has(topic.slug)}
                  onChange=${(event) =>
                    toggleTopic(topic.slug, event.target.checked)}
                />
                <span
                  class="topic-dot"
                  style=${`background:${topic.color}`}
                ></span>
                ${topic.label}
                ${always &&
                html`<span class="text-muted" style="font-size:11px"
                  >always on</span
                >`}
              </label>
            `;
          })}
        </div>
      </div>
    </div>
  `;
}
