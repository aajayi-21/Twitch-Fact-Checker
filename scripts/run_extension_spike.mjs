#!/usr/bin/env node
/**
 * Run the browser-engine spikes INSIDE the extension, unattended.
 *
 * Loads a patched copy of the unpacked extension into a real Chrome and runs
 * one of three tests:
 *
 *   --mode bench      (S3) the speech benchmark as an extension page
 *                     (chrome-extension://…/spike/stt_bench.html)
 *   --mode live       (S3) tab capture + local transcription together in an
 *                     offscreen document: a tab plays the fixture clip aloud,
 *                     the extension captures it and transcribes it live
 *   --mode providers  (S2) claim extraction and web-grounded verification
 *                     against each AI provider, from an offscreen document.
 *                     Needs API keys in a git-ignored file (see below) and
 *                     spends a few cents per provider.
 *
 * The extension copy differs from the source tree only in manifest.json,
 * according to --manifest:
 *
 *   plain      the manifest as committed
 *   csp        adds 'wasm-unsafe-eval' to the extension-page CSP
 *   isolated   csp + cross-origin isolation (COOP/COEP), which enables
 *              WebAssembly threads
 *   hosts      plain + host permissions for the four provider APIs
 *
 * API keys for --mode providers are read from `.env.spike` in the repo root
 * (already git-ignored by the `.env.*` rule), one per line; providers
 * without a key are skipped:
 *
 *     OPENROUTER_API_KEY=...
 *     ANTHROPIC_API_KEY=...
 *     OPENAI_API_KEY=...
 *     GEMINI_API_KEY=...
 *
 * Keys are handed to the extension page over the local DevTools pipe. They
 * are never printed, never put in a URL, and never written to a report.
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
 *     node scripts/run_extension_spike.mjs --mode providers
 *     node scripts/run_extension_spike.mjs --mode providers --manifest hosts
 *
 * Options:
 *   --mode bench|live|providers
 *   --manifest plain|csp|isolated|hosts   (default csp; providers: plain)
 *   --keys FILE         providers: key file (default .env.spike)
 *   --only a,b          providers: test only these provider ids
 *   --provider-model ID=MODEL   providers: use MODEL for both stages of
 *                       provider ID (e.g. openrouter=openai/gpt-6-luna)
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
    "--keys": "keys",
    "--only": "only",
    "--provider-model": "providerModel",
  },
  flags: {
    "--headed": ["headed", true],
    "--gpu": ["gpu", true],
    "--verbose": ["verbose", true],
  },
  defaults: {
    mode: null,
    manifest: null,
    keys: path.join(REPO_ROOT, ".env.spike"),
    only: null,
    providerModel: null,
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
  hosts: {},
});

/** Environment-variable name -> provider id, for the key file. */
const KEY_VARIABLES = Object.freeze({
  OPENROUTER_API_KEY: "openrouter",
  ANTHROPIC_API_KEY: "anthropic",
  OPENAI_API_KEY: "openai",
  GEMINI_API_KEY: "gemini",
});

/** Origins added to host_permissions by `--manifest hosts`. */
const PROVIDER_ORIGINS = Object.freeze([
  "https://openrouter.ai/*",
  "https://api.anthropic.com/*",
  "https://api.openai.com/*",
  "https://generativelanguage.googleapis.com/*",
]);

/**
 * Read API keys from a KEY=value file. Values never leave this process
 * except over the DevTools pipe to the extension page under test.
 *
 * @param {string} filePath
 * @param {string[]|null} onlyProviders
 * @returns {Promise<Record<string, string>>} provider id -> key
 */
