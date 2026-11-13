/**
 * The ledger: the viewer's own record of sessions, claims, verdicts,
 * feedback and provider usage, kept in this browser's IndexedDB.
 *
 * It is the one source of truth for the History and Usage screens,
 * whichever engine produced the data. The service worker writes to it as
 * frames arrive; the app tab reads from it. Both are extension pages on the
 * same origin, so they open the same database, and a BroadcastChannel tells
 * open app tabs when something changed.
 *
 * Nothing here leaves the device. The only way data gets out is the export
 * the viewer triggers themselves.
 *
 * Shapes of the stored rows: ./records.mjs. This module is deliberately
 * thin — every decision that can be made without a database lives in
 * records.mjs and queries.mjs, which are unit-tested under Node; this file
 * is exercised against a real browser by scripts/run_app_e2e.mjs.
 */

const DATABASE_NAME = "factchecker";
const DATABASE_VERSION = 1;
const CHANGE_CHANNEL = "factchecker-ledger";

export const STORE = Object.freeze({
  SESSIONS: "sessions",
  CLAIMS: "claims",
  VERDICTS: "verdicts",
  FEEDBACK: "feedback",
  USAGE: "usage_events",
  META: "meta",
});

const DATA_STORES = Object.freeze([
  STORE.SESSIONS,
  STORE.CLAIMS,
  STORE.VERDICTS,
  STORE.FEEDBACK,
  STORE.USAGE,
]);

const CHANNEL_INDEX = "channel"; // [platform, channel_id]

/** Create the schema. Runs once per database version. */
const upgrade = (database) => {
  const sessions = database.createObjectStore(STORE.SESSIONS, {keyPath: "id"});
  sessions.createIndex("started_at", "started_at");
  sessions.createIndex("backend_session_id", "backend_session_id");
  sessions.createIndex(CHANNEL_INDEX, ["platform", "channel_id"]);

  const claims = database.createObjectStore(STORE.CLAIMS, {keyPath: "id"});
  claims.createIndex("session_id", "session_id");
  claims.createIndex("gated_at", "gated_at");

  const verdicts = database.createObjectStore(STORE.VERDICTS, {keyPath: "id"});
  verdicts.createIndex("session_id", "session_id");
  verdicts.createIndex("checked_at", "checked_at");
  verdicts.createIndex(CHANNEL_INDEX, ["platform", "channel_id"]);

  database.createObjectStore(STORE.FEEDBACK, {keyPath: "verdict_id"});

  const usage = database.createObjectStore(STORE.USAGE, {keyPath: "id", autoIncrement: true});
  usage.createIndex("session_id", "session_id");
  usage.createIndex("ts", "ts");

  database.createObjectStore(STORE.META, {keyPath: "key"});
};

let databasePromise = null;

/** @returns {Promise<IDBDatabase>} one shared connection per context */
const openDatabase = () => {
  if (!databasePromise) {
    databasePromise = new Promise((resolve, reject) => {
      const request = indexedDB.open(DATABASE_NAME, DATABASE_VERSION);
      request.onupgradeneeded = () => upgrade(request.result);
      request.onsuccess = () => {
        const database = request.result;
        // Another context is upgrading the schema: let go, and reconnect on
        // the next call.
        database.onversionchange = () => {
          database.close();
          databasePromise = null;
        };
        resolve(database);
      };
      request.onerror = () => reject(request.error);
      request.onblocked = () => reject(new Error("The history database is blocked by another tab."));
    }).catch((error) => {
      databasePromise = null;
      throw error;
    });
  }
  return databasePromise;
};

/** An IndexedDB request as a promise. Safe to await inside a transaction. */
const done = (request) =>
  new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });

/**
 * Run `work` inside one transaction and resolve once it has committed.
 *
 * @template T
 * @param {string[]} storeNames
 * @param {IDBTransactionMode} mode
 * @param {(stores: Record<string, IDBObjectStore>) => Promise<T>|T} work
 * @returns {Promise<T>}
 */
