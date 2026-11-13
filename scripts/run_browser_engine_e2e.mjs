#!/usr/bin/env node
/**
 * The browser engine, end to end, with nothing else running.
 *
 * This is the build guide's phase-4 finish line: a clean Chrome profile and
 * NO backend go from install to a real verdict on a (recorded) stream.
 *
 *   1. The extension is installed; its welcome tab opens.
 *   2. Welcome flow: agree → "This browser" → paste an API key (checked live
 *      with the provider) → download and test a speech model → run the
 *      sample check, which returns a real verdict.
 *   3. A tab plays 43 s of recorded speech containing four checkable claims;
 *      Start is pressed on it.
 *   4. The extension transcribes, finds the claims and checks them with the
 *      provider. The verdicts, the claims that were skipped and the cost of
 *      every call land in the history.
 *   5. Removing the key ends a running session.
 *
 * THIS MAKES REAL, PAID PROVIDER CALLS with the key you supply: one sample
 * check plus one stream's worth — roughly five cents per provider.
 *
 * Keys are read from the git-ignored `.env.spike` in the repo root
 * (OPENROUTER_API_KEY=, ANTHROPIC_API_KEY=, OPENAI_API_KEY=, GEMINI_API_KEY=).
 * They are typed into the extension's own key form and never printed.
 *
 * No dependencies: Node 22+, a local Chrome, network access, and the vendored
 * speech runtime (python scripts/vendor_stt_runtime.py).
 *
 * Usage (from the repo root):
 *
 *     node scripts/run_browser_engine_e2e.mjs --provider anthropic
 *     node scripts/run_browser_engine_e2e.mjs --provider openrouter --headed
 *
 * Options:
 *   --provider ID        anthropic | openrouter | openai | gemini (required)
 *   --headed             show the browser window
 *   --screens DIR        where to save screenshots (default .stt-bench/screens)
 *   --timeout-s N        limit for the model download and self-test (default 900)
 *   --chrome PATH        Chrome binary (default: google-chrome on PATH)
 *   --chrome-flag=FLAG   extra Chrome flag; repeatable
 */

import {cp, mkdir, readFile, rm, stat, writeFile} from "node:fs/promises";
import path from "node:path";

import {
  EXTENSION_DIR,
  REPO_ROOT,
  launchChrome,
  parseCli,
  startStaticServer,
  unpackedExtensionId,
  waitForTitle,
} from "./lib/chrome_harness.mjs";

const WORK_DIR = path.join(REPO_ROOT, ".stt-bench");
const ENV_FILE = path.join(REPO_ROOT, ".env.spike");
const KEY_VARIABLES = Object.freeze({
  openrouter: "OPENROUTER_API_KEY",
  anthropic: "ANTHROPIC_API_KEY",
  openai: "OPENAI_API_KEY",
  gemini: "GEMINI_API_KEY",
});

