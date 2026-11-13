/**
 * Unit tests for the browser engine's claim pipeline: duplicate detection,
 * transcript batching, spending limits, and the orchestrator that ties claim
 * detection to checking — all with scripted providers and a fake clock.
 *
 * Run from the repository root:
 *
 *     node --test "extension/tests/*.test.mjs"
 */

import assert from "node:assert/strict";
import {test} from "node:test";

import {ERROR_KIND, ProviderError} from "../engine/providers/common.mjs";
import {BrowserPipeline} from "../engine/pipeline/browser_pipeline.mjs";
import {
  BUDGET_STATE,
  DEFAULT_BUDGETS,
  budgetStatus,
  normalizeBudgets,
} from "../engine/pipeline/budget.mjs";
import {ClaimBuffer} from "../engine/pipeline/claim_buffer.mjs";
import {ClaimMemory, normalizeClaim, tokenSetRatio} from "../engine/pipeline/dedupe.mjs";

/* ------------------------------------------------------------------ */
/* Duplicate detection                                                  */
/* ------------------------------------------------------------------ */

test("normalizeClaim matches the backend: lower case, no punctuation, single spaces", () => {
  assert.equal(
    normalizeClaim("  The speed of light is about 300,000 km per second.  "),
    "the speed of light is about 300000 km per second"
  );
  assert.equal(normalizeClaim("Crime   doubled — in a year!"), "crime doubled — in a year");
  assert.equal(normalizeClaim(null), "");
});

// Values from rapidfuzz.fuzz.token_set_ratio on the normalised pair, computed
// with the backend's own environment. Both engines must drop the same repeats.
const RAPIDFUZZ_GOLDEN = [
  ["the eiffel tower is 330 meters tall", "the eiffel tower is 330 meters in height", 92.3077],
  ["the eiffel tower is 330 meters tall", "the eiffel tower is 330 meters tall", 100],
  ["inflation is at forty percent this year", "this year inflation is at forty percent", 100],
  ["brazil has won five fifa world cups", "the eiffel tower is 330 meters tall", 34.2857],
  ["unemployment fell last quarter", "unemployment rose last quarter", 90.9091],
  [
    "the speed of light is about 300000 km per second",
    "light travels at roughly 300000 kilometres per second",
    67.3267,
  ],
  ["crime doubled in a year", "crime doubled in a year in the city", 100],
  ["a b c", "c b a d e f g h", 100],
  ["", "anything", 0],
  [
    "police are arresting protesters at the port of dover right now",
    "protesters are being arrested by police at dover",
    76.9231,
  ],
];

test("tokenSetRatio agrees with rapidfuzz on the golden pairs", () => {
  for (const [first, second, expected] of RAPIDFUZZ_GOLDEN) {
    const actual = tokenSetRatio(first, second);
    assert.ok(
      Math.abs(actual - expected) < 0.001,
      `"${first}" vs "${second}": got ${actual.toFixed(4)}, rapidfuzz says ${expected}`
    );
    assert.equal(tokenSetRatio(second, first).toFixed(4), actual.toFixed(4), "symmetric");
  }
});

test("ClaimMemory drops a rephrasing and keeps a different claim", () => {
  const memory = new ClaimMemory();
  assert.equal(memory.isDuplicate("The Eiffel Tower is 330 meters tall."), false);
  assert.equal(memory.isDuplicate("The Eiffel Tower is 330 meters in height."), true);
  assert.equal(memory.isDuplicate("the eiffel tower is 330 METERS tall!!"), true);
  assert.equal(memory.isDuplicate("Brazil has won five FIFA World Cups."), false);
  // Known limit, shared with the backend: one changed word in a short claim
  // still scores above the threshold.
  const short = new ClaimMemory();
  short.isDuplicate("Unemployment fell last quarter.");
  assert.equal(short.isDuplicate("Unemployment rose last quarter."), true);
});

/* ------------------------------------------------------------------ */
/* Transcript batching                                                  */
/* ------------------------------------------------------------------ */

test("ClaimBuffer: the first batch waits only for enough words", () => {
  const buffer = new ClaimBuffer();
  assert.equal(buffer.shouldRun(0), false);
  buffer.add("just five words spoken here");
  assert.equal(buffer.shouldRun(0), false);
  buffer.add("and now three more");
  assert.equal(buffer.shouldRun(0), true);
});

