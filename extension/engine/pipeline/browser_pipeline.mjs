/**
 * The browser engine's claim pipeline: transcript in, verdicts out, with no
 * backend involved.
 *
 *   utterances ─▶ buffer ─▶ GATE (one call per interval) ─▶ claims
 *        claims ─▶ sensitivity ─▶ topics ─▶ duplicates ─▶ queue
 *         queue ─▶ VERIFY (one at a time, rate-limited) ─▶ verdict
 *
 * It is the JavaScript counterpart of the middle of backend/app/pipeline.py
 * and speaks the same frame vocabulary through `emit` — status, claim,
 * verdict, usage, error — so the overlay, the history and the usage screen
 * cannot tell which engine a session ran on.
 *
 * The rules it shares with the backend, and where each comes from:
 *   - batching and the context tail            claim_buffer.mjs
 *   - sensitivity thresholds                   generated/prompts.mjs (exported from Python)
 *   - duplicate suppression                    dedupe.mjs
 *   - verdict invariants (no sources -> UNVERIFIED, …)   applied inside every
 *     provider module by verdict_rules.mjs
 *   - a bounded verify queue that drops the OLDEST claim, a verify rate
 *     limit, one retry on a timeout, and a cooldown after a rate-limit or
 *     billing error                            here
 *
 * Added here because the viewer pays per call: spending limits (budget.mjs).
 *
 * Not ported (companion-only, build guide §9.1): the Jev pre-screen,
 * contradiction alerts, on-screen (vision) claims.
 *
 * No chrome.* or DOM APIs; providers, the clock and the sleeper are passed
 * in, so the whole flow is unit-tested under Node.
 */

import {SENSITIVITY_THRESHOLDS, TOPICS} from "../generated/prompts.mjs";
import {ERROR_KIND, ProviderError} from "../providers/common.mjs";
import {BUDGET_STATE, budgetStatus} from "./budget.mjs";
import {ClaimBuffer} from "./claim_buffer.mjs";
import {ClaimMemory} from "./dedupe.mjs";

export const PIPELINE_DEFAULTS = Object.freeze({
  tickMs: 1000,
  gateTimeoutMs: 15_000, // backend GATE_TIMEOUT_S
  verifyTimeoutMs: 45_000, // backend VERIFY_TIMEOUT_S
  verifyPerMinute: 8, // backend VERIFY_RPM
  verifyBurst: 2,
  verifyQueueMax: 3, // backend VERIFY_QUEUE_MAXSIZE
  rateLimitCooldownS: 60, // when the provider does not say how long
  blockedCooldownS: 300, // out of credit, or the key was refused
  flushTimeoutMs: 20_000, // on Stop: how long to finish what was heard
});

const isoSeconds = (ms) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");

/**
 * Whether a call failed by running out of time: either the provider helper
 * said so, or this pipeline's own per-call time limit fired (which surfaces
 * as the abort signal's TimeoutError).
 */
const isTimeout = (error) =>
  (error instanceof ProviderError && error.kind === ERROR_KIND.TIMEOUT) ||
  error?.name === "TimeoutError";

/**
 * One provider call's (or one verify's) usage as a wire frame — the same
 * shape the backend sends (backend/app/models.py UsageFrame).
 *
 * @param {"gate"|"verify"} stage
 * @param {string} providerId
 * @param {string} model
 * @param {import("../providers/common.mjs").Usage} usage
 * @param {string|null} [verdictId]
 * @returns {object}
 */
export const usageFrame = (stage, providerId, model, usage, verdictId = null) => {
  const frame = {
    type: "usage",
    stage,
    provider: providerId,
    model,
    calls: usage.calls ?? 1,
    input_tokens: usage.inputTokens ?? 0,
    cached_input_tokens: usage.cachedInputTokens ?? 0,
    output_tokens: usage.outputTokens ?? 0,
    web_searches: usage.webSearches ?? 0,
  };
  // Present only when the provider reported an exact cost.
  if (typeof usage.costUsd === "number") {
    frame.cost_usd = usage.costUsd;
  }
  if (verdictId) {
    frame.verdict_id = verdictId;
  }
  return frame;
};

const defaultSleep = (ms, signal) =>
  new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => {
      clearTimeout(timer);
      resolve();
    });
  });

/**
 * @typedef {object} StageConfig
 * @property {object} provider - a provider module (providers/index.mjs)
 * @property {string} apiKey
 * @property {string} model
 */

