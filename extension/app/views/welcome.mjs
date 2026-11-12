/**
 * First-run welcome flow: what the app does and touches, getting the local
 * backend running, adding an API key, and a real test check.
 *
 * Each step can be revisited; nothing is captured or sent until the person
 * has read the first step and agreed.
 */

import {Button, Card, ExternalLink, Notice, StatusPill} from "../components.mjs";
import {
  BACKEND_ERROR,
  completeOnboarding,
  recordConsent,
  refreshBackend,
  runTestCheck,
  useAppState,
} from "../lib/app_store.mjs";
import {html, useEffect, useState} from "../lib/html.mjs";
import {ROUTES} from "../lib/routes.mjs";
import {KEY_PROVIDERS, describeConnection, describeStages} from "../lib/setup_model.mjs";
import {KeyForm} from "./key_form.mjs";

const STEPS = Object.freeze(["How it works", "Local backend", "API key", "Try it"]);

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
        <strong>Audio stays on this computer.</strong> It goes to the backend program you run
        locally, which turns it into text.
      </li>
      <li>
        <strong>Claims go to the AI provider whose key you add.</strong> Only the sentence being
        checked is sent, so the provider can search the web and judge it. You pay that
        provider directly for what you use.
      </li>
      <li>
        <strong>Your history stays local</strong>, in a database file next to the backend. Nothing
        is sent to us; there is no account and no analytics.
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
            <p class="field-help">OpenAI and Gemini keys will be supported by the built-in engine.</p>
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

const TryItStep = ({onFinish, onBack}) => {
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);

  const run = async () => {
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      setResult(await runTestCheck(SAMPLE_CLAIM));
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
  const [step, setStep] = useState(0);

  useEffect(() => {
    document.querySelector(".wizard-step-heading")?.focus();
  }, [step]);

  const finish = async () => {
    await completeOnboarding();
    navigate(ROUTES.HOME);
  };

  const body = [
    html`<${HowItWorksStep}
      alreadyAgreed=${Boolean(onboarding.consentAt)}
      onNext=${async () => {
        if (!onboarding.consentAt) {
          await recordConsent();
        }
        setStep(1);
      }}
    />`,
    html`<${BackendStep} onNext=${() => setStep(2)} onBack=${() => setStep(0)} />`,
    html`<${ProviderStep} onNext=${() => setStep(3)} onBack=${() => setStep(1)} />`,
    html`<${TryItStep} onFinish=${finish} onBack=${() => setStep(2)} />`,
  ][step];

  return html`
    <div class="wizard">
      <header class="wizard-header">
        <img src="../icons/icon48.png" width="40" height="40" alt="" />
        <div>
          <h1 class="page-title">Welcome to Live Stream Fact-Checker</h1>
          <p class="page-subtitle">Four short steps and you are ready to watch.</p>
        </div>
      </header>
      <ol class="wizard-steps" aria-label="Setup progress">
        ${STEPS.map(
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
        Step ${step + 1} of ${STEPS.length}: ${STEPS[step]}
      </h2>
      ${body}
    </div>
  `;
};
