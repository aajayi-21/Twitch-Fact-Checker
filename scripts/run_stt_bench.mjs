#!/usr/bin/env node
/**
 * Run the in-browser speech benchmark in a real Chrome, unattended.
 *
 * Serves the extension folder from a loopback static server, launches Chrome
 * with a throwaway profile, opens extension/spike/stt_bench.html with
 * `auto=1`, waits for the report over the DevTools protocol, prints a summary
 * table and saves the full JSON.
 *
 * This runs the page from http://127.0.0.1 — to run it as a real extension
 * page, use scripts/run_extension_spike.mjs --mode bench.
 *
 * No dependencies: Node 22+ and a local Chrome.
 *
 * Usage (from the repo root; run scripts/vendor_stt_runtime.py first):
 *
 *     node scripts/run_stt_bench.mjs --headed --label my-machine
 *     node scripts/run_stt_bench.mjs --models moonshine-base,whisper-base.en
 *     node scripts/run_stt_bench.mjs --fallback --allow-software --label software-gpu
 *     node scripts/run_stt_bench.mjs --clip fixture --label long-clip
 *     node scripts/run_stt_bench.mjs --no-isolated          # single-threaded CPU path
 *
 * Options:
 *   --models a,b        model ids (default: moonshine-base)
 *   --variants a,b      restrict to these variant ids
 *   --clip jfk|fixture  built-in 11 s clip, or backend/tests/fixtures/claims_16k.wav
 *   --fallback          force the software GPU adapter
 *   --allow-software    benchmark WebGPU on a software adapter
 *   --low-power         prefer the low-power GPU
 *   --stop-first        stop at the first passing configuration
 *   --threads N         WASM threads
 *   --no-isolated       serve without COOP/COEP (no SharedArrayBuffer/threads)
 *   --headed            show the browser window instead of running headless
 *   --gpu               headless only: add the Linux flags that expose the GPU
 *   --verbose           print the page's console and Chrome's GPU errors after the run
 *   --runtime-device    diagnostic: let the runtime create its own GPU device
 *   --chrome PATH       Chrome binary (default: google-chrome on PATH)
 *   --chrome-flag=FLAG  extra Chrome flag; repeatable
 *   --profile-dir DIR   Chrome profile (keeps downloaded models between runs)
 *   --port N            loopback port for the static server (default 8765)
 *   --timeout SECONDS   overall limit (default 1800)
 *   --label NAME        report name (default: run)
 *   --page PATH         page under extension/ to open (default: spike/stt_bench.html)
 *   --out DIR           report directory (default: docs/spike-results/raw)
 */

import {mkdir, writeFile} from "node:fs/promises";
import path from "node:path";

import {
  FIXTURE_WAV,
  LINUX_HEADLESS_GPU_FLAGS,
  REPO_ROOT,
  RUNTIME_STAMP,
  gpuStderrLines,
  launchChrome,
  parseCli,
  printBenchSummary,
  requireFile,
  startStaticServer,
  waitForTitle,
} from "./lib/chrome_harness.mjs";

const CLI_SPEC = {
  values: {
    "--models": "models",
    "--variants": "variants",
    "--clip": "clip",
    "--threads": "threads",
    "--chrome": "chrome",
    "--profile-dir": "profileDir",
    "--port": "port",
    "--timeout": "timeoutS",
    "--label": "label",
    "--page": "page",
    "--out": "out",
  },
  flags: {
    "--fallback": ["fallback", true],
    "--allow-software": ["allowSoftware", true],
    "--low-power": ["lowPower", true],
    "--stop-first": ["stopFirst", true],
    "--no-isolated": ["isolated", false],
    "--headed": ["headed", true],
    "--gpu": ["gpu", true],
    "--verbose": ["verbose", true],
    "--runtime-device": ["ownDevice", false],
  },
  defaults: {
    models: "moonshine-base",
    variants: null,
    clip: "jfk",
    fallback: false,
    allowSoftware: false,
    lowPower: false,
    stopFirst: false,
    threads: null,
    isolated: true,
    headed: false,
    gpu: false,
    verbose: false,
    ownDevice: true,
    chrome: "google-chrome",
    // On disk, not in /tmp: profiles hold hundreds of MB of model files.
    profileDir: path.join(REPO_ROOT, ".stt-bench", "profile-web"),
    port: 8765,
    timeoutS: 1800,
    label: "run",
    page: "spike/stt_bench.html",
    out: path.join(REPO_ROOT, "docs", "spike-results", "raw"),
  },
};

