#!/usr/bin/env node
/**
 * End-to-end test of in-browser speech in a real Chrome.
 *
 * Does what a viewer would do, with a real model and real audio:
 *
 *   1. Settings → Speech recognition → "This browser" → Download and test.
 *      The model is really downloaded and the self-test really runs.
 *   2. A tab plays a recorded clip of speech; Start is pressed on it.
 *   3. The extension captures the tab, transcribes it in the offscreen
 *      document, and sends TEXT to the backend — which here is a stand-in
 *      that records what arrives. The test passes if the words are right
 *      and not one audio frame was sent.
 *
 * It also checks the guard rails: a session refuses to start when the
 * chosen model was never tested, and the crash guard blocks the GPU when a
 * previous GPU attempt was left unfinished.
 *
 * The backend stand-in has no speech model and makes no provider calls, so
 * the run costs nothing. The first run downloads the model (63 MB for the
 * default); it is kept in .stt-bench/ for later runs unless --fresh is given.
 *
 * No dependencies: Node 22+, a local Chrome, network access to
 * huggingface.co, and the vendored speech runtime
 * (python scripts/vendor_stt_runtime.py).
 *
 * Usage (from the repo root):
 *
 *     node scripts/run_browser_speech_e2e.mjs
 *     node scripts/run_browser_speech_e2e.mjs --gpu --model whisper-base.en
 *
 * Options:
 *   --model ID           moonshine-base (default), whisper-base.en, parakeet-ctc-0.6b
 *   --gpu                give headless Chrome a GPU (Linux needs Vulkan flags)
 *   --headed             show the browser window
 *   --fresh              start from an empty profile (downloads the model again)
 *   --timeout-s N        limit for the download and self-test (default 900)
 *   --chrome PATH        Chrome binary (default: google-chrome on PATH)
 *   --chrome-flag=FLAG   extra Chrome flag; repeatable
 */

import {cp, mkdir, readFile, rm, stat, writeFile} from "node:fs/promises";
import {createServer} from "node:http";
import path from "node:path";

import {wordErrorRate} from "../extension/engine/stt/wer.mjs";
import {
  EXTENSION_DIR,
  FIXTURE_REFERENCE,
  LINUX_HEADLESS_GPU_FLAGS,
  REPO_ROOT,
  launchChrome,
  parseCli,
  startStaticServer,
  unpackedExtensionId,
  waitForTitle,
} from "./lib/chrome_harness.mjs";
import {acceptWebSocket} from "./lib/fake_backend_history.mjs";

const WORK_DIR = path.join(REPO_ROOT, ".stt-bench");
const FAKE_BACKEND_PORT = 8733;
const MAX_WORD_ERROR_RATE = 0.25;

const CLI_SPEC = {
  values: {"--model": "model", "--timeout-s": "timeoutS", "--chrome": "chrome"},
  flags: {"--gpu": ["gpu", true], "--headed": ["headed", true], "--fresh": ["fresh", true]},
  defaults: {
    model: "moonshine-base",
    timeoutS: "900",
    chrome: "google-chrome",
    gpu: false,
    headed: false,
    fresh: false,
  },
};

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * A backend with no speech model: answers the status endpoints the app
 * polls, and records what a session sends over /ws/audio.
 */
