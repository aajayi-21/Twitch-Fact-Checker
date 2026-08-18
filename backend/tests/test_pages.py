"""Tests for the two streamer pages: served, self-contained, no build step.

The external-asset regex is the cheapest possible permanent guard on the
repo's central convention: single-file vanilla pages, zero CDN, zero bundler.
"""

import re
from pathlib import Path

import pytest

from tests.test_streamer_app import streamer_client  # noqa: F401 - fixture

STATIC_DIR = Path(__file__).resolve().parent.parent / "streamer" / "static"
PAGES = ("overlay.html", "control.html")

# <script src=...>, <link href=...>, @import, or url(http...) pointing off-box.
_EXTERNAL_REF_RE = re.compile(
    r"""(?:src|href)\s*=\s*["']https?://|@import|url\(\s*["']?https?://""",
    re.IGNORECASE,
)
# Anchors are fine (they open documentation in a new tab); assets are not.
_ANCHOR_RE = re.compile(r"<a\s[^>]*href\s*=\s*[\"']https?://", re.IGNORECASE)


class TestServed:
    @pytest.mark.parametrize("path", ["/overlay", "/control"])
    def test_pages_return_html(self, streamer_client, path: str) -> None:  # noqa: F811
        response = streamer_client.get(path)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert response.text.lstrip().lower().startswith("<!doctype html>")


class TestSelfContained:
    @pytest.mark.parametrize("name", PAGES)
    def test_no_external_assets(self, name: str) -> None:
        """No CDN scripts, no external stylesheets, no remote fonts — the
        pages must work on a machine with no internet at all."""
        source = (STATIC_DIR / name).read_text()
        for line in source.splitlines():
            if _ANCHOR_RE.search(line):
                continue  # documentation links open in a tab; not assets
            assert not _EXTERNAL_REF_RE.search(line), f"{name}: {line.strip()}"

    @pytest.mark.parametrize("name", PAGES)
    def test_no_hardcoded_topic_colors(self, name: str) -> None:
        """The pages fetch /meta/topics; a hard-coded topic palette would be
        the 4th copy the endpoint exists to prevent. (Verdict-label colors
        are a different palette and are allowed.)"""
        source = (STATIC_DIR / name).read_text()
        # The politics red is the canary: it appears in every topic-palette
        # copy and in nothing else.
        assert "#e74c3c" not in source

    def test_overlay_has_no_anchors_at_all(self) -> None:
        """Nothing in a browser source may navigate: a clicked link would
        replace the overlay mid-broadcast. Sources render as spans."""
        source = (STATIC_DIR / "overlay.html").read_text()
        body = source.split("<body>", 1)[1]
        assert "<a " not in body

    def test_overlay_defaults_exclude_true_and_unverified(self) -> None:
        """The narrowing-only default: corrections are the format; a TRUE
        toast for every accurate remark is noise."""
        source = (STATIC_DIR / "overlay.html").read_text()
        assert '"FALSE,MISLEADING"' in source
