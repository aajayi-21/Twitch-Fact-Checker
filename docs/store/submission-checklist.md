# Submitting to the Chrome Web Store — checklist

> **Status: prepared, not submitted — and on hold.** Nothing has been uploaded,
> published, tagged or hosted. The owner's instruction of 2026-11-13 is not to deploy the
> app publicly, on the store or any other way, for now. This file is what to do when that
> changes.

What is ready, and the steps only the account holder can take. Copy for every form field
is in [`listing.md`](listing.md).

## Ready in the repository

- [x] Store package: `python scripts/build_extension.py` → `dist/live-stream-fact-checker-chrome-<version>.zip`
      (8.6 MB). The build fails on anything a store would reject that it can detect:
      description length, an unlisted permission, a missing file, remote or inline code.
- [x] The package was installed from `dist/chrome` into a clean Chrome profile and run
      end to end, with no key (the recorded example) and with a real key
      (`scripts/run_browser_engine_e2e.mjs --extension dist/chrome`).
- [x] Install-time permission prompt mentions only the four stream sites: the package
      requests no host permission up front.
- [x] In-product disclosure (first setup step), matching the privacy policy.
- [x] A path a reviewer can follow with no API key.
- [x] Privacy policy text: [`../privacy-policy.md`](../privacy-policy.md).
- [x] Listing copy, permission justifications, data-use answers, reviewer instructions.
- [x] Five 1280×800 screenshots and a 440×280 promo tile in `assets/`.
- [x] CI builds and checks the package on every push (`.github/workflows/ci.yml`). It
      deliberately does not attach the zips to the run, which on a public repository
      would be a public download.

## Before the first upload — yours to do

1. **Licence.** There are no licence files, by decision: none has been chosen. Until one
   is, nobody else may legally redistribute the code. The earlier plan
   (`../distribution-monetization-plan.md` §3.1) was MIT for `extension/` and AGPL-3.0 for
   the backend, on the reasoning that the extension was useless without the backend —
   which stopped being true when the browser engine was added, so that choice deserves a
   second look. When you add one, write the copyright line yourself, and put "Open
   source" back in the listing if it then applies.
2. **Publish the privacy policy at a public URL.** The simplest route is GitHub Pages:
   repository Settings → Pages → deploy from branch `master`, folder `/docs`. The policy
   is then at `https://<user>.github.io/<repo>/privacy-policy`. Any public URL works.
3. **Developer account.** Register at <https://chrome.google.com/webstore/devconsole>
   (one-time US$5 fee) and turn on 2-step verification on that Google account. A new
   account can hold two extensions.
4. **A key for the reviewer.** Create an API key at one provider with a small spending
   cap (a dollar is plenty) and paste it into the reviewer instructions in `listing.md`
   where it says `[PASTE A LOW-LIMIT KEY HERE]`. Revoke it after approval.
5. **Decide on the overlay screenshot.** `assets/1-overlay.png` uses an illustrated
   "stream". A real one is more convincing, if you have the right to show it.
6. **Tag the release**, so the package version comes from the tag:
   `git tag v0.1.0 && git push origin v0.1.0`, then build.

## Upload

1. Dev console → New item → upload the Chrome zip.
2. Store listing tab: paste from `listing.md`; upload the icon, screenshots, promo tile.
3. Privacy practices tab: single purpose, one justification per permission, remote
   code "No", tick the three data categories, certify, privacy policy URL.
4. Distribution: **Unlisted**, all regions, free.
5. Test instructions: paste the reviewer block, with the key.
6. Submit for review. First reviews for a new developer with `tabCapture` are slow —
   days to a few weeks. An "additional information" email is normal; answer from
   `listing.md`.

## After approval

- Install from the unlisted link on a second machine; repeat the reviewer steps.
- Run a real stream on each site.
- Switch to Public when satisfied.
- Later releases can be uploaded from CI with the Chrome Web Store API (v2): add a job
  that runs on a `v*` tag, with the publisher id, item id and a service-account
  credential as repository secrets. Not set up yet — it needs the item to exist first.

## Not yet ready for other stores

- **Microsoft Edge Add-ons:** the Chrome zip is expected to work unchanged, but it has
  not been loaded in Edge.
- **Firefox (addons.mozilla.org):** `dist/live-stream-fact-checker-firefox-<version>.zip`
  is built and passes the same checks, with the data-collection declaration AMO requires.
  It has **not been run in Firefox** since the app was rebuilt; treat it as untested. On
  Firefox the extension only works with the local backend.

## Known gaps a reviewer or user may notice

- OpenAI and Gemini keys are accepted but have not been run against the live services.
- YouTube and Rumble channel names in History depend on page selectors that have not
  been checked against the live sites.
- There is no "what changed" notice on update yet. It is needed before any release that
  changes what data is sent or stored.
