# Vendored frontend runtime

The console and overlay are a **no-build** Preact webapp: these files are the
entire runtime, served locally so the pages work with zero network and zero
Node. Do not edit them; re-vendor with the procedure below.

Runtime resolution is an inline `<script type="importmap">` in
`console.html` / `overlay.html` mapping the bare specifiers (`preact`,
`preact/hooks`, `@preact/signals`, `@preact/signals-core`, `htm`) to these
files — that is why the separate dist modules are vendored rather than a
combined "standalone" bundle: `@preact/signals` imports bare `preact` /
`preact/hooks` and patches Preact's `options` hook, so all consumers must
resolve to the same module instances.

Browser floor: import maps need Chromium 89+, `:has()` needs 105+.
OBS 30+ (CEF ≈ Chromium 127+) comfortably covers the overlay and dock;
treat OBS 29 as the minimum, 30+ recommended.

## Pinned contents

| file | package | version | sha256 |
|---|---|---|---|
| preact/preact.module.js | preact | 10.27.1 | ce620c385b7e6dbfc0f1c1868e952a506cd17ec9acaff26891d7b21aed33dc8e |
| preact/hooks.module.js | preact (hooks) | 10.27.1 | 07e9d21ae13a08182bed24ed9bb95be5759db2037a049393fa0b77dfbe337074 |
| preact/signals.module.js | @preact/signals | 2.3.1 | ac8317f0afe56b039caf3b77b3c4374037504cbda578fd8d0d46a061552b835c |
| preact/signals-core.module.js | @preact/signals-core | 1.11.0 | 1b1bd69e8f4f7fdb802c72206bd5d1f036594121672b1a9dbe84dce38b65b3ab |
| htm/htm.module.js | htm | 3.1.1 | ab33dd3f38059b9be4d5f5350128eefb2356639c4e0bbe9d9e8b3ba75847e9e4 |
| inter/inter-latin-wght-normal.woff2 | @fontsource-variable/inter | 5.2.5 | f052ee44c3728dfd23aba8a4567150bc314d23903026fbb6ad089422c2df56af |
| inter/LICENSE-OFL.txt | (SIL OFL 1.1 for Inter) | — | — |

Hashes are over the files AS VENDORED (after the sourcemap-comment strip).

## Re-vendor procedure

```bash
cd backend/streamer/static/vendor
curl -fsSL -o preact/preact.module.js        https://cdn.jsdelivr.net/npm/preact@<V>/dist/preact.module.js
curl -fsSL -o preact/hooks.module.js         https://cdn.jsdelivr.net/npm/preact@<V>/hooks/dist/hooks.module.js
curl -fsSL -o preact/signals.module.js       https://cdn.jsdelivr.net/npm/@preact/signals@<V>/dist/signals.module.js
curl -fsSL -o preact/signals-core.module.js  https://cdn.jsdelivr.net/npm/@preact/signals-core@<V>/dist/signals-core.module.js
curl -fsSL -o htm/htm.module.js              https://cdn.jsdelivr.net/npm/htm@<V>/dist/htm.module.js
curl -fsSL -o inter/inter-latin-wght-normal.woff2 https://cdn.jsdelivr.net/npm/@fontsource-variable/inter@<V>/files/inter-latin-wght-normal.woff2
# strip sourcemap trailers (we don't ship .map files; DevTools would 404)
sed -i 's|//# sourceMappingURL=.*$||' preact/*.js htm/*.js
# then: update this table (versions + sha256sum), and run the suite —
# tests/test_pages.py scans vendor files for external refs and verifies the
# import map still resolves.
```
