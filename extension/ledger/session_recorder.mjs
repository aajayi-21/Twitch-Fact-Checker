/**
 * Turns a capture session's event stream into ledger rows.
 *
 * The service worker feeds this three kinds of event: capture starting and
 * ending, frames from the engine (ready / verdict / claim / usage), and
 * "the tab is now showing something else". From those it keeps exactly one
 * open ledger session per capture and attributes every row to the channel
 * that was on screen when it arrived.
 *
 * A ledger session is one continuous stretch of ONE channel:
 *   - the engine reconnecting (a new "ready") starts a new one, matching the
 *     companion backend, which also starts a new session per connection;
 *   - the tab moving to a different channel starts a new one, so watch time
 *     and spend are not credited to the wrong creator.
 *
 * Everything outside is injected — the ledger, the small piece of state that
 * must survive a service-worker restart, the clock — so the whole state
 * machine is unit-tested under Node.
 */

import {
  ENGINE,
  buildClaimRecord,
  buildFeedbackRecord,
  buildSessionRecord,
  buildUsageRecord,
  buildVerdictRecord,
  isSameChannel,
  isoSeconds,
  normalizeIdentity,
} from "./records.mjs";

const DAY_MS = 86_400_000;

/**
 * @typedef {object} RecorderState
 * @property {number} tabId - the captured tab
 * @property {object} identity - what the tab shows now (normalizeIdentity)
 * @property {object|null} session - the open ledger session row, if any
 */

/**
 * @param {object} deps
 * @param {object} deps.ledger - putSession, updateSession, recordVerdict,
 *   recordClaim, recordUsage, fillInSessionChannel, saveFeedback,
 *   deleteOlderThan (see ledger.mjs)
 * @param {{get: () => Promise<RecorderState|null>, set: (state: RecorderState|null) => Promise<void>}} deps.memory
 *   state that outlives a service-worker restart
 * @param {() => Date|number} [deps.now]
 * @param {() => string} [deps.newId]
 */
