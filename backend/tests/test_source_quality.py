"""Tests for the citation tier list.

Exists because ``FactChecker._enforce_invariants`` downgrades a verdict only
when it has NO citations, so one fandom wiki is enough to keep a FALSE alive —
adequate for a private overlay chip, not for a public accusation.
"""

import pytest

from app.models import Source

from streamer.chat.source_quality import (
    registrable_domain,
    summarize_sources,
    tier_for_domain,
    tier_for_url,
)


class TestRegistrableDomain:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://www.reuters.com/world/a", "reuters.com"),
            ("https://reuters.com/world/a", "reuters.com"),
            ("http://NASA.GOV/mission", "nasa.gov"),
            ("https://news.bbc.co.uk/story", "news.bbc.co.uk"),
            ("not a url", None),
            ("", None),
        ],
    )
    def test_normalizes_hosts(self, url: str, expected: str | None) -> None:
        assert registrable_domain(url) == expected


class TestTiers:
    @pytest.mark.parametrize(
        ("domain", "tier"),
        [
            # A - primary/official/peer-reviewed
            ("nasa.gov", "A"),
            ("who.int", "A"),
            ("ons.gov.uk", "A"),
            ("army.mil", "A"),
            ("nature.com", "A"),
            ("ec.europa.eu", "A"),
            # B - established outlets, fact-checkers, reference
            ("reuters.com", "B"),
            ("bbc.co.uk", "B"),
            ("snopes.com", "B"),
            ("britannica.com", "B"),
            # *.edu is B, not A: student pages and personal faculty sites
            # live there too.
            ("mit.edu", "B"),
            # arxiv is a preprint server, not peer review.
            ("arxiv.org", "B"),
            # C - context, never sufficient alone
            ("wikipedia.org", "C"),
            ("en.wikipedia.org", "C"),
            ("stackoverflow.com", "C"),
            # D - drops the post
            ("reddit.com", "D"),
            ("old.reddit.com", "D"),
            ("x.com", "D"),
            ("starwars.fandom.com", "D"),
            ("someone.blogspot.com", "D"),
            ("prnewswire.com", "D"),
            ("youtube.com", "D"),
        ],
    )
    def test_domain_tiers(self, domain: str, tier: str) -> None:
        assert tier_for_domain(domain) == tier

    def test_unknown_domains_are_c_not_banned(self) -> None:
        """Unknown is untrusted, not disqualifying — it just cannot carry a
        post on its own."""
        assert tier_for_domain("some-local-paper.example") == "C"

    def test_subdomains_inherit_from_a_known_parent(self) -> None:
        assert tier_for_domain("news.bbc.co.uk") == "B"
        assert tier_for_url("https://data.worldbank.org/indicator/x") == "A"

    def test_unparseable_url_is_treated_as_worst_case(self) -> None:
        assert tier_for_domain(None) == "D"


class TestOperatorOverrides:
    def test_can_promote_an_unknown_domain(self) -> None:
        assert tier_for_domain("trusted.example", extra={"trusted.example": "B"}) == "B"

    def test_can_demote_a_known_domain(self) -> None:
        assert tier_for_domain("cnn.com", extra={"cnn.com": "C"}) == "C"

    def test_cannot_promote_out_of_the_denylist(self) -> None:
        """The clamp that stops "just whitelist fandom.com" from being a
        one-line config change that defeats the whole bar."""
        assert tier_for_domain("reddit.com", extra={"reddit.com": "A"}) == "D"
        assert (
            tier_for_domain("starwars.fandom.com", extra={"starwars.fandom.com": "A"})
            == "D"
        )


class TestSummarize:
    def test_reports_best_worst_and_distinct_domains(self) -> None:
        summary = summarize_sources(
            [
                Source(url="https://www.nasa.gov/a"),
                Source(url="https://www.reuters.com/b"),
                Source(url="https://www.reuters.com/c"),
            ]
        )
        assert summary.best_tier == "A"
        assert summary.worst_tier == "B"
        # Three URLs, two sites: distinct DOMAINS is the honest count.
        assert summary.distinct_domains == 2
        assert summary.has_denylisted is False

    def test_a_single_denylisted_source_taints_the_set(self) -> None:
        summary = summarize_sources(
            [
                Source(url="https://www.reuters.com/a"),
                Source(url="https://reddit.com/b"),
            ]
        )
        assert summary.has_denylisted is True

    def test_empty_sources_are_the_worst_case(self) -> None:
        summary = summarize_sources([])
        assert summary.best_tier == "D"
        assert summary.distinct_domains == 0
        assert summary.has_denylisted is True

    def test_wikipedia_alone_never_carries_a_post(self) -> None:
        summary = summarize_sources(
            [
                Source(url="https://en.wikipedia.org/wiki/A"),
                Source(url="https://en.wikipedia.org/wiki/B"),
            ]
        )
        assert summary.best_tier == "C"

    def test_wikipedia_with_a_primary_source_is_fine(self) -> None:
        summary = summarize_sources(
            [
                Source(url="https://en.wikipedia.org/wiki/A"),
                Source(url="https://www.who.int/data"),
            ]
        )
        assert summary.best_tier == "A"
        assert summary.distinct_domains == 2

    def test_the_streamers_own_site_can_never_vindicate_them(self) -> None:
        summary = summarize_sources(
            [Source(url="https://blog.somestreamer.com/post")],
            self_domains=frozenset({"somestreamer.com"}),
        )
        assert summary.has_denylisted is True
