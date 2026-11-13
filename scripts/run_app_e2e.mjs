#!/usr/bin/env node
/**
 * End-to-end test of the extension's app (app/app.html) in a real Chrome.
 *
 * Loads the unpacked extension and walks the app the way a new user would:
 * the welcome tab opens on install, the backend is found once it starts, a
 * wrong key is refused and a good one accepted, the sample check shows a
 * verdict, settings persist to chrome.storage, and the popup reuses the open
 * app tab. Then the history: past sessions are imported from the backend,
 * results are searched, filtered, rated, exported and deleted, a scripted
 * live session is captured through the real service worker and offscreen
 * document into IndexedDB, and the Usage screen prices it. Screenshots of
 * every screen are saved for review.
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
import {
  LIVE_CLAIM,
  LIVE_SESSION_ID,
  SEARCHABLE_CLAIM,
  acceptWebSocket,
  listSessions,
  liveSessionFrames,
  seedHistory,
} from "./lib/fake_backend_history.mjs";

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
    // What the real backend would hold in fact_checker.db.
    history: seedHistory(),
    // Hello frames received on /ws/audio, newest last.
    hellos: [],
  };
  const hint =(key) => (key ? `…${key.slice(-4)}` : null);
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
          sessions: state.history.size,
          claims: 902,
          verify_calls: 411,
          est_cost_usd: 4.521,
          labels: {TRUE: 173, FALSE: 61, MISLEADING: 38, UNVERIFIED: 139},
        },
      },
    ],
    "GET /stats/sessions": (body, url) => {
      const limit = Number(url.searchParams.get("limit") ?? 50);
      const offset = Number(url.searchParams.get("offset") ?? 0);
      return [200, listSessions(state.history).slice(offset, offset + limit)];
    },
    "POST /feedback": () => [200, {ok: true}],
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
      const url = new URL(request.url, "http://x");
      const route = `${request.method} ${url.pathname}`;
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
      const sessionDetail = /^GET \/stats\/sessions\/([^/]+)$/.exec(route);
      const [status, payload] = sessionDetail
        ? state.history.has(decodeURIComponent(sessionDetail[1]))
          ? [200, state.history.get(decodeURIComponent(sessionDetail[1]))]
          : [404, {detail: "unknown session id"}]
        : (routes[route]?.(body, url) ?? [404, {detail: "Not Found"}]);
      response.writeHead(status, {...cors, "Content-Type": "application/json"});
      response.end(JSON.stringify(payload));
    });
  });
  // /ws/audio: answer a hello with "ready", play the scripted session, and
  // close cleanly on "stop" — the frames a real session would produce, with
  // no speech model behind them.
  server.on("upgrade", (request, socket) => {
    if (new URL(request.url, "http://x").pathname !== "/ws/audio") {
      socket.destroy();
      return;
    }
    acceptWebSocket(request, socket, (text, connection) => {
      const frame = JSON.parse(text);
      if (frame.type === "hello") {
        state.hellos.push(frame);
        connection.sendJson({
          type: "ready",
          server_version: "e2e-fake",
          model: "distil-small.en",
          session_id: LIVE_SESSION_ID,
        });
        liveSessionFrames().forEach((liveFrame, index) => {
          setTimeout(() => connection.sendJson(liveFrame), 300 + index * 60);
        });
      } else if (frame.type === "stop") {
        connection.close(1000);
      }
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

/** Everything in the history database, read through the extension's own module. */
const ledgerSnapshot = (app) =>
  app.eval(`import("../ledger/ledger.mjs").then((ledger) => ledger.loadLedger())`);

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** For a failed width check: which elements stick out past the viewport. */
const WIDE_ELEMENTS = `[...document.querySelectorAll("body *")]
  .filter((element) => element.getBoundingClientRect().right > window.innerWidth + 1)
  .slice(0, 6)
  .map((element) => element.tagName.toLowerCase() + "." + element.className)
  .join(", ")`;

/** Capture every file the page offers for download, as text. */
const captureDownloads = (app) =>
  app.eval(`(() => {
    window.__downloads = [];
    const original = URL.createObjectURL.bind(URL);
    URL.createObjectURL = (blob) => {
      blob.text().then((text) => window.__downloads.push(text));
      return original(blob);
    };
    return true;
  })()`);