export class BrowserPipeline {
  /**
   * @param {object} options
   * @param {StageConfig} options.gate - who finds claims
   * @param {StageConfig} options.verify - who checks them
   * @param {(frame: object) => void} options.emit - receives every frame
   * @param {"low"|"medium"|"high"} [options.sensitivity]
   * @param {string[]|null} [options.enabledTopics] - null: all topics
   * @param {{dailyChecks: number, monthlyUsd: number}} options.budgets
   * @param {{checksToday: number, spendThisMonthUsd: number}} options.spent -
   *   where the viewer stood when the session started; kept current here
   * @param {(usageFrame: object) => number|null} [options.priceUsage] - cost
   *   of one usage frame in USD, null when unknown
   * @param {() => number} [options.now] - milliseconds
   * @param {(ms: number, signal?: AbortSignal) => Promise<void>} [options.sleep]
   * @param {() => string} [options.newId]
   * @param {Partial<typeof PIPELINE_DEFAULTS>} [options.tuning]
   */
  constructor({
    gate,
    verify,
    emit,
    sensitivity = "medium",
    enabledTopics = null,
    budgets,
    spent,
    priceUsage = () => null,
    now = () => Date.now(),
    sleep = defaultSleep,
    newId = () => crypto.randomUUID().replaceAll("-", ""),
    tuning = {},
  }) {
    this.gate = gate;
    this.verify = verify;
    this.emit = emit;
    this.budgets = budgets;
    this.spent = {...spent};
    this.priceUsage = priceUsage;
    this.now = now;
    this.sleep = sleep;
    this.newId = newId;
    this.tuning = {...PIPELINE_DEFAULTS, ...tuning};

    this.buffer = new ClaimBuffer();
    this.memory = new ClaimMemory();
    this.applyConfig({sensitivity, topics: enabledTopics});

    /** Claims waiting to be verified, oldest first. */
    this.queue = [];
    this.gateRun = null;
    this.verifyRun = null;
    this.timer = null;
    this.stopping = false;
    this.disposed = false;
    this.abort = new AbortController();

    this.cooldownUntilMs = 0;
    this.cooldownReason = null;
    this.tokens = this.tuning.verifyBurst;
    this.tokensAtMs = this.now();
    /** The budget state last announced, so each change is said once. */
    this.announcedBudget = BUDGET_STATE.OK;
    this.idleWaiters = [];

    this.counts = {gateCalls: 0, verifyCalls: 0, claims: 0, verdicts: 0};
  }

  /** Begin checking the buffer once a second. */
  start() {
    this.timer = setInterval(() => this.tick(), this.tuning.tickMs);
  }

  /** @param {string} text - one transcribed utterance */
  addTranscript(text) {
    if (!this.stopping && !this.disposed) {
      this.buffer.add(text);
    }
  }

  /**
   * Live setting changes from the app.
   *
   * @param {{sensitivity?: string, topics?: string[]|null}} config
   */
  applyConfig({sensitivity, topics} = {}) {
    if (sensitivity in SENSITIVITY_THRESHOLDS) {
      this.threshold = SENSITIVITY_THRESHOLDS[sensitivity];
    }
    if (topics !== undefined) {
      // "other" is the catch-all and can never be switched off.
      this.enabledTopics = Array.isArray(topics) ? new Set([...topics, "other"]) : new Set(TOPICS);
    }
  }

