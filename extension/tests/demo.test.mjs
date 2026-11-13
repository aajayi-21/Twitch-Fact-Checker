/**
 * Unit tests for the no-key demo: the recorded answers and the stand-in
 * provider that replays them through the real pipeline.
 *
 * Run from the repository root:
 *
 *     node --test "extension/tests/*.test.mjs"
 */

import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import {test} from "node:test";

import {createDemoProvider} from "../engine/demo/demo_provider.mjs";
import {DEMO_RECORDING} from "../engine/demo/recording.mjs";
import {EVIDENCE_LEVELS, LABELS, TOPICS} from "../engine/generated/prompts.mjs";
import {BrowserPipeline} from "../engine/pipeline/browser_pipeline.mjs";

const instant = {wait: async () => {}};

// What the Fast speech model actually produced for the clip in a browser
// run — mis-hearings included ("Dora Awards", "cat" for "chat").
const HEARD_BY_THE_MODEL =
  "Welcome back to the stream everyone we are live did you know that Eiffel Tower is 450 meters " +
  "tall Look it up, cat. Honestly this patch is terrible, the developers have no idea what they " +
  "are doing. I'm going to Push the left lane and take the tower before their jungler responds. " +
  "Someone said the Great Wall of China is visible from space with the naked eye, and that's " +
  "just facts. Messi has won eight million Dora Awards no other player is close trust me our " +
  "team is winning the whole tournament This year easy by the way the speed of light is about " +
  "300 000 kilometers per second anyway Remember to hit that follow button.";

test("the recording is a complete, well-formed real run", () => {
  assert.match(DEMO_RECORDING.recordedAt, /^\d{4}-\d\d-\d\d$/);
  assert.ok(DEMO_RECORDING.provider && DEMO_RECORDING.model);
  assert.equal(DEMO_RECORDING.claims.length, 4);
  for (const claim of DEMO_RECORDING.claims) {
    assert.ok(TOPICS.includes(claim.topic), claim.topic);
    assert.ok(claim.check_worthiness >= 0.55, "every recorded claim passes the default threshold");
    assert.ok(LABELS.includes(claim.verdict.label));
    assert.ok(EVIDENCE_LEVELS.includes(claim.verdict.evidence));
    assert.ok(claim.verdict.explanation.length > 40);
    // The product rule: no real verdict without sources.
    assert.ok(claim.verdict.sources.length >= 1, claim.claim_text);
    for (const source of claim.verdict.sources) {
      assert.match(source.url, /^https:\/\//);
    }
    assert.doesNotThrow(() => new RegExp(claim.heardWhen));
  }
});

test("the demo clip is in the package and is what the runner expects", async () => {
  const clip = await readFile(new URL("../engine/demo/demo_16k.wav", import.meta.url));
  // Canonical 44-byte header (the runner skips exactly that), 16 kHz mono PCM16.
  assert.equal(clip.toString("latin1", 0, 4), "RIFF");
  assert.equal(clip.toString("latin1", 36, 40), "data");
  assert.equal(clip.readUInt32LE(24), 16000);
  assert.equal(clip.readUInt16LE(22), 1);
  assert.equal(clip.readUInt16LE(34), 16);
  const seconds = (clip.length - 44) / 2 / 16000;
  assert.ok(seconds > 30 && seconds < 60, `${seconds} s`);
});

test("gate: a claim is returned once its words are heard, and only once", async () => {
  const provider = createDemoProvider(DEMO_RECORDING, instant);
  const first = await provider.runGate({transcript: "did you know that Eiffel Tower is 450 meters"});
  assert.deepEqual(first.claims.map((claim) => claim.claim_text), [DEMO_RECORDING.claims[0].claim_text]);
  assert.deepEqual(Object.keys(first.claims[0]).sort(), ["check_worthiness", "claim_text", "topic"]);
  const again = await provider.runGate({transcript: "the eiffel tower, as I said"});
  assert.deepEqual(again.claims, []);
  const nothing = await provider.runGate({transcript: "honestly this patch is terrible"});
  assert.deepEqual(nothing.claims, []);
});

test("gate: a claim split across two batches is caught through the context", async () => {
  const provider = createDemoProvider(DEMO_RECORDING, instant);
  // "…the speed of" ended one batch and "light is…" began the next.
  const {claims} = await provider.runGate({
    context: "by the way the speed of",
    transcript: "light is about 300 000 kilometers per second",
  });
  assert.equal(claims.length, 1);
  assert.equal(claims[0].topic, "science_tech");
  // Without the context the second half alone says nothing recognisable.
  const alone = await createDemoProvider(DEMO_RECORDING, instant).runGate({
    transcript: "light is about 300 000 kilometers per second",
  });
  assert.deepEqual(alone.claims, []);
});

test("gate: every recorded claim is recognised in what the speech model really heard", async () => {
  const provider = createDemoProvider(DEMO_RECORDING, instant);
  const {claims} = await provider.runGate({transcript: HEARD_BY_THE_MODEL});
  assert.equal(claims.length, 4);
});

test("verify: returns the recorded verdict, as a copy, and refuses anything else", async () => {
  const provider = createDemoProvider(DEMO_RECORDING, instant);
  const recorded = DEMO_RECORDING.claims[0];
  const {verdict, mode} = await provider.runVerify({claim: recorded.claim_text});
  assert.equal(mode, "single");
  assert.equal(verdict.label, recorded.verdict.label);
  verdict.sources.length = 0;
  assert.ok(recorded.verdict.sources.length > 0, "the recording is not mutated");
  await assert.rejects(provider.runVerify({claim: "The moon is made of cheese."}), /no answer/);
});

test("the real pipeline, fed the heard text, yields the four recorded verdicts", async () => {
  const provider = createDemoProvider(DEMO_RECORDING, instant);
  const stage = {provider, apiKey: "", model: DEMO_RECORDING.model};
  const frames = [];
  const pipeline = new BrowserPipeline({
    gate: stage,
    verify: stage,
    emit: (frame) => frames.push(frame),
    budgets: {dailyChecks: 0, monthlyUsd: 0},
    spent: {checksToday: 0, spendThisMonthUsd: 0},
    tuning: {verifyPerMinute: 6000, verifyBurst: 10},
  });
  // Utterance by utterance, as the transcriber delivers it.
  for (const sentence of HEARD_BY_THE_MODEL.split(/(?<=[.?!])\s+/)) {
    pipeline.addTranscript(sentence);
    pipeline.tick();
    await pipeline.gateRun;
  }
  await pipeline.stop();
  const verdicts = frames.filter((frame) => frame.type === "verdict");
  assert.deepEqual(verdicts.map((verdict) => verdict.label).sort(), ["FALSE", "FALSE", "TRUE", "TRUE"]);
  assert.ok(verdicts.every((verdict) => verdict.sources.length > 0 && verdict.used_fallback === false));
  // Nothing was called, so nothing may look as if it cost money.
  const usage = frames.filter((frame) => frame.type === "usage");
  assert.ok(usage.every((frame) => frame.input_tokens === 0 && !("cost_usd" in frame)));
});