const lastDownload = async (app) => {
  await pause(300);
  return app.eval(`window.__downloads.at(-1) ?? null`);
};

/**
 * History and Usage against the fake backend: import the backend's past
 * sessions, find and judge results, watch a scripted live session arrive,
 * and read the spend.
 */
const historyAndUsageChecks = async ({chrome, page, app, appUrl, fake, screenshot, playerUrl}) => {
  console.log("\nHistory: import from the backend");
  await page.send("Page.navigate", {url: `${appUrl}#/history`});
  await app.waitForText("Nothing here yet");
  await screenshot(page, "history-empty");
  await captureDownloads(app);
  await app.click("Import past results from the backend");
  await app.waitForText("51 results");
  let ledger = await ledgerSnapshot(app);
  check(
    "imports every session with its claims, results and flat-rate spend",
    ledger.sessions.length === 5 &&
      ledger.verdicts.length === 51 &&
      ledger.claims.length === 56 &&
      ledger.usage.length === 4,
    `sessions ${ledger.sessions.length}, verdicts ${ledger.verdicts.length}, claims ${ledger.claims.length}, usage ${ledger.usage.length}`
  );
  check(
    "YouTube sessions keep their channel",
    ledger.sessions.some((session) => session.platform === "youtube" && session.channel_id === "@newsanalyst")
  );
  await screenshot(page, "history-results");

  console.log("\nHistory: results");
  await app.type(".filter-search", "eiffel");
  await app.waitForText("1 of 51 results");
  check("search finds a result by its claim", (await app.text()).includes(SEARCHABLE_CLAIM));
  await app.click("FALSE");
  await app.waitForText("330 metres");
  const detail = await app.text();
  check(
    "opening a result shows its explanation, source, model and timing",
    detail.includes("Source 0") && detail.includes("anthropic · claude-haiku-5-5") && /Took\s+2\.0 s/.test(detail)
  );
  await app.click("👎 Wrong");
  await app.waitForText("It should have been");
  await app.select(".result-actions select", "TRUE");
  await pause(300);
  ledger = await ledgerSnapshot(app);
  check(
    "a rating and its correction are stored in the history",
    ledger.feedback.length === 1 &&
      ledger.feedback[0].rating === "down" &&
      ledger.feedback[0].corrected_label === "TRUE",
    JSON.stringify(ledger.feedback)
  );
  const sentFeedback = fake.state.requests.filter((request) => request.route === "POST /feedback").at(-1)?.body;
  check(
    "…and sent to the backend with its field names",
    sentFeedback?.verdict_id === "s-youtube-v0" && sentFeedback?.rating === "down" && sentFeedback?.corrected_label === "TRUE",
    JSON.stringify(sentFeedback)
  );
  await screenshot(page, "history-result-open");
  await app.click("Clear filters");
  await app.select('select[aria-label="Site"]', "kick");
  await app.waitForText("2 of 51 results");
  await app.click("Misleading");
  await app.waitForText("1 of 51 results");
  check("site and verdict filters combine", (await app.text()).includes("Crime doubled in a year."));
  await app.click("Export CSV");
  const csv = await lastDownload(app);
  check(
    "Export CSV hands over exactly the filtered results",
    typeof csv === "string" &&
      csv.trimEnd().split("\r\n").length === 2 &&
      csv.includes("checked_at,platform,channel") &&
      csv.includes("Crime doubled in a year."),
    String(csv).slice(0, 200)
  );
  await app.click("Clear filters");
  await app.select('select[aria-label="Your rating"]', "down");
  await app.waitForText("1 of 51 results");
  check("results can be filtered by your own rating", (await app.text()).includes(SEARCHABLE_CLAIM));
  await app.click("Clear filters");

  console.log("\nHistory: channels and sessions");
  await app.click("Channels");
  await app.waitForText("Adjudicated");
  const channelRows = await app.eval(
    `[...document.querySelectorAll("#panel-channels tbody tr")].map((row) => row.innerText.replace(/\\s+/g, " "))`
  );
  const twitchRow = channelRows.find((row) => row.startsWith("DebateStreamer Twitch")) ?? "";
  const kickRow = channelRows.find((row) => row.startsWith("DebateStreamer Kick")) ?? "";
  check("one row per channel per site", channelRows.length === 4, channelRows.join(" | "));
  check(
    "a channel with 40 adjudicated checks shows its shares",
    twitchRow.includes(" 1 session 45 40 60% 25% 15% 11% "),
    twitchRow
  );
  check(
    "a channel with too few shows no percentages",
    kickRow.includes(" 1 session 2 2 — — — 0% "),
    kickRow
  );
  check("the same name on another site is a separate channel", Boolean(twitchRow) && Boolean(kickRow));
  await screenshot(page, "history-channels");
  await app.eval(
    `[...document.querySelectorAll("#panel-channels tbody tr")].find((row) => row.innerText.includes("Kick")).querySelector("button").click()`
  );
  await app.waitForText("2 of 51 results");
  check("a channel's Results button opens its results", true);
  await app.click("Sessions");
  await app.waitForText("Claims heard");
  check(
    "sessions list each stretch of watching",
    (await app.eval(`document.querySelectorAll("#panel-sessions tbody tr").length`)) === 5
  );
  await screenshot(page, "history-sessions");
  await page.send("Emulation.setDeviceMetricsOverride", {width: 420, height: 800, deviceScaleFactor: 1, mobile: false});
  await pause(200);
  check(
    "wide tables scroll inside their own box at a narrow width",
    await app.eval(`document.documentElement.scrollWidth <= window.innerWidth`),
    await app.eval(WIDE_ELEMENTS)
  );

  console.log("\nLive session (scripted over the real capture path)");
  await page.send("Emulation.setDeviceMetricsOverride", {width: 1280, height: 900, deviceScaleFactor: 1, mobile: false});
  await app.click("Results");
  await app.click("Clear filters");
  await chrome.openPage(playerUrl);
  await app.eval(`(async () => {
    const [tab] = await chrome.tabs.query({url: ${JSON.stringify(new URL(playerUrl).origin + "/*")}});
    await chrome.runtime.sendMessage({target: "background", type: "CAPTURE_START", payload: {tabId: tab.id}});
  })()`);
  await app.waitForText(LIVE_CLAIM);
  check("a result appears in History as it happens, without reloading", true);
  check("the extension asks the backend for per-claim frames", fake.state.hellos.at(-1)?.send_claims === true);
  await pause(1200);
  ledger = await ledgerSnapshot(app);
  const live = ledger.sessions.find((session) => session.backend_session_id === LIVE_SESSION_ID);
  const liveUsage = ledger.usage.filter((row) => row.session_id === live?.id);
  const liveClaims = ledger.claims.filter((claim) => claim.session_id === live?.id);
  check("the session is recorded under the backend's session id", Boolean(live) && live.ended_at === null);
  check(
    "both claims are recorded, the checked one once with its final outcome",
    liveClaims.length === 2 && liveClaims.find((claim) => claim.id === "live-c1")?.outcome === "verified",
    JSON.stringify(liveClaims.map((claim) => [claim.id, claim.outcome]))
  );
  const estimated = liveUsage.find((row) => row.verdict_id === "live-v1");
  check(
    "an Anthropic call is priced from its tokens and search",
    estimated?.cost_source === "estimate" && Math.abs(estimated.cost_usd - 0.0109) < 1e-9,
    JSON.stringify(estimated)
  );
  check(
    "an OpenRouter cost is kept exactly; an unpriced call is left unpriced",
    liveUsage.some((row) => row.cost_source === "provider" && row.cost_usd === 0.0078) &&
      liveUsage.some((row) => row.cost_source === "unknown" && row.cost_usd === null),
    JSON.stringify(liveUsage.map((row) => [row.model, row.cost_source, row.cost_usd]))
  );
  await app.eval(`chrome.runtime.sendMessage({target: "background", type: "CAPTURE_STOP", payload: {}})`);
  let ended = null;
  for (let attempt = 0; attempt < 40 && !ended; attempt += 1) {
    await pause(200);
    ended = (await ledgerSnapshot(app)).sessions.find((session) => session.id === live?.id)?.ended_at;
  }
  check("stopping capture closes the session", Boolean(ended));
  // Opening the player put the app tab in the background, where Chrome
  // throttles timers and withholds focus events. Bring it back, as a person
  // returning to the tab would.
  await page.send("Page.bringToFront");

  console.log("\nUsage");
  await page.send("Page.navigate", {url: `${appUrl}#/usage`});
  await app.waitForText("Spend per day");
  const week = await app.text();
  check("totals the week across imported and live sessions", week.includes("52") && week.includes("fact-checks"));
  check("marks estimated spend with ≈", /≈ \$\d/.test(week));
  check("says when a call could not be priced", week.includes("1 call had no cost reported"));
  check("says when part of the period is the backend's flat-rate estimate", week.includes("imported from the backend"));
  check(
    "draws a bar for each day with spend",
    (await app.eval(`document.querySelectorAll(".chart-bar").length`)) >= 3
  );
  await app.eval(`document.querySelector('.chart-hit[tabindex="0"]').focus()`);
  await pause(150);
  check(
    "a day's figures show on keyboard focus",
    /\$\d/.test((await app.eval(`document.querySelector(".chart-tooltip")?.innerText ?? ""`)))
  );
  check("breaks spend down by model, step, site and channel", ["By model", "By step", "By site", "By channel"].every((title) => week.includes(title)));
  await screenshot(page, "usage-week");
  await app.click("Today");
  await app.waitForText("≈ $0.019");
  check("today's spend is the live session's four calls", (await app.text()).includes("4 provider calls"));
  await screenshot(page, "usage-today");

  await page.send("Page.navigate", {url: `${appUrl}#/home`});
  await app.waitForText("Latest results");
  const homeAfter = await app.text();
  check("Home shows the latest result and today's spend from the history", homeAfter.includes(LIVE_CLAIM) && homeAfter.includes("≈ $0.019"));
  await screenshot(page, "home-with-history");

  console.log("\nHistory: deleting");
  await page.send("Page.navigate", {url: `${appUrl}#/history`});
  await app.waitForText("52 results");
  await app.type(".filter-search", "forty percent");
  await app.waitForText("1 of 52 results");
  await app.click("FALSE");
  await app.click("Delete");
  check("deleting asks first", (await app.text()).includes("Delete this result?"));
  await app.click("Delete this result?");
  await app.waitForText("0 of 51 results");
  ledger = await ledgerSnapshot(app);
  check(
    "a deleted result is gone, and what it cost stays in the usage totals",
    !ledger.verdicts.some((verdict) => verdict.id === "live-v1") &&
      ledger.usage.some((row) => row.verdict_id === "live-v1")
  );
  await app.click("Clear filters");
  await app.click("Sessions");
  await app.waitForText("Claims heard");
  await app.eval(
    `[...document.querySelectorAll("#panel-sessions tbody tr")].find((row) => row.innerText.includes("Late show")).querySelector(".row-actions button:last-child").click()`
  );
  await pause(150);
  await app.click("Delete it?");
  await pause(500);
  ledger = await ledgerSnapshot(app);
  check(
    "deleting a session removes its results, claims and usage",
    !ledger.sessions.some((session) => session.id === "s-kick") &&
      !ledger.verdicts.some((verdict) => verdict.session_id === "s-kick") &&
      !ledger.claims.some((claim) => claim.session_id === "s-kick") &&
      !ledger.usage.some((row) => row.session_id === "s-kick")
  );
};

