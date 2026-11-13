#!/usr/bin/env node
/**
 * Make the Chrome Web Store images from the real extension.
 *
 *   docs/store/assets/1-overlay.png     a verdict card over a player (1280×800)
 *   docs/store/assets/2-welcome.png     the first setup step
 *   docs/store/assets/3-history.png     History, one result opened
 *   docs/store/assets/4-usage.png       Usage
 *   docs/store/assets/5-providers.png   AI provider and spending limits
 *   docs/store/assets/promo-440x280.png the small promo tile
 *
 * The screens are the extension's own, loaded in Chrome and filled with
 * made-up history (the same seed the end-to-end test uses). Nothing is
 * drawn by hand, so the images cannot drift from the product: re-run this
 * after a visual change.
 *
 * The overlay shot uses the real overlay code over an ILLUSTRATED player —
 * a page added to a temporary copy of the extension — because a screenshot
 * of a real stream would show someone else's content.
 *
 * No provider calls, no backend, no network. Placeholder API keys are used
 * so the provider screen shows a connected state; they are not real.
 *
 * Usage (from the repo root):
 *
 *     node scripts/make_store_assets.mjs
 *
 * Options:
 *   --out DIR            where to write (default docs/store/assets)
 *   --chrome PATH        Chrome binary (default: google-chrome on PATH)
 *   --chrome-flag=FLAG   extra Chrome flag; repeatable
 */

import {cp, mkdir, rm, writeFile} from "node:fs/promises";
import path from "node:path";

import {DEMO_RECORDING} from "../extension/engine/demo/recording.mjs";
import {
  EXTENSION_DIR,
  REPO_ROOT,
  launchChrome,
  parseCli,
  unpackedExtensionId,
} from "./lib/chrome_harness.mjs";

const WORK_DIR = path.join(REPO_ROOT, ".stt-bench");
const SCREEN = Object.freeze({width: 1280, height: 800});
const PROMO = Object.freeze({width: 440, height: 280});

const CLI_SPEC = {
  values: {"--out": "out", "--chrome": "chrome"},
  flags: {},
  defaults: {out: path.join(REPO_ROOT, "docs", "store", "assets"), chrome: "google-chrome"},
};

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const HOUR_MS = 3_600_000;
const iso = (ms) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");

/**
 * Made-up viewing history for the screenshots: invented channels, and
 * well-known statements whose verdicts are not in doubt, so a store image
 * never shows the product getting a fact wrong. The four from the demo
 * recording carry their real explanations and sources.
 */
const EXAMPLE_CLAIMS = Object.freeze([
  ["TRUE", "Mount Everest is 8,849 metres high.", "science_tech", "The 2020 China–Nepal survey set the height at 8,848.86 metres.", "https://en.wikipedia.org/wiki/Mount_Everest"],
  ["FALSE", "Humans only use ten percent of their brains.", "health", "Brain imaging shows activity across virtually the whole brain; the ten-percent figure is a long-debunked myth.", "https://en.wikipedia.org/wiki/Ten_percent_of_the_brain_myth"],
  ["TRUE", "The Berlin Wall fell in 1989.", "history", "The border was opened on 9 November 1989.", "https://en.wikipedia.org/wiki/Fall_of_the_Berlin_Wall"],
  ["FALSE", "Lightning never strikes the same place twice.", "science_tech", "Tall structures are struck repeatedly; the Empire State Building is hit around twenty times a year.", "https://en.wikipedia.org/wiki/Lightning"],
  ["MISLEADING", "The Sahara is the largest desert in the world.", "science_tech", "It is the largest hot desert. Antarctica and the Arctic, which are also deserts, are both larger.", "https://en.wikipedia.org/wiki/List_of_deserts_by_area"],
  ["TRUE", "A marathon is 42.195 kilometres long.", "sports", "The distance was standardised at 42.195 km in 1921.", "https://en.wikipedia.org/wiki/Marathon"],
  ["FALSE", "Goldfish have a three-second memory.", "science_tech", "Studies show goldfish remember learned tasks for months.", "https://en.wikipedia.org/wiki/Goldfish"],
  ["TRUE", "Venus is the hottest planet in the solar system.", "science_tech", "Its surface averages about 465 °C, hotter than Mercury despite being farther from the Sun.", "https://en.wikipedia.org/wiki/Venus"],
  ["UNVERIFIED", "Police are making arrests at the port right now.", "politics", "No reliable source could confirm an event said to be happening at this moment.", null],
  ["TRUE", "Water boils at 100 degrees Celsius at sea level.", "science_tech", "At standard atmospheric pressure the boiling point of water is 100 °C.", "https://en.wikipedia.org/wiki/Boiling_point"],
  ["FALSE", "Vikings wore horned helmets into battle.", "history", "No horned helmet has been found in a Viking-age battle context; the image comes from nineteenth-century costume design.", "https://en.wikipedia.org/wiki/Horned_helmet"],
]);