/** @param {object} options @param {string} origin */
const buildPageUrl = (options, origin) => {
  const query = new URLSearchParams({auto: "1", models: options.models});
  if (options.variants) {
    query.set("variants", options.variants);
  }
  if (options.clip === "fixture") {
    query.set("clipUrl", `${origin}/fixture/claims_16k.wav`);
    query.set("refUrl", `${origin}/fixture/claims_reference.txt`);
  }
  const switches = {
    fallback: options.fallback,
    allowSoftware: options.allowSoftware,
    stopFirst: options.stopFirst,
  };
  for (const [name, enabled] of Object.entries(switches)) {
    if (enabled) {
      query.set(name, "1");
    }
  }
  if (options.lowPower) {
    query.set("power", "low-power");
  }
  if (options.threads) {
    query.set("threads", String(options.threads));
  }
  if (!options.ownDevice) {
    query.set("ownDevice", "0");
  }
  return `${origin}/extension/${options.page}?${query}`;
};

const main = async () => {
  const options = parseCli(process.argv.slice(2), CLI_SPEC);
  if (!["jfk", "fixture"].includes(options.clip)) {
    throw new Error(`--clip must be "jfk" or "fixture"`);
  }
  await requireFile(
    RUNTIME_STAMP,
    "Speech runtime is not vendored — run: python3 scripts/vendor_stt_runtime.py"
  );
  if (options.clip === "fixture") {
    await requireFile(
      FIXTURE_WAV,
      "backend/tests/fixtures/claims_16k.wav is missing — build it with " +
        "backend/scripts/make_fixture_wav.py"
    );
  }

  const server = await startStaticServer({
    isolated: options.isolated,
    port: Number(options.port),
  });
  const chromeFlags = [
    ...(options.gpu && !options.headed ? LINUX_HEADLESS_GPU_FLAGS : []),
    ...options.chromeFlags,
  ];
  const chrome = await launchChrome({
    chrome: options.chrome,
    profileDir: options.profileDir,
    headed: options.headed,
    flags: chromeFlags,
  });
  let page = null;
  try {
    const pageUrl = buildPageUrl(options, server.origin);
    console.log(`Opening ${pageUrl}`);
    page = await chrome.openPage(pageUrl);
    await waitForTitle(page, {
      prefixes: ["bench:done", "bench:error"],
      timeoutS: Number(options.timeoutS),
      statusExpression: 'document.getElementById("status")?.textContent ?? ""',
    });

    const report = JSON.parse(await page.evaluate("JSON.stringify(window.__sttBenchReport)"));
    report.runner = {
      label: options.label,
      headless: !options.headed,
      chromeFlags,
      isolated: options.isolated,
      chromeVersion: chrome.version,
    };
    printBenchSummary(report);
    if (options.verbose) {
      console.log("\nPage console:\n" + (page.consoleLines.join("\n") || "(empty)"));
      const gpuLines = gpuStderrLines(chrome.readStderr());
      console.log("\nChrome GPU-related stderr:\n" + (gpuLines.join("\n") || "(none)"));
    }

    await mkdir(options.out, {recursive: true});
    const outputPath = path.join(options.out, `${options.label}.json`);
    await writeFile(outputPath, `${JSON.stringify(report, null, 2)}\n`);
    console.log(`\nSaved ${path.relative(REPO_ROOT, outputPath)}`);
    return report.error ? 1 : 0;
  } catch (error) {
    if (page?.consoleLines.length) {
      console.error("Page console (last 30 lines):\n" + page.consoleLines.slice(-30).join("\n"));
    }
    throw error;
  } finally {
    chrome.close();
    server.close();
  }
};

main().then(
  (exitCode) => process.exit(exitCode),
  (error) => {
    console.error(`error: ${error.message}`);
    process.exit(1);
  }
);
