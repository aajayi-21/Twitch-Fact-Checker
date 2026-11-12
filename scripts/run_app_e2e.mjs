#!/usr/bin/env node
/**
 * End-to-end test of the extension's app (app/app.html) in a real Chrome.
 *
 * Loads the unpacked extension and walks the app the way a new user would:
 * the welcome tab opens on install, the backend is found once it starts, a
 * wrong key is refused and a good one accepted, the sample check shows a
 * verdict, settings persist to chrome.storage, and the popup reuses the open
 * app tab. Screenshots of every screen are saved for review.
 *
 * The backend is a small fake built into this script (same routes and
 * response shapes as backend/app/setup.py and friends), so the run needs no
 * API key, makes no provider calls and never touches backend/.env. Use
 * --real-backend to point it at a real one instead: the steps that need a
 * working key are skipped, and in exchange the production capture path is
 * exercised — Start on a (silent) tab must reach "capturing" through the
 * service worker, the offscreen document and the backend's WebSocket. The
 * tab is silent so that no claims are heard and no provider calls are made.
 *
 * No dependencies: Node 22+ and a local Chrome.
 *
 * Usage (from the repo root):
 *
 *     node scripts/run_app_e2e.mjs
 *     node scripts/run_app_e2e.mjs --headed
 *     node scripts/run_app_e2e.mjs --real-backend http://127.0.0.1:8732
 *
 * Options:
 *   --headed               show the browser window
 *   --real-backend ORIGIN  use a running backend at this http origin
 *   --screens DIR          where to save screenshots (default .stt-bench/screens)
 *   --chrome PATH          Chrome binary (default: google-chrome on PATH)
 *   --chrome-flag=FLAG     extra Chrome flag; repeatable
 */

import {cp, mkdir, rm, writeFile} from "node:fs/promises";
import {createServer} from "node:http";
import path from "node:path";

import {
  EXTENSION_DIR,
  REPO_ROOT,
  launchChrome,
  parseCli,
  startStaticServer,
  unpackedExtensionId,
} from "./lib/chrome_harness.mjs";

const WORK_DIR = path.join(REPO_ROOT, ".stt-bench");
const FAKE_BACKEND_PORT = 8731;
const GOOD_KEY = "sk-test-good-key-1234";
const BAD_KEY = "sk-test-wrong-key";

const CLI_SPEC = {
  values: {"--real-backend": "realBackend", "--screens": "screens", "--chrome": "chrome"},
  flags: {"--headed": ["headed", true]},
  defaults: {
    realBackend: null,
    screens: path.join(WORK_DIR, "screens"),
    chrome: "google-chrome",
    headed: false,
  },
};

/**
 * A stand-in backend with the real one's routes and response shapes.
 * Starts unconfigured; accepts exactly GOOD_KEY.
 */
