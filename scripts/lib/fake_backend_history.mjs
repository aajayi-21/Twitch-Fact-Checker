/**
 * The parts of the fake backend (scripts/run_app_e2e.mjs) that the history
 * and usage screens need:
 *
 *   - a seeded `fact_checker.db` worth of past sessions, served in the shapes
 *     of GET /stats/sessions and /stats/sessions/{id} (backend/app/db.py);
 *   - a minimal WebSocket endpoint that plays one scripted session over
 *     /ws/audio with the real frame vocabulary (backend/app/models.py), so
 *     the extension's capture path can be driven end to end with no speech
 *     model and no provider calls.
 *
 * No dependencies: the WebSocket framing (RFC 6455) is done by hand.
 */

import {createHash} from "node:crypto";

const HOUR_MS = 3_600_000;
const DAY_MS = 24 * HOUR_MS;

const iso = (ms) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");

export const SEARCHABLE_CLAIM = "The Eiffel Tower is 450 metres tall.";

/**
 * Past sessions as the real backend would have recorded them.
 *
 *   s-twitch   Twitch · DebateStreamer    45 checks (40 adjudicated: enough
 *              for shares — 24 True, 10 False, 6 Misleading — plus 5 Unverified)
 *   s-youtube  YouTube · @newsanalyst     3 checks, one of them searchable
 *   s-kick     Kick · DebateStreamer      2 checks (same name, other site)
 *   s-old      Twitch · oldchannel        1 check, over a year ago
 *   s-quiet    YouTube, no channel        nothing heard
 *
 * @param {number} [now]
 * @returns {Map<string, {session: object, claims: object[], verdicts: object[]}>}
 */
export const seedHistory = (now = Date.now()) => {
  const sessions = new Map();
  const add = ({id, platform, channel, title, startMs, hours, labels, costPerCheck = 0.011}) => {
    const claims = [];
    const verdicts = [];
    labels.forEach(([label, claimText, extra = {}], index) => {
      const checkedMs = startMs + (index + 1) * 60_000;
      claims.push({
        id: `${id}-c${index}`,
        session_id: id,
        text: claimText,
        normalized: claimText.toLowerCase(),
        topic: extra.topic ?? "politics",
        check_worthiness: 0.9,
        stream_time_s: (index + 1) * 60,
        gated_at: iso(checkedMs - 3000),
        outcome: "verified",
        completed_at: iso(checkedMs),
        has_visual_cue: 0,
        gate_pass_id: null,
      });
      verdicts.push({
        id: `${id}-v${index}`,
        claim_id: `${id}-c${index}`,
        session_id: id,
        label,
        explanation: extra.explanation ?? `Checked against two sources; the claim is ${label.toLowerCase()}.`,
        checked_at: iso(checkedMs),
        used_fallback: 0,
        latency_ms: 2000 + index * 10,
        provider: "anthropic",
        model: "claude-haiku-5-5",
        evidence: label === "UNVERIFIED" ? "partial" : "strong",
        sources:
          label === "UNVERIFIED"
            ? []
            : [{rank: 0, url: `https://example.org/${id}/${index}`, domain: "example.org", title: `Source ${index}`}],
        feedback: null,
      });
    });
    // One claim that was heard and not checked, so the funnel is visible.
    claims.push({
      id: `${id}-skipped`,
      session_id: id,
      text: "People say all sorts of things about this.",
      normalized: "people say all sorts of things about this",
      topic: "other",
      check_worthiness: 0.2,
      stream_time_s: 30,
      gated_at: iso(startMs + 30_000),
      outcome: "below_threshold",
      completed_at: null,
      has_visual_cue: 0,
      gate_pass_id: null,
    });
    sessions.set(id, {
      session: {
        id,
        platform,
        channel,
        title,
        started_at: iso(startMs),
        ended_at: iso(startMs + hours * HOUR_MS),
        speech_seconds: hours * 3000,
        audio_seconds: hours * 3600,
        gate_calls: Math.round(hours * 50),
        verify_calls: labels.length,
        est_cost_usd: Number((labels.length * costPerCheck).toFixed(4)),
        stt_drop_counts: "{}",
      },
      claims,
      verdicts,
    });
  };

  const many = (label, count) =>
    Array.from({length: count}, (unused, index) => [label, `Debate claim ${label} number ${index + 1}.`]);

  add({
    id: "s-twitch",
    platform: "twitch",
    channel: "DebateStreamer",
    title: "Election night debate",
    startMs: now - 3 * DAY_MS,
    hours: 2,
    labels: [...many("TRUE", 24), ...many("FALSE", 10), ...many("MISLEADING", 6), ...many("UNVERIFIED", 5)],
  });
  add({
    id: "s-youtube",
    platform: "youtube",
    channel: "@newsanalyst",
    title: "Budget speech reaction",
    startMs: now - 5 * DAY_MS,
    hours: 1,
    labels: [
      ["FALSE", SEARCHABLE_CLAIM, {
        topic: "history",
        explanation: "The Eiffel Tower is about 330 metres tall including its antennas, not 450 metres.",
      }],
      ["TRUE", "The speed of light is about 300,000 kilometres per second.", {topic: "science_tech"}],
      ["UNVERIFIED", "Police are making arrests at the port right now."],
    ],
  });
  add({
    id: "s-kick",
    platform: "kick",
    channel: "DebateStreamer",
    title: "Late show",
    startMs: now - 6 * DAY_MS,
    hours: 0.5,
    labels: [
      ["TRUE", "Unemployment fell last quarter."],
      ["MISLEADING", "Crime doubled in a year."],
    ],
  });
  add({
    id: "s-old",
    platform: "twitch",
    channel: "oldchannel",
    title: "A long time ago",
    startMs: now - 400 * DAY_MS,
    hours: 1,
    labels: [["TRUE", "An old claim from last year."]],
  });
  add({
    id: "s-quiet",
    platform: "youtube",
    channel: null,
    title: "Music stream",
    startMs: now - 2 * DAY_MS,
    hours: 0.25,
    labels: [],
  });
  return sessions;
};