const transact = async (storeNames, mode, work) => {
  const database = await openDatabase();
  const transaction = database.transaction(storeNames, mode);
  const committed = new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error);
    transaction.onabort = () =>
      reject(transaction.error ?? new Error("The history write was cancelled."));
  });
  const stores = Object.fromEntries(
    storeNames.map((name) => [name, transaction.objectStore(name)])
  );
  let result;
  try {
    result = await work(stores);
  } catch (error) {
    try {
      transaction.abort();
    } catch {
      // Already finished or aborted — the original error is what matters.
    }
    await committed.catch(() => {});
    throw error;
  }
  await committed;
  return result;
};

let changeChannel = null;

const announceChange = () => {
  try {
    changeChannel ??= new BroadcastChannel(CHANGE_CHANNEL);
    changeChannel.postMessage({type: "changed"});
  } catch (error) {
    console.debug("[fact-checker] ledger change not announced:", error);
  }
};

/**
 * Be told when any context changes the ledger.
 *
 * @param {() => void} listener
 * @returns {() => void} unsubscribe
 */
export const subscribeToLedger = (listener) => {
  const channel = new BroadcastChannel(CHANGE_CHANNEL);
  channel.onmessage = () => listener();
  return () => channel.close();
};

/** Delete every row an index lists under `key`. */
const deleteByIndex = async (store, indexName, key) => {
  const keys = await done(store.index(indexName).getAllKeys(key));
  keys.forEach((primaryKey) => store.delete(primaryKey));
  return keys;
};

const touchSession = async (sessions, sessionId, at) => {
  const session = await done(sessions.get(sessionId));
  if (session && (!session.last_activity_at || at > session.last_activity_at)) {
    sessions.put({...session, last_activity_at: at});
  }
};

/* ------------------------------------------------------------------ */
/* Reads                                                                */
/* ------------------------------------------------------------------ */

/**
 * Everything, for the History and Usage screens.
 *
 * @returns {Promise<{sessions: object[], claims: object[], verdicts: object[],
 *   feedback: object[], usage: object[]}>}
 */
export const loadLedger = () =>
  transact(DATA_STORES, "readonly", async (stores) => {
    const [sessions, claims, verdicts, feedback, usage] = await Promise.all(
      DATA_STORES.map((name) => done(stores[name].getAll()))
    );
    return {sessions, claims, verdicts, feedback, usage};
  });

/** @returns {Promise<Record<string, number>>} row count per store */
export const countLedgerRows = () =>
  transact(DATA_STORES, "readonly", async (stores) => {
    const counts = await Promise.all(DATA_STORES.map((name) => done(stores[name].count())));
    return Object.fromEntries(DATA_STORES.map((name, index) => [name, counts[index]]));
  });

/** @returns {Promise<object|undefined>} */
export const getSession = (sessionId) =>
  transact([STORE.SESSIONS], "readonly", (stores) => done(stores[STORE.SESSIONS].get(sessionId)));

/**
 * @param {string} key
 * @returns {Promise<unknown>} the stored value, or undefined
 */
export const getMeta = async (key) =>
  (await transact([STORE.META], "readonly", (stores) => done(stores[STORE.META].get(key))))?.value;

export const setMeta = (key, value) =>
  transact([STORE.META], "readwrite", (stores) => done(stores[STORE.META].put({key, value})));

/* ------------------------------------------------------------------ */
/* Writes from a running session                                        */
/* ------------------------------------------------------------------ */

export const putSession = async (session) => {
  await transact([STORE.SESSIONS], "readwrite", (stores) =>
    done(stores[STORE.SESSIONS].put(session))
  );
  announceChange();
};

/**
 * @param {string} sessionId
 * @param {object} patch
 * @returns {Promise<object|null>} the updated session, or null if unknown
 */
export const updateSession = async (sessionId, patch) => {
  const updated = await transact([STORE.SESSIONS], "readwrite", async (stores) => {
    const session = await done(stores[STORE.SESSIONS].get(sessionId));
    if (!session) {
      return null;
    }
    const next = {...session, ...patch};
    await done(stores[STORE.SESSIONS].put(next));
    return next;
  });
  if (updated) {
    announceChange();
  }
  return updated;
};

