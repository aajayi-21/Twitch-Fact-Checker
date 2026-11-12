/**
 * Shared plumbing for the provider modules: one HTTP helper with a common
 * error vocabulary, JSON extraction, and the usage shape every provider
 * reports in.
 *
 * Provider modules use plain `fetch`, not vendor SDKs. The extension has no
 * build step and may not load remote code (Manifest V3), so every dependency
 * would have to be vendored as source; four REST calls do not justify four
 * SDKs.
 */

/** What went wrong, in terms the UI can act on (ProviderError.kind). */
export const ERROR_KIND = Object.freeze({
  AUTH: "auth", // key missing, wrong, revoked, or not allowed to do this
  BILLING: "billing", // no credit / billing not enabled — fix on the provider's site
  RATE_LIMIT: "rate_limit", // slow down; retryAfterS may say for how long
  BAD_REQUEST: "bad_request", // the request was rejected: unknown model, unsupported option
  SERVER: "server", // the provider failed; safe to retry later
  NETWORK: "network", // could not reach the provider at all
  TIMEOUT: "timeout",
  REFUSED: "refused", // the model declined to answer
  INVALID_OUTPUT: "invalid_output", // answered, but not in the required shape
});

export class ProviderError extends Error {
  /**
   * @param {string} kind - ERROR_KIND.*
   * @param {string} message
   * @param {object} [details]
   * @param {number|null} [details.status] - HTTP status, when there was one
   * @param {number|null} [details.retryAfterS]
   */
  constructor(kind, message, {status = null, retryAfterS = null} = {}) {
    super(message);
    this.name = "ProviderError";
    this.kind = kind;
    this.status = status;
    this.retryAfterS = retryAfterS;
  }
}

const DEFAULT_TIMEOUT_MS = 60_000;

/**
 * Pull a human-readable message out of the error bodies the four providers
 * use: {error: {message}}, {error: "…"}, {message}, or [{error: {message}}].
 *
 * @param {unknown} body
 * @returns {string|null}
 */
export const extractErrorMessage = (body) => {
  const root = Array.isArray(body) ? body[0] : body;
  if (typeof root !== "object" || root === null) {
    return typeof root === "string" && root ? root : null;
  }
  const error = root.error;
  if (typeof error === "string") {
    return error;
  }
  if (typeof error?.message === "string") {
    return error.message;
  }
  return typeof root.message === "string" ? root.message : null;
};

// Shapes of the four providers' keys. Providers echo a (partly masked) key
// back in "invalid key" messages; nothing key-shaped may reach the UI, a log
// or a report.
const KEY_SHAPED = /\b(?:sk-[A-Za-z0-9_*-]{6,}|AIza[A-Za-z0-9_*-]{6,})/g;

/**
 * Remove API keys, and anything shaped like one, from provider-supplied text.
 *
 * @param {string} text
 * @param {string[]} [secrets] - exact values to remove (the key just sent)
 * @returns {string}
 */
export const redactSecrets = (text, secrets = []) => {
  let cleaned = String(text);
  for (const secret of secrets) {
    if (typeof secret === "string" && secret.length >= 6) {
      cleaned = cleaned.replaceAll(secret, "[key removed]");
    }
  }
  return cleaned.replace(KEY_SHAPED, "[key removed]");
};

/**
 * Default mapping from HTTP status to error kind. Providers pass
 * `classifyError` to refine it (e.g. Gemini reports a bad key as 400).
 *
 * @param {number} status
 * @returns {string}
 */
export const kindForStatus = (status) => {
  if (status === 401 || status === 403) {
    return ERROR_KIND.AUTH;
  }
  if (status === 402) {
    return ERROR_KIND.BILLING;
  }
  if (status === 429) {
    return ERROR_KIND.RATE_LIMIT;
  }
  if (status === 408) {
    return ERROR_KIND.TIMEOUT;
  }
  return status >= 500 ? ERROR_KIND.SERVER : ERROR_KIND.BAD_REQUEST;
};

/**
 * Send a JSON request and return the parsed JSON response.
 *
 * API keys travel only in `headers` — never in the URL, which ends up in
 * logs and history.
 *
 * @param {string} url
 * @param {object} options
 * @param {"GET"|"POST"} [options.method]
 * @param {Record<string, string>} options.headers
 * @param {object} [options.body] - serialised as JSON
 * @param {AbortSignal} [options.signal] - caller cancellation
 * @param {number} [options.timeoutMs]
 * @param {(status: number, message: string, body: unknown) => string|null} [options.classifyError]
 *   provider-specific refinement; return null to keep the default kind
 * @param {typeof fetch} [options.fetchImpl] - injection point for tests
 * @returns {Promise<any>}
 * @throws {ProviderError}
 */
