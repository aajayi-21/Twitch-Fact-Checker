// The console's state: signals mirroring the server, plus client-only bits.
//
// Data-flow doctrine (unchanged from the first panel): the events socket is
// a DOORBELL, never the source of truth — any interesting push triggers a
// debounced /bot/status refetch, and pollers reconcile regardless, so a
// dropped WS frame can never leave the UI lying. Every bot mutation returns
// the full status payload, which is assigned straight into the signal.

import { signal, computed } from "@preact/signals";
import * as api from "./api.mjs";

// ---- server mirrors -------------------------------------------------------- //
export const health = signal(null);
export const botStatus = signal(null);
export const sessionConfig = signal(null);
export const overlayConfig = signal(null);
export const setupStatus = signal(null); // /setup/status (LLM)
export const twitchStatus = signal(null); // /setup/twitch
export const topics = signal([]);
export const decisions = signal([]);
export const consoleStats = signal(null);
export const summary = signal(null);
export const channels = signal([]);

// ---- client state ----------------------------------------------------------- //
export const wsConnected = signal(false);
export const selectedIdx = signal(0);
export const transcriptTail = signal([]); // last ~10 transcript lines
export const notice = signal(null); // {kind: "error"|"ok", text}
export const nowMs = signal(Date.now()); // 1 Hz tick: TTLs, elapsed, mute
export const fetchedAtMs = signal(0); // performance.now() of last botStatus

// ---- deriveds ---------------------------------------------------------------- //
export const bot = computed(() => botStatus.value?.bot ?? null);
export const queue = computed(() => bot.value?.queue ?? []);
export const dryRun = computed(() => botStatus.value?.dry_run ?? true);
export const topicColors = computed(() =>
  Object.fromEntries(topics.value.map((topic) => [topic.slug, topic.color]))
);
export const topicLabels = computed(() =>
  Object.fromEntries(topics.value.map((topic) => [topic.slug, topic.label]))
);

// ---- notices ------------------------------------------------------------------ //
let noticeTimer = null;
export function flash(kind, text, ms = kind === "error" ? 6000 : 2200) {
  notice.value = { kind, text };
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => (notice.value = null), ms);
}

// ---- fetch helpers -------------------------------------------------------------- //
const quiet = async (fn) => {
  try {
    return await fn();
  } catch {
    return null; // pollers stay silent; the status dots tell the story
  }
};

export async function refreshBot() {
  const status = await quiet(api.getBotStatus);
  if (status) {
    botStatus.value = status;
    fetchedAtMs.value = performance.now();
    const queueLength = status.bot?.queue?.length ?? 0;
    selectedIdx.value = Math.min(
      selectedIdx.value,
      Math.max(0, queueLength - 1)
    );
    document.title = queueLength ? `(${queueLength}) Fact-Checker` : "Fact-Checker";
  } else {
    health.value = null; // backend unreachable: kill the dot
  }
}

export const refreshHealth = async () => {
  health.value = await quiet(api.getHealth);
};
export const refreshSession = async () => {
  const config = await quiet(api.getSessionConfig);
  if (config) sessionConfig.value = config;
};
export const refreshOverlayConfig = async () => {
  const config = await quiet(api.getOverlayConfig);
  if (config) overlayConfig.value = config;
};
export const refreshSetup = async () => {
  setupStatus.value = await quiet(api.getSetupStatus);
  twitchStatus.value = await quiet(api.getTwitchStatus);
};
export const refreshDecisions = async (options) => {
  const rows = await quiet(() => api.getDecisions(options));
  if (rows) decisions.value = rows;
};
export const refreshConsoleStats = async () => {
  const stats = await quiet(api.getConsoleStats);
  if (stats) consoleStats.value = stats;
};
export const refreshAnalytics = async () => {
  const [summaryResult, channelsResult] = await Promise.all([
    quiet(api.getSummary),
    quiet(api.getChannels),
  ]);
  if (summaryResult) summary.value = summaryResult;
  if (channelsResult) channels.value = channelsResult;
};

// ---- actions (bot mutations assign the returned status directly) ---------------- //
const act = async (call, okText) => {
  try {
    const status = await call();
    botStatus.value = status;
    fetchedAtMs.value = performance.now();
    if (okText) flash("ok", okText);
    return true;
  } catch (error) {
    // The backend's exact clamp/validation reason, verbatim.
    flash("error", String(error.message || error));
    return false;
  }
};

export const setMode = (mode) => act(() => api.postMode(mode));
export const setMute = (muted, duration) =>
  act(() => api.postMute(muted, duration));
export const setDryRun = (dryRun) =>
  act(
    () => api.postDryRun(dryRun),
    dryRun ? "Dry run ON — posting nothing" : "LIVE — the bot may post"
  );
export const approve = (postId) => act(() => api.postApprove(postId));
export const skip = (postId) => act(() => api.postSkip(postId));
export const setTrusted = (trusted) => act(() => api.postTrust(trusted));
export const retract = (handle) =>
  act(() => api.postRetract(handle), "Retraction posted");
export const saveBotSettings = (partial) =>
  act(() => api.postBotSettings(partial), "Settings applied");

export async function savePipeline(partial) {
  try {
    const config = await api.postSessionConfig(partial);
    sessionConfig.value = config;
    return true;
  } catch (error) {
    flash("error", String(error.message || error));
    return false;
  }
}

export async function saveOverlayConfig(config) {
  try {
    overlayConfig.value = await api.postOverlayConfig(config);
    flash("ok", "Overlay updated live");
    return true;
  } catch (error) {
    flash("error", String(error.message || error));
    return false;
  }
}

// ---- tickers + pollers ------------------------------------------------------------ //
export function startTickers() {
  setInterval(() => (nowMs.value = Date.now()), 1000);
  setInterval(() => {
    refreshBot();
    refreshHealth();
  }, 5000);
  setInterval(refreshConsoleStats, 15000);
  setInterval(refreshSession, 15000);
}

export function pushTranscript(text) {
  transcriptTail.value = [...transcriptTail.value.slice(-9), text];
}

// Debounced doorbell for WS pushes.
let refreshTimer = null;
export function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => {
    refreshBot();
    refreshConsoleStats();
  }, 250);
}

export async function boot() {
  await Promise.all([
    api.getTopics().then((payload) => (topics.value = payload.topics || [])).catch(() => {}),
    refreshHealth(),
    refreshBot(),
    refreshSession(),
    refreshOverlayConfig(),
    refreshSetup(),
    refreshConsoleStats(),
  ]);
  startTickers();
}