  /** Run the gate if it is due. Called by the timer; tests call it directly. */
  tick() {
    if (this.stopping || this.disposed || this.gateRun) {
      return;
    }
    if (this.buffer.shouldRun(this.now())) {
      this.gateRun = this.#runGate().finally(() => {
        this.gateRun = null;
      });
    }
  }

  /**
   * Stop was pressed: look at what was heard since the last gate pass,
   * finish the checks already queued (within a time limit), then shut down.
   *
   * @returns {Promise<void>}
   */
  async stop() {
    if (this.disposed) {
      return;
    }
    this.stopping = true;
    clearInterval(this.timer);
    await this.gateRun;
    if (this.buffer.hasPending) {
      await this.#runGate();
    }
    // A wall-clock bound, deliberately not the injectable `sleep`: that one
    // paces the rate limiter, and this one is about how long a person waits
    // after pressing Stop.
    let flushTimer;
    await Promise.race([
      this.#whenIdle(),
      new Promise((resolve) => {
        flushTimer = setTimeout(resolve, this.tuning.flushTimeoutMs);
      }),
    ]);
    clearTimeout(flushTimer);
    this.dispose();
  }

  /** Abandon everything now. Safe to call more than once. */
  dispose() {
    this.disposed = true;
    clearInterval(this.timer);
    this.abort.abort();
    this.queue = [];
    this.idleWaiters.splice(0).forEach((resolve) => resolve());
  }

  /* ---------------------------------------------------------------- */
  /* Gate                                                               */
  /* ---------------------------------------------------------------- */

  async #runGate() {
    const batch = this.buffer.drain(this.now());
    if (!batch) {
      return;
    }
    // Claim detection is a paid call too: no budget, no gate. The batch is
    // dropped rather than kept — hours of transcript must not pile up.
    if (this.#budgetBlocks() || this.#coolingDown()) {
      return;
    }
    this.counts.gateCalls += 1;
    let result;
    try {
      result = await this.gate.provider.runGate({
        apiKey: this.gate.apiKey,
        model: this.gate.model,
        context: batch.context,
        transcript: batch.transcript,
        signal: this.#signal(this.tuning.gateTimeoutMs),
      });
    } catch (error) {
      this.#handleFailure(error, "gate");
      return;
    }
    if (this.disposed) {
      return;
    }
    this.#emitUsage("gate", this.gate, result.usage);
    for (const claim of result.claims) {
      this.#route({id: this.newId(), ...claim});
    }
  }

  /** Decide what happens to one claim the gate found. */
  #route(claim) {
    this.counts.claims += 1;
    if (claim.check_worthiness < this.threshold) {
      this.#emitClaim(claim, "below_threshold");
      return;
    }
    if (!this.enabledTopics.has(claim.topic)) {
      this.#emitClaim(claim, "topic_skipped");
      this.emit({
        type: "status",
        stage: "topic_skipped",
        claim: claim.claim_text,
        topic: claim.topic,
      });
      return;
    }
    if (this.memory.isDuplicate(claim.claim_text)) {
      this.#emitClaim(claim, "duplicate");
      return;
    }
    // The newest claim is the most relevant to what is being said now, so a
    // full queue gives up its OLDEST entry.
    if (this.queue.length >= this.tuning.verifyQueueMax) {
      this.#emitClaim(this.queue.shift(), "queue_dropped");
    }
    this.queue.push(claim);
    this.#emitClaim(claim, "pending");
    this.emit({type: "status", stage: "verifying", claim: claim.claim_text});
    this.#pump();
  }

  /* ---------------------------------------------------------------- */
  /* Verify                                                             */
  /* ---------------------------------------------------------------- */

  #pump() {
    if (this.verifyRun || this.disposed) {
      return;
    }
    const claim = this.queue.shift();
    if (!claim) {
      this.idleWaiters.splice(0).forEach((resolve) => resolve());
      return;
    }
    this.verifyRun = this.#verifyClaim(claim).finally(() => {
      this.verifyRun = null;
      this.#pump();
    });
  }

  #whenIdle() {
    if (!this.verifyRun && this.queue.length === 0) {
      return Promise.resolve();
    }
    return new Promise((resolve) => this.idleWaiters.push(resolve));
  }

  async #verifyClaim(claim) {
    if (this.#coolingDown()) {
      const seconds = Math.ceil((this.cooldownUntilMs - this.now()) / 1000);
      this.#emitClaim(claim, "verify_failed");
      this.emit({
        type: "error",
        code: "quota_cooldown",
        message: `Fact-checks paused for ${seconds}s: ${this.cooldownReason}`,
        fatal: false,
      });
      return;
    }
    if (this.#budgetBlocks()) {
      this.#emitClaim(claim, "verify_failed");
      return;
    }
    await this.#takeToken();
    if (this.disposed) {
      return;
    }
    // Count the attempt: it is billed whether or not a verdict comes back.
    this.counts.verifyCalls += 1;
    this.spent.checksToday += 1;
    const startedAt = this.now();
    let result;
    try {
      result = await this.#verifyWithRetry(claim);
    } catch (error) {
      if (!this.disposed) {
        this.#emitClaim(claim, "verify_failed");
        this.#handleFailure(error, "verify");
      }
      return;
    }
    if (this.disposed) {
      return;
    }
    const verdictId = this.newId();
    this.counts.verdicts += 1;
    this.emit({
      type: "verdict",
      id: verdictId,
      claim: claim.claim_text,
      topic: claim.topic,
      label: result.verdict.label,
      explanation: result.verdict.explanation,
      sources: result.verdict.sources,
      checked_at: isoSeconds(this.now()),
      // Any path other than the provider's first-choice structured answer.
      used_fallback: !["single", "strict"].includes(result.mode),
      claim_id: claim.id,
      latency_ms: Math.round(this.now() - startedAt),
      provider: this.verify.provider.id,
      model: this.verify.model,
    });
    this.#emitClaim(claim, "verified");
    this.#emitUsage("verify", this.verify, result.usage, verdictId);
    this.#announceBudget();
  }

  /** One retry when the provider simply took too long (as the backend does). */
  async #verifyWithRetry(claim) {
    const attempt = () =>
      this.verify.provider.runVerify({
        apiKey: this.verify.apiKey,
        model: this.verify.model,
        claim: claim.claim_text,
        signal: this.#signal(this.tuning.verifyTimeoutMs),
      });
    try {
      return await attempt();
    } catch (error) {
      if (!isTimeout(error) || this.disposed) {
        throw error;
      }
      return attempt();
    }
  }

  /** Token bucket: at most `verifyPerMinute` checks a minute, small bursts allowed. */
  async #takeToken() {
    const perMs = this.tuning.verifyPerMinute / 60_000;
    const refill = () => {
      const nowMs = this.now();
      this.tokens = Math.min(
        this.tuning.verifyBurst,
        this.tokens + (nowMs - this.tokensAtMs) * perMs
      );
      this.tokensAtMs = nowMs;
    };
    refill();
    while (this.tokens < 1 && !this.disposed) {
      await this.sleep(Math.ceil((1 - this.tokens) / perMs), this.abort.signal);
      refill();
    }
    this.tokens -= 1;
  }

  /* ---------------------------------------------------------------- */
  /* Failures, cooldown, budget                                         */
  /* ---------------------------------------------------------------- */

  #coolingDown() {
    return this.now() < this.cooldownUntilMs;
  }

  #cooldown(seconds, reason) {
    this.cooldownUntilMs = Math.max(this.cooldownUntilMs, this.now() + seconds * 1000);
    this.cooldownReason = reason;
  }

  /**
   * @param {unknown} error
   * @param {"gate"|"verify"} stage
   */
  #handleFailure(error, stage) {
    if (this.disposed) {
      return; // aborted on purpose
    }
    const kind = error instanceof ProviderError ? error.kind : null;
    const detail = isTimeout(error)
      ? "the provider took too long to answer"
      : String(error?.message ?? error);
    const provider = this[stage].provider.label;
    if (kind === ERROR_KIND.RATE_LIMIT) {
      const seconds = error.retryAfterS ?? this.tuning.rateLimitCooldownS;
      this.#cooldown(seconds, `${provider} rate limit reached.`);
      this.emit({
        type: "error",
        code: "quota_cooldown",
        message: `${provider} rate limit reached; pausing fact-checks for ${Math.ceil(seconds)}s.`,
        fatal: false,
      });
      return;
    }
    if (kind === ERROR_KIND.BILLING || kind === ERROR_KIND.AUTH) {
      // Every further call would fail the same way; stop spending attempts
      // and say what to fix.
      const reason =
        kind === ERROR_KIND.BILLING
          ? `${provider} reports no credit or billing: ${detail}`
          : `${provider} refused the API key: ${detail}`;
      this.#cooldown(this.tuning.blockedCooldownS, reason);
      this.emit({type: "error", code: "quota_cooldown", message: reason, fatal: false});
      return;
    }
    if (stage === "verify") {
      this.emit({
        type: "error",
        code: "llm_failure",
        message: `Fact-check failed: ${detail}`,
        fatal: false,
      });
    } else {
      // A missed gate pass costs one batch of transcript, silently — the
      // same trade the backend makes.
      console.warn("[fact-checker] claim detection failed (batch dropped):", detail);
    }
  }

  /** @returns {boolean} whether a spending limit forbids provider calls now */
  #budgetBlocks() {
    return this.#announceBudget() === BUDGET_STATE.STOP;
  }

  /** Tell the viewer when a limit is near or reached — once per change. */
  #announceBudget() {
    const status = budgetStatus(this.budgets, this.spent);
    if (status.state !== this.announcedBudget) {
      this.announcedBudget = status.state;
      if (status.state !== BUDGET_STATE.OK) {
        this.emit({
          type: "error",
          code: status.state === BUDGET_STATE.STOP ? "budget_reached" : "budget_warning",
          message: status.message,
          fatal: false,
        });
      }
    }
    return status.state;
  }

  /* ---------------------------------------------------------------- */
  /* Frames                                                             */
  /* ---------------------------------------------------------------- */

  #signal(timeoutMs) {
    return AbortSignal.any([this.abort.signal, AbortSignal.timeout(timeoutMs)]);
  }

  #emitClaim(claim, outcome) {
    this.emit({
      type: "claim",
      id: claim.id,
      text: claim.claim_text,
      topic: claim.topic,
      check_worthiness: claim.check_worthiness,
      outcome,
    });
  }

  #emitUsage(stage, config, usage, verdictId = null) {
    const frame = usageFrame(stage, config.provider.id, config.model, usage, verdictId);
    const cost = this.priceUsage(frame);
    if (typeof cost === "number" && Number.isFinite(cost)) {
      this.spent.spendThisMonthUsd += cost;
    }
    this.emit(frame);
  }
}
