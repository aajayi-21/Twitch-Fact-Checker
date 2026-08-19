// One thin fetch wrapper per endpoint. Errors normalize to Error(detail) so
// views can surface the backend's exact clamp/validation reasons verbatim —
// refused loudly, never rewritten client-side.

async function request(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = `${response.status}`;
    try {
      const body = await response.json();
      detail =
        typeof body.detail === "string"
          ? body.detail
          : JSON.stringify(body.detail ?? body);
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return response.json();
}

const post = (path, body) =>
  request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });

// ---- reads ---------------------------------------------------------------- //
export const getHealth = () => request("/healthz");
export const getBotStatus = () => request("/bot/status");
export const getSessionConfig = () => request("/session/config");
export const getOverlayConfig = () => request("/overlay/config");
export const getSetupStatus = () => request("/setup/status");
export const getTwitchStatus = () => request("/setup/twitch");
export const getTopics = () => request("/meta/topics");
export const getDecisions = ({ status, limit = 200, before } = {}) => {
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  params.set("limit", String(limit));
  if (before) params.set("before", before);
  return request(`/bot/posts?${params}`);
};
export const getConsoleStats = () => request("/stats/console");
export const getSummary = () => request("/stats/summary");
export const getChannels = () => request("/stats/channels");
export const getChatStats = () => request("/stats/chat");
export const getBotSettings = () => request("/bot/settings");

// ---- bot mutations (each returns the full /bot/status payload) ------------ //
export const postMode = (mode) => post("/bot/mode", { mode });
export const postMute = (muted, duration) =>
  post("/bot/mute", duration ? { muted, duration } : { muted });
export const postDryRun = (dryRun) => post("/bot/dry-run", { dry_run: dryRun });
export const postApprove = (postId) => post(`/bot/queue/${postId}/post`, {});
export const postSkip = (postId) => post(`/bot/queue/${postId}/skip`, {});
export const postTrust = (trusted) => post("/bot/trust", { trusted });
export const postRetract = (handle) => post("/bot/retract", { handle });
export const postBotSettings = (partial) => post("/bot/settings", partial);

// ---- pipeline / overlay / setup ------------------------------------------- //
export const postSessionConfig = (partial) => post("/session/config", partial);
export const postOverlayConfig = (config) => post("/overlay/config", config);
export const postTestVerdict = (label = "FALSE") =>
  post("/events/test", { label });
export const postLlmCredentials = (body) => post("/setup/credentials", body);
export const postTwitchToken = (token, channel) =>
  post("/setup/twitch", { token, channel: channel || null });
export const postDeviceStart = () => post("/setup/twitch/device", {});
export const postDevicePoll = (channel) =>
  post("/setup/twitch/device/poll", { channel: channel || null });
