/**
 * Spike S2 — offscreen document: make provider calls where the browser
 * engine will make them.
 *
 * Protocol (BroadcastChannel "provider-spike", kept off chrome.runtime
 * messaging so the production service worker never sees spike traffic):
 *
 *   in   {type: "call", id, providerId, operation, args}
 *   out  {type: "result", id, ok: true, value, ms}
 *   out  {type: "result", id, ok: false, error: {kind, message, status}, ms}
 *
 * `args` carries the API key. It is used for the request and nothing else:
 * never logged, never echoed back, never stored.
 */

import {ProviderError} from "../engine/providers/common.mjs";
import {getProvider} from "../engine/providers/index.mjs";

const ALLOWED_OPERATIONS = new Set(["validateKey", "runGate", "runVerify"]);

const channel = new BroadcastChannel("provider-spike");

channel.addEventListener("message", async (event) => {
  const message = event.data;
  if (message?.type !== "call") {
    return;
  }
  const startedAt = performance.now();
  const reply = (body) =>
    channel.postMessage({type: "result", id: message.id, ms: performance.now() - startedAt, ...body});
  try {
    if (!ALLOWED_OPERATIONS.has(message.operation)) {
      throw new Error(`unknown operation "${message.operation}"`);
    }
    const provider = getProvider(message.providerId);
    const value =
      message.operation === "validateKey"
        ? await provider.validateKey(message.args.apiKey)
        : await provider[message.operation](message.args);
    reply({ok: true, value: value ?? null});
  } catch (error) {
    reply({
      ok: false,
      error: {
        kind: error instanceof ProviderError ? error.kind : "internal",
        message: String(error?.message ?? error),
        status: error instanceof ProviderError ? error.status : null,
      },
    });
  }
});

channel.postMessage({type: "offscreen-ready"});