test("ClaimBuffer: later batches wait for the interval as well", () => {
  const buffer = new ClaimBuffer({intervalS: 12});
  buffer.add("one two three four five six seven eight");
  assert.deepEqual(buffer.drain(1000), {
    context: "",
    transcript: "one two three four five six seven eight",
  });
  buffer.add("nine ten eleven twelve thirteen fourteen fifteen sixteen");
  assert.equal(buffer.shouldRun(5000), false, "enough words, too soon");
  assert.equal(buffer.shouldRun(13_000), true);
});

test("ClaimBuffer: each batch carries the tail of what came before", () => {
  const buffer = new ClaimBuffer({contextTailWords: 4});
  buffer.add("the senator said the bill");
  buffer.add("would cost two trillion dollars");
  const first = buffer.drain(0);
  assert.equal(first.transcript, "the senator said the bill would cost two trillion dollars");
  buffer.add("he also said it was fully funded");
  const second = buffer.drain(20_000);
  assert.equal(second.context, "cost two trillion dollars");
  assert.equal(second.transcript, "he also said it was fully funded");
});

test("ClaimBuffer: draining nothing returns null and still restarts the interval", () => {
  const buffer = new ClaimBuffer({intervalS: 12});
  buffer.add("   ");
  assert.equal(buffer.hasPending, false);
  assert.equal(buffer.drain(1000), null);
  buffer.add("one two three four five six seven eight");
  assert.equal(buffer.shouldRun(2000), false);
});

/* ------------------------------------------------------------------ */
/* Spending limits                                                      */
/* ------------------------------------------------------------------ */

test("budgets are on by default and junk falls back to the defaults", () => {
  assert.deepEqual(normalizeBudgets(undefined), {...DEFAULT_BUDGETS});
  assert.deepEqual(normalizeBudgets({dailyChecks: "lots", monthlyUsd: -3}), {...DEFAULT_BUDGETS});
  assert.deepEqual(normalizeBudgets({dailyChecks: 40.6, monthlyUsd: 2.5}), {
    dailyChecks: 41,
    monthlyUsd: 2.5,
  });
  // Zero is a deliberate "no limit", not junk.
  assert.deepEqual(normalizeBudgets({dailyChecks: 0, monthlyUsd: 0}), {
    dailyChecks: 0,
    monthlyUsd: 0,
  });
});

test("budgetStatus: fine, then a warning at 80%, then a stop", () => {
  const budgets = {dailyChecks: 100, monthlyUsd: 10};
  assert.equal(budgetStatus(budgets, {checksToday: 79, spendThisMonthUsd: 1}).state, BUDGET_STATE.OK);
  const warn = budgetStatus(budgets, {checksToday: 80, spendThisMonthUsd: 1});
  assert.equal(warn.state, BUDGET_STATE.WARN);
  assert.equal(warn.limit, "daily");
  assert.equal(warn.message, "80 of today's 100 fact-checks used.");
  const stop = budgetStatus(budgets, {checksToday: 100, spendThisMonthUsd: 1});
  assert.equal(stop.state, BUDGET_STATE.STOP);
  assert.match(stop.message, /limit of 100 fact-checks has been reached/);
});

test("budgetStatus: the monthly amount is a limit too, and a stop outranks a warning", () => {
  const budgets = {dailyChecks: 100, monthlyUsd: 10};
  const stop = budgetStatus(budgets, {checksToday: 85, spendThisMonthUsd: 10.02});
  assert.equal(stop.state, BUDGET_STATE.STOP);
  assert.equal(stop.limit, "monthly");
  assert.match(stop.message, /\$10\.00/);
  const warn = budgetStatus(budgets, {checksToday: 3, spendThisMonthUsd: 8});
  assert.equal(warn.message, "$8.00 of this month's $10.00 limit spent.");
});

test("budgetStatus: a limit of zero is no limit", () => {
  const status = budgetStatus(
    {dailyChecks: 0, monthlyUsd: 0},
    {checksToday: 9999, spendThisMonthUsd: 9999}
  );
  assert.equal(status.state, BUDGET_STATE.OK);
});

