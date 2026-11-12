/**
 * OpenAI provider: the Responses API, called directly from the extension
 * with the viewer's own API key.
 *
 *   - gate: strict JSON via `text.format` (json_schema).
 *   - verify: the hosted `web_search` tool plus strict JSON in one call. The
 *     model searches itself, so it shares the self-search prompt with
 *     Anthropic. Sources come from the search calls' own source lists and
 *     from `url_citation` annotations — never from a URL the model only
 *     wrote into its JSON.
 *
 * `store: false` keeps requests out of OpenAI's stored-response history.
 */

import {GATE_SCHEMA, VERDICT_SELF_SEARCH_SCHEMA} from "../generated/prompts.mjs";
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

const API_ORIGIN = "https://api.openai.com";

// The prompt states a search cap; the API has no per-request cap of its own.
const STATED_MAX_SEARCHES = 2;

export const id = "openai";
export const label = "OpenAI";
export const keyUrl = "https://platform.openai.com/api-keys";
export const hostPermission = `${API_ORIGIN}/*`;
// The efficient tier, in the same class as the backend's Claude Haiku default.
export const defaultModels = Object.freeze({gate: "gpt-6-luna", verify: "gpt-6-luna"});

const headers = (apiKey) => ({Authorization: `Bearer ${apiKey}`});

/** A key with no credit answers 429 `insufficient_quota`, which is not a rate limit. */
const classifyError = (status, message, body) =>
  body?.error?.code === "insufficient_quota" || /exceeded your current quota/i.test(message)
    ? ERROR_KIND.BILLING
    : null;

const jsonFormat = (name, schema) => ({format: {type: "json_schema", name, strict: true, schema}});

/** @param {object} response @returns {Partial<import("./common.mjs").Usage>} */
const readUsage = (response) => {
  const usage = response.usage ?? {};
  const cached = usage.input_tokens_details?.cached_tokens ?? 0;
  return {
    inputTokens: (usage.input_tokens ?? 0) - cached,
    cachedInputTokens: cached,
    outputTokens: usage.output_tokens ?? 0,
    // Each search action the model ran is one billable tool call.
    webSearches: (response.output ?? []).filter(
      (item) => item.type === "web_search_call" && (item.action?.type ?? "search") === "search"
    ).length,
  };
};

/**
 * Read the answer text, raising for the non-answers the API reports in-band.
 *
 * @param {object} response
 * @returns {string}
 */
const readOutputText = (response) => {
  if (response.status === "incomplete") {
    const reason = response.incomplete_details?.reason ?? "unknown";
    throw new ProviderError(ERROR_KIND.INVALID_OUTPUT, `The response was cut off (${reason}).`);
  }
  const parts = [];
  for (const item of response.output ?? []) {
    if (item.type !== "message") {
      continue;
    }
    for (const part of item.content ?? []) {
      if (part.type === "refusal") {
        throw new ProviderError(ERROR_KIND.REFUSED, part.refusal || "The model declined.");
      }
      if (part.type === "output_text") {
        parts.push(part.text ?? "");
      }
    }
  }
  return parts.join("");
};

/** @param {object} response @returns {{retrieved: object[], cited: object[]}} */
const collectSources = (response) => {
  const retrieved = [];
  const cited = [];
  for (const item of response.output ?? []) {
    if (item.type === "web_search_call") {
      for (const source of item.action?.sources ?? []) {
        if (source.url) {
          retrieved.push({url: source.url, title: source.title ?? null});
        }
      }
    } else if (item.type === "message") {
      for (const part of item.content ?? []) {
        for (const annotation of part.annotations ?? []) {
          if (annotation.type === "url_citation" && annotation.url) {
            cited.push({url: annotation.url, title: annotation.title ?? null});
          }
        }
      }
    }
  }
  return {retrieved, cited};
};

const createResponse = (apiKey, body, options) =>
  requestJson(`${API_ORIGIN}/v1/responses`, {
    headers: headers(apiKey),
    body: {store: false, ...body},
    classifyError,
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
  await requestJson(`${API_ORIGIN}/v1/models`, {
    method: "GET",
    headers: headers(apiKey),
    classifyError,
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
  const response = await createResponse(
    apiKey,
    {
      model,
      input: [
        {role: "system", content: prompt.instructions},
        {role: "user", content: prompt.input},
      ],
      text: jsonFormat("gate_result", GATE_SCHEMA),
    },
    options
  );
  return {
    claims: normalizeGateClaims(parseModelJson(readOutputText(response))),
    usage: addUsage(emptyUsage(), readUsage(response)),
  };
};

/**
 * Fact-check one claim with web search.
 *
 * @param {object} request - see providers/anthropic.mjs runVerify
 * @returns {Promise<{verdict: object, usage: import("./common.mjs").Usage, mode: string}>}
 */
export const runVerify = async ({apiKey, claim, model = defaultModels.verify, ...options}) => {
  const response = await createResponse(
    apiKey,
    {
      model,
      input: [
        {
          role: "system",
          content: buildSelfSearchVerifySystem({
            searchTool: "the web_search tool",
            maxUses: STATED_MAX_SEARCHES,
          }),
        },
        {role: "user", content: claim},
      ],
      tools: [{type: "web_search", search_context_size: "low"}],
      // Without this the response lists only what the answer cited, and a
      // JSON answer cites nothing inline.
      include: ["web_search_call.action.sources"],
      text: jsonFormat("verdict", VERDICT_SELF_SEARCH_SCHEMA),
    },
    options
  );
  const payload = parseModelJson(readOutputText(response));
  const sources = selectSources({...collectSources(response), picked: payload.source_urls ?? []});
  return {
    verdict: finalizeVerdict(payload, sources),
    usage: addUsage(emptyUsage(), readUsage(response)),
    mode: "single",
  };
};