/** Store a verdict (replacing any earlier row with the same id). */
export const recordVerdict = async (verdict) => {
  await transact([STORE.VERDICTS, STORE.SESSIONS], "readwrite", async (stores) => {
    await done(stores[STORE.VERDICTS].put(verdict));
    await touchSession(stores[STORE.SESSIONS], verdict.session_id, verdict.checked_at);
  });
  announceChange();
};

/**
 * Store a claim. A claim is announced twice when it is checked ("pending",
 * then its outcome); the first time it was heard is kept.
 */
export const recordClaim = async (claim) => {
  await transact([STORE.CLAIMS, STORE.SESSIONS], "readwrite", async (stores) => {
    const existing = await done(stores[STORE.CLAIMS].get(claim.id));
    await done(
      stores[STORE.CLAIMS].put(existing ? {...claim, gated_at: existing.gated_at} : claim)
    );
    await touchSession(stores[STORE.SESSIONS], claim.session_id, claim.gated_at);
  });
  announceChange();
};

/** Store one provider call's usage. */
export const recordUsage = async (usage) => {
  await transact([STORE.USAGE, STORE.SESSIONS], "readwrite", async (stores) => {
    await done(stores[STORE.USAGE].add(usage));
    await touchSession(stores[STORE.SESSIONS], usage.session_id, usage.ts);
  });
  announceChange();
};

/**
 * A channel that was unknown when the session started has been resolved
 * (YouTube renders the owner link late). Fill it in on the session and on
 * every row already written for it.
 *
 * @param {string} sessionId
 * @param {{platform: string|null, channel_id: string|null, channel_name: string|null}} channel
 */
export const fillInSessionChannel = async (sessionId, channel) => {
  const fields = {
    platform: channel.platform,
    channel_id: channel.channel_id,
    channel_name: channel.channel_name,
  };
  await transact(
    [STORE.SESSIONS, STORE.CLAIMS, STORE.VERDICTS, STORE.USAGE],
    "readwrite",
    async (stores) => {
      const session = await done(stores[STORE.SESSIONS].get(sessionId));
      if (session) {
        stores[STORE.SESSIONS].put({...session, ...fields});
      }
      for (const name of [STORE.CLAIMS, STORE.VERDICTS, STORE.USAGE]) {
        const rows = await done(stores[name].index("session_id").getAll(sessionId));
        rows
          .filter((row) => !row.channel_id)
          .forEach((row) => stores[name].put({...row, ...fields}));
      }
    }
  );
  announceChange();
};

/* ------------------------------------------------------------------ */
/* Feedback                                                             */
/* ------------------------------------------------------------------ */

export const saveFeedback = async (feedback) => {
  await transact([STORE.FEEDBACK], "readwrite", (stores) =>
    done(stores[STORE.FEEDBACK].put(feedback))
  );
  announceChange();
};

/* ------------------------------------------------------------------ */
/* Import                                                               */
/* ------------------------------------------------------------------ */

/**
 * Add sessions imported from the companion backend.
 *
 * A session the ledger already holds — recorded live, or imported before —
 * is skipped whole, so importing twice changes nothing.
 *
 * @param {{session: object, claims: object[], verdicts: object[],
 *   feedback: object[], usage: object[]}[]} batches - from recordsFromBackendSession
 * @returns {Promise<{sessions: number, verdicts: number, skipped: number}>}
 */
export const importSessions = async (batches) => {
  const result = await transact(DATA_STORES, "readwrite", async (stores) => {
    const counts = {sessions: 0, verdicts: 0, skipped: 0};
    for (const batch of batches) {
      const backendId = batch.session.backend_session_id;
      const known = await done(
        stores[STORE.SESSIONS].index("backend_session_id").count(backendId)
      );
      if (known > 0) {
        counts.skipped += 1;
        continue;
      }
      stores[STORE.SESSIONS].put(batch.session);
      batch.claims.forEach((claim) => stores[STORE.CLAIMS].put(claim));
      batch.verdicts.forEach((verdict) => stores[STORE.VERDICTS].put(verdict));
      batch.feedback.forEach((feedback) => stores[STORE.FEEDBACK].put(feedback));
      batch.usage.forEach((usage) => stores[STORE.USAGE].add(usage));
      counts.sessions += 1;
      counts.verdicts += batch.verdicts.length;
    }
    return counts;
  });
  if (result.sessions > 0) {
    announceChange();
  }
  return result;
};

