// Setup — the Nightbot-convention checklist (connect → mod the bot →
// !fc enable → OBS), plus the overlay style picker and source-URL builder:
// this is where a streamer is when positioning OBS, so the overlay's home
// is here rather than a nav item the design doesn't have.
import { useState, useEffect, useRef } from "preact/hooks";
import { html } from "../html.mjs";
import * as api from "../api.mjs";
import {
  botStatus,
  bot,
  setupStatus,
  twitchStatus,
  overlayConfig,
  saveOverlayConfig,
  refreshSetup,
  refreshBot,
  flash,
} from "../store.mjs";
import { Toggle } from "../components/ui.mjs";

const OVERLAY_STYLES = [
  ["toast", "Toast", "card + filled label pill"],
  ["lowerthird", "Lower-third", "broadcast band"],
  ["chip", "Chip", "one line, minimal"],
  ["stamp", "Stamp", "purple band + seal"],
];
const POSITIONS = [
  "top-left",
  "top-center",
  "top-right",
  "bottom-left",
  "bottom-center",
  "bottom-right",
];
const ALL_LABELS = ["TRUE", "FALSE", "MISLEADING", "UNVERIFIED"];

function Step({ done, title, children }) {
  return html`
    <div style="display:flex;gap:10px;align-items:baseline;padding:9px 0">
      <span
        style=${`flex:none;width:1.4em;color:${
          done ? "var(--verdict-true)" : "var(--color-neutral-600)"
        }`}
        >${done ? "✓" : "○"}</span
      >
      <div style="flex:1;min-width:0">
        <strong style="font-size:14px">${title}</strong>
        <div style="font-size:12.5px;color:var(--color-neutral-500)">
          ${children}
        </div>
      </div>
    </div>
  `;
}

function LlmStep() {
  const status = setupStatus.value;
  const [provider, setProvider] = useState("openrouter");
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true);
    try {
      await api.postLlmCredentials({ provider, api_key: key });
      setKey(""); // hygiene: the key lives only in this input + one POST
      flash("ok", "Key verified ✓");
      refreshSetup();
    } catch (error) {
      flash("error", String(error.message || error));
    } finally {
      setBusy(false);
    }
  };
  return html`
    <${Step} done=${Boolean(status?.configured)} title="AI provider">
      ${status?.configured
        ? html`connected`
        : html`<div class="q-actions" style="margin-top:6px;gap:8px">
            <select
              class="input"
              style="width:130px"
              value=${provider}
              onChange=${(event) => setProvider(event.target.value)}
            >
              <option value="openrouter">OpenRouter</option>
              <option value="gemini">Gemini</option>
            </select>
            <input
              class="input"
              type="password"
              style="flex:1"
              placeholder="API key"
              autocomplete="off"
              value=${key}
              onInput=${(event) => setKey(event.target.value)}
            />
            <button
              class="btn btn-primary"
              disabled=${busy || !key}
              onClick=${save}
            >
              Save & verify
            </button>
          </div>`}
    <//>
  `;
}

function TwitchStep() {
  const status = twitchStatus.value;
  const [token, setToken] = useState("");
  const [channel, setChannel] = useState("");
  const [device, setDevice] = useState(null); // {user_code, verification_uri}
  const pollTimer = useRef(null);
  useEffect(() => () => clearInterval(pollTimer.current), []);

  const paste = async () => {
    try {
      await api.postTwitchToken(token, channel);
      setToken("");
      flash("ok", "Twitch connected");
      refreshSetup();
      refreshBot();
    } catch (error) {
      flash("error", String(error.message || error));
    }
  };
  const startDevice = async () => {
    try {
      const grant = await api.postDeviceStart();
      setDevice(grant);
      pollTimer.current = setInterval(async () => {
        try {
          const result = await api.postDevicePoll(channel);
          if (!result.pending) {
            clearInterval(pollTimer.current);
            setDevice(null);
            flash("ok", "Twitch connected");
            refreshSetup();
            refreshBot();
          }
        } catch (error) {
          clearInterval(pollTimer.current);
          setDevice(null);
          flash("error", String(error.message || error));
        }
      }, (grant.interval_s || 5) * 1000);
    } catch (error) {
      flash("error", String(error.message || error));
    }
  };

  return html`
    <${Step}
      done=${Boolean(status?.configured)}
      title="Connect Twitch (the BOT account)"
    >
      ${status?.configured
        ? html`connected as ${status.login} (${status.token_hint || "…"})
            ${status.expires_at ? ` · expires ${status.expires_at}` : ""}
            ${status.has_refresh ? " · auto-refresh on" : ""}`
        : device
          ? html`Go to
              <span style="user-select:all">twitch.tv/activate</span> and
              enter
              <div
                style="font-size:26px;font-weight:700;letter-spacing:.15em;color:var(--color-accent)"
              >
                ${device.user_code}
              </div>`
          : html`<div class="q-actions" style="margin-top:6px;gap:8px;flex-wrap:wrap">
              <input
                class="input"
                style="width:170px"
                placeholder="your channel"
                value=${channel}
                onInput=${(event) => setChannel(event.target.value)}
              />
              <button
                class="btn btn-primary"
                disabled=${!status?.client_id_set}
                title=${status?.client_id_set
                  ? ""
                  : "Set TWITCH_CLIENT_ID in .env (register a free public app" +
                    " at dev.twitch.tv/console/apps) — or paste a token"}
                onClick=${startDevice}
              >
                Connect with Twitch
              </button>
              <input
                class="input"
                type="password"
                style="flex:1;min-width:200px"
                placeholder="…or paste a chat:read chat:edit token"
                autocomplete="off"
                value=${token}
                onInput=${(event) => setToken(event.target.value)}
              />
              <button
                class="btn btn-secondary"
                disabled=${!token}
                onClick=${paste}
              >
                Save token
              </button>
            </div>`}
    <//>
  `;
}

