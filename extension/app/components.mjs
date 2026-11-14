/**
 * Small presentational components shared by the app's screens.
 */

import {html, useEffect, useState} from "./lib/html.mjs";

/**
 * @param {object} props
 * @param {"primary"|"secondary"|"ghost"} [props.variant]
 * @param {boolean} [props.busy] - shows progress and blocks clicks
 */
export const Button = ({variant = "secondary", busy = false, disabled = false, children, ...rest}) => html`
  <button
    type="button"
    class=${`button button-${variant}`}
    disabled=${disabled || busy}
    aria-busy=${busy ? "true" : undefined}
    ...${rest}
  >
    ${busy ? html`<span class="spinner" aria-hidden="true"></span>` : null}${children}
  </button>
`;

/**
 * A coloured dot and a word. `flat` drops the chip around it, for a status
 * that sits in a row of facts.
 *
 * @param {{state: "ok"|"warn"|"bad"|"idle"|"busy", flat?: boolean, children: any}} props
 */
export const StatusPill = ({state, flat = false, children}) => html`
  <span class=${`pill${flat ? " pill-flat" : ""}`} data-state=${state}
    ><span class="pill-dot" aria-hidden="true"></span>${children}</span
  >
`;

/**
 * The mark: a D built from transcript lines, with the one flagged claim in
 * the accent colour. Decorative wherever the wordmark or a label sits by it.
 *
 * @param {{size?: number}} props - height in CSS pixels
 */
export const BrandMark = ({size = 29}) => html`
  <svg
    viewBox="34 26 62 76"
    width=${Math.round((size * 62) / 76)}
    height=${size}
    aria-hidden="true"
    focusable="false"
  >
    <g class="logo-ink" stroke-width="9">
      <path d="M40.5 34.5H70.4" />
      <path d="M40.5 49.25H84.1" />
      <path d="M40.5 64H87.5" />
      <path class="logo-accent" d="M40.5 78.75H84.1" />
      <path d="M40.5 93.5H70.4" />
    </g>
  </svg>
`;

/**
 * The wordmark "doxa", drawn rather than typeset: three round bowls and an
 * x whose rising stroke overshoots like a check mark, in the accent colour.
 *
 * @param {{size?: number}} props - height in CSS pixels
 */
export const BrandWordmark = ({size = 24}) => html`
  <svg viewBox="0 13 229 80" width=${Math.round((size * 229) / 80)} height=${size} role="img" aria-label="Doxa">
    <g class="logo-ink" stroke-width="9">
      <circle cx="30" cy="64" r="20" />
      <path d="M50 22V84" />
      <circle cx="89" cy="64" r="20" />
      <path d="M126 44L162 84" />
      <path class="logo-accent" d="M126 84L175 30" />
      <circle cx="199" cy="64" r="20" />
      <path d="M219 44V84" />
    </g>
  </svg>
`;

/** @param {{title?: string, aside?: any, children: any, tone?: string, id?: string}} props */
export const Card = ({title, aside, tone, id, children}) => html`
  <section class="card" data-tone=${tone} id=${id}>
    ${title || aside
      ? html`<header class="card-header">
          ${title ? html`<h2 class="card-title">${title}</h2>` : null}
          ${aside ? html`<div class="card-aside">${aside}</div>` : null}
        </header>`
      : null}
    ${children}
  </section>
`;

/** @param {{tone: "info"|"warn"|"error"|"success", children: any}} props */
export const Notice = ({tone, children}) => html`
  <p class="notice" data-tone=${tone} role=${tone === "error" ? "alert" : "status"}>${children}</p>
`;

/**
 * A labelled group of mutually exclusive options.
 *
 * @param {object} props
 * @param {string} props.name
 * @param {string} props.label - accessible name of the group
 * @param {{value: string, label: string, hint?: string}[]} props.options
 * @param {string} props.value
 * @param {(value: string) => void} props.onChange
 * @param {boolean} [props.compact] - a short row of plain options, no hints
 */
