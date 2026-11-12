/**
 * Anthropic provider: Claude over the Messages API, called directly from the
 * extension with the viewer's own API key.
 *
 * Mirrors backend/app/llm_anthropic.py:
 *   - gate: strict JSON output (`output_config.format`), thinking off, the
 *     static instructions sent as a cached system block;
 *   - verify: the server-side `web_search` tool plus strict JSON in one call,
 *     adaptive thinking at low effort. The model searches itself; source URLs
 *     are taken only from what the tool returned.
 *
 * Raw HTTP rather than the Anthropic SDK: the extension has no build step
 * and vendors every dependency as source (see providers/common.mjs).
 *
 * The `anthropic-dangerous-direct-browser-access` header is what allows a
 * browser origin to call the API; it exists for bring-your-own-key apps like
 * this one, where the key belongs to the person using the browser.
 */

import {
  BACKEND_DEFAULTS,
  GATE_SCHEMA,
  VERDICT_SELF_SEARCH_SCHEMA,
} from "../generated/prompts.mjs";
import {buildGatePrompt, buildSelfSearchVerifySystem} from "../pipeline/prompt_builders.mjs";
import {finalizeVerdict, normalizeGateClaims, selectSources} from "../pipeline/verdict_rules.mjs";
import {
  ERROR_KIND,
  ProviderError,
  addUsage,
  emptyUsage,
  parseModelJson,
  requestJson,
} from "./common.mjs";

const API_ORIGIN = "https://api.anthropic.com";
const API_VERSION = "2026-10-21";

// Claude Haiku 5.5 takes the basic web search tool; the dynamic-filtering
// variant is for the larger models.
const WEB_SEARCH_TOOL_TYPE = "web_search_20250305";

const GATE_MAX_TOKENS = 1200;
// Verification thinks, and thinking tokens count against max_tokens.
const VERIFY_MAX_TOKENS = 4096;
// The server may pause a long search turn; resume it at most this many times.
const MAX_PAUSE_CONTINUATIONS = 2;

export const id = "anthropic";
export const label = "Anthropic";
export const keyUrl = "https://platform.claude.com/settings/keys";
export const hostPermission = `${API_ORIGIN}/*`;
export const defaultModels = Object.freeze({
  gate: BACKEND_DEFAULTS.anthropic_model,
  verify: BACKEND_DEFAULTS.anthropic_model,
});

const headers = (apiKey) => ({
  "x-api-key": apiKey,
  "anthropic-version": API_VERSION,
  "anthropic-dangerous-direct-browser-access": "true",
});

/** Out of credit arrives as a 400 with a telling message, not a 402. */
const classifyError = (status, message) =>
  status === 400 && /credit balance|billing/i.test(message) ? ERROR_KIND.BILLING : null;

/** @param {object} usage - the API's usage object @returns {Partial<import("./common.mjs").Usage>} */
const readUsage = (usage = {}) => ({
  inputTokens: (usage.input_tokens ?? 0) + (usage.cache_creation_input_tokens ?? 0),
  cachedInputTokens: usage.cache_read_input_tokens ?? 0,
  outputTokens: usage.output_tokens ?? 0,
  webSearches: usage.server_tool_use?.web_search_requests ?? 0,
});

const cachedSystem = (text) => [{type: "text", text, cache_control: {type: "ephemeral"}}];

const createMessage = (apiKey, body, options) =>
  requestJson(`${API_ORIGIN}/v1/messages`, {
    headers: headers(apiKey),
    body,
    classifyError,
    signal: options.signal,
    fetchImpl: options.fetchImpl,
  });

/**
 * The model's prose after the last server-tool block. With web search the
 * content is [text?, server_tool_use, web_search_tool_result, …, text]: text
 * before a search is narration, and the answer follows the last tool block.
 *
 * @param {object[]} content
 * @returns {string}
 */
const finalText = (content) => {
  let segment = [];
  for (const block of content) {
    if (block.type === "text") {
      segment.push(block.text ?? "");
    } else if (block.type === "server_tool_use" || String(block.type).endsWith("_tool_result")) {
      segment = [];
    }
  }
  return segment.join("");
};

/**
 * @param {object[]} content
 * @returns {{retrieved: object[], cited: object[]}}
 */
const collectSources = (content) => {
  const retrieved = [];
  const cited = [];
  for (const block of content) {
    if (block.type === "web_search_tool_result") {
      // On failure `content` is an error object, not a list of results.
      if (Array.isArray(block.content)) {
        for (const result of block.content) {
          if (result.type === "web_search_result" && result.url) {
            retrieved.push({url: result.url, title: result.title ?? null});
          }
        }
      }
    } else if (block.type === "text") {
      for (const citation of block.citations ?? []) {
        if (citation.type === "web_search_result_location" && citation.url) {
          cited.push({url: citation.url, title: citation.title ?? null});
        }
      }
    }
  }
  return {retrieved, cited};
};

