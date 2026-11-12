/**
 * Unit tests for the provider modules and the verdict rules, against
 * simulated HTTP responses. No network and no API keys.
 *
 * Run from the repo root (Node 20+, no dependencies):
 *
 *     node --test "extension/tests/*.test.mjs"
 */

import assert from "node:assert/strict";
import {describe, it} from "node:test";

import {
  MAX_EXPLANATION_CHARS,
  NO_SOURCES_NOTE,
  WEAK_EVIDENCE_NOTE,
} from "../engine/generated/prompts.mjs";
import {
  buildAttachedResultsVerifySystem,
  buildGatePrompt,
  buildSelfSearchVerifySystem,
} from "../engine/pipeline/prompt_builders.mjs";
import {
  finalizeVerdict,
  normalizeGateClaims,
  selectSources,
} from "../engine/pipeline/verdict_rules.mjs";
import * as anthropic from "../engine/providers/anthropic.mjs";
import {
  ERROR_KIND,
  ProviderError,
  addUsage,
  emptyUsage,
  extractErrorMessage,
  parseModelJson,
  redactSecrets,
  requestJson,
} from "../engine/providers/common.mjs";
import * as gemini from "../engine/providers/gemini.mjs";
import {PROVIDERS, getProvider} from "../engine/providers/index.mjs";
import * as openai from "../engine/providers/openai.mjs";
import * as openrouter from "../engine/providers/openrouter.mjs";

const KEY = "test-key-not-real";

/**
 * A fake `fetch` that replays queued responses and records every request.
 * Each queued entry is a JSON body, or {status, body, headers} for errors.
 */
const fakeFetch = (...queued) => {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({
      url,
      method: init.method,
      headers: init.headers,
      body: init.body ? JSON.parse(init.body) : null,
    });
    const next = queued.shift();
    if (next === undefined) {
      throw new Error(`unexpected request to ${url}`);
    }
    const {status = 200, body = next, headers = {}} = next?.__response ? next : {};
    return {
      ok: status >= 200 && status < 300,
      status,
      statusText: "",
      headers: {get: (name) => headers[name.toLowerCase()] ?? null},
      text: async () => (typeof body === "string" ? body : JSON.stringify(body)),
    };
  };
  return {fetchImpl, calls};
};

const errorResponse = (status, body, headers = {}) => ({__response: true, status, body, headers});

const VERDICT_JSON = JSON.stringify({
  evidence: "strong",
  explanation: "The tower is 330 metres tall, not 450.",
  label: "FALSE",
  source_urls: ["https://www.toureiffel.paris/en/the-monument/key-figures"],
});
const EIFFEL_URL = "https://www.toureiffel.paris/en/the-monument/key-figures";
const GATE_JSON = JSON.stringify({
  claims: [{claim_text: "The Eiffel Tower is 450 meters tall.", check_worthiness: 0.9, topic: "other"}],
});

describe("prompt builders", () => {
  it("fills the gate input and marks empty context", () => {
    const prompt = buildGatePrompt("  ", " the tower is tall ");
    assert.match(prompt.instructions, /HARD EXCLUSIONS/);
    assert.match(prompt.input, /CONTEXT \(reference resolution only\): \(none\)/);
    assert.match(prompt.input, /NEW TRANSCRIPT: the tower is tall\n/);
    assert.doesNotMatch(prompt.input, /\{context\}|\{new_transcript\}/);
  });

  it("fills every placeholder of the self-search prompt", () => {
    const system = buildSelfSearchVerifySystem({
      searchTool: "Google Search",
      maxUses: 3,
      date: "2026-11-11",
    });
    assert.match(system, /Today is 2026-11-11\./);
    assert.match(system, /Use Google Search\s+to find/);
    assert.match(system, /at most\s+3 times/);
    assert.doesNotMatch(system, /\{[a-z_]+\}/);
  });

  it("never tells an attached-results provider to search", () => {
    const system = buildAttachedResultsVerifySystem({date: "2026-11-11"});
    assert.match(system, /results\s+retrieved for that claim are attached/);
    assert.doesNotMatch(system, /web_search tool/);
    assert.doesNotMatch(system, /\{date\}/);
  });
});

