/**
 * The extension's full-tab app: welcome flow, home, history, usage, AI
 * provider, settings.
 *
 * Opens in a new tab on install and from every "settings" entry point
 * (toolbar popup, overlay, chrome://extensions). No build step: Preact and
 * htm are vendored as ES modules under extension/vendor.
 */

import {StatusPill} from "./components.mjs";
import {initApp, useAppState} from "./lib/app_store.mjs";
import {initLedger} from "./lib/ledger_store.mjs";
import {initSpeech} from "./lib/speech_actions.mjs";
import {html, render, useEffect, useState} from "./lib/html.mjs";
import {ROUTES, landingRoute, parseRoute} from "./lib/routes.mjs";
import {HistoryView} from "./views/history.mjs";
import {HomeView} from "./views/home.mjs";
import {ProvidersView} from "./views/providers.mjs";
import {SettingsView} from "./views/settings.mjs";
import {UsageView} from "./views/usage.mjs";
import {WelcomeView} from "./views/welcome.mjs";

// Promise-safe API namespace (see shared/capabilities.js § namespace idiom).
const chrome = globalThis.browser ?? globalThis.chrome;

const NAV_ITEMS = Object.freeze([
  {route: ROUTES.HOME, label: "Home"},
  {route: ROUTES.HISTORY, label: "History"},
  {route: ROUTES.USAGE, label: "Usage"},
  {route: ROUTES.PROVIDERS, label: "AI provider"},
  {route: ROUTES.SETTINGS, label: "Settings"},
]);

const VIEWS = Object.freeze({
  [ROUTES.HOME]: HomeView,
  [ROUTES.HISTORY]: HistoryView,
  [ROUTES.USAGE]: UsageView,
  [ROUTES.PROVIDERS]: ProvidersView,
  [ROUTES.SETTINGS]: SettingsView,
});

const navigate = (route) => {
  location.hash = `#${route}`;
};

/** The current route, following the URL hash. Null until a route is chosen. */
const useRoute = () => {
  const [route, setRoute] = useState(() => parseRoute(location.hash));
  useEffect(() => {
    const onHashChange = () => setRoute(parseRoute(location.hash));
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);
  return route;
};

const Sidebar = ({route}) => {
  const {backendState, setup} = useAppState();
  const up = backendState === "up";
  return html`
    <aside class="sidebar">
      <a class="brand" href=${`#${ROUTES.HOME}`}>
        <img src="../icons/icon48.png" width="28" height="28" alt="" />
        <span class="brand-name">Live Stream<br />Fact-Checker</span>
      </a>
      <nav class="nav" aria-label="Sections">
        ${NAV_ITEMS.map(
          (item) => html`
            <a
              key=${item.route}
              class="nav-link"
              href=${`#${item.route}`}
              aria-current=${item.route === route ? "page" : undefined}
            >
              ${item.label}
            </a>
          `
        )}
      </nav>
      <div class="sidebar-footer">
        <${StatusPill} state=${up ? (setup?.configured ? "ok" : "warn") : backendState === "checking" ? "busy" : "bad"}>
          ${up
            ? setup?.configured
              ? "Ready to check"
              : "Needs an API key"
            : backendState === "checking"
              ? "Checking backend…"
              : "Backend not running"}
        <//>
        <a class="sidebar-link" href=${`#${ROUTES.WELCOME}`}>Setup guide</a>
        <span class="version">Version ${chrome.runtime.getManifest().version}</span>
      </div>
    </aside>
  `;
};

const App = () => {
  const {ready, onboarding, setup, backendState} = useAppState();
  const route = useRoute();

  // A visit with no (valid) route is sent to welcome or home — but only once
  // the backend has answered or been found absent, so an existing user is
  // not flashed the welcome flow while the first status request is in flight.
  const canDecide = ready && backendState !== "checking";
  useEffect(() => {
    if (route === null && canDecide) {
      navigate(
        landingRoute({
          onboardingCompleted: Boolean(onboarding.completedAt),
          providerConfigured: Boolean(setup?.configured),
        })
      );
    }
  }, [route, canDecide]);

  useEffect(() => {
    const label = NAV_ITEMS.find((item) => item.route === route)?.label;
    document.title = `${route === ROUTES.WELCOME ? "Welcome" : (label ?? "Loading")} — Live Stream Fact-Checker`;
    // Move focus with the page so keyboard and screen-reader users land on
    // the new content, not on the link they just used.
    document.querySelector("main")?.focus();
  }, [route]);

  if (!ready || route === null) {
    return html`<p class="boot-message">Loading…</p>`;
  }
  if (route === ROUTES.WELCOME) {
    return html`
      <main class="welcome-main" tabindex="-1">
        <${WelcomeView} navigate=${navigate} />
      </main>
    `;
  }
  const View = VIEWS[route];
  return html`
    <div class="layout">
      <${Sidebar} route=${route} />
      <main class="content" tabindex="-1">
        <${View} navigate=${navigate} />
      </main>
    </div>
  `;
};

const root = document.getElementById("app");
root.textContent = "";
render(html`<${App} />`, root);
initApp().catch((error) => {
  console.error("[fact-checker] app failed to start:", error);
  root.textContent = "The app could not start. Reload this tab, or reload the extension.";
});
// The history loads on its own: a problem reading it is shown on the History
// and Usage screens and must not stop the rest of the app from starting.
initLedger().catch((error) => {
  console.error("[fact-checker] history failed to start:", error);
});
initSpeech().catch((error) => {
  console.error("[fact-checker] speech settings failed to load:", error);
});
