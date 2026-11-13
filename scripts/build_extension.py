#!/usr/bin/env python3
"""Build the store packages of the extension.

``extension/manifest.json`` is written for development: one manifest that
both Chrome and Firefox load unpacked, with the local backend's addresses
granted up front. A store package needs something cleaner, so this script
derives one manifest per browser from it, copies the files a release needs,
checks the result, and zips it.

    dist/chrome/     + dist/live-stream-fact-checker-chrome-<version>.zip
    dist/firefox/    + dist/live-stream-fact-checker-firefox-<version>.zip

What changes for the Chrome package:

- the Firefox-only keys go (``browser_specific_settings``,
  ``background.scripts``);
- the local backend's addresses move from ``host_permissions`` to
  ``optional_host_permissions`` — the app asks for them when someone chooses
  the backend (extension/app/lib/engine_actions.mjs), so installing asks for
  nothing about the local network. The ``ws://`` patterns are dropped:
  WebSocket connections need no host permission;
- the version is stamped (``--version``, else the latest ``v*`` git tag, else
  the manifest's own).

What changes for the Firefox package (which only ever uses the backend):

- the Chrome-only keys and permissions go (``service_worker``,
  ``tabCapture``, ``offscreen``, the optional Gemini host);
- ``data_collection_permissions`` is added, as addons.mozilla.org requires;
- the in-browser speech runtime (27 MB) is left out — Firefox cannot run it.

Both packages leave out tests, spikes and build tooling.

The build FAILS, rather than producing a package a store would reject, when:
a file the manifest names is missing; the description is over 132 characters;
the permissions differ from the list this script expects (so adding one is a
deliberate edit here, with a justification in docs/store/listing.md); or the
extension's own code loads script from a URL, uses ``eval``, or has an inline
``<script>`` (Manifest V3 forbids remote and inline code).

Usage (from the repo root)::

    python scripts/build_extension.py                 # both browsers
    python scripts/build_extension.py --target chrome --version 0.2.0
    python scripts/build_extension.py --check         # lint only, build nothing
"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
EXTENSION_DIR = REPO_ROOT / "extension"
DEFAULT_OUT_DIR = REPO_ROOT / "dist"
PACKAGE_STEM = "live-stream-fact-checker"

TARGETS: tuple[str, ...] = ("chrome", "firefox")

#: Chrome Web Store limit for the manifest ``description``.
MAX_DESCRIPTION_CHARS = 132
#: Chrome Web Store limit for the manifest ``name``.
MAX_NAME_CHARS = 75

#: Never shipped: tests, throwaway spikes, build tooling.
EXCLUDED_DIRS: frozenset[str] = frozenset({"tests", "spike", "__pycache__"})
EXCLUDED_FILES: frozenset[str] = frozenset({"icons/make_icons.py", ".DS_Store"})
#: The in-browser speech runtime. Chrome needs it; Firefox cannot use it.
SPEECH_RUNTIME_DIR = "vendor/transformers"
SPEECH_RUNTIME_MARKER = f"{SPEECH_RUNTIME_DIR}/transformers.js"

#: Third-party code, vendored with integrity checks by scripts/vendor_*.py.
#: It is not scanned for the patterns below: the speech runtime carries its
#: default CDN address as a string, which the extension overrides
#: (extension/engine/stt/stt_runtime.mjs) and never fetches.
VENDOR_DIR = "vendor"

#: The permissions each store package must have — no more, no fewer. Each has
#: a written justification in docs/store/listing.md; change both together.
EXPECTED_PERMISSIONS: dict[str, dict[str, list[str]]] = {
    "chrome": {
        "permissions": [
            "activeTab",
            "alarms",
            "offscreen",
            "storage",
            "tabCapture",
            "unlimitedStorage",
        ],
        "host_permissions": [],
        "optional_host_permissions": [
            "http://127.0.0.1/*",
            "http://localhost/*",
            "https://127.0.0.1/*",
            "https://generativelanguage.googleapis.com/*",
            "https://localhost/*",
        ],
    },
    "firefox": {
        "permissions": ["activeTab", "alarms", "storage", "unlimitedStorage"],
        "host_permissions": [
            "http://127.0.0.1/*",
            "http://localhost/*",
            "https://127.0.0.1/*",
            "https://localhost/*",
            "ws://127.0.0.1/*",
            "ws://localhost/*",
            "wss://127.0.0.1/*",
            "wss://localhost/*",
        ],
        "optional_host_permissions": [],
    },
}

#: What addons.mozilla.org is told the Firefox package transmits. On Firefox
#: the stream's audio goes to the backend program on the same computer and
#: nothing else leaves the browser; that is still "website content" leaving
#: the extension, so it is declared rather than argued away.
FIREFOX_DATA_COLLECTION: dict[str, list[str]] = {"required": ["websiteContent"]}
#: ``data_collection_permissions`` needs Firefox 140.
FIREFOX_MIN_VERSION = "140.0"

_LOCAL_HOST_RE = re.compile(r"^(https?|wss?)://(127\.0\.0\.1|localhost)/\*$")
_VERSION_RE = re.compile(r"^\d+(\.\d+){0,3}$")

_REMOTE_CODE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"""\bfrom\s*["']https?://"""), "imports a module from a URL"),
    (re.compile(r"""\bimport\s*\(\s*["'`]https?://"""), "dynamically imports a URL"),
    (re.compile(r"""\bimportScripts\s*\(\s*["'`]https?://"""), "importScripts a URL"),
    (re.compile(r"""(?<![\w.])eval\s*\("""), "calls eval()"),
    (re.compile(r"""\bnew\s+Function\s*\("""), "builds code with new Function()"),
)
_SCRIPT_TAG_RE = re.compile(
    r"<script\b([^>]*)>(.*?)</script>", re.IGNORECASE | re.DOTALL
)
_SRC_ATTR_RE = re.compile(r"""\bsrc\s*=\s*["']([^"']*)["']""", re.IGNORECASE)


