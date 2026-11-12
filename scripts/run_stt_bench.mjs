#!/usr/bin/env node
/**
 * Run the in-browser speech benchmark in a real Chrome, unattended.
 *
 * Serves the extension folder from a loopback static server, launches Chrome
 * with a throwaway profile, opens extension/spike/stt_bench.html with
 * `auto=1`, waits for the report over the DevTools protocol, prints a summary
 * table and saves the full JSON.
 *
 * No dependencies: Node 22+ (built-in WebSocket) and a local Chrome.
 *
 * Usage (from the repo root; run scripts/vendor_stt_runtime.py first):
 *
 *     node scripts/run_stt_bench.mjs --label intel-arc
 *     node scripts/run_stt_bench.mjs --models moonshine-base,whisper-base.en
 *     node scripts/run_stt_bench.mjs --fallback --allow-software --label software-gpu
 *     node scripts/run_stt_bench.mjs --clip fixture --label long-clip
 *     node scripts/run_stt_bench.mjs --no-isolated          # single-threaded CPU path
 *     node scripts/run_stt_bench.mjs --headed --chrome-flag=--enable-unsafe-webgpu
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

import {spawn} from "node:child_process";
import {createReadStream} from "node:fs";
import {mkdir, stat, writeFile} from "node:fs/promises";
import {createServer} from "node:http";
import {tmpdir} from "node:os";
import path from "node:path";
import {fileURLToPath} from "node:url";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const EXTENSION_DIR = path.join(REPO_ROOT, "extension");
const FIXTURE_WAV = path.join(REPO_ROOT, "backend", "tests", "fixtures", "claims_16k.wav");
const FIXTURE_REFERENCE = path.join(EXTENSION_DIR, "spike", "assets", "claims_reference.txt");
const RUNTIME_STAMP = path.join(EXTENSION_DIR, "vendor", "transformers", "VERSIONS.txt");

const CONTENT_TYPES = Object.freeze({
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json",
  ".wasm": "application/wasm",
  ".wav": "audio/wav",
  ".txt": "text/plain; charset=utf-8",
});

const POLL_INTERVAL_MS = 1000;

/** @param {string[]} argv */
const parseArgs = (argv) => {
  const options = {
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
    verbose: false,
    ownDevice: true,
    chrome: "google-chrome",
    chromeFlags: [],
    profileDir: path.join(tmpdir(), "factchecker-stt-bench-profile"),
    timeoutS: 1800,
    label: "run",
    page: "spike/stt_bench.html",
    port: 8765,
    out: path.join(REPO_ROOT, "docs", "spike-results", "raw"),
  };
  const valueOptions = {
    "--models": "models",
    "--variants": "variants",
    "--clip": "clip",
    "--threads": "threads",
    "--chrome": "chrome",
    "--profile-dir": "profileDir",
    "--timeout": "timeoutS",
    "--label": "label",
    "--out": "out",
    "--page": "page",
    "--port": "port",
  };
  const flagOptions = {
    "--fallback": ["fallback", true],
    "--allow-software": ["allowSoftware", true],
    "--low-power": ["lowPower", true],
    "--stop-first": ["stopFirst", true],
    "--no-isolated": ["isolated", false],
    "--headed": ["headed", true],
    "--verbose": ["verbose", true],
    "--runtime-device": ["ownDevice", false],
  };
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument.startsWith("--chrome-flag=")) {
      options.chromeFlags.push(argument.slice("--chrome-flag=".length));
    } else if (argument in valueOptions) {
      index += 1;
      if (argv[index] === undefined) {
        throw new Error(`${argument} needs a value`);
      }
      options[valueOptions[argument]] = argv[index];
    } else if (argument in flagOptions) {
      const [key, value] = flagOptions[argument];
      options[key] = value;
    } else {
      throw new Error(`Unknown option: ${argument}`);
    }
  }
  options.timeoutS = Number(options.timeoutS);
  if (!["jfk", "fixture"].includes(options.clip)) {
    throw new Error(`--clip must be "jfk" or "fixture"`);
  }
  return options;
};

