/**
 * Prompt builders for the in-extension pipeline.
 *
 * All wording comes from engine/generated/prompts.mjs, which is rendered
 * from the backend's Python prompts — never edit prompt text here. This
 * module only fills in the per-request values.
 *
 * Two verify shapes exist because providers search in two different ways
 * (see the notes in backend/app/prompts.py):
 *
 *   self-search       the model calls a search tool itself (Anthropic,
 *                     OpenAI, Gemini) — the prompt tells it to search and
 *                     caps how often.
 *   attached-results  the platform searches first and attaches the results
 *                     (OpenRouter's web plugin) — the prompt never says
 *                     "search", and the user message must be the bare claim
 *                     because it doubles as the search query.
 */

import {
  GATE_INPUT_TEMPLATE,
  GATE_INSTRUCTIONS,
  VERIFY_ATTACHED_RESULTS_SYSTEM_TEMPLATE,
  VERIFY_SELF_SEARCH_SYSTEM_TEMPLATE,
} from "../generated/prompts.mjs";

/** @returns {string} today's date as YYYY-MM-DD (UTC), like the backend */
export const todayIso = () => new Date().toISOString().slice(0, 10);

/**
 * The claim-gate prompt, split for prompt caching: `instructions` is
 * identical on every pass (send it as a cacheable system block) and `input`
 * is the transcript batch.
 *
 * @param {string} context - already-processed transcript, for reference resolution
 * @param {string} newTranscript - the fresh text to extract claims from
 * @returns {{instructions: string, input: string}}
 */
export const buildGatePrompt = (context, newTranscript) => ({
  instructions: GATE_INSTRUCTIONS,
  input: GATE_INPUT_TEMPLATE.replace("{context}", context.trim() || "(none)").replace(
    "{new_transcript}",
    newTranscript.trim()
  ),
});

/**
 * System prompt for providers whose model runs the search itself.
 *
 * @param {object} options
 * @param {string} options.searchTool - how to name the tool in the prompt,
 *   e.g. "the web_search tool" or "Google Search"
 * @param {number} options.maxUses - the search cap stated to the model
 * @param {string} [options.date]
 * @returns {string}
 */
export const buildSelfSearchVerifySystem = ({searchTool, maxUses, date = todayIso()}) =>
  VERIFY_SELF_SEARCH_SYSTEM_TEMPLATE.replace("{date}", date)
    .replace("{max_uses}", String(maxUses))
    .replace("{search_tool}", searchTool);

/**
 * System prompt for providers that attach search results to the request.
 *
 * @param {object} [options]
 * @param {string} [options.date]
 * @returns {string}
 */
export const buildAttachedResultsVerifySystem = ({date = todayIso()} = {}) =>
  VERIFY_ATTACHED_RESULTS_SYSTEM_TEMPLATE.replace("{date}", date);
