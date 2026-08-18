"""Canonical topic labels and colors, served at ``GET /meta/topics``.

The slugs themselves come from :data:`app.models.TOPICS` (the wire contract);
labels and colors were previously only in the extension
(``extension/shared/settings.js`` and its content-script mirror). The two new
backend-served pages fetch this endpoint instead of hard-coding a third and
fourth copy — ``tests/test_topics_sync.py`` asserts this module and the
extension's copies stay identical.
"""

from __future__ import annotations

from app.models import TOPICS

TOPIC_LABELS: dict[str, str] = {
    "politics": "Politics & current events",
    "health": "Health & medicine",
    "science_tech": "Science & technology",
    "money": "Money & economy",
    "history": "History",
    "sports": "Sports",
    "gaming": "Gaming",
    "entertainment": "Entertainment & pop culture",
    "other": "Everything else",
}

TOPIC_COLORS: dict[str, str] = {
    "politics": "#e74c3c",
    "health": "#2ecc71",
    "science_tech": "#3498db",
    "money": "#f1c40f",
    "history": "#9b59b6",
    "sports": "#e67e22",
    "gaming": "#1abc9c",
    "entertainment": "#e84393",
    "other": "#95a5a6",
}

assert set(TOPIC_LABELS) == set(TOPICS), "labels out of sync with app.models.TOPICS"
assert set(TOPIC_COLORS) == set(TOPICS), "colors out of sync with app.models.TOPICS"


def topics_payload() -> dict[str, list[dict[str, str]]]:
    return {
        "topics": [
            {"slug": slug, "label": TOPIC_LABELS[slug], "color": TOPIC_COLORS[slug]}
            for slug in TOPICS
        ]
    }
