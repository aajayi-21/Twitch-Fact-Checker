"""Citation quality tiers — the "high confidence" half of public posting.

``FactChecker._enforce_invariants`` downgrades a non-UNVERIFIED verdict only
when it has NO citations at all::

    if label != "UNVERIFIED" and not sources:

That is the right bar for a private overlay chip, and far too low for a public
accusation: one fandom wiki or one Reddit thread is enough to keep a ``FALSE``
alive. `docs/improvement-report.md` §6.5 proposed a source tier list for
exactly this reason, and public posting is where it earns its keep.

Deliberately NOT wired into ``_enforce_invariants`` here. Doing that would
silently change what every existing overlay user sees and needs its own eval
against the feedback table — two blast radii in one change. This module gates
only what the bot says out loud.

Tiers:

``A`` primary, official, or peer-reviewed — the thing itself, or the body that
    measures it.
``B`` established wires, major outlets, dedicated fact-checkers, reference.
``C`` usable context, never sufficient alone. **The default for anything
    unrecognized**, because unknown is untrusted, not banned.
``D`` presence in a citation list DROPS the post. Not "low quality" — actively
    unusable as the evidentiary basis for calling a person wrong in public.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, NamedTuple
from urllib.parse import urlsplit

from app.models import Source

Tier = Literal["A", "B", "C", "D"]

# Order matters only for readability; lookup is exact-then-suffix.
_TIER_A: frozenset[str] = frozenset(
    {
        "who.int",
        "nih.gov",
        "cdc.gov",
        "nasa.gov",
        "noaa.gov",
        "usgs.gov",
        "bls.gov",
        "census.gov",
        "sec.gov",
        "federalreserve.gov",
        "cbo.gov",
        "gao.gov",
        "europa.eu",
        "ec.europa.eu",
        "eurostat.ec.europa.eu",
        "un.org",
        "imf.org",
        "worldbank.org",
        "oecd.org",
        "wto.org",
        "ons.gov.uk",
        "nhs.uk",
        "ipcc.ch",
        "iea.org",
        "nature.com",
        "science.org",
        "nejm.org",
        "thelancet.com",
        "bmj.com",
        "cell.com",
        "pnas.org",
        "pubmed.ncbi.nlm.nih.gov",
        "ncbi.nlm.nih.gov",
        "cochranelibrary.com",
    }
)

_TIER_B: frozenset[str] = frozenset(
    {
        "reuters.com",
        "apnews.com",
        "afp.com",
        "bbc.com",
        "bbc.co.uk",
        "npr.org",
        "pbs.org",
        "nytimes.com",
        "washingtonpost.com",
        "wsj.com",
        "ft.com",
        "economist.com",
        "theguardian.com",
        "bloomberg.com",
        "cnn.com",
        "nbcnews.com",
        "cbsnews.com",
        "abcnews.go.com",
        "politico.com",
        "axios.com",
        "thehill.com",
        "snopes.com",
        "politifact.com",
        "factcheck.org",
        "fullfact.org",
        "britannica.com",
        "espn.com",
        "arxiv.org",
    }
)

# Presence of ANY of these drops the post. User-generated, aggregated, or
# self-published: the claimant is not a check on the claim.
_TIER_D: frozenset[str] = frozenset(
    {
        "reddit.com",
        "quora.com",
        "answers.com",
        "x.com",
        "twitter.com",
        "facebook.com",
        "instagram.com",
        "tiktok.com",
        "youtube.com",
        "youtu.be",
        "twitch.tv",
        "kick.com",
        "rumble.com",
        "medium.com",
        "substack.com",
        "ehow.com",
        "prnewswire.com",
        "businesswire.com",
        "globenewswire.com",
    }
)

# Suffix rules, applied when an exact match misses. Checked longest-first so
# "gov.uk" cannot be shadowed by a bare "uk"-style entry.
_SUFFIX_TIERS: tuple[tuple[str, Tier], ...] = (
    (".fandom.com", "D"),
    (".wikia.com", "D"),
    (".blogspot.com", "D"),
    (".wordpress.com", "D"),
    (".substack.com", "D"),
    (".tumblr.com", "D"),
    (".wikipedia.org", "C"),
    (".wikimedia.org", "C"),
    (".stackexchange.com", "C"),
    # *.edu is B, NOT A: it covers student pages, course notes, and personal
    # faculty sites as readily as it covers published research.
    (".edu", "B"),
    (".ac.uk", "B"),
    (".gov.uk", "A"),
    (".gov.au", "A"),
    (".gov.ca", "A"),
    (".gov", "A"),
    (".mil", "A"),
    (".int", "A"),
)

_TIER_C_EXACT: frozenset[str] = frozenset(
    {
        "wikipedia.org",
        "stackoverflow.com",
        "investopedia.com",
        "history.com",
        "nationalgeographic.com",
    }
)

_TIER_ORDER: dict[Tier, int] = {"A": 0, "B": 1, "C": 2, "D": 3}


class SourceSummary(NamedTuple):
    """What the posting predicate needs to know about a citation list."""

    best_tier: Tier
    worst_tier: Tier
    distinct_domains: int
    has_denylisted: bool
    domains: tuple[str, ...]


def registrable_domain(url: str) -> str | None:
    """Lowercased host with a leading ``www.`` stripped, or ``None``.

    Mirrors the ``urlsplit(source.url).hostname`` shape ``db.record_verdict``
    already uses for the ``sources.domain`` column, so a chat message and the
    persisted row can never disagree about what a citation's domain was.
    """
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host or None


def tier_for_domain(
    domain: str | None, *, extra: Mapping[str, Tier] | None = None
) -> Tier:
    """Tier of one registrable domain. Unknown is ``C`` — untrusted, not banned.

    ``extra`` lets an operator classify domains this list has never heard of.
    It can demote anything, and promote out of A/B/C — but it can NEVER promote
    out of ``D``. That clamp is the point: without it, "just whitelist
    fandom.com" is a one-line config change that defeats the entire bar.
    """
    if domain is None:
        # Unparseable: treat as the worst case rather than silently ignoring.
        return "D"
    builtin = _builtin_tier(domain)
    if builtin == "D":
        return "D"
    if extra:
        override = extra.get(domain)
        if override is not None:
            return override
    return builtin


def _builtin_tier(domain: str) -> Tier:
    if domain in _TIER_D:
        return "D"
    if domain in _TIER_A:
        return "A"
    if domain in _TIER_B:
        return "B"
    if domain in _TIER_C_EXACT:
        return "C"
    for suffix, tier in _SUFFIX_TIERS:
        if domain.endswith(suffix):
            return tier
    # Subdomains inherit from a known parent (news.bbc.co.uk -> bbc.co.uk).
    parts = domain.split(".")
    for index in range(1, len(parts) - 1):
        parent = ".".join(parts[index:])
        if parent in _TIER_D:
            return "D"
        if parent in _TIER_A:
            return "A"
        if parent in _TIER_B:
            return "B"
        if parent in _TIER_C_EXACT:
            return "C"
    return "C"


def tier_for_url(url: str, *, extra: Mapping[str, Tier] | None = None) -> Tier:
    return tier_for_domain(registrable_domain(url), extra=extra)


def summarize_sources(
    sources: list[Source],
    *,
    extra: Mapping[str, Tier] | None = None,
    self_domains: frozenset[str] = frozenset(),
) -> SourceSummary:
    """Collapse a citation list into the signals the predicate runs on.

    ``self_domains`` are treated as ``D``: a streamer's own site must never be
    the source that vindicates or condemns them.
    """
    if not sources:
        return SourceSummary("D", "D", 0, True, ())
    domains: list[str] = []
    tiers: list[Tier] = []
    for source in sources:
        domain = registrable_domain(source.url)
        if domain is None:
            tiers.append("D")
            continue
        if domain not in domains:
            domains.append(domain)
        tier = (
            "D"
            if _is_self_domain(domain, self_domains)
            else tier_for_domain(domain, extra=extra)
        )
        tiers.append(tier)
    best = min(tiers, key=lambda tier: _TIER_ORDER[tier])
    worst = max(tiers, key=lambda tier: _TIER_ORDER[tier])
    return SourceSummary(
        best_tier=best,
        worst_tier=worst,
        distinct_domains=len(domains),
        has_denylisted="D" in tiers,
        domains=tuple(domains),
    )


def _is_self_domain(domain: str, self_domains: frozenset[str]) -> bool:
    return any(
        domain == own or domain.endswith(f".{own}") for own in self_domains if own
    )
