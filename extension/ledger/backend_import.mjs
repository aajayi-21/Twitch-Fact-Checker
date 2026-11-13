/**
 * One-off import of past results from the companion backend's database.
 *
 * Before the extension kept its own history, everything was recorded in the
 * backend's `fact_checker.db`. This pulls those sessions across through the
 * backend's existing read-only `/stats` endpoints so the History and Usage
 * screens start with the viewer's real past, not an empty page.
 *
 * Safe to repeat: a session the ledger already has — imported before, or
 * recorded live — is skipped, so running it twice adds nothing.
 *
 * The HTTP client and the ledger are passed in, so the paging and skipping
 * logic is unit-tested under Node.
 */

import {recordsFromBackendSession} from "./records.mjs";

const PAGE_SIZE = 100;
const WRITE_BATCH = 20;

/**
 * @param {object} deps
 * @param {{statsSessions: Function, statsSession: Function}} deps.api - app/lib/backend_api.mjs
 * @param {{knownBackendSessions: Function, importSessions: Function}} deps.ledger
 * @param {(progress: {done: number, total: number}) => void} [deps.onProgress]
 * @param {AbortSignal} [deps.signal]
 * @returns {Promise<{found: number, sessions: number, verdicts: number,
 *   skipped: number, failed: number}>}
 */
export const importFromBackend = async ({api, ledger, onProgress = () => {}, signal}) => {
  // 1. List every session the backend has.
  const listed = new Map();
  for (let offset = 0; ; offset += PAGE_SIZE) {
    signal?.throwIfAborted();
    const page = await api.statsSessions({limit: PAGE_SIZE, offset});
    const fresh = (Array.isArray(page) ? page : []).filter(
      (row) => typeof row?.id === "string" && !listed.has(row.id)
    );
    fresh.forEach((row) => listed.set(row.id, row));
    // A backend that predates `offset` ignores it and repeats page one, so
    // "nothing new" ends the loop as surely as a short page does.
    if (fresh.length === 0 || page.length < PAGE_SIZE) {
      break;
    }
  }

  // 2. Fetch and store the ones the ledger does not have yet.
  const ids = [...listed.keys()];
  const known = await ledger.knownBackendSessions(ids);
  const wanted = ids.filter((id) => !known.has(id));
  const result = {found: ids.length, sessions: 0, verdicts: 0, skipped: known.size, failed: 0};
  let done = 0;
  onProgress({done, total: wanted.length});

  for (let start = 0; start < wanted.length; start += WRITE_BATCH) {
    const batch = [];
    for (const sessionId of wanted.slice(start, start + WRITE_BATCH)) {
      signal?.throwIfAborted();
      try {
        const records = recordsFromBackendSession(await api.statsSession(sessionId));
        if (records) {
          batch.push(records);
        } else {
          result.failed += 1;
        }
      } catch (error) {
        // One unreadable session must not cost the rest; it stays missing
        // and a later import will try it again.
        console.warn(`[fact-checker] could not import session ${sessionId}:`, error);
        result.failed += 1;
      }
      done += 1;
      onProgress({done, total: wanted.length});
    }
    if (batch.length > 0) {
      const written = await ledger.importSessions(batch);
      result.sessions += written.sessions;
      result.verdicts += written.verdicts;
      result.skipped += written.skipped;
    }
  }
  return result;
};
