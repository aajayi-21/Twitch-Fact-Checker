/**
 * Gemini provider: Google's Interactions API, called directly from the
 * extension with the viewer's own API key.
 *
 *   - gate: structured JSON output (`response_format` with a schema).
 *   - verify: the `google_search` tool. The model searches itself, so it
 *     shares the self-search prompt with Anthropic and OpenAI.
 *
 * Verify has two modes, because combining search grounding with schema-
 * enforced JSON is a preview feature that developers report as unreliable
 * (empty citation metadata, truncated JSON):
 *
 *   "strict"    one call: google_search + `response_format`.
 *   "prompted"  one call: google_search, JSON asked for by the prompt only;
 *               if the answer is not parseable JSON, a second, ungrounded
 *               call reshapes it into the schema.
 *   "auto"      strict first; if it yields unusable JSON or no sources, fall
 *               back to prompted. This is the default.
 *
 * The result's `mode` says which path produced the verdict, so the cost of
 * the fallback is visible.
 *
 * The key goes in the `x-goog-api-key` header, never in a `?key=` query
 * parameter. `store: false` keeps interactions out of Google's stored history.
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

const API_ORIGIN = "https://generativelanguage.googleapis.com";
const API_REVISION = "2026-05-20";

// The prompt states a search cap; the API has no per-request cap of its own.
const STATED_MAX_SEARCHES = 2;

export const id = "gemini";
export const label = "Gemini";
export const keyUrl = "https://aistudio.google.com/apikey";
export const hostPermission = `${API_ORIGIN}/*`;
// The Flash-Lite tier, in the same class as the backend's Claude Haiku default.
export const defaultModels = Object.freeze({
  gate: "gemini-3.5-flash-lite",
  verify: "gemini-3.5-flash-lite",
});

const headers = (apiKey) => ({"x-goog-api-key": apiKey, "Api-Revision": API_REVISION});

/**
 * Google reports a bad key as 400, and billing problems (including "search
 * grounding is not available on the free tier") in the message text.
 */
const classifyError = (status, message) => {
  if (/api key not valid|api_key_invalid|permission denied/i.test(message)) {
    return ERROR_KIND.AUTH;
  }
  if (/billing|free tier|free_tier|payment/i.test(message)) {
    return ERROR_KIND.BILLING;
  }
  return null;
};

/**
 * Gemini's schema dialect is narrower than JSON Schema; `additionalProperties`
 * is the keyword it has historically rejected, and it adds nothing here
 * because the output is validated again in verdict_rules.mjs.
 *
 * @param {unknown} schema
 * @returns {unknown} a deep copy without `additionalProperties`
 */
export const toGeminiSchema = (schema) => {
  if (Array.isArray(schema)) {
    return schema.map(toGeminiSchema);
  }
  if (typeof schema !== "object" || schema === null) {
    return schema;
  }
  return Object.fromEntries(
    Object.entries(schema)
      .filter(([key]) => key !== "additionalProperties")
      .map(([key, value]) => [key, toGeminiSchema(value)])
  );
};

const jsonFormat = (schema) => ({
  type: "text",
  mime_type: "application/json",
  schema: toGeminiSchema(schema),
});

/** @param {object} interaction @returns {Partial<import("./common.mjs").Usage>} */
const readUsage = (interaction) => {
  const usage = interaction.usage ?? {};
  const cached = usage.total_cached_tokens ?? 0;
  const counted = (usage.grounding_tool_count ?? [])
    .filter((entry) => entry.type === "google_search")
    .reduce((sum, entry) => sum + (entry.count ?? 0), 0);
  // Billing is per search QUERY, so fall back to counting the queries issued.
  const queries = (interaction.steps ?? [])
    .filter((step) => step.type === "google_search_call")
    .reduce((sum, step) => sum + (step.queries?.length ?? step.arguments?.queries?.length ?? 1), 0);
  return {
    inputTokens: (usage.total_input_tokens ?? 0) - cached,
    cachedInputTokens: cached,
    // Thinking is billed as output.
    outputTokens: (usage.total_output_tokens ?? 0) + (usage.total_thought_tokens ?? 0),
    webSearches: counted || queries,
  };
};

/** @param {object} interaction @returns {string} */
const readOutputText = (interaction) => {
  if (interaction.status && interaction.status !== "completed") {
    throw new ProviderError(
      ERROR_KIND.INVALID_OUTPUT,
      `The interaction ended with status "${interaction.status}".`
    );
  }
  const parts = [];
  for (const step of interaction.steps ?? []) {
    if (step.type !== "model_output") {
      continue;
    }
    for (const block of step.content ?? []) {
      if (block.type === "text") {
        parts.push(block.text ?? "");
      }
    }
  }
  return parts.join("");
};

/** @param {object} interaction @returns {{retrieved: object[], cited: object[]}} */
const collectSources = (interaction) => {
  const retrieved = [];
  const cited = [];
  for (const step of interaction.steps ?? []) {
    if (step.type === "google_search_result") {
      for (const result of step.results ?? step.content ?? []) {
        if (typeof result?.url === "string" && result.url) {
          retrieved.push({url: result.url, title: result.title ?? null});
        }
      }
    } else if (step.type === "model_output") {
      for (const block of step.content ?? []) {
        for (const annotation of block.annotations ?? []) {
          if (annotation.type === "url_citation" && annotation.url) {
            cited.push({url: annotation.url, title: annotation.title ?? null});
          }
        }
      }
    }
  }
  return {retrieved, cited};
};

