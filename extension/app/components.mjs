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

/** @param {{state: "ok"|"warn"|"bad"|"idle"|"busy", children: any}} props */
export const StatusPill = ({state, children}) => html`
  <span class="pill" data-state=${state}><span class="pill-dot" aria-hidden="true"></span>${children}</span>
`;

/** @param {{title?: string, aside?: any, children: any, tone?: string}} props */
export const Card = ({title, aside, tone, children}) => html`
  <section class="card" data-tone=${tone}>
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
 */
export const Segmented = ({name, label, options, value, onChange}) => html`
  <div class="segmented" role="radiogroup" aria-label=${label}>
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
