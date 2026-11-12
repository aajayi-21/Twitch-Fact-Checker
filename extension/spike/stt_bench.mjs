/**
 * Developer benchmark page for in-browser speech-to-text (Phase 0, spike S1).
 *
 * Opens as an extension page (chrome-extension://…/spike/stt_bench.html) or
 * from any static server. It answers, for the machine it runs on: is there a
 * usable GPU, which model configurations work on it, how fast, and which one
 * the app would pick. "Copy report" produces a JSON blob to send back, which
 * is how results from GPUs we do not own get collected.
 *
 * URL parameters (all optional; used by scripts/run_stt_bench.mjs):
 *   auto=1             start immediately
 *   models=a,b         model ids (default: moonshine-base)
 *   variants=a,b       restrict to these variant ids
 *   clipUrl=…          16 kHz mono WAV to use instead of the built-in clip
 *   refUrl=…           text file holding that clip's transcript
 *   fallback=1         force the software GPU adapter
 *   allowSoftware=1    benchmark WebGPU on a software adapter
 *   power=low-power    GPU power preference
 *   threads=N          WASM thread count
 *   stopFirst=1        stop at the first passing configuration
 *   ownDevice=0        diagnostic: let the runtime create its own GPU device
 *
 * When a run finishes the report is on `window.__sttBenchReport` and the
 * document title becomes "bench:done" (or "bench:error").
 */

import {decodeWavPcm16} from "../engine/stt/audio_utils.mjs";
import {benchmarkModel, defaultThreadCount, describeEnvironment} from "../engine/stt/benchmark.mjs";
import {describeGpu, probeGpu} from "../engine/stt/gpu_probe.mjs";
import {STT_MODELS, getSttModel} from "../engine/stt/models.mjs";
import {SKIP_REASON, FAIL_REASON} from "../engine/stt/stt_plan.mjs";

const BUILT_IN_CLIP = Object.freeze({
  url: new URL("./assets/jfk_16k.wav", import.meta.url).href,
  // John F. Kennedy, inaugural address, 1961 (public domain).
  reference:
    "And so my fellow Americans, ask not what your country can do for you, " +
    "ask what you can do for your country.",
});

const DEFAULT_MODEL_IDS = Object.freeze(["moonshine-base"]);

const SKIP_COPY = Object.freeze({
  [SKIP_REASON.NO_GPU]: "skipped — no GPU available to the browser",
  [SKIP_REASON.SOFTWARE_GPU]: "skipped — only a software renderer was found",
  [SKIP_REASON.NO_SHADER_F16]: "skipped — this GPU lacks 16-bit shader support",
});

const FAIL_COPY = Object.freeze({
  [FAIL_REASON.ERROR]: "failed",
  [FAIL_REASON.WRONG_OUTPUT]: "wrong transcript",
  [FAIL_REASON.TOO_SLOW]: "too slow",
});

const STAGE_COPY = Object.freeze({
  probe: "checking the GPU",
  load: "loading the model",
  warmup: "warming up",
});

const params = new URLSearchParams(location.search);
const element = (id) => document.getElementById(id);

/** @param {string} tag @param {object} [props] @param {(Node|string)[]} [children] */
const make = (tag, props = {}, children = []) => {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children);
  return node;
};

const formatSeconds = (ms) => (Number.isFinite(ms) ? `${(ms / 1000).toFixed(1)} s` : "—");
const formatSpeed = (rtf) => (Number.isFinite(rtf) && rtf > 0 ? `${(1 / rtf).toFixed(1)}×` : "—");
const formatPercent = (ratio) => (Number.isFinite(ratio) ? `${(ratio * 100).toFixed(0)}%` : "—");

const setStatus = (text) => {
  element("status").textContent = text;
};

const renderMachine = async () => {
  const probe = await probeGpu();
  const rows = [
    ["GPU", describeGpu(probe)],
    ["WebGPU", probe.webgpuAvailable ? "available" : "not available in this browser"],
    ["Usable for speech", probe.usable ? "yes" : `no (${probe.reason})`],
    ["16-bit shaders", probe.shaderF16 ? "yes" : "no"],
    ["Vendor / architecture", [probe.vendor, probe.architecture].filter(Boolean).join(" / ") || "—"],
    [
      "Largest GPU buffer",
      probe.limits.maxBufferSize ? `${Math.round(probe.limits.maxBufferSize / 2 ** 20)} MB` : "—",
    ],
    ["CPU threads for fallback", `${defaultThreadCount()} of ${navigator.hardwareConcurrency ?? "?"}`],
    ["Cross-origin isolated", globalThis.crossOriginIsolated ? "yes" : "no (CPU fallback is single-threaded)"],
    ["Browser", navigator.userAgent],
  ];
  element("machine").replaceChildren(
    ...rows.flatMap(([term, value]) => [make("dt", {textContent: term}), make("dd", {textContent: value})])
  );
};