const createInteraction = (apiKey, body, options) =>
  requestJson(`${API_ORIGIN}/v1beta/interactions`, {
    headers: headers(apiKey),
    body: {store: false, ...body},
    classifyError,
    signal: options.signal,
    fetchImpl: options.fetchImpl,
  });

/**
 * Check a key with a free call. Note that a free-tier key passes this and
 * can run the gate, but cannot use search grounding — verify will fail with
 * a billing error.
 *
 * @param {string} apiKey
 * @param {object} [options]
 * @returns {Promise<void>}
 * @throws {ProviderError}
 */
export const validateKey = async (apiKey, options = {}) => {
  await requestJson(`${API_ORIGIN}/v1beta/models?pageSize=1`, {
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
  const interaction = await createInteraction(
    apiKey,
    {
      model,
      system_instruction: prompt.instructions,
      input: prompt.input,
      response_format: jsonFormat(GATE_SCHEMA),
      generation_config: {thinking_level: "minimal"},
    },
    options
  );
  return {
    claims: normalizeGateClaims(parseModelJson(readOutputText(interaction))),
    usage: addUsage(emptyUsage(), readUsage(interaction)),
  };
};

/**
 * One grounded call. With `strict`, the schema is enforced by the API.
 *
 * @returns {Promise<{payload: object|null, sources: object[], usage: object, text: string}>}
 *   `payload` is null when the text is not parseable JSON
 */
const groundedCall = async ({apiKey, claim, model, strict}, options) => {
  const interaction = await createInteraction(
    apiKey,
    {
      model,
      system_instruction: buildSelfSearchVerifySystem({
        searchTool: "Google Search",
        maxUses: STATED_MAX_SEARCHES,
      }),
      input: claim,
      tools: [{type: "google_search"}],
      ...(strict ? {response_format: jsonFormat(VERDICT_SELF_SEARCH_SCHEMA)} : {}),
      generation_config: {thinking_level: "low"},
    },
    options
  );
  const text = readOutputText(interaction);
  let payload = null;
  try {
    payload = parseModelJson(text);
  } catch (error) {
    if (!(error instanceof ProviderError)) {
      throw error;
    }
  }
  const sources = selectSources({
    ...collectSources(interaction),
    picked: payload?.source_urls ?? [],
  });
  return {payload, sources, usage: readUsage(interaction), text};
};

/** Reshape a free-text verdict into the schema; no search, so no new facts. */
const repairCall = async ({apiKey, model, text}, options) => {
  const interaction = await createInteraction(
    apiKey,
    {
      model,
      system_instruction:
        "Convert the fact-check below into the required JSON object. Copy its " +
        "label, evidence rating and explanation faithfully; do not add facts.",
      input: text,
      response_format: jsonFormat(VERDICT_SELF_SEARCH_SCHEMA),
      generation_config: {thinking_level: "minimal"},
    },
    options
  );
  return {payload: parseModelJson(readOutputText(interaction)), usage: readUsage(interaction)};
};

/**
 * Fact-check one claim with Google Search grounding.
 *
 * @param {object} request - see providers/anthropic.mjs runVerify, plus:
 * @param {"auto"|"strict"|"prompted"} [request.verifyMode]
 * @returns {Promise<{verdict: object, usage: import("./common.mjs").Usage, mode: string}>}
 */
export const runVerify = async ({
  apiKey,
  claim,
  model = defaultModels.verify,
  verifyMode = "auto",
  ...options
}) => {
  let usage = emptyUsage();
  if (verifyMode !== "prompted") {
    const strict = await groundedCall({apiKey, claim, model, strict: true}, options);
    usage = addUsage(usage, strict.usage);
    const usable = strict.payload !== null && strict.sources.length > 0;
    if (usable || verifyMode === "strict") {
      if (strict.payload === null) {
        throw new ProviderError(
          ERROR_KIND.INVALID_OUTPUT,
          `Gemini returned unusable JSON with search grounding (got: ${strict.text.slice(0, 120)}).`
        );
      }
      return {verdict: finalizeVerdict(strict.payload, strict.sources), usage, mode: "strict"};
    }
  }

  const prompted = await groundedCall({apiKey, claim, model, strict: false}, options);
  usage = addUsage(usage, prompted.usage);
  if (prompted.payload !== null) {
    return {
      verdict: finalizeVerdict(prompted.payload, prompted.sources),
      usage,
      mode: verifyMode === "auto" ? "prompted-after-strict" : "prompted",
    };
  }
  const repaired = await repairCall({apiKey, model, text: prompted.text}, options);
  usage = addUsage(usage, repaired.usage);
  return {
    verdict: finalizeVerdict(repaired.payload, prompted.sources),
    usage,
    mode: verifyMode === "auto" ? "repaired-after-strict" : "repaired",
  };
};
