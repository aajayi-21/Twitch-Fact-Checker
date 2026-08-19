// Style 1f — broadcast-news lower-third: angled verdict block, one-line
// claim + explanation, sources column. Forces a full-width band (main.mjs
// pins max_stack to 1 and the anchor spans the margins for this style).
import { html } from "../html.mjs";
import { sourceDomains } from "../format.mjs";
import { DwellBar, LABEL_MARKS } from "./shared.mjs";

export function LowerThird({ verdict, durationMs, topicLabel }) {
  const { shown } = sourceDomains(verdict.sources, 2);
  return html`
    <div class="ov-lowerthird">
      <div class="ov-lt-block">
        <span class="ov-lt-mark">${LABEL_MARKS[verdict.label]}</span>
        <span class="ov-lt-word">${verdict.label}</span>
      </div>
      <div class="ov-lt-main">
        <span class="ov-kicker">
          Fact check${topicLabel ? ` · ${topicLabel}` : ""}
        </span>
        <p class="ov-lt-claim">${verdict.claim}</p>
        ${verdict.explanation &&
        html`<p class="ov-lt-expl">${verdict.explanation}</p>`}
      </div>
      <div class="ov-lt-side">
        ${shown.map((domain) => html`<span key=${domain}>${domain}</span>`)}
        <span class="ov-bot">🤖 !fc why ${verdict.id.slice(0, 4)}</span>
      </div>
      <${DwellBar} durationMs=${durationMs} />
    </div>`;
}
