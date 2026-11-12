/**
 * Unit tests for the app's logic layer: the backend client's error mapping,
 * the settings rules, and the provider/stage view-model.
 *
 * Run from the repo root (Node 20+, no dependencies):
 *
 *     node --test "extension/tests/*.test.mjs"
 */

import assert from "node:assert/strict";
import {readFile, readdir} from "node:fs/promises";
import path from "node:path";
import {describe, it} from "node:test";
import {fileURLToPath} from "node:url";

import {
  BACKEND_ERROR,
  BackendError,
  createBackendApi,
  readErrorDetail,
} from "../app/lib/backend_api.mjs";
import {ROUTES, landingRoute, parseRoute} from "../app/lib/routes.mjs";
import {
  clampDurationSeconds,
  isValidBackendUrl,
  setAllTopics,
  setTopicEnabled,
  topicsMasterState,
} from "../app/lib/settings_rules.mjs";
import {
  KEY_PROVIDERS,
  describeConnection,
  describeStages,
  isStageFormDirty,
  stageFormFields,
  stageFormFromStatus,
  stageFormProblem,
  stageRequestFromForm,
  withStageProvider,
} from "../app/lib/setup_model.mjs";

const ORIGIN = "http://127.0.0.1:8710";

/** A fake fetch replaying one response and recording the request. */
const fakeFetch = ({status = 200, body = {}, throws = null} = {}) => {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({url, method: init.method, body: init.body ? JSON.parse(init.body) : null});
    if (throws) {
      throw throws;
    }
    return {
      ok: status >= 200 && status < 300,
      status,
      json: async () => {
        if (body === "not json") {
          throw new SyntaxError("Unexpected token");
        }
        return body;
      },
    };
  };
  return {fetchImpl, calls};
};

const apiWith = (fake, origin = ORIGIN) =>
  createBackendApi({getOrigin: () => origin, fetchImpl: fake.fetchImpl});

const STATUS = Object.freeze({
  configured: true,
  gate: {provider: "anthropic", model: "claude-haiku-5-5", configured: true},
  verify: {provider: "anthropic", model: "claude-haiku-5-5", configured: true},
  providers: {
    anthropic: {
      configured: true,
      auth_method: "api_key",
      key_hint: "…abcd",
      gate_model: "claude-haiku-5-5",
      verify_model: "claude-haiku-5-5",
    },
    openrouter: {
      configured: true,
      key_hint: "…wxyz",
      credits: {total: 10, usage: 3.5},
      gate_model: "openai/gpt-6-luna",
      verify_model: "google/gemini-3.5-flash-lite",
      jev_mode: "off",
    },
    ollama: {configured: true, reachable: false, base_url: "http://127.0.0.1:11434/v1"},
  },
});

