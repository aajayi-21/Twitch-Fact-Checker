#!/usr/bin/env node
/**
 * Does the extension still find the channel on the real sites?
 *
 * YouTube and Rumble address a stream by video id, so the history's channel
 * attribution depends on reading the channel link out of the page
 * (extension/content/page_identity.js). Those layouts change without notice
 * and cannot be unit-tested, so this opens real pages in Chrome with the
 * extension loaded, asks the content script what it sees, and prints the
 * identity the service worker would record.
 *
 * It only loads public pages and reads them; nothing is captured or sent to
 * a backend. Needs network access.
 *
 * Usage (from the repo root):
 *
 *     node scripts/check_page_identity.mjs URL [URL…]
 *     node scripts/check_page_identity.mjs --headed "https://www.youtube.com/watch?v=jNQXAC9IVRw"
 *
 * Options:
 *   --headed            show the browser window (some sites refuse headless)
 *   --wait SECONDS      how long to wait for the channel link (default 15)
 *   --chrome PATH       Chrome binary (default: google-chrome on PATH)
 *   --chrome-flag=FLAG  extra Chrome flag; repeatable
 *
 * Exit code 1 if any YouTube or Rumble video page ends up without a channel.
 */

import {cp, mkdir, rm} from "node:fs/promises";
import path from "node:path";

import {identityFromPage} from "../extension/ledger/stream_identity.mjs";
import {EXTENSION_DIR, REPO_ROOT, launchChrome, parseCli} from "./lib/chrome_harness.mjs";

const WORK_DIR = path.join(REPO_ROOT, ".stt-bench");

const CLI_SPEC = {
  values: {"--wait": "wait", "--chrome": "chrome"},
  flags: {"--headed": ["headed", true]},
  defaults: {wait: "15", chrome: "google-chrome", headed: false},
};

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const main = async () => {
  const argv = process.argv.slice(2);
  const urls = argv.filter((argument) => /^https?:\/\//.test(argument));
  const options = parseCli(
    argv.filter((argument) => !urls.includes(argument)),
    CLI_SPEC
  );
  if (urls.length === 0) {
    console.error("usage: node scripts/check_page_identity.mjs [--headed] URL [URL…]");
    return 2;
  }

  const extensionPath = path.join(WORK_DIR, "ext-identity-check");
  await rm(extensionPath, {recursive: true, force: true});
  await cp(EXTENSION_DIR, extensionPath, {
    recursive: true,
    filter: (source) => !source.includes(`${path.sep}vendor${path.sep}transformers`),
  });
  const profileDir = path.join(WORK_DIR, "profile-identity-check");
  await rm(profileDir, {recursive: true, force: true});
  await mkdir(WORK_DIR, {recursive: true});

  const chrome = await launchChrome({
    chrome: options.chrome,
    profileDir,
    headed: options.headed,
    flags: ["--enable-unsafe-extension-debugging", "--mute-audio", ...options.chromeFlags],
  });
  let missing = 0;
  try {
    const {id: extensionId} = await chrome.send("Extensions.loadUnpacked", {path: extensionPath});
    // An extension page, to reach chrome.tabs from.
    const control = await chrome.openPage(`chrome-extension://${extensionId}/popup/popup.html`);
    const askEveryTab = () =>
      control.evaluate(`(async () => {
        const replies = [];
        for (const tab of await chrome.tabs.query({})) {
          try {
            const reply = await chrome.tabs.sendMessage(tab.id, {type: "GET_PAGE_IDENTITY", payload: {}});
            if (reply) { replies.push(reply); }
          } catch { /* no content script in this tab */ }
        }
        return replies;
      })()`);

    for (const url of urls) {
      const page = await chrome.openPage(url);
      const deadline = Date.now() + Number(options.wait) * 1000;
      let owner = null;
      let location = {href: url, title: ""};
      for (;;) {
        await pause(1000);
        location = await page
          .evaluate(`({href: location.href, title: document.title})`)
          .catch(() => location);
        // Only this URL's tab is open besides the control page.
        [owner = null] = await askEveryTab();
        if (owner?.ownerHref || Date.now() > deadline) {
          break;
        }
      }
      const identity = identityFromPage({
        url: location.href,
        title: location.title,
        ownerHref: owner?.ownerHref ?? null,
        ownerName: owner?.ownerName ?? null,
      });
      const needsOwnerLink = ["youtube", "rumble"].includes(identity.platform);
      const found = Boolean(identity.channelId);
      if (needsOwnerLink && !found) {
        missing += 1;
      }
      console.log(`\n${found ? "ok  " : needsOwnerLink ? "FAIL" : "note"}  ${url}`);
      console.log(`      landed on      ${location.href}`);
      console.log(`      content script ${owner ? JSON.stringify(owner) : "did not answer (not a supported page?)"}`);
      console.log(`      identity       ${JSON.stringify(identity)}`);
      await page.send("Page.close").catch(() => {});
    }
  } finally {
    chrome.close();
  }
  return missing === 0 ? 0 : 1;
};

main().then(
  (exitCode) => process.exit(exitCode),
  (error) => {
    console.error(`\nerror: ${error.message}`);
    process.exit(1);
  }
);