const exampleHistory = (now = Date.now()) => {
  const recorded = DEMO_RECORDING.claims.map((claim) => [
    claim.verdict.label,
    claim.claim_text,
    claim.topic,
    claim.verdict.explanation,
    claim.verdict.sources,
  ]);
  const session = (id, platform, channel, title, hoursAgo, hours, claims) => {
    const startMs = now - hoursAgo * HOUR_MS;
    return {
      session: {
        id,
        platform,
        channel,
        title,
        started_at: iso(startMs),
        ended_at: iso(startMs + hours * HOUR_MS),
        speech_seconds: hours * 3000,
        audio_seconds: hours * 3600,
        gate_calls: Math.round(hours * 50),
        verify_calls: claims.length,
        est_cost_usd: Number((claims.length * 0.0112).toFixed(4)),
      },
      claims: claims.map(([, text, topic], index) => ({
        id: `${id}-c${index}`,
        text,
        topic,
        check_worthiness: 0.85,
        gated_at: iso(startMs + (index + 1) * 600_000),
        outcome: "verified",
      })),
      verdicts: claims.map(([label, , , explanation, sources], index) => ({
        id: `${id}-v${index}`,
        claim_id: `${id}-c${index}`,
        label,
        explanation,
        checked_at: iso(startMs + (index + 1) * 600_000 + 3000),
        used_fallback: 0,
        latency_ms: 2100 + index * 130,
        provider: "anthropic",
        model: "claude-haiku-5-5",
        sources: (Array.isArray(sources) ? sources : sources ? [{url: sources, title: null}] : []).map(
          (source, rank) => ({rank, url: source.url, title: source.title ?? null})
        ),
        feedback: null,
      })),
    };
  };
  return [
    // Newest first in History: the opened result is a recorded, real one.
    session("x-1", "twitch", "DebateStreamer", "Friday debate night", 2, 1.5, [...EXAMPLE_CLAIMS.slice(0, 3), ...recorded.slice(1), recorded[0]]),
    session("x-2", "youtube", "@newsanalyst", "Budget speech, live reaction", 27, 2, EXAMPLE_CLAIMS.slice(3, 8)),
    session("x-3", "kick", "latenightpod", "Late show", 52, 1, EXAMPLE_CLAIMS.slice(8, 10)),
    session("x-4", "twitch", "DebateStreamer", "Election special", 98, 2.5, EXAMPLE_CLAIMS.slice(10)),
  ];
};

/** The illustrated player: the real overlay, mounted over a drawn backdrop. */
const MOCK_STREAM_HTML = `<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>Illustration</title>
    <link rel="stylesheet" href="./mock.css" />
  </head>
  <body>
    <div id="player" class="player">
      <div class="scene"></div>
      <div class="bar"><span class="live">LIVE</span><span class="name">Sample stream</span></div>
    </div>
    <script src="../content/overlay.js"></script>
    <script type="module" src="./mock_stream.js"></script>
  </body>
</html>
`;

const MOCK_CSS = `
html, body { margin: 0; height: 100%; background: #08080c; font-family: "Segoe UI", system-ui, sans-serif; }
.player { position: relative; width: 100vw; height: 100vh; overflow: hidden; }
.scene {
  position: absolute; inset: 0;
  background:
    radial-gradient(ellipse at 28% 62%, rgba(145, 71, 255, 0.34), transparent 55%),
    radial-gradient(ellipse at 74% 30%, rgba(52, 210, 123, 0.16), transparent 50%),
    linear-gradient(160deg, #1a1a2b 0%, #0f0f18 60%, #171325 100%);
}
.bar {
  position: absolute; left: 0; right: 0; bottom: 0; display: flex; align-items: center; gap: 12px;
  padding: 18px 24px; color: #e9e9f2; font-size: 15px;
  background: linear-gradient(transparent, rgba(0, 0, 0, 0.65));
}
.live { padding: 2px 8px; border-radius: 4px; background: #e53935; font-weight: 700; font-size: 12px; letter-spacing: 0.05em; }
.name { opacity: 0.85; }
.promo {
  display: flex; flex-direction: column; justify-content: center; gap: 14px; height: 100vh; box-sizing: border-box;
  padding: 0 34px; color: #efeff4;
  background:
    radial-gradient(ellipse at 85% 15%, rgba(145, 71, 255, 0.45), transparent 60%),
    linear-gradient(150deg, #1b1b2a, #0e0e14);
}
.promo-head { display: flex; align-items: center; gap: 14px; }
.promo-head img { width: 56px; height: 56px; }
.promo-name { font-size: 25px; font-weight: 800; line-height: 1.1; letter-spacing: -0.01em; }
.promo-line { font-size: 15.5px; line-height: 1.35; color: #c9c9da; }
.chips { display: flex; gap: 8px; }
.chip { padding: 3px 10px; border-radius: 5px; font-size: 12px; font-weight: 800; letter-spacing: 0.04em; color: #101018; }
`;