/* ------------------------------------------------------------------ */
/* The pipeline                                                         */
/* ------------------------------------------------------------------ */

const EIFFEL = {claim_text: "The Eiffel Tower is 450 meters tall.", check_worthiness: 0.9, topic: "history"};
const OPINION = {claim_text: "This patch is terrible.", check_worthiness: 0.2, topic: "gaming"};
const SPORTS = {claim_text: "Brazil has won five World Cups.", check_worthiness: 0.9, topic: "sports"};
const USAGE = {inputTokens: 1000, cachedInputTokens: 0, outputTokens: 100, webSearches: 1, costUsd: null, calls: 1};
const LONG_ENOUGH = "so apparently the eiffel tower is four hundred and fifty meters tall";

const verdictFor = (label = "FALSE") => ({
  verdict: {
    label,
    explanation: "It is about 330 metres tall.",
    evidence: "strong",
    sources: [{url: "https://www.toureiffel.paris/facts", title: "Key figures"}],
  },
  usage: USAGE,
  mode: "single",
});

/**
 * A pipeline with scripted providers and a clock the test controls.
 * `gateScript` / `verifyScript` are lists of results (or errors to throw),
 * one per call.
 */
const makePipeline = ({gateScript = [], verifyScript = [], ...overrides} = {}) => {
  const frames = [];
  const calls = {gate: [], verify: []};
  let nowMs = 1_000_000;
  let nextId = 0;
  const play = (script, record) => async (request) => {
    record.push(request);
    const step = script.shift();
    if (step instanceof Error) {
      throw step;
    }
    if (typeof step === "function") {
      return step(request);
    }
    return step ?? {claims: [], usage: USAGE};
  };
  const provider = (id) => ({
    id,
    label: id === "anthropic" ? "Anthropic" : "OpenRouter",
    runGate: play(gateScript, calls.gate),
    runVerify: play(verifyScript, calls.verify),
  });
  const pipeline = new BrowserPipeline({
    gate: {provider: provider("anthropic"), apiKey: "gate-key", model: "gate-model"},
    verify: {provider: provider("openrouter"), apiKey: "verify-key", model: "verify-model"},
    emit: (frame) => frames.push(frame),
    budgets: {dailyChecks: 150, monthlyUsd: 10},
    spent: {checksToday: 0, spendThisMonthUsd: 0},
    now: () => nowMs,
    sleep: async (ms) => {
      nowMs += ms;
    },
    newId: () => `id${(nextId += 1)}`,
    ...overrides,
  });
  return {
    pipeline,
    frames,
    calls,
    advance: (ms) => {
      nowMs += ms;
    },
    types: () => frames.map((frame) => (frame.type === "claim" ? `claim:${frame.outcome}` : frame.type)),
    of: (type) => frames.filter((frame) => frame.type === type),
  };
};

/** Feed text, run the gate, and wait for everything it set in motion. */
const hear = async (harness, text = LONG_ENOUGH) => {
  harness.pipeline.addTranscript(text);
  harness.pipeline.tick();
  await harness.pipeline.gateRun;
  await harness.pipeline.verifyRun;
};

