/**
 * Shared plumbing for the browser spikes: a loopback static server, a Chrome
 * launcher, and a minimal DevTools-protocol client.
 *
 * No dependencies — Node 22+ and a local Chrome.
 *
 * Chrome is driven over `--remote-debugging-pipe` rather than a TCP port,
 * because the DevTools `Extensions.loadUnpacked` command (the only way to
 * load an unpacked extension into branded Chrome since `--load-extension`
 * was removed) is only exposed on the pipe transport.
 */

import {spawn} from "node:child_process";
import {createHash} from "node:crypto";
import {createReadStream} from "node:fs";
import {stat} from "node:fs/promises";
import {createServer} from "node:http";
import path from "node:path";
import {fileURLToPath} from "node:url";

export const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
export const EXTENSION_DIR = path.join(REPO_ROOT, "extension");
export const FIXTURE_WAV = path.join(REPO_ROOT, "backend", "tests", "fixtures", "claims_16k.wav");
export const FIXTURE_REFERENCE = path.join(
  EXTENSION_DIR,
  "spike",
  "assets",
  "claims_reference.txt"
);
export const RUNTIME_STAMP = path.join(EXTENSION_DIR, "vendor", "transformers", "VERSIONS.txt");

/** Flags that give HEADLESS Chrome on Linux a real GPU for WebGPU. */
export const LINUX_HEADLESS_GPU_FLAGS = Object.freeze([
  "--enable-features=Vulkan",
  "--use-angle=vulkan",
  "--enable-unsafe-webgpu",
]);

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

// Minimal page that plays the fixture clip out loud, for tab-capture tests.
const PLAYER_PAGE = `<!DOCTYPE html>
<html lang="en">
  <head><meta charset="utf-8" /><title>fixture-player</title></head>
  <body>
    <audio id="player" src="/fixture/claims_16k.wav" controls></audio>
    <script>
      const player = document.getElementById("player");
      // ?delay=N postpones playback so capture can start first.
      const delayMs = Number(new URLSearchParams(location.search).get("delay") ?? 0) * 1000;
      player.addEventListener("ended", () => { document.title = "fixture-player:ended"; });
      setTimeout(() => {
        player.play().then(
          () => { document.title = "fixture-player:playing"; },
          (error) => { document.title = "fixture-player:blocked " + error.name; }
        );
      }, delayMs);
    </script>
  </body>
</html>
`;

/**
 * Loopback static server.
 *
 *   /extension/**                 the extension folder
 *   /fixture/claims_16k.wav       the backend audio fixture
 *   /fixture/claims_reference.txt its transcript
 *   /fixture/player.html          a page that plays the fixture aloud
 *
 * A FIXED port keeps the page origin stable between runs. Downloaded models
 * are cached per origin, so a random port stores a fresh copy of every model
 * on every run (this filled a 12 GB quota during spike S1).
 *
 * @param {object} options
 * @param {boolean} options.isolated - send COOP/COEP (cross-origin isolation,
 *   which unlocks WebAssembly threads)
 * @param {number} options.port - preferred port; falls back to a random one
 * @returns {Promise<{origin: string, close: () => void}>}
 */