describe("selectSources", () => {
  const retrieved = [
    {url: "https://a.example/one", title: "One"},
    {url: "https://b.example/two/", title: "Two"},
  ];
  const cited = [{url: "https://c.example/three", title: "Three"}];

  it("keeps picked URLs only if the search really returned them", () => {
    const sources = selectSources({
      retrieved,
      cited,
      picked: ["https://b.example/two", "https://made-up.example/never-seen"],
    });
    assert.deepEqual(sources, [{url: "https://b.example/two/", title: "Two"}]);
  });

  it("falls back to cited, then to retrieved", () => {
    assert.deepEqual(selectSources({retrieved, cited, picked: ["https://nope.example"]}), cited);
    assert.deepEqual(selectSources({retrieved, picked: []}), retrieved);
  });

  it("returns nothing when there was no search output", () => {
    assert.deepEqual(selectSources({picked: ["https://anything.example"]}), []);
  });

  it("deduplicates and caps at five", () => {
    const many = Array.from({length: 9}, (_, index) => ({url: `https://x.example/${index % 7}`}));
    const sources = selectSources({retrieved: many});
    assert.equal(sources.length, 5);
    assert.equal(new Set(sources.map((source) => source.url)).size, 5);
  });
});

describe("finalizeVerdict", () => {
  const source = [{url: "https://a.example", title: null}];

  it("passes a well-supported verdict through", () => {
    const verdict = finalizeVerdict(
      {label: "false", explanation: " It is 330 m. ", evidence: "strong"},
      source
    );
    assert.deepEqual(verdict, {
      label: "FALSE",
      explanation: "It is 330 m.",
      evidence: "strong",
      sources: source,
    });
  });

  it("downgrades a verdict with no sources", () => {
    const verdict = finalizeVerdict({label: "TRUE", explanation: "Yes.", evidence: "strong"}, []);
    assert.equal(verdict.label, "UNVERIFIED");
    assert.ok(verdict.explanation.endsWith(NO_SOURCES_NOTE));
  });

  it("downgrades a verdict whose own evidence rating is weak", () => {
    for (const evidence of ["partial", "none"]) {
      const verdict = finalizeVerdict({label: "FALSE", explanation: "No.", evidence}, source);
      assert.equal(verdict.label, "UNVERIFIED", evidence);
      assert.ok(verdict.explanation.endsWith(WEAK_EVIDENCE_NOTE));
    }
  });

  it("leaves UNVERIFIED alone and adds no note", () => {
    const verdict = finalizeVerdict(
      {label: "UNVERIFIED", explanation: "Unclear.", evidence: "none"},
      []
    );
    assert.equal(verdict.explanation, "Unclear.");
  });

  it("clamps a long explanation", () => {
    const verdict = finalizeVerdict(
      {label: "TRUE", explanation: "word ".repeat(200), evidence: "strong"},
      source
    );
    assert.equal(verdict.explanation.length, MAX_EXPLANATION_CHARS);
    assert.ok(verdict.explanation.endsWith("…"));
  });

  it("rejects output that is not a verdict", () => {
    assert.throws(() => finalizeVerdict(null, source), /not an object/);
    assert.throws(() => finalizeVerdict({label: "MAYBE", explanation: "x"}, source), /unknown label/);
    assert.throws(() => finalizeVerdict({label: "TRUE", explanation: " "}, source), /no explanation/);
  });

  it("treats an unknown evidence rating as absent, not as weak", () => {
    const verdict = finalizeVerdict({label: "TRUE", explanation: "Yes.", evidence: "solid"}, source);
    assert.equal(verdict.evidence, null);
    assert.equal(verdict.label, "TRUE");
  });
});

describe("normalizeGateClaims", () => {
  it("coerces topics, clamps scores and drops malformed claims", () => {
    const claims = normalizeGateClaims({
      claims: [
        {claim_text: " A fact. ", check_worthiness: 1.4, topic: "Politics"},
        {claim_text: "Another.", check_worthiness: "0.5", topic: "astrology"},
        {claim_text: "", check_worthiness: 0.9, topic: "other"},
        {claim_text: "No score.", topic: "other"},
        "not an object",
      ],
    });
    assert.deepEqual(claims, [
      {claim_text: "A fact.", check_worthiness: 1, topic: "politics"},
      {claim_text: "Another.", check_worthiness: 0.5, topic: "other"},
    ]);
  });

  it("accepts an empty list and rejects a missing one", () => {
    assert.deepEqual(normalizeGateClaims({claims: []}), []);
    assert.throws(() => normalizeGateClaims({}), /no claims array/);
  });
});