test("pipeline: a claim is found, checked and reported in the backend's frames", async () => {
  const harness = makePipeline({
    gateScript: [{claims: [EIFFEL], usage: USAGE}],
    verifyScript: [verdictFor("FALSE")],
  });
  await hear(harness);
  assert.deepEqual(harness.types(), [
    "usage", // the gate call
    "claim:pending",
    "status",
    "verdict",
    "claim:verified",
    "usage", // the verify call
  ]);
  const [verdict] = harness.of("verdict");
  assert.deepEqual(
    {...verdict, checked_at: "x"},
    {
      type: "verdict",
      id: "id2",
      claim: EIFFEL.claim_text,
      topic: "history",
      label: "FALSE",
      explanation: "It is about 330 metres tall.",
      sources: [{url: "https://www.toureiffel.paris/facts", title: "Key figures"}],
      checked_at: "x",
      used_fallback: false,
      claim_id: "id1",
      latency_ms: 0,
      provider: "openrouter",
      model: "verify-model",
    }
  );
  assert.match(verdict.checked_at, /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/);
  const [gateUsage, verifyUsage] = harness.of("usage");
  assert.equal(gateUsage.stage, "gate");
  assert.equal(gateUsage.provider, "anthropic");
  assert.equal(gateUsage.model, "gate-model");
  assert.equal("verdict_id" in gateUsage, false);
  assert.equal(verifyUsage.stage, "verify");
  assert.equal(verifyUsage.verdict_id, "id2");
  assert.equal(verifyUsage.web_searches, 1);
  assert.equal("cost_usd" in verifyUsage, false, "no cost unless the provider reported one");
  // Each stage used its own provider, key and model.
  assert.equal(harness.calls.gate[0].apiKey, "gate-key");
  assert.equal(harness.calls.gate[0].transcript, LONG_ENOUGH);
  assert.equal(harness.calls.verify[0].apiKey, "verify-key");
  assert.equal(harness.calls.verify[0].claim, EIFFEL.claim_text);
});

test("pipeline: an exact provider cost is passed on", async () => {
  const harness = makePipeline({
    gateScript: [{claims: [EIFFEL], usage: {...USAGE, costUsd: 0.0004}}],
    verifyScript: [{...verdictFor(), usage: {...USAGE, costUsd: 0.0078}}],
  });
  await hear(harness);
  assert.deepEqual(harness.of("usage").map((frame) => frame.cost_usd), [0.0004, 0.0078]);
});

test("pipeline: nothing is sent to the gate until there is enough to say", async () => {
  const harness = makePipeline();
  harness.pipeline.addTranscript("too short");
  harness.pipeline.tick();
  assert.equal(harness.pipeline.gateRun, null);
  assert.equal(harness.calls.gate.length, 0);
});

test("pipeline: opinions fall below the threshold; sensitivity moves it", async () => {
  const borderline = {...EIFFEL, check_worthiness: 0.5};
  const medium = makePipeline({gateScript: [{claims: [OPINION, borderline], usage: USAGE}]});
  await hear(medium);
  assert.deepEqual(medium.types(), ["usage", "claim:below_threshold", "claim:below_threshold"]);
  assert.equal(medium.calls.verify.length, 0);

  const high = makePipeline({
    sensitivity: "high",
    gateScript: [{claims: [OPINION, borderline], usage: USAGE}],
    verifyScript: [verdictFor()],
  });
  await hear(high);
  assert.equal(high.calls.verify.length, 1);
});

test("pipeline: a claim on a topic that is switched off is skipped and said so", async () => {
  const harness = makePipeline({
    enabledTopics: ["history", "politics"],
    gateScript: [{claims: [SPORTS, EIFFEL], usage: USAGE}],
    verifyScript: [verdictFor()],
  });
  await hear(harness);
  const skipped = harness.frames.find((frame) => frame.stage === "topic_skipped");
  assert.deepEqual(skipped, {
    type: "status",
    stage: "topic_skipped",
    claim: SPORTS.claim_text,
    topic: "sports",
  });
  assert.equal(harness.calls.verify.length, 1);
  assert.equal(harness.calls.verify[0].claim, EIFFEL.claim_text);
});

test("pipeline: the catch-all topic cannot be switched off", async () => {
  const other = {claim_text: "The moon is 384,000 km away.", check_worthiness: 0.9, topic: "other"};
  const harness = makePipeline({
    enabledTopics: [],
    gateScript: [{claims: [other], usage: USAGE}],
    verifyScript: [verdictFor("TRUE")],
  });
  await hear(harness);
  assert.equal(harness.calls.verify.length, 1);
});

test("pipeline: a repeated claim is checked once", async () => {
  const harness = makePipeline({
    gateScript: [
      {claims: [EIFFEL], usage: USAGE},
      {claims: [{...EIFFEL, claim_text: "the Eiffel Tower is 450 meters tall!"}], usage: USAGE},
    ],
    verifyScript: [verdictFor()],
  });
  await hear(harness);
  harness.advance(13_000);
  await hear(harness, "and I will say it again the eiffel tower is four fifty meters");
  assert.equal(harness.calls.verify.length, 1);
  assert.equal(harness.types().at(-1), "claim:duplicate");
});

