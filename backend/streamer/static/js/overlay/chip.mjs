// Style 1g — minimal chip: one line, near-zero screen cost. The ticket for
// "always on" overlays. Claim renders unquoted and relies on ellipsis.
import { html } from "../html.mjs";
import { sourceDomains } from "../format.mjs";
import { DwellBar, LABEL_MARKS } from "./shared.mjs";

export function Chip({ verdict, durationMs }) {
  const { shown, extra } = sourceDomains(verdict.sources, 1);
  const sourceText =
    shown.length > 0 ? `${shown[0]}${extra > 0 ? ` +${extra}` : ""}` : null;
  return html`
    <div class="ov-chip">
      <span class="ov-label-pill">
        ${LABEL_MARKS[verdict.label]} ${verdict.label}
      </span>
      <span class="ov-chip-claim">${verdict.claim.replace(/\.$/, "")}</span>
      ${sourceText && html`<span class="ov-chip-src">${sourceText}</span>`}
      <${DwellBar} durationMs=${durationMs} className="ov-chip-dwell" />
    </div>`;
}
