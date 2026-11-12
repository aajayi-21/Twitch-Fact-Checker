/**
 * Provider registry. Every provider module exports the same surface:
 *
 *   id, label, keyUrl, hostPermission, defaultModels: {gate, verify}
 *   validateKey(apiKey, options?)            -> Promise<void>
 *   runGate({apiKey, context, transcript, model?, signal?})
 *                                            -> Promise<{claims, usage}>
 *   runVerify({apiKey, claim, model?, signal?})
 *                                            -> Promise<{verdict, usage, mode}>
 *
 * and throws ProviderError (providers/common.mjs) with a `kind` the UI can
 * act on. Keys are passed per call and never stored by these modules.
 */

import * as anthropic from "./anthropic.mjs";
import * as gemini from "./gemini.mjs";
import * as openai from "./openai.mjs";
import * as openrouter from "./openrouter.mjs";

export const PROVIDERS = Object.freeze({
  [openrouter.id]: openrouter,
  [anthropic.id]: anthropic,
  [openai.id]: openai,
  [gemini.id]: gemini,
});

/**
 * @param {string} providerId
 * @returns {typeof anthropic} a provider module
 * @throws {Error} on an unknown id
 */
export const getProvider = (providerId) => {
  const provider = PROVIDERS[providerId];
  if (!provider) {
    throw new Error(
      `Unknown provider "${providerId}". Known providers: ${Object.keys(PROVIDERS).join(", ")}.`
    );
  }
  return provider;
};