const createFakeBackend = () => {
  const state = {hellos: [], transcripts: [], audioFrames: 0, stops: 0};
  const routes = {
    "/healthz": {
      status: "ok",
      server_version: "speech-e2e-fake",
      whisper_model: "none",
      configured: true,
      checks_today: 0,
      accepts_audio: false,
      accepts_transcripts: true,
    },
    "/setup/status": {
      configured: true,
      gate: {provider: "anthropic", model: "claude-haiku-5-5", configured: true},
      verify: {provider: "anthropic", model: "claude-haiku-5-5", configured: true},
      providers: {
        anthropic: {configured: true, auth_method: "api_key", key_hint: "…test"},
        openrouter: {configured: false, key_hint: null, credits: null},
        ollama: {configured: true, reachable: false, base_url: "http://127.0.0.1:11434/v1"},
      },
    },
    "/stats/summary": {today: {}, totals: {sessions: 0, labels: {}}},
  };
  const server = createServer((request, response) => {
    const body = routes[new URL(request.url, "http://x").pathname];
    response.writeHead(body ? 200 : 404, {
      "Access-Control-Allow-Origin": request.headers.origin ?? "*",
      "Content-Type": "application/json",
    });
    response.end(JSON.stringify(body ?? {detail: "Not Found"}));
  });
  server.on("upgrade", (request, socket) => {
    acceptWebSocket(
      request,
      socket,
      (text, connection) => {
        const frame = JSON.parse(text);
        if (frame.type === "hello") {
          state.hellos.push(frame);
          connection.sendJson({
            type: "ready",
            server_version: "speech-e2e-fake",
            model: "none",
            session_id: `speech-e2e-${state.hellos.length}`,
          });
        } else if (frame.type === "transcript") {
          state.transcripts.push(frame);
        } else if (frame.type === "stop") {
          state.stops += 1;
          connection.close(1000);
        }
      },
      () => {
        state.audioFrames += 1;
      }
    );
  });
  return {
    state,
    start: () => new Promise((resolve) => server.listen(FAKE_BACKEND_PORT, "127.0.0.1", resolve)),
    stop: () => server.close(),
  };
};

const results = [];
const check = (name, condition, detail = "") => {
  results.push({name, pass: Boolean(condition)});
  console.log(`${condition ? "  ok  " : "  FAIL"}  ${name}${detail && !condition ? ` — ${detail}` : ""}`);
};