/**
 * Loopback static server: /extension/** from the extension folder, plus the
 * backend audio fixture under /fixture/. COOP/COEP make the page cross-origin
 * isolated, which is what unlocks WASM threads.
 *
 * @param {boolean} isolated
 * @param {number} port - preferred loopback port
 * @returns {Promise<{origin: string, close: () => void}>}
 */
const startServer = (isolated, port) =>
  new Promise((resolve, reject) => {
    const resolvePath = (urlPath) => {
      if (urlPath === "/fixture/claims_16k.wav") {
        return FIXTURE_WAV;
      }
      if (urlPath === "/fixture/claims_reference.txt") {
        return FIXTURE_REFERENCE;
      }
      if (!urlPath.startsWith("/extension/")) {
        return null;
      }
      const filePath = path.normalize(path.join(REPO_ROOT, urlPath));
      return filePath.startsWith(EXTENSION_DIR + path.sep) ? filePath : null;
    };

    const server = createServer(async (request, response) => {
      const urlPath = decodeURIComponent(new URL(request.url, "http://localhost").pathname);
      const filePath = resolvePath(urlPath);
      try {
        if (!filePath || !(await stat(filePath)).isFile()) {
          throw new Error("not found");
        }
      } catch {
        response.writeHead(404).end("Not found");
        return;
      }
      const headers = {
        "Content-Type": CONTENT_TYPES[path.extname(filePath)] ?? "application/octet-stream",
        "Cache-Control": "no-store",
      };
      if (isolated) {
        headers["Cross-Origin-Opener-Policy"] = "same-origin";
        headers["Cross-Origin-Embedder-Policy"] = "require-corp";
      }
      response.writeHead(200, headers);
      createReadStream(filePath).pipe(response);
    });
    const onListening = () => {
      resolve({
        origin: `http://127.0.0.1:${server.address().port}`,
        close: () => server.close(),
      });
    };
    // A FIXED port keeps the page's origin stable between runs. Downloaded
    // models are cached per origin, so a random port would store a fresh copy
    // of every model on every run (this filled a 12 GB quota during the spike).
    server.once("error", (error) => {
      if (error.code !== "EADDRINUSE") {
        reject(error);
        return;
      }
      console.warn(`Port ${port} is busy — using a random port (models will be re-downloaded).`);
      server.once("error", reject);
      server.listen(0, "127.0.0.1", onListening);
    });
    server.listen(port, "127.0.0.1", onListening);
  });

/**
 * Launch Chrome and resolve with its DevTools HTTP endpoint.
 *
 * @param {object} options - parsed CLI options
 * @returns {Promise<{child: import("node:child_process").ChildProcess, endpoint: string, readStderr: () => string}>}
 */
