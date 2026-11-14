#!/usr/bin/env node
/**
 * Service-worker lifecycle checks that unit tests cannot make (release build
 * plan, Phase 3).
 *
 *   1. The daily history clean-up alarm is created once. A worker that
 *      restarts leaves it alone; it used to re-arm it one minute out on every
 *      start, so its own firing woke the worker every minute, forever.
 *   2. A stream tab opened BEFORE the extension was installed gets its overlay
 *      when a session starts on it (the content scripts are injected).
 *
 * About check 2: the real Start comes from the toolbar popup, and that click
 * is what grants `activeTab`, the access to the tab that injection needs.
 * Automation cannot click the toolbar, so the copy of the extension loaded
 * here also gets a host permission for the test page, which grants the same
 * access to it. This checks everything the extension does with that access;
 * Chrome granting it on a click is the one part it takes on trust.
 *
 * Opens one public Twitch page, so it needs network access. The session it
 * starts is stopped at once; with no backend running, no audio goes anywhere.
 *
 * Usage (from the repo root):
 *
 *     node scripts/check_lifecycle.mjs
 *     node scripts/check_lifecycle.mjs --headed --url https://www.twitch.tv/somechannel
 *
 * Options:
 *   --url URL           the stream page to open before installing (default: Twitch)
 *   --headed            show the browser window
 *   --chrome PATH       Chrome binary (default: google-chrome on PATH)
 *   --chrome-flag=FLAG  extra Chrome flag; repeatable
 */

import {cp, mkdir, readFile, rm, writeFile} from "node:fs/promises";
import path from "node:path";

import {
  EXTENSION_DIR,
  REPO_ROOT,
  launchChrome,
  parseCli,
  unpackedExtensionId,
} from "./lib/chrome_harness.mjs";

const WORK_DIR = path.join(REPO_ROOT, ".stt-bench");
const RETENTION_ALARM = "ledger-retention";

const CLI_SPEC = {
  values: {"--url": "url", "--chrome": "chrome"},
  flags: {"--headed": ["headed", true]},
  defaults: {url: "https://www.twitch.tv/twitch", chrome: "google-chrome", headed: false},
};

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const results = [];
const check = (name, condition, detail = "") => {
  results.push({name, ok: Boolean(condition)});
  console.log(`  ${condition ? "ok  " : "FAIL"}  ${name}${!condition && detail ? `\n        ${detail}` : ""}`);
};

const main = async () => {
  const options = parseCli(process.argv.slice(2), CLI_SPEC);
  const extensionPath = path.join(WORK_DIR, "ext-lifecycle-check");
  await rm(extensionPath, {recursive: true, force: true});
  // The speech runtime is not needed here and is large.
  await cp(EXTENSION_DIR, extensionPath, {
    recursive: true,
    filter: (source) => !source.includes(`${path.sep}vendor${path.sep}transformers`),
  });
  // Stands in for the activeTab grant of a toolbar click (see the header).
  const manifestPath = path.join(extensionPath, "manifest.json");
  const manifest = JSON.parse(await readFile(manifestPath, "utf8"));
  const streamPattern = `${new URL(options.url).origin}/*`;
  manifest.host_permissions = [...(manifest.host_permissions ?? []), streamPattern];
  await writeFile(manifestPath, JSON.stringify(manifest, null, 2));
  const profileDir = path.join(WORK_DIR, "profile-lifecycle-check");
  await rm(profileDir, {recursive: true, force: true});
  await mkdir(WORK_DIR, {recursive: true});

  const chrome = await launchChrome({
    chrome: options.chrome,
    profileDir,
    headed: options.headed,
    flags: [
      "--enable-unsafe-extension-debugging",
      "--mute-audio",
      // Lets tabCapture run without a toolbar click, as in the other e2e scripts.
      `--allowlisted-extension-id=${unpackedExtensionId(extensionPath)}`,
      ...options.chromeFlags,
    ],
  });
  try {
    console.log(`\nA stream tab opened before the extension is installed: ${options.url}`);
    const stream = await chrome.openPage(options.url);
    await pause(6000);

    const {id: extensionId} = await chrome.send("Extensions.loadUnpacked", {path: extensionPath});
    // An extension page, to reach the extension APIs from.
    const control = await chrome.openPage(`chrome-extension://${extensionId}/popup/popup.html`);
    const run = (expression) => control.evaluate(`(async () => (${expression}))()`);
    const alarmTime = () =>
      run(`chrome.alarms.get(${JSON.stringify(RETENTION_ALARM)}).then((alarm) => alarm?.scheduledTime ?? null)`);

    console.log("\nThe history clean-up alarm");
    let firstTime = null;
    for (let attempt = 0; attempt < 20 && firstTime === null; attempt += 1) {
      await pause(250);
      firstTime = await alarmTime();
    }
    check("the alarm exists after install", firstTime !== null);
    await control.send("ServiceWorker.enable");
    await pause(2000); // so a re-armed alarm would land visibly later
    await control.send("ServiceWorker.stopAllWorkers");
    await pause(500);
    // Any message wakes the worker; this one is ignored (it is not from a tab).
    await run(
      `chrome.runtime.sendMessage({target: "background", type: "PAGE_IDENTITY", payload: {}}).then(() => true, () => true)`
    );
    await pause(1500);
    const afterRestart = await alarmTime();
    check(
      "a restarted worker leaves the alarm where it was",
      afterRestart === firstTime,
      `scheduled ${new Date(firstTime).toISOString()}, after the restart ${afterRestart && new Date(afterRestart).toISOString()}`
    );

    console.log("\nThe overlay in a tab that predates the extension");
    const tabId = await run(
      `chrome.tabs.query({url: ${JSON.stringify(streamPattern)}}).then(([tab]) => tab?.id ?? null)`
    );
    if (typeof tabId !== "number") {
      throw new Error("could not find the stream tab");
    }
    const contentAnswers = () =>
      run(`chrome.tabs.sendMessage(${tabId}, {type: "GET_PAGE_IDENTITY"}).then(() => true, () => false)`);
    check("before Start, the tab has no content script", (await contentAnswers()) === false);
    await run(
      `chrome.runtime.sendMessage({target: "background", type: "CAPTURE_START", payload: {tabId: ${tabId}}}).then(() => true)`
    );
    let answers = false;
    for (let attempt = 0; attempt < 40 && !answers; attempt += 1) {
      await pause(250);
      answers = await contentAnswers();
    }
    check("after Start, the content script is there and answers", answers);
    const overlays = await stream.evaluate(`document.querySelectorAll("#stream-fact-checker-overlay").length`);
    console.log(`        overlay elements on the page: ${overlays}`);
    check("no more than one overlay", overlays <= 1, `${overlays} overlays`);
    await run(`chrome.runtime.sendMessage({target: "background", type: "CAPTURE_STOP", payload: {}}).then(() => true)`);
    await pause(1000);
  } finally {
    chrome.close();
  }
  const passed = results.filter((result) => result.ok).length;
  console.log(`\n${passed} of ${results.length} checks passed.`);
  return passed === results.length ? 0 : 1;
};

main()
  .then((code) => process.exit(code))
  .catch((error) => {
    console.error(`\nerror: ${error.stack ?? error}`);
    process.exit(1);
  });
