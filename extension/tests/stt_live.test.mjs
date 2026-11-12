/**
 * Unit tests for the live speech path: the streaming segmenter and the
 * transcriber's queueing, ordering and drop policy.
 *
 * Run from the repo root (Node 20+, no dependencies):
 *
 *     node --test "extension/tests/*.test.mjs"
 */

import assert from "node:assert/strict";
import {afterEach, beforeEach, describe, it} from "node:test";

import {splitOnSilence} from "../engine/stt/audio_utils.mjs";
import {LiveTranscriber} from "../engine/stt/live_transcriber.mjs";
import {StreamSegmenter} from "../engine/stt/stream_segmenter.mjs";

const SAMPLE_RATE = 16000;

/** Build audio from [seconds, amplitude] spans of a 220 Hz tone. */
const makeAudio = (spans) => {
  const total = spans.reduce((sum, [seconds]) => sum + Math.round(seconds * SAMPLE_RATE), 0);
  const samples = new Float32Array(total);
  let cursor = 0;
  for (const [seconds, amplitude] of spans) {
    const count = Math.round(seconds * SAMPLE_RATE);
    for (let index = 0; index < count; index += 1) {
      samples[cursor + index] = amplitude * Math.sin((2 * Math.PI * 220 * index) / SAMPLE_RATE);
    }
    cursor += count;
  }
  return samples;
};

/** Feed `samples` in fixed-size chunks, the way capture delivers them. */
const feed = (segmenter, samples, chunkSamples = 4000) => {
  const segments = [];
  for (let offset = 0; offset < samples.length; offset += chunkSamples) {
    segments.push(...segmenter.push(samples.subarray(offset, offset + chunkSamples)));
  }
  return segments;
};

const seconds = (segment) => segment.endS - segment.startS;

describe("StreamSegmenter", () => {
  it("emits nothing for silence", () => {
    const segmenter = new StreamSegmenter();
    assert.deepEqual(feed(segmenter, new Float32Array(SAMPLE_RATE * 3)), []);
    assert.deepEqual(segmenter.flush(), []);
  });

  it("closes an utterance once the pause after it is long enough", () => {
    const segmenter = new StreamSegmenter();
    const audio = makeAudio([
      [0.5, 0],
      [2, 0.5],
      [1, 0],
    ]);
    const segments = feed(segmenter, audio);
    assert.equal(segments.length, 1);
    // 2 s of speech plus up to 200 ms of pad on each side.
    assert.ok(seconds(segments[0]) >= 2 && seconds(segments[0]) <= 2.5, `${seconds(segments[0])}`);
    assert.ok(Math.abs(segments[0].startS - 0.3) < 0.1, `start ${segments[0].startS}`);
    assert.equal(segments[0].forced, false);
    assert.equal(segments[0].samples.length, Math.round(seconds(segments[0]) * SAMPLE_RATE));
  });

  it("does not close before the minimum pause has elapsed", () => {
    const segmenter = new StreamSegmenter();
    const audio = makeAudio([
      [1, 0.5],
      [0.3, 0],
    ]);
    assert.deepEqual(feed(segmenter, audio), []);
    assert.equal(segmenter.flush().length, 1);
  });

  it("keeps a short pause inside one utterance", () => {
    const segmenter = new StreamSegmenter();
    const audio = makeAudio([
      [1, 0.5],
      [0.2, 0],
      [1, 0.5],
      [1, 0],
    ]);
    const segments = feed(segmenter, audio);
    assert.equal(segments.length, 1);
    assert.ok(seconds(segments[0]) > 2.1);
  });

  it("separates two utterances and keeps them in stream order", () => {
    const segmenter = new StreamSegmenter();
    const audio = makeAudio([
      [0.5, 0],
      [1.5, 0.5],
      [0.8, 0],
      [2.5, 0.5],
      [1, 0],
    ]);
    const segments = feed(segmenter, audio);
    assert.equal(segments.length, 2);
    assert.ok(segments[0].endS <= segments[1].startS + 0.21, "second starts after the first (pad may touch)");
    assert.ok(Math.abs(seconds(segments[1]) - 2.9) < 0.3, `${seconds(segments[1])}`);
  });

  it("cuts unbroken speech at the length cap and loses no audio", () => {
    const segmenter = new StreamSegmenter({maxSegmentS: 4});
    const audio = makeAudio([
      [11, 0.5],
      [1, 0],
    ]);
    const segments = [...feed(segmenter, audio), ...segmenter.flush()];
    assert.ok(segments.length >= 3, `${segments.length} segments`);
    assert.ok(segments.slice(0, -1).every((segment) => segment.forced));
    for (const segment of segments) {
      assert.ok(seconds(segment) <= 4.01, `${seconds(segment)}`);
    }
    // Forced cuts are contiguous: each segment starts where the last ended.
    for (let index = 1; index < segments.length; index += 1) {
      assert.ok(Math.abs(segments[index].startS - segments[index - 1].endS) < 1e-9);
    }
    const covered = segments.reduce((sum, segment) => sum + seconds(segment), 0);
    assert.ok(covered >= 11 && covered <= 11.5, `covered ${covered}`);
  });

  it("discards a click too short to be speech", () => {
    const segmenter = new StreamSegmenter();
    const audio = makeAudio([
      [0.5, 0],
      [0.09, 0.5],
      [1, 0],
    ]);
    assert.deepEqual([...feed(segmenter, audio), ...segmenter.flush()], []);
  });

  it("gives the same result however the audio is chunked", () => {
    const audio = makeAudio([
      [0.4, 0],
      [1.7, 0.5],
      [0.7, 0],
      [2.2, 0.3],
      [0.9, 0],
    ]);
    const shape = (segments) => segments.map((segment) => [segment.startS, segment.endS]);
    const reference = shape(feed(new StreamSegmenter(), audio, 4000));
    assert.equal(reference.length, 2);
    for (const chunkSamples of [1, 137, 512, 16000, audio.length]) {
      assert.deepEqual(shape(feed(new StreamSegmenter(), audio, chunkSamples)), reference);
    }
  });

  it("agrees with the batch segmenter on clean speech", () => {
    const audio = makeAudio([
      [0.5, 0],
      [2, 0.5],
      [0.9, 0],
      [3, 0.5],
      [1, 0],
    ]);
    const batch = splitOnSilence(audio).map((range) => [
      range.start / SAMPLE_RATE,
      range.end / SAMPLE_RATE,
    ]);
    const streamed = feed(new StreamSegmenter(), audio).map((segment) => [
      segment.startS,
      segment.endS,
    ]);
    assert.equal(streamed.length, batch.length);
    streamed.forEach(([startS, endS], index) => {
      assert.ok(Math.abs(startS - batch[index][0]) < 0.1, `start ${startS} vs ${batch[index][0]}`);
      assert.ok(Math.abs(endS - batch[index][1]) < 0.1, `end ${endS} vs ${batch[index][1]}`);
    });
  });

  it("still hears a quiet speaker after a loud one", () => {
    const segmenter = new StreamSegmenter({peakHalfLifeS: 2});
    const audio = makeAudio([
      [1, 0.9],
      [6, 0],
      [1.5, 0.02],
      [1, 0],
    ]);
    assert.equal(feed(segmenter, audio).length, 2);
  });

  it("ignores line noise below the floor", () => {
    const segmenter = new StreamSegmenter();
    const audio = makeAudio([[3, 0.001]]);
    assert.deepEqual([...feed(segmenter, audio), ...segmenter.flush()], []);
  });
});

