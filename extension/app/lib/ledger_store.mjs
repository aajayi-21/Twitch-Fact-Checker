/**
 * The viewer's history, as the app's screens see it.
 *
 * Loads the whole ledger from IndexedDB into memory once, reloads when any
 * context changes it (the service worker records a verdict; another app tab
 * deletes a session), and exposes the handful of actions the screens need.
 * The numbers shown are computed from this snapshot by ledger/queries.mjs.
 */

import {importFromBackend} from "../../ledger/backend_import.mjs";
import {exportFileName, ledgerToJson, verdictsToCsv} from "../../ledger/export.mjs";
import * as ledger from "../../ledger/ledger.mjs";
import {buildFeedbackRecord, isoSeconds} from "../../ledger/records.mjs";
import {useEffect, useState} from "./html.mjs";

// Promise-safe API namespace (see shared/capabilities.js § namespace idiom).
const chrome = globalThis.browser ?? globalThis.chrome;

const IMPORT_META_KEY = "backendImport";
// A burst of writes (a verdict, its claim and its usage row arrive together)
// should cause one reload, not three.
const RELOAD_DEBOUNCE_MS = 250;

/**
 * @typedef {object} LedgerState
 * @property {boolean} loaded
 * @property {string|null} error - why the history could not be read
 * @property {object[]} sessions
 * @property {object[]} claims
 * @property {object[]} verdicts
 * @property {object[]} usage
 * @property {Map<string, object>} feedback - by verdict id
 * @property {object|null} lastImport - {at, sessions, verdicts} of the last import
 */

/** @type {LedgerState} */
let state = {
  loaded: false,
  error: null,
  sessions: [],
  claims: [],
  verdicts: [],
  usage: [],
  feedback: new Map(),
  lastImport: null,
};

const listeners = new Set();

const setState = (patch) => {
  state = {...state, ...patch};
  listeners.forEach((listener) => listener());
};

/** Subscribe a component to the history. */
export const useLedger = () => {
  const [, forceRender] = useState(0);
  const renderedState = state;
  useEffect(() => {
    const listener = () => forceRender((count) => count + 1);
    listeners.add(listener);
    if (state !== renderedState) {
      listener(); // changed between render and effect (see app_store.mjs)
    }
    return () => listeners.delete(listener);
  }, []);
  return state;
};

export const reloadLedger = async () => {
  try {
    const [snapshot, lastImport] = await Promise.all([
      ledger.loadLedger(),
      ledger.getMeta(IMPORT_META_KEY),
    ]);
    setState({
      loaded: true,
      error: null,
      sessions: snapshot.sessions,
      claims: snapshot.claims,
      verdicts: snapshot.verdicts,
      usage: snapshot.usage,
      feedback: new Map(snapshot.feedback.map((row) => [row.verdict_id, row])),
      lastImport: lastImport ?? null,
    });
  } catch (error) {
    console.error("[fact-checker] reading the history failed:", error);
    setState({loaded: true, error: String(error?.message ?? error)});
  }
};

let reloadTimer = null;

/** Load the history and keep it fresh. Call once at startup. */
export const initLedger = async () => {
  ledger.subscribeToLedger(() => {
    clearTimeout(reloadTimer);
    reloadTimer = setTimeout(() => reloadLedger(), RELOAD_DEBOUNCE_MS);
  });
  // Ask the browser not to evict this history under disk pressure. It may
  // say no; nothing depends on the answer.
  navigator.storage?.persist?.().catch(() => {});
  await reloadLedger();
};

/* ------------------------------------------------------------------ */
/* Actions                                                              */
/* ------------------------------------------------------------------ */

/**
 * Rate a verdict. Stored here first; the backend's copy is best-effort.
 *
 * @param {object} feedback - {verdictId, rating, correctedLabel?, note?}
 * @param {{submitFeedback: Function}|null} [api] - the backend client, when up
 */
export const rateVerdict = async (feedback, api = null) => {
  const record = buildFeedbackRecord(feedback, isoSeconds());
  if (!record) {
    return;
  }
  await ledger.saveFeedback(record);
  await reloadLedger();
  api?.submitFeedback(feedback).catch((error) => {
    console.debug("[fact-checker] feedback not sent to the backend:", error);
  });
};

export const removeVerdict = async (verdictId) => {
  await ledger.deleteVerdict(verdictId);
  await reloadLedger();
};

export const removeSession = async (sessionId) => {
  await ledger.deleteSession(sessionId);
  await reloadLedger();
};

export const removeChannel = async (platform, channelId) => {
  await ledger.deleteChannel(platform, channelId);
  await reloadLedger();
};

export const removeEverything = async () => {
  await ledger.deleteEverything();
  await reloadLedger();
};

/**
 * Copy past results from the companion backend into the history.
 *
 * @param {object} api - the backend client (app/lib/backend_api.mjs)
 * @param {(progress: {done: number, total: number}) => void} [onProgress]
 * @returns {Promise<object>} what importFromBackend reports
 */
export const importBackendHistory = async (api, onProgress) => {
  const result = await importFromBackend({api, ledger, onProgress});
  await ledger.setMeta(IMPORT_META_KEY, {
    at: isoSeconds(),
    sessions: result.sessions,
    verdicts: result.verdicts,
  });
  await reloadLedger();
  return result;
};

/** Hand the viewer a file. Works in extension pages without any permission. */
const download = (fileName, mimeType, text) => {
  const url = URL.createObjectURL(new Blob([text], {type: mimeType}));
  const link = document.createElement("a");
  link.href = url;
  link.download = fileName;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
};

/**
 * @param {object[]} verdicts - already filtered and ordered
 * @param {string} [stem]
 */
export const exportVerdictsCsv = (verdicts, stem = "fact-checks") => {
  // The BOM makes Excel read the file as UTF-8.
  download(
    exportFileName(stem, "csv"),
    "text/csv;charset=utf-8",
    `﻿${verdictsToCsv(verdicts, state.feedback)}`
  );
};

/**
 * @param {object} [part] - defaults to the whole history
 * @param {string} [stem]
 */
export const exportJson = (part = null, stem = "fact-checker-history") => {
  const everything = part ?? {
    sessions: state.sessions,
    claims: state.claims,
    verdicts: state.verdicts,
    feedback: [...state.feedback.values()],
    usage: state.usage,
  };
  download(
    exportFileName(stem, "json"),
    "application/json",
    ledgerToJson(everything, {
      exportedAt: isoSeconds(),
      appVersion: chrome.runtime.getManifest().version,
    })
  );
};
