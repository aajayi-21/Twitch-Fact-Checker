# Vendored UI runtime

Preact, its hooks and htm, copied from `backend/streamer/static/vendor` by
`scripts/vendor_ui_runtime.py` with bare import specifiers rewritten to
relative paths (extension pages cannot use an import map). Versions and
upstream hashes are recorded in that folder's README.

Do not edit these files; re-run the script instead.

`transformers/` (the speech runtime) is fetched separately by
`scripts/vendor_stt_runtime.py` and is git-ignored.
