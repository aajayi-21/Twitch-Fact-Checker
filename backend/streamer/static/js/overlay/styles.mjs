// The style registry: console-selectable verdict-display treatments.
// Adding a style = one component module + one entry here + one console
// picker label; nothing else changes.
import { Toast } from "./toast.mjs";
import { LowerThird } from "./lowerthird.mjs";
import { Chip } from "./chip.mjs";
import { Stamp } from "./stamp.mjs";

export const STYLES = {
  toast: Toast,
  lowerthird: LowerThird,
  chip: Chip,
  stamp: Stamp,
};

export const STYLE_NAMES = Object.keys(STYLES);
