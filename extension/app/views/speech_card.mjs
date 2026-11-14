/**
 * Settings → Speech recognition: where the stream's audio is turned into
 * text — the local backend, or this browser — and, for the browser, the
 * model, the device, and the one-time download and test.
 *
 * Nothing here is taken on trust: a model configuration is offered for use
 * only after it has transcribed a known clip correctly and quickly enough
 * on this machine. The card says what was tried and what happened.
 */

import {SPEECH_TIERS, STT_VARIANTS, getSttModel} from "../../engine/stt/models.mjs";
import {
  NOT_READY,
  SPEECH_ENGINE,
  deviceLabel,
  speedLabel,
} from "../../engine/stt/speech_setup.mjs";
import {FAIL_REASON, SKIP_REASON} from "../../engine/stt/stt_plan.mjs";
import {Button, Card, ConfirmButton, Notice, Segmented, SettingRow} from "../components.mjs";
import {useAppState} from "../lib/app_store.mjs";
import {ENGINE_MODE, useEngine} from "../lib/engine_actions.mjs";
import {html} from "../lib/html.mjs";
import {
  deleteSpeechModels,
  runSpeechTest,
  updateSpeechSettings,
  useSpeech,
} from "../lib/speech_actions.mjs";

const ENGINE_OPTIONS = Object.freeze([
  {
    value: SPEECH_ENGINE.BACKEND,
    label: "Local backend",
    hint: "The backend's own speech model",
  },
  {
    value: SPEECH_ENGINE.BROWSER,
    label: "This browser",
    hint: "A model that runs inside the extension",
  },
]);

const DEVICE_OPTIONS = Object.freeze([
  ["auto", "Automatic"],
  ["gpu", "GPU only"],
  ["cpu", "CPU only"],
]);

const WHY_FAILED = Object.freeze({
  [FAIL_REASON.ERROR]: "did not run",
  [FAIL_REASON.WRONG_OUTPUT]: "ran, but the text was wrong",
  [FAIL_REASON.TOO_SLOW]: "right, but too slow for live speech",
});

const WHY_SKIPPED = Object.freeze({
  [SKIP_REASON.NO_GPU]: "the browser offers no GPU",
  [SKIP_REASON.SOFTWARE_GPU]: "the only GPU is a software renderer",
  [SKIP_REASON.NO_SHADER_F16]: "the GPU lacks 16-bit shader support",
  [SKIP_REASON.USER_PREFERENCE]: "excluded by the device setting",
});

/** "GPU (16-bit encoder)" — a configuration in words. */
const variantName = (variantId) => {
  const variant = STT_VARIANTS[variantId];
  const precision = {
    "webgpu-mixed": "16-bit encoder",
    "webgpu-fp16": "16-bit",
    "webgpu-fp32": "32-bit",
    "webgpu-q4f16": "4-bit weights, 16-bit maths",
    "webgpu-q4": "4-bit weights",
    "wasm-q8": "8-bit",
  }[variantId];
  return `${deviceLabel(variant?.device)}${precision ? ` (${precision})` : ""}`;
};

/**
 * The download a viewer should expect for a model: its first-choice build.
 * A different build is fetched only if that one fails the test here.
 */
const expectedDownload = (model) => `about ${model.downloadMb[model.variants[0]]} MB`;

const megabytes = (bytes) => `${Math.round(bytes / 1e6)} MB`;

const describeProgress = (progress) => {
  const on = progress.variantId ? ` — ${variantName(progress.variantId)}` : "";
  // A model that is already on disk reports itself as 100 % at once.
  if (progress.stage === "download" && progress.percent < 100) {
    const amount =
      progress.totalMb > 0
        ? ` ${progress.percent}% (${Math.round(progress.loadedMb)} of ${Math.round(progress.totalMb)} MB)`
        : "";
    return `Downloading the model${amount}${on}`;
  }
  if (progress.stage === "load" || progress.stage === "download") {
    return `Loading the model${on}`;
  }
  if (progress.stage === "warmup" || progress.stage === "transcribe") {
    return `Testing${on}`;
  }
  return "Getting ready…";
};

