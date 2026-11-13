/**
 * Reads who owns the stream on this page — the channel link shown with the
 * player — so YouTube and Rumble sessions can be attributed to a channel.
 * (Their URLs carry a video id; Twitch and Kick URLs name the channel, and
 * need nothing from the page.)
 *
 * Returns the raw link only. Turning it into a channel id is done in the
 * service worker by ledger/stream_identity.mjs, which is unit-tested.
 *
 * Classic content script, loaded before content/content.js: defines
 * `self.readStreamOwner` (no imports).
 */

(() => {
  "use strict";

  /**
   * Where each site puts the channel link, most specific first. Several
   * candidates per site because these layouts change without notice; a miss
   * simply leaves the session without a channel.
   */
  const OWNER_LINK_SELECTORS = Object.freeze({
    "youtube.com": [
      "ytd-watch-metadata ytd-video-owner-renderer ytd-channel-name a[href]",
      "ytd-video-owner-renderer ytd-channel-name a[href]",
      "ytd-video-owner-renderer a[href^='/@']",
      "ytd-video-owner-renderer a[href^='/channel/']",
      "#owner a[href^='/@']",
      "#owner a[href^='/channel/']",
    ],
    "rumble.com": [
      "a.media-by--a[href]",
      ".media-by-channel-container a[href]",
      ".media-heading-info a[href^='/c/']",
      ".media-heading-info a[href^='/user/']",
    ],
  });

  /** Where the display name is, when it is not the link's own text. */
  const OWNER_NAME_SELECTORS = Object.freeze({
    "rumble.com": [".media-heading-name"],
  });

  const firstMatch = (root, selectors) => {
    for (const selector of selectors ?? []) {
      const element = root.querySelector(selector);
      if (element) {
        return element;
      }
    }
    return null;
  };

  const visibleText = (element) => (element?.textContent ?? "").replace(/\s+/g, " ").trim();

  /**
   * @returns {{ownerHref: string|null, ownerName: string|null}}
   */
  self.readStreamOwner = () => {
    const host = location.hostname.replace(/^www\./, "");
    const link = firstMatch(document, OWNER_LINK_SELECTORS[host]);
    if (!link) {
      return {ownerHref: null, ownerName: null};
    }
    const nameElement = firstMatch(link, OWNER_NAME_SELECTORS[host]) ?? link;
    return {
      ownerHref: link.getAttribute("href"),
      ownerName: visibleText(nameElement) || null,
    };
  };
})();