const CLI_SPEC = {
  values: {
    "--provider": "provider",
    "--screens": "screens",
    "--timeout-s": "timeoutS",
    "--chrome": "chrome",
  },
  flags: {"--headed": ["headed", true]},
  defaults: {
    provider: null,
    screens: path.join(WORK_DIR, "screens"),
    timeoutS: "900",
    chrome: "google-chrome",
    headed: false,
  },
};

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** The key for `providerId` from .env.spike, or null. Never logged. */
const readKey = async (providerId) => {
  let contents = "";
  try {
    contents = await readFile(ENV_FILE, "utf8");
  } catch {
    return null;
  }
  const line = contents
    .split("\n")
    .find((candidate) => candidate.trim().startsWith(`${KEY_VARIABLES[providerId]}=`));
  const value = line?.slice(line.indexOf("=") + 1).trim().replace(/^["']|["']$/g, "");
  return value || null;
};

const results = [];
const check = (name, condition, detail = "") => {
  results.push({name, pass: Boolean(condition)});
  console.log(`${condition ? "  ok  " : "  FAIL"}  ${name}${detail && !condition ? ` — ${detail}` : ""}`);
};

const main = async () => {
  const options = parseCli(process.argv.slice(2), CLI_SPEC);
  if (!KEY_VARIABLES[options.provider]) {
    throw new Error(`--provider must be one of: ${Object.keys(KEY_VARIABLES).join(", ")}`);
  }
  const apiKey = await readKey(options.provider);
  if (!apiKey) {
    throw new Error(`no ${KEY_VARIABLES[options.provider]} in .env.spike — add one to run this provider`);
  }
  try {
    await stat(path.join(EXTENSION_DIR, "vendor", "transformers", "transformers.js"));
  } catch {
    throw new Error("the speech runtime is not vendored: run `python scripts/vendor_stt_runtime.py` first");
  }
  /** Anything that would put the key on screen or in a log fails loudly. */
  const assertNoKey = (label, text) => {
    if (String(text).includes(apiKey)) {
      throw new Error(`the API key appeared in ${label}`);
    }
    return text;
  };

  const extensionPath = path.join(WORK_DIR, "ext-engine-e2e");
  await rm(extensionPath, {recursive: true, force: true});
  await cp(EXTENSION_DIR, extensionPath, {recursive: true});
  // A clean profile every time: that is the point of this test.
  const profileDir = path.join(WORK_DIR, "profile-engine-e2e");
  await rm(profileDir, {recursive: true, force: true});
  await mkdir(options.screens, {recursive: true});

  const staticServer = await startStaticServer({isolated: false, port: 8765});
  const chrome = await launchChrome({
    chrome: options.chrome,
    profileDir,
    headed: options.headed,
    flags: [
      "--enable-unsafe-extension-debugging",
      `--allowlisted-extension-id=${unpackedExtensionId(extensionPath)}`,
      "--window-size=1280,900",
      ...options.chromeFlags,
    ],
  });
  const screenshot = async (page, name) => {
    const {data} = await page.send("Page.captureScreenshot", {format: "png", captureBeyondViewport: true});
    await writeFile(path.join(options.screens, `engine-${options.provider}-${name}.png`), Buffer.from(data, "base64"));
  };

  try {
    const {id: extensionId} = await chrome.send("Extensions.loadUnpacked", {path: extensionPath});
    const appUrl = `chrome-extension://${extensionId}/app/app.html`;

    console.log(`\nInstall (clean profile, no backend running) — provider: ${options.provider}`);
    let welcome = null;
    for (let attempt = 0; attempt < 40 && !welcome; attempt += 1) {
      await pause(150);
      const {targetInfos} = await chrome.send("Target.getTargets");
      welcome = targetInfos.find((target) => target.type === "page" && target.url.startsWith(appUrl));
    }
    check("installing opens the welcome flow", welcome?.url.endsWith("#/welcome"), welcome?.url);
    if (!welcome) {
      throw new Error("the app tab never opened");
    }
    const page = await chrome.attach(welcome.targetId);
    await page.send("Page.enable");
    const run = (expression) => page.evaluate(`(async () => (${expression}))()`);
    const text = async () => assertNoKey("the page text", await run("document.body.innerText"));
    const waitForText = async (needle, timeoutMs = 20_000) => {
      const deadline = Date.now() + timeoutMs;
      for (;;) {
        const body = await text().catch(() => "");
        if (body.includes(needle)) {
          return body;
        }
        if (Date.now() > deadline) {
          throw new Error(`timed out waiting for "${needle}"\n--- page ---\n${body.slice(-1500)}`);
        }
        await pause(250);
      }
    };
    const click = (label) =>
      run(`(() => {
        const target = [...document.querySelectorAll("button, label, summary")].find(
          (node) => node.innerText.trim().startsWith(${JSON.stringify(label)}) && !node.disabled
        );
        if (!target) { throw new Error("nothing clickable starts with: ${label}"); }
        target.click();
        return true;
      })()`);
    const stored = (area, key) =>
      run(`chrome.storage.${area}.get(${JSON.stringify(key)}).then((all) => all[${JSON.stringify(key)}] ?? null)`);
    const ledger = () => run(`import("../ledger/ledger.mjs").then((module) => module.loadLedger())`);
    const captureSession = () => stored("session", "captureSession");
    const waitForSession = async (isDone, timeoutMs) => {
      const deadline = Date.now() + timeoutMs;
      let session = null;
      while (Date.now() < deadline) {
        session = await captureSession();
        if (session && isDone(session)) {
          break;
        }
        await pause(250);
      }
      return session;
    };
    const startCapture = () =>
      run(`(async () => {
        const [tab] = await chrome.tabs.query({url: "${staticServer.origin}/*"});
        await chrome.runtime.sendMessage({target: "background", type: "CAPTURE_START", payload: {tabId: tab.id}});
        return true;
      })()`);

    console.log("\nWelcome flow");
    await waitForText("What this extension does");
    await click("I understand");
    await waitForText("Where should it run?");
    check(
      "the browser engine is the suggested choice",
      (await run(`document.querySelector('input[name="welcome-engine"]:checked')?.value`)) === "browser"
    );
    await click("Continue");
    await waitForText("Add an API key");
    check("choosing it is stored", (await stored("local", "engine"))?.mode === "browser");
    const keyStep = await text();
    check(
      "all four providers are offered",
      ["OpenRouter", "Anthropic", "OpenAI", "Gemini"].every((name) => keyStep.includes(name))
    );

    await run(`(() => {
      document.querySelector('input[name="welcome-provider"][value="${options.provider}"]').click();
      return true;
    })()`);
    await pause(200);
    const fieldId = `#api-key-${options.provider}`;
    // A wrong key first: it must be refused by the provider, in plain words.
    const typeKey = (value) =>
      run(`(() => {
        const input = document.querySelector(${JSON.stringify(fieldId)});
        input.focus();
        Object.getOwnPropertyDescriptor(Object.getPrototypeOf(input), "value").set.call(input, ${JSON.stringify(value)});
        input.dispatchEvent(new Event("input", {bubbles: true}));
        return input.type;
      })()`);
    check("the key field hides what is typed", (await typeKey("sk-definitely-not-a-real-key-000000")) === "password");
    await click("Save and check key");
    await waitForText("did not accept that key", 30_000);
    check("a wrong key is refused by the provider, with a plain explanation", true);
    check("…and is not stored", (await stored("local", "providerKeys")) === null);

    await typeKey(apiKey);
    await click("Save and check key");
    await waitForText("Speech recognition", 30_000);
    check("the real key is accepted after a live check", true);
    const keys = await stored("local", "providerKeys");
    check("the key is kept in this browser's local storage only", keys?.[options.provider] === apiKey);
    check(
      "…never in synced storage",
      !JSON.stringify(await run("chrome.storage.sync.get(null)")).includes(apiKey)
    );

    console.log("\nSpeech (downloads a 63 MB model into the clean profile)");
    await click("Download and test");
    const startedAt = Date.now();
    for (;;) {
      const body = await text();
      if (/Works on this device|cannot transcribe live speech|could not finish/.test(body)) {
        break;
      }
      if (Date.now() - startedAt > Number(options.timeoutS) * 1000) {
        throw new Error(`the speech self-test did not finish within ${options.timeoutS} s`);
      }
      await pause(1000);
    }
    check("the speech model downloads and passes its self-test", (await text()).includes("Works on this device"));
    await screenshot(page, "welcome-speech");
    await click("Continue");
    await waitForText("Try a real check");

    console.log("\nSample check (a real provider call)");
    await click("Run the sample check");
    await waitForText("Finish", 120_000);
    const sample = await text();
    const sampleLabel = /\b(TRUE|FALSE|MISLEADING|UNVERIFIED)\b/.exec(sample.slice(sample.indexOf("Try a real check")))?.[1];
    console.log(`        verdict for "The Eiffel Tower is 450 meters tall.": ${sampleLabel ?? "none"}`);
    check(
      "the sample claim comes back FALSE with sources",
      sampleLabel === "FALSE" && (await run(`document.querySelectorAll(".verdict-sources a").length`)) > 0,
      sample.slice(sample.indexOf("Try a real check"), sample.indexOf("Try a real check") + 600)
    );
    const sampleUsage = (await ledger()).usage.filter((row) => row.session_id === "sample-check");
    check(
      "what the sample check cost is recorded, though no stream was running",
      sampleUsage.length === 2 && sampleUsage.every((row) => row.provider === options.provider),
      JSON.stringify(sampleUsage.map((row) => [row.stage, row.provider, row.cost_usd]))
    );
    await screenshot(page, "welcome-verdict");
    await click("Finish");
    await waitForText("Latest results");
    const home = await text();
    check("Home shows the engine and the connected provider", home.includes("This browser") && home.includes("Connected"));
    check("…and says it is ready", home.includes("Ready to check"), home.slice(0, 300));
    await screenshot(page, "home");

    const popup = await chrome.openPage(`chrome-extension://${extensionId}/popup/popup.html`);
    await pause(800);
    const popupText = await popup.evaluate("document.body.innerText");
    check("the popup needs no backend", popupText.includes("Runs in this browser") && !popupText.includes("Backend offline"), popupText.slice(0, 200));
    await popup.send("Page.close").catch(() => {});

    console.log("\nLive session: a tab playing recorded speech with four checkable claims");
    const player = await chrome.openPage(`${staticServer.origin}/fixture/player.html?delay=100000`);
    await page.send("Page.bringToFront");
    await startCapture();
    let session = await waitForSession(
      (current) => current.status === "error" || (current.status === "capturing" && current.speech?.status === "ready"),
      120_000
    );
    check(
      "Start reaches \"capturing\" with no backend",
      session?.status === "capturing" && session.speech?.status === "ready",
      JSON.stringify(session)
    );
    await player.send("Runtime.evaluate", {
      expression: 'document.getElementById("player").play()',
      userGesture: true,
      awaitPromise: true,
    });
    await waitForTitle(player, {prefixes: ["fixture-player:ended"], timeoutS: 120});
    await pause(2000);
    await run(`chrome.runtime.sendMessage({target: "background", type: "CAPTURE_STOP", payload: {}})`);
    // Stop looks at the last words and finishes the checks under way.
    session = await waitForSession((current) => current.status === "idle" || current.status === "error", 90_000);
    check("Stop ends the session cleanly", session?.status === "idle" && !session.lastError, JSON.stringify(session));

    const history = await ledger();
    assertNoKey("the history", JSON.stringify(history));
    const [recorded] = history.sessions;
    check(
      "the session is in the history, marked as run by this browser",
      history.sessions.length === 1 && recorded.engine === "browser" && recorded.backend_session_id === null && Boolean(recorded.ended_at),
      JSON.stringify(history.sessions)
    );
    console.log(`        claims heard: ${history.claims.length} — ${history.claims.map((claim) => claim.outcome).join(", ")}`);
    for (const verdict of history.verdicts) {
      console.log(`        ${verdict.label.padEnd(10)} ${verdict.claim_text}`);
    }
    const eiffel = history.verdicts.find((verdict) => /eiffel/i.test(verdict.claim_text));
    check("claims were found in the speech and checked", history.verdicts.length >= 2, `${history.verdicts.length} verdicts`);
    check(
      "the false claim about the Eiffel Tower is marked FALSE, with sources",
      eiffel?.label === "FALSE" && eiffel.sources.length > 0,
      JSON.stringify(eiffel ?? history.verdicts.map((verdict) => verdict.claim_text))
    );
    check(
      "opinions and game talk were not checked",
      !history.verdicts.some((verdict) => /patch is terrible|left lane/i.test(verdict.claim_text))
    );
    check(
      "every verdict names the provider and model that produced it",
      history.verdicts.every((verdict) => verdict.provider === options.provider && verdict.model)
    );
    const spend = history.usage.reduce((sum, row) => sum + (row.cost_usd ?? 0), 0);
    const stages = new Set(history.usage.map((row) => row.stage));
    console.log(
      `        ${history.usage.length} provider calls recorded, ` +
        `${history.usage.filter((row) => row.cost_usd !== null).length} priced, $${spend.toFixed(4)} in all`
    );
    check("every provider call is in the usage record, for both steps", stages.has("gate") && stages.has("verify"));
    if (["anthropic", "openrouter"].includes(options.provider)) {
      check(
        "…with a cost",
        history.usage.every((row) => typeof row.cost_usd === "number") && spend > 0,
        JSON.stringify(history.usage.map((row) => [row.stage, row.cost_source, row.cost_usd]))
      );
    }

    await page.send("Page.navigate", {url: `${appUrl}#/history`});
    await waitForText("Results");
    check("History shows the results", (await text()).includes(history.verdicts[0].claim_text));
    await screenshot(page, "history");
    await page.send("Page.navigate", {url: `${appUrl}#/usage`});
    await waitForText("fact-checks");
    await screenshot(page, "usage");

    console.log("\nRemoving the key");
    await startCapture();
    session = await waitForSession((current) => current.status === "capturing" || current.status === "error", 60_000);
    check("a second session starts", session?.status === "capturing", JSON.stringify(session));
    await page.send("Page.navigate", {url: `${appUrl}#/providers`});
    await waitForText("Remove key");
    await click("Remove key");
    await click("Remove this key?");
    session = await waitForSession((current) => current.status === "idle" || current.status === "error", 30_000);
    check(
      "removing the key in use ends the running session",
      session?.lastError === "ERR_KEY_REMOVED",
      JSON.stringify(session)
    );
    check("…and the key is gone from storage", !(await stored("local", "providerKeys"))?.[options.provider]);
    await waitForText("No provider is connected yet");
    check("the app says a key is needed again", true);

    const problems = page.consoleLines.filter(
      (line) =>
        /^\[(error|exception)\]/.test(line) &&
        // Expected: the wrong key's 401, and the backend probe before
        // "This browser" was chosen.
        !/Failed to load resource|ERR_CONNECTION_REFUSED/.test(line)
    );
    assertNoKey("the console", page.consoleLines.join("\n"));
    check("no errors in the app's console", problems.length === 0, problems.slice(0, 3).join(" | "));
  } finally {
    chrome.close();
    staticServer.close();
  }

  const failed = results.filter((result) => !result.pass);
  console.log(`\n${results.length - failed.length} of ${results.length} checks passed.`);
  return failed.length === 0 ? 0 : 1;
};

main().then(
  (exitCode) => process.exit(exitCode),
  (error) => {
    console.error(`\nerror: ${error.message}`);
    process.exit(1);
  }
);