const readKeyFile = async (filePath, onlyProviders) => {
  let text;
  try {
    text = await readFile(filePath, "utf8");
  } catch {
    throw new Error(
      `No key file at ${path.relative(REPO_ROOT, filePath)}. Create it with lines such as ` +
        `OPENROUTER_API_KEY=... (see the header of this script).`
    );
  }
  const keys = {};
  for (const line of text.split("\n")) {
    const match = line.match(/^\s*(?:export\s+)?([A-Z_]+)\s*=\s*(.*?)\s*$/);
    const providerId = match && KEY_VARIABLES[match[1]];
    const value = match?.[2].replace(/^["']|["']$/g, "");
    if (providerId && value && (!onlyProviders || onlyProviders.includes(providerId))) {
      keys[providerId] = value;
    }
  }
  return keys;
};

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
  if (manifestVariant === "hosts") {
    manifest.host_permissions = [...manifest.host_permissions, ...PROVIDER_ORIGINS];
  }
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

/** One line per call, with no key material. */
const describeCall = (call) => {
  if (!call.ok) {
    return `FAILED (${call.error.kind}${call.error.status ? ` ${call.error.status}` : ""}): ${String(call.error.message).slice(0, 110)}`;
  }
  const usage = call.usage;
  const cost = usage.costUsd === null ? "" : `, $${usage.costUsd.toFixed(4)}`;
  return (
    `${(call.ms / 1000).toFixed(1)} s, ${usage.inputTokens}+${usage.cachedInputTokens} in / ` +
    `${usage.outputTokens} out, ${usage.webSearches} search(es), ${usage.calls} call(s)${cost}`
  );
};

/** @param {object} options @param {string} extensionId */
const runProviders = async (options, chrome, extensionId) => {
  const onlyProviders = options.only ? options.only.split(",").filter(Boolean) : null;
  const keys = await readKeyFile(options.keys, onlyProviders);
  const found = Object.keys(keys);
  if (found.length === 0) {
    throw new Error("The key file holds no usable keys.");
  }
  console.log(`Keys found for: ${found.join(", ")}`);

  const controller = await chrome.openPage(`chrome-extension://${extensionId}/spike/providers.html`);
  const statusExpression = 'document.getElementById("status")?.textContent ?? ""';
  await waitForTitle(controller, {prefixes: ["providers:ready"], timeoutS: 30});
  // Fire and poll: the run takes minutes, longer than one evaluate should block.
  const models = {};
  if (options.providerModel) {
    const [providerId, ...rest] = options.providerModel.split("=");
    const model = rest.join("=");
    if (!providerId || !model) {
      throw new Error("--provider-model expects ID=MODEL");
    }
    models[providerId] = {gate: model, verify: model};
  }
  await controller.evaluate(
    `void window.runProviderSpike(${JSON.stringify({keys, models})}); "started"`
  );
  await waitForTitle(controller, {
    prefixes: ["providers:done", "providers:error"],
    timeoutS: Number(options.timeoutS),
    statusExpression,
  });
  if (options.verbose) {
    console.log("\nController console:\n" + (controller.consoleLines.join("\n") || "(empty)"));
  }
  const report = JSON.parse(await controller.evaluate("JSON.stringify(window.__providerSpikeReport)"));
  if (report.error) {
    console.log(`\nProvider test error: ${report.error}`);
    return report;
  }
  for (const provider of report.providers) {
    console.log(`\n=== ${provider.providerId} ===`);
    if (provider.skipped) {
      console.log(`  skipped: ${provider.skipped}`);
      continue;
    }
    console.log(`  models: gate ${provider.models.gate}, verify ${provider.models.verify}`);
    console.log(`  host permission granted: ${provider.hostPermissionGranted}`);
    console.log(
      `  key check: ${provider.validateKey.ok ? "ok" : `FAILED (${provider.validateKey.error.kind}): ${provider.validateKey.error.message}`}` +
        ` | wrong key -> ${provider.wrongKey.rejected ? `rejected as "${provider.wrongKey.kind}" (${provider.wrongKey.status})` : "ACCEPTED?!"}`
    );
    if (provider.gate) {
      console.log(`  gate: ${describeCall(provider.gate)}`);
      if (provider.gate.ok) {
        console.log(
          `        ${provider.gate.claims.length} claim(s); expected topics found: ` +
            `${provider.gate.expectedFound.join(", ") || "none"}; leaked: ${provider.gate.excludedLeaked.join(", ") || "none"}`
        );
      }
    }
    for (const check of provider.verify ?? []) {
      const mode = check.requestedMode ? ` [${check.requestedMode}${check.ok ? ` -> ${check.mode}` : ""}]` : "";
      console.log(`  verify${mode}: "${check.claim.slice(0, 48)}…" expected ${check.expected}`);
      if (check.ok) {
        console.log(
          `        -> ${check.label} (${check.matchesExpected ? "match" : "MISMATCH"}), evidence ${check.evidence}, ` +
            `${check.sources.length} source(s); ${describeCall(check)}`
        );
      } else {
        console.log(`        -> ${describeCall(check)}`);
      }
    }
  }
  return report;
};

const main = async () => {
  const options = parseCli(process.argv.slice(2), CLI_SPEC);
  if (!["bench", "live", "providers"].includes(options.mode)) {
    throw new Error('--mode must be "bench", "live" or "providers"');
  }
  // The committed manifest already carries the WASM CSP, so provider calls
  // are tested against it unchanged unless asked otherwise.
  options.manifest ??= options.mode === "providers" ? "plain" : "csp";
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

    const runners = {
      bench: () => runBench(options, chrome, extensionId),
      live: () => runLive(options, chrome, extensionId, server.origin),
      providers: () => runProviders(options, chrome, extensionId),
    };
    const report = await runners[options.mode]();
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