export const startStaticServer = ({isolated, port}) =>
  new Promise((resolve, reject) => {
    const resolveFile = (urlPath) => {
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
      const headers = {"Cache-Control": "no-store"};
      if (isolated) {
        headers["Cross-Origin-Opener-Policy"] = "same-origin";
        headers["Cross-Origin-Embedder-Policy"] = "require-corp";
      }
      if (urlPath === "/fixture/player.html") {
        response.writeHead(200, {...headers, "Content-Type": CONTENT_TYPES[".html"]});
        response.end(PLAYER_PAGE);
        return;
      }
      const filePath = resolveFile(urlPath);
      try {
        if (!filePath || !(await stat(filePath)).isFile()) {
          throw new Error("not found");
        }
      } catch {
        response.writeHead(404).end("Not found");
        return;
      }
      headers["Content-Type"] =
        CONTENT_TYPES[path.extname(filePath)] ?? "application/octet-stream";
      response.writeHead(200, headers);
      createReadStream(filePath).pipe(response);
    });

    const onListening = () => {
      resolve({
        origin: `http://127.0.0.1:${server.address().port}`,
        close: () => server.close(),
      });
    };
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
 * The id Chrome assigns an unpacked extension is derived from its absolute
 * path: the first 32 hex digits of SHA-256(path), with 0-f mapped to a-p.
 * Needed before launch, for `--allowlisted-extension-id`.
 *
 * @param {string} extensionPath - absolute path of the unpacked extension
 * @returns {string}
 */
export const unpackedExtensionId = (extensionPath) =>
  [...createHash("sha256").update(extensionPath).digest("hex").slice(0, 32)]
    .map((digit) => String.fromCharCode(97 + parseInt(digit, 16)))
    .join("");

/**
 * @typedef {object} DevToolsSession
 * @property {(method: string, params?: object) => Promise<object>} send
 * @property {(expression: string) => Promise<any>} evaluate - returns by value
 * @property {string[]} consoleLines - console output and uncaught exceptions
 */

/**
 * @typedef {object} ChromeHandle
 * @property {(method: string, params?: object) => Promise<object>} send -
 *   browser-level DevTools command
 * @property {(url: string) => Promise<DevToolsSession>} openPage - new tab
 * @property {(targetId: string) => Promise<DevToolsSession>} attach
 * @property {() => string} readStderr - everything Chrome wrote to stderr
 * @property {() => void} close - kill the browser
 */

/**
 * Launch Chrome on the DevTools pipe.
 *
 * @param {object} options
 * @param {string} options.chrome - binary name or path
 * @param {string} options.profileDir - user data directory
 * @param {boolean} [options.headed] - show a window instead of headless
 * @param {string[]} [options.flags] - extra Chrome flags
 * @returns {Promise<ChromeHandle>}
 */
export const launchChrome = ({chrome, profileDir, headed = false, flags = []}) =>
  new Promise((resolve, reject) => {
    const child = spawn(
      chrome,
      [
        "--remote-debugging-pipe",
        `--user-data-dir=${profileDir}`,
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        ...(headed ? [] : ["--headless=new"]),
        ...flags,
        "about:blank",
      ],
      {stdio: ["ignore", "ignore", "pipe", "pipe", "pipe"]}
    );
    const [, , stderrStream, commandPipe, eventPipe] = child.stdio;

    let stderr = "";
    stderrStream.setEncoding("utf8");
    stderrStream.on("data", (chunk) => {
      stderr += chunk;
    });

    const pending = new Map();
    /** @type {Map<string, (method: string, params: object) => void>} */
    const sessionListeners = new Map();
    let nextId = 1;
    let settled = false;

    const send = (method, params = {}, sessionId = undefined) =>
      new Promise((resolveCall, rejectCall) => {
        const id = nextId;
        nextId += 1;
        pending.set(id, {resolve: resolveCall, reject: rejectCall, method});
        commandPipe.write(`${JSON.stringify({id, method, params, sessionId})}\0`);
      });

    let buffered = "";
    eventPipe.setEncoding("utf8");
    eventPipe.on("data", (chunk) => {
      buffered += chunk;
      let end = buffered.indexOf("\0");
      while (end !== -1) {
        const message = JSON.parse(buffered.slice(0, end));
        buffered = buffered.slice(end + 1);
        end = buffered.indexOf("\0");
        if (message.id && pending.has(message.id)) {
          const call = pending.get(message.id);
          pending.delete(message.id);
          if (message.error) {
            call.reject(new Error(`${call.method}: ${message.error.message}`));
          } else {
            call.resolve(message.result);
          }
        } else if (message.method && message.sessionId) {
          sessionListeners.get(message.sessionId)?.(message.method, message.params);
        }
      }
    });

    /** @param {string} targetId @returns {Promise<DevToolsSession>} */
    const attach = async (targetId) => {
      const {sessionId} = await send("Target.attachToTarget", {targetId, flatten: true});
      const consoleLines = [];
      sessionListeners.set(sessionId, (method, params) => {
        if (method === "Runtime.consoleAPICalled") {
          const text = params.args
            .map((argument) => argument.value ?? argument.description ?? "")
            .join(" ");
          consoleLines.push(`[${params.type}] ${text}`);
        } else if (method === "Runtime.exceptionThrown") {
          const details = params.exceptionDetails;
          consoleLines.push(`[exception] ${details.exception?.description ?? details.text}`);
        }
      });
      const sessionSend = (method, params = {}) => send(method, params, sessionId);
      await sessionSend("Runtime.enable");
      return {
        send: sessionSend,
        consoleLines,
        evaluate: async (expression) => {
          const {result, exceptionDetails} = await sessionSend("Runtime.evaluate", {
            expression,
            returnByValue: true,
            awaitPromise: true,
          });
          if (exceptionDetails) {
            throw new Error(exceptionDetails.exception?.description ?? exceptionDetails.text);
          }
          return result.value;
        },
      };
    };

    const handle = {
      send: (method, params = {}) => send(method, params),
      attach,
      openPage: async (url) => {
        const {targetId} = await send("Target.createTarget", {url});
        return attach(targetId);
      },
      readStderr: () => stderr,
      close: () => {
        child.removeAllListeners("exit");
        child.kill();
      },
    };

    child.once("error", (error) => {
      if (!settled) {
        settled = true;
        reject(new Error(`Could not start Chrome: ${error.message}`));
      }
    });
    child.once("exit", (code) => {
      const error = new Error(`Chrome exited (code ${code}).\n${stderr.slice(-2000)}`);
      for (const call of pending.values()) {
        call.reject(error);
      }
      pending.clear();
      if (!settled) {
        settled = true;
        reject(error);
      }
    });

    // The pipe accepts commands as soon as the process is up; the first
    // successful round trip is the readiness signal.
    send("Browser.getVersion").then(
      (version) => {
        if (!settled) {
          settled = true;
          resolve({...handle, version: version.product});
        }
      },
      (error) => {
        if (!settled) {
          settled = true;
          reject(error);
        }
      }
    );
  });

/**
 * Poll a page until its document title starts with one of `prefixes`.
 * A renderer stalled by a GPU-process crash never answers an evaluate, so
 * each poll races a timer and the overall deadline always fires.
 *
 * @param {DevToolsSession} page
 * @param {object} options
 * @param {string[]} options.prefixes
 * @param {number} options.timeoutS
 * @param {string} [options.statusExpression] - evaluated for progress output
 * @returns {Promise<string>} the matching title
 */
export const waitForTitle = async (page, {prefixes, timeoutS, statusExpression = '""'}) => {
  const deadline = Date.now() + timeoutS * 1000;
  let lastStatus = "";
  for (;;) {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    const state = await Promise.race([
      page
        .evaluate(`({title: document.title, status: String(${statusExpression})})`)
        .catch(() => ({title: "", status: ""})),
      new Promise((resolve) => setTimeout(() => resolve({title: "", status: ""}), 5000)),
    ]);
    if (state.status && state.status !== lastStatus) {
      lastStatus = state.status;
      console.log(`  ${state.status}`);
    }
    if (prefixes.some((prefix) => state.title.startsWith(prefix))) {
      return state.title;
    }
    if (Date.now() > deadline) {
      throw new Error(`Timed out after ${timeoutS} s (last status: ${lastStatus || "none"}).`);
    }
  }
};

/**
 * Print the benchmark report (the shape produced by spike/stt_bench.mjs).
 *
 * @param {object} report
 */
export const printBenchSummary = (report) => {
  if (report.error) {
    console.log(`Benchmark error: ${report.error}`);
    return;
  }
  const firstGpu = report.models[0]?.gpu;
  console.log(
    `\nGPU: ${firstGpu?.label} — usable: ${firstGpu?.usable}, shader-f16: ${firstGpu?.shaderF16}`
  );
  console.log(
    `Origin: ${report.environment.origin}, isolated: ${report.environment.crossOriginIsolated}, ` +
      `WASM threads: ${report.options.numThreads}, ` +
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
        result: run.verdict.pass
          ? selected
            ? "PASS (selected)"
            : "pass"
          : `FAIL: ${run.verdict.reason}`,
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

/**
 * Lines of Chrome's stderr that concern the GPU process.
 *
 * @param {string} stderr
 * @returns {string[]}
 */
export const gpuStderrLines = (stderr) =>
  stderr
    .split("\n")
    .filter((line) => /gpu|vulkan|dawn|webgpu|angle|fatal|check failed|signal/i.test(line));

/**
 * Tiny CLI parser shared by the spike scripts.
 *
 * @param {string[]} argv
 * @param {object} spec
 * @param {Record<string, string>} spec.values - "--flag" -> option key (takes a value)
 * @param {Record<string, [string, any]>} spec.flags - "--flag" -> [key, value]
 * @param {object} spec.defaults
 * @returns {object} options, plus `chromeFlags` from repeated --chrome-flag=…
 */
export const parseCli = (argv, {values, flags, defaults}) => {
  const options = {...defaults, chromeFlags: []};
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument.startsWith("--chrome-flag=")) {
      options.chromeFlags.push(argument.slice("--chrome-flag=".length));
    } else if (argument in values) {
      index += 1;
      if (argv[index] === undefined) {
        throw new Error(`${argument} needs a value`);
      }
      options[values[argument]] = argv[index];
    } else if (argument in flags) {
      const [key, value] = flags[argument];
      options[key] = value;
    } else {
      throw new Error(`Unknown option: ${argument}`);
    }
  }
  return options;
};

/**
 * Fail early, with the fix, when a prerequisite file is missing.
 *
 * @param {string} filePath
 * @param {string} hint
 */
export const requireFile = async (filePath, hint) => {
  try {
    await stat(filePath);
  } catch {
    throw new Error(hint);
  }
};
