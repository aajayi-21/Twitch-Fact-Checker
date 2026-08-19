// Style 1e — refined toast: filled label pill, verdict-colored dwell bar.
import { html } from "../html.mjs";
import { sourceDomains } from "../format.mjs";
import { DwellBar, LABEL_MARKS } from "./shared.mjs";

export function Toast({ verdict, durationMs }) {
  const { shown } = sourceDomains(verdict.sources, 2);
  return html`
    <article class="ov-toast">
      <div class="ov-toast-head">
        <span class="ov-label-pill">
          ${LABEL_MARKS[verdict.label]} ${verdict.label}
        </span>
        <span class="ov-kicker">Fact check</span>
        <span class="ov-bot">🤖 !fc why ${verdict.id.slice(0, 4)}</span>
      </div>
      <p class="ov-claim">“${verdict.claim}”</p>
      ${verdict.explanation &&
      html`<p class="ov-expl">${verdict.explanation}</p>`}
      ${shown.length > 0 &&
      html`<div class="ov-sources">
        ${shown.map(
          (domain) => html`<span class="ov-source" key=${domain}>${domain}</span>`
        )}
      </div>`}
      <${DwellBar} durationMs=${durationMs} />
    </article>`;
}
