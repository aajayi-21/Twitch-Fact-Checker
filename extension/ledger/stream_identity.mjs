/**
 * Working out which channel a tab is showing.
 *
 * Identity is (platform, channel id). Twitch and Kick put the channel in the
 * URL. YouTube and Rumble do not — a watch URL carries a video id — so for
 * those the content script reads the channel link shown under the player
 * (content/page_identity.js) and this module turns that link into an id.
 *
 * Pure, so it is unit-tested under Node.
 */

const PLATFORM_BY_HOSTNAME = Object.freeze({
  "twitch.tv": "twitch",
  "youtube.com": "youtube",
  "kick.com": "kick",
  "rumble.com": "rumble",
});

const PLATFORM_ORIGIN = Object.freeze({
  twitch: "https://www.twitch.tv",
  youtube: "https://www.youtube.com",
  kick: "https://kick.com",
  rumble: "https://rumble.com",
});

// First path segments that are site sections, not channels.
const TWITCH_SECTIONS = new Set([
  "directory", "videos", "settings", "subscriptions", "inventory", "drops", "wallet",
  "search", "downloads", "turbo", "jobs", "p", "friends", "store", "privacy", "prime",
  "following", "u", "team", "collections", "clip",
]);
// Twitch wrappers whose SECOND segment is the channel.
const TWITCH_WRAPPERS = new Set(["popout", "moderator", "embed"]);
const KICK_SECTIONS = new Set([
  "browse", "categories", "category", "following", "dashboard", "video", "videos",
  "search", "settings", "auth", "transactions", "subscriptions", "community-guidelines",
]);

// " - Twitch"-style suffix on tab titles (Kick's is " - Watch Live on KICK").
const TITLE_SUFFIX = /\s*[-–—|·]\s*(Watch Live on )?(Twitch|YouTube|Kick|Rumble)\s*$/i;
// YouTube prefixes the title with an unread-notification count: "(3) Title".
const TITLE_COUNT_PREFIX = /^\(\d+\+?\)\s*/;

const SLUG = /^[\w.-]+$/;

const parseUrl = (url, base) => {
  try {
    return new URL(url, base);
  } catch {
    return null;
  }
};

/**
 * @param {string} url
 * @returns {string|null} "twitch" | "youtube" | "kick" | "rumble"
 */
export const platformFromUrl = (url) => {
  const hostname = parseUrl(url)?.hostname.replace(/^(www|m)\./, "");
  return PLATFORM_BY_HOSTNAME[hostname] ?? null;
};

/**
 * The channel a path names, when it names one.
 *
 * @param {string|null} platform
 * @param {string} pathname
 * @returns {string|null} a channel id as stored in the ledger
 */
export const channelFromPath = (platform, pathname) => {
  const segments = String(pathname ?? "")
    .split("/")
    .filter(Boolean)
    .map((segment) => {
      try {
        return decodeURIComponent(segment);
      } catch {
        return segment;
      }
    });
  const [first, second] = segments;
  if (!first) {
    return null;
  }
  if (platform === "twitch") {
    const channel = TWITCH_WRAPPERS.has(first.toLowerCase()) ? second : first;
    return channel && SLUG.test(channel) && !TWITCH_SECTIONS.has(channel.toLowerCase())
      ? channel.toLowerCase()
      : null;
  }
  if (platform === "kick") {
    return SLUG.test(first) && !KICK_SECTIONS.has(first.toLowerCase())
      ? first.toLowerCase()
      : null;
  }
  if (platform === "youtube") {
    if (first.startsWith("@") && first.length > 1) {
      return first.toLowerCase();
    }
    // "UC…" channel ids are case-sensitive.
    if (first === "channel" && /^UC[\w-]{22}$/.test(second ?? "")) {
      return second;
    }
    // Legacy vanity URLs; kept distinct from handles.
    if ((first === "c" || first === "user") && second) {
      return `${first}/${second.toLowerCase()}`;
    }
    return null;
  }
  if (platform === "rumble") {
    if (first === "c" && second) {
      return second.toLowerCase();
    }
    if (first === "user" && second) {
      return `user/${second.toLowerCase()}`;
    }
    return null;
  }
  return null;
};

/**
 * The channel an owner link points at. Only links to the platform's own
 * site count — a creator's off-site link under the player is not a channel.
 *
 * @param {string|null} platform
 * @param {string|null|undefined} href - absolute or site-relative
 * @returns {string|null}
 */
export const channelFromOwnerHref = (platform, href) => {
  const origin = PLATFORM_ORIGIN[platform];
  if (!origin || typeof href !== "string" || !href) {
    return null;
  }
  const url = parseUrl(href, origin);
  if (!url || platformFromUrl(url.href) !== platform) {
    return null;
  }
  return channelFromPath(platform, url.pathname);
};

/**
 * A stream's address without the parts that vary per visit (timestamps,
 * playlists, referral tags), so the same video is always the same URL.
 *
 * @param {string} url
 * @returns {string|null}
 */
export const cleanStreamUrl = (url) => {
  const parsed = parseUrl(url);
  if (!parsed || !/^https?:$/.test(parsed.protocol)) {
    return null;
  }
  const videoId = parsed.searchParams.get("v");
  if (platformFromUrl(url) === "youtube" && parsed.pathname === "/watch" && videoId) {
    return `${parsed.origin}/watch?v=${encodeURIComponent(videoId)}`;
  }
  return `${parsed.origin}${parsed.pathname}`;
};

/**
 * @param {string|null|undefined} title - a tab title
 * @returns {string|null}
 */
export const cleanStreamTitle = (title) =>
  String(title ?? "")
    .replace(TITLE_COUNT_PREFIX, "")
    .replace(TITLE_SUFFIX, "")
    .trim() || null;

/**
 * Identity of the page a tab is showing.
 *
 * @param {object} page
 * @param {string} [page.url]
 * @param {string} [page.title]
 * @param {string|null} [page.ownerHref] - the channel link under the player
 * @param {string|null} [page.ownerName] - that link's visible name
 * @returns {{platform: string|null, channelId: string|null, channelName: string|null,
 *   streamUrl: string|null, streamTitle: string|null}}
 */
export const identityFromPage = ({url = "", title = "", ownerHref = null, ownerName = null} = {}) => {
  const platform = platformFromUrl(url);
  const fromPath = channelFromPath(platform, parseUrl(url)?.pathname ?? "");
  // The address is authoritative when it names a channel; the owner link
  // covers pages that are addressed by video id.
  const channelId = fromPath ?? channelFromOwnerHref(platform, ownerHref);
  const name = typeof ownerName === "string" ? ownerName.replace(/\s+/g, " ").trim() : "";
  return {
    platform,
    channelId,
    channelName: channelId ? name || null : null,
    streamUrl: cleanStreamUrl(url),
    streamTitle: cleanStreamTitle(title),
  };
};