const createFakeBackend = () => {
  const state = {
    anthropicKey: null,
    openrouterKey: null,
    gateProvider: "anthropic",
    verifyProvider: "anthropic",
    models: {
      anthropic: {gate: "claude-haiku-5-5", verify: "claude-haiku-5-5"},
      openrouter: {gate: "openai/gpt-6-luna", verify: "google/gemini-3.5-flash-lite"},
    },
    requests: [],
  };
  const hint = (key) => (key ? `…${key.slice(-4)}` : null);
  const configured = () => Boolean(state.anthropicKey || state.openrouterKey);
  const stage = (name) => {
    const provider = state[`${name}Provider`];
    return {
      provider: configured() ? provider : null,
      model: configured() ? (state.models[provider]?.[name] ?? "gemma3:4b") : null,
      configured: configured(),
    };
  };
  const setupStatus = () => ({
    configured: configured(),
    gate: stage("gate"),
    verify: stage("verify"),
    providers: {
      anthropic: {
        configured: Boolean(state.anthropicKey),
        auth_method: state.anthropicKey ? "api_key" : null,
        key_hint: hint(state.anthropicKey),
        gate_model: state.models.anthropic.gate,
        verify_model: state.models.anthropic.verify,
      },
      openrouter: {
        configured: Boolean(state.openrouterKey),
        key_hint: hint(state.openrouterKey),
        credits: state.openrouterKey ? {total: 10, usage: 2.75} : null,
        gate_model: state.models.openrouter.gate,
        verify_model: state.models.openrouter.verify,
        jev_mode: "off",
        jev_model: "typesafe/jev-1",
        jev_min_check_probability: 0.2,
      },
      ollama: {configured: true, reachable: false, base_url: "http://127.0.0.1:11434/v1"},
    },
  });
  const routes = {
    "GET /healthz": () => [
      200,
      {
        status: "ok",
        server_version: "e2e-fake",
        whisper_model: "distil-small.en",
        configured: configured(),
        checks_today: 14,
        est_cost_today_usd: 0.154,
      },
    ],
    "GET /setup/status": () => [200, setupStatus()],
    "GET /stats/summary": () => [
      200,
      {
        today: {sessions: 2, claims: 31, verify_calls: 14, est_cost_usd: 0.154, labels: {}},
        totals: {
          sessions: 48,
          claims: 902,
          verify_calls: 411,
          est_cost_usd: 4.521,
          labels: {TRUE: 173, FALSE: 61, MISLEADING: 38, UNVERIFIED: 139},
        },
      },
    ],
    "POST /setup/credentials": (body) => {
      if (!["anthropic", "openrouter"].includes(body.provider)) {
        return [400, {detail: "provider must be one of: anthropic, openrouter, ollama"}];
      }
      if (body.provider === "anthropic" && body.api_key === "") {
        // An empty Anthropic key means "use the `ant auth login` sign-in".
        return [401, {detail: "No Anthropic credentials found: run `ant auth login`"}];
      }
      if (body.api_key !== GOOD_KEY) {
        return [401, {detail: "invalid x-api-key"}];
      }
      state[`${body.provider}Key`] = body.api_key;
      state.gateProvider = body.provider;
      state.verifyProvider = body.provider;
      return [200, setupStatus()];
    },
    "POST /setup/stages": (body) => {
      for (const name of ["gate", "verify"]) {
        const provider = body[`${name}_provider`];
        if (provider !== "ollama" && !state[`${provider}Key`]) {
          return [409, {detail: `${provider} is not configured`}];
        }
        state[`${name}Provider`] = provider;
        if (body[`${name}_model`] && state.models[provider]) {
          state.models[provider][name] = body[`${name}_model`];
        }
      }
      return [200, setupStatus()];
    },
    "POST /debug/text": (body) => [
      200,
      {
        claims: [{id: "c1", claim_text: body.text, check_worthiness: 0.9, topic: "other"}],
        verdicts: [
          {
            id: "v1",
            claim: body.text,
            topic: "other",
            label: "FALSE",
            explanation:
              "The Eiffel Tower is about 330 metres tall including its antennas, not 450 metres.",
            sources: [
              {url: "https://www.toureiffel.paris/en/the-monument/key-figures", title: "Key figures — Eiffel Tower"},
              {url: "https://en.wikipedia.org/wiki/Eiffel_Tower", title: "Eiffel Tower — Wikipedia"},
            ],
            checked_at: new Date().toISOString(),
            used_fallback: false,
            evidence: "strong",
          },
        ],
      },
    ],
  };

  const server = createServer((request, response) => {
    const chunks = [];
    request.on("data", (chunk) => chunks.push(chunk));
    request.on("end", () => {
      const route = `${request.method} ${new URL(request.url, "http://x").pathname}`;
      const cors = {
        "Access-Control-Allow-Origin": request.headers.origin ?? "*",
        "Access-Control-Allow-Headers": "content-type",
      };
      if (request.method === "OPTIONS") {
        response.writeHead(204, cors).end();
        return;
      }
      let body = {};
      try {
        body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString("utf8")) : {};
      } catch {
        body = {};
      }
      state.requests.push({route, body});
      const [status, payload] = routes[route]?.(body) ?? [404, {detail: "Not Found"}];
      response.writeHead(status, {...cors, "Content-Type": "application/json"});
      response.end(JSON.stringify(payload));
    });
  });
  return {
    state,
    start: () => new Promise((resolve) => server.listen(FAKE_BACKEND_PORT, "127.0.0.1", resolve)),
    stop: () => server.close(),
  };
};