describe("backend client", () => {
  it("calls the right endpoints with the backend's field names", async () => {
    const fake = fakeFetch({body: STATUS});
    const api = apiWith(fake);
    await api.health();
    await api.setupStatus();
    await api.saveCredentials({provider: "openrouter", apiKey: "sk-or-test"});
    await api.saveStages({gateProvider: "ollama", verifyProvider: "anthropic", jevMode: "off"});
    await api.runTestCheck("The Eiffel Tower is 450 meters tall.");
    assert.deepEqual(
      fake.calls.map((call) => `${call.method} ${call.url.replace(ORIGIN, "")}`),
      ["GET /healthz", "GET /setup/status", "POST /setup/credentials", "POST /setup/stages", "POST /debug/text"]
    );
    assert.deepEqual(fake.calls[2].body, {provider: "openrouter", api_key: "sk-or-test"});
    assert.deepEqual(fake.calls[3].body, {
      gate_provider: "ollama",
      verify_provider: "anthropic",
      gate_model: "",
      verify_model: "",
      jev_mode: "off",
    });
    assert.equal(fake.calls[4].body.text, "The Eiffel Tower is 450 meters tall.");
  });

  it("reports a backend that is not running, not a crash", async () => {
    const api = apiWith(fakeFetch({throws: new TypeError("Failed to fetch")}));
    await assert.rejects(api.health(), (error) => {
      assert.ok(error instanceof BackendError);
      assert.equal(error.kind, BACKEND_ERROR.UNREACHABLE);
      assert.match(error.message, /not running/);
      return true;
    });
  });

  it("distinguishes a slow backend from an absent one", async () => {
    const timeout = Object.assign(new Error("timed out"), {name: "TimeoutError"});
    const api = apiWith(fakeFetch({throws: timeout}));
    await assert.rejects(api.health(), (error) => error.kind === BACKEND_ERROR.TIMEOUT);
  });

  it("refuses to call anything when the configured address is unusable", async () => {
    const fake = fakeFetch();
    const api = apiWith(fake, null);
    await assert.rejects(api.health(), (error) => error.kind === BACKEND_ERROR.UNREACHABLE);
    assert.equal(fake.calls.length, 0);
  });

  it("maps each HTTP status to something the screen can act on", async () => {
    const cases = [
      [401, BACKEND_ERROR.KEY_REJECTED],
      [409, BACKEND_ERROR.NOT_READY],
      [400, BACKEND_ERROR.INVALID],
      [422, BACKEND_ERROR.INVALID],
      [502, BACKEND_ERROR.PROVIDER_DOWN],
      [500, BACKEND_ERROR.SERVER],
    ];
    for (const [status, kind] of cases) {
      const api = apiWith(fakeFetch({status, body: {detail: "the reason"}}));
      await assert.rejects(
        api.saveCredentials({provider: "anthropic", apiKey: "x"}),
        (error) => error.kind === kind && error.status === status && error.message === "the reason"
      );
    }
  });

  it("reads FastAPI's string and validation-list error bodies", () => {
    assert.equal(readErrorDetail({detail: "api_key must be non-empty"}), "api_key must be non-empty");
    assert.equal(
      readErrorDetail({detail: [{msg: "field required"}, {msg: "value is not a valid string"}]}),
      "field required; value is not a valid string"
    );
    assert.equal(readErrorDetail({detail: []}), null);
    assert.equal(readErrorDetail(null), null);
  });

  it("falls back to a plain message when an error has no readable body", async () => {
    const api = apiWith(fakeFetch({status: 503, body: "not json"}));
    await assert.rejects(api.health(), /HTTP 503/);
  });
});

describe("settings rules", () => {
  it("accepts only ws:// and wss:// backend addresses", () => {
    assert.equal(isValidBackendUrl("ws://127.0.0.1:8710/ws/audio"), true);
    assert.equal(isValidBackendUrl("wss://example.org/ws/audio"), true);
    for (const bad of ["http://127.0.0.1:8710", "127.0.0.1:8710", "", "ws://", "not a url"]) {
      assert.equal(isValidBackendUrl(bad), false, bad);
    }
  });

  it("clamps the popup duration and falls back on junk", () => {
    assert.equal(clampDurationSeconds("12", 10), 12);
    assert.equal(clampDurationSeconds("1", 10), 4);
    assert.equal(clampDurationSeconds("999", 10), 60);
    assert.equal(clampDurationSeconds("7.6", 10), 8);
    assert.equal(clampDurationSeconds("abc", 10), 10);
    assert.equal(clampDurationSeconds("", 10), 10);
  });

  const SLUGS = ["politics", "health", "other"];

  it("never lets the catch-all topic be switched off", () => {
    assert.deepEqual(setTopicEnabled({politics: true}, "other", false), {politics: true, other: true});
    assert.deepEqual(setAllTopics(SLUGS, false), {politics: false, health: false, other: true});
    assert.deepEqual(setTopicEnabled({politics: true}, "politics", false), {politics: false});
  });

  it("summarises the optional topics for the master checkbox", () => {
    assert.equal(topicsMasterState({}, SLUGS), "all", "missing keys count as enabled");
    assert.equal(topicsMasterState({politics: false}, SLUGS), "some");
    assert.equal(topicsMasterState({politics: false, health: false, other: true}, SLUGS), "none");
  });
});

