# Privacy policy — Live Stream Fact-Checker

*Effective 13 November 2026. Applies to the Live Stream Fact-Checker browser extension.*

## The short version

- There is no account, no analytics and no telemetry. **The developer of this extension
  receives nothing from it** — no audio, no text, no history, no usage figures, no crash
  reports. The extension has no server of its own to send anything to.
- The **audio** of a stream never leaves your computer.
- The **text** of what is said is sent to the AI provider whose API key you add, because
  that is how claims are found and checked.
- Your **history** and your **API keys** are stored in your browser, on your device.

## What the extension processes, and where it goes

| What | When | Where it goes | Kept |
|---|---|---|---|
| **Audio of the stream tab** | Only after you press Start on that tab, until you press Stop or close it | Turned into text on your computer — by a speech model inside the browser, or, if you choose the "local backend" option, by the backend program running on the same computer. It is not sent to the developer, to an AI provider, or anywhere off your machine | Not stored |
| **Transcript text** (the words spoken) | While checking is running | Sent to **your AI provider** in short batches so it can pick out factual claims; each claim is then sent again so the provider can search the web and judge it | Not stored by the extension. Claims that were picked out are stored in your history (below). What your provider keeps is governed by its own policy |
| **Your API key(s)** | When you add one | Stored in this browser's local extension storage (or, with the local backend, in that program's own settings file on your computer). Sent only to the provider it belongs to, as the credential for your requests | Until you remove it. "Remove key" deletes it at once |
| **History**: the site, channel, stream address and title of each session; the claims heard; verdicts, explanations and sources; your ratings; and what each provider call cost | While checking is running | Stored in this browser's database on your device. Not sent anywhere | Until you delete it, or for the period you choose under Settings → Your data. You can export or delete it at any time |
| **Settings** (sensitivity, topics, overlay position and similar) | When you change them | Stored with your browser profile. If you have turned on your browser's own sync, the browser vendor syncs these settings between your devices. API keys and history are never part of this | Until you change them |
| **The stream page itself** | On Twitch, YouTube, Kick and Rumble pages | The extension reads the page's address, title and the channel link shown with the player, to label your history, and draws the verdict overlay. It does not read chat, messages, cookies or account details | See History |
| **Screenshots of the stream** (the optional, experimental "check on-screen claims" setting, off by default, local backend only) | Only while that setting is on and checking is running | Sent to the backend program on your computer; when a claim refers to something on screen, the most recent one is passed to your AI provider with that claim | Never stored |

## Who else is involved

- **Your AI provider** — whichever of OpenRouter, Anthropic, OpenAI or Google (Gemini)
  you add a key for. It receives the transcript text and claims described above, sees
  your IP address like any service you connect to, and may search the web on your behalf.
  You have your own agreement with that provider; its privacy policy applies to what it
  receives.
- **Hugging Face** — if you use speech recognition inside the browser, the speech model
  is downloaded once from huggingface.co, which therefore sees your IP address and which
  model file was requested. No audio or text is sent there.

Nobody else. The extension loads no advertising, tracking or analytics code, and all of
its program code ships inside the extension package.

## Your choices

- Checking runs only between Start and Stop, on the one tab you started it on.
- Remove an API key at any time under **AI provider**; a running session ends at once.
- Export or delete your history, or set how long it is kept, under **Settings → Your
  data**.
- Uninstalling the extension deletes everything it stored in the browser.

## Limited use

The extension's use of information received from the browser APIs it has permission for
adheres to the Chrome Web Store User Data Policy, including the Limited Use requirements.
Information is used only to provide the single purpose of the extension — fact-checking
what is said in a stream you are watching and showing sourced verdicts. It is not sold,
not used for advertising, not used to determine creditworthiness, and not read by any
person at the developer.

## Children

The extension is not directed at children and collects no personal information from
anyone.

## Changes

If a future version changes what is sent or stored, this page will be updated before
that version is released, and the extension will say so when it updates.

## Contact

Questions or concerns: open an issue at
<https://github.com/aajayi-21/Twitch-Fact-Checker/issues>.
