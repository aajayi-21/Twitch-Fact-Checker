/**
 * HTTP client for the local backend, as used by the app's screens.
 *
 * Every failure is turned into a BackendError whose `kind` tells the UI what
 * to say and whether the person can fix it — the backend not running is by
 * far the most common case and must never read like a crash.
 *
 * Pure apart from `fetch`, which is injectable, so it is unit-tested under
 * Node without a backend.
 */

/** What went wrong (BackendError.kind). */
export const BACKEND_ERROR = Object.freeze({
  UNREACHABLE: "unreachable", // nothing answered: the backend is not running
  TIMEOUT: "timeout", // it answered too slowly
  KEY_REJECTED: "key_rejected", // 401: the provider refused the key
  NOT_READY: "not_ready", // 409: a chosen provider is not connected
  INVALID: "invalid", // 400/404/422: the request was refused; detail says why
  PROVIDER_DOWN: "provider_down", // 502: the backend could not reach the provider
  SERVER: "server", // 5xx: the backend failed
});

export class BackendError extends Error {
  /**
   * @param {string} kind - BACKEND_ERROR.*
   * @param {string} message - safe to show
   * @param {number|null} [status]
   */
  constructor(kind, message, status = null) {
    super(message);
    this.name = "BackendError";
    this.kind = kind;
    this.status = status;
  }
}

const TIMEOUTS_MS = Object.freeze({
  status: 2500,
  // Saving a key validates it live against the provider.
  credentials: 30_000,
  // Config write + hot-swap, plus a model check against the provider.
  stages: 15_000,
  // A real gate call and one or more web-search verifications.
  testCheck: 90_000,
});

const KIND_BY_STATUS = Object.freeze({
  400: BACKEND_ERROR.INVALID,
  401: BACKEND_ERROR.KEY_REJECTED,
  404: BACKEND_ERROR.INVALID,
  409: BACKEND_ERROR.NOT_READY,
  422: BACKEND_ERROR.INVALID,
  502: BACKEND_ERROR.PROVIDER_DOWN,
});

/**
 * FastAPI reports errors as {detail: "…"} or, for validation failures,
 * {detail: [{msg, loc}, …]}.
 *
 * @param {unknown} body
 * @returns {string|null}
 */
export const readErrorDetail = (body) => {
  const detail = body?.detail;
  if (typeof detail === "string" && detail) {
    return detail;
  }
  if (Array.isArray(detail)) {
    const messages = detail.map((entry) => entry?.msg).filter(Boolean);
    return messages.length > 0 ? messages.join("; ") : null;
  }
  return null;
};

/**
 * @param {object} options
 * @param {() => string|null} options.getOrigin - the backend's http(s)
 *   origin, or null when the configured address is unusable
 * @param {typeof fetch} [options.fetchImpl]
 */
export const createBackendApi = ({getOrigin, fetchImpl = (...args) => fetch(...args)}) => {
  const request = async (path, {method = "GET", body, timeoutMs}) => {
    const origin = getOrigin();
    if (!origin) {
      throw new BackendError(
        BACKEND_ERROR.UNREACHABLE,
        "The backend address in Settings is not a valid ws:// or wss:// URL."
      );
    }
    let response;
    try {
      response = await fetchImpl(`${origin}${path}`, {
        method,
        cache: "no-store",
        headers: body ? {"Content-Type": "application/json"} : undefined,
        body: body ? JSON.stringify(body) : undefined,
        signal: AbortSignal.timeout(timeoutMs),
      });
    } catch (error) {
      if (error?.name === "TimeoutError") {
        throw new BackendError(
          BACKEND_ERROR.TIMEOUT,
          `The backend did not answer within ${Math.round(timeoutMs / 1000)} seconds.`
        );
      }
      throw new BackendError(BACKEND_ERROR.UNREACHABLE, "The local backend is not running.");
    }
    let parsed = null;
    try {
      parsed = await response.json();
    } catch {
      parsed = null;
    }
    if (!response.ok) {
      const kind =
        KIND_BY_STATUS[response.status] ??
        (response.status >= 500 ? BACKEND_ERROR.SERVER : BACKEND_ERROR.INVALID);
      throw new BackendError(
        kind,
        readErrorDetail(parsed) ?? `The backend answered with an error (HTTP ${response.status}).`,
        response.status
      );
    }
    return parsed;
  };

  return {
    /** Liveness, speech-engine state, active models, today's check count. */
    health: () => request("/healthz", {timeoutMs: TIMEOUTS_MS.status}),

    /** Which providers are connected and how each stage is routed. */
    setupStatus: () => request("/setup/status", {timeoutMs: TIMEOUTS_MS.status}),

    /**
     * Validate a key live and store it in the backend's `.env`. The key is
     * sent once and never kept in the browser.
     *
     * @param {{provider: string, apiKey: string}} credentials
     */
    saveCredentials: ({provider, apiKey}) =>
      request("/setup/credentials", {
        method: "POST",
        body: {provider, api_key: apiKey},
        timeoutMs: TIMEOUTS_MS.credentials,
      }),

    /**
     * Route each stage to a provider and model.
     *
     * @param {{gateProvider: string, verifyProvider: string, gateModel?: string,
     *   verifyModel?: string, jevMode?: string}} stages
     */
    saveStages: ({gateProvider, verifyProvider, gateModel = "", verifyModel = "", jevMode = ""}) =>
      request("/setup/stages", {
        method: "POST",
        body: {
          gate_provider: gateProvider,
          verify_provider: verifyProvider,
          gate_model: gateModel,
          verify_model: verifyModel,
          jev_mode: jevMode,
        },
        timeoutMs: TIMEOUTS_MS.stages,
      }),

    /** Lifetime and today totals: sessions, claims, verdict labels, cost. */
    statsSummary: () => request("/stats/summary", {timeoutMs: TIMEOUTS_MS.status}),

    /**
     * Run a sentence through the real pipeline (claim detection, then
     * web-search verification). Makes paid provider calls.
     *
     * @param {string} text
     */
    runTestCheck: (text) =>
      request("/debug/text", {
        method: "POST",
        body: {text, sensitivity: "high"},
        timeoutMs: TIMEOUTS_MS.testCheck,
      }),
  };
};
