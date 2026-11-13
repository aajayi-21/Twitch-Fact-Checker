/**
 * A stand-in "provider" that answers from a recording, so the demo can show
 * the whole pipeline with no API key and no network call.
 *
 * It has the same surface as the real provider modules (providers/index.mjs),
 * so the demo runs the REAL BrowserPipeline — batching, thresholds, duplicate
 * suppression, the verify queue — and only the two model calls are replayed:
 *
 *   runGate    returns each recorded claim once its tell-tale words appear
 *              in the text transcribed on this machine;
 *   runVerify  returns the verdict that was recorded for that claim.
 *
 * Pure apart from the injectable delay: unit-tested under Node.
 */

import {ERROR_KIND, ProviderError, emptyUsage} from "../providers/common.mjs";

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * @param {object} recording - see demo/recording.mjs
 * @param {object} [options]
 * @param {number} [options.gateDelayMs] - a model call is not instant; a
 *   short wait keeps the demo's rhythm honest
 * @param {number} [options.verifyDelayMs]
 * @param {(ms: number) => Promise<void>} [options.wait]
 * @returns {object} a provider module look-alike, good for one demo run
 */
export const createDemoProvider = (
  recording,
  {gateDelayMs = 300, verifyDelayMs = 900, wait = pause} = {}
) => {
  const said = new Set();
  return {
    id: recording.provider,
    label: "Recorded example",
    defaultModels: {gate: recording.model, verify: recording.model},
    validateKey: async () => {},

    runGate: async ({context = "", transcript}) => {
      await wait(gateDelayMs);
      // A claim can straddle two batches, so the context tail counts too.
      const heard = `${context} ${transcript}`.toLowerCase();
      const claims = recording.claims
        .filter((claim) => !said.has(claim.claim_text) && new RegExp(claim.heardWhen).test(heard))
        .map((claim) => {
          said.add(claim.claim_text);
          return {
            claim_text: claim.claim_text,
            check_worthiness: claim.check_worthiness,
            topic: claim.topic,
          };
        });
      return {claims, usage: emptyUsage()};
    },

    runVerify: async ({claim}) => {
      await wait(verifyDelayMs);
      const recorded = recording.claims.find((candidate) => candidate.claim_text === claim);
      if (!recorded?.verdict) {
        throw new ProviderError(
          ERROR_KIND.INVALID_OUTPUT,
          "The recorded example has no answer for this claim."
        );
      }
      return {
        verdict: {
          ...recorded.verdict,
          sources: recorded.verdict.sources.map((source) => ({...source})),
        },
        usage: emptyUsage(),
        mode: "single",
      };
    },
  };
};