export const Segmented = ({name, label, options, value, onChange, compact = false}) => html`
  <div class=${`segmented${compact ? " segmented-compact" : ""}`} role="radiogroup" aria-label=${label}>
    ${options.map(
      (option) => html`
        <label class="segment" key=${option.value}>
          <input
            type="radio"
            name=${name}
            value=${option.value}
            checked=${option.value === value}
            onChange=${() => onChange(option.value)}
          />
          <span class="segment-body">
            <span class="segment-label">${option.label}</span>
            ${option.hint ? html`<span class="segment-hint">${option.hint}</span>` : null}
          </span>
        </label>
      `
    )}
  </div>
`;

/**
 * A setting row: label and help on the left, the control on the right (or
 * below, with `stacked`).
 */
export const SettingRow = ({label, help, htmlFor, stacked = false, children}) => html`
  <div class=${`setting-row${stacked ? " setting-row-stacked" : ""}`}>
    <div class="setting-text">
      ${htmlFor
        ? html`<label class="setting-label" for=${htmlFor}>${label}</label>`
        : html`<span class="setting-label">${label}</span>`}
      ${help ? html`<p class="setting-help">${help}</p>` : null}
    </div>
    <div class="setting-control">${children}</div>
  </div>
`;

/** A switch. `label` is read by screen readers when no visible label is tied to it. */
export const Toggle = ({id, checked, onChange, label}) => html`
  <label class="toggle">
    <input
      id=${id}
      type="checkbox"
      role="switch"
      checked=${checked}
      aria-label=${label}
      onChange=${(event) => onChange(event.currentTarget.checked)}
    />
    <span class="toggle-track" aria-hidden="true"><span class="toggle-thumb"></span></span>
  </label>
`;

/**
 * A button for something that cannot be undone: the first click arms it and
 * changes its label, a second click within a few seconds does the deed.
 *
 * @param {object} props
 * @param {string} props.confirmLabel - shown while armed, e.g. "Delete for good?"
 * @param {() => void|Promise<void>} props.onConfirm
 */
export const ConfirmButton = ({confirmLabel, onConfirm, children, ...rest}) => {
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!armed) {
      return undefined;
    }
    const timer = setTimeout(() => setArmed(false), 4000);
    return () => clearTimeout(timer);
  }, [armed]);
  const onClick = async () => {
    if (!armed) {
      setArmed(true);
      return;
    }
    setBusy(true);
    try {
      await onConfirm();
    } finally {
      setBusy(false);
      setArmed(false);
    }
  };
  return html`
    <${Button}
      variant=${armed ? "danger" : "ghost"}
      busy=${busy}
      onClick=${onClick}
      onBlur=${() => setArmed(false)}
      ...${rest}
    >
      ${armed ? confirmLabel : children}
    <//>
  `;
};

/** The coloured TRUE / FALSE / MISLEADING / UNVERIFIED chip. */
export const LabelChip = ({label}) => html`
  <span class="verdict-label" data-label=${label}>${label}</span>
`;

/**
 * A row of tabs that switch what is shown below them.
 *
 * @param {object} props
 * @param {string} props.label - accessible name
 * @param {{id: string, label: string, count?: number}[]} props.tabs
 * @param {string} props.active
 * @param {(id: string) => void} props.onSelect
 */
export const Tabs = ({label, tabs, active, onSelect}) => html`
  <div class="tabs" role="tablist" aria-label=${label}>
    ${tabs.map(
      (tab) => html`
        <button
          key=${tab.id}
          type="button"
          role="tab"
          class="tab"
          id=${`tab-${tab.id}`}
          aria-selected=${tab.id === active ? "true" : "false"}
          aria-controls=${`panel-${tab.id}`}
          tabindex=${tab.id === active ? 0 : -1}
          onClick=${() => onSelect(tab.id)}
          onKeyDown=${(event) => {
            const step = {ArrowRight: 1, ArrowLeft: -1}[event.key];
            if (!step) {
              return;
            }
            const index = tabs.findIndex((candidate) => candidate.id === active);
            const next = tabs[(index + step + tabs.length) % tabs.length];
            onSelect(next.id);
            document.getElementById(`tab-${next.id}`)?.focus();
          }}
        >
          ${tab.label}${Number.isFinite(tab.count)
            ? html`<span class="tab-count">${tab.count}</span>`
            : null}
        </button>
      `
    )}
  </div>
`;

/** Opens in a new tab without handing the page a reference back to the app. */
export const ExternalLink = ({href, children}) => html`
  <a href=${href} target="_blank" rel="noopener noreferrer">${children}</a>
`;