const TestResult = ({result, model}) => {
  if (!result) {
    return null;
  }
  const attempts = html`
    ${result.runs.length + result.skipped.length > 1 || !result.selected
      ? html`<ul class="speech-runs">
          ${result.runs.map(
            (run) => html`
              <li key=${run.variantId}>
                <span>${variantName(run.variantId)}</span>
                <span class="muted">
                  ${run.pass
                    ? `works · ${speedLabel(run.realTimeFactor)}`
                    : `${WHY_FAILED[run.reason] ?? "failed"}${run.error ? ` (${run.error})` : ""}`}
                </span>
              </li>
            `
          )}
          ${result.skipped.map(
            (skip) => html`
              <li key=${skip.variantId}>
                <span>${variantName(skip.variantId)}</span>
                <span class="muted">not tried — ${WHY_SKIPPED[skip.reason] ?? skip.reason}</span>
              </li>
            `
          )}
        </ul>`
      : null}
  `;
  if (!result.selected) {
    return html`
      <${Notice} tone="warn">
        ${model.label} cannot transcribe live speech on this device. Try the Fast model, or keep
        using the local backend.
      <//>
      ${attempts}
    `;
  }
  const errors = Math.round((result.selected.wordErrorRate ?? 0) * 100);
  return html`
    <${Notice} tone="success">
      Works on this device: ${deviceLabel(result.selected.device)},
      ${" "}${speedLabel(result.selected.realTimeFactor)},
      ${" "}${errors === 0 ? "every word of the test clip right" : `${100 - errors}% of the test clip's words right`}.
      ${result.selected.device === "webgpu" && result.gpuLabel ? ` GPU: ${result.gpuLabel}.` : ""}
    <//>
    ${attempts}
  `;
};

export const SpeechCard = () => {
  const speech = useSpeech();
  const health = useAppState((app) => app.health);
  // With the browser engine there is no backend to transcribe, so speech
  // runs here whatever this card's own switch says, and the switch is hidden.
  const browserEngine = useEngine().state.mode === ENGINE_MODE.BROWSER;
  const {state, plan, progress} = speech;
  const {settings} = state;
  const inBrowser = browserEngine || settings.engine === SPEECH_ENGINE.BROWSER;
  const model = getSttModel(settings.modelId);
  const result = state.results[model.id] ?? null;
  const testing = progress !== null;
  const blocked = Boolean(state.gpuBlocked);
  const runtimeMissing = speech.loaded && speech.runtimeVersion === null;

  return html`
    <${Card} title="Speech recognition" id="section-speech">
      ${browserEngine
        ? html`<p class="setting-help">
            Speech is turned into text inside this browser, by a model that runs on this
            computer. No audio leaves it.
          </p>`
        : html`<${SettingRow}
            label="Where speech is turned into text"
            help="Either way the audio stays on this computer. Applies the next time you press Start."
            stacked
          >
            <${Segmented}
              name="speech-engine"
              label="Speech engine"
              options=${ENGINE_OPTIONS}
              value=${settings.engine}
              onChange=${(engine) => updateSpeechSettings({engine})}
            />
          <//>`}

      ${!inBrowser && health?.accepts_audio === false
        ? html`<${Notice} tone="warn">
            This backend was started without a speech model, so it cannot transcribe. Choose
            “This browser”, or restart the backend with a model.
          <//>`
        : null}

      ${inBrowser
        ? html`
            ${runtimeMissing
              ? html`<${Notice} tone="error">
                  The speech runtime is not in this copy of the extension. From the project folder
                  run <code>python scripts/vendor_stt_runtime.py</code>, then reload the extension.
                <//>`
              : null}
            ${!browserEngine && health && health.accepts_transcripts !== true
              ? html`<${Notice} tone="warn">
                  The running backend is too old to accept text from the browser. Update and restart
                  it before using this.
                <//>`
              : null}

            <${SettingRow} label="Model" stacked>
              <div class="choice-grid" role="radiogroup" aria-label="Speech model">
                ${SPEECH_TIERS.map((tier) => {
                  const tierModel = getSttModel(tier.modelId);
                  return html`
                    <label class="choice" key=${tier.modelId}>
                      <input
                        type="radio"
                        name="speech-model"
                        value=${tier.modelId}
                        checked=${settings.modelId === tier.modelId}
                        disabled=${testing}
                        onChange=${() => updateSpeechSettings({modelId: tier.modelId})}
                      />
                      <span class="choice-body">
                        <span class="choice-title">${tier.label}</span>
                        <span class="choice-text">${tier.summary}</span>
                        <span class="choice-meta">
                          ${tierModel.label} · ${expectedDownload(tierModel)} download
                        </span>
                      </span>
                    </label>
                  `;
                })}
              </div>
            <//>

            <${SettingRow}
              label="Run on"
              help="Automatic tries each model's best option first and keeps the first that passes the test."
              htmlFor="speech-device"
            >
              <select
                id="speech-device"
                value=${settings.devicePreference}
                disabled=${testing}
                onChange=${(event) =>
                  updateSpeechSettings({devicePreference: event.currentTarget.value})}
              >
                ${DEVICE_OPTIONS.map(
                  ([value, label]) => html`<option key=${value} value=${value}>${label}</option>`
                )}
              </select>
            <//>
            ${settings.devicePreference === "cpu"
              ? null
              : html`<${SettingRow}
                  label="On a laptop with two GPUs, use"
                  htmlFor="speech-power"
                >
                  <select
                    id="speech-power"
                    value=${settings.powerPreference}
                    disabled=${testing}
                    onChange=${(event) =>
                      updateSpeechSettings({powerPreference: event.currentTarget.value})}
                  >
                    <option value="high-performance">The faster one</option>
                    <option value="low-power">The power-saving one</option>
                  </select>
                <//>`}

            <${SettingRow}
              label="Download and test"
              help="Downloads the model once, then transcribes an eleven-second clip to confirm it is right and fast enough here. Needed again after a browser update or a change above."
              stacked
            >
              ${blocked
                ? html`<${Notice} tone="warn">
                    The GPU has been switched off for speech: ${state.gpuBlocked.reason} Testing again
                    will try it again; “Test without the GPU” will not.
                  <//>`
                : null}
              <div class="form-actions">
                <${Button}
                  variant="primary"
                  busy=${testing}
                  disabled=${runtimeMissing}
                  onClick=${() => runSpeechTest()}
                >
                  ${testing ? describeProgress(progress) : result ? "Test again" : "Download and test"}
                <//>
                ${blocked && !testing
                  ? html`<${Button} disabled=${runtimeMissing} onClick=${() => runSpeechTest({withoutGpu: true})}>
                      Test without the GPU
                    <//>`
                  : null}
              </div>
              ${progress?.stage === "download" && progress.percent !== null
                ? html`<progress class="speech-progress" max="100" value=${progress.percent}>
                    ${progress.percent}%
                  </progress>`
                : null}
              ${speech.testError
                ? html`<${Notice} tone="error">The test could not finish: ${speech.testError}<//>`
                : null}
              ${!testing && plan && !plan.ready && plan.reason === NOT_READY.STALE
                ? html`<${Notice} tone="info">
                    The last test was made before a browser or extension update, or with different
                    choices above. Test again before the next stream.
                  <//>`
                : null}
              ${!testing && !(plan && plan.reason === NOT_READY.STALE)
                ? html`<${TestResult} result=${result} model=${model} />`
                : null}
            <//>

            <${SettingRow}
              label="Downloaded models"
              help=${speech.modelBytes === null
                ? "Kept in this browser so they download only once."
                : speech.modelBytes === 0
                  ? "None yet."
                  : `About ${megabytes(speech.modelBytes)} on disk, kept so they download only once.`}
            >
              <${ConfirmButton}
                disabled=${testing || !speech.modelBytes}
                confirmLabel="Delete the models?"
                onConfirm=${deleteSpeechModels}
              >
                Delete
              <//>
            <//>
          `
        : null}
    <//>
  `;
};