describe("common helpers", () => {
  it("parses bare, fenced and wrapped JSON", () => {
    assert.deepEqual(parseModelJson('{"a": 1}'), {a: 1});
    assert.deepEqual(parseModelJson('```json\n{"a": 1}\n```'), {a: 1});
    assert.deepEqual(parseModelJson('Here you go: {"a": {"b": 2}} Hope that helps.'), {a: {b: 2}});
  });

  it("raises INVALID_OUTPUT for anything else", () => {
    for (const text of ["", "LABEL: TRUE", "[1, 2]", '{"a": '] ) {
      assert.throws(
        () => parseModelJson(text),
        (error) => error instanceof ProviderError && error.kind === ERROR_KIND.INVALID_OUTPUT
      );
    }
  });

  it("reads error messages from every provider's body shape", () => {
    assert.equal(extractErrorMessage({error: {message: "bad key"}}), "bad key");
    assert.equal(extractErrorMessage({error: "bad key"}), "bad key");
    assert.equal(extractErrorMessage([{error: {message: "bad key"}}]), "bad key");
    assert.equal(extractErrorMessage({message: "bad key"}), "bad key");
    assert.equal(extractErrorMessage({}), null);
  });

  it("maps HTTP failures to actionable kinds", async () => {
    const cases = [
      [401, ERROR_KIND.AUTH],
      [403, ERROR_KIND.AUTH],
      [402, ERROR_KIND.BILLING],
      [429, ERROR_KIND.RATE_LIMIT],
      [400, ERROR_KIND.BAD_REQUEST],
      [404, ERROR_KIND.BAD_REQUEST],
      [503, ERROR_KIND.SERVER],
    ];
    for (const [status, kind] of cases) {
      const {fetchImpl} = fakeFetch(errorResponse(status, {error: {message: "nope"}}));
      await assert.rejects(
        requestJson("https://x.example", {headers: {}, body: {}, fetchImpl}),
        (error) => error.kind === kind && error.status === status && error.message === "nope"
      );
    }
  });

  it("carries Retry-After and reports network failures", async () => {
    const limited = fakeFetch(errorResponse(429, {error: "slow"}, {"retry-after": "17"}));
    await assert.rejects(
      requestJson("https://x.example", {headers: {}, body: {}, fetchImpl: limited.fetchImpl}),
      (error) => error.retryAfterS === 17
    );
    const offline = async () => {
      throw new TypeError("Failed to fetch");
    };
    await assert.rejects(
      requestJson("https://x.example", {headers: {}, body: {}, fetchImpl: offline}),
      (error) => error.kind === ERROR_KIND.NETWORK
    );
  });

  it("lets the caller's cancellation through untouched", async () => {
    const controller = new AbortController();
    controller.abort();
    const aborted = async (_url, init) => {
      init.signal.throwIfAborted();
    };
    await assert.rejects(
      requestJson("https://x.example", {
        headers: {},
        body: {},
        fetchImpl: aborted,
        signal: controller.signal,
      }),
      (error) => !(error instanceof ProviderError)
    );
  });

  it("strips keys, and anything key-shaped, from provider error text", async () => {
    assert.equal(
      redactSecrets("Incorrect API key provided: sk-proj-************abcd. Check it."),
      "Incorrect API key provided: [key removed]. Check it."
    );
    assert.equal(redactSecrets("bad key AIzaSyD-1234567890abc"), "bad key [key removed]");
    assert.equal(redactSecrets("token zz-custom-secret-value", ["zz-custom-secret-value"]), "token [key removed]");
    assert.equal(redactSecrets("nothing to hide here"), "nothing to hide here");

    const secret = "zz-opaque-key-0123456789";
    const {fetchImpl} = fakeFetch(
      errorResponse(401, {error: {message: `Invalid key ${secret} (also sk-live-9999999999)`}})
    );
    await assert.rejects(
      requestJson("https://x.example", {headers: {Authorization: `Bearer ${secret}`}, body: {}, fetchImpl}),
      (error) => !error.message.includes(secret) && !error.message.includes("sk-live") && error.message.includes("[key removed]")
    );
  });

  it("adds usage, keeping cost unknown until a provider reports one", () => {
    let usage = addUsage(emptyUsage(), {inputTokens: 10, outputTokens: 5, webSearches: 1});
    assert.equal(usage.costUsd, null);
    assert.equal(usage.calls, 1);
    usage = addUsage(usage, {inputTokens: 1, costUsd: 0.002});
    assert.deepEqual(usage, {
      inputTokens: 11,
      cachedInputTokens: 0,
      outputTokens: 5,
      webSearches: 1,
      costUsd: 0.002,
      calls: 2,
    });
  });
});