test("pipeline: live setting changes apply to the next batch", async () => {
  const harness = makePipeline({
    gateScript: [
      {claims: [OPINION], usage: USAGE},
      {claims: [{...OPINION, claim_text: "That boss has 2 million HP."}], usage: USAGE},
    ],
    verifyScript: [verdictFor()],
  });
  await hear(harness);
  assert.equal(harness.calls.verify.length, 0);
  harness.pipeline.applyConfig({sensitivity: "high", topics: null});
  harness.pipeline.threshold = 0.1; // "high" is 0.35; go lower to admit a 0.2 claim
  harness.advance(13_000);
  await hear(harness, "they keep saying that boss has two million hit points total");
  assert.equal(harness.calls.verify.length, 1);
});

test("pipeline: a full queue gives up its oldest claim", async () => {
  // Genuinely different sentences: near-identical ones would (rightly) be
  // dropped as duplicates before they ever reached the queue.
  const claims = [
    "Mount Everest is 8,849 metres high.",
    "Brazil has won five World Cups.",
    "Water boils at 100 degrees Celsius at sea level.",
    "The Berlin Wall fell in 1989.",
    "Jupiter has more than ninety known moons.",
  ].map((claim_text) => ({claim_text, check_worthiness: 0.9, topic: "other"}));
  let releaseFirst;
  const harness = makePipeline({
    gateScript: [{claims, usage: USAGE}],
    verifyScript: [
      () => new Promise((resolve) => {
        releaseFirst = () => resolve(verdictFor());
      }),
      verdictFor(),
      verdictFor(),
      verdictFor(),
    ],
  });
  harness.pipeline.addTranscript(LONG_ENOUGH);
  harness.pipeline.tick();
  await harness.pipeline.gateRun;
  // A is being verified; B, C, D fill the queue of three; E pushes B out.
  const dropped = harness.frames.filter((frame) => frame.outcome === "queue_dropped");
  assert.deepEqual(dropped.map((frame) => frame.text), [claims[1].claim_text]);
  releaseFirst();
  await harness.pipeline.stop();
  assert.deepEqual(
    harness.calls.verify.map((call) => call.claim),
    [claims[0], claims[2], claims[3], claims[4]].map((claim) => claim.claim_text)
  );
});

test("pipeline: checks are rate-limited to a burst of two, then eight a minute", async () => {
  const claims = [
    "Mount Everest is 8,849 metres high.",
    "Brazil has won five World Cups.",
    "The Berlin Wall fell in 1989.",
  ].map((claim_text) => ({claim_text, check_worthiness: 0.9, topic: "other"}));
  const startedAt = [];
  let harness;
  const timed = () => {
    startedAt.push(harness.pipeline.now());
    return verdictFor();
  };
  harness = makePipeline({
    gateScript: [{claims, usage: USAGE}],
    verifyScript: [timed, timed, timed],
  });
  await hear(harness);
  await harness.pipeline.stop();
  assert.equal(startedAt.length, 3);
  assert.equal(startedAt[1] - startedAt[0], 0, "the second rides the burst");
  assert.equal(startedAt[2] - startedAt[1], 7500, "the third waits 60 s / 8");
});

test("pipeline: a timeout is retried once, then reported", async () => {
  const timeout = new ProviderError(ERROR_KIND.TIMEOUT, "No response within 45 s.");
  const recovered = makePipeline({
    gateScript: [{claims: [EIFFEL], usage: USAGE}],
    verifyScript: [timeout, verdictFor()],
  });
  await hear(recovered);
  assert.equal(recovered.calls.verify.length, 2);
  assert.equal(recovered.of("verdict").length, 1);

  const failed = makePipeline({
    gateScript: [{claims: [EIFFEL], usage: USAGE}],
    verifyScript: [timeout, timeout],
  });
  await hear(failed);
  assert.equal(failed.calls.verify.length, 2);
  assert.deepEqual(failed.types().slice(-2), ["claim:verify_failed", "error"]);
  const [error] = failed.of("error");
  assert.equal(error.code, "llm_failure");
  assert.equal(error.fatal, false);
  assert.match(error.message, /took too long/);
});

