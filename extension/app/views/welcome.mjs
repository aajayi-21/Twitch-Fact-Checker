/**
 * First-run welcome flow: what the app does and touches, where it should
 * run, and then the setup that choice needs — ending in a real test check.
 *
 *   This browser:   API key → speech model → try it
 *   Local backend:  start the backend → API key → try it
 *
 * Each step can be revisited; nothing is captured or sent until the person
 * has read the first step and agreed.
 */

import {Button, Card, ExternalLink, Notice, Segmented, StatusPill} from "../components.mjs";
import {
  BACKEND_ERROR,
  completeOnboarding,
  recordConsent,
  refreshBackend,
  runTestCheck,
  useAppState,
} from "../lib/app_store.mjs";
import {HAS_OFFSCREEN_CAPTURE} from "../../shared/capabilities.js";
import {
  ENGINE_MODE,
  connectProvider,
  runBrowserTestCheck,
  setEngineMode,
  useEngine,
} from "../lib/engine_actions.mjs";
import {BROWSER_PROVIDERS, describeProviderError} from "../lib/engine_model.mjs";
import {html, useEffect, useState} from "../lib/html.mjs";
import {ROUTES} from "../lib/routes.mjs";
import {KEY_PROVIDERS, describeConnection, describeStages} from "../lib/setup_model.mjs";
import {useSpeech} from "../lib/speech_actions.mjs";
import {ENGINE_OPTIONS} from "./engine_card.mjs";
import {KeyForm} from "./key_form.mjs";
import {browserKeyHelp} from "./providers_browser.mjs";
import {SpeechCard} from "./speech_card.mjs";

const STEPS_BY_MODE = Object.freeze({
  [ENGINE_MODE.BROWSER]: ["How it works", "Where it runs", "API key", "Speech", "Try it"],
  [ENGINE_MODE.COMPANION]: ["How it works", "Where it runs", "Local backend", "API key", "Try it"],
});

const SAMPLE_CLAIM = "The Eiffel Tower is 450 meters tall.";

const HowItWorksStep = ({onNext, alreadyAgreed}) => html`
  <${Card} title="What this extension does">
    <p class="lede">
      While you watch a stream, it listens for factual claims and shows a sourced verdict —
      true, false, misleading or unverified — over the player.
    </p>
    <ul class="disclosure">
      <li>
        <strong>It listens only when you press Start</strong> on a stream tab, and stops when you
        press Stop or close the tab.
      </li>
      <li>
        <strong>Audio stays on this computer.</strong> It is turned into text here — inside the
        browser, or by the backend program if you choose to run one.
      </li>
      <li>
        <strong>Claims go to the AI provider whose key you add.</strong> Only the sentence being
        checked is sent, so the provider can search the web and judge it. You pay that
        provider directly for what you use.
      </li>
      <li>
        <strong>Your history stays local</strong>, in this browser. Nothing is sent to us; there
        is no account and no analytics.
      </li>
      <li>
        <strong>Verdicts are written by AI and can be wrong.</strong> Every one links its sources
        so you can check for yourself.
      </li>
    </ul>
    <div class="form-actions">
      <${Button} variant="primary" onClick=${onNext}>
        ${alreadyAgreed ? "Continue" : "I understand — continue"}
      <//>
    </div>
  <//>
`;