const main = async () => {
  const options = parseCli(process.argv.slice(2), CLI_SPEC);
  try {
    await stat(path.join(EXTENSION_DIR, "vendor", "transformers", "transformers.js"));
  } catch {
    throw new Error("the speech runtime is not vendored: run `python scripts/vendor_stt_runtime.py` first");
  }

  // A copy at a stable path keeps the extension id, and with it the origin
  // that the downloaded model is cached under.
  const extensionPath = path.join(WORK_DIR, "ext-speech-e2e");
  await rm(extensionPath, {recursive: true, force: true});
  await cp(EXTENSION_DIR, extensionPath, {recursive: true});
  const profileDir = path.join(WORK_DIR, "profile-speech-e2e");
  if (options.fresh) {
    await rm(profileDir, {recursive: true, force: true});
  }
  await mkdir(profileDir, {recursive: true});

  const fake = createFakeBackend();
  await fake.start();
  const staticServer = await startStaticServer({isolated: false, port: 8765});
  const chrome = await launchChrome({
    chrome: options.chrome,
    profileDir,
    headed: options.headed,
    flags: [
      "--enable-unsafe-extension-debugging",
      `--allowlisted-extension-id=${unpackedExtensionId(extensionPath)}`,
      "--window-size=1280,900",
      ...(options.gpu ? LINUX_HEADLESS_GPU_FLAGS : []),
      ...options.chromeFlags,
    ],
  });

  try {
    const {id: extensionId} = await chrome.send("Extensions.loadUnpacked", {path: extensionPath});
    const appUrl = `chrome-extension://${extensionId}/app/app.html`;
    const backendUrl = `ws://127.0.0.1:${FAKE_BACKEND_PORT}/ws/audio`;
    const page = await chrome.openPage(`${appUrl}#/settings`);
    await page.send("Page.enable");
    const run = (expression) => page.evaluate(`(async () => (${expression}))()`);
    const speechState = () => run(`chrome.storage.local.get("speech").then((stored) => stored.speech ?? null)`);
    const text = () => run("document.body.innerText");
    const waitForText = async (needle, timeoutMs = 15_000) => {
      const deadline = Date.now() + timeoutMs;
      for (;;) {
        const body = await text().catch(() => "");
        if (body.includes(needle)) {
          return body;
        }
        if (Date.now() > deadline) {
          throw new Error(`timed out waiting for "${needle}"\n--- page ---\n${body.slice(-1200)}`);
        }
        await pause(250);
      }
    };
    const click = (label) =>
      run(`(() => {
        const target = [...document.querySelectorAll("button, label")].find(
          (node) => node.innerText.trim().startsWith(${JSON.stringify(label)}) && !node.disabled
        );
        if (!target) { throw new Error("nothing clickable starts with: ${label}"); }
        target.click();
        return true;
      })()`);
    const captureSession = () =>
      run(`chrome.storage.session.get("captureSession").then((stored) => stored.captureSession ?? null)`);
    const startCapture = () =>
      run(`(async () => {
        const [tab] = await chrome.tabs.query({url: "${staticServer.origin}/*"});
        await chrome.runtime.sendMessage({target: "background", type: "CAPTURE_START", payload: {tabId: tab.id}});
        return true;
      })()`);
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

    // A known starting point: this backend, default speech settings, done
    // with the welcome flow.
    await run(`Promise.all([
      chrome.storage.sync.set({settings: {backendUrl: ${JSON.stringify(backendUrl)}}}),
      chrome.storage.local.set({onboarding: {consentAt: "x", completedAt: "x"}}),
      chrome.storage.local.remove("speech"),
    ])`);
    // Installing the extension opens its welcome tab, which can take over
    // this one; go back to Settings explicitly before reloading.
    await pause(1000);
    await page.send("Page.navigate", {url: `${appUrl}#/settings`});
    await pause(300);
    await page.send("Page.reload");
    await waitForText("Speech recognition");

    console.log("\nSettings: choosing in-browser speech");
    const before = await text();
    check("the backend is the default speech engine", (await speechState()) === null);
    check(
      "says when the backend has no speech model of its own",
      before.includes("started without a speech model")
    );
    await click("This browser");
    await waitForText("Download and test");
    await run(`(() => {
      const option = document.querySelector('input[name="speech-model"][value=${JSON.stringify(options.model)}]');
      if (!option) { throw new Error("no such model: ${options.model}"); }
      option.click();
      return true;
    })()`);
    await pause(300);
    let state = await speechState();
    check(
      "the choice is stored on this device",
      state?.settings.engine === "browser" && state.settings.modelId === options.model,
      JSON.stringify(state?.settings)
    );

    const player = await chrome.openPage(`${staticServer.origin}/fixture/player.html?delay=100000`);
    await page.send("Page.bringToFront");

    console.log("\nGuard rail: an untested model");
    await startCapture();
    let session = await waitForSession((current) => current.status === "error", 8000);
    check(
      "Start is refused, with its own reason, before the model has been tested",
      session?.status === "error" && session.lastError === "ERR_SPEECH_NOT_READY",
      JSON.stringify(session)
    );
    check("…and nothing was opened to the backend", fake.state.hellos.length === 0);

    console.log(`\nDownload and test (${options.model}; the first run downloads it)`);
    await click("Download and test");
    const testStartedAt = Date.now();
    let lastShown = "";
    for (;;) {
      const body = await text();
      if (/Works on this device|cannot transcribe live speech|could not finish/.test(body)) {
        break;
      }
      const progress = /(Downloading the model[^\n]*|Loading the model[^\n]*|Testing[^\n]*)/.exec(body)?.[1];
      if (progress && progress !== lastShown) {
        lastShown = progress;
        console.log(`        ${progress}`);
      }
      if (Date.now() - testStartedAt > Number(options.timeoutS) * 1000) {
        throw new Error(`the self-test did not finish within ${options.timeoutS} s`);
      }
      await pause(1000);
    }
    state = await speechState();
    const result = state?.results?.[options.model];
    const settingsText = await text();
    check(
      "the self-test passes and says so in plain words",
      settingsText.includes("Works on this device") && Boolean(result?.selected),
      /could not finish[^\n]*/.exec(settingsText)?.[0] ?? JSON.stringify(result?.runs)
    );
    if (result?.selected) {
      console.log(
        `        chose ${result.selected.variantId}: ${(1 / result.selected.realTimeFactor).toFixed(1)}× real time, ` +
          `word error rate ${result.selected.wordErrorRate}`
      );
    }
    check("the crash-guard marker is cleared when the test returns", state?.gpuAttempt === null);
    await mkdir(path.join(WORK_DIR, "screens"), {recursive: true});
    const {data: shot} = await page.send("Page.captureScreenshot", {format: "png", captureBeyondViewport: true});
    await writeFile(path.join(WORK_DIR, "screens", "speech-settings.png"), Buffer.from(shot, "base64"));
    await page.send("Page.navigate", {url: `${appUrl}#/home`});
    await waitForText("In this browser");
    check(
      "Home says where speech is transcribed",
      /In this browser · .+ · (CPU|GPU) · [\d.]+× real time/.test(await text())
    );
    if (!result?.selected) {
      throw new Error("no configuration passed; the live part cannot run");
    }

    console.log("\nLive session: a tab playing recorded speech");
    await startCapture();
    session = await waitForSession(
      (current) => current.status === "error" || (current.status === "capturing" && current.speech?.status === "ready"),
      120_000
    );
    check(
      "Start reaches \"capturing\" with the model loaded",
      session?.status === "capturing" && session.speech?.status === "ready",
      JSON.stringify(session)
    );
    check(
      "the session tells the backend it will send text",
      fake.state.hellos.at(-1)?.format === "transcript",
      JSON.stringify(fake.state.hellos.at(-1))
    );
    await player.send("Runtime.evaluate", {
      expression: 'document.getElementById("player").play()',
      userGesture: true,
      awaitPromise: true,
    });
    await waitForTitle(player, {
      prefixes: ["fixture-player:ended"],
      timeoutS: 120,
      statusExpression: '`playing ${document.getElementById("player").currentTime.toFixed(0)} s`',
    });
    await pause(1500); // let the last pause reach the segmenter
    const liveCount = fake.state.transcripts.length;
    check("transcripts arrive while the clip is still playing", liveCount >= 3, `${liveCount} so far`);
    await run(`chrome.runtime.sendMessage({target: "background", type: "CAPTURE_STOP", payload: {}})`);
    session = await waitForSession((current) => current.status === "idle" || current.status === "error", 20_000);
    check("Stop ends the session cleanly", session?.status === "idle" && !session.lastError, JSON.stringify(session));

    const reference = await readFile(FIXTURE_REFERENCE, "utf8");
    const heard = fake.state.transcripts.map((frame) => frame.text).join(" ");
    const errorRate = wordErrorRate(reference, heard);
    console.log(`        heard ${fake.state.transcripts.length} utterances, word error rate ${errorRate.toFixed(3)}`);
    console.log(`        "${heard.slice(0, 160)}…"`);
    check(
      `the words are right (word error rate under ${MAX_WORD_ERROR_RATE})`,
      errorRate < MAX_WORD_ERROR_RATE,
      `${errorRate.toFixed(3)}: ${heard}`
    );
    check(
      "each transcript carries its position in the stream",
      fake.state.transcripts.every((frame) => frame.end > frame.start && frame.start >= 0) &&
        fake.state.transcripts.at(-1).end > 30
    );
    check("no audio was sent to the backend, only text", fake.state.audioFrames === 0, `${fake.state.audioFrames} audio frames`);
    check("the backend was asked to finish the session", fake.state.stops === 1);

    console.log("\nGuard rail: an unfinished GPU attempt");
    await run(`chrome.storage.local.get("speech").then(({speech}) =>
      chrome.storage.local.set({speech: {...speech, gpuAttempt: {startedAt: "2026-01-01T00:00:00Z", context: "session", variantId: "webgpu-mixed"}}}))`);
    // A fresh load of the app is "the next start": the guard runs then.
    await page.send("Page.navigate", {url: `${appUrl}#/settings`});
    await waitForText("Speech recognition");
    await page.send("Page.reload");
    await waitForText("The GPU has been switched off for speech");
    state = await speechState();
    check(
      "at the next start the GPU is blocked and the reason is shown",
      state?.gpuAttempt === null && /closed unexpectedly/.test(state?.gpuBlocked?.reason ?? ""),
      JSON.stringify({attempt: state?.gpuAttempt, blocked: state?.gpuBlocked})
    );
    check("…with a way to test without the GPU", (await text()).includes("Test without the GPU"));

    const problems = page.consoleLines.filter((line) => /^\[(error|exception)\]/.test(line));
    check("no errors in the app's console", problems.length === 0, problems.slice(0, 3).join(" | "));
  } finally {
    chrome.close();
    fake.stop();
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