/** Helpers that act on a page the way a person would. */
const pageActions = (page) => {
  const run = (body) => page.evaluate(`(async () => { ${body} })()`);
  // After an action, give the framework a moment to re-render before the
  // next step reads the page — a person is never faster than a repaint.
  const settle = "await new Promise((resolve) => setTimeout(resolve, 80));";
  return {
    text: () => run("return document.body.innerText;"),
    hash: () => run("return location.hash;"),
    /**
     * Click the first enabled button, link or label whose text matches.
     * Pass `{settle: false}` when the click closes the page itself: an
     * evaluate still waiting inside a closed page never returns.
     */
    click: (label, {settle: wait = true} = {}) =>
      run(`
        const wanted = ${JSON.stringify(label)};
        const candidates = [...document.querySelectorAll("button, a, label, summary")];
        const target = candidates.find(
          (node) => node.innerText.trim().startsWith(wanted) && !node.disabled
        );
        if (!target) { throw new Error("nothing clickable starts with: " + wanted); }
        target.click();
        ${wait ? settle : ""}
      `),
    /** Type into a field so the framework sees it. */
    type: (selector, value) =>
      run(`
        const input = document.querySelector(${JSON.stringify(selector)});
        if (!input) { throw new Error("no element matches ${selector}"); }
        input.focus(); // so a later blur() really fires, as it would for a person
        const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(input), "value").set;
        setter.call(input, ${JSON.stringify(value)});
        input.dispatchEvent(new Event("input", {bubbles: true}));
        ${settle}
      `),
    blur: (selector) => run(`document.querySelector(${JSON.stringify(selector)}).blur(); ${settle}`),
    select: (selector, value) =>
      run(`
        const select = document.querySelector(${JSON.stringify(selector)});
        select.value = ${JSON.stringify(value)};
        select.dispatchEvent(new Event("change", {bubbles: true}));
        ${settle}
      `),
    isDisabled: (label) =>
      run(`
        const button = [...document.querySelectorAll("button")].find(
          (node) => node.innerText.trim().startsWith(${JSON.stringify(label)})
        );
        return button ? button.disabled : null;
      `),
    eval: (expression) => run(`return (${expression});`),
    /** Poll until the page text contains `needle`. */
    waitForText: async (needle, timeoutMs = 12000) => {
      const deadline = Date.now() + timeoutMs;
      for (;;) {
        const text = await run("return document.body.innerText;").catch(() => "");
        if (text.includes(needle)) {
          return text;
        }
        if (Date.now() > deadline) {
          throw new Error(`timed out waiting for text: "${needle}"\n--- page text ---\n${text.slice(0, 900)}`);
        }
        await new Promise((resolve) => setTimeout(resolve, 150));
      }
    },
  };
};

const results = [];
const check = (name, condition, detail = "") => {
  results.push({name, pass: Boolean(condition), detail});
  console.log(`${condition ? "  ok  " : "  FAIL"}  ${name}${detail && !condition ? ` — ${detail}` : ""}`);
};

