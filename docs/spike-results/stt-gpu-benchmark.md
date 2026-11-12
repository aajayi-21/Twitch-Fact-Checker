# Spike S1 — Local speech-to-text across GPUs: results

*Measured 2026-11-11 on branch `spike/webgpu-stt`. This is Phase 0, spike S1 of
[`viewer-app-build-guide.md`](../viewer-app-build-guide.md): "can a viewer's GPU
transcribe live in the browser, and can we support a variety of GPUs?"*

## 1. Answer

**Yes for the machine tested, with one hard rule: never trust a GPU, measure it.**

- Local transcription works in stock Chrome with no flags, on the GPU and on the CPU.
- **Two of the four 16-bit GPU configurations produced fast, silent garbage**, on a GPU
  that advertises 16-bit support. Nothing in the WebGPU API reveals this in advance; only
  running a clip with a known transcript does.
- So GPU support is built as a **self-test ladder**: describe the GPU, try configurations
  in order, accept the first one that is both *correct* and *fast enough*, fall back to
  the CPU. Every failure case this produced in testing ended in a working configuration.

**What is proven and what is not.** One real GPU was tested (Intel Arc integrated). The
no-GPU, software-GPU and CPU-only paths were tested for real on the same machine. NVIDIA,
AMD and Apple GPUs were **not** tested — §6 is how to collect those results.

## 2. Test machine

| | |
|---|---|
| GPU | Intel Arc 130V/140V integrated (Lunar Lake); WebGPU reports `intel` / `xe-2lpg` |
| CPU | Intel Core Ultra 7 258V, 8 threads |
| OS / browser | Linux (Wayland, Mesa), Chrome 157.0.8103.50 |
| WebGPU | Available in stock Chrome, no flags; `shader-f16` advertised; 4 GB buffer limit |
| Runtime | Transformers.js 4.3.2 + ONNX Runtime Web 1.31.0-dev (vendored, no CDN) |

Two clips, each with a known transcript: 11 s of real speech (JFK, public domain) cut
into four short utterances, and the 43 s synthetic fixture
(`backend/tests/fixtures/claims_16k.wav`) cut into six longer ones. Segmentation mirrors
the backend's voice-activity settings (500 ms pause, 10 s cap).

"Speed" below is how many times faster than real time the speech was transcribed. The
pass mark is **2×** (real-time factor 0.5) and a word error rate under 25 % — a
broken/working test, not a quality bar.

## 3. Results

### 3.1 Speed and correctness on the Intel GPU

| Model | Configuration | Short utterances (real speech) | Longer utterances (fixture) | Verdict |
|---|---|---|---|---|
| Moonshine Base | CPU, 4 threads | 13–24× | 18× | **pass — selected** |
| | GPU, 32-bit | 8–13× | 7–13× | pass |
| | GPU, 16-bit encoder † | 19× | 48× | **wrong output** (100 % word errors) |
| Whisper Base (en) | GPU, 16-bit encoder | 3.3–6.8× | 7–10× | **pass — selected** |
| | GPU, 32-bit | 3.3–6.5× | 9× | pass |
| | CPU, 4 threads | 1.3–3.5× | 4.0× | too slow on short utterances in most runs |
| Distil-Whisper Small (en) | GPU, 16-bit encoder | 3.0× | 6–8× | **pass — selected** |
| | GPU, 32-bit | 3.2× | 7–8× | pass |
| | CPU, 4 threads | 0.8× | 2.2× | too slow |
| Parakeet CTC 0.6B | GPU, 4-bit weights | 18× | 41× | **pass — selected** |
| | GPU, 4-bit + 16-bit maths | 18× | 43× | **wrong output** (empty transcript) |
| | CPU, 4 threads | 11× | — | pass |
| Moonshine Tiny | CPU / GPU 32-bit | 15–20× | 14–30× | passes on real speech; too inaccurate on the fixture (29–33 % errors) |

Ranges are across repeated runs; run-to-run variation on this laptop was up to 2×.

† Measured before this configuration was removed from Moonshine's ladder; its report and
those of a few superseded early runs were not kept. Every other figure has a report in
`reports/`.

Word error rates of the passing configurations: 0 % on the real-speech clip for every
model except Moonshine Tiny; on the synthetic fixture, Whisper Base 8 %, Distil-Whisper
Small 11 %, Moonshine Base 15 %, Parakeet 19 % (the fixture is robotic text-to-speech,
which penalises some models more than real speech does).