const renderModelChoices = () => {
  const requested = params.get("models")?.split(",").filter(Boolean) ?? DEFAULT_MODEL_IDS;
  element("model-choices").replaceChildren(
    ...STT_MODELS.map((model) =>
      make("label", {}, [
        make("input", {
          type: "checkbox",
          name: "model",
          value: model.id,
          checked: requested.includes(model.id),
        }),
        `${model.label} (${model.tier})`,
      ])
    )
  );
};

const selectedModelIds = () =>
  [...document.querySelectorAll('input[name="model"]:checked')].map((input) => input.value);

/**
 * Resolve the clip to benchmark: explicit URL parameters, the user's file, or
 * the built-in clip.
 *
 * @returns {Promise<{samples: Float32Array, referenceText: string|null, label: string}>}
 */
const loadClip = async () => {
  const fetchBuffer = async (url) => {
    const response = await fetch(url);
    if (!response.ok) {
      throw new Error(`Could not load ${url} (HTTP ${response.status}).`);
    }
    return response;
  };

  const clipUrl = params.get("clipUrl");
  if (clipUrl) {
    const refUrl = params.get("refUrl");
    return {
      samples: decodeWavPcm16(await (await fetchBuffer(clipUrl)).arrayBuffer()),
      referenceText: refUrl ? (await (await fetchBuffer(refUrl)).text()).trim() : null,
      label: clipUrl,
    };
  }
  if (document.querySelector('input[name="clip"]:checked').value === "file") {
    const [file] = element("clip-file").files;
    if (!file) {
      throw new Error("Choose a WAV file first.");
    }
    return {
      samples: decodeWavPcm16(await file.arrayBuffer()),
      referenceText: element("clip-reference").value.trim() || null,
      label: file.name,
    };
  }
  return {
    samples: decodeWavPcm16(await (await fetchBuffer(BUILT_IN_CLIP.url)).arrayBuffer()),
    referenceText: BUILT_IN_CLIP.reference,
    label: "built-in clip",
  };
};

/** @param {object} modelReport */
const appendModelRows = (modelReport) => {
  const model = getSttModel(modelReport.modelId);
  const body = element("results");
  for (const skip of modelReport.skipped) {
    body.append(
      make("tr", {}, [
        make("td", {textContent: model.label}),
        make("td", {textContent: skip.variantId}),
        make("td", {className: "skip", textContent: SKIP_COPY[skip.reason] ?? skip.reason, colSpan: 7}),
      ])
    );
  }
  for (const run of modelReport.runs) {
    const chosen = modelReport.selected?.variantId === run.variantId;
    const resultText = run.verdict.pass
      ? chosen
        ? "pass — selected"
        : "pass"
      : FAIL_COPY[run.verdict.reason] ?? run.verdict.reason;
    body.append(
      make("tr", {className: chosen ? "chosen" : ""}, [
        make("td", {textContent: model.label}),
        make("td", {textContent: run.variantId}),
        make("td", {className: run.verdict.pass ? "pass" : "fail", textContent: resultText}),
        make("td", {className: "num", textContent: formatSpeed(run.realTimeFactor)}),
        make("td", {className: "num", textContent: formatPercent(run.wordErrorRate)}),
        make("td", {className: "num", textContent: formatSeconds(run.loadMs)}),
        make("td", {className: "num", textContent: formatSeconds(run.warmupMs)}),
        make("td", {className: "num", textContent: run.downloadMb ? `${run.downloadMb} MB` : "—"}),
        make("td", {className: "wrap", textContent: run.ok ? run.text : run.error}),
      ])
    );
  }
};

/** @param {object[]} modelReports */
const renderSummary = (modelReports) => {
  element("summary").replaceChildren(
    ...modelReports.map((modelReport) => {
      const model = getSttModel(modelReport.modelId);
      const {selected} = modelReport;
      if (!selected) {
        return make("p", {
          className: "fail",
          textContent: `${model.label}: no configuration passed on this machine.`,
        });
      }
      const run = modelReport.runs.find((candidate) => candidate.variantId === selected.variantId);
      const where = selected.device === "webgpu" ? `GPU (${modelReport.gpu.label})` : "CPU";
      return make("p", {
        className: "pass",
        textContent: `${model.label}: use ${selected.variantId} on the ${where} — ${formatSpeed(run.realTimeFactor)} real time.`,
      });
    })
  );
};