const main = async () => {
  const options = parseCli(process.argv.slice(2), CLI_SPEC);
  const usingFake = !options.realBackend;
  const backendOrigin = options.realBackend ?? `http://127.0.0.1:${FAKE_BACKEND_PORT}`;
  const backendUrl = `${backendOrigin.replace(/^http/, "ws")}/ws/audio`;

  // A copy at a stable path, so the extension id is stable too.
  const extensionPath = path.join(WORK_DIR, "ext-app-e2e");
  await rm(extensionPath, {recursive: true, force: true});
  await cp(EXTENSION_DIR, extensionPath, {
    recursive: true,
    // The 27 MB speech runtime is irrelevant here.
    filter: (source) => !source.includes(`${path.sep}vendor${path.sep}transformers`),
  });
  const profileDir = path.join(WORK_DIR, "profile-app-e2e");
  await rm(profileDir, {recursive: true, force: true});
  await mkdir(options.screens, {recursive: true});

  const fake = createFakeBackend();
  const chrome = await launchChrome({
    chrome: options.chrome,
    profileDir,
    headed: options.headed,
    flags: [
      "--enable-unsafe-extension-debugging",
      // Lets the capture check start without the toolbar click a person gives.
      `--allowlisted-extension-id=${unpackedExtensionId(extensionPath)}`,
      "--window-size=1280,900",
      ...options.chromeFlags,
    ],
  });
  const staticServer = usingFake ? null : await startStaticServer({isolated: false, port: 8765});
  const consoleProblems = [];
  let shotIndex = 0;
  const screenshot = async (page, name) => {
    shotIndex += 1;
    await page.send("Emulation.setDeviceMetricsOverride", {
      width: 1280,
      height: 900,
      deviceScaleFactor: 1,
      mobile: false,
    });
    const {data} = await page.send("Page.captureScreenshot", {format: "png", captureBeyondViewport: true});
    const file = path.join(options.screens, `${String(shotIndex).padStart(2, "0")}-${name}.png`);
    await writeFile(file, Buffer.from(data, "base64"));
  };

  try {
    const expectedId = unpackedExtensionId(extensionPath);
    const {id: extensionId} = await chrome.send("Extensions.loadUnpacked", {path: extensionPath});
    const appUrl = `chrome-extension://${extensionId}/app/app.html`;
    console.log(`Loaded extension ${extensionId} (${extensionId === expectedId ? "expected id" : "UNEXPECTED id"})`);

    console.log("\nInstall");
    // onInstalled should have opened the welcome tab by itself.
    let welcomeTarget = null;
    for (let attempt = 0; attempt < 40 && !welcomeTarget; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 150));
      const {targetInfos} = await chrome.send("Target.getTargets");
      welcomeTarget = targetInfos.find((target) => target.type === "page" && target.url.startsWith(appUrl));
    }
    check("installing opens the app in a new tab", Boolean(welcomeTarget));
    check("…on the welcome flow", welcomeTarget?.url.endsWith("#/welcome"), welcomeTarget?.url);
    if (!welcomeTarget) {
      throw new Error("the app tab never opened; cannot continue");
    }
    const page = await chrome.attach(welcomeTarget.targetId);
    await page.send("Page.enable");
    const app = pageActions(page);

    // Point the app at the test backend, then start clean.
    await app.eval(`chrome.storage.sync.set({settings: {backendUrl: ${JSON.stringify(backendUrl)}}})`);
    await page.send("Page.reload");
    await app.waitForText("What this extension does");

    console.log("\nWelcome: how it works");
    const disclosure = await app.text();
    check("explains what is captured and where it goes", /Audio stays on this computer/.test(disclosure) && /can be wrong/.test(disclosure));
    await screenshot(page, "welcome-how-it-works");
    await app.click("I understand");
    await app.waitForText("Start the local backend");
    check("consent is recorded on this device", Boolean(await app.eval(`chrome.storage.local.get("onboarding").then((s) => s.onboarding?.consentAt)`)));

    console.log("\nWelcome: backend");
    if (usingFake) {
      await app.waitForText("Not running");
      check("says the backend is not running, with how to start it", (await app.text()).includes("./backend/run.sh"));
      check("Continue is blocked until the backend is found", (await app.isDisabled("Continue")) === true);
      await screenshot(page, "welcome-backend-down");
      await fake.start();
    }
    await app.waitForText("Connected to the backend");
    check("notices the backend by itself once it starts", true);
    await screenshot(page, "welcome-backend-up");
    await app.click("Continue");
    await app.waitForText("Add an API key");

    console.log("\nWelcome: API key");
    await screenshot(page, "welcome-key");
    if ((await app.text()).includes("Already connected")) {
      // A real backend may already have a provider (a stored key, or an
      // `ant auth login` sign-in on this machine).
      check("an existing connection is recognised", true);
      await app.click("Use a different key");
    }
    check("Save is disabled with an empty key", (await app.isDisabled("Save and check key")) === true);
    await app.type("#api-key-anthropic", BAD_KEY);
    await app.click("Save and check key");
    await app.waitForText("did not accept that key");
    check("a rejected key gets a plain explanation", true);
    check("the key field is a password field by default", (await app.eval(`document.querySelector("#api-key-anthropic").type`)) === "password");
    await screenshot(page, "welcome-key-rejected");

    if (usingFake) {
      await app.type("#api-key-anthropic", GOOD_KEY);
      await app.click("Save and check key");
      await app.waitForText("Try a real check");
      check("a good key is accepted and the flow moves on", true);
      const sent = fake.state.requests.filter((request) => request.route === "POST /setup/credentials");
      check("the key was sent to the backend with the right field names", sent.at(-1)?.body.api_key === GOOD_KEY && sent.at(-1)?.body.provider === "anthropic");
      check(
        "the key is not kept anywhere in browser storage",
        await app.eval(`Promise.all([chrome.storage.sync.get(null), chrome.storage.local.get(null)]).then((all) => !JSON.stringify(all).includes(${JSON.stringify(GOOD_KEY)}))`)
      );

      console.log("\nWelcome: sample check");
      await app.click("Run the sample check");
      await app.waitForText("330 metres");
      const verdictText = await app.text();
      check("shows the verdict with its label and sources", verdictText.includes("FALSE") && verdictText.includes("Key figures"));
      await screenshot(page, "welcome-verdict");
      await app.click("Finish");
      await app.waitForText("All time");
      check("finishing lands on Home", (await app.hash()) === "#/home");
      check("completion is remembered", Boolean(await app.eval(`chrome.storage.local.get("onboarding").then((s) => s.onboarding?.completedAt)`)));

      console.log("\nHome");
      const home = await app.text();
      check("shows today's numbers from the backend", home.includes("14") && home.includes("fact-checks"));
      check("shows the connected provider and model", home.includes("Anthropic (claude-haiku-5-5)"));
      check("sidebar reports ready", home.includes("Ready to check"));
      await screenshot(page, "home");
    } else {
      await page.send("Page.navigate", {url: `${appUrl}#/home`});
      await app.waitForText("Local backend");
      check("Home renders against the real backend", (await app.text()).includes("Running"));
      await screenshot(page, "home-real-backend");
    }

    console.log("\nAI provider");
    await page.send("Page.navigate", {url: `${appUrl}#/providers`});
    await app.waitForText("OpenRouter");
    const providers = await app.text();
    if (usingFake) {
      check("shows the key by its last four characters only", providers.includes("Key ending …1234") && !providers.includes(GOOD_KEY));
      await app.click("Advanced");
      check("Apply is disabled while nothing has changed", (await app.isDisabled("Apply")) === true);
      await app.select("#verify-provider", "openrouter");
      check("switching a stage prefills that provider's stored model", (await app.eval(`document.querySelector("#verify-model").value`)) === "google/gemini-3.5-flash-lite");
      await app.click("Apply");
      await app.waitForText("Connect that provider first");
      check("routing to an unconnected provider is refused with a reason", true);
      await screenshot(page, "providers-advanced");
      await app.click("Use the sign-in instead of a key");
      await app.waitForText("No usable Anthropic sign-in was found");
      check("asking for the console sign-in without one explains what to run", true);
      await app.select("#verify-provider", "anthropic");
      await app.type("#verify-model", "claude-sonnet-5-5");
      await app.click("Apply");
      await app.waitForText("Applied.");
      const staged = fake.state.requests.filter((request) => request.route === "POST /setup/stages").at(-1)?.body;
      check("a model change is applied with the backend's field names", staged?.verify_model === "claude-sonnet-5-5" && staged?.gate_provider === "anthropic");
    } else {
      check("both providers are listed", providers.includes("Anthropic") && providers.includes("OpenRouter"));
      await screenshot(page, "providers-real-backend");
    }

    console.log("\nSettings");
    await page.send("Page.navigate", {url: `${appUrl}#/settings`});
    await app.waitForText("What gets checked");
    await screenshot(page, "settings");
    const stored = () => app.eval(`chrome.storage.sync.get("settings").then((s) => s.settings)`);
    await app.click("High");
    await app.waitForText("Saved.");
    check("choosing a sensitivity saves it immediately", (await stored()).sensitivity === "high");
    await app.click("Bottom left");
    await new Promise((resolve) => setTimeout(resolve, 250));
    check("choosing a position saves it", (await stored()).popupPosition === "bottom-left");
    await app.eval(`document.querySelector("#show-transcript").click()`);
    await new Promise((resolve) => setTimeout(resolve, 250));
    check("the transcript switch saves", (await stored()).showTranscript === true);
    await app.click("All topics");
    await new Promise((resolve) => setTimeout(resolve, 250));
    const topics = (await stored()).topics;
    check("switching all topics off keeps the catch-all on", topics.politics === false && topics.other === true);
    await app.type("#popup-duration", "500");
    await app.blur("#popup-duration");
    await new Promise((resolve) => setTimeout(resolve, 250));
    check("an out-of-range duration is clamped", (await stored()).popupDurationS === 60);
    await app.type("#backend-url", "http://not-a-websocket");
    await app.blur("#backend-url");
    await new Promise((resolve) => setTimeout(resolve, 250));
    check("an invalid backend address is not saved", (await stored()).backendUrl === backendUrl);
    check("…and is explained", (await app.eval(`!document.querySelector("#backend-url-error").hidden`)) === true);
    await screenshot(page, "settings-invalid-url");
    await app.type("#backend-url", backendUrl);
    await app.blur("#backend-url");

    console.log("\nEntry points");
    const popup = await chrome.openPage(`chrome-extension://${extensionId}/popup/popup.html`);
    const popupActions = pageActions(popup);
    await popupActions.waitForText("Open dashboard");
    if (usingFake) {
      check(
        "the popup hides its \"add a key\" card once a provider is connected",
        (await popupActions.eval(`getComputedStyle(document.querySelector("#setup-card")).display`)) === "none"
      );
    }
    await screenshot(popup, "popup");
    // The popup closes itself, so do not wait inside it after the click.
    await popupActions.click("Open dashboard", {settle: false}).catch(() => {});
    await new Promise((resolve) => setTimeout(resolve, 800));
    const {targetInfos} = await chrome.send("Target.getTargets");
    const appTabs = targetInfos.filter((target) => target.type === "page" && target.url.startsWith(appUrl));
    check("the popup's link reuses the open app tab instead of opening another", appTabs.length === 1, `${appTabs.length} app tabs`);
    check("…and moves it to Home", appTabs[0]?.url.endsWith("#/home"), appTabs[0]?.url);

    await page.send("Page.navigate", {url: appUrl});
    await app.waitForText("All time");
    check("a returning user with no route lands on Home, not on the welcome flow", (await app.hash()) === "#/home");

    if (!usingFake) {
      console.log("\nCapture (production path, silent tab)");
      await chrome.openPage(`${staticServer.origin}/fixture/player.html?delay=100000`);
      const session = () => app.eval(`chrome.storage.session.get("captureSession").then((s) => s.captureSession ?? null)`);
      await app.eval(`(async () => {
        const [tab] = await chrome.tabs.query({url: "${staticServer.origin}/*"});
        await chrome.runtime.sendMessage({target: "background", type: "CAPTURE_START", payload: {tabId: tab.id}});
      })()`);
      let state = null;
      for (let attempt = 0; attempt < 60 && state?.status !== "capturing" && state?.status !== "error"; attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, 250));
        state = await session();
      }
      check("Start reaches \"capturing\" through the service worker, offscreen document and backend", state?.status === "capturing", JSON.stringify(state));
      await new Promise((resolve) => setTimeout(resolve, 2500));
      state = await session();
      check("…and stays connected", state?.status === "capturing" && state?.wsState === "open", JSON.stringify(state));
      await app.eval(`chrome.runtime.sendMessage({target: "background", type: "CAPTURE_STOP", payload: {}})`);
      for (let attempt = 0; attempt < 40 && state?.status === "capturing"; attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, 250));
        state = await session();
      }
      check("Stop ends the session cleanly", state?.status === "idle" && !state?.lastError, JSON.stringify(state));
    }

    await page.send("Emulation.setDeviceMetricsOverride", {width: 420, height: 800, deviceScaleFactor: 1, mobile: false});
    await new Promise((resolve) => setTimeout(resolve, 200));
    check("no sideways scrolling at a narrow width", await app.eval(`document.documentElement.scrollWidth <= window.innerWidth`));
    const {data} = await page.send("Page.captureScreenshot", {format: "png"});
    await writeFile(path.join(options.screens, "99-home-narrow.png"), Buffer.from(data, "base64"));

    consoleProblems.push(
      ...page.consoleLines.filter(
        (line) =>
          /^\[(error|exception)\]/.test(line) &&
          // Expected: the browser logs the refused requests this test provokes.
          !/Failed to load resource|ERR_CONNECTION_REFUSED/.test(line)
      )
    );
    check("no errors in the app's console", consoleProblems.length === 0, consoleProblems.slice(0, 3).join(" | "));
  } finally {
    chrome.close();
    fake.stop();
    staticServer?.close();
  }

  const failed = results.filter((result) => !result.pass);
  console.log(`\n${results.length - failed.length} of ${results.length} checks passed.`);
  console.log(`Screenshots: ${path.relative(REPO_ROOT, options.screens)}/`);
  return failed.length === 0 ? 0 : 1;
};

main().then(
  (exitCode) => process.exit(exitCode),
  (error) => {
    console.error(`\nerror: ${error.message}`);
    process.exit(1);
  }
);