export const requestJson = async (
  url,
  {
    method = "POST",
    headers,
    body,
    signal,
    timeoutMs = DEFAULT_TIMEOUT_MS,
    classifyError,
    fetchImpl = globalThis.fetch,
  }
) => {
  const timeout = AbortSignal.timeout(timeoutMs);
  let response;
  try {
    response = await fetchImpl(url, {
      method,
      headers: {...(body ? {"Content-Type": "application/json"} : {}), ...headers},
      body: body ? JSON.stringify(body) : undefined,
      signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
    });
  } catch (error) {
    if (signal?.aborted) {
      throw error; // the caller cancelled; not a provider failure
    }
    if (error?.name === "TimeoutError" || timeout.aborted) {
      throw new ProviderError(ERROR_KIND.TIMEOUT, `No response within ${timeoutMs / 1000} s.`);
    }
    throw new ProviderError(
      ERROR_KIND.NETWORK,
      `Could not reach the provider (${error?.message ?? error}).`
    );
  }

  const text = await response.text();
  let parsed = null;
  try {
    parsed = text ? JSON.parse(text) : null;
  } catch {
    parsed = null;
  }
  if (!response.ok) {
    // Providers quote the offending key (partly masked) in auth errors.
    const message = redactSecrets(
      extractErrorMessage(parsed) ?? (text.slice(0, 300) || response.statusText),
      Object.values(headers).map((value) => String(value).replace(/^Bearer /, ""))
    );
    const retryAfter = Number(response.headers.get("retry-after"));
    throw new ProviderError(
      classifyError?.(response.status, message, parsed) ?? kindForStatus(response.status),
      message,
      {
        status: response.status,
        retryAfterS: Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter : null,
      }
    );
  }
  if (parsed === null) {
    throw new ProviderError(ERROR_KIND.INVALID_OUTPUT, "The provider returned a non-JSON body.", {
      status: response.status,
    });
  }
  return parsed;
};

/**
 * Parse the JSON object a model wrote. Strict-schema modes return bare JSON;
 * weaker modes sometimes wrap it in a code fence or add a sentence around it,
 * so fall back to the outermost {...}.
 *
 * @param {string} text
 * @returns {object}
 * @throws {ProviderError} INVALID_OUTPUT when no JSON object can be read
 */
export const parseModelJson = (text) => {
  const trimmed = String(text ?? "").trim();
  const candidates = [trimmed];
  const fenced = trimmed.match(/```(?:json)?\s*([\s\S]*?)```/i);
  if (fenced) {
    candidates.push(fenced[1].trim());
  }
  const first = trimmed.indexOf("{");
  const last = trimmed.lastIndexOf("}");
  if (first !== -1 && last > first) {
    candidates.push(trimmed.slice(first, last + 1));
  }
  for (const candidate of candidates) {
    try {
      const value = JSON.parse(candidate);
      if (typeof value === "object" && value !== null && !Array.isArray(value)) {
        return value;
      }
    } catch {
      // try the next candidate
    }
  }
  throw new ProviderError(
    ERROR_KIND.INVALID_OUTPUT,
    `The model did not return a JSON object (got: ${trimmed.slice(0, 120) || "nothing"}).`
  );
};

/**
 * @typedef {object} Usage
 * @property {number} inputTokens - prompt tokens billed at the full rate
 * @property {number} cachedInputTokens - prompt tokens read from a cache
 * @property {number} outputTokens - includes reasoning tokens
 * @property {number} webSearches - billable searches the provider ran
 * @property {number|null} costUsd - exact cost when the provider reports it
 * @property {number} calls - HTTP requests this result took
 */

/** @returns {Usage} */
export const emptyUsage = () => ({
  inputTokens: 0,
  cachedInputTokens: 0,
  outputTokens: 0,
  webSearches: 0,
  costUsd: null,
  calls: 0,
});

/**
 * @param {Usage} total
 * @param {Partial<Usage>} part
 * @returns {Usage}
 */
export const addUsage = (total, part) => ({
  inputTokens: total.inputTokens + (part.inputTokens ?? 0),
  cachedInputTokens: total.cachedInputTokens + (part.cachedInputTokens ?? 0),
  outputTokens: total.outputTokens + (part.outputTokens ?? 0),
  webSearches: total.webSearches + (part.webSearches ?? 0),
  costUsd:
    part.costUsd === null || part.costUsd === undefined
      ? total.costUsd
      : (total.costUsd ?? 0) + part.costUsd,
  calls: total.calls + (part.calls ?? 1),
});
