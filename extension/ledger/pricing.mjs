/**
 * Price table for estimating what a provider call cost.
 *
 * Why a table shipped in the package: prices cannot be fetched at runtime
 * without a new host permission and a new data flow, and three of the four
 * providers do not report a cost with the response. OpenRouter does, and its
 * figure is always used as-is; this table is only consulted when a call
 * arrives without a cost.
 *
 * Every figure derived from this table is an ESTIMATE and is shown with "≈".
 * It ignores the 25 % surcharge on tokens written to the prompt cache (they
 * are counted as ordinary input), so it runs slightly low on the first call
 * after an idle period.
 *
 * To change a price: edit the entry, bump PRICING_VERSION to today's date.
 * Each usage row records the version that priced it.
 */

export const PRICING_VERSION = "2026-11-12";

/** Where a usage row's cost came from. */
export const COST_SOURCE = Object.freeze({
  PROVIDER: "provider", // exact, reported by the provider with the response
  ESTIMATE: "estimate", // token counts × this table
  FREE: "free", // runs on the viewer's own machine
  UNKNOWN: "unknown", // no reported cost and no price for this model
});

const PER_MILLION = 1_000_000;

/**
 * USD per million tokens, and per web search. `match` is tested against the
 * model id, first hit wins — dated ids such as "claude-haiku-5-5-20261001"
 * match their family entry.
 */
const PRICE_TABLE = Object.freeze({
  anthropic: {
    webSearchUsd: 0.01, // $10 per 1,000 searches
    models: [
      // Haiku 5.5 doubles in price above 100K prompt tokens; this pipeline's
      // prompts are a few thousand, so only the base tier is modelled.
      {match: /^claude-haiku-5-5/, input: 0.1, cachedInput: 0.01, output: 0.5},
      {match: /^claude-sonnet-5-5/, input: 2, cachedInput: 0.1, output: 10},
      {match: /^claude-opus-5-5/, input: 4, cachedInput: 0.2, output: 20},
      {match: /^claude-opus-5(?!-5)/, input: 5, cachedInput: 0.5, output: 25},
      {match: /^claude-fable-5-2/, input: 8, cachedInput: 0.2, output: 40},
    ],
  },
});

/** Providers that run on the viewer's machine and cost nothing per call. */
const FREE_PROVIDERS = new Set(["ollama"]);

/**
 * @param {string} provider
 * @param {string} model
 * @returns {{input: number, cachedInput: number, output: number, webSearchUsd: number}|null}
 */
export const findPrice = (provider, model) => {
  const entry = PRICE_TABLE[provider];
  const price = entry?.models.find((candidate) => candidate.match.test(model ?? ""));
  return price ? {...price, webSearchUsd: entry.webSearchUsd} : null;
};

/**
 * Work out what one provider call cost.
 *
 * @param {object} call
 * @param {string} call.provider
 * @param {string} call.model
 * @param {number} [call.inputTokens] - input billed at the full rate
 * @param {number} [call.cachedInputTokens] - input read from the prompt cache
 * @param {number} [call.outputTokens]
 * @param {number} [call.webSearches]
 * @param {number|null} [call.reportedCostUsd] - the provider's own figure
 * @returns {{costUsd: number|null, costSource: string, pricingVersion: string|null}}
 */
export const priceCall = ({
  provider,
  model,
  inputTokens = 0,
  cachedInputTokens = 0,
  outputTokens = 0,
  webSearches = 0,
  reportedCostUsd = null,
}) => {
  if (typeof reportedCostUsd === "number" && Number.isFinite(reportedCostUsd)) {
    return {costUsd: reportedCostUsd, costSource: COST_SOURCE.PROVIDER, pricingVersion: null};
  }
  if (FREE_PROVIDERS.has(provider)) {
    return {costUsd: 0, costSource: COST_SOURCE.FREE, pricingVersion: null};
  }
  const price = findPrice(provider, model);
  if (!price) {
    return {costUsd: null, costSource: COST_SOURCE.UNKNOWN, pricingVersion: null};
  }
  const tokenCost =
    (inputTokens * price.input +
      cachedInputTokens * price.cachedInput +
      outputTokens * price.output) /
    PER_MILLION;
  return {
    costUsd: tokenCost + webSearches * price.webSearchUsd,
    costSource: COST_SOURCE.ESTIMATE,
    pricingVersion: PRICING_VERSION,
  };
};
