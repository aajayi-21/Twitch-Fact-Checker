# Spike S3 — Local speech-to-text inside the extension: results

*Measured 2026-11-11 on branch `spike/stt-in-extension`. This is Phase 0, spike S3 of
[`viewer-app-build-guide.md`](../viewer-app-build-guide.md): "does the speech worker live
happily in the offscreen document, alongside tab capture?" It follows
[S1](stt-gpu-benchmark.md), which ran the same models from a local web server.*

## 1. Answer

**Yes.** A tab's audio was captured by the extension's existing capture code and
transcribed locally, live, in an offscreen document — on the CPU and on the GPU — with no
audio lost and no interference between the two.

- **One manifest change is required:** `'wasm-unsafe-eval'` in the extension-page content
  security policy. Without it the speech runtime cannot start. It is now in
  `extension/manifest.json`.
- **Nothing else is required.** No new permissions, no host permission for the model
  download, and no cross-origin isolation.
- **Cross-origin isolation is optional** and only helps one case (Whisper on the CPU).
  It is not adopted.

Same caveat as S1: one machine, one real GPU (Intel Arc integrated).

## 2. What was tested

The test loads the real unpacked extension into Chrome 157 and drives it unattended:

```
tab playing the 43 s fixture clip aloud
        │  chrome.tabCapture
        ▼
offscreen document
   offscreen/audio_capture.js   (production code, unchanged in behaviour)
        │  16 kHz PCM frames, one per 250 ms
        ▼
   engine/stt/live_transcriber.mjs
        segmenter ─▶ queue ─▶ worker (model on GPU or CPU) ─▶ transcripts
```

Each run records the transcript and its error rate against the known script, how long
each utterance waited for its transcript, and whether capture frames kept arriving on
time while the model was running.

## 3. Results

### 3.1 Live capture + transcription in the offscreen document

| Model | Runs on | Speed | Delay after an utterance ends | Late / lost audio | Word errors |
|---|---|---|---|---|---|
| Moonshine Base | CPU, 1 thread | 18× | 0.4 s (max 0.5 s) | none | 15 % |
| Parakeet 0.6B | GPU | 30× | 0.25 s (max 0.35 s) | none | 12 % |
| Whisper Base (en) | GPU | 9–11× | 0.8 s (max 1.1 s) | none | 10–13 % |
| Moonshine Base | GPU | 10× | 0.7 s (max 0.9 s) | none | 12 % |
| Whisper Base (en) | CPU, 1 thread | 3× | 2.3 s (max 2.7 s) | none | 11 % |

- **"Delay"** is from the moment an utterance is closed to its transcript arriving. An
  utterance is closed 0.5 s after the speaker pauses, so the delay a viewer would
  perceive from the end of speech is this figure plus half a second.
- **Capture was never disturbed.** In every run, all ~45 s of audio arrived, the longest
  gap between 250 ms frames was 257–261 ms, and no utterance was dropped.
- **The capture path costs no accuracy.** Moonshine Base scored 15 % on this clip both
  through tab capture and when fed the file directly in S1.
- Word error rates are on the synthetic fixture, which is harder than real speech (S1
  measured 0 % on real speech for all of these). They vary by a few points between runs
  because capture timing shifts where utterances are cut.
- The Whisper GPU run was repeated in a normal Chrome window with no GPU flags: 11×,
  0.8 s delay, nothing late.

### 3.2 As an extension page (the S1 benchmark, re-run from `chrome-extension://`)

| Manifest | Result |
|---|---|
| As it was (no CSP entry) | **Fails**: `CompileError` — WebAssembly is blocked |
| With `'wasm-unsafe-eval'` | Works: Moonshine Base CPU 14–15×, GPU 12×; Whisper Base GPU 6×; Parakeet GPU 18×. All correct |

Speeds match S1's, so running from the extension origin costs nothing. Also confirmed in
the extension context:

- The 643 MB Parakeet model downloads, caches and loads without running out of memory.
- Models download from Hugging Face with **no host permission** (the files are served
  with permissive cross-origin headers).
- Models are cached per extension, and reload in 1–2 s on the next start.
- WebGPU is available to extension pages, to the offscreen document and to workers
  inside it.

### 3.3 Cross-origin isolation (optional manifest keys)

Adding `cross_origin_embedder_policy` and `cross_origin_opener_policy` makes extension
pages cross-origin isolated, which unlocks WebAssembly threads.

