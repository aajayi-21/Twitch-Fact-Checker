/**
 * OpenRouter provider: one key, many models, called directly from the
 * extension with the viewer's own API key.
 *
 * Mirrors backend/app/llm_openrouter.py:
 *   - gate: strict `json_schema` response format, with the response-healing
 *     plugin to repair near-valid JSON.
 *   - verify: the `web` plugin searches on the USER message before the model
 *     runs and attaches the results, so the system prompt never says
 *     "search" and the user message is the bare claim. Citations come back
 *     as `url_citation` annotations — the only place sources are read from.
 *
 * OpenRouter reports the exact cost of every request (`usage.cost`), which
 * is what makes the usage screen precise for this provider.
 *
 * Not ported yet: the backend consults OpenRouter's model catalogue to learn
 * which models support strict JSON and falls back to `json_object` for the
 * rest (openrouter_catalogue.py). Here `require_parameters` makes an
 * unsupported model fail loudly instead.
 */

import {
  BACKEND_DEFAULTS,
  GATE_SCHEMA,
  OPENROUTER_WEB_SEARCH_PROMPT,
  VERDICT_ATTACHED_RESULTS_SCHEMA,
} from "../generated/prompts.mjs";
import {buildAttachedResultsVerifySystem, buildGatePrompt} from "../pipeline/prompt_builders.mjs";
import {finalizeVerdict, normalizeGateClaims, selectSources} from "../pipeline/verdict_rules.mjs";
import {ERROR_KIND, ProviderError, addUsage, emptyUsage, parseModelJson, requestJson} from "./common.mjs";

const API_ORIGIN = "https://openrouter.ai";
const APP_TITLE = "Live Stream Fact-Checker";

export const id = "openrouter";
export const label = "OpenRouter";
export const keyUrl = "https://openrouter.ai/keys";
export const hostPermission = `${API_ORIGIN}/*`;
// Not the backend's default: that model (inception/mercury-2.5-preview) has
// been withdrawn from OpenRouter and returns 404. Of the three families
// measured live (docs/spike-results/provider-calls.md) this was the fastest
// at both stages and got every test claim right; the per-check cost is the
// search fee either way. Viewers can change it under AI provider.
const DEFAULT_MODEL = "google/gemini-3.5-flash-lite";
export const defaultModels = Object.freeze({gate: DEFAULT_MODEL, verify: DEFAULT_MODEL});

const headers = (apiKey) => ({Authorization: `Bearer ${apiKey}`, "X-Title": APP_TITLE});

const strictFormat = (name, schema) => ({
  type: "json_schema",
  json_schema: {name, strict: true, schema},
});

/**
 * @param {object} completion
 * @param {number} assumedSearches - the web plugin runs one search per call
 * @returns {Partial<import("./common.mjs").Usage>}
 */
const readUsage = (completion, assumedSearches) => {
  const usage = completion.usage ?? {};
  const cached = usage.prompt_tokens_details?.cached_tokens ?? 0;
  return {
    inputTokens: (usage.prompt_tokens ?? 0) - cached,
    cachedInputTokens: cached,
    outputTokens: usage.completion_tokens ?? 0,
    webSearches: usage.server_tool_use?.web_search_requests ?? assumedSearches,
    costUsd: typeof usage.cost === "number" ? usage.cost : null,
  };
};

/** @param {object} completion @returns {object} the assistant message */
const readMessage = (completion) => {
  // OpenRouter can report an upstream failure inside a 200 response.
  if (completion.error) {
    throw new ProviderError(
      ERROR_KIND.SERVER,
      completion.error.message ?? "The upstream model failed."
    );
  }
  const choice = completion.choices?.[0];
  if (!choice?.message) {
    throw new ProviderError(ERROR_KIND.INVALID_OUTPUT, "The response had no message.");
  }
  if (choice.message.refusal) {
    throw new ProviderError(ERROR_KIND.REFUSED, choice.message.refusal);
  }
  if (choice.finish_reason === "length") {
    throw new ProviderError(ERROR_KIND.INVALID_OUTPUT, "The response was cut off (length).");
  }
  return choice.message;
};

const createCompletion = (apiKey, body, options) =>
  requestJson(`${API_ORIGIN}/api/v1/chat/completions`, {
    headers: headers(apiKey),
    // Only route to an upstream that honours every parameter sent — above
    // all the strict response format.
    body: {provider: {require_parameters: true}, usage: {include: true}, ...body},
    signal: options.signal,
    fetchImpl: options.fetchImpl,
  });

/**
 * Check a key with a free call.
 *
 * @param {string} apiKey
 * @param {object} [options]
 * @returns {Promise<void>}
 * @throws {ProviderError}
 */
export const validateKey = async (apiKey, options = {}) => {
  await requestJson(`${API_ORIGIN}/api/v1/key`, {
    method: "GET",
    headers: headers(apiKey),
    signal: options.signal,
    fetchImpl: options.fetchImpl,
  });
};

/**
 * Extract checkable claims from a transcript batch.
 *
 * @param {object} request - see providers/anthropic.mjs runGate
 * @returns {Promise<{claims: object[], usage: import("./common.mjs").Usage}>}
 */
export const runGate = async ({apiKey, context, transcript, model = defaultModels.gate, ...options}) => {
  const prompt = buildGatePrompt(context, transcript);
  const completion = await createCompletion(
    apiKey,
    {
      model,
      messages: [
        {role: "system", content: prompt.instructions},
        {role: "user", content: prompt.input},
      ],
      response_format: strictFormat("gate_result", GATE_SCHEMA),
      plugins: [{id: "response-healing"}],
    },
    options
  );
  return {
    claims: normalizeGateClaims(parseModelJson(readMessage(completion).content)),
    usage: addUsage(emptyUsage(), readUsage(completion, 0)),
  };
};

/**
 * Fact-check one claim with the web plugin.
 *
 * @param {object} request - see providers/anthropic.mjs runVerify
 * @returns {Promise<{verdict: object, usage: import("./common.mjs").Usage, mode: string}>}
 */
export const runVerify = async ({apiKey, claim, model = defaultModels.verify, ...options}) => {
  const webPlugin = {
    id: "web",
    max_results: BACKEND_DEFAULTS.openrouter_web_max_results,
    search_prompt: OPENROUTER_WEB_SEARCH_PROMPT,
  };
  // "auto" leaves the engine to OpenRouter (native search where a model has it).
  if (BACKEND_DEFAULTS.openrouter_web_engine !== "auto") {
    webPlugin.engine = BACKEND_DEFAULTS.openrouter_web_engine;
  }
  const completion = await createCompletion(
    apiKey,
    {
      model,
      messages: [
        {role: "system", content: buildAttachedResultsVerifySystem()},
        // The bare claim: the web plugin uses this message as its search query.
        {role: "user", content: claim},
      ],
      response_format: strictFormat("verdict", VERDICT_ATTACHED_RESULTS_SCHEMA),
      plugins: [webPlugin, {id: "response-healing"}],
    },
    options
  );
  const message = readMessage(completion);
  const cited = (message.annotations ?? [])
    .filter((annotation) => annotation.type === "url_citation" && annotation.url_citation?.url)
    .map((annotation) => ({
      url: annotation.url_citation.url,
      title: annotation.url_citation.title ?? null,
    }));
  return {
    verdict: finalizeVerdict(parseModelJson(message.content), selectSources({cited})),
    usage: addUsage(emptyUsage(), readUsage(completion, 1)),
    mode: "single",
  };
};
