// Global keyboard control: J/K select, Enter post, X skip, M mute.
// Active only on the cockpit and dock (the mid-stream surfaces), dead when
// typing in a field — a streamer mid-broadcast cannot aim a mouse.
import { route } from "./router.mjs";
import { queue, selectedIdx, bot, approve, skip, setMute } from "./store.mjs";

const TYPING_TAGS = new Set(["INPUT", "TEXTAREA", "SELECT"]);

export function installKeys() {
  window.addEventListener("keydown", (event) => {
    if (!["cockpit", "dock"].includes(route.value)) return;
    if (TYPING_TAGS.has(event.target?.tagName)) return;
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    const items = queue.value;
    switch (event.key) {
      case "j":
      case "J":
        selectedIdx.value = Math.min(selectedIdx.value + 1, items.length - 1);
        break;
      case "k":
      case "K":
        selectedIdx.value = Math.max(selectedIdx.value - 1, 0);
        break;
      case "Enter":
        if (items[selectedIdx.value]) approve(items[selectedIdx.value].post_id);
        break;
      case "x":
      case "X":
        if (items[selectedIdx.value]) skip(items[selectedIdx.value].post_id);
        break;
      case "m":
      case "M":
        setMute(!(bot.value?.muted ?? false));
        break;
      default:
        return;
    }
    event.preventDefault();
  });
}
