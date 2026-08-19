"""Tests for the streamer web frontend's static sources.

Three permanent guards, all fully offline:

- **Self-contained**: no page, stylesheet, or module — vendored files
  included — may reference an external asset, and no CSS may ``@import`` at
  all. The app must render on a machine with no internet.
- **Topic palette**: app sources fetch ``/meta/topics`` rather than
  hard-coding a copy (canary: the politics red, which appears in every
  hand-copied palette and nothing else).
- **Import-map integrity**: the no-build ESM setup's riskiest failure —
  a bare specifier that nothing maps, or a mapped path that does not exist —
  is a red test rather than a blank page in OBS.
"""

import json
import re
from pathlib import Path

import pytest

from tests.test_streamer_app import streamer_client  # noqa: F401 - fixture

STATIC_DIR = Path(__file__).resolve().parent.parent / "streamer" / "static"

SOURCE_SUFFIXES = {".html", ".css", ".mjs"}
ALL_SOURCES = sorted(
    path
    for path in STATIC_DIR.rglob("*")
    if path.suffix in SOURCE_SUFFIXES and path.is_file()
)
APP_SOURCES = [path for path in ALL_SOURCES if "vendor" not in path.parts]
HTML_PAGES = [path for path in APP_SOURCES if path.suffix == ".html"]

# <script src=http...>, <link href=http...>, any @import, url(http...).
_EXTERNAL_REF_RE = re.compile(
    r"""(?:src|href)\s*=\s*["']https?://|@import|url\(\s*["']?https?://""",
    re.IGNORECASE,
)
# Anchors opening documentation in a browser tab are fine; assets are not.
_ANCHOR_RE = re.compile(r"<a\s[^>]*href\s*=\s*[\"']https?://", re.IGNORECASE)

_IMPORT_MAP_RE = re.compile(
    r"<script type=\"importmap\">\s*(\{.*?\})\s*</script>", re.DOTALL
)
# import ... from "spec"  |  import "spec"  |  import("spec")  |  export ... from "spec"
_MJS_SPECIFIER_RE = re.compile(
    r"""(?:^|\s)(?:import|export)[^"'()]*?from\s*["']([^"']+)["']"""
    r"""|(?:^|\s)import\s*["']([^"']+)["']"""
    r"""|import\(\s*["']([^"']+)["']\s*\)""",
    re.MULTILINE,
)


def relative_name(path: Path) -> str:
    return str(path.relative_to(STATIC_DIR))


class TestServed:
    @pytest.mark.parametrize("route", ["/overlay", "/control"])
    def test_pages_return_html(self, streamer_client, route: str) -> None:  # noqa: F811
        response = streamer_client.get(route)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert response.text.lstrip().lower().startswith("<!doctype html>")

    def test_static_mount_serves_vendor_js_with_a_script_mime(
        self, streamer_client  # noqa: F811
    ) -> None:
        """Module scripts hard-fail on a non-JavaScript content type."""
        response = streamer_client.get("/static/vendor/preact/preact.module.js")
        assert response.status_code == 200
        assert "javascript" in response.headers["content-type"]

    def test_static_mount_serves_the_font(self, streamer_client) -> None:  # noqa: F811
        response = streamer_client.get(
            "/static/vendor/inter/inter-latin-wght-normal.woff2"
        )
        assert response.status_code == 200


class TestSelfContained:
    @pytest.mark.parametrize("path", ALL_SOURCES, ids=relative_name)
    def test_no_external_assets_anywhere(self, path: Path) -> None:
        """Vendored files INCLUDED: a compromised or careless re-vendor that
        introduces a CDN reference must fail here."""
        for line in path.read_text().splitlines():
            if _ANCHOR_RE.search(line):
                continue
            assert not _EXTERNAL_REF_RE.search(line), f"{path.name}: {line.strip()}"

    @pytest.mark.parametrize("path", APP_SOURCES, ids=relative_name)
    def test_no_hardcoded_topic_colors(self, path: Path) -> None:
        """App sources fetch /meta/topics; the politics red is the canary
        that appears in every hand-copied palette."""
        assert "#e74c3c" not in path.read_text(), path.name


class TestImportMapIntegrity:
    """The no-build risk, converted into a red test.

    Every HTML page's import map must point at real files, and every import
    specifier in every app module must be either relative-and-existing or a
    key in that map. A typo here is otherwise a blank page in OBS with no
    console anyone is watching.
    """

    def _import_map(self, page: Path) -> dict[str, str]:
        match = _IMPORT_MAP_RE.search(page.read_text())
        if match is None:
            return {}
        return json.loads(match.group(1)).get("imports", {})

    @pytest.mark.parametrize("page", HTML_PAGES, ids=relative_name)
    def test_mapped_paths_exist(self, page: Path) -> None:
        for specifier, target in self._import_map(page).items():
            assert target.startswith("/static/"), f"{specifier} -> {target}"
            assert (
                STATIC_DIR / target.removeprefix("/static/")
            ).is_file(), f"{page.name}: {specifier} -> {target} (missing file)"

    def test_every_module_specifier_resolves(self) -> None:
        # The union of both pages' maps: modules are shared across pages.
        mapped: set[str] = set()
        for page in HTML_PAGES:
            mapped |= set(self._import_map(page))
        modules = [path for path in APP_SOURCES if path.suffix == ".mjs"]
        for module in modules:
            for match in _MJS_SPECIFIER_RE.finditer(module.read_text()):
                specifier = next(group for group in match.groups() if group)
                if specifier.startswith("."):
                    resolved = (module.parent / specifier).resolve()
                    assert resolved.is_file(), (
                        f"{relative_name(module)}: broken relative import "
                        f"{specifier!r}"
                    )
                else:
                    assert specifier in mapped, (
                        f"{relative_name(module)}: bare specifier {specifier!r} "
                        "is not in any page's import map"
                    )

    def test_html_module_entrypoints_exist(self) -> None:
        script_re = re.compile(
            r"<script type=\"module\" src=\"(/static/[^\"]+)\">"
        )
        for page in HTML_PAGES:
            for src in script_re.findall(page.read_text()):
                assert (
                    STATIC_DIR / src.removeprefix("/static/")
                ).is_file(), f"{page.name}: missing entrypoint {src}"
