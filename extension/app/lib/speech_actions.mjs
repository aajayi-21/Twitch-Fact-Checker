/**
 * In-browser speech, as the app's screens see it: the stored record, kept
 * fresh, plus the actions that change it — choosing an engine and model,
 * running the on-device test, and deleting downloaded models.
 *
 * The test is the only place a model configuration is ever approved for
 * this machine (engine/stt/stt_plan.mjs): it downloads the model if needed,
 * transcribes a short bundled clip with a known transcript on each
 * candidate configuration in turn, and keeps the first that is both right
 * and fast enough.
 */

import {decodeWavPcm16} from "../../engine/stt/audio_utils.mjs";
import {benchmarkModel} from "../../engine/stt/benchmark.mjs";
import {gpuCacheKey, probeGpu} from "../../engine/stt/gpu_probe.mjs";
import {getSttModel} from "../../engine/stt/models.mjs";
import {
  SPEECH_STORAGE_KEY,
  beginGpuAttempt,
  clearForRetest,
  endGpuAttempt,
  environmentKey,
  normalizeSpeechState,
  planBrowserSpeech,
  resultFromBenchmark,
} from "../../engine/stt/speech_setup.mjs";
import {
  browserMajorVersion,
  loadGuardedSpeechState,
  loadRuntimeVersion,
  updateSpeechState,
  withGpuSpeechLock,
} from "../../engine/stt/speech_store.mjs";
import {useEffect, useState} from "./html.mjs";

// Promise-safe API namespace (see shared/capabilities.js § namespace idiom).
const chrome = globalThis.browser ?? globalThis.chrome;

const SELF_TEST_CLIP_URL = new URL("../../engine/stt/assets/selftest_16k.wav", import.meta.url);
// John F. Kennedy's 1961 inaugural address (public domain), eleven seconds.
const SELF_TEST_REFERENCE =
  "And so my fellow Americans, ask not what your country can do for you, " +
  "ask what you can do for your country.";

// Where the speech library keeps downloaded model files.
const MODEL_CACHE_NAME = "transformers-cache";

/**
 * @typedef {object} SpeechView
 * @property {boolean} loaded
 * @property {import("../../engine/stt/speech_setup.mjs").SpeechState} state
 * @property {string|null} runtimeVersion - null: the runtime is not installed
 * @property {import("../../engine/stt/speech_setup.mjs").SpeechPlan|null} plan
 * @property {boolean} guardTripped - the crash guard blocked the GPU just now
 * @property {{stage: string, variantId: string|null, percent: number|null,
 *   loadedMb: number|null, totalMb: number|null}|null} progress - a test is running
 * @property {string|null} testError
 * @property {number|null} modelBytes - downloaded models on disk
 */

/** @type {SpeechView} */
let view = {
  loaded: false,
  state: normalizeSpeechState(null),
  runtimeVersion: null,
  plan: null,
  guardTripped: false,
  progress: null,
  testError: null,
  modelBytes: null,
};

const listeners = new Set();

const setView = (patch) => {
  view = {...view, ...patch};
  listeners.forEach((listener) => listener());
};

const planFor = (state, runtimeVersion) =>
  planBrowserSpeech(state, {
    browserMajor: browserMajorVersion(),
    runtimeVersion: runtimeVersion ?? "missing",
  });

/** Subscribe a component to the speech record. */
export const useSpeech = () => {
  const [, forceRender] = useState(0);
  const rendered = view;
  useEffect(() => {
    const listener = () => forceRender((count) => count + 1);
    listeners.add(listener);
    if (view !== rendered) {
      listener(); // changed between render and effect (see app_store.mjs)
    }
    return () => listeners.delete(listener);
  }, []);
  return view;
};

/** How much disk the downloaded models take, or null when unknown. */
const measureModelBytes = async () => {
  try {
    if (!(await caches.has(MODEL_CACHE_NAME))) {
      return 0;
    }
    const estimate = await navigator.storage.estimate();
    return estimate.usageDetails?.caches ?? null;
  } catch {
    return null;
  }
};

