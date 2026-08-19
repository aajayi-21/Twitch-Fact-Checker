// Style 1h — verdict stamp: purple header band, oversized outline-stroke
// watermark bleeding off the corner, rotated stamped seal.
import { html } from "../html.mjs";
import { sourceDomains } from "../format.mjs";
import { DwellBar } from "./shared.mjs";

export function Stamp({ verdict, durationMs }) {
  const { shown } = sourceDomains(verdict.sources, 2);
  return html`
    <article class="ov-stamp">
      <div class="ov-stamp-band">
        <span class="ov-kicker">⛨ Live fact check</span>
        <span class="ov-bot">🤖 !fc why ${verdict.id.slice(0, 4)}</span>
      </div>
      <div class="ov-stamp-body">
        <span class="ov-stamp-water" aria-hidden="true">${verdict.label}</span>
        <p class="ov-claim">“${verdict.claim}”</p>
        ${verdict.explanation &&
        html`<p class="ov-expl">${verdict.explanation}</p>`}
        ${shown.length > 0 &&
        html`<div class="ov-sources">
          ${shown.map(
            (domain) =>
              html`<span class="ov-source" key=${domain}>${domain}</span>`
          )}
        </div>`}
        <span class="ov-stamp-seal">${verdict.label}</span>
      </div>
      <${DwellBar} durationMs=${durationMs} />
    </article>`;
}
