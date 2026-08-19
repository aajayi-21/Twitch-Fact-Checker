// On-stream overlay entry point.
//
// Config precedence (per KEY, not per document):
//   1. URL params present in the source URL are PINNED — they never move,
//      so a second OBS source can run ?style=chip forever;
//   2. everything else follows the server config (/overlay/config), which
//      the console updates live via an overlay_config hub push — style
//      switches apply to FUTURE toasts without a reload, and the OBS source
//      URL never changes.
//   3. labels stay NARROWING-ONLY: a URL can hide labels the server shows,
//      never surface ones it hides (intersection, exactly like before).
//
// The events socket subscribes WITHOUT a server-side channel filter and
// narrows client-side instead: a server filter would also drop the
// console's "Send test verdict" frames (reserved channel __test__) and the
// overlay_config pushes (no channel). Client-side narrowing keeps the same
// privacy posture — this page only ever RECEIVES verdicts.
//
// Reconnect policy: forever, 0.5 s -> 15 s backoff. An OBS source runs for
// eight hours; a give-up kills the overlay silently mid-broadcast.
// Connection state renders only under ?preview=1 — a red "disconnected"
// badge on a live broadcast is worse than showing nothing.

import { render } from "preact";
import { signal, computed } from "@preact/signals";
import { html } from "../html.mjs";
import { clampNum } from "../format.mjs";
import { STYLES, STYLE_NAMES } from "./styles.mjs";

const VALID_LABELS = ["TRUE", "FALSE", "MISLEADING", "UNVERIFIED"];
const DEFAULT_LABELS = ["FALSE", "MISLEADING"];
const VALID_POSITIONS = new Set([
  "top-left",
  "top-center",
  "top-right",
  "bottom-left",
  "bottom-center",
  "bottom-right",
]);
const EXIT_MS = 220;

// ---- URL params: whitelisted/clamped, and PINNED when present ------------ //
const params = new URLSearchParams(location.search);
const pinned = {};
if (STYLE_NAMES.includes(params.get("style"))) pinned.style = params.get("style");
if (VALID_POSITIONS.has(params.get("position")))
  pinned.position = params.get("position");
if (params.get("duration") !== null)
  pinned.duration_s = clampNum(params.get("duration"), 4, 60, 14);
if (params.get("max") !== null)
  pinned.max_stack = clampNum(params.get("max"), 1, 3, 1);
if (params.get("scale") !== null)
  pinned.scale = clampNum(params.get("scale"), 0.5, 3, 1);
if (params.get("margin") !== null)
  pinned.margin = clampNum(params.get("margin"), 0, 400, 56);
const urlLabels = (params.get("labels") || "")
  .toUpperCase()
  .split(",")
  .map((label) => label.trim())
  .filter((label) => VALID_LABELS.includes(label));
const channelParam = (params.get("channel") || "").trim().toLowerCase();
const offsetX = clampNum(params.get("offsetx"), -1000, 1000, 0);
const offsetY = clampNum(params.get("offsety"), -1000, 1000, 0);
const preview = params.get("preview") === "1";

const DEFAULT_CONFIG = {
  style: "toast",
  position: "bottom-left",
  duration_s: 14,
  labels: DEFAULT_LABELS,
  max_stack: 1,
  scale: 1,
  margin: 56,
};

// ---- state ---------------------------------------------------------------- //
const serverConfig = signal(DEFAULT_CONFIG);
const topicColors = signal({});
const topicLabels = signal({});
const toasts = signal([]); // [{key, verdict, phase}]
const wsState = signal("connecting");

const effective = computed(() => {
  const merged = { ...DEFAULT_CONFIG, ...serverConfig.value, ...pinned };
  // Labels: narrowing-only intersection with whatever the server allows.
  const serverLabels = serverConfig.value.labels || DEFAULT_LABELS;
  merged.labels = urlLabels.length
    ? serverLabels.filter((label) => urlLabels.includes(label))
    : serverLabels;
  return merged;
});

// ---- toast lifecycle ------------------------------------------------------- //
let nextKey = 1;
const timers = new Map();

