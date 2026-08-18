"""Asserts streamer/topics.py and the extension's two copies stay identical.

The topic palette exists in three places by necessity (the backend endpoint,
extension/shared/settings.js, and its classic-script mirror in
content/overlay.js — content scripts cannot import modules and must render
with the backend down). This test converts the "keep in sync" comments into
something that actually fails.
"""

import json
import re
from pathlib import Path

import pytest

from streamer.topics import TOPIC_COLORS, TOPIC_LABELS

EXTENSION_DIR = Path(__file__).resolve().parent.parent.parent / "extension"


def parse_js_object_literal(source: str, name: str) -> dict[str, str]:
    """Extract `const NAME = Object.freeze({ ... })` as a dict.

    A regex, not a JS parser — deliberately brittle: if the extension's
    format changes enough to break this, a human should look anyway.
    """
    match = re.search(
        rf"{name}\s*=\s*Object\.freeze\(\{{(.*?)\}}\)", source, re.DOTALL
    )
    assert match, f"{name} not found"
    body = match.group(1)
    entries: dict[str, str] = {}
    for key, value in re.findall(r"(\w+):\s*\"([^\"]*)\"", body):
        entries[key] = value
    return entries


@pytest.fixture()
def settings_js() -> str:
    path = EXTENSION_DIR / "shared" / "settings.js"
    if not path.exists():
        pytest.skip("extension/ not present (backend-only checkout)")
    return path.read_text()


@pytest.fixture()
def overlay_js() -> str:
    path = EXTENSION_DIR / "content" / "overlay.js"
    if not path.exists():
        pytest.skip("extension/ not present (backend-only checkout)")
    return path.read_text()


class TestPaletteSync:
    def test_labels_match_shared_settings(self, settings_js: str) -> None:
        assert parse_js_object_literal(settings_js, "TOPIC_LABELS") == TOPIC_LABELS

    def test_colors_match_shared_settings(self, settings_js: str) -> None:
        assert parse_js_object_literal(settings_js, "TOPIC_COLORS") == TOPIC_COLORS

    def test_colors_match_the_content_script_mirror(self, overlay_js: str) -> None:
        assert parse_js_object_literal(overlay_js, "TOPIC_COLORS") == TOPIC_COLORS

    def test_labels_match_the_content_script_mirror(self, overlay_js: str) -> None:
        assert parse_js_object_literal(overlay_js, "TOPIC_LABELS") == TOPIC_LABELS