const mockStreamScript = (verdicts) => `
const overlay = new FactCheckOverlay({popupPosition: "top-right", popupDurationS: 60, showTranscript: false});
overlay.mount(document.getElementById("player"));
overlay.setConnectionState(true);
const verdicts = ${JSON.stringify(verdicts)};
overlay.renderHistory(verdicts);
overlay.addVerdict(verdicts[0]);
document.title = "ready";
`;

const PROMO_HTML = `<!doctype html>
<html lang="en">
  <head><meta charset="utf-8" /><title>Promo</title><link rel="stylesheet" href="./mock.css" /></head>
  <body>
    <div class="promo">
      <div class="promo-head">
        <img src="../icons/icon128.png" alt="" />
        <div class="promo-name">Live Stream<br />Fact-Checker</div>
      </div>
      <div class="promo-line">Sourced verdicts over the stream you are watching.</div>
      <div class="chips">
        <span class="chip" style="background:#34d27b">TRUE</span>
        <span class="chip" style="background:#ff6b6b">FALSE</span>
        <span class="chip" style="background:#f5b547">MISLEADING</span>
        <span class="chip" style="background:#8b8fa3">UNVERIFIED</span>
      </div>
    </div>
  </body>
</html>
`;

const main = async () => {
  const options = parseCli(process.argv.slice(2), CLI_SPEC);
  await mkdir(options.out, {recursive: true});

  const extensionPath = path.join(WORK_DIR, "ext-store-assets");
  await rm(extensionPath, {recursive: true, force: true});
  await cp(EXTENSION_DIR, extensionPath, {
    recursive: true,
    filter: (source) => !source.includes(`${path.sep}vendor${path.sep}transformers`),
  });
  const verdicts = DEMO_RECORDING.claims.map((claim, index) => ({
    id: `mock-${index}`,
    claim: claim.claim_text,
    topic: claim.topic,
    label: claim.verdict.label,
    explanation: claim.verdict.explanation,
    sources: claim.verdict.sources.slice(0, 3),
    checked_at: new Date().toISOString(),
  }));
  const mockDir = path.join(extensionPath, "mock");
  await mkdir(mockDir, {recursive: true});
  await writeFile(path.join(mockDir, "mock_stream.html"), MOCK_STREAM_HTML);
  await writeFile(path.join(mockDir, "mock_stream.js"), mockStreamScript(verdicts));
  await writeFile(path.join(mockDir, "mock.css"), MOCK_CSS);
  await writeFile(path.join(mockDir, "promo.html"), PROMO_HTML);

  const profileDir = path.join(WORK_DIR, "profile-store-assets");
  await rm(profileDir, {recursive: true, force: true});
  const chrome = await launchChrome({
    chrome: options.chrome,
    profileDir,
    headed: false,
    flags: [
      "--enable-unsafe-extension-debugging",
      `--allowlisted-extension-id=${unpackedExtensionId(extensionPath)}`,
      `--window-size=${SCREEN.width},${SCREEN.height}`,
      ...options.chromeFlags,
    ],
  });

  try {
    const {id} = await chrome.send("Extensions.loadUnpacked", {path: extensionPath});
    const base = `chrome-extension://${id}`;
    const page = await chrome.openPage(`${base}/app/app.html#/home`);
    await page.send("Page.enable");
    const run = (expression) => page.evaluate(`(async () => (${expression}))()`);
    const shoot = async (target, name, size = SCREEN) => {
      await target.send("Emulation.setDeviceMetricsOverride", {...size, deviceScaleFactor: 1, mobile: false});
      await pause(350);
      const {data} = await target.send("Page.captureScreenshot", {format: "png"});
      await writeFile(path.join(options.out, name), Buffer.from(data, "base64"));
      console.log(`  ${name}`);
    };
    const goTo = async (route, waitFor) => {
      await page.send("Page.navigate", {url: `${base}/app/app.html#${route}`});
      await pause(300);
      await page.send("Page.reload");
      for (let attempt = 0; attempt < 60; attempt += 1) {
        await pause(250);
        if ((await run("document.body.innerText").catch(() => "")).includes(waitFor)) {
          return;
        }
      }
      throw new Error(`"${waitFor}" never appeared on ${route}`);
    };
    const click = (label) =>
      run(`(() => {
        const target = [...document.querySelectorAll("button, summary, label")].find(
          (node) => node.innerText.trim().startsWith(${JSON.stringify(label)})
        );
        if (!target) { throw new Error("nothing clickable starts with: ${label}"); }
        target.click();
        return true;
      })()`);

    await pause(1500); // let the install-time welcome tab settle
    // A finished setup on the browser engine, with made-up history. The keys
    // are placeholders: only their last four characters are ever displayed.
    await run(`(async () => {
      const setup = await import("../engine/stt/speech_setup.mjs");
      const store = await import("../engine/stt/speech_store.mjs");
      const {getSttModel} = await import("../engine/stt/models.mjs");
      const model = getSttModel("moonshine-base");
      const key = setup.environmentKey({model, gpuCacheKey: "x", browserMajor: store.browserMajorVersion(),
        runtimeVersion: "missing", devicePreference: "auto", powerPreference: "high-performance"});
      await chrome.storage.local.set({
        onboarding: {consentAt: "x", completedAt: "x"},
        engine: {mode: "browser"},
        providerKeys: {
          openrouter: "sk-or-placeholder-not-a-real-key-7f3a",
          anthropic: "sk-ant-placeholder-not-a-real-key-c41d",
        },
        speech: {settings: {engine: "browser", modelId: "moonshine-base"}, results: {"moonshine-base": {
          testedAt: "t", environmentKey: key, gpuLabel: "", runs: [], skipped: [],
          selected: {variantId: "wasm-q8", device: "wasm", realTimeFactor: 0.066, wordErrorRate: 0, numThreads: 1}}}},
      });
      const {recordsFromBackendSession} = await import("../ledger/records.mjs");
      const ledger = await import("../ledger/ledger.mjs");
      const batches = ${JSON.stringify(exampleHistory())}
        .map(recordsFromBackendSession)
        .map((batch) => {
          // Shown as sessions this browser recorded: one priced usage row
          // per provider call, with token counts typical of a real check.
          const [summary] = batch.usage;
          const call = (verdict, fields) => ({
            ...summary,
            imported: false,
            ts: verdict.checked_at,
            calls: 1,
            pricing_version: "example",
            ...fields,
          });
          return {
            ...batch,
            session: {...batch.session, imported: false, engine: "browser"},
            usage: batch.verdicts.flatMap((verdict) => [
              call(verdict, {stage: "gate", input_tokens: 310, cached_input_tokens: 2959,
                output_tokens: 110, web_searches: 0, cost_usd: 0.000116, verdict_id: null}),
              call(verdict, {stage: "verify", input_tokens: 8200, cached_input_tokens: 0,
                output_tokens: 190, web_searches: verdict.sources.length > 0 ? 1 : 0,
                cost_usd: verdict.sources.length > 0 ? 0.0109 : 0.0009, verdict_id: verdict.id}),
            ]),
          };
        });
      await ledger.importSessions(batches);
      return true;
    })()`);

    console.log(`Writing to ${path.relative(REPO_ROOT, options.out)}/`);

    const stream = await chrome.openPage(`${base}/mock/mock_stream.html`);
    for (let attempt = 0; attempt < 40 && (await stream.evaluate("document.title")) !== "ready"; attempt += 1) {
      await pause(150);
    }
    await pause(900); // the card's entrance animation
    await shoot(stream, "1-overlay.png");
    await stream.send("Page.close").catch(() => {});
    await page.send("Page.bringToFront");

    await goTo("/welcome", "What this extension does");
    await shoot(page, "2-welcome.png");

    await goTo("/history", "Results");
    // Open the newest result (one of the recorded, real ones).
    await run(`(document.querySelector(".result-summary").click(), true)`);
    await pause(300);
    await shoot(page, "3-history.png");

    await goTo("/usage", "Spend per day");
    await shoot(page, "4-usage.png");

    await goTo("/providers", "Spending limits");
    await shoot(page, "5-providers.png");

    const promo = await chrome.openPage(`${base}/mock/promo.html`);
    await pause(500);
    await shoot(promo, "promo-440x280.png", PROMO);
  } finally {
    chrome.close();
    // Chrome is still letting go of its profile at this point; clearing up
    // is a courtesy and must never hide a real error from above.
    await pause(500);
    await rm(extensionPath, {recursive: true, force: true}).catch(() => {});
    await rm(profileDir, {recursive: true, force: true, maxRetries: 5, retryDelay: 300}).catch(() => {});
  }
  return 0;
};

main().then(
  (exitCode) => process.exit(exitCode),
  (error) => {
    console.error(`\nerror: ${error.message}`);
    process.exit(1);
  }
);
