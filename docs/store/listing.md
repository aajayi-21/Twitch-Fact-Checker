# Chrome Web Store listing — copy and answers

> **Prepared, not submitted.** Nothing has been uploaded or published anywhere. This is
> the material for when the owner decides to go ahead.

Everything the Developer Dashboard asks for, ready to paste. Where a field has a length
limit, the text here is inside it. The upload itself is `dist/live-stream-fact-checker-chrome-<version>.zip`
from `python scripts/build_extension.py`; the steps are in
[`submission-checklist.md`](submission-checklist.md).

## Store listing tab

**Name** (75 max) — from the manifest:

> Live Stream Fact-Checker

**Summary** (132 max) — from the manifest `description`:

> Fact-checks what is said in a live stream you are watching and shows sourced verdicts over the player.

**Category:** Productivity → *Tools* (closest fit; there is no news/media-literacy category).

**Language:** English.

**Detailed description:**

> Watching a stream where someone states "facts" at speed? Live Stream Fact-Checker
> listens along with you and, when a checkable claim is made, puts a small card over the
> player: TRUE, FALSE, MISLEADING or UNVERIFIED, with a two-sentence explanation and
> links to the sources it used.
>
> It is a referee, not an oracle. Every verdict shows its sources so you can check it
> yourself. When the evidence is thin, it says UNVERIFIED rather than guess. Opinions,
> predictions, jokes and game talk are left alone.
>
> HOW IT WORKS
> • Press Start on a stream tab on Twitch, YouTube, Kick or Rumble. It listens only
>   between Start and Stop.
> • The audio is turned into text on your own computer, by a speech model that runs
>   inside the browser. The audio never leaves your machine.
> • The text is sent to the AI provider whose API key you add — OpenRouter, Anthropic,
>   OpenAI or Google Gemini — which picks out factual claims and checks each one against
>   the web.
> • Verdicts appear over the player and are saved to a history only you can see.
>
> WHAT YOU NEED
> • An API key from one of the providers above. You pay the provider directly; a typical
>   check costs about a cent. Spending limits are on by default.
> • A one-time download of a speech model (about 63 MB for the default).
> • No key yet? The setup includes a recorded example you can play without one.
>
> YOUR DATA
> • No account. No analytics. No telemetry. The developer receives nothing.
> • Your history and API keys stay in your browser. Export or delete them whenever you
>   like.
>
> ALSO INCLUDED
> • History across all four sites, searchable and filterable, with per-channel summaries
>   that only show percentages once there is enough data to mean something.
> • A usage screen showing what each session cost.
> • Choice of topics to check, how sensitive to be, and where the cards appear.
> • For developers: an optional local backend with extra features.
>
> Verdicts are produced by AI models and can be wrong. Treat them as a prompt to look,
> not as the last word.
>
> Not affiliated with Twitch, YouTube, Kick or Rumble.

**Screenshots** (1280×800; made by `node scripts/make_store_assets.mjs`, in `assets/`):

| File | Shows |
|---|---|
| `1-overlay.png` | A verdict card over a player. The "stream" is an illustration, not a real channel |
| `2-welcome.png` | The first setup step, which states what is captured and where it goes |
| `3-history.png` | History, with one result opened to its explanation and sources |
| `4-usage.png` | Spend per day and the breakdowns |
| `5-providers.png` | API keys by last four characters, and the spending limits |

All data in them is made up. Before publishing, consider replacing `1-overlay.png` with
one taken on a real stream you have the right to show.

**Small promo tile** (440×280): `assets/promo-440x280.png`.
**Store icon** (128×128): `extension/icons/icon128.png`.

## Privacy practices tab

**Single purpose** (1,000 max):

> Fact-checks spoken claims in a live stream the user is watching and shows sourced
> verdicts over the player.

**Permission justifications** — one field each. These are exactly the permissions in the
package; `scripts/build_extension.py` fails if the manifest and its list ever differ.

