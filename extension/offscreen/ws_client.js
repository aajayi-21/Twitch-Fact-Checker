/**
 * BackendSocket — the single WebSocket to the local backend.
 *
 * Protocol (§2.1 of the plan):
 *  - on open, send the hello handshake as the first text frame;
 *  - binary frames are raw Int16LE 16 kHz mono PCM, 4000 samples each;
 *  - server frames are JSON text, dispatched verbatim to `onEvent`;
 *  - {"type":"config", ...} for mid-session updates;
 *  - {"type":"stop"} for a graceful end (server flushes and closes 1000).
 *
 * Reconnect policy: exponential backoff 0.5 s → 15 s cap; give up after
 * 5 minutes of continuous disconnection (a dead backend must not hold the
 * tab capture hostage — `onGiveUp` lets the owner end the session).
 * A reconnect is a brand-new connection; the server's
 * new-connection-preempts-old rule makes this self-healing.
 *
 * Audio while not OPEN is DROPPED (counted in `droppedMs`): a live stream
 * cannot be paused, so buffering through an outage would only produce
 * stale fact-checks.
 */

const INITIAL_BACKOFF_MS = 500;
const MAX_BACKOFF_MS = 15000;
const GIVE_UP_AFTER_MS = 5 * 60 * 1000;
const FRAME_DURATION_MS = 250; // one 4000-sample frame at 16 kHz
const STOP_FLUSH_TIMEOUT_MS = 10000; // bound on the server's post-stop flush

export class BackendSocket {
  /**
   * @param {object} options
   * @param {string} options.url - ws:// or wss:// backend URL
   * @param {object} options.hello - the hello frame; kept by reference so the
   *   owner can update fields (e.g. sensitivity) and reconnect hellos stay
   *   current.
   * @param {(frame: object) => void} options.onEvent - every parsed server frame
   * @param {(wsState: string, reconnectAttempt: number) => void} options.onStateChange
   *   wsState: "connecting" | "open" | "reconnecting" | "closed"
   * @param {(reason: string) => void} options.onGiveUp - reconnection abandoned
   */
  constructor({url, hello, onEvent, onStateChange, onGiveUp}) {
    this.url = url;
    this.hello = hello;
    this.onEvent = onEvent;
    this.onStateChange = onStateChange;
    this.onGiveUp = onGiveUp;
    this.socket = null;
    this.reconnectAttempt = 0;
    this.reconnectTimerId = null;
    this.disconnectedSince = null;
    this.droppedMs = 0;
    this.closedByClient = false;
  }

  /** Open (or re-open) the connection. Safe to call from the backoff timer. */
  connect() {
    if (this.closedByClient) {
      return;
    }
    this.clearReconnectTimer();
    let socket;
    try {
      socket = new WebSocket(this.url);
    } catch (error) {
      // Malformed URL — retrying cannot help.
      console.error(`[fact-checker] invalid backend URL "${this.url}":`, error);
      this.emitState("closed");
      this.onGiveUp?.(`invalid backend URL: ${this.url}`);
      return;
    }
    this.socket = socket;
    this.emitState(this.reconnectAttempt > 0 ? "reconnecting" : "connecting");

    socket.addEventListener("open", () => {
      if (socket !== this.socket) {
        return; // stale socket from before a reconnect
      }
      this.disconnectedSince = null;
      this.reconnectAttempt = 0;
      try {
        socket.send(JSON.stringify(this.hello));
      } catch (error) {
        console.error("[fact-checker] hello send failed:", error);
        socket.close();
        return;
      }
      this.emitState("open");
    });

    socket.addEventListener("message", (event) => {
      if (socket !== this.socket || typeof event.data !== "string") {
        return;
      }
      let frame;
      try {
        frame = JSON.parse(event.data);
      } catch (error) {
        console.error("[fact-checker] unparseable server frame:", event.data, error);
        return;
      }
      this.onEvent?.(frame);
    });

    socket.addEventListener("close", (event) => {
      if (socket !== this.socket) {
        return;
      }
      this.socket = null;
      if (this.closedByClient) {
        this.emitState("closed");
        return;
      }
      console.warn(
        `[fact-checker] WebSocket closed (code ${event.code}); scheduling reconnect`
      );
      this.scheduleReconnect();
    });

    // "error" carries no detail and is always followed by "close" — the
    // close handler owns the reconnect logic.
    socket.addEventListener("error", () => {});
  }

