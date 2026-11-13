/**
 * Hash routes of the app, and the rule for where a visit should land.
 * Pure, so it is unit-tested under Node.
 */

export const ROUTES = Object.freeze({
  WELCOME: "/welcome",
  HOME: "/home",
  HISTORY: "/history",
  USAGE: "/usage",
  PROVIDERS: "/providers",
  SETTINGS: "/settings",
});

const KNOWN_ROUTES = new Set(Object.values(ROUTES));

/**
 * @param {string} hash - location.hash, e.g. "#/settings"
 * @returns {string|null} a known route, or null for anything else
 */
export const parseRoute = (hash) => {
  const route = String(hash ?? "").replace(/^#/, "").replace(/\/+$/, "");
  return KNOWN_ROUTES.has(route) ? route : null;
};

/**
 * Where to send a visit that names no (valid) route.
 *
 * The welcome flow is for people who have never set the app up. Someone
 * whose backend is already connected to a provider — every existing user at
 * the moment this app replaces the old settings page — goes straight home.
 *
 * @param {object} state
 * @param {boolean} state.onboardingCompleted
 * @param {boolean} state.providerConfigured - the backend reports a provider
 * @returns {string}
 */
export const landingRoute = ({onboardingCompleted, providerConfigured}) =>
  onboardingCompleted || providerConfigured ? ROUTES.HOME : ROUTES.WELCOME;