/** Settings → Your data: retention, re-import, export, delete everything. */
const dataSettingsChecks = async ({app, page, screenshot}) => {
  console.log("\nSettings: your data");
  await captureDownloads(app);
  const stored = () => app.eval(`chrome.storage.sync.get("settings").then((s) => s.settings)`);
  await app.select("#history-retention", "365");
  await pause(250);
  check("the retention choice is saved", (await stored()).historyRetentionDays === 365);
  check(
    "a daily sweep is scheduled to apply it",
    (await app.eval(`chrome.alarms.get("ledger-retention").then((alarm) => alarm?.periodInMinutes ?? null)`)) === 1440
  );
  const removed = await app.eval(
    `import("../ledger/ledger.mjs").then((ledger) => ledger.deleteOlderThan(new Date(Date.now() - 365 * 86400000).toISOString()))`
  );
  let ledger = await ledgerSnapshot(app);
  check(
    "the sweep removes sessions older than that, with their results",
    removed === 1 && !ledger.sessions.some((session) => session.id === "s-old") && !ledger.verdicts.some((verdict) => verdict.session_id === "s-old"),
    `removed ${removed}`
  );
  await app.click("Import past results from the backend");
  await app.waitForText("Imported 2 sessions with 3 results.");
  check("importing again adds only what is missing", (await app.text()).includes("3 already here"));
  await app.click("Import past results from the backend");
  await app.waitForText("Nothing new to import.");
  check("…and a third time adds nothing", true);
  await screenshot(page, "settings-data");
  await app.click("Export everything");
  const exported = JSON.parse((await lastDownload(app)) ?? "{}");
  ledger = await ledgerSnapshot(app);
  check(
    "Export everything is one JSON document of the whole history",
    exported.format === "live-stream-fact-checker-export" &&
      exported.sessions?.length === ledger.sessions.length &&
      exported.verdicts?.length === ledger.verdicts.length &&
      exported.usage?.length === ledger.usage.length &&
      exported.feedback?.length === 1
  );
  await app.click("Delete all history");
  await app.click("Delete all history for good?");
  await app.waitForText("Your history has been deleted.");
  ledger = await ledgerSnapshot(app);
  check(
    "Delete all history empties every store",
    Object.values(ledger).every((rows) => rows.length === 0),
    JSON.stringify(Object.fromEntries(Object.entries(ledger).map(([name, rows]) => [name, rows.length])))
  );
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
  // Serves the silent "player" page that capture is started on.
  const staticServer = await startStaticServer({isolated: false, port: 8765});
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
    check(
      "explains what is captured and where it goes, and links the privacy policy",
      /Audio stays on this computer/.test(disclosure) &&
        /The words go to the AI provider.*the audio does not/s.test(disclosure) &&
        /can be wrong/.test(disclosure) &&
        (await app.eval(`[...document.querySelectorAll("a")].some((link) => link.href.endsWith("/docs/privacy-policy.md"))`))
    );
    await screenshot(page, "welcome-how-it-works");
    await app.click("I understand");
    await app.waitForText("Where should it run?");
    check("consent is recorded on this device", Boolean(await app.eval(`chrome.storage.local.get("onboarding").then((s) => s.onboarding?.consentAt)`)));

    console.log("\nWelcome: where it runs");
    check(
      "a first run is steered to the engine that needs no install",
      (await app.eval(`document.querySelector('input[name="welcome-engine"]:checked')?.value`)) === "browser"
    );
    await screenshot(page, "welcome-engine");
    // This script exercises the local-backend path; the browser engine has
    // its own (scripts/run_browser_engine_e2e.mjs).
    await app.click("Local backend");
    await app.click("Continue");
    await app.waitForText("Start the local backend");
    check(
      "choosing the local backend is stored",
      (await app.eval(`chrome.storage.local.get("engine").then((s) => s.engine?.mode)`)) === "companion"
    );

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
      check("shows the connected provider and model", home.includes("Anthropic (claude-haiku-5-5)"));
      check("sidebar reports ready", home.includes("Ready to check"));
      check(
        "with an empty history, offers to import what the backend holds",
        home.includes("Bring your past results across") && home.includes("5 earlier sessions")
      );
      await screenshot(page, "home");

      await historyAndUsageChecks({
        chrome,
        page,
        app,
        appUrl,
        fake,
        screenshot,
        playerUrl: `${staticServer.origin}/fixture/player.html?delay=100000`,
      });
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

    if (usingFake) {
      await dataSettingsChecks({app, page, screenshot});
    }

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

      // The real backend names its session in the ready frame; the service
      // worker must have recorded it, and closed it on Stop.
      const recorded = await ledgerSnapshot(app);
      const [liveSession] = recorded.sessions;
      check(
        "the session was recorded in the history under the backend's session id",
        recorded.sessions.length === 1 && /^[0-9a-f]{32}$/.test(liveSession?.backend_session_id ?? ""),
        JSON.stringify(recorded.sessions)
      );
      check("…and closed when capture stopped", Boolean(liveSession?.ended_at), JSON.stringify(liveSession));
      await page.send("Page.navigate", {url: `${appUrl}#/history`});
      await app.waitForText("Sessions");
      await screenshot(page, "history-real-backend");
      await page.send("Page.navigate", {url: appUrl});
      await app.waitForText("All time");
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