describe("provider connection wording", () => {
  it("offers exactly the providers the backend can take a key for", () => {
    assert.deepEqual(
      KEY_PROVIDERS.map((provider) => provider.id),
      ["anthropic", "openrouter"]
    );
    for (const provider of KEY_PROVIDERS) {
      assert.match(provider.keyUrl, /^https:\/\//);
    }
  });

  it("describes a stored key by its last four characters only", () => {
    assert.deepEqual(describeConnection(STATUS, "anthropic"), {
      state: "connected",
      detail: "Key ending …abcd",
      method: "api_key",
    });
  });

  it("includes remaining OpenRouter credit", () => {
    assert.equal(describeConnection(STATUS, "openrouter").detail, "Key ending …wxyz · $6.50 credit left");
  });

  it("reports an existing console sign-in truthfully", () => {
    const status = {providers: {anthropic: {configured: true, auth_method: "sign_in", key_hint: null}}};
    const connection = describeConnection(status, "anthropic");
    assert.equal(connection.state, "connected");
    assert.equal(connection.method, "sign_in");
    assert.match(connection.detail, /ant auth login/);
  });

  it("handles a missing key, an unreachable Ollama, and no status at all", () => {
    assert.equal(describeConnection({providers: {openrouter: {configured: false}}}, "openrouter").state, "missing");
    assert.equal(describeConnection(STATUS, "ollama").state, "unreachable");
    assert.equal(describeConnection(null, "anthropic").state, "missing");
  });

  it("summarises the stages, collapsing them when they are the same", () => {
    assert.equal(describeStages(STATUS), "Anthropic (claude-haiku-5-5)");
    const split = {
      ...STATUS,
      gate: {provider: "ollama", model: "gemma3:4b"},
      verify: {provider: "openrouter", model: "openai/gpt-6-luna"},
    };
    assert.equal(
      describeStages(split),
      "Ollama (gemma3:4b) finds claims · OpenRouter (openai/gpt-6-luna) checks them"
    );
    assert.equal(describeStages({configured: false}), null);
    assert.equal(describeStages(null), null);
  });
});

describe("stage routing form", () => {
  it("starts from what the backend has and is not dirty", () => {
    const form = stageFormFromStatus(STATUS);
    assert.deepEqual(form, {
      gateProvider: "anthropic",
      verifyProvider: "anthropic",
      gateModel: "claude-haiku-5-5",
      verifyModel: "claude-haiku-5-5",
      jevMode: "off",
    });
    assert.equal(isStageFormDirty(form, STATUS), false);
  });

  it("prefills the other provider's stored model when a stage is switched", () => {
    const form = withStageProvider(stageFormFromStatus(STATUS), "verify", "openrouter", STATUS);
    assert.equal(form.verifyModel, "google/gemini-3.5-flash-lite");
    assert.equal(form.gateModel, "claude-haiku-5-5", "the other stage is untouched");
    assert.equal(isStageFormDirty(form, STATUS), true);
  });

  it("shows model and Jev controls only where they apply", () => {
    const base = stageFormFromStatus(STATUS);
    assert.deepEqual(stageFormFields(base), {gateModel: true, verifyModel: true, jevMode: false});
    assert.deepEqual(stageFormFields({...base, gateProvider: "ollama"}), {
      gateModel: false,
      verifyModel: true,
      jevMode: false,
    });
    assert.equal(stageFormFields({...base, gateProvider: "openrouter"}).jevMode, true);
  });

  it("is dirty for a changed model or Jev mode, but not for an emptied field", () => {
    const base = stageFormFromStatus(STATUS);
    assert.equal(isStageFormDirty({...base, gateModel: "claude-sonnet-5-5"}, STATUS), true);
    assert.equal(isStageFormDirty({...base, gateModel: "  "}, STATUS), false);
    const onOpenRouter = withStageProvider(base, "gate", "openrouter", STATUS);
    const sameAsStored = {...STATUS, gate: {provider: "openrouter", model: "openai/gpt-6-luna"}};
    assert.equal(isStageFormDirty(onOpenRouter, sameAsStored), false);
    assert.equal(isStageFormDirty({...onOpenRouter, jevMode: "screen"}, sameAsStored), true);
  });

  it("ignores the Jev mode when the gate is not on OpenRouter", () => {
    const form = {...stageFormFromStatus(STATUS), jevMode: "screen"};
    assert.equal(isStageFormDirty(form, STATUS), false);
    assert.equal(stageRequestFromForm(form).jevMode, "");
  });

  it("refuses a Jev id typed into a model field", () => {
    const base = stageFormFromStatus(STATUS);
    assert.match(stageFormProblem({...base, verifyModel: "typesafe/jev-1"}), /pre-screen/);
    assert.match(stageFormProblem({...base, gateModel: " ~typesafe/jev-latest "}), /pre-screen/);
    assert.equal(stageFormProblem(base), null);
  });

  it("sends models only for stages that have one to set", () => {
    const form = {...stageFormFromStatus(STATUS), gateProvider: "ollama", gateModel: "left-over", verifyModel: " x/y "};
    assert.deepEqual(stageRequestFromForm(form), {
      gateProvider: "ollama",
      verifyProvider: "anthropic",
      gateModel: "",
      verifyModel: "x/y",
      jevMode: "",
    });
  });
});

describe("routes", () => {
  it("recognises the app's routes and nothing else", () => {
    assert.equal(parseRoute("#/settings"), ROUTES.SETTINGS);
    assert.equal(parseRoute("#/providers/"), ROUTES.PROVIDERS);
    assert.equal(parseRoute("#/welcome"), ROUTES.WELCOME);
    for (const hash of ["", "#", "#/", "#/nope", "#settings", undefined]) {
      assert.equal(parseRoute(hash), null, String(hash));
    }
  });

  it("sends a brand-new install to the welcome flow", () => {
    assert.equal(landingRoute({onboardingCompleted: false, providerConfigured: false}), ROUTES.WELCOME);
  });

  it("sends an existing user straight home, even if they never saw the welcome flow", () => {
    assert.equal(landingRoute({onboardingCompleted: false, providerConfigured: true}), ROUTES.HOME);
    assert.equal(landingRoute({onboardingCompleted: true, providerConfigured: false}), ROUTES.HOME);
  });
});

const EXTENSION_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const REPO_ROOT = path.resolve(EXTENSION_DIR, "..");

/** Every file under `directory` with one of `extensions`, recursively. */
const listFiles = async (directory, extensions) => {
  const found = [];
  for (const entry of await readdir(directory, {withFileTypes: true})) {
    const full = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      found.push(...(await listFiles(full, extensions)));
    } else if (extensions.some((extension) => entry.name.endsWith(extension))) {
      found.push(full);
    }
  }
  return found;
};

