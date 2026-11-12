#!/usr/bin/env node
/**
 * Spike S3 — run local speech-to-text INSIDE the extension, unattended.
 *
 * Loads a patched copy of the unpacked extension into a real Chrome and runs
 * one of two tests:
 *
 *   --mode bench   the speech benchmark as an extension page
 *                  (chrome-extension://…/spike/stt_bench.html)
 *   --mode live    tab capture + local transcription together in an
 *                  offscreen document: a tab plays the fixture clip aloud,
 *                  the extension captures it and transcribes it live
 *
 * The extension copy differs from the source tree only in manifest.json,
 * according to --manifest:
 *
 *   plain      the manifest as committed
 *   csp        adds 'wasm-unsafe-eval' to the extension-page CSP
 *   isolated   csp + cross-origin isolation (COOP/COEP), which enables
 *              WebAssembly threads
 *
 * so each variant answers "does it work with exactly this manifest?".
 *
 * No dependencies: Node 22+ and a local Chrome.
 *
 * Usage (from the repo root; run scripts/vendor_stt_runtime.py first):
 *
 *     node scripts/run_extension_spike.mjs --mode bench --manifest csp --gpu
 *     node scripts/run_extension_spike.mjs --mode live --manifest csp --gpu \
 *         --model whisper-base.en --variant webgpu-mixed
 *     node scripts/run_extension_spike.mjs --mode live --manifest isolated
 *
 * Options:
 *   --mode bench|live
 *   --manifest plain|csp|isolated   (default csp)
 *   --models a,b        bench: model ids (default moonshine-base)
 *   --variants a,b      bench: restrict to these variant ids
 *   --model ID          live: model id (default moonshine-base)
 *   --variant ID        live: configuration (default: the model's first)
 *   --threads N         WASM threads
 *   --headed            show the browser window
 *   --gpu               headless only: add the Linux flags that expose the GPU
 *   --verbose           print page consoles and Chrome's GPU errors
 *   --chrome PATH       Chrome binary (default: google-chrome on PATH)
 *   --chrome-flag=FLAG  extra Chrome flag; repeatable
 *   --port N            loopback port for the audio page (default 8765)
 *   --timeout SECONDS   overall limit (default 900)
 *   --label NAME        report name (default: <mode>-<manifest>)
 *   --out DIR           report directory (default: docs/spike-results/raw)
 */

import {cp, mkdir, readFile, rm, writeFile} from "node:fs/promises";
import path from "node:path";

import {
  EXTENSION_DIR,
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
  unpackedExtensionId,
  waitForTitle,
} from "./lib/chrome_harness.mjs";

const WORK_DIR = path.join(REPO_ROOT, ".stt-bench");

const CLI_SPEC = {
  values: {
    "--mode": "mode",
    "--manifest": "manifest",
    "--models": "models",
    "--variants": "variants",
    "--model": "model",
    "--variant": "variant",
    "--threads": "threads",
    "--chrome": "chrome",
    "--port": "port",
    "--timeout": "timeoutS",
    "--label": "label",
    "--out": "out",
  },
  flags: {
    "--headed": ["headed", true],
    "--gpu": ["gpu", true],
    "--verbose": ["verbose", true],
  },
  defaults: {
    mode: null,
    manifest: "csp",
    models: "moonshine-base",
    variants: null,
    model: "moonshine-base",
    variant: null,
    threads: null,
    headed: false,
    gpu: false,
    verbose: false,
    chrome: "google-chrome",
    port: 8765,
    timeoutS: 900,
    label: null,
    out: path.join(REPO_ROOT, "docs", "spike-results", "raw"),
  },
};

const WASM_CSP = "script-src 'self' 'wasm-unsafe-eval'; object-src 'self'";

/** manifest.json edits per --manifest value. */
const MANIFEST_PATCHES = Object.freeze({
  plain: {},
  csp: {
    content_security_policy: {extension_pages: WASM_CSP},
  },
  isolated: {
    content_security_policy: {extension_pages: WASM_CSP},
    cross_origin_embedder_policy: {value: "require-corp"},
    cross_origin_opener_policy: {value: "same-origin"},
  },
});