/**
 * Check a key with a free call.
 *
 * @param {string} apiKey
 * @param {object} [options]
 * @returns {Promise<void>}
 * @throws {ProviderError}
 */
export const validateKey = async (apiKey, options = {}) => {
  await requestJson(`${API_ORIGIN}/v1/models?limit=1`, {
    method: "GET",
    headers: headers(apiKey),
    signal: options.signal,
    fetchImpl: options.fetchImpl,
  });
};

/**
 * Extract checkable claims from a transcript batch.
 *
 * @param {object} request
 * @param {string} request.apiKey
 * @param {string} request.context
 * @param {string} request.transcript
 * @param {string} [request.model]
 * @param {AbortSignal} [request.signal]
 * @returns {Promise<{claims: object[], usage: import("./common.mjs").Usage}>}
 */
export const runGate = async ({apiKey, context, transcript, model = defaultModels.gate, ...options}) => {
  const prompt = buildGatePrompt(context, transcript);
  const response = await createMessage(
    apiKey,
    {
      model,
      max_tokens: GATE_MAX_TOKENS,
      system: cachedSystem(prompt.instructions),
      messages: [{role: "user", content: prompt.input}],
      // Extraction needs no deliberation, and thinking tokens would count
      // against max_tokens. Disabled thinking is accepted at effort "low".
      thinking: {type: "disabled"},
      output_config: {effort: "low", format: {type: "json_schema", schema: GATE_SCHEMA}},
    },
    options
  );
  if (response.stop_reason === "refusal") {
    throw new ProviderError(ERROR_KIND.REFUSED, "The model declined to process this transcript.");
  }
  if (response.stop_reason === "max_tokens") {
    throw new ProviderError(ERROR_KIND.INVALID_OUTPUT, "The claim list was cut off (max_tokens).");
  }
  return {
    claims: normalizeGateClaims(parseModelJson(finalText(response.content))),
    usage: addUsage(emptyUsage(), readUsage(response.usage)),
  };
};

/**
 * Fact-check one claim with web search.
 *
 * @param {object} request
 * @param {string} request.apiKey
 * @param {string} request.claim
 * @param {string} [request.model]
 * @param {number} [request.maxSearches]
 * @param {AbortSignal} [request.signal]
 * @returns {Promise<{verdict: import("../pipeline/verdict_rules.mjs").Verdict,
 *   usage: import("./common.mjs").Usage, mode: string}>}
 */
export const runVerify = async ({
  apiKey,
  claim,
  model = defaultModels.verify,
  maxSearches = BACKEND_DEFAULTS.anthropic_web_max_uses,
  ...options
}) => {
  const system = cachedSystem(
    buildSelfSearchVerifySystem({searchTool: "the web_search tool", maxUses: maxSearches})
  );
  let usage = emptyUsage();
  const content = [];
  let stopReason = null;
  for (let attempt = 0; attempt <= MAX_PAUSE_CONTINUATIONS; attempt += 1) {
    const messages = [{role: "user", content: claim}];
    if (content.length > 0) {
      // Resume a paused search turn by sending back what it produced so far.
      messages.push({role: "assistant", content: [...content]});
    }
    const response = await createMessage(
      apiKey,
      {
        model,
        max_tokens: VERIFY_MAX_TOKENS,
        system,
        messages,
        tools: [{type: WEB_SEARCH_TOOL_TYPE, name: "web_search", max_uses: maxSearches}],
        thinking: {type: "adaptive"},
        output_config: {
          effort: "low",
          format: {type: "json_schema", schema: VERDICT_SELF_SEARCH_SCHEMA},
        },
      },
      options
    );
    usage = addUsage(usage, readUsage(response.usage));
    content.push(...response.content);
    stopReason = response.stop_reason;
    if (stopReason !== "pause_turn") {
      break;
    }
  }
  if (stopReason === "refusal") {
    throw new ProviderError(ERROR_KIND.REFUSED, "The model declined to check this claim.");
  }
  if (stopReason === "pause_turn") {
    throw new ProviderError(ERROR_KIND.SERVER, "The search turn did not finish.");
  }
  if (stopReason === "max_tokens") {
    throw new ProviderError(ERROR_KIND.INVALID_OUTPUT, "The verdict was cut off (max_tokens).");
  }
  const payload = parseModelJson(finalText(content));
  const sources = selectSources({...collectSources(content), picked: payload.source_urls ?? []});
  return {verdict: finalizeVerdict(payload, sources), usage, mode: "single"};
};
