/**
 * How the browser engine's providers are presented, and what to say when a
 * provider call fails. Pure, so the wording is unit-tested under Node.
 */

import {ERROR_KIND} from "../../engine/providers/common.mjs";
import {PROVIDERS} from "../../engine/providers/index.mjs";

/**
 * The four providers, in the order they are offered.
 *
 * `verifiedLive` is false for the two whose modules follow the published
 * API but have not yet made a real call from this extension — said plainly
 * on their cards rather than discovered on a stream.
 *
 * `needsPermission`: the browser lets the extension call the other three
 * without asking; Google's API has to be granted as a site permission first.
 */
export const BROWSER_PROVIDERS = Object.freeze(
  [
    {
      id: "openrouter",
      blurb: "One key for many models. Reports the exact cost of every check.",
      keyPlaceholder: "sk-or-…",
      verifiedLive: true,
      needsPermission: false,
    },
    {
      id: "anthropic",
      blurb: "Claude, direct from Anthropic.",
      keyPlaceholder: "sk-ant-…",
      verifiedLive: true,
      needsPermission: false,
    },
    {
      id: "openai",
      blurb: "GPT, direct from OpenAI.",
      keyPlaceholder: "sk-…",
      verifiedLive: false,
      needsPermission: false,
    },
    {
      id: "gemini",
      blurb: "Gemini, direct from Google.",
      keyPlaceholder: "AIza…",
      verifiedLive: false,
      needsPermission: true,
    },
  ].map((entry) =>
    Object.freeze({
      ...entry,
      label: PROVIDERS[entry.id].label,
      keyUrl: PROVIDERS[entry.id].keyUrl,
      hostPermission: PROVIDERS[entry.id].hostPermission,
      defaultModels: PROVIDERS[entry.id].defaultModels,
    })
  )
);

/** @param {string} providerId @returns {object|undefined} */
export const browserProvider = (providerId) =>
  BROWSER_PROVIDERS.find((provider) => provider.id === providerId);

/** Thrown when the viewer declines the site permission a provider needs. */
export class PermissionDeclined extends Error {
  constructor(providerLabel) {
    super(`Permission to reach ${providerLabel} was not granted.`);
    this.name = "PermissionDeclined";
  }
}

/**
 * What to tell the viewer when checking or using a key fails.
 *
 * @param {unknown} error - a ProviderError, PermissionDeclined, or anything
 * @param {string} providerLabel
 * @returns {string}
 */
export const describeProviderError = (error, providerLabel) => {
  if (error instanceof PermissionDeclined) {
    return `${providerLabel} can only be used once you allow the extension to reach it. Try again and choose Allow.`;
  }
  switch (error?.kind) {
    case ERROR_KIND.AUTH:
      return `${providerLabel} did not accept that key. Check it was copied in full and try again.`;
    case ERROR_KIND.BILLING:
      return `${providerLabel} accepted the key, but the account has no credit or billing set up.`;
    case ERROR_KIND.RATE_LIMIT:
      return `${providerLabel} is rate-limiting this key right now. Wait a moment and try again.`;
    case ERROR_KIND.NETWORK:
      return `${providerLabel} could not be reached. Check your connection and try again.`;
    case ERROR_KIND.TIMEOUT:
      return `${providerLabel} took too long to answer. Try again in a moment.`;
    case ERROR_KIND.BAD_REQUEST:
      return `${providerLabel} rejected the request: ${error.message}`;
    case ERROR_KIND.SERVER:
      return `${providerLabel} is having problems right now. Try again later.`;
    case ERROR_KIND.REFUSED:
      return `${providerLabel}'s model declined to answer.`;
    case ERROR_KIND.INVALID_OUTPUT:
      return `${providerLabel} answered, but not in a usable form: ${error.message}`;
    default:
      return String(error?.message ?? error ?? "Something went wrong.");
  }
};