function showVerdict(verdict) {
  const config = effective.value;
  if (!config.labels.includes(verdict.label)) return;
  const key = nextKey++;
  const maxStack = config.style === "lowerthird" ? 1 : config.max_stack;
  const kept = toasts.value.slice(-(maxStack - 1));
  toasts.value = [...kept, { key, verdict, phase: "enter" }];
  requestAnimationFrame(() =>
    requestAnimationFrame(() => {
      toasts.value = toasts.value.map((toast) =>
        toast.key === key ? { ...toast, phase: "in" } : toast
      );
    })
  );
  const dwellMs = config.duration_s * 1000;
  timers.set(
    key,
    setTimeout(() => {
      toasts.value = toasts.value.map((toast) =>
        toast.key === key ? { ...toast, phase: "out" } : toast
      );
      timers.set(
        key,
        setTimeout(() => {
          toasts.value = toasts.value.filter((toast) => toast.key !== key);
          timers.delete(key);
        }, EXIT_MS)
      );
    }, dwellMs)
  );
}

// ---- events socket --------------------------------------------------------- //
let backoffMs = 500;
function connect() {
  const socketParams = new URLSearchParams({
    types: "verdict,overlay_config",
  });
  if (params.get("token")) socketParams.set("token", params.get("token"));
  const socket = new WebSocket(`ws://${location.host}/ws/events?${socketParams}`);
  socket.onopen = () => {
    backoffMs = 500;
    wsState.value = "open";
  };
  socket.onmessage = (message) => {
    let payload;
    try {
      payload = JSON.parse(message.data);
    } catch {
      return;
    }
    if (payload.type !== "event") return;
    const frame = payload.frame || {};
    if (frame.type === "overlay_config" && frame.config) {
      serverConfig.value = { ...DEFAULT_CONFIG, ...frame.config };
      return;
    }
    if (frame.type === "verdict") {
      // Client-side channel narrowing; __test__ (the console's test
      // verdict) is always admitted so positioning is testable pre-stream.
      if (
        channelParam &&
        payload.channel !== channelParam &&
        payload.channel !== "__test__"
      )
        return;
      showVerdict(frame);
    }
  };
  socket.onclose = () => {
    wsState.value = "closed";
    setTimeout(connect, backoffMs);
    backoffMs = Math.min(backoffMs * 2, 15000);
  };
  socket.onerror = () => socket.close();
}

// ---- rendering -------------------------------------------------------------- //
function Anchor() {
  const config = effective.value;
  const Style = STYLES[config.style] || STYLES.toast;
  const [vertical, horizontal] = config.position.split("-");
  const style = {
    [vertical === "top" ? "top" : "bottom"]: `${config.margin + offsetY}px`,
    flexDirection: vertical === "bottom" ? "column-reverse" : "column",
  };
  if (config.style === "lowerthird") {
    style.left = `${config.margin + offsetX}px`;
    style.right = `${config.margin - offsetX}px`;
  } else if (horizontal === "center") {
    style.left = "50%";
    style.transform = `translateX(calc(-50% + ${offsetX}px))`;
  } else {
    style[horizontal] = `${config.margin + offsetX}px`;
  }
  return html`
    <div id="anchor" data-style=${config.style} style=${style}>
      ${toasts.value.map(
        (toast) => html`
          <div
            class="ov-item"
            key=${toast.key}
            data-phase=${toast.phase}
            data-label=${toast.verdict.label}
          >
            <${Style}
              verdict=${toast.verdict}
              durationMs=${config.duration_s * 1000}
              topicColor=${topicColors.value[toast.verdict.topic]}
              topicLabel=${topicLabels.value[toast.verdict.topic]}
            />
          </div>
        `
      )}
    </div>`;
}

function Badge() {
  return html`<div
    id="badge"
    data-state=${wsState.value === "open" ? "open" : "closed"}
  >
    ${wsState.value === "open"
      ? `overlay connected · ${effective.value.style}`
      : "overlay reconnecting…"}
  </div>`;
}

function App() {
  document.documentElement.style.fontSize = `${16 * effective.value.scale}px`;
  return html`<${Badge} /><${Anchor} />`;
}

// ---- boot -------------------------------------------------------------------- //
if (preview) document.body.classList.add("preview");

Promise.allSettled([
  fetch("/overlay/config").then((response) => response.json()),
  fetch("/meta/topics").then((response) => response.json()),
]).then(([configResult, topicsResult]) => {
  // Failure-tolerant: the overlay must render through a backend restart.
  if (configResult.status === "fulfilled") {
    serverConfig.value = { ...DEFAULT_CONFIG, ...configResult.value };
  }
  if (topicsResult.status === "fulfilled") {
    const colors = {};
    const labels = {};
    for (const topic of topicsResult.value.topics || []) {
      colors[topic.slug] = topic.color;
      labels[topic.slug] = topic.label;
    }
    topicColors.value = colors;
    topicLabels.value = labels;
  }
  render(html`<${App} />`, document.getElementById("root"));
  connect();
});
