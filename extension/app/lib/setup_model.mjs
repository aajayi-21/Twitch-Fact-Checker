/**
 * Turns the backend's /setup/status into what the screens show: whether a
 * provider is connected and how, a one-line summary, and the stage-routing
 * form with its "has anything changed?" check.
 *
 * Pure (no DOM, no network), so the wording and the rules are unit-tested.
 */

/** Providers a viewer connects with an API key in the app. */
export const KEY_PROVIDERS = Object.freeze([
  Object.freeze({
    id: "anthropic",
    label: "Anthropic",
    blurb: "Claude Haiku with Anthropic's own web search. The backend's default.",
    keyUrl: "https://platform.claude.com/settings/keys",
    keyPlaceholder: "sk-ant-…",
  }),
  Object.freeze({
    id: "openrouter",
    label: "OpenRouter",
    blurb: "One key for many models. Needs a few dollars of credit for web search.",
    keyUrl: "https://openrouter.ai/keys",
    keyPlaceholder: "sk-or-…",
  }),
]);

export const PROVIDER_LABELS = Object.freeze({
  anthropic: "Anthropic",
  openrouter: "OpenRouter",
  ollama: "Ollama",
});

/** Providers whose stage model can be set from the app (Ollama's is .env-only). */
const MODEL_PROVIDERS = new Set(["anthropic", "openrouter"]);

/**
 * Jev ids. Jev answers yes/no decisions, not chat completions, so it can
 * never be a gate or verify model — it is selected as the pre-screen instead.
 * Mirrors the backend's JEV_FAMILY_RE.
 */
const JEV_SLUG_RE = /^~?typesafe\/jev-/i;

const formatCredits = (credits) =>
  credits && Number.isFinite(credits.total) && Number.isFinite(credits.usage)
    ? `$${(credits.total - credits.usage).toFixed(2)} credit left`
    : null;

/**
 * @typedef {object} ProviderConnection
 * @property {"connected"|"missing"|"unreachable"} state
 * @property {string} detail - short description of the state
 * @property {"api_key"|"sign_in"|null} method
 */

/**
 * @param {object|null} status - /setup/status body
 * @param {string} providerId
 * @returns {ProviderConnection}
 */
export const describeConnection = (status, providerId) => {
  const info = status?.providers?.[providerId] ?? {};
  if (providerId === "ollama") {
    return info.reachable
      ? {state: "connected", detail: `Running at ${info.base_url ?? "its default address"}`, method: null}
      : {state: "unreachable", detail: "Not reachable — is `ollama serve` running?", method: null};
  }
  if (!info.configured) {
    return {state: "missing", detail: "No key added", method: null};
  }
  if (providerId === "anthropic" && info.auth_method === "sign_in") {
    return {
      state: "connected",
      detail: "Signed in on this computer with `ant auth login`",
      method: "sign_in",
    };
  }
  const parts = [`Key ending ${info.key_hint ?? "…"}`];
  const credits = formatCredits(info.credits);
  if (credits) {
    parts.push(credits);
  }
  return {state: "connected", detail: parts.join(" · "), method: "api_key"};
};

/**
 * One line for the home screen: which provider and model run each stage.
 *
 * @param {object|null} status
 * @returns {string|null} null when nothing is configured
 */
export const describeStages = (status) => {
  if (!status?.configured) {
    return null;
  }
  const part = (stage) => {
    const provider = PROVIDER_LABELS[stage?.provider] ?? stage?.provider ?? "?";
    return stage?.model ? `${provider} (${stage.model})` : provider;
  };
  const gate = part(status.gate);
  const verify = part(status.verify);
  return gate === verify ? gate : `${gate} finds claims · ${verify} checks them`;
};

/**
 * The stored model of `providerId` for a stage. Read from
 * providers.<id>, not stage.model: stage.model reports the ACTIVE provider's
 * model, so it could not prefill the field when the provider is switched.
 */
const storedModel = (status, stage, providerId) =>
  status?.providers?.[providerId]?.[`${stage}_model`] ?? "";

const storedJevMode = (status) => status?.providers?.openrouter?.jev_mode ?? "off";

/**
 * @typedef {object} StageForm
 * @property {string} gateProvider
 * @property {string} verifyProvider
 * @property {string} gateModel
 * @property {string} verifyModel
 * @property {string} jevMode
 */

/**
 * @param {object} status
 * @returns {StageForm} the form as the backend currently has it
 */
export const stageFormFromStatus = (status) => {
  const gateProvider = status?.gate?.provider ?? "anthropic";
  const verifyProvider = status?.verify?.provider ?? "anthropic";
  return {
    gateProvider,
    verifyProvider,
    gateModel: storedModel(status, "gate", gateProvider),
    verifyModel: storedModel(status, "verify", verifyProvider),
    jevMode: storedJevMode(status),
  };
};

/**
 * Switch a stage's provider, swapping in that provider's stored model.
 *
 * @param {StageForm} form
 * @param {"gate"|"verify"} stage
 * @param {string} providerId
 * @param {object} status
 * @returns {StageForm}
 */
export const withStageProvider = (form, stage, providerId, status) => ({
  ...form,
  [`${stage}Provider`]: providerId,
  [`${stage}Model`]: storedModel(status, stage, providerId),
});

/**
 * @param {StageForm} form
 * @returns {{gateModel: boolean, verifyModel: boolean, jevMode: boolean}}
 *   which optional controls apply to the chosen providers
 */
export const stageFormFields = (form) => ({
  gateModel: MODEL_PROVIDERS.has(form.gateProvider),
  verifyModel: MODEL_PROVIDERS.has(form.verifyProvider),
  jevMode: form.gateProvider === "openrouter",
});

/**
 * @param {StageForm} form
 * @returns {string|null} a message when the form cannot be applied
 */
export const stageFormProblem = (form) =>
  [form.gateModel, form.verifyModel].some((model) => JEV_SLUG_RE.test(model.trim()))
    ? "Jev is a pre-screen, not a chat model — enter a chat model and choose a Jev pre-screen mode instead."
    : null;

/**
 * @param {StageForm} form
 * @param {object} status
 * @returns {boolean} true when applying would change something
 */
export const isStageFormDirty = (form, status) => {
  const fields = stageFormFields(form);
  const modelChanged = (stage) => {
    const typed = form[`${stage}Model`].trim();
    return typed !== "" && typed !== storedModel(status, stage, form[`${stage}Provider`]);
  };
  return (
    form.gateProvider !== (status?.gate?.provider ?? "") ||
    form.verifyProvider !== (status?.verify?.provider ?? "") ||
    (fields.gateModel && modelChanged("gate")) ||
    (fields.verifyModel && modelChanged("verify")) ||
    (fields.jevMode && form.jevMode !== storedJevMode(status))
  );
};

/**
 * The request for `saveStages`: models and the Jev mode are sent only where
 * they apply, so an empty field never overwrites a stored value.
 *
 * @param {StageForm} form
 */
export const stageRequestFromForm = (form) => {
  const fields = stageFormFields(form);
  return {
    gateProvider: form.gateProvider,
    verifyProvider: form.verifyProvider,
    gateModel: fields.gateModel ? form.gateModel.trim() : "",
    verifyModel: fields.verifyModel ? form.verifyModel.trim() : "",
    jevMode: fields.jevMode ? form.jevMode : "",
  };
};