/**
 * Which of these backend session ids the ledger already has.
 *
 * @param {string[]} backendSessionIds
 * @returns {Promise<Set<string>>}
 */
export const knownBackendSessions = (backendSessionIds) =>
  transact([STORE.SESSIONS], "readonly", async (stores) => {
    const index = stores[STORE.SESSIONS].index("backend_session_id");
    const counts = await Promise.all(backendSessionIds.map((id) => done(index.count(id))));
    return new Set(backendSessionIds.filter((id, position) => counts[position] > 0));
  });

/* ------------------------------------------------------------------ */
/* Deleting                                                             */
/* ------------------------------------------------------------------ */

/** Remove a session and everything recorded during it. Inside a transaction. */
const removeSession = async (stores, sessionId) => {
  stores[STORE.SESSIONS].delete(sessionId);
  await deleteByIndex(stores[STORE.CLAIMS], "session_id", sessionId);
  const verdictIds = await deleteByIndex(stores[STORE.VERDICTS], "session_id", sessionId);
  verdictIds.forEach((verdictId) => stores[STORE.FEEDBACK].delete(verdictId));
  await deleteByIndex(stores[STORE.USAGE], "session_id", sessionId);
};

/**
 * Delete one verdict and the viewer's rating of it. What the check cost
 * stays in the usage totals — the money was spent.
 */
export const deleteVerdict = async (verdictId) => {
  await transact([STORE.VERDICTS, STORE.FEEDBACK], "readwrite", (stores) => {
    stores[STORE.VERDICTS].delete(verdictId);
    stores[STORE.FEEDBACK].delete(verdictId);
  });
  announceChange();
};

/** Delete a session with its claims, verdicts, ratings and usage. */
export const deleteSession = async (sessionId) => {
  await transact(DATA_STORES, "readwrite", (stores) => removeSession(stores, sessionId));
  announceChange();
};

/**
 * Delete everything recorded for one channel.
 *
 * @param {string} platform
 * @param {string} channelId
 */
export const deleteChannel = async (platform, channelId) => {
  await transact(DATA_STORES, "readwrite", async (stores) => {
    const key = [platform, channelId];
    const sessionIds = await done(stores[STORE.SESSIONS].index(CHANNEL_INDEX).getAllKeys(key));
    for (const sessionId of sessionIds) {
      await removeSession(stores, sessionId);
    }
    // A session that moved between channels keeps its first channel on the
    // session row; its later verdicts carry their own.
    const strayVerdicts = await deleteByIndex(stores[STORE.VERDICTS], CHANNEL_INDEX, key);
    strayVerdicts.forEach((verdictId) => stores[STORE.FEEDBACK].delete(verdictId));
  });
  announceChange();
};

/**
 * Delete sessions that started before `cutoffIso`, with everything recorded
 * during them (the retention setting).
 *
 * @param {string} cutoffIso
 * @returns {Promise<number>} sessions removed
 */
export const deleteOlderThan = async (cutoffIso) => {
  const removed = await transact(DATA_STORES, "readwrite", async (stores) => {
    const sessionIds = await done(
      stores[STORE.SESSIONS].index("started_at").getAllKeys(IDBKeyRange.upperBound(cutoffIso, true))
    );
    for (const sessionId of sessionIds) {
      await removeSession(stores, sessionId);
    }
    return sessionIds.length;
  });
  if (removed > 0) {
    announceChange();
  }
  return removed;
};

/** Delete the whole history, including import markers. */
export const deleteEverything = async () => {
  await transact([...DATA_STORES, STORE.META], "readwrite", (stores) => {
    Object.values(stores).forEach((store) => store.clear());
  });
  announceChange();
};