| Model | Without isolation (1 thread) | With isolation (4 threads) |
|---|---|---|
| Moonshine Base, CPU | 18×, 0.4 s delay | 17×, 0.4 s delay |
| Whisper Base, CPU | 3×, 2.3 s delay | 6.5×, 1.3 s delay |
| Whisper Base, GPU | 9–11× | 10× |

Tab capture, the popup, the settings page and the service worker all kept working with
isolation on. But the only beneficiary is Whisper on the CPU, which the plan does not
rely on (Moonshine is the CPU tier). Isolation also changes how every extension page
may load cross-origin resources, and that risk was not explored beyond the pages above.

**Decision: do not adopt isolation.** Revisit only if a CPU-only "Balanced" tier is
wanted.

## 4. What this settles for the plan

| Question from the guide | Answer |
|---|---|
| Can the speech model share the offscreen document with tab capture? | Yes, CPU and GPU |
| Does inference starve audio capture? | No — frame timing was unaffected in every run |
| Is a manifest change needed? | One line: `'wasm-unsafe-eval'` |
| Are new permissions needed for model download? | No |
| Is cross-origin isolation needed? | No |
| Does a large model download survive in an extension? | Yes (643 MB) |
| Which tier gives the fastest verdicts? | Parakeet on GPU: transcript 0.25 s after the utterance closes |

It also confirms S1's tiering with live numbers: **Moonshine Base on the CPU is a sound
universal default** (18× on one thread, under half a second of delay), and **Parakeet on
the GPU is the best accurate tier** where its self-test passes.

## 5. What was built

| Path | What it is |
|---|---|
| `extension/engine/stt/stream_segmenter.mjs` | Cuts a live audio stream into utterances at pauses (energy-based; the neural detector replaces one decision in Phase 3) |
| `extension/engine/stt/live_transcriber.mjs` | Owns the worker and the queue; transcribes utterances in order, and drops the oldest when the model cannot keep up |
| `extension/engine/stt/stt_worker.mjs` | The transcription worker |
| `extension/engine/stt/stt_runtime.mjs` | Model loading shared by the live worker and the benchmark worker |
| `extension/spike/offscreen_stt.*`, `live_stt.*` | The test's offscreen document and controller page |
| `extension/tests/stt_live.test.mjs` | 20 unit tests for the segmenter and the queue (74 in total) |
| `scripts/run_extension_spike.mjs` | Loads the extension into Chrome and runs either test unattended |
| `scripts/lib/chrome_harness.mjs` | Shared Chrome launcher and DevTools client |
| `docs/spike-results/reports/ext-*.json` | The raw reports behind every number here |

Two changes touch existing code:

- `extension/manifest.json` — the CSP line above.
- `extension/offscreen/audio_capture.js` — the audio worklet is now located relative to
  the module instead of the hosting page, so the class works from any extension page.
  Behaviour from `offscreen.html` is unchanged; the live test exercises this path.

Run it:

```bash
python3 scripts/vendor_stt_runtime.py
node --test "extension/tests/*.test.mjs"
node scripts/run_extension_spike.mjs --mode live --headed --model whisper-base.en --variant webgpu-mixed
```

Headless runs need `--gpu` to see the GPU on Linux. Working files (Chrome profiles with
downloaded models, patched extension copies) go to `.stt-bench/`, which is git-ignored
and safe to delete.

## 6. Not covered

- **The production session flow.** The test drives capture from a spike page, not from
  the popup → service worker → offscreen path, and transcripts are not yet sent anywhere.
  Wiring local speech into that flow is Phase 3.
- **The viewer's click.** Chrome was started with a flag that lets the extension capture
  a tab without the toolbar click a person provides. The real flow already has that
  click.
- **Real streams.** One clean, synthetic clip with pauses between sentences. Music,
  overlapping speakers and long unbroken speech will stress the energy-based segmenter;
  the planned neural detector is the fix.
- **Long sessions.** 45 seconds per run. Memory growth and heat over hours are unmeasured.
- **Firefox.** It has no offscreen documents; it stays on the companion backend.
- **Other GPUs**, as in S1.
- One early report (`ext-live-csp-moonshine-cpu.json`) records 4 threads; it ran on 1.
  The thread count was mis-reported before a fix made in this spike.
