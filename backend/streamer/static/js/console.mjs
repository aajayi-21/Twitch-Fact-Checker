// Console entry point: boot the store, start the router/keys/socket, render.
import { render } from "preact";
import { html } from "./html.mjs";
import { boot } from "./store.mjs";
import { connectEvents } from "./ws.mjs";
import { route, startRouter } from "./router.mjs";
import { installKeys } from "./keys.mjs";
import { Shell } from "./components/shell.mjs";
import { Notice } from "./components/ui.mjs";
import { Cockpit } from "./views/cockpit.mjs";
import { Setup } from "./views/setup.mjs";
import { Pipeline } from "./views/pipeline.mjs";
import { Bot } from "./views/bot.mjs";
import { Decisions } from "./views/decisions.mjs";
import { Analytics } from "./views/analytics.mjs";
import { Dock } from "./views/dock.mjs";

const VIEW_COMPONENTS = {
  cockpit: Cockpit,
  setup: Setup,
  pipeline: Pipeline,
  bot: Bot,
  decisions: Decisions,
  analytics: Analytics,
};

function App() {
  if (route.value === "dock") {
    // The dock renders full-bleed with no sidebar — it lives in a 340px
    // OBS Custom Browser Dock.
    return html`<${Dock} /><${Notice} />`;
  }
  const View = VIEW_COMPONENTS[route.value] ?? Cockpit;
  return html`<${Shell}><${View} /><//><${Notice} />`;
}

startRouter();
installKeys();
boot();
connectEvents();
render(html`<${App} />`, document.getElementById("root"));
