/**
 * Browser capability detection — the one place that knows how this
 * extension captures audio in the current browser.
 *
 * Chrome and Firefox need genuinely different capture architectures, and
 * the difference is NOT cosmetic:
 *
 *   Chrome  — chrome.tabCapture mints a stream id, an offscreen document
 *             redeems it with getUserMedia and owns the whole session
 *             (MediaStream + AudioContext + WebSocket).
 *   Firefox — implements NEITHER tabCapture nor the offscreen API
 *             (bugzilla 1391223), so there is no way to capture a tab's
 *             audio and nowhere Chrome-shaped to put it. Instead the
 *             content script taps the page's own <video> element through
 *             an AudioContext and streams PCM to the background EVENT PAGE
 *             (Firefox MV3 keeps a DOM-bearing background page rather than
 *             a service worker), which owns the WebSocket.
 *
 * Why the WebSocket cannot live in the Firefox content script: under MV3 a
 * content script's network requests run in the PAGE's context and are
 * subject to the page's CSP, and Twitch/YouTube ship a `connect-src` that
 * would block ws://127.0.0.1. Extension pages have no such restriction.
 */

/**
 * THE NAMESPACE IDIOM used across this extension:
 *
 *     const chrome = globalThis.browser ?? globalThis.chrome;
 *
 * Every file that calls an extension API declares that line in its own
 * scope (module scope, or inside a content script's IIFE), shadowing the
 * global. Rationale: only Firefox's `browser.*` namespace is guaranteed to
 * return promises — its `chrome.*` alias is the callback-style one — and
 * this codebase `await`s API calls everywhere. Shadowing keeps all ~70 call
 * sites and their docs written in the canonical `chrome.*` spelling while
 * silently binding to `browser.*` on Firefox. Chrome has no `browser`
 * global, so it falls through to its own promise-returning MV3 `chrome`.
 */
const chrome = globalThis.browser ?? globalThis.chrome;

/**
 * True when the browser can capture tab audio the Chrome way (tabCapture +
 * an offscreen document). False on Firefox, which routes through the
 * content-script capture path instead.
 *
 * @type {boolean}
 */
export const HAS_OFFSCREEN_CAPTURE = Boolean(
  chrome?.offscreen?.createDocument && chrome?.tabCapture?.getMediaStreamId
);