const launchChrome = (options) =>
  new Promise((resolve, reject) => {
    const flags = [
      "--remote-debugging-port=0",
      `--user-data-dir=${options.profileDir}`,
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-background-networking",
      ...(options.headed ? [] : ["--headless=new"]),
      ...options.chromeFlags,
      "about:blank",
    ];
    const child = spawn(options.chrome, flags, {stdio: ["ignore", "ignore", "pipe"]});
    let stderr = "";
    let resolved = false;
    const onData = (chunk) => {
      stderr += chunk;
      const match = stderr.match(/DevTools listening on ws:\/\/([^/\s]+)\//);
      if (match && !resolved) {
        resolved = true;
        // Keep collecting: GPU-process errors land on stderr (see --verbose).
        resolve({child, endpoint: `http://${match[1]}`, readStderr: () => stderr});
      }
    };
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", onData);
    child.once("error", (error) => reject(new Error(`Could not start Chrome: ${error.message}`)));
    child.once("exit", (code) =>
      reject(new Error(`Chrome exited early (code ${code}).\n${stderr.slice(-2000)}`))
    );
  });

/**
 * Minimal DevTools-protocol client over the page target's WebSocket.
 *
 * @param {string} webSocketUrl
 * @returns {Promise<{send: Function, onEvent: Function, close: Function}>}
 */
const connectDevTools = (webSocketUrl) =>
  new Promise((resolve, reject) => {
    const socket = new WebSocket(webSocketUrl);
    const pending = new Map();
    const listeners = [];
    let nextId = 1;
    socket.addEventListener("error", () => reject(new Error("DevTools connection failed")));
    socket.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (message.id && pending.has(message.id)) {
        const {resolve: resolveCall, reject: rejectCall} = pending.get(message.id);
        pending.delete(message.id);
        if (message.error) {
          rejectCall(new Error(message.error.message));
        } else {
          resolveCall(message.result);
        }
      } else if (message.method) {
        listeners.forEach((listener) => listener(message.method, message.params));
      }
    });
    socket.addEventListener("open", () => {
      resolve({
        send: (method, params = {}) =>
          new Promise((resolveCall, rejectCall) => {
            const id = nextId;
            nextId += 1;
            pending.set(id, {resolve: resolveCall, reject: rejectCall});
            socket.send(JSON.stringify({id, method, params}));
          }),
        onEvent: (listener) => listeners.push(listener),
        close: () => socket.close(),
      });
    });
  });

/** @param {object} devtools @param {string} expression */
const evaluate = async (devtools, expression) => {
  const {result, exceptionDetails} = await devtools.send("Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise: true,
  });
  if (exceptionDetails) {
    throw new Error(exceptionDetails.exception?.description ?? exceptionDetails.text);
  }
  return result.value;
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
  if (options.fallback) {
    query.set("fallback", "1");
  }
  if (options.allowSoftware) {
    query.set("allowSoftware", "1");
  }
  if (options.lowPower) {
    query.set("power", "low-power");
  }
  if (options.stopFirst) {
    query.set("stopFirst", "1");
  }
  if (options.threads) {
    query.set("threads", String(options.threads));
  }
  if (!options.ownDevice) {
    query.set("ownDevice", "0");
  }
  return `${origin}/extension/${options.page}?${query}`;
};

/** @param {object} report */
const printSummary = (report) => {
  if (report.error) {
    console.log(`Benchmark error: ${report.error}`);
    return;
  }
  const firstGpu = report.models[0]?.gpu;
  console.log(`\nGPU: ${firstGpu?.label} — usable: ${firstGpu?.usable}, shader-f16: ${firstGpu?.shaderF16}`);
  console.log(
    `Isolated: ${report.environment.crossOriginIsolated}, WASM threads: ${report.options.numThreads}, ` +
      `clip: ${report.models[0]?.audio.seconds.toFixed(1)} s in ${report.models[0]?.audio.segments} segment(s)\n`
  );
  const rows = [];
  for (const model of report.models) {
    for (const skip of model.skipped) {
      rows.push({model: model.modelId, variant: skip.variantId, result: `skipped: ${skip.reason}`});
    }
    for (const run of model.runs) {
      const selected = model.selected?.variantId === run.variantId;
      rows.push({
        model: model.modelId,
        variant: run.variantId,
        result: run.verdict.pass ? (selected ? "PASS (selected)" : "pass") : `FAIL: ${run.verdict.reason}`,
        "speed (x real time)": run.ok ? (1 / run.realTimeFactor).toFixed(1) : "",
        "WER %": Number.isFinite(run.wordErrorRate) ? (run.wordErrorRate * 100).toFixed(1) : "",
        "load s": run.ok ? (run.loadMs / 1000).toFixed(1) : "",
        "warm-up s": run.ok ? (run.warmupMs / 1000).toFixed(1) : "",
        "session GPU": run.sessionGpu
          ? `${run.sessionGpu.vendor}${run.sessionGpu.isFallbackAdapter ? " (fallback)" : ""}`
          : "",
        error: run.ok ? "" : String(run.error).slice(0, 90),
      });
    }
  }
  console.table(rows);
};

