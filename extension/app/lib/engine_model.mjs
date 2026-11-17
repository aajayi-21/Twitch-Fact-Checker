/**
 * How the browser engine's providers are presented, and what to say when a
 * provider call fails. Pure, so the wording is unit-tested under Node.
 */

import {ERROR_KIND} from "../../engine/providers/common.mjs";
import {PROVIDERS} from "../../engine/providers/index.mjs";
import {canPrice} from "../../ledger/pricing.mjs";

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
    case ERROR_KIND.MODEL_UNAVAILABLE:
      return `${providerLabel} cannot run that model: ${error.message}. Check the model name, or leave it empty for the default.`;
    default:
      return String(error?.message ?? error ?? "Something went wrong.");
  }
};

/**
 * What to do with a model name typed for a stage, given the provider's
 * answer about it (providers/openrouter.mjs checkModel).
 *
 * @param {string} model
 * @param {{exists: boolean, strictJson: boolean}|null} check - null: the
 *   provider could not be asked
 * @returns {{save: boolean, tone: "warn"|"error"|null, message: string|null}}
 */
export const judgeModelCheck = (model, check) => {
  if (check === null) {
    return {
      save: true,
      tone: "warn",
      message: `Could not check "${model}" with OpenRouter just now, so it was saved unchecked.`,
    };
  }
  if (!check.exists) {
    return {
      save: false,
      tone: "error",
      message: `OpenRouter has no model called "${model}". Copy the name from openrouter.ai/models, or leave the field empty for the default. Nothing was changed.`,
    };
  }
  if (!check.strictJson) {
    return {
      save: true,
      tone: "warn",
      message: `Saved, but "${model}" does not offer strict JSON output on OpenRouter, which this app needs: checks with it will fail. Choose another model.`,
    };
  }
  return {save: true, tone: null, message: null};
};

/**
 * The models in a plan whose calls this app cannot price, and which the
 * monthly spending limit therefore cannot count.
 *
 * @param {{gate: {providerId: string, model: string}|null,
 *   verify: {providerId: string, model: string}|null}} plan
 * @returns {string[]} each model once, in stage order
 */
export const unpricedModels = (plan) => {
  const models = [];
  for (const stage of [plan?.gate, plan?.verify]) {
    if (stage && !canPrice(stage.providerId, stage.model) && !models.includes(stage.model)) {
      models.push(stage.model);
    }
  }
  return models;
};

/**
 * The warning shown wherever the monthly limit is, when it cannot count
 * every model in use; null when it can (or there is no monthly limit).
 *
 * @param {object} plan - see unpricedModels
 * @param {{dailyChecks: number, monthlyUsd: number}} budgets
 * @returns {string|null}
 */
export const unpricedWarning = (plan, budgets) => {
  const models = unpricedModels(plan);
  if (models.length === 0 || !(budgets?.monthlyUsd > 0)) {
    return null;
  }
  const names = models.join(" and ");
  const daily =
    budgets.dailyChecks > 0
      ? ` The daily limit of ${budgets.dailyChecks} fact-checks still applies.`
      : " With no daily limit either, nothing caps what these calls cost.";
  return `The monthly limit cannot count ${names}: this app does not know ${models.length === 1 ? "its" : "their"} price.${daily}`;
};
