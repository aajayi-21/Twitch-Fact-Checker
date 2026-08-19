// The console's events socket: a doorbell (never the source of truth).
// Reconnects forever at 0.5s -> 15s backoff; the pollers keep the UI honest
// while it is down, and the status dot shows the drop.
import {
  wsConnected,
  scheduleRefresh,
  pushTranscript,
  refreshOverlayConfig,
} from "./store.mjs";

let backoffMs = 500;

export function connectEvents() {
  const params = new URLSearchParams({
    types: "verdict,status,error,contradiction,transcript,overlay_config",
  });
  const socket = new WebSocket(`ws://${location.host}/ws/events?${params}`);
  socket.onopen = () => {
    backoffMs = 500;
    wsConnected.value = true;
  };
  socket.onmessage = (message) => {
    let payload;
    try {
      payload = JSON.parse(message.data);
    } catch {
      return;
    }
    if (payload.type !== "event") return;
    const frame = payload.frame || {};
    if (frame.type === "transcript") {
      // High-volume; feeds the cockpit's rolling box, never a full refetch.
      pushTranscript(frame.text || "");
      return;
    }
    if (frame.type === "overlay_config") {
      refreshOverlayConfig();
      return;
    }
    scheduleRefresh();
  };
  socket.onclose = () => {
    wsConnected.value = false;
    setTimeout(connectEvents, backoffMs);
    backoffMs = Math.min(backoffMs * 2, 15000);
  };
  socket.onerror = () => socket.close();
}
