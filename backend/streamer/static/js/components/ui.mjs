// Thin wrappers over the Nocturne classes — never restyle them, compose them.
import { html } from "../html.mjs";
import { notice } from "../store.mjs";

export const Key = ({ children }) =>
  html`<kbd class="key-hint">${children}</kbd>`;

export const Tag = ({ label, decision, className = "tag", children }) =>
  html`<span
    class=${className}
    data-label=${label}
    data-decision=${decision}
    >${children ?? label}</span
  >`;

export function Seg({ name, options, value, onChange, compact }) {
  return html`<span class="seg">
    ${options.map(
      ([optionValue, optionLabel]) => html`
        <label
          class="seg-opt"
          key=${optionValue}
          style=${compact ? "padding:5px 10px;font-size:12px" : ""}
        >
          <input
            type="radio"
            name=${name}
            checked=${value === optionValue}
            onChange=${() => onChange(optionValue)}
          />
          <span>${optionLabel}</span>
        </label>
      `
    )}
  </span>`;
}

export const Toggle = ({ pressed, onToggle, title }) =>
  html`<button
    type="button"
    class="toggle"
    aria-pressed=${String(Boolean(pressed))}
    title=${title}
    onClick=${() => onToggle(!pressed)}
  ></button>`;

export const Pips = ({ used, cap }) =>
  html`<span class="pips" title=${`${used}/${cap} posts this hour`}>
    ${Array.from(
      { length: Math.max(cap, 0) },
      (_, index) =>
        html`<span
          class="pip"
          key=${index}
          data-filled=${index < used ? "" : undefined}
        ></span>`
    )}
  </span>`;

export const Dot = ({ on, warn, children }) =>
  html`<span class="dot" data-on=${on ? "" : undefined} data-warn=${
    warn ? "" : undefined
  }>${children}</span>`;

export function Notice() {
  const current = notice.value;
  if (!current) return null;
  return html`<div class="notice" data-kind=${current.kind}>
    ${current.text}
  </div>`;
}