class BuildError(Exception):
    """The package would be rejected, or cannot be built."""


# --------------------------------------------------------------------------- #
# Manifests
# --------------------------------------------------------------------------- #


def _is_local_host(pattern: str) -> bool:
    return bool(_LOCAL_HOST_RE.match(pattern))


def chrome_manifest(dev: dict[str, Any], version: str) -> dict[str, Any]:
    """The Chrome Web Store manifest, derived from the development one."""
    manifest = json.loads(json.dumps(dev))
    manifest["version"] = version
    manifest.pop("browser_specific_settings", None)
    manifest["background"].pop("scripts", None)

    host_permissions = manifest.pop("host_permissions", [])
    required = [host for host in host_permissions if not _is_local_host(host)]
    # WebSockets need no host permission, so only http(s) is worth asking for.
    local_http = [
        host
        for host in host_permissions
        if _is_local_host(host) and host.startswith("http")
    ]
    if required:
        manifest["host_permissions"] = required
    manifest["optional_host_permissions"] = sorted(
        {*manifest.get("optional_host_permissions", []), *local_http}
    )
    return manifest


def firefox_manifest(dev: dict[str, Any], version: str) -> dict[str, Any]:
    """The addons.mozilla.org manifest, derived from the development one."""
    manifest = json.loads(json.dumps(dev))
    manifest["version"] = version
    manifest.pop("minimum_chrome_version", None)
    manifest["background"].pop("service_worker", None)
    manifest["permissions"] = [
        permission
        for permission in manifest["permissions"]
        if permission not in ("tabCapture", "offscreen")
    ]
    # The browser engine (the only user of provider hosts) is Chrome-only.
    manifest.pop("optional_host_permissions", None)
    gecko = manifest.setdefault("browser_specific_settings", {}).setdefault("gecko", {})
    gecko["strict_min_version"] = FIREFOX_MIN_VERSION
    gecko["data_collection_permissions"] = FIREFOX_DATA_COLLECTION
    return manifest


def build_manifest(target: str, dev: dict[str, Any], version: str) -> dict[str, Any]:
    if target == "chrome":
        return chrome_manifest(dev, version)
    if target == "firefox":
        return firefox_manifest(dev, version)
    raise BuildError(f"unknown target {target!r}; expected one of {list(TARGETS)}")


# --------------------------------------------------------------------------- #
# Files
# --------------------------------------------------------------------------- #


