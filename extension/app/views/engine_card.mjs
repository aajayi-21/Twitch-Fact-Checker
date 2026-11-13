/**
 * The one switch between the two engines: check claims inside this
 * extension with the viewer's own key, or use the local backend program.
 * Shown on the AI provider screen and in Settings.
 *
 * Chrome only: the browser engine needs the offscreen document, so on
 * Firefox there is nothing to choose and the card is not shown.
 */

import {HAS_OFFSCREEN_CAPTURE} from "../../shared/capabilities.js";
import {Card, Segmented} from "../components.mjs";
import {ENGINE_MODE, setEngineMode, useEngine} from "../lib/engine_actions.mjs";
import {html} from "../lib/html.mjs";

export const ENGINE_OPTIONS = Object.freeze([
  {
    value: ENGINE_MODE.BROWSER,
    label: "This browser",
    hint: "Nothing else to install. Your API key stays in this browser.",
  },
  {
    value: ENGINE_MODE.COMPANION,
    label: "Local backend",
    hint: "The program you run on this computer. More features, more setup.",
  },
]);

export const EngineCard = () => {
  const engine = useEngine();
  if (!HAS_OFFSCREEN_CAPTURE) {
    return null;
  }
  return html`
    <${Card} title="Where it runs">
      <p class="setting-help">
        Either way the audio stays on this computer and only the sentence being checked goes to
        your AI provider. Each has its own API keys. Applies the next time you press Start.
      </p>
      <${Segmented}
        name="engine-mode"
        label="Where it runs"
        options=${ENGINE_OPTIONS}
        value=${engine.state.mode}
        onChange=${setEngineMode}
      />
    <//>
  `;
};