/** Choose the engine. New installs are steered to the one with no install. */
const EngineStep = ({onNext, onBack}) => {
  const engine = useEngine();
  const {onboarding} = useAppState();
  const [mode, setMode] = useState(() =>
    // Someone revisiting the guide keeps what they chose; a first run starts
    // on the browser engine where the browser can run it.
    onboarding.completedAt || !HAS_OFFSCREEN_CAPTURE ? engine.state.mode : ENGINE_MODE.BROWSER
  );
  const [saving, setSaving] = useState(false);
  const proceed = async () => {
    setSaving(true);
    try {
      await setEngineMode(mode);
      onNext();
    } finally {
      setSaving(false);
    }
  };
  return html`
    <${Card} title="Where should it run?">
      <p class="lede">
        The checker can run entirely inside this browser, or use a program on your computer.
        You can change this later under AI provider.
      </p>
      ${HAS_OFFSCREEN_CAPTURE
        ? html`<${Segmented}
            name="welcome-engine"
            label="Where it runs"
            options=${ENGINE_OPTIONS}
            value=${mode}
            onChange=${setMode}
          />`
        : html`<${Notice} tone="info">
            This browser can only use the local backend; running inside the browser needs Chrome
            or Edge.
          <//>`}
      <div class="form-actions">
        <${Button} variant="primary" onClick=${proceed} busy=${saving}>Continue<//>
        <${Button} variant="ghost" onClick=${onBack} disabled=${saving}>Back<//>
      </div>
    <//>
  `;
};

/** Browser engine: one key, checked with the provider and kept in this browser. */
const BrowserProviderStep = ({onNext, onBack}) => {
  const engine = useEngine();
  const [providerId, setProviderId] = useState(BROWSER_PROVIDERS[0].id);
  const [replacing, setReplacing] = useState(false);
  const provider = BROWSER_PROVIDERS.find((candidate) => candidate.id === providerId);
  const showForm = !engine.plan.ready || replacing;

  return html`
    <${Card} title="Add an API key">
      <p class="lede">
        The fact-checker needs an AI provider to find claims and check them against the web. You
        pay the provider directly; a typical check costs about a cent.
      </p>
      ${engine.plan.ready && !replacing
        ? html`
            <${Notice} tone="success">Already connected: ${engine.plan.summary}.<//>
            <div class="form-actions">
              <${Button} variant="primary" onClick=${onNext}>Continue<//>
              <${Button} onClick=${() => setReplacing(true)}>Add another key<//>
              <${Button} variant="ghost" onClick=${onBack}>Back<//>
            </div>
          `
        : null}
      ${showForm
        ? html`
            <div class="choice-grid" role="radiogroup" aria-label="AI provider">
              ${BROWSER_PROVIDERS.map(
                (candidate) => html`
                  <label class="choice" key=${candidate.id}>
                    <input
                      type="radio"
                      name="welcome-provider"
                      value=${candidate.id}
                      checked=${candidate.id === providerId}
                      onChange=${() => setProviderId(candidate.id)}
                    />
                    <span class="choice-body">
                      <span class="choice-title">${candidate.label}</span>
                      <span class="choice-text">${candidate.blurb}</span>
                      <span class="choice-meta">
                        ${engine.hints[candidate.id]
                          ? `Key ending ${engine.hints[candidate.id]}`
                          : candidate.verifiedLive
                            ? "No key added"
                            : "Not yet tested live"}
                      </span>
                    </span>
                  </label>
                `
              )}
            </div>
            <${KeyForm}
              key=${provider.id}
              provider=${provider}
              save=${connectProvider}
              describeError=${describeProviderError}
              help=${browserKeyHelp(provider)}
              onSaved=${() => {
                setReplacing(false);
                onNext();
              }}
              onCancel=${replacing ? () => setReplacing(false) : onBack}
            />
          `
        : null}
    <//>
  `;
};

/** Browser engine: download a speech model and prove it works here. */
const SpeechStep = ({onNext, onBack}) => {
  const speech = useSpeech();
  const ready = Boolean(speech.plan?.ready);
  return html`
    <${SpeechCard} />
    <div class="form-actions">
      <${Button} variant="primary" onClick=${onNext} disabled=${!ready || speech.progress !== null}>
        Continue
      <//>
      <${Button} variant="ghost" onClick=${onBack} disabled=${speech.progress !== null}>Back<//>
      ${ready
        ? null
        : html`<span class="muted">Download and test a model to continue.</span>`}
    </div>
  `;
};

const BackendStep = ({onNext, onBack}) => {
  const {backendState, health, settings} = useAppState();
  const up = backendState === "up";
  return html`
    <${Card}
      title="Start the local backend"
      aside=${html`<${StatusPill} state=${up ? "ok" : backendState === "checking" ? "busy" : "bad"}>
        ${up ? "Running" : backendState === "checking" ? "Checking…" : "Not running"}
      <//>`}
    >
      <p class="lede">
        Speech recognition and fact-checking currently run in a small program on your computer.
        Start it once and leave it running while you watch.
      </p>
      ${up
        ? html`<${Notice} tone="success">
            Connected to the backend${health?.server_version ? ` (version ${health.server_version})` : ""}.
          <//>`
        : html`
            <ol class="steps-list">
              <li>Open a terminal in the project folder.</li>
              <li>Run <code>./backend/run.sh</code></li>
              <li>Leave it running. This page notices by itself.</li>
            </ol>
            <p class="field-help">
              Looking for it at <code>${settings?.backendUrl}</code>. The first start downloads a
              speech model (about 170 MB), so it can take a minute.
            </p>
          `}
      <div class="form-actions">
        <${Button} variant="primary" onClick=${onNext} disabled=${!up}>Continue<//>
        ${up ? null : html`<${Button} onClick=${() => refreshBackend()}>Check again<//>`}
        <${Button} variant="ghost" onClick=${onBack}>Back<//>
      </div>
    <//>
  `;
};

const ProviderStep = ({onNext, onBack}) => {
  const {setup} = useAppState();
  const [providerId, setProviderId] = useState(KEY_PROVIDERS[0].id);
  const [replacing, setReplacing] = useState(false);
  const provider = KEY_PROVIDERS.find((candidate) => candidate.id === providerId);
  const summary = describeStages(setup);
  const showForm = !setup?.configured || replacing;

  return html`
    <${Card} title="Add an API key">
      <p class="lede">
        The fact-checker needs an AI provider to find claims and check them against the web.
        A typical check costs about a cent.
      </p>
      ${setup?.configured && !replacing
        ? html`
            <${Notice} tone="success">Already connected: ${summary}.<//>
            <div class="form-actions">
              <${Button} variant="primary" onClick=${onNext}>Continue<//>
              <${Button} onClick=${() => setReplacing(true)}>Use a different key<//>
              <${Button} variant="ghost" onClick=${onBack}>Back<//>
            </div>
          `
        : null}
      ${showForm
        ? html`
            <div class="choice-grid" role="radiogroup" aria-label="AI provider">
              ${KEY_PROVIDERS.map(
                (candidate) => html`
                  <label class="choice" key=${candidate.id}>
                    <input
                      type="radio"
                      name="welcome-provider"
                      value=${candidate.id}
                      checked=${candidate.id === providerId}
                      onChange=${() => setProviderId(candidate.id)}
                    />
                    <span class="choice-body">
                      <span class="choice-title">${candidate.label}</span>
                      <span class="choice-text">${candidate.blurb}</span>
                      <span class="choice-meta">${describeConnection(setup, candidate.id).detail}</span>
                    </span>
                  </label>
                `
              )}
            </div>
            <${KeyForm}
              key=${provider.id}
              provider=${provider}
              onSaved=${() => {
                setReplacing(false);
                onNext();
              }}
              onCancel=${replacing ? () => setReplacing(false) : onBack}
            />
            <p class="field-help">
              OpenAI and Gemini keys work with the built-in engine: go back two steps and choose
              “This browser”.
            </p>
          `
        : null}
    <//>
  `;
};

/** A verdict card in the style of the overlay popup. */
const VerdictPreview = ({verdict}) => html`
  <article class="verdict" data-label=${verdict.label}>
    <header class="verdict-header">
      <span class="verdict-label">${verdict.label}</span>
      <span class="verdict-claim">${verdict.claim}</span>
    </header>
    <p class="verdict-text">${verdict.explanation}</p>
    ${verdict.sources?.length
      ? html`<ul class="verdict-sources">
          ${verdict.sources.slice(0, 3).map(
            (source) => html`<li key=${source.url}>
              <${ExternalLink} href=${source.url}>${source.title || new URL(source.url).hostname}<//>
            </li>`
          )}
        </ul>`
      : null}
  </article>
`;

const describeTestError = (error) => {
  if (error?.name === "ProviderError") {
    return describeProviderError(error, "Your AI provider");
  }
  switch (error?.kind) {
    case BACKEND_ERROR.UNREACHABLE:
      return "The local backend stopped responding. Start it again and retry.";
    case BACKEND_ERROR.NOT_READY:
      return "The backend has no working API key yet. Go back and add one.";
    case BACKEND_ERROR.TIMEOUT:
      return "The check took too long. It may still work on a stream; try again if you like.";
    case BACKEND_ERROR.INVALID:
      return "This backend has its test endpoint switched off, so the sample check is unavailable. Everything else is ready.";
    default:
      return error?.message || "The sample check failed.";
  }
};

const TryItStep = ({onFinish, onBack, browserEngine}) => {
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);

  const run = async () => {
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      setResult(await (browserEngine ? runBrowserTestCheck : runTestCheck)(SAMPLE_CLAIM));
    } catch (testError) {
      setError(describeTestError(testError));
    } finally {
      setRunning(false);
    }
  };

  return html`
    <${Card} title="Try a real check">
      <p class="lede">
        Run one sentence through the whole chain — the same claim detection and web search a
        stream gets. This makes one real request to your provider (about a cent).
      </p>
      <blockquote class="sample">“${SAMPLE_CLAIM}”</blockquote>
      ${result?.verdicts?.length
        ? result.verdicts.map((verdict) => html`<${VerdictPreview} key=${verdict.id} verdict=${verdict} />`)
        : null}
      ${result && !result.verdicts?.length
        ? html`<${Notice} tone="warn">
            The check ran, but no verdict came back for this sentence. That can happen; it does
            not mean setup failed.
          <//>`
        : null}
      ${error ? html`<${Notice} tone="error">${error}<//>` : null}
      <div class="form-actions">
        ${result
          ? html`<${Button} variant="primary" onClick=${onFinish}>Finish<//>`
          : html`<${Button} variant="primary" onClick=${run} busy=${running}>
              ${running ? "Checking…" : "Run the sample check"}
            <//>`}
        ${result ? null : html`<${Button} onClick=${onFinish} disabled=${running}>Skip<//>`}
        <${Button} variant="ghost" onClick=${onBack} disabled=${running}>Back<//>
      </div>
    <//>
  `;
};

export const WelcomeView = ({navigate}) => {
  const {onboarding} = useAppState();
  const engine = useEngine();
  const [step, setStep] = useState(0);
  const browserEngine = engine.state.mode === ENGINE_MODE.BROWSER;
  const steps = STEPS_BY_MODE[engine.state.mode];

  useEffect(() => {
    document.querySelector(".wizard-step-heading")?.focus();
  }, [step]);

  const finish = async () => {
    await completeOnboarding();
    navigate(ROUTES.HOME);
  };
  const next = () => setStep(step + 1);
  const back = () => setStep(step - 1);

  const body = [
    html`<${HowItWorksStep}
      alreadyAgreed=${Boolean(onboarding.consentAt)}
      onNext=${async () => {
        if (!onboarding.consentAt) {
          await recordConsent();
        }
        next();
      }}
    />`,
    html`<${EngineStep} onNext=${next} onBack=${back} />`,
    ...(browserEngine
      ? [
          html`<${BrowserProviderStep} onNext=${next} onBack=${back} />`,
          html`<${SpeechStep} onNext=${next} onBack=${back} />`,
        ]
      : [
          html`<${BackendStep} onNext=${next} onBack=${back} />`,
          html`<${ProviderStep} onNext=${next} onBack=${back} />`,
        ]),
    html`<${TryItStep} onFinish=${finish} onBack=${back} browserEngine=${browserEngine} />`,
  ][step];

  return html`
    <div class="wizard">
      <header class="wizard-header">
        <img src="../icons/icon48.png" width="40" height="40" alt="" />
        <div>
          <h1 class="page-title">Welcome to Live Stream Fact-Checker</h1>
          <p class="page-subtitle">Five short steps and you are ready to watch.</p>
        </div>
      </header>
      <ol class="wizard-steps" aria-label="Setup progress">
        ${steps.map(
          (name, index) => html`
            <li
              key=${name}
              class="wizard-step"
              data-state=${index < step ? "done" : index === step ? "current" : "todo"}
              aria-current=${index === step ? "step" : undefined}
            >
              <span class="wizard-step-number" aria-hidden="true">${index < step ? "✓" : index + 1}</span>
              <span>${name}</span>
            </li>
          `
        )}
      </ol>
      <h2 class="wizard-step-heading visually-hidden" tabindex="-1">
        Step ${step + 1} of ${steps.length}: ${steps[step]}
      </h2>
      ${body}
    </div>
  `;
};
