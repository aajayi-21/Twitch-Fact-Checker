#!/usr/bin/env python3
"""Vendor the UI runtime (Preact + htm) into ``extension/vendor``.

The streamer console already vendors these files under
``backend/streamer/static/vendor`` (pinned versions and hashes are recorded in
that folder's README). This script copies the ones the extension's app needs
and makes one change: the upstream modules import each other by bare package
name (``from "preact"``), which the console resolves with an inline import
map. Extension pages cannot do that — Manifest V3's content security policy
blocks inline scripts, import maps included — so the specifiers are rewritten
to relative paths.

The output is small (about 16 KB) and is committed, so "Load unpacked" works
straight from a checkout. Re-run after re-vendoring the console's copies.

Usage (from the repo root)::

    python3 scripts/vendor_ui_runtime.py           # write the files
    python3 scripts/vendor_ui_runtime.py --check   # exit 1 if they are stale
"""

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = REPO_ROOT / "backend" / "streamer" / "static" / "vendor"
OUTPUT_DIR = REPO_ROOT / "extension" / "vendor"

# Source file (under SOURCE_DIR) -> output file (under OUTPUT_DIR).
FILES: dict[str, str] = {
    "preact/preact.module.js": "preact/preact.module.js",
    "preact/hooks.module.js": "preact/hooks.module.js",
    "htm/htm.module.js": "htm/htm.module.js",
}

# Bare specifier -> path relative to the importing file's directory. Every
# importer here lives in ``preact/``.
SPECIFIER_REWRITES: dict[str, str] = {
    "preact": "./preact.module.js",
}

_IMPORT_FROM = re.compile(r'from\s*"([^"./][^"]*)"')

README = """\
# Vendored UI runtime

Preact, its hooks and htm, copied from `backend/streamer/static/vendor` by
`scripts/vendor_ui_runtime.py` with bare import specifiers rewritten to
relative paths (extension pages cannot use an import map). Versions and
upstream hashes are recorded in that folder's README.

Do not edit these files; re-run the script instead.

`transformers/` (the speech runtime) is fetched separately by
`scripts/vendor_stt_runtime.py` and is git-ignored.
"""


def rewrite_imports(source: str, file_name: str) -> str:
    """Replace bare specifiers; fail on one this script does not know."""

    def replace(match: re.Match[str]) -> str:
        specifier = match.group(1)
        if specifier not in SPECIFIER_REWRITES:
            raise RuntimeError(
                f"{file_name} imports unknown bare specifier {specifier!r}; "
                "add it to SPECIFIER_REWRITES"
            )
        return f'from"{SPECIFIER_REWRITES[specifier]}"'

    return _IMPORT_FROM.sub(replace, source)


def build_outputs() -> dict[Path, str]:
    """Every output path with the exact text it should contain."""
    outputs: dict[Path, str] = {OUTPUT_DIR / "README.md": README}
    for source_name, output_name in FILES.items():
        source = (SOURCE_DIR / source_name).read_text(encoding="utf-8")
        outputs[OUTPUT_DIR / output_name] = rewrite_imports(source, source_name)
    return outputs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--check", action="store_true", help="exit 1 if the vendored files are stale"
    )
    args = parser.parse_args()
    try:
        outputs = build_outputs()
    except (OSError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    stale = [
        path
        for path, text in outputs.items()
        if not path.exists() or path.read_text(encoding="utf-8") != text
    ]
    if args.check:
        for path in stale:
            print(f"stale: {path.relative_to(REPO_ROOT)}", file=sys.stderr)
        return 1 if stale else 0
    for path, text in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
