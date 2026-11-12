/**
 * Opening the extension's full-tab app (app/app.html) from anywhere.
 *
 * Reuses an app tab that is already open instead of piling up duplicates:
 * the tab is focused and, when a route is given, pointed at it. Used by the
 * service worker (on install, and on behalf of content scripts, which cannot
 * open extension pages themselves) and by the toolbar popup.
 */

// Promise-safe API namespace (see shared/capabilities.js § namespace idiom).
const chrome = globalThis.browser ?? globalThis.chrome;

const APP_PAGE = "app/app.html";

/** Routes of the app (mirrors app/lib/routes.mjs). */
export const APP_ROUTE = Object.freeze({
  WELCOME: "/welcome",
  HOME: "/home",
  PROVIDERS: "/providers",
  SETTINGS: "/settings",
});

/**
 * Find an open app tab without the "tabs" permission. `runtime.getContexts`
 * lists the extension's own pages (Chrome 116+); Firefox lacks it, and there
 * `tabs.query` may see the extension's own tabs.
 *
 * @returns {Promise<{tabId: number, windowId: number}|null>}
 */
const findAppTab = async () => {
  const appUrl = chrome.runtime.getURL(APP_PAGE);
  try {
    if (chrome.runtime.getContexts) {
      const contexts = await chrome.runtime.getContexts({contextTypes: ["TAB"]});
      const context = contexts.find((candidate) => candidate.documentUrl?.startsWith(appUrl));
      return context ? {tabId: context.tabId, windowId: context.windowId} : null;
    }
    const tabs = await chrome.tabs.query({url: `${appUrl}*`});
    return tabs.length > 0 ? {tabId: tabs[0].id, windowId: tabs[0].windowId} : null;
  } catch (error) {
    console.debug("[fact-checker] looking for an open app tab failed:", error);
    return null;
  }
};

/**
 * Open the app, or bring its existing tab to the front.
 *
 * @param {string} [route] - one of APP_ROUTE; omitted, the app decides
 *   (welcome for a new install, home otherwise)
 * @returns {Promise<void>}
 */
export const openAppTab = async (route) => {
  const url = chrome.runtime.getURL(route ? `${APP_PAGE}#${route}` : APP_PAGE);
  const existing = await findAppTab();
  if (existing) {
    await chrome.tabs.update(existing.tabId, route ? {active: true, url} : {active: true});
    await chrome.windows.update(existing.windowId, {focused: true});
    return;
  }
  await chrome.tabs.create({url});
};