| Permission | Justification |
|---|---|
| `tabCapture` | Captures the audio of the one stream tab on which the user presses Start, so that speech in the stream can be transcribed and checked. Capture stops when the user presses Stop or closes the tab. |
| `offscreen` | Tab audio can only be processed in a document, and a service worker has none. The offscreen document holds the audio stream and runs the speech-to-text model (WebAssembly/WebGPU) for the duration of a session. |
| `storage` | Saves the user's settings, their API key(s), and the state of the current session. |
| `unlimitedStorage` | The user's history of results is kept locally in IndexedDB, and the speech model (63–455 MB) is cached locally so it is downloaded only once. Both would otherwise be evicted under the default quota. |
| `activeTab` | When the user clicks the toolbar button on a stream tab, reads that tab's address and title to label the session in the user's local history. |
| `alarms` | Runs a once-a-day local clean-up that deletes history older than the period the user chose. |
| Content scripts on `twitch.tv`, `youtube.com`, `kick.com`, `rumble.com` | Draws the verdict overlay inside the video player on the four supported sites, pauses checking during ad breaks, and reads the channel shown with the player to label the user's local history. No other site is touched. |
| `https://generativelanguage.googleapis.com/*` (optional) | Requested only if the user adds a Google Gemini API key. Needed to send claim text to the Gemini API, which does not accept cross-origin requests otherwise. |
| `http://127.0.0.1/*`, `http://localhost/*`, `https://127.0.0.1/*`, `https://localhost/*` (optional) | Requested only if the user chooses the optional "local backend" mode, to talk to software running on the user's own computer. Never requested otherwise. |

**Host permission justification** (if asked for one text): the two entries above.

**Are you using remote code?** No.

> All JavaScript and WebAssembly is included in the package. The speech-recognition
> model's weights are downloaded once as data (not code) from huggingface.co at a pinned
> revision and executed by the bundled ONNX Runtime WebAssembly build. The bundled speech
> library contains a default CDN address as a string; the extension overrides it with the
> packaged files and never requests it.

**Data usage — what the extension collects** (tick these):

| Category | Tick? | Why |
|---|---|---|
| Personally identifiable information | No | |
| Health information | No | |
| Financial and payment information | No | |
| Authentication information | **Yes** | The user's own API key(s) for their chosen AI provider, stored locally and sent only to that provider |
| Personal communications | No | |
| Location | No | |
| Web history | **Yes** | The address and title of stream pages on which the user ran the checker, kept in the user's local history |
| User activity | No | |
| Website content | **Yes** | Audio of the stream tab is transcribed on the device; the transcript text is sent to the user's chosen AI provider to find and check claims |

**Certifications** (all three apply and are true):

- I do not sell or transfer user data to third parties, outside of the approved use cases.
- I do not use or transfer user data for purposes that are unrelated to my item's single purpose.
- I do not use or transfer user data to determine creditworthiness or for lending purposes.

**Privacy policy URL:** the published address of [`../privacy-policy.md`](../privacy-policy.md)
(see the checklist for hosting it).

## Test instructions for the reviewer

Paste into "Test instructions" (Distribution → or the review notes field):

> No account or login exists. The extension can be fully exercised without an API key:
>
> 1. After install a setup tab opens. Click "I understand — continue".
> 2. Keep "This browser" selected and click Continue.
> 3. On the API key step click "Skip this for now".
> 4. Click "Download and test" (downloads a 63 MB speech model once and runs a short
>    self-test; takes under a minute). Then click Continue.
> 5. Click "Play the recorded example". A bundled 43-second recording is transcribed
>    on-device and four verdict cards appear. The verdicts in this example are recorded,
>    as the screen states; nothing is sent anywhere.
>
> To exercise the live path, use this API key, limited to a small budget, on the
> "AI provider" screen: [PASTE A LOW-LIMIT KEY HERE]. Then open any stream on twitch.tv,
> click the toolbar button and press Start. A status pill appears in the player; verdict
> cards appear when someone states a checkable fact.
>
> Optional host permissions are requested only on user action: Gemini's API host when a
> Gemini key is added, and localhost when the developer-only "Local backend" mode is
> chosen.

## Distribution tab

- **Visibility:** Unlisted for the first release (installable by link, still fully
  reviewed), then Public.
- **Regions:** all.
- **Pricing:** free.