/**
 * Copy the extension to a stable path (its id is derived from the path) and
 * apply the manifest patch.
 *
 * @param {string} manifestVariant
 * @returns {Promise<string>} the copy's absolute path
 */
const prepareExtensionCopy = async (manifestVariant) => {
  const target = path.join(WORK_DIR, `ext-${manifestVariant}`);
  await rm(target, {recursive: true, force: true});
  await cp(EXTENSION_DIR, target, {recursive: true});
  const manifestPath = path.join(target, "manifest.json");
  const manifest = JSON.parse(await readFile(manifestPath, "utf8"));
  Object.assign(manifest, MANIFEST_PATCHES[manifestVariant]);
  await writeFile(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`);
  return target;
};

/** @param {object} options @param {string} extensionId */
const runBench = async (options, chrome, extensionId) => {
  const query = new URLSearchParams({auto: "1", models: options.models});
  if (options.variants) {
    query.set("variants", options.variants);
  }
  if (options.threads) {
    query.set("threads", String(options.threads));
  }
  const page = await chrome.openPage(
    `chrome-extension://${extensionId}/spike/stt_bench.html?${query}`
  );
  try {
    await waitForTitle(page, {
      prefixes: ["bench:done", "bench:error"],
      timeoutS: Number(options.timeoutS),
      statusExpression: 'document.getElementById("status")?.textContent ?? ""',
    });
  } finally {
    if (options.verbose || page.consoleLines.some((line) => /error|exception|refused/i.test(line))) {
      console.log("\nExtension page console:\n" + (page.consoleLines.join("\n") || "(empty)"));
    }
  }
  const report = JSON.parse(await page.evaluate("JSON.stringify(window.__sttBenchReport)"));
  printBenchSummary(report);
  return report;
};

/** @param {object} options @param {string} extensionId @param {string} origin */
const runLive = async (options, chrome, extensionId, origin) => {
  const player = await chrome.openPage(`${origin}/fixture/player.html?delay=100000`);
  const query = new URLSearchParams({model: options.model, target: origin});
  if (options.variant) {
    query.set("variant", options.variant);
  }
  if (options.threads) {
    query.set("threads", String(options.threads));
  }
  const controller = await chrome.openPage(
    `chrome-extension://${extensionId}/spike/live_stt.html?${query}`
  );
  const statusExpression = 'document.getElementById("status")?.textContent ?? ""';
  try {
    const ready = await waitForTitle(controller, {
      prefixes: ["live:capturing", "live:error"],
      timeoutS: Number(options.timeoutS),
      statusExpression,
    });
    if (ready.startsWith("live:capturing")) {
      // Start the audio only now, so the whole clip is captured. userGesture
      // satisfies the autoplay policy without a launch flag.
      await player.send("Runtime.evaluate", {
        expression: 'document.getElementById("player").play()',
        userGesture: true,
        awaitPromise: true,
      });
      await waitForTitle(player, {
        prefixes: ["fixture-player:ended"],
        timeoutS: 120,
        statusExpression:
          '`playing ${document.getElementById("player").currentTime.toFixed(0)} s`',
      });
      // Let the last utterance's closing pause reach the segmenter.
      await new Promise((resolve) => setTimeout(resolve, 1500));
      await controller.evaluate("window.finishLiveStt()");
      await waitForTitle(controller, {
        prefixes: ["live:done", "live:error"],
        timeoutS: 300,
        statusExpression,
      });
    }
  } finally {
    if (options.verbose) {
      console.log("\nController console:\n" + (controller.consoleLines.join("\n") || "(empty)"));
    }
  }
  const report = JSON.parse(await controller.evaluate("JSON.stringify(window.__liveSttReport)"));
  if (report.error) {
    console.log(`\nLive test error: ${report.error}`);
    return report;
  }
  console.log("");
  console.table({
    "model / configuration": `${report.modelId} / ${report.variantId}`,
    "offscreen isolated": report.environment.crossOriginIsolated,
    "WASM threads": report.load.numThreads,
    "session GPU": report.load.sessionGpu
      ? `${report.load.sessionGpu.vendor} ${report.load.sessionGpu.architecture}`
      : "(CPU)",
    "model load (s)": (report.load.loadMs / 1000).toFixed(1),
    "audio received (s)": report.capture.receivedSeconds.toFixed(1),
    "longest gap between frames (ms)": Math.round(report.capture.maxFrameGapMs),
    "late frames": report.capture.lateFrames,
    "utterances transcribed / dropped": `${report.stats.segmentsTranscribed} / ${report.stats.segmentsDropped}`,
    "speed (x real time)": report.realTimeFactor ? (1 / report.realTimeFactor).toFixed(1) : "",
    "median / max lag (ms)": `${Math.round(report.medianLagMs)} / ${Math.round(report.maxLagMs)}`,
    "word error rate (%)": (report.wordErrorRate * 100).toFixed(1),
  });
  console.log(`\nTranscript: ${report.text}`);
  return report;
};