/**
 * @param {Map<string, object>} history - from seedHistory
 * @returns {object[]} session rows, newest first (GET /stats/sessions)
 */
export const listSessions = (history) =>
  [...history.values()]
    .map((entry) => entry.session)
    .sort((first, second) => (first.started_at < second.started_at ? 1 : -1));

/** Frames of the scripted live session, in the order they are sent. */
export const LIVE_SESSION_ID = "e2e-live-session";
export const LIVE_CLAIM = "Inflation is at forty percent this year.";

export const liveSessionFrames = () => [
  {type: "claim", id: "live-c0", text: "Everyone knows this game is rigged.", topic: "gaming", check_worthiness: 0.1, outcome: "below_threshold"},
  {type: "claim", id: "live-c1", text: LIVE_CLAIM, topic: "money", check_worthiness: 0.95, outcome: "pending"},
  {type: "status", stage: "verifying", claim: LIVE_CLAIM},
  // A gate call on Anthropic: no cost reported, so the app estimates it.
  {type: "usage", stage: "gate", provider: "anthropic", model: "claude-haiku-5-5", input_tokens: 268, cached_input_tokens: 2959, output_tokens: 120, web_searches: 0},
  {
    type: "verdict",
    id: "live-v1",
    claim: LIVE_CLAIM,
    topic: "money",
    label: "FALSE",
    explanation: "Official figures put annual inflation at 3.1 percent, not forty.",
    sources: [{url: "https://www.ons.gov.uk/economy/inflationandpriceindices", title: "Inflation and price indices"}],
    checked_at: iso(Date.now()),
    used_fallback: false,
    claim_id: "live-c1",
    latency_ms: 2300,
    provider: "anthropic",
    model: "claude-haiku-5-5",
  },
  {type: "claim", id: "live-c1", text: LIVE_CLAIM, topic: "money", check_worthiness: 0.95, outcome: "verified"},
  {type: "usage", stage: "verify", provider: "anthropic", model: "claude-haiku-5-5", input_tokens: 8000, cached_input_tokens: 0, output_tokens: 200, web_searches: 1, verdict_id: "live-v1"},
  // An OpenRouter call with its exact cost, and one with no price at all.
  {type: "usage", stage: "verify", provider: "openrouter", model: "google/gemini-3.5-flash-lite", input_tokens: 2100, cached_input_tokens: 0, output_tokens: 90, web_searches: 1, cost_usd: 0.0078},
  {type: "usage", stage: "gate", provider: "openrouter", model: "some/unpriced-model", input_tokens: 500, cached_input_tokens: 0, output_tokens: 20, web_searches: 0},
];

const WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
const OPCODE = Object.freeze({TEXT: 0x1, CLOSE: 0x8, PING: 0x9, PONG: 0xa});

/**
 * Complete a WebSocket handshake on an HTTP upgrade and return a connection
 * that sends JSON text frames. Incoming text frames are passed to `onText`;
 * binary frames (the audio) are read and dropped.
 *
 * @param {import("node:http").IncomingMessage} request
 * @param {import("node:net").Socket} socket
 * @param {(text: string, connection: {sendJson: Function, close: Function}) => void} onText
 */
export const acceptWebSocket = (request, socket, onText) => {
  const accept = createHash("sha1")
    .update(`${request.headers["sec-websocket-key"]}${WEBSOCKET_GUID}`)
    .digest("base64");
  socket.write(
    [
      "HTTP/1.1 101 Switching Protocols",
      "Upgrade: websocket",
      "Connection: Upgrade",
      `Sec-WebSocket-Accept: ${accept}`,
      "",
      "",
    ].join("\r\n")
  );

  const sendFrame = (opcode, payload) => {
    if (socket.destroyed) {
      return;
    }
    let header;
    if (payload.length < 126) {
      header = Buffer.from([0x80 | opcode, payload.length]);
    } else if (payload.length < 65_536) {
      header = Buffer.alloc(4);
      header[0] = 0x80 | opcode;
      header[1] = 126;
      header.writeUInt16BE(payload.length, 2);
    } else {
      header = Buffer.alloc(10);
      header[0] = 0x80 | opcode;
      header[1] = 127;
      header.writeBigUInt64BE(BigInt(payload.length), 2);
    }
    socket.write(Buffer.concat([header, payload]));
  };
  const connection = {
    sendJson: (value) => sendFrame(OPCODE.TEXT, Buffer.from(JSON.stringify(value), "utf8")),
    close: (code = 1000) => {
      const body = Buffer.alloc(2);
      body.writeUInt16BE(code);
      sendFrame(OPCODE.CLOSE, body);
      socket.end();
    },
  };

  let pending = Buffer.alloc(0);
  socket.on("data", (chunk) => {
    pending = Buffer.concat([pending, chunk]);
    for (;;) {
      if (pending.length < 2) {
        return;
      }
      const opcode = pending[0] & 0x0f;
      const masked = (pending[1] & 0x80) !== 0;
      let length = pending[1] & 0x7f;
      let offset = 2;
      if (length === 126) {
        if (pending.length < 4) {
          return;
        }
        length = pending.readUInt16BE(2);
        offset = 4;
      } else if (length === 127) {
        if (pending.length < 10) {
          return;
        }
        length = Number(pending.readBigUInt64BE(2));
        offset = 10;
      }
      const maskLength = masked ? 4 : 0;
      const end = offset + maskLength + length;
      if (pending.length < end) {
        return;
      }
      const payload = Buffer.from(pending.subarray(offset + maskLength, end));
      if (masked) {
        const mask = pending.subarray(offset, offset + 4);
        for (let index = 0; index < payload.length; index += 1) {
          payload[index] ^= mask[index % 4];
        }
      }
      pending = pending.subarray(end);
      if (opcode === OPCODE.TEXT) {
        onText(payload.toString("utf8"), connection);
      } else if (opcode === OPCODE.PING) {
        sendFrame(OPCODE.PONG, payload);
      } else if (opcode === OPCODE.CLOSE) {
        socket.end();
        return;
      }
    }
  });
  socket.on("error", () => {});
  return connection;
};