### 3.2 Machines without a usable GPU (simulated on the same hardware)

| Situation | How it was produced | What happened |
|---|---|---|
| No GPU adapter | Headless Chrome, no GPU flags | GPU rungs skipped. Moonshine Base on CPU 24×, Whisper Base 3.5× — pass. Distil-Whisper Small 0.8× — correctly reported "too slow" |
| Software renderer, default behaviour | Forced fallback adapter (SwiftShader) | Recognised as software, GPU rungs skipped, CPU selected |
| Software renderer, GPU forced anyway | Same, with the test override | Moonshine Base ran correctly at **0.2×** with a 14 s warm-up — rejected as too slow, CPU selected. Whisper Base needed over a minute to warm up and was slower still (run not kept) |
| CPU, single thread | Page served without cross-origin isolation | Moonshine Base 10–11× — pass. Whisper Base 1.1–3.0× — marginal. Distil-Whisper Small 0.4–1.1× — fail |

### 3.3 Other measurements

- **First-use warm-up** (shader compilation) on the Intel GPU: 0.4–1.0 s. Not a concern.
- **Model load from cache:** 2–12 s depending on size.
- **Download size** per selected configuration: Moonshine Base 63 MB (CPU), Whisper Base
  250 MB, Distil-Whisper Small 489 MB, Parakeet 643 MB.

## 4. Findings that change the plan

### 4.1 16-bit GPU paths cannot be trusted — the self-test is mandatory

Three separate 16-bit failures appeared on one GPU, each of a different kind:

1. **All fully 16-bit model files fail to load** — the runtime rejects the published
   16-bit decoder files as invalid. This is a model-export defect, identical on every
   GPU. Those configurations are off the ladder until the runtime or models are updated.
2. **Moonshine with a 16-bit encoder loads, runs at up to 48×, and outputs
   `". I. . I. . I."`** for every clip.
3. **Parakeet with 16-bit maths loads, runs at 43×, and outputs nothing.**

Whisper's 16-bit encoder works correctly on the same GPU. So the failure is per-model and
per-driver, and a speed-only benchmark would have *preferred* the broken configurations.
The self-test checks correctness before speed for exactly this reason.

### 4.2 The GPU matters for the accurate models, not the fast one

- **Moonshine Base is CPU-first.** Its CPU build is as fast or faster than its GPU build
  on this machine, is a quarter of the download, has no shader warm-up, and stays above
  10× even on a single thread. It is the configuration that works on *any* machine.
- **The Whisper family needs the GPU.** Whisper always processes a fixed 30-second
  window, however short the utterance, so short live utterances are expensive: on CPU it
  falls below real time; on this integrated GPU it clears the bar at 3–7×, with limited
  headroom.
- **Parakeet is where the GPU shines: 18–41× on an integrated GPU.** It produces its
  whole transcript in one pass instead of one token at a time, which suits a GPU. It
  also managed 11× on the CPU. It is the strongest candidate for the "Accurate" tier, at
  the cost of a 612–643 MB download.

This reshapes the tiers in the guide: **Fast = Moonshine Base on CPU (universal);
Accurate = Parakeet on GPU (where the self-test passes)**, with Whisper Base as the
middle option for GPUs that fail Parakeet.

### 4.3 The engine must open the GPU device itself

Left to choose its own GPU, the runtime ignored the request for the fallback adapter and
ran on the Intel GPU while the page reported the software renderer. The benchmark caught
this because every run records the GPU its session actually used. The engine now opens
the `GPUDevice` and hands it to the runtime, so the GPU that was checked is the GPU that
is used — which is also what gives access to device-loss events.

### 4.4 A GPU-process crash is possible and must be survived

During testing, Chrome's GPU process crashed repeatedly (exit code 133) on first GPU
inference. Chrome then refused to hand out the GPU, first to the worker and, after
further attempts, to the whole page. **The cause was the test harness, not the GPU:** the
scratch volume is RAM-backed and had filled with 12 GB of duplicated model downloads, so
Chrome's shader cache could not write. After clearing it, stock Chrome passed seven
consecutive runs.

It still matters, because a viewer with a full disk would hit the same thing, and a GPU
process crash interrupts video in every tab:

- The ladder already degrades correctly (it selected the CPU each time).
- Phase 3 needs a **crash guard**: record "GPU attempt in progress" before the first GPU
  inference and clear it afterwards. If the marker is still set at the next start, the
  previous attempt took the GPU process down — do not retry automatically; use the CPU
  and tell the viewer.