test("pipeline: the pipeline's own time limit counts as a timeout too", async () => {
  const ownTimeout = Object.assign(new Error("signal timed out"), {name: "TimeoutError"});
  const harness = makePipeline({
    gateScript: [{claims: [EIFFEL], usage: USAGE}],
    verifyScript: [ownTimeout, verdictFor()],
  });
  await hear(harness);
  assert.equal(harness.of("verdict").length, 1);
});

test("pipeline: any other verify failure is reported and the session carries on", async () => {
  const harness = makePipeline({
    gateScript: [
      {claims: [EIFFEL], usage: USAGE},
      {claims: [SPORTS], usage: USAGE},
    ],
    verifyScript: [new ProviderError(ERROR_KIND.INVALID_OUTPUT, "not JSON"), verdictFor("TRUE")],
  });
  await hear(harness);
  assert.equal(harness.of("error")[0].message, "Fact-check failed: not JSON");
  harness.advance(13_000);
  await hear(harness, "brazil has won the world cup five times in total you know");
  assert.equal(harness.of("verdict").length, 1);
});

test("pipeline: a rate limit pauses checking for as long as the provider says", async () => {
  const limited = new ProviderError(ERROR_KIND.RATE_LIMIT, "slow down", {retryAfterS: 30});
  const harness = makePipeline({
    gateScript: [
      {claims: [EIFFEL], usage: USAGE},
      {claims: [SPORTS], usage: USAGE},
      {claims: [{...SPORTS, claim_text: "Germany has won four World Cups."}], usage: USAGE},
    ],
    verifyScript: [limited, verdictFor("TRUE")],
  });
  await hear(harness);
  assert.equal(harness.of("error")[0].code, "quota_cooldown");
  assert.match(harness.of("error")[0].message, /OpenRouter rate limit reached; pausing fact-checks for 30s/);

  // During the pause no provider is called at all — the gate costs money too.
  harness.advance(13_000);
  await hear(harness, "brazil has won the world cup five times in total you know");
  assert.equal(harness.calls.gate.length, 1);
  assert.equal(harness.calls.verify.length, 1);

  // Once it has passed, checking resumes.
  harness.advance(30_000);
  await hear(harness, "and germany has won the world cup four times as well");
  assert.equal(harness.calls.gate.length, 2);
  assert.equal(harness.of("verdict").length, 1);
});

test("pipeline: a refused key or empty balance stops calls and says what to fix", async () => {
  for (const [kind, expected] of [
    [ERROR_KIND.AUTH, /Anthropic refused the API key: invalid x-api-key/],
    [ERROR_KIND.BILLING, /Anthropic reports no credit or billing/],
  ]) {
    const harness = makePipeline({
      gateScript: [new ProviderError(kind, "invalid x-api-key")],
    });
    await hear(harness);
    assert.match(harness.of("error")[0].message, expected);
    harness.advance(13_000);
    await hear(harness, "more talking that would otherwise go to the gate right now");
    assert.equal(harness.calls.gate.length, 1, "no second attempt while blocked");
  }
});

test("pipeline: a failed gate pass loses one batch, quietly", async () => {
  const harness = makePipeline({
    gateScript: [new ProviderError(ERROR_KIND.SERVER, "500"), {claims: [EIFFEL], usage: USAGE}],
    verifyScript: [verdictFor()],
  });
  const warn = console.warn;
  console.warn = () => {};
  try {
    await hear(harness);
  } finally {
    console.warn = warn;
  }
  assert.deepEqual(harness.frames, []);
  harness.advance(13_000);
  await hear(harness);
  assert.equal(harness.of("verdict").length, 1);
});