/**
 * Stand-in for the transcription worker: answers "load" with "ready", and
 * holds each "transcribe" until the test releases it.
 */
class FakeWorker {
  static instances = [];

  constructor() {
    this.listeners = {message: [], error: []};
    this.transcribeRequests = [];
    this.terminated = false;
    FakeWorker.instances.push(this);
  }

  addEventListener(type, listener) {
    this.listeners[type].push(listener);
  }

  emit(data) {
    this.listeners.message.forEach((listener) => listener({data}));
  }

  postMessage(message) {
    if (message.type === "load") {
      queueMicrotask(() =>
        this.emit(this.failLoad ? {type: "error", fatal: true, message: "no gpu"} : {type: "ready", info: {loadMs: 1}})
      );
    } else if (message.type === "transcribe") {
      this.transcribeRequests.push(message);
    }
  }

  /** Complete the oldest outstanding transcribe request. */
  reply(text = "ok") {
    const request = this.transcribeRequests.shift();
    this.emit({type: "transcript", id: request.id, text, inferenceMs: 5});
    return request;
  }

  terminate() {
    this.terminated = true;
  }
}

/** One PCM16 capture frame holding `seconds` of a tone at `amplitude`. */
const pcmFrame = (durationS, amplitude) => {
  const count = Math.round(durationS * SAMPLE_RATE);
  const int16 = new Int16Array(count);
  for (let index = 0; index < count; index += 1) {
    int16[index] = Math.round(amplitude * 32767 * Math.sin((2 * Math.PI * 220 * index) / SAMPLE_RATE));
  }
  return int16.buffer;
};

/** Push one utterance of `durationS` followed by a closing pause. */
const pushUtterance = (transcriber, durationS) => {
  transcriber.pushPcm16(pcmFrame(durationS, 0.5));
  transcriber.pushPcm16(pcmFrame(0.8, 0));
};