def collect_files(extension_dir: Path, target: str) -> list[str]:
    """Paths (relative, POSIX) of every file the ``target`` package ships."""
    files: list[str] = []
    for path in sorted(extension_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(extension_dir)
        if EXCLUDED_DIRS.intersection(relative.parts):
            continue
        name = relative.as_posix()
        if name in EXCLUDED_FILES or relative.name in EXCLUDED_FILES:
            continue
        if name == "manifest.json":
            continue  # written separately, per target
        if target == "firefox" and name.startswith(f"{SPEECH_RUNTIME_DIR}/"):
            continue
        files.append(name)
    return files


def _referenced_files(manifest: dict[str, Any]) -> list[str]:
    """Every file path the manifest names."""
    named: list[str] = []
    background = manifest.get("background", {})
    if "service_worker" in background:
        named.append(background["service_worker"])
    named.extend(background.get("scripts", []))
    action = manifest.get("action", {})
    if "default_popup" in action:
        named.append(action["default_popup"])
    named.extend(action.get("default_icon", {}).values())
    named.extend(manifest.get("icons", {}).values())
    if "page" in manifest.get("options_ui", {}):
        named.append(manifest["options_ui"]["page"])
    for script in manifest.get("content_scripts", []):
        named.extend(script.get("js", []))
        named.extend(script.get("css", []))
    for resources in manifest.get("web_accessible_resources", []):
        named.extend(resources.get("resources", []))
    return named


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #


def lint_manifest(target: str, manifest: dict[str, Any], files: list[str]) -> list[str]:
    """Problems that would get the package rejected. Empty means clean."""
    problems: list[str] = []
    if manifest.get("manifest_version") != 3:
        problems.append("manifest_version must be 3")
    name = manifest.get("name", "")
    if not name or len(name) > MAX_NAME_CHARS:
        problems.append(f"name must be 1–{MAX_NAME_CHARS} characters (is {len(name)})")
    description = manifest.get("description", "")
    if not description or len(description) > MAX_DESCRIPTION_CHARS:
        problems.append(
            f"description must be 1–{MAX_DESCRIPTION_CHARS} characters "
            f"(is {len(description)})"
        )
    if not _VERSION_RE.match(str(manifest.get("version", ""))):
        problems.append(
            f"version {manifest.get('version')!r} must be 1–4 dot-separated integers"
        )
    for forbidden in ("key", "update_url"):
        if forbidden in manifest:
            problems.append(f"{forbidden!r} must not be in a store manifest")

    expected = EXPECTED_PERMISSIONS[target]
    for field, wanted in expected.items():
        actual = sorted(manifest.get(field, []))
        if actual != sorted(wanted):
            problems.append(
                f"{field} is {actual}, expected {sorted(wanted)} — a permission "
                "change must be made in EXPECTED_PERMISSIONS and justified in "
                "docs/store/listing.md"
            )

    csp = manifest.get("content_security_policy", {}).get("extension_pages", "")
    if "'unsafe-eval'" in csp or "http:" in csp or "https:" in csp:
        problems.append("content_security_policy must not allow eval or remote hosts")
    if target == "chrome" and "'wasm-unsafe-eval'" not in csp:
        problems.append(
            "content_security_policy needs 'wasm-unsafe-eval' (the speech model "
            "is WebAssembly)"
        )

    present = set(files)
    for named in _referenced_files(manifest):
        if named not in present:
            problems.append(
                f"the manifest names {named!r}, which is not in the package"
            )

    if target == "firefox":
        gecko = manifest.get("browser_specific_settings", {}).get("gecko", {})
        if "data_collection_permissions" not in gecko:
            problems.append("Firefox needs gecko.data_collection_permissions")
        if not gecko.get("id"):
            problems.append("Firefox needs gecko.id")
    if target == "chrome" and SPEECH_RUNTIME_MARKER not in present:
        problems.append(
            f"{SPEECH_RUNTIME_MARKER} is missing: run "
            "`python scripts/vendor_stt_runtime.py` before building"
        )
    return problems


def scan_code(extension_dir: Path, files: list[str]) -> list[str]:
    """Remote or inline code in the extension's OWN files (not ``vendor/``)."""
    problems: list[str] = []
    for name in files:
        if name.startswith(f"{VENDOR_DIR}/"):
            continue
        suffix = Path(name).suffix
        if suffix not in (".js", ".mjs", ".html"):
            continue
        text = (extension_dir / name).read_text(encoding="utf-8")
        if suffix == ".html":
            for attributes, body in _SCRIPT_TAG_RE.findall(text):
                source = _SRC_ATTR_RE.search(attributes)
                if source is None:
                    if body.strip():
                        problems.append(f"{name}: inline <script> (not allowed in MV3)")
                elif re.match(r"^(https?:)?//", source.group(1)):
                    problems.append(f"{name}: <script src> loads {source.group(1)}")
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith(("//", "*", "/*")):
                continue  # comments may quote the very things being avoided
            for pattern, what in _REMOTE_CODE_PATTERNS:
                if pattern.search(line):
                    problems.append(f"{name}:{line_number}: {what}")
    return problems


# --------------------------------------------------------------------------- #
# Version
# --------------------------------------------------------------------------- #


def version_from_git(repo_root: Path) -> str | None:
    """``1.2.3`` from the latest ``v1.2.3`` tag reachable from HEAD, if any."""
    try:
        described = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    tag = described.stdout.strip()
    if described.returncode != 0 or not tag:
        return None
    version = tag.removeprefix("v")
    return version if _VERSION_RE.match(version) else None


def resolve_version(explicit: str | None, dev: dict[str, Any], repo_root: Path) -> str:
    """``--version``, else the latest release tag, else the manifest's own."""
    version = explicit or version_from_git(repo_root) or str(dev.get("version", ""))
    if not _VERSION_RE.match(version):
        raise BuildError(
            f"version {version!r} is not 1–4 dot-separated integers (Chrome's rule)"
        )
    return version


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


def write_package(
    extension_dir: Path, out_dir: Path, manifest: dict[str, Any], files: list[str]
) -> None:
    """Copy ``files`` and write ``manifest`` into a fresh ``out_dir``."""
    if out_dir.exists():
        shutil.rmtree(out_dir)
    for name in files:
        destination = out_dir / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(extension_dir / name, destination)
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def write_zip(package_dir: Path, zip_path: Path) -> str:
    """Zip ``package_dir`` reproducibly; return the archive's SHA-256.

    Entries are sorted and carry a fixed timestamp, so the same source always
    yields the same bytes — a store upload can be matched to a commit.
    """
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for path in sorted(package_dir.rglob("*")):
            if not path.is_file():
                continue
            info = zipfile.ZipInfo(
                path.relative_to(package_dir).as_posix(),
                date_time=(1980, 1, 1, 0, 0, 0),
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    return hashlib.sha256(zip_path.read_bytes()).hexdigest()


def build_target(
    target: str,
    *,
    extension_dir: Path,
    out_dir: Path,
    version: str,
    check_only: bool = False,
) -> dict[str, Any]:
    """Check, and unless ``check_only`` build, one browser's package.

    Raises:
        BuildError: listing every problem found.
    """
    dev = json.loads((extension_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest = build_manifest(target, dev, version)
    files = collect_files(extension_dir, target)
    problems = lint_manifest(target, manifest, files) + scan_code(extension_dir, files)
    if problems:
        raise BuildError(
            f"the {target} package has {len(problems)} problem(s):\n  - "
            + "\n  - ".join(problems)
        )
    result: dict[str, Any] = {"target": target, "version": version, "files": len(files)}
    if check_only:
        return result
    package_dir = out_dir / target
    write_package(extension_dir, package_dir, manifest, files)
    zip_path = out_dir / f"{PACKAGE_STEM}-{target}-{version}.zip"
    result["sha256"] = write_zip(package_dir, zip_path)
    result["zip"] = zip_path
    result["zip_mb"] = zip_path.stat().st_size / 1e6
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--target", choices=[*TARGETS, "all"], default="all")
    parser.add_argument("--version", help="override the version to stamp")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--check", action="store_true", help="lint only; write nothing")
    args = parser.parse_args()
    targets = TARGETS if args.target == "all" else (args.target,)
    try:
        dev = json.loads((EXTENSION_DIR / "manifest.json").read_text(encoding="utf-8"))
        version = resolve_version(args.version, dev, REPO_ROOT)
        for target in targets:
            result = build_target(
                target,
                extension_dir=EXTENSION_DIR,
                out_dir=args.out,
                version=version,
                check_only=args.check,
            )
            if args.check:
                print(f"{target}: ok — {result['files']} files, version {version}")
            else:
                print(
                    f"{target}: {result['zip'].relative_to(REPO_ROOT)} "
                    f"({result['zip_mb']:.1f} MB, {result['files']} files, "
                    f"version {version})\n         sha256 {result['sha256']}"
                )
    except BuildError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