test("pipeline: the daily limit warns at 80% and then stops every provider call", async () => {
  const harness = makePipeline({
    budgets: {dailyChecks: 5, monthlyUsd: 0},
    spent: {checksToday: 3, spendThisMonthUsd: 0},
    gateScript: [
      {claims: [EIFFEL], usage: USAGE},
      {claims: [SPORTS], usage: USAGE},
      {claims: [{...SPORTS, claim_text: "Germany has won four World Cups."}], usage: USAGE},
    ],
    verifyScript: [verdictFor(), verdictFor()],
  });
  await hear(harness); // 4 of 5 -> warning
  assert.deepEqual(harness.of("error").map((frame) => frame.code), ["budget_warning"]);
  assert.equal(harness.of("error")[0].message, "4 of today's 5 fact-checks used.");

  harness.advance(13_000);
  await hear(harness, "brazil has won the world cup five times in total you know"); // 5 of 5
  assert.equal(harness.of("verdict").length, 2);
  assert.deepEqual(harness.of("error").map((frame) => frame.code), ["budget_warning", "budget_reached"]);

  harness.advance(13_000);
  await hear(harness, "and germany has won the world cup four times as well");
  assert.equal(harness.calls.gate.length, 2, "the gate is not called once the limit is reached");
  assert.equal(harness.of("error").length, 2, "and the limit is announced once, not every tick");
});

test("pipeline: the monthly limit counts what each call cost", async () => {
  const harness = makePipeline({
    budgets: {dailyChecks: 0, monthlyUsd: 1},
    spent: {checksToday: 0, spendThisMonthUsd: 0.99},
    priceUsage: (frame) => (frame.stage === "verify" ? 0.011 : 0.0001),
    gateScript: [
      {claims: [EIFFEL], usage: USAGE},
      {claims: [SPORTS], usage: USAGE},
    ],
    verifyScript: [verdictFor()],
  });
  await hear(harness);
  assert.equal(harness.of("verdict").length, 1);
  assert.equal(harness.of("error").at(-1).code, "budget_reached");
  assert.match(harness.of("error").at(-1).message, /\$1\.00/);
  harness.advance(13_000);
  await hear(harness, "brazil has won the world cup five times in total you know");
  assert.equal(harness.calls.gate.length, 1);
});

test("pipeline: stop looks at the last words and finishes the checks in hand", async () => {
  const harness = makePipeline({
    gateScript: [{claims: [EIFFEL], usage: USAGE}],
    verifyScript: [verdictFor()],
  });
  // Too few words for a live gate pass; Stop must not lose them.
  harness.pipeline.addTranscript("the eiffel tower is 450 meters");
  harness.pipeline.tick();
  assert.equal(harness.calls.gate.length, 0);
  await harness.pipeline.stop();
  assert.equal(harness.calls.gate.length, 1);
  assert.equal(harness.of("verdict").length, 1);
  assert.equal(harness.pipeline.disposed, true);
  // After Stop nothing more is accepted.
  harness.pipeline.addTranscript("words after the session has ended do not count at all");
  harness.pipeline.tick();
  assert.equal(harness.calls.gate.length, 1);
});

test("pipeline: stop does not wait forever for a provider that never answers", async () => {
  const harness = makePipeline({
    gateScript: [{claims: [EIFFEL], usage: USAGE}],
    verifyScript: [() => new Promise(() => {})],
    tuning: {flushTimeoutMs: 30},
  });
  await Promise.race([hear(harness), new Promise((resolve) => setTimeout(resolve, 20))]);
  await harness.pipeline.stop();
  assert.equal(harness.pipeline.disposed, true);
  assert.equal(harness.of("verdict").length, 0);
});

test("pipeline: disposing mid-check emits nothing further", async () => {
  let finish;
  const harness = makePipeline({
    gateScript: [{claims: [EIFFEL], usage: USAGE}],
    verifyScript: [() => new Promise((resolve) => {
      finish = () => resolve(verdictFor());
    })],
  });
  harness.pipeline.addTranscript(LONG_ENOUGH);
  harness.pipeline.tick();
  await harness.pipeline.gateRun;
  const before = harness.frames.length;
  harness.pipeline.dispose();
  finish();
  await harness.pipeline.verifyRun;
  assert.equal(harness.frames.length, before);
  harness.pipeline.dispose(); // twice is fine
});

test("pipeline: a non-first-choice answer path is marked as a fallback", async () => {
  const harness = makePipeline({
    gateScript: [{claims: [EIFFEL], usage: USAGE}],
    verifyScript: [{...verdictFor(), mode: "repaired-after-strict"}],
  });
  await hear(harness);
  assert.equal(harness.of("verdict")[0].used_fallback, true);
});
