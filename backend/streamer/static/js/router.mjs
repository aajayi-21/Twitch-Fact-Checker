// Hash routing: #/cockpit (default), #/setup, #/pipeline, #/bot,
// #/decisions, #/analytics, #/dock. ?dock=1 is an alias for OBS dock URL
// fields; the dock view renders without the sidebar shell.
import { signal } from "@preact/signals";

export const VIEWS = [
  "cockpit",
  "setup",
  "pipeline",
  "bot",
  "decisions",
  "analytics",
  "dock",
];

export const route = signal("cockpit");

function parse() {
  const hash = location.hash.replace(/^#\/?/, "").split("?")[0];
  return VIEWS.includes(hash) ? hash : "cockpit";
}

export function startRouter() {
  if (new URLSearchParams(location.search).get("dock") === "1") {
    location.hash = "#/dock";
  }
  route.value = parse();
  window.addEventListener("hashchange", () => (route.value = parse()));
}

export const navigate = (view) => {
  location.hash = `#/${view}`;
};