export const createSessionRecorder = ({
  ledger,
  memory,
  now = () => new Date(),
  newId = () => crypto.randomUUID(),
}) => {
  // Message handlers run concurrently. A "ready" followed at once by a
  // "verdict" must be applied in order, or the verdict finds no session, so
  // every operation waits for the one before it.
  let queue = Promise.resolve();
  const inOrder = (work) => {
    const run = queue.then(work);
    queue = run.catch(() => {});
    return run;
  };

  const nowIso = () => isoSeconds(now());

  const openSession = async (state, backendSessionId, engine = state.session?.engine) => {
    const session = buildSessionRecord({
      id: newId(),
      backendSessionId,
      identity: state.identity,
      startedAt: nowIso(),
      engine: engine ?? state.engine ?? ENGINE.COMPANION,
    });
    await ledger.putSession(session);
    state.session = session;
  };

  const closeSession = async (state) => {
    if (!state.session) {
      return;
    }
    const endedAt = nowIso();
    await ledger.updateSession(state.session.id, {ended_at: endedAt, last_activity_at: endedAt});
    state.session = null;
  };

  /** State for `tabId`, starting fresh if the stored state is for another tab. */
  const stateFor = async (tabId) => {
    const stored = await memory.get();
    return stored && stored.tabId === tabId
      ? stored
      : {tabId, identity: normalizeIdentity(), session: null};
  };

  const applyIdentity = async (state, next) => {
    const previous = state.identity;
    state.identity = next;
    const session = state.session;
    if (!session) {
      return;
    }
    const streamFields = {stream_url: next.stream_url, stream_title: next.stream_title};

    if (isSameChannel(previous, next)) {
      // Same channel, possibly a different video or a renamed stream.
      const patch = {...streamFields, channel_name: next.channel_name ?? session.channel_name};
      const changed = Object.keys(patch).some((key) => patch[key] !== session[key]);
      if (changed) {
        await ledger.updateSession(session.id, patch);
        state.session = {...session, ...patch};
      }
      return;
    }
    const samePlatform = !previous.platform || previous.platform === next.platform;
    if (!previous.channel_id && next.channel_id && samePlatform) {
      // The channel was not known when the session opened (YouTube renders
      // the owner link late): this is the same session, now attributable.
      await ledger.fillInSessionChannel(session.id, next);
      await ledger.updateSession(session.id, streamFields);
      state.session = {...session, ...next};
      return;
    }
    if (!next.channel_id && next.stream_url === previous.stream_url) {
      // Same page, but the owner link is momentarily missing while the site
      // re-renders. Keep what is known rather than splitting the session.
      state.identity = previous;
      return;
    }
    // A different channel: close this session and open one for the new
    // channel, on the same engine connection.
    const backendSessionId = session.backend_session_id;
    await closeSession(state);
    await openSession(state, backendSessionId, session.engine);
  };

  const recordFrame = async (state, event) => {
    if (!state.session) {
      // A frame with no "ready" before it (the worker lost its state):
      // record it anyway, in a session opened now.
      await openSession(state, null);
    }
    const stamp = nowIso();
    if (event.type === "verdict") {
      const verdict = buildVerdictRecord(event, state.session);
      if (verdict) {
        await ledger.recordVerdict(verdict);
      }
    } else if (event.type === "claim") {
      const claim = buildClaimRecord(event, state.session, stamp);
      if (claim) {
        await ledger.recordClaim(claim);
      }
    } else if (event.type === "usage") {
      const usage = buildUsageRecord(event, state.session, stamp);
      if (usage) {
        await ledger.recordUsage(usage);
      }
    }
  };

  return {
    /**
     * A capture is about to start on `tabId`.
     *
     * @param {number} tabId
     * @param {object} identity - camelCase, from identityFromPage
     */
    captureStarting: (tabId, identity) =>
      inOrder(async () => {
        const stale = await memory.get();
        if (stale) {
          await closeSession(stale);
        }
        await memory.set({tabId, identity: normalizeIdentity(identity), session: null});
      }),

    /**
     * A frame arrived from the engine. Frames the ledger does not keep
     * (transcripts, status, errors) are ignored.
     *
     * @param {number} tabId
     * @param {object} event
     */
    engineEvent: (tabId, event) => {
      if (!["ready", "verdict", "claim", "usage"].includes(event?.type)) {
        return Promise.resolve();
      }
      return inOrder(async () => {
        const state = await stateFor(tabId);
        if (event.type === "ready") {
          await closeSession(state);
          // The in-extension engine says so in its ready frame; the backend
          // (which predates the field) says nothing.
          state.engine = event.engine === ENGINE.BROWSER ? ENGINE.BROWSER : ENGINE.COMPANION;
          await openSession(
            state,
            typeof event.session_id === "string" ? event.session_id : null,
            state.engine
          );
        } else {
          await recordFrame(state, event);
        }
        await memory.set(state);
      });
    },

    /**
     * The captured tab now shows something else (or its channel became
     * known). Ignored for any tab other than the captured one.
     *
     * @param {number} tabId
     * @param {object} identity - camelCase, from identityFromPage
     */
    identityChanged: (tabId, identity) =>
      inOrder(async () => {
        const state = await memory.get();
        if (!state || state.tabId !== tabId) {
          return;
        }
        await applyIdentity(state, normalizeIdentity(identity));
        await memory.set(state);
      }),

    /** The capture has ended, for any reason. */
    captureEnded: () =>
      inOrder(async () => {
        const state = await memory.get();
        if (state) {
          await closeSession(state);
        }
        await memory.set(null);
      }),

    /**
     * The viewer rated a verdict.
     *
     * @param {{verdictId: string, rating: "up"|"down", correctedLabel?: string|null,
     *   note?: string|null}} feedback
     */
    feedback: (feedback) =>
      inOrder(async () => {
        const record = buildFeedbackRecord(feedback, nowIso());
        if (record) {
          await ledger.saveFeedback(record);
        }
      }),

    /**
     * Apply the retention setting: delete sessions older than `days`.
     *
     * @param {number} days - 0 (or anything not positive) keeps everything
     * @returns {Promise<number>} sessions removed
     */
    applyRetention: (days) =>
      inOrder(async () => {
        if (!Number.isFinite(days) || days <= 0) {
          return 0;
        }
        return ledger.deleteOlderThan(isoSeconds(new Date(now()).getTime() - days * DAY_MS));
      }),
  };
};