const main = async () => {
  const options = parseCli(process.argv.slice(2), CLI_SPEC);
  if (!["bench", "live"].includes(options.mode)) {
    throw new Error('--mode must be "bench" or "live"');
  }
  if (!(options.manifest in MANIFEST_PATCHES)) {
    throw new Error(`--manifest must be one of: ${Object.keys(MANIFEST_PATCHES).join(", ")}`);
  }
  const label = options.label ?? `${options.mode}-${options.manifest}`;
  await requireFile(
    RUNTIME_STAMP,
    "Speech runtime is not vendored — run: python3 scripts/vendor_stt_runtime.py"
  );
  if (options.mode === "live") {
    await requireFile(
      FIXTURE_WAV,
      "backend/tests/fixtures/claims_16k.wav is missing — build it with " +
        "backend/scripts/make_fixture_wav.py"
    );
  }

  const extensionPath = await prepareExtensionCopy(options.manifest);
  const expectedId = unpackedExtensionId(extensionPath);
  const server = await startStaticServer({isolated: false, port: Number(options.port)});
  const chromeFlags = [
    // DevTools may load unpacked extensions only with this switch.
    "--enable-unsafe-extension-debugging",
    // Lets the extension capture a tab without the toolbar click (activeTab)
    // that a person would provide.
    `--allowlisted-extension-id=${expectedId}`,
    ...(options.gpu && !options.headed ? LINUX_HEADLESS_GPU_FLAGS : []),
    ...options.chromeFlags,
  ];
  const chrome = await launchChrome({
    chrome: options.chrome,
    // One profile per manifest variant: the extension id (and so its model
    // cache) differs per copy, and the profile lives on disk, not in /tmp.
    profileDir: path.join(WORK_DIR, `profile-ext-${options.manifest}`),
    headed: options.headed,
    flags: chromeFlags,
  });
  try {
    const {id: extensionId} = await chrome.send("Extensions.loadUnpacked", {path: extensionPath});
    if (extensionId !== expectedId) {
      throw new Error(`Extension id ${extensionId} differs from the expected ${expectedId}.`);
    }
    console.log(`Loaded extension ${extensionId} (manifest: ${options.manifest})`);

    const report =
      options.mode === "bench"
        ? await runBench(options, chrome, extensionId)
        : await runLive(options, chrome, extensionId, server.origin);
    report.runner = {
      label,
      mode: options.mode,
      manifest: options.manifest,
      headless: !options.headed,
      chromeFlags,
      chromeVersion: chrome.version,
    };
    if (options.verbose) {
      const gpuLines = gpuStderrLines(chrome.readStderr());
      console.log("\nChrome GPU-related stderr:\n" + (gpuLines.join("\n") || "(none)"));
    }
    await mkdir(options.out, {recursive: true});
    const outputPath = path.join(options.out, `${label}.json`);
    await writeFile(outputPath, `${JSON.stringify(report, null, 2)}\n`);
    console.log(`\nSaved ${path.relative(REPO_ROOT, outputPath)}`);
    return report.error ? 1 : 0;
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
