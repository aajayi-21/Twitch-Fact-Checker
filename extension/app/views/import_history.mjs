/**
 * "Import past results from the local backend" — shown on an empty History
 * screen and under Settings → Data.
 */

import {Button, Notice} from "../components.mjs";
import {backendApi, useAppState} from "../lib/app_store.mjs";
import {formatDateTime, plural} from "../lib/format.mjs";
import {html, useState} from "../lib/html.mjs";
import {importBackendHistory, useLedger} from "../lib/ledger_store.mjs";

/** What to say once an import has finished. */
const describeResult = (result) => {
  if (result.found === 0) {
    return "The backend has no past sessions to import.";
  }
  const added =
    result.sessions > 0
      ? `Imported ${plural(result.sessions, "session")} with ${plural(result.verdicts, "result")}.`
      : "Nothing new to import.";
  const skipped = result.skipped > 0 ? ` ${result.skipped} already here.` : "";
  const failed =
    result.failed > 0
      ? ` ${plural(result.failed, "session")} could not be read; import again to retry.`
      : "";
  return `${added}${skipped}${failed}`;
};

/**
 * @param {object} props
 * @param {boolean} [props.compact] - a single line, for the empty History screen
 */
export const ImportHistory = ({compact = false}) => {
  const {backendState, summary} = useAppState();
  const {lastImport} = useLedger();
  const [progress, setProgress] = useState(null);
  const [outcome, setOutcome] = useState(null);

  const up = backendState === "up";
  const backendSessions = summary?.totals?.sessions ?? null;
  const running = progress !== null;

  const run = async () => {
    setOutcome(null);
    setProgress({done: 0, total: 0});
    try {
      const result = await importBackendHistory(backendApi, setProgress);
      setOutcome({tone: result.failed > 0 ? "warn" : "success", text: describeResult(result)});
    } catch (error) {
      setOutcome({tone: "error", text: `The import stopped: ${error?.message ?? error}`});
    } finally {
      setProgress(null);
    }
  };

  return html`
    <div class="import-history">
      ${compact
        ? null
        : html`<p class="setting-help">
            Before this app kept its own history, results were stored only by the local backend.
            Importing copies them here. It can be repeated safely: sessions already here are
            skipped.
            ${lastImport
              ? ` Last import: ${formatDateTime(lastImport.at)} (${plural(lastImport.sessions, "session")} added).`
              : null}
          </p>`}
      <div class="form-actions">
        <${Button} variant=${compact ? "primary" : "secondary"} busy=${running} disabled=${!up} onClick=${run}>
          ${running
            ? progress.total > 0
              ? `Importing ${progress.done} of ${progress.total}…`
              : "Looking for past sessions…"
            : "Import past results from the backend"}
        <//>
        ${up
          ? backendSessions !== null && !running
            ? html`<span class="muted">The backend holds ${plural(backendSessions, "session")}.</span>`
            : null
          : html`<span class="muted">Start the local backend to import from it.</span>`}
      </div>
      ${outcome ? html`<${Notice} tone=${outcome.tone}>${outcome.text}<//>` : null}
    </div>
  `;
};