/** Load the record and keep it fresh. Call once at startup. */
export const initSpeech = async () => {
  const [{state, tripped}, runtimeVersion, modelBytes] = await Promise.all([
    loadGuardedSpeechState(),
    loadRuntimeVersion(),
    measureModelBytes(),
  ]);
  setView({
    loaded: true,
    state,
    runtimeVersion,
    plan: planFor(state, runtimeVersion),
    guardTripped: tripped,
    modelBytes,
  });
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && changes[SPEECH_STORAGE_KEY]) {
      // The service worker changed it (a session's GPU attempt or failure).
      const fresh = normalizeSpeechState(changes[SPEECH_STORAGE_KEY].newValue);
      setView({state: fresh, plan: planFor(fresh, view.runtimeVersion)});
    }
  });
};

const store = async (change) => {
  const state = await updateSpeechState(change);
  setView({state, plan: planFor(state, view.runtimeVersion)});
  return state;
};

/**
 * Change the viewer's speech choices. Takes effect at the next Start.
 *
 * @param {Partial<import("../../engine/stt/speech_setup.mjs").SpeechState["settings"]>} patch
 */
export const updateSpeechSettings = (patch) =>
  store((state) => ({...state, settings: {...state.settings, ...patch}}));

/**
 * Download (if needed) and test the chosen model on this device, and store
 * what was found. Never throws: a failure is shown as `testError`.
 *
 * @param {object} [options]
 * @param {boolean} [options.withoutGpu] - also switch the device setting to
 *   "CPU only" first (the safe way out after a GPU crash)
 */
export const runSpeechTest = async ({withoutGpu = false} = {}) => {
  if (view.progress) {
    return;
  }
  setView({progress: {stage: "preparing", variantId: null, percent: null}, testError: null, guardTripped: false});
  try {
    if (withoutGpu) {
      await updateSpeechSettings({devicePreference: "cpu"});
    }
    const {settings} = view.state;
    const model = getSttModel(settings.modelId);
    await store((state) => clearForRetest(state, model.id));

    const clip = await fetch(SELF_TEST_CLIP_URL);
    if (!clip.ok) {
      throw new Error("The built-in test clip is missing from this copy of the extension.");
    }
    const samples = decodeWavPcm16(await clip.arrayBuffer());
    const probe = await probeGpu({powerPreference: settings.powerPreference});
    const key = environmentKey({
      model,
      gpuCacheKey: gpuCacheKey(probe),
      browserMajor: browserMajorVersion(),
      runtimeVersion: view.runtimeVersion ?? "missing",
      devicePreference: settings.devicePreference,
      powerPreference: settings.powerPreference,
    });
    const mayUseGpu = settings.devicePreference !== "cpu";

    const report = await withGpuSpeechLock(async () => {
      if (mayUseGpu) {
        // Crash guard: on disk before any GPU configuration is tried.
        await updateSpeechState((state) =>
          beginGpuAttempt(state, {
            context: "selftest",
            variantId: model.id,
            at: new Date().toISOString(),
          })
        );
      }
      try {
        return await benchmarkModel(model, samples, {
          referenceText: SELF_TEST_REFERENCE,
          devicePreference: settings.devicePreference,
          powerPreference: settings.powerPreference,
          stopAtFirstPass: true,
          onProgress: ({variantId, stage, detail}) => {
            if (stage === "done") {
              return;
            }
            setView({
              progress: {
                stage,
                variantId,
                percent: stage === "download" ? (detail?.percent ?? null) : null,
                loadedMb: stage === "download" ? (detail?.loadedMb ?? null) : null,
                totalMb: stage === "download" ? (detail?.totalMb ?? null) : null,
              },
            });
          },
        });
      } finally {
        if (mayUseGpu) {
          await updateSpeechState(endGpuAttempt);
        }
      }
    });

    await store((state) => ({
      ...state,
      results: {
        ...state.results,
        [model.id]: resultFromBenchmark(report, key, new Date().toISOString()),
      },
    }));
  } catch (error) {
    console.error("[fact-checker] speech test failed:", error);
    setView({testError: String(error?.message ?? error)});
  } finally {
    setView({progress: null, modelBytes: await measureModelBytes()});
  }
};

/**
 * Delete every downloaded model. The tests are forgotten with them, since
 * the next test has to download again anyway.
 */
export const deleteSpeechModels = async () => {
  await caches.delete(MODEL_CACHE_NAME);
  await store((state) => ({...state, results: {}}));
  setView({modelBytes: await measureModelBytes()});
};