const main = async () => {
  const options = parseArgs(process.argv.slice(2));
  try {
    await stat(RUNTIME_STAMP);
  } catch {
    throw new Error("Speech runtime is not vendored — run: python3 scripts/vendor_stt_runtime.py");
  }
  if (options.clip === "fixture") {
    try {
      await stat(FIXTURE_WAV);
    } catch {
      throw new Error(
        "backend/tests/fixtures/claims_16k.wav is missing — build it with " +
          "backend/scripts/make_fixture_wav.py"
      );
    }
  }

  const server = await startServer(options.isolated, Number(options.port));
  const {child, endpoint, readStderr} = await launchChrome(options);
  let devtools = null;
  const consoleLines = [];
  try {
    const targets = await (await fetch(`${endpoint}/json/list`)).json();
    const page = targets.find((target) => target.type === "page");
    if (!page) {
      throw new Error("Chrome exposed no page target.");
    }
    devtools = await connectDevTools(page.webSocketDebuggerUrl);
    devtools.onEvent((method, params) => {
      if (method === "Runtime.consoleAPICalled") {
        const text = params.args.map((argument) => argument.value ?? argument.description ?? "").join(" ");
        consoleLines.push(`[${params.type}] ${text}`);
      } else if (method === "Runtime.exceptionThrown") {
        consoleLines.push(`[exception] ${params.exceptionDetails.exception?.description ?? params.exceptionDetails.text}`);
      }
    });
    await devtools.send("Runtime.enable");
    await devtools.send("Page.enable");

    const pageUrl = buildPageUrl(options, server.origin);
    console.log(`Opening ${pageUrl}`);
    await devtools.send("Page.navigate", {url: pageUrl});

    const deadline = Date.now() + options.timeoutS * 1000;
    let lastStatus = "";
    for (;;) {
      await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
      // Race against a timer: a renderer stalled by a GPU-process crash never
      // answers, and the overall deadline below must still fire.
      const state = await Promise.race([
        evaluate(
          devtools,
          `({title: document.title, status: document.getElementById("status")?.textContent ?? ""})`
        ).catch(() => ({title: "", status: ""})),
        new Promise((resolve) => setTimeout(() => resolve({title: "", status: ""}), 5000)),
      ]);
      if (state.status && state.status !== lastStatus) {
        lastStatus = state.status;
        console.log(`  ${state.status}`);
      }
      if (state.title === "bench:done" || state.title === "bench:error") {
        break;
      }
      if (Date.now() > deadline) {
        throw new Error(`Timed out after ${options.timeoutS} s (last status: ${lastStatus || "none"}).`);
      }
    }

    const report = JSON.parse(await evaluate(devtools, "JSON.stringify(window.__sttBenchReport)"));
    report.runner = {
      label: options.label,
      headless: !options.headed,
      chromeFlags: options.chromeFlags,
      isolated: options.isolated,
      chromeVersion: (await devtools.send("Browser.getVersion").catch(() => null))?.product ?? null,
    };
    printSummary(report);
    if (options.verbose) {
      console.log("\nPage console:\n" + (consoleLines.join("\n") || "(empty)"));
      const gpuLines = readStderr()
        .split("\n")
        .filter((line) => /gpu|vulkan|dawn|webgpu|angle|fatal|check failed|signal/i.test(line));
      console.log("\nChrome GPU-related stderr:\n" + (gpuLines.join("\n") || "(none)"));
    }

    await mkdir(options.out, {recursive: true});
    const outputPath = path.join(options.out, `${options.label}.json`);
    await writeFile(outputPath, `${JSON.stringify(report, null, 2)}\n`);
    console.log(`\nSaved ${path.relative(REPO_ROOT, outputPath)}`);
    return report.error ? 1 : 0;
  } catch (error) {
    if (consoleLines.length > 0) {
      console.error("Page console (last 30 lines):\n" + consoleLines.slice(-30).join("\n"));
    }
    throw error;
  } finally {
    devtools?.close();
    child.removeAllListeners("exit");
    child.kill();
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