describe("registry", () => {
  it("offers the four providers with a uniform surface", () => {
    assert.deepEqual(Object.keys(PROVIDERS).sort(), ["anthropic", "gemini", "openai", "openrouter"]);
    for (const provider of Object.values(PROVIDERS)) {
      for (const name of ["validateKey", "runGate", "runVerify"]) {
        assert.equal(typeof provider[name], "function", `${provider.id}.${name}`);
      }
      assert.ok(provider.defaultModels.gate && provider.defaultModels.verify, provider.id);
      assert.match(provider.hostPermission, /^https:\/\/[a-z.]+\/\*$/, provider.id);
      assert.match(provider.keyUrl, /^https:\/\//, provider.id);
    }
    assert.throws(() => getProvider("ollama"), /Known providers/);
  });

  it("never puts the API key in a URL", async () => {
    const replies = {
      anthropic: {data: []},
      openai: {data: []},
      gemini: {models: []},
      openrouter: {data: {}},
    };
    for (const provider of Object.values(PROVIDERS)) {
      const {fetchImpl, calls} = fakeFetch(replies[provider.id]);
      await provider.validateKey(KEY, {fetchImpl});
      assert.equal(calls[0].method, "GET");
      assert.ok(!calls[0].url.includes(KEY), provider.id);
      assert.ok(Object.values(calls[0].headers).some((value) => value.includes(KEY)), provider.id);
    }
  });
});

describe("anthropic", () => {
  const searchTurn = (text, extra = {}) => ({
    stop_reason: "end_turn",
    usage: {
      input_tokens: 900,
      cache_read_input_tokens: 1700,
      cache_creation_input_tokens: 0,
      output_tokens: 210,
      server_tool_use: {web_search_requests: 1},
    },
    content: [
      {type: "text", text: "I'll look that up."},
      {type: "server_tool_use", id: "srvtoolu_1", name: "web_search", input: {query: "eiffel tower height"}},
      {
        type: "web_search_tool_result",
        tool_use_id: "srvtoolu_1",
        content: [
          {type: "web_search_result", url: EIFFEL_URL, title: "Key figures"},
          {type: "web_search_result", url: "https://en.wikipedia.org/wiki/Eiffel_Tower", title: "Eiffel Tower"},
        ],
      },
      {type: "text", text},
    ],
    ...extra,
  });

  it("sends the browser-access header and a strict, cached, thinking-off gate request", async () => {
    const {fetchImpl, calls} = fakeFetch({
      stop_reason: "end_turn",
      usage: {input_tokens: 40, cache_read_input_tokens: 1700, output_tokens: 30},
      content: [{type: "text", text: GATE_JSON}],
    });
    const result = await anthropic.runGate({apiKey: KEY, context: "", transcript: "the tower is 450 meters", fetchImpl});
    const [call] = calls;
    assert.equal(call.url, "https://api.anthropic.com/v1/messages");
    assert.equal(call.headers["x-api-key"], KEY);
    assert.equal(call.headers["anthropic-dangerous-direct-browser-access"], "true");
    assert.equal(call.headers["anthropic-version"], "2026-10-21");
    assert.equal(call.body.model, "claude-haiku-5-5");
    assert.deepEqual(call.body.thinking, {type: "disabled"});
    assert.equal(call.body.output_config.format.type, "json_schema");
    assert.deepEqual(call.body.system[0].cache_control, {type: "ephemeral"});
    assert.ok(!("temperature" in call.body), "sampling parameters are rejected on this API version");
    assert.equal(result.claims.length, 1);
    assert.deepEqual(result.usage, {
      inputTokens: 40,
      cachedInputTokens: 1700,
      outputTokens: 30,
      webSearches: 0,
      costUsd: null,
      calls: 1,
    });
  });

  it("verifies with the web search tool and takes sources only from its results", async () => {
    const {fetchImpl, calls} = fakeFetch(searchTurn(VERDICT_JSON));
    const result = await anthropic.runVerify({apiKey: KEY, claim: "The Eiffel Tower is 450 meters tall.", fetchImpl});
    const [call] = calls;
    assert.deepEqual(call.body.tools, [{type: "web_search_20250305", name: "web_search", max_uses: 2}]);
    assert.deepEqual(call.body.thinking, {type: "adaptive"});
    assert.deepEqual(call.body.messages, [{role: "user", content: "The Eiffel Tower is 450 meters tall."}]);
    assert.equal(result.verdict.label, "FALSE");
    assert.deepEqual(result.verdict.sources, [{url: EIFFEL_URL, title: "Key figures"}]);
    assert.equal(result.usage.webSearches, 1);
  });

  it("drops a source URL the model invented", async () => {
    const invented = JSON.stringify({
      evidence: "strong",
      explanation: "It is 330 m.",
      label: "FALSE",
      source_urls: ["https://totally-made-up.example/eiffel"],
    });
    const {fetchImpl} = fakeFetch(searchTurn(invented));
    const result = await anthropic.runVerify({apiKey: KEY, claim: "x", fetchImpl});
    assert.equal(result.verdict.label, "FALSE");
    assert.ok(result.verdict.sources.every((source) => !source.url.includes("made-up")));
    assert.equal(result.verdict.sources.length, 2, "falls back to what the search returned");
  });

  it("downgrades a confident verdict when the model never searched", async () => {
    const {fetchImpl} = fakeFetch({
      stop_reason: "end_turn",
      usage: {input_tokens: 10, output_tokens: 10},
      content: [{type: "text", text: VERDICT_JSON}],
    });
    const result = await anthropic.runVerify({apiKey: KEY, claim: "x", fetchImpl});
    assert.equal(result.verdict.label, "UNVERIFIED");
    assert.deepEqual(result.verdict.sources, []);
  });

  it("resumes a paused search turn by sending back the content so far", async () => {
    const paused = {
      stop_reason: "pause_turn",
      usage: {input_tokens: 100, output_tokens: 20, server_tool_use: {web_search_requests: 1}},
      content: [{type: "server_tool_use", id: "srvtoolu_1", name: "web_search", input: {query: "q"}}],
    };
    const {fetchImpl, calls} = fakeFetch(paused, searchTurn(VERDICT_JSON));
    const result = await anthropic.runVerify({apiKey: KEY, claim: "x", fetchImpl});
    assert.equal(calls.length, 2);
    assert.equal(calls[1].body.messages.length, 2);
    assert.equal(calls[1].body.messages[1].role, "assistant");
    assert.equal(calls[1].body.messages[1].content[0].type, "server_tool_use");
    assert.equal(result.verdict.label, "FALSE");
    assert.equal(result.usage.calls, 2);
    assert.equal(result.usage.webSearches, 2);
  });

  it("gives up on a turn that stays paused, and surfaces refusals", async () => {
    const paused = {stop_reason: "pause_turn", usage: {}, content: [{type: "server_tool_use", id: "s", name: "web_search", input: {}}]};
    const stuck = fakeFetch(paused, paused, paused);
    await assert.rejects(anthropic.runVerify({apiKey: KEY, claim: "x", fetchImpl: stuck.fetchImpl}), /did not finish/);
    const refused = fakeFetch({stop_reason: "refusal", usage: {}, content: []});
    await assert.rejects(
      anthropic.runVerify({apiKey: KEY, claim: "x", fetchImpl: refused.fetchImpl}),
      (error) => error.kind === ERROR_KIND.REFUSED
    );
  });

  it("ignores a failed search (an error object where results would be)", async () => {
    const {fetchImpl} = fakeFetch({
      stop_reason: "end_turn",
      usage: {},
      content: [
        {type: "server_tool_use", id: "s", name: "web_search", input: {}},
        {type: "web_search_tool_result", tool_use_id: "s", content: {type: "web_search_tool_result_error", error_code: "max_uses_exceeded"}},
        {type: "text", text: VERDICT_JSON},
      ],
    });
    const result = await anthropic.runVerify({apiKey: KEY, claim: "x", fetchImpl});
    assert.equal(result.verdict.label, "UNVERIFIED");
  });

  it("reports an exhausted balance as a billing problem", async () => {
    const {fetchImpl} = fakeFetch(
      errorResponse(400, {type: "error", error: {type: "invalid_request_error", message: "Your credit balance is too low to access the Anthropic API."}})
    );
    await assert.rejects(
      anthropic.runGate({apiKey: KEY, context: "", transcript: "x", fetchImpl}),
      (error) => error.kind === ERROR_KIND.BILLING
    );
  });
});

describe("openai", () => {
  const response = (text, extra = {}) => ({
    status: "completed",
    usage: {input_tokens: 2100, input_tokens_details: {cached_tokens: 1800}, output_tokens: 150},
    output: [
      {
        type: "web_search_call",
        status: "completed",
        action: {type: "search", query: "eiffel tower height", sources: [{type: "url", url: EIFFEL_URL}]},
      },
      {type: "message", role: "assistant", content: [{type: "output_text", text, annotations: []}]},
    ],
    ...extra,
  });

  it("asks for strict JSON and does not store the request", async () => {
    const {fetchImpl, calls} = fakeFetch({
      status: "completed",
      usage: {input_tokens: 500, output_tokens: 40},
      output: [{type: "message", content: [{type: "output_text", text: GATE_JSON}]}],
    });
    const result = await openai.runGate({apiKey: KEY, context: "", transcript: "x", fetchImpl});
    const [call] = calls;
    assert.equal(call.url, "https://api.openai.com/v1/responses");
    assert.equal(call.headers.Authorization, `Bearer ${KEY}`);
    assert.equal(call.body.store, false);
    assert.equal(call.body.text.format.type, "json_schema");
    assert.equal(call.body.text.format.strict, true);
    assert.equal(call.body.input[0].role, "system");
    assert.equal(result.claims.length, 1);
  });

  it("verifies with web search, requesting the consulted sources", async () => {
    const {fetchImpl, calls} = fakeFetch(response(VERDICT_JSON));
    const result = await openai.runVerify({apiKey: KEY, claim: "The Eiffel Tower is 450 meters tall.", fetchImpl});
    const [call] = calls;
    assert.equal(call.body.tools[0].type, "web_search");
    assert.deepEqual(call.body.include, ["web_search_call.action.sources"]);
    assert.equal(call.body.input[1].content, "The Eiffel Tower is 450 meters tall.");
    assert.equal(result.verdict.label, "FALSE");
    assert.deepEqual(result.verdict.sources, [{url: EIFFEL_URL, title: null}]);
    assert.deepEqual(result.usage, {
      inputTokens: 300,
      cachedInputTokens: 1800,
      outputTokens: 150,
      webSearches: 1,
      costUsd: null,
      calls: 1,
    });
  });

  it("counts only search actions as billable searches", async () => {
    const {fetchImpl} = fakeFetch(
      response(VERDICT_JSON, {
        output: [
          {type: "web_search_call", action: {type: "search", sources: [{url: EIFFEL_URL}]}},
          {type: "web_search_call", action: {type: "open_page", url: EIFFEL_URL}},
          {type: "web_search_call", action: {type: "search", sources: []}},
          {type: "message", content: [{type: "output_text", text: VERDICT_JSON}]},
        ],
      })
    );
    const result = await openai.runVerify({apiKey: KEY, claim: "x", fetchImpl});
    assert.equal(result.usage.webSearches, 2);
  });

  it("surfaces refusals and cut-off responses", async () => {
    const refused = fakeFetch({status: "completed", output: [{type: "message", content: [{type: "refusal", refusal: "I can't help with that."}]}]});
    await assert.rejects(
      openai.runVerify({apiKey: KEY, claim: "x", fetchImpl: refused.fetchImpl}),
      (error) => error.kind === ERROR_KIND.REFUSED
    );
    const cut = fakeFetch({status: "incomplete", incomplete_details: {reason: "max_output_tokens"}, output: []});
    await assert.rejects(openai.runVerify({apiKey: KEY, claim: "x", fetchImpl: cut.fetchImpl}), /cut off \(max_output_tokens\)/);
  });

  it("reports an empty account as billing, not as a rate limit", async () => {
    const {fetchImpl} = fakeFetch(
      errorResponse(429, {error: {message: "You exceeded your current quota.", code: "insufficient_quota"}})
    );
    await assert.rejects(
      openai.validateKey(KEY, {fetchImpl}),
      (error) => error.kind === ERROR_KIND.BILLING
    );
  });
});

describe("gemini", () => {
  const interaction = (text, {annotations = [], extra = {}} = {}) => ({
    status: "completed",
    usage: {
      total_input_tokens: 2000,
      total_cached_tokens: 0,
      total_output_tokens: 120,
      total_thought_tokens: 80,
      grounding_tool_count: [{type: "google_search", count: 2}],
    },
    steps: [
      {type: "google_search_call", id: "search_001", queries: ["eiffel tower height", "eiffel tower 450 m"]},
      {type: "google_search_result", call_id: "search_001"},
      {type: "model_output", content: [{type: "text", text, annotations}]},
    ],
    ...extra,
  });
  const citation = {type: "url_citation", url: EIFFEL_URL, title: "Key figures", start_index: 0, end_index: 10};

  it("strips the schema keyword Gemini rejects, at every depth", () => {
    const converted = gemini.toGeminiSchema({
      type: "object",
      additionalProperties: false,
      properties: {claims: {type: "array", items: {type: "object", additionalProperties: false, properties: {}}}},
    });
    assert.ok(!JSON.stringify(converted).includes("additionalProperties"));
    assert.equal(converted.properties.claims.items.type, "object");
  });

  it("sends the key in a header and the schema in response_format", async () => {
    const {fetchImpl, calls} = fakeFetch(interaction(GATE_JSON));
    await gemini.runGate({apiKey: KEY, context: "", transcript: "x", fetchImpl});
    const [call] = calls;
    assert.equal(call.url, "https://generativelanguage.googleapis.com/v1beta/interactions");
    assert.equal(call.headers["x-goog-api-key"], KEY);
    assert.equal(call.headers["Api-Revision"], "2026-05-20");
    assert.equal(call.body.store, false);
    assert.equal(call.body.response_format.mime_type, "application/json");
    assert.match(call.body.system_instruction, /HARD EXCLUSIONS/);
    assert.ok(!("tools" in call.body));
  });

  it("uses the strict single call when it returns JSON and sources", async () => {
    const {fetchImpl, calls} = fakeFetch(interaction(VERDICT_JSON, {annotations: [citation]}));
    const result = await gemini.runVerify({apiKey: KEY, claim: "x", fetchImpl});
    assert.equal(calls.length, 1);
    assert.deepEqual(calls[0].body.tools, [{type: "google_search"}]);
    assert.ok(calls[0].body.response_format);
    assert.match(calls[0].body.system_instruction, /Use Google Search/);
    assert.equal(result.mode, "strict");
    assert.equal(result.verdict.label, "FALSE");
    assert.deepEqual(result.verdict.sources, [{url: EIFFEL_URL, title: "Key figures"}]);
    assert.equal(result.usage.webSearches, 2, "billed per search query");
    assert.equal(result.usage.outputTokens, 200, "thinking tokens are billed as output");
  });

  it("falls back to a prompted call when strict mode loses the citations", async () => {
    const {fetchImpl, calls} = fakeFetch(
      interaction(VERDICT_JSON),
      interaction("```json\n" + VERDICT_JSON + "\n```", {annotations: [citation]})
    );
    const result = await gemini.runVerify({apiKey: KEY, claim: "x", fetchImpl});
    assert.equal(calls.length, 2);
    assert.ok(!("response_format" in calls[1].body), "second call asks for JSON by prompt only");
    assert.equal(result.mode, "prompted-after-strict");
    assert.equal(result.verdict.label, "FALSE");
    assert.equal(result.usage.calls, 2);
    assert.equal(result.usage.webSearches, 4, "the fallback's searches are billed too");
  });

  it("falls back when strict mode returns truncated JSON", async () => {
    const {fetchImpl, calls} = fakeFetch(
      interaction('ower is 330 metres tall.", "label": "FALSE"', {annotations: [citation]}),
      interaction(VERDICT_JSON, {annotations: [citation]})
    );
    const result = await gemini.runVerify({apiKey: KEY, claim: "x", fetchImpl});
    assert.equal(calls.length, 2);
    assert.equal(result.verdict.label, "FALSE");
  });

  it("repairs a free-text answer with a third, ungrounded call", async () => {
    const {fetchImpl, calls} = fakeFetch(
      interaction(VERDICT_JSON),
      interaction("The claim is false: the tower is 330 metres tall.", {annotations: [citation]}),
      interaction(VERDICT_JSON)
    );
    const result = await gemini.runVerify({apiKey: KEY, claim: "x", fetchImpl});
    assert.equal(calls.length, 3);
    assert.ok(!("tools" in calls[2].body), "the repair call must not search");
    assert.equal(result.mode, "repaired-after-strict");
    assert.deepEqual(result.verdict.sources, [{url: EIFFEL_URL, title: "Key figures"}]);
  });

  it("honours an explicit mode", async () => {
    const strictOnly = fakeFetch(interaction(VERDICT_JSON));
    const strict = await gemini.runVerify({apiKey: KEY, claim: "x", verifyMode: "strict", fetchImpl: strictOnly.fetchImpl});
    assert.equal(strictOnly.calls.length, 1);
    assert.equal(strict.verdict.label, "UNVERIFIED", "no citations -> downgraded, not retried");

    const promptedOnly = fakeFetch(interaction(VERDICT_JSON, {annotations: [citation]}));
    const prompted = await gemini.runVerify({apiKey: KEY, claim: "x", verifyMode: "prompted", fetchImpl: promptedOnly.fetchImpl});
    assert.equal(prompted.mode, "prompted");
    assert.ok(!("response_format" in promptedOnly.calls[0].body));
  });

  it("explains a free-tier key and a bad key distinctly", async () => {
    const freeTier = fakeFetch(
      errorResponse(429, {error: {message: "Search grounding is not available on the free tier. Enable billing.", status: "RESOURCE_EXHAUSTED"}})
    );
    await assert.rejects(
      gemini.runVerify({apiKey: KEY, claim: "x", fetchImpl: freeTier.fetchImpl}),
      (error) => error.kind === ERROR_KIND.BILLING
    );
    const badKey = fakeFetch(errorResponse(400, {error: {message: "API key not valid. Please pass a valid API key."}}));
    await assert.rejects(
      gemini.validateKey(KEY, {fetchImpl: badKey.fetchImpl}),
      (error) => error.kind === ERROR_KIND.AUTH
    );
  });

  it("rejects an interaction that did not complete", async () => {
    const {fetchImpl} = fakeFetch({status: "failed", steps: []});
    await assert.rejects(gemini.runGate({apiKey: KEY, context: "", transcript: "x", fetchImpl}), /status "failed"/);
  });
});

describe("openrouter", () => {
  const completion = (content, {annotations = [], usage = {}} = {}) => ({
    choices: [{finish_reason: "stop", message: {role: "assistant", content, annotations}}],
    usage: {prompt_tokens: 1900, completion_tokens: 90, cost: 0.0074, ...usage},
  });
  const citation = {type: "url_citation", url_citation: {url: EIFFEL_URL, title: "Key figures"}};
  const ATTACHED_VERDICT = JSON.stringify({label: "FALSE", explanation: "It is 330 m.", evidence: "strong"});

  it("requires strict JSON support from the upstream model", async () => {
    const {fetchImpl, calls} = fakeFetch(completion(GATE_JSON));
    const result = await openrouter.runGate({apiKey: KEY, context: "", transcript: "x", fetchImpl});
    const [call] = calls;
    assert.equal(call.url, "https://openrouter.ai/api/v1/chat/completions");
    assert.equal(call.headers.Authorization, `Bearer ${KEY}`);
    assert.deepEqual(call.body.provider, {require_parameters: true});
    assert.equal(call.body.response_format.json_schema.strict, true);
    assert.deepEqual(call.body.plugins, [{id: "response-healing"}]);
    assert.equal(result.usage.costUsd, 0.0074);
    assert.equal(result.usage.webSearches, 0);
  });

  it("verifies with the web plugin and the bare claim as the user message", async () => {
    const {fetchImpl, calls} = fakeFetch(completion(ATTACHED_VERDICT, {annotations: [citation]}));
    const result = await openrouter.runVerify({apiKey: KEY, claim: "The Eiffel Tower is 450 meters tall.", fetchImpl});
    const [call] = calls;
    const webPlugin = call.body.plugins.find((plugin) => plugin.id === "web");
    assert.equal(webPlugin.max_results, 5);
    assert.equal(webPlugin.engine, "exa");
    assert.match(webPlugin.search_prompt, /evidence for the fact-check/);
    assert.deepEqual(call.body.messages[1], {role: "user", content: "The Eiffel Tower is 450 meters tall."});
    assert.doesNotMatch(call.body.messages[0].content, /web_search tool/);
    assert.equal(result.verdict.label, "FALSE");
    assert.deepEqual(result.verdict.sources, [{url: EIFFEL_URL, title: "Key figures"}]);
    assert.equal(result.usage.costUsd, 0.0074);
    assert.equal(result.usage.webSearches, 1);
  });

  it("downgrades a verdict that came back without citations", async () => {
    const {fetchImpl} = fakeFetch(completion(ATTACHED_VERDICT));
    const result = await openrouter.runVerify({apiKey: KEY, claim: "x", fetchImpl});
    assert.equal(result.verdict.label, "UNVERIFIED");
  });

  it("treats an error inside a 200 response as a failure", async () => {
    const {fetchImpl} = fakeFetch({error: {message: "Upstream provider timed out", code: 504}});
    await assert.rejects(
      openrouter.runVerify({apiKey: KEY, claim: "x", fetchImpl}),
      (error) => error.kind === ERROR_KIND.SERVER
    );
  });

  it("reports missing credit as a billing problem", async () => {
    const {fetchImpl} = fakeFetch(errorResponse(402, {error: {message: "Insufficient credits"}}));
    await assert.rejects(
      openrouter.runVerify({apiKey: KEY, claim: "x", fetchImpl}),
      (error) => error.kind === ERROR_KIND.BILLING
    );
  });
});