  /**
   * Send one 250 ms binary PCM frame, or drop it (counting droppedMs) when
   * the socket is not OPEN.
   *
   * @param {ArrayBuffer} pcmFrame
   */
  sendAudio(pcmFrame) {
    if (this.socket?.readyState === WebSocket.OPEN) {
      try {
        this.socket.send(pcmFrame);
        return;
      } catch (error) {
        console.warn("[fact-checker] audio frame send failed:", error);
      }
    }
    this.droppedMs += FRAME_DURATION_MS;
  }

  /**
   * Forward a mid-session config change ({"type":"config", ...}). Updated
   * sensitivity is also folded into the hello so reconnects carry it.
   *
   * @param {object} config - e.g. {sensitivity: "high"}
   * @returns {boolean} true if delivered now, false if the socket was down
   */
  sendConfig(config) {
    if (config.sensitivity !== undefined) {
      this.hello.sensitivity = config.sensitivity;
    }
    if (this.socket?.readyState !== WebSocket.OPEN) {
      return false;
    }
    try {
      this.socket.send(JSON.stringify({type: "config", ...config}));
      return true;
    } catch (error) {
      console.warn("[fact-checker] config send failed:", error);
      return false;
    }
  }

  /**
   * Graceful shutdown: send {"type":"stop"} so the server flushes and runs a
   * final gate pass, then keep the socket registered and keep dispatching
   * incoming frames (flush status/verdicts) to `onEvent` until the server
   * closes 1000, with a STOP_FLUSH_TIMEOUT_MS force-close fallback.
   *
   * `this.socket` must keep pointing at the closing socket during the wait —
   * the message listener drops frames from any socket that is not
   * `this.socket`, so nulling it early would silently discard the flush.
   */
  async stop() {
    this.closedByClient = true;
    this.clearReconnectTimer();
    const socket = this.socket;
    if (socket && socket.readyState === WebSocket.OPEN) {
      try {
        socket.send(JSON.stringify({type: "stop"}));
      } catch (error) {
        console.warn("[fact-checker] stop frame send failed:", error);
      }
      await new Promise((resolve) => {
        const timeoutId = setTimeout(() => {
          console.warn(
            "[fact-checker] server did not close after stop; force-closing"
          );
          try {
            socket.close(1000);
          } catch (error) {
            console.debug("[fact-checker] close after stop failed:", error);
          }
          resolve();
        }, STOP_FLUSH_TIMEOUT_MS);
        socket.addEventListener(
          "close",
          () => {
            clearTimeout(timeoutId);
            resolve();
          },
          {once: true}
        );
      });
    } else if (socket) {
      try {
        socket.close(1000);
      } catch (error) {
        console.debug("[fact-checker] close failed:", error);
      }
    }
    this.socket = null;
    this.emitState("closed");
  }

  /** Immediate teardown — no stop frame, no waiting. Used on fatal errors. */
  destroy() {
    this.closedByClient = true;
    this.clearReconnectTimer();
    if (this.socket) {
      try {
        this.socket.close(1000);
      } catch (error) {
        console.debug("[fact-checker] close failed:", error);
      }
      this.socket = null;
    }
    this.emitState("closed");
  }

  scheduleReconnect() {
    const now = Date.now();
    if (this.disconnectedSince === null) {
      this.disconnectedSince = now;
    }
    if (now - this.disconnectedSince >= GIVE_UP_AFTER_MS) {
      console.error(
        "[fact-checker] backend unreachable for 5 minutes; giving up"
      );
      this.emitState("closed");
      this.onGiveUp?.("backend unreachable for 5 minutes");
      return;
    }
    this.reconnectAttempt += 1;
    const delayMs = Math.min(
      MAX_BACKOFF_MS,
      INITIAL_BACKOFF_MS * 2 ** (this.reconnectAttempt - 1)
    );
    this.emitState("reconnecting");
    this.reconnectTimerId = setTimeout(() => {
      this.connect();
    }, delayMs);
  }

  clearReconnectTimer() {
    if (this.reconnectTimerId !== null) {
      clearTimeout(this.reconnectTimerId);
      this.reconnectTimerId = null;
    }
  }

  emitState(wsState) {
    this.onStateChange?.(wsState, this.reconnectAttempt);
  }
}