function OverlayCard() {
  const stored = overlayConfig.value;
  const [draft, setDraft] = useState(null);
  const config = draft ?? stored;
  if (!config) return null;
  const edit = (patch) => setDraft({ ...config, ...patch });
  const apply = async () => {
    if (await saveOverlayConfig(config)) setDraft(null);
  };
  const overlayUrl = `${location.origin}/overlay`;
  return html`
    <div class="card" style="margin-top:14px">
      <span class="card-kicker">On-stream overlay</span>
      <div class="style-picker">
        ${OVERLAY_STYLES.map(
          ([value, name, blurb]) => html`
            <button
              key=${value}
              class="style-opt"
              aria-pressed=${String(config.style === value)}
              onClick=${() => edit({ style: value })}
            >
              <span class="style-thumb" data-thumb=${value}></span>
              <strong>${name}</strong>
              <span class="text-muted" style="font-size:11px">${blurb}</span>
            </button>
          `
        )}
      </div>
      <div class="form-grid" style="margin-top:10px">
        <label class="field"
          ><span style="font-size:12px" class="text-muted">Position</span>
          <select
            class="input"
            value=${config.position}
            onChange=${(event) => edit({ position: event.target.value })}
          >
            ${POSITIONS.map(
              (position) =>
                html`<option key=${position} value=${position}>
                  ${position}
                </option>`
            )}
          </select>
        </label>
        <label class="field"
          ><span style="font-size:12px" class="text-muted"
            >Dwell seconds (4–60)</span
          >
          <input
            class="input"
            type="number"
            min="4"
            max="60"
            value=${config.duration_s}
            onInput=${(event) =>
              edit({ duration_s: Number(event.target.value) })}
          />
        </label>
      </div>
      <div
        style="display:flex;gap:14px;align-items:center;flex-wrap:wrap;font-size:13px"
      >
        <span class="text-muted">Labels shown:</span>
        ${ALL_LABELS.map(
          (label) => html`
            <label
              key=${label}
              style="display:flex;gap:6px;align-items:center;cursor:pointer"
            >
              <input
                type="checkbox"
                checked=${config.labels.includes(label)}
                onChange=${(event) =>
                  edit({
                    labels: event.target.checked
                      ? [...config.labels, label]
                      : config.labels.filter((entry) => entry !== label),
                  })}
              />
              ${label}
            </label>
          `
        )}
        <span class="text-muted" style="margin-left:auto"
          >max stacked
          <input
            class="input"
            type="number"
            min="1"
            max="3"
            style="width:58px;display:inline-block;margin-left:6px"
            value=${config.max_stack}
            onInput=${(event) =>
              edit({ max_stack: Number(event.target.value) })}
        /></span>
      </div>
      <div class="q-actions" style="margin-top:4px">
        <button class="btn btn-primary" disabled=${!draft} onClick=${apply}>
          Apply to live overlay
        </button>
        <button
          class="btn btn-secondary"
          onClick=${() =>
            api
              .postTestVerdict("FALSE")
              .then(() => flash("ok", "Test verdict sent — watch the overlay"))
              .catch((error) => flash("error", String(error.message || error)))}
        >
          Send test verdict
        </button>
        <span class="q-note">
          changes push to the running overlay — the OBS URL never changes
        </span>
      </div>
      <div class="card-meta" style="user-select:all">${overlayUrl}</div>
      <span class="card-meta">
        OBS Browser Source (1920×1080): use the URL above; turn OFF “Shutdown
        source when not visible” and “Refresh browser when scene becomes
        active”. Preview in a tab: ${overlayUrl}?preview=1 · Dock:
        ${location.origin}/control#/dock
      </span>
    </div>
  `;
}

export function Setup() {
  const status = botStatus.value;
  const currentBot = bot.value;
  return html`
    <div class="view-head">
      <h4>Setup</h4>
      <span class="text-muted" style="font-size:12.5px"
        >once per channel, ~5 minutes</span
      >
    </div>
    <div class="card">
      <${LlmStep} />
      <${TwitchStep} />
      <${Step}
        done=${Boolean(currentBot?.is_moderator)}
        title="Mod the bot in your channel"
      >
        Type${" "}
        <code style="user-select:all"
          >/mod ${status?.bot_login || "yourbot"}</code
        >${" "}
        in your own chat — the consent proof, and it lifts link filtering.
      <//>
      <${Step}
        done=${Boolean(currentBot?.armed)}
        title="Arm it — YOU (the broadcaster) type !fc enable in chat"
      >
        Recorded with your user id as the auditable consent row. New channels
        start in review mode until 10 approvals (or !fc trust).
      <//>
      <${Step}
        done=${(status?.live_sessions || []).length > 0}
        title="Start the audio ingest before each stream"
      >
        <code style="user-select:all"
          >uv run fact-checker-ingest
          twitch.tv/${status?.channel || "you"}</code
        >${" "}
        — or --source device for zero-delay local capture; add --video for
        on-screen claims.
      <//>
    </div>
    <${OverlayCard} />
  `;
}