- Never retry a failed GPU configuration in the same browser session.

### 4.5 Smaller findings

- **Software renderers are recognised by name as well as by flag**, since Linux's
  `llvmpipe` reaches Chrome as an ordinary device. Verified against SwiftShader.
- **CPU threads help less than expected** for Moonshine (1 → 4 threads: 11× → 13–24×) and
  more for Whisper. Cross-origin isolation is therefore *nice to have*, not required, for
  the default tier — good news for spike S3.
- **Model caches are per origin.** A changing origin silently re-downloads everything;
  the extension's origin is fixed, so this only bit the test harness.
- **Moonshine Tiny is not accurate enough** to be offered.
- Adapter names are often blank in Chrome (`description: ""`); the UI must fall back to
  vendor and architecture ("Intel (xe-2lpg)").

## 5. What was built

| Path | What it is |
|---|---|
| `extension/engine/stt/gpu_probe.mjs` | Describes the GPU (vendor, software or hardware, 16-bit support, limits) and opens the device with raised limits. Never throws |
| `extension/engine/stt/models.mjs` | Model catalogue: configurations per model in measured preference order, with download sizes |
| `extension/engine/stt/stt_plan.mjs` | The ladder: which configurations to try, the pass/fail rule, the selection, and the viewer's Auto / GPU / CPU setting |
| `extension/engine/stt/benchmark.mjs`, `bench_worker.mjs` | Runs each configuration in a fresh worker and scores it |
| `extension/engine/stt/wer.mjs`, `audio_utils.mjs` | Transcript scoring (same rules as the backend's `compare_stt.py`) and pause-based segmentation |
| `extension/spike/stt_bench.html` | Benchmark page with a "Copy report" button |
| `extension/tests/stt_engine.test.mjs` | 54 unit tests for the logic above |
| `scripts/vendor_stt_runtime.py` | Downloads the pinned runtime with integrity checks (git-ignored output, ~28 MB) |
| `scripts/run_stt_bench.mjs` | Runs the benchmark unattended in a real Chrome and saves the report |
| `docs/spike-results/reports/*.json` | The raw reports behind every number here |

Run the tests and a benchmark:

```bash
python3 scripts/vendor_stt_runtime.py
node --test "extension/tests/*.test.mjs"
node scripts/run_stt_bench.mjs --headed --label my-machine
```

Without `--headed`, Chrome runs headless and has no GPU unless you add
`--chrome-flag=--enable-features=Vulkan --chrome-flag=--use-angle=vulkan
--chrome-flag=--enable-unsafe-webgpu` (Linux).

## 6. Collecting results from other GPUs

This is the open part of "a variety of GPUs". The benchmark page is self-contained, so
anyone with the repo can produce a comparable report in a few minutes:

1. `python3 scripts/vendor_stt_runtime.py`
2. `node scripts/run_stt_bench.mjs --headed --models moonshine-base,whisper-base.en,parakeet-ctc-0.6b --label <gpu-name>`
3. Send back `docs/spike-results/raw/<gpu-name>.json`.

Priority order, by how many viewers each represents and how different the driver stack is:

| Target | Why |
|---|---|
| NVIDIA discrete, Windows | Largest share of streaming viewers; D3D12 backend, untested |
| AMD integrated or discrete, Windows | Different driver; 16-bit behaviour unknown |
| Apple Silicon, macOS | Metal backend; the most common laptop GPU |
| Older Intel integrated (pre-2021), Windows | The weakest hardware likely to try this |
| Any machine with two GPUs | Checks the power-preference setting |

For each, the questions are the ones answered above: does the 16-bit Whisper encoder give
correct output, does Parakeet pass, and what is the speed on short utterances.

## 7. Not covered by this spike

- **Running inside the extension.** Everything ran from a local server. The extension
  needs `'wasm-unsafe-eval'` in its content security policy, and the worker has to
  coexist with tab capture in the offscreen document. That is spike S3; Chrome 157 would
  not load an unpacked extension from the command line, so it needs a manual check.
- **Real stream audio.** Both clips are clean speech. Music, crosstalk and game audio
  will raise error rates for every model; the backend's fixture set is the place to
  compare.
- **Sustained load.** Runs lasted under a minute. Thermal throttling over a two-hour
  stream, and contention with video decoding or a game on the same GPU, are unmeasured.
- **Neural voice-activity detection.** A simple pause detector stood in for it.