describe("LiveTranscriber", () => {
  const originalWorker = globalThis.Worker;

  beforeEach(() => {
    FakeWorker.instances = [];
    globalThis.Worker = FakeWorker;
  });

  afterEach(() => {
    globalThis.Worker = originalWorker;
  });

  const start = async (options) => {
    const transcripts = [];
    const fatals = [];
    const transcriber = new LiveTranscriber(
      {onTranscript: (transcript) => transcripts.push(transcript), onFatal: (message) => fatals.push(message)},
      options
    );
    const info = await transcriber.load({repo: "x", device: "wasm", dtype: "q8", numThreads: 1});
    return {transcriber, transcripts, fatals, info, worker: FakeWorker.instances.at(-1)};
  };

  it("resolves load() with the worker's ready info", async () => {
    const {info} = await start();
    assert.deepEqual(info, {loadMs: 1});
  });

  it("rejects load() when the model cannot be loaded", async () => {
    const transcriber = new LiveTranscriber({onTranscript: () => {}});
    const loading = transcriber.load({repo: "x", device: "webgpu", dtype: "fp32", numThreads: 1});
    FakeWorker.instances.at(-1).failLoad = true;
    await assert.rejects(loading, /no gpu/);
  });

  it("sends one utterance at a time and reports transcripts in order", async () => {
    const {transcriber, transcripts, worker} = await start();
    pushUtterance(transcriber, 1.5);
    pushUtterance(transcriber, 2);
    assert.equal(worker.transcribeRequests.length, 1, "second utterance waits its turn");

    worker.reply("first");
    assert.equal(worker.transcribeRequests.length, 1, "second is sent after the first returns");
    worker.reply("second");

    assert.deepEqual(
      transcripts.map((transcript) => transcript.text),
      ["first", "second"]
    );
    assert.ok(transcripts[0].endS <= transcripts[1].startS + 0.21);
    assert.equal(transcripts[0].inferenceMs, 5);
    assert.ok(transcripts[1].waitMs >= 0 && transcripts[1].lagMs >= transcripts[1].waitMs);
    assert.equal(transcriber.stats.segmentsTranscribed, 2);
    assert.equal(transcriber.stats.framesReceived, 4);
  });

  it("drops the oldest waiting utterances when the model cannot keep up", async () => {
    const {transcriber, transcripts, worker} = await start({maxBacklogS: 5});
    // The first utterance is in flight and never answered; the rest pile up.
    for (let index = 0; index < 5; index += 1) {
      pushUtterance(transcriber, 2);
    }
    assert.ok(transcriber.stats.segmentsDropped >= 1, "backlog was trimmed");
    assert.ok(transcriber.stats.droppedSeconds > 0);
    const waitingSeconds = transcriber.waiting.reduce((sum, item) => sum + item.seconds, 0);
    assert.ok(waitingSeconds <= 5 + 2.5, `backlog ${waitingSeconds}s`);

    // The in-flight utterance is never dropped, and the newest survives.
    const firstId = worker.transcribeRequests[0].id;
    worker.reply("oldest in flight");
    assert.equal(transcripts[0].text, "oldest in flight");
    assert.equal(firstId, 1);
    const survivingIds = transcriber.waiting.map((item) => item.id);
    assert.ok(survivingIds.includes(5) || transcriber.inFlight?.id === 5, "newest utterance kept");
  });

  it("finish() flushes the open utterance and waits for the queue to drain", async () => {
    const {transcriber, transcripts, worker} = await start();
    transcriber.pushPcm16(pcmFrame(1.5, 0.5)); // no closing pause yet
    assert.equal(worker.transcribeRequests.length, 0);

    let finished = false;
    const finishing = transcriber.finish().then(() => {
      finished = true;
    });
    assert.equal(worker.transcribeRequests.length, 1, "flush sends the open utterance");
    await Promise.resolve();
    assert.equal(finished, false);
    worker.reply("tail");
    await finishing;
    assert.equal(transcripts.at(-1).text, "tail");
  });

  it("finish() resolves at once when nothing is pending", async () => {
    const {transcriber} = await start();
    await transcriber.finish();
  });

  it("skips a failed utterance and keeps going", async () => {
    const {transcriber, transcripts, worker, fatals} = await start();
    pushUtterance(transcriber, 1.5);
    pushUtterance(transcriber, 1.5);
    const failed = worker.transcribeRequests.shift();
    worker.emit({type: "error", id: failed.id, fatal: false, message: "bad utterance"});
    worker.reply("next one");
    assert.deepEqual(
      transcripts.map((transcript) => transcript.text),
      ["next one"]
    );
    assert.equal(transcriber.stats.utteranceErrors, 1);
    assert.deepEqual(fatals, []);
  });

  it("reports a lost GPU as fatal instead of producing transcripts", async () => {
    const {transcriber, transcripts, worker, fatals} = await start();
    pushUtterance(transcriber, 1.5);
    worker.emit({type: "error", id: 1, fatal: true, message: "GPU device lost (destroyed)"});
    assert.deepEqual(fatals, ["GPU device lost (destroyed)"]);
    assert.deepEqual(transcripts, []);
  });

  it("ignores replies that arrive after dispose()", async () => {
    const {transcriber, transcripts, worker} = await start();
    pushUtterance(transcriber, 1.5);
    const request = worker.transcribeRequests[0];
    transcriber.dispose();
    worker.emit({type: "transcript", id: request.id, text: "late", inferenceMs: 1});
    assert.deepEqual(transcripts, []);
  });
});