describe("packaging rules", () => {
  it("loads no code or styles from the network (Manifest V3 forbids remote code)", async () => {
    const files = [
      ...(await listFiles(path.join(EXTENSION_DIR, "app"), [".mjs", ".html", ".css"])),
      ...(await listFiles(path.join(EXTENSION_DIR, "popup"), [".js", ".html", ".css"])),
      ...(await listFiles(path.join(EXTENSION_DIR, "shared"), [".js"])),
    ];
    assert.ok(files.length > 10, "expected to scan the app's files");
    const remoteLoad =
      /(?:import\s[^;]*?from\s*|import\s*\(\s*|<script[^>]*\ssrc=|<link[^>]*\shref=|@import\s+(?:url\()?)["']https?:\/\//;
    for (const file of files) {
      const text = await readFile(file, "utf8");
      assert.doesNotMatch(text, remoteLoad, path.relative(REPO_ROOT, file));
    }
  });

  it("uses no inline scripts, which the extension's content security policy blocks", async () => {
    for (const file of await listFiles(path.join(EXTENSION_DIR, "app"), [".html"])) {
      const html = await readFile(file, "utf8");
      for (const [tag] of html.matchAll(/<script\b[^>]*>/g)) {
        assert.match(tag, /\ssrc=/, `${path.relative(REPO_ROOT, file)}: ${tag}`);
      }
      assert.doesNotMatch(html, /\son[a-z]+=/, "inline event handlers are blocked too");
    }
  });

  it("keeps the vendored UI runtime identical to the console's pinned copies", async () => {
    const source = path.join(REPO_ROOT, "backend", "streamer", "static", "vendor");
    const vendored = path.join(EXTENSION_DIR, "vendor");
    for (const name of ["preact/preact.module.js", "preact/hooks.module.js", "htm/htm.module.js"]) {
      const upstream = await readFile(path.join(source, name), "utf8");
      const copy = await readFile(path.join(vendored, name), "utf8");
      // The only permitted difference: bare specifiers made relative.
      assert.equal(copy, upstream.replace(/from\s*"preact"/g, 'from"./preact.module.js"'), name);
      assert.doesNotMatch(copy, /from\s*"[^."]/, `${name} still has a bare import`);
    }
  });

  it("opens the app as the extension's options page, in a tab", async () => {
    const manifest = JSON.parse(await readFile(path.join(EXTENSION_DIR, "manifest.json"), "utf8"));
    assert.deepEqual(manifest.options_ui, {page: "app/app.html", open_in_tab: true});
    assert.match(manifest.content_security_policy.extension_pages, /script-src 'self' 'wasm-unsafe-eval'/);
  });
});
