// Pieces every overlay style shares (kept cycle-free of the registry).
import { useEffect, useRef } from "preact/hooks";
import { html } from "../html.mjs";

export const LABEL_MARKS = {
  FALSE: "✕",
  MISLEADING: "⚠",
  TRUE: "✓",
  UNVERIFIED: "?",
};

/**
 * The verdict-colored countdown bar: scaleX(1 -> 0) over the dwell time.
 * Transform-only, so the animation stays on the compositor — no layout, no
 * paint, no encoder headroom spent.
 */
export function DwellBar({ durationMs, className = "ov-dwell" }) {
  const fill = useRef(null);
  useEffect(() => {
    const node = fill.current;
    if (!node) return;
    node.style.transform = "scaleX(1)";
    node.style.transition = `transform ${durationMs}ms linear`;
    // Two rAFs: the first ensures the initial scaleX(1) is committed before
    // the transition target is set, so the bar animates instead of jumping.
    requestAnimationFrame(() =>
      requestAnimationFrame(() => {
        node.style.transform = "scaleX(0)";
      })
    );
  }, [durationMs]);
  return html`<div class=${className}><div ref=${fill}></div></div>`;
}