const describeProgress = ({modelId, variantId, stage, detail}) => {
  const prefix = `${getSttModel(modelId).label} · ${variantId}: `;
  if (stage === "download") {
    return `${prefix}downloading ${detail.percent}% of ${detail.totalMb.toFixed(0)} MB`;
  }
  if (stage === "transcribe") {
    return `${prefix}transcribing segment ${detail.segment} of ${detail.of}`;
  }
  return `${prefix}${STAGE_COPY[stage] ?? stage}`;
};

const runBenchmark = async () => {
  const runButton = element("run-button");
  const copyButton = element("copy-button");
  runButton.disabled = true;
  copyButton.disabled = true;
  element("results").replaceChildren();
  element("summary").replaceChildren();
  window.__sttBenchReport = null;
  document.title = "bench:running";

  try {
    const modelIds = selectedModelIds();
    if (modelIds.length === 0) {
      throw new Error("Choose at least one model.");
    }
    setStatus("Loading audio…");
    const clip = await loadClip();
    const options = {
      referenceText: clip.referenceText,
      forceFallbackAdapter: element("opt-fallback").checked,
      allowSoftwareGpu: element("opt-allow-software").checked,
      powerPreference: element("opt-low-power").checked ? "low-power" : "high-performance",
      stopAtFirstPass: element("opt-stop-first").checked,
      useOwnDevice: params.get("ownDevice") !== "0",
      onlyVariants: params.get("variants")?.split(",").filter(Boolean) ?? null,
      numThreads: Number(params.get("threads")) || defaultThreadCount(),
      onProgress: (update) => {
        if (update.stage !== "done") {
          setStatus(describeProgress(update));
        }
      },
    };

    const modelReports = [];
    for (const modelId of modelIds) {
      const modelReport = await benchmarkModel(getSttModel(modelId), clip.samples, options);
      modelReports.push(modelReport);
      appendModelRows(modelReport);
    }
    renderSummary(modelReports);

    window.__sttBenchReport = {
      schema: 1,
      environment: describeEnvironment(),
      clip: clip.label,
      options: {
        forceFallbackAdapter: options.forceFallbackAdapter,
        allowSoftwareGpu: options.allowSoftwareGpu,
        powerPreference: options.powerPreference,
        numThreads: options.numThreads,
        useOwnDevice: options.useOwnDevice,
      },
      models: modelReports,
    };
    copyButton.disabled = false;
    setStatus("Finished.");
    document.title = "bench:done";
  } catch (error) {
    console.error("[stt-bench] benchmark failed:", error);
    window.__sttBenchReport = {schema: 1, error: String(error?.message ?? error)};
    setStatus(`Could not run the benchmark: ${error?.message ?? error}`);
    document.title = "bench:error";
  } finally {
    runButton.disabled = false;
  }
};

const copyReport = async () => {
  const json = JSON.stringify(window.__sttBenchReport, null, 2);
  try {
    await navigator.clipboard.writeText(json);
    setStatus("Report copied to the clipboard.");
  } catch (error) {
    console.error("[stt-bench] clipboard write failed:", error);
    setStatus("Could not copy automatically — the report is in the console.");
    console.log(json);
  }
};

const applyUrlOptions = () => {
  element("opt-fallback").checked = params.get("fallback") === "1";
  element("opt-allow-software").checked = params.get("allowSoftware") === "1";
  element("opt-low-power").checked = params.get("power") === "low-power";
  element("opt-stop-first").checked = params.get("stopFirst") === "1";
};

const wireControls = () => {
  for (const radio of document.querySelectorAll('input[name="clip"]')) {
    radio.addEventListener("change", () => {
      const useFile = radio.value === "file" && radio.checked;
      element("clip-file").hidden = !useFile;
      element("clip-reference").hidden = !useFile;
    });
  }
  element("run-button").addEventListener("click", runBenchmark);
  element("copy-button").addEventListener("click", copyReport);
};

renderModelChoices();
applyUrlOptions();
wireControls();
await renderMachine();
if (params.get("auto") === "1") {
  await runBenchmark();
}
