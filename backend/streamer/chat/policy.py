"""The postable predicate: from a verdict to post / review / drop.

**Pure.** Same inputs, same decision — no clock reads, no I/O, no globals.
Time arrives as floats, history arrives behind a Protocol, and the whole
policy is testable as a table of signal combinations.

The evaluation ORDER is a design decision, not an accident:
**authorization first, then content safety, then rate limits last.** The
first failing gate's slug is what gets recorded and shown ("why didn't it
post that?"), and putting rate caps last guarantees a ``hourly_cap`` drop is
known to have been *otherwise fully postable* — which is the only honest
input to "should the cap be 6 or 8?".

What "high confidence" means operationally: there is no numeric confidence on
:class:`~app.models.Verdict`. The signals that exist are the label, the
citations (count, distinct domains, quality tier), ``used_fallback``, the
gate's ``check_worthiness``, and the claim's age. The bar this module sets:

- only FALSE/MISLEADING by default (TRUE opt-in; UNVERIFIED never);
- never a fallback-parsed verdict (the structured grounded call failed and
  the label came out of a regex over prose — fine for a private overlay,
  not a voice speaking in public);
- ≥ 2 citations across ≥ 2 distinct registrable domains, best tier A or B,
  no tier-D source anywhere in the list;
- politics/health require a tier-A source — that is where a wrong public
  verdict costs the most;
- ``check_worthiness`` ≥ 0.70 by default — deliberately ABOVE the pipeline's
  "medium" verify threshold (0.55). **The bot checks more than it says**:
  sensitivity tunes what gets verified (private, cheap); this tunes what
  gets spoken (public, expensive).

Hard clamps live HERE, in :class:`PostingPolicy` validation, not in whatever
layer loads the config — so no code path (env var, control panel, ``!fc``)
can configure its way into the dangerous zone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from app.models import TOPICS, Verdict

from streamer.chat.format import (
    claim_is_postable,
    explanation_is_postable,
)
from streamer.chat.source_quality import Tier, summarize_sources

# Labels that may EVER be posted. UNVERIFIED is not in this set and no
# configuration can add it: a public shrug is noise, and a public shrug about
# a person is worse than noise.
POSTABLE_LABELS: frozenset[str] = frozenset({"FALSE", "MISLEADING", "TRUE"})
DEFAULT_LABELS: frozenset[str] = frozenset({"FALSE", "MISLEADING"})

# Where a wrong public verdict costs the most; these topics demand a tier-A
# citation regardless of configuration.
HIGH_STAKES_TOPICS: frozenset[str] = frozenset({"politics", "health"})

# No posts for this long after joining a channel: the bot must never speak
# into a conversation it has not heard.
JOIN_GRACE_S = 60.0

# The un-configurable 10-minute guard: what stops "the bot dunked on the
# streamer four times during one rant" from being a clip.
TEN_MINUTE_CAP = 3
TEN_MINUTE_WINDOW_S = 600.0

Mode = Literal["auto", "review", "off"]
Action = Literal["post", "review", "drop"]


class PolicyConfigError(ValueError):
    """A configuration attempt crossed a hard clamp."""


@dataclass(frozen=True, slots=True)
class PostingPolicy:
    """Every streamer-facing knob, with the clamps enforced at construction.

    Frozen: a ``!fc`` command REPLACES the whole policy rather than mutating a
    field, so a decision can never observe a half-applied change.
    """

    mode: Mode = "review"  # probation default: nothing auto-posts until earned
    labels: frozenset[str] = DEFAULT_LABELS
    topics: frozenset[str] = frozenset(TOPICS)
    min_check_worthiness: float = 0.70
    min_sources: int = 2
    posts_per_hour: int = 6
    min_gap_s: float = 90.0
    claim_cooldown_s: float = 1800.0
    topic_cooldown_s: float = 300.0
    max_claim_age_s: float = 90.0
    template: Literal["standard", "compact", "verbose"] = "standard"
    sources_style: Literal["domain", "url"] = "domain"
    source_tiers_extra: dict[str, Tier] = field(default_factory=dict)

    def __post_init__(self) -> None:
        illegal = self.labels - POSTABLE_LABELS
        if illegal:
            raise PolicyConfigError(
                f"labels may never include {sorted(illegal)}; "
                f"postable labels are {sorted(POSTABLE_LABELS)}"
            )
        if not self.labels:
            raise PolicyConfigError("at least one postable label is required")
        if not 1 <= self.posts_per_hour <= 12:
            raise PolicyConfigError(
                f"posts_per_hour must be 1..12, got {self.posts_per_hour}"
            )
        if self.min_gap_s < 45.0:
            raise PolicyConfigError(f"min_gap_s must be >= 45, got {self.min_gap_s}")
        if self.claim_cooldown_s < 600.0:
            raise PolicyConfigError(
                f"claim_cooldown_s must be >= 600, got {self.claim_cooldown_s}"
            )
        if not 20.0 <= self.max_claim_age_s <= 180.0:
            raise PolicyConfigError(
                f"max_claim_age_s must be 20..180, got {self.max_claim_age_s}"
            )
        if not 0.60 <= self.min_check_worthiness <= 0.95:
            raise PolicyConfigError(
                "min_check_worthiness must be 0.60..0.95, got "
                f"{self.min_check_worthiness}"
            )
        if not 2 <= self.min_sources <= 5:
            raise PolicyConfigError(f"min_sources must be 2..5, got {self.min_sources}")
        unknown_topics = self.topics - set(TOPICS)
        if unknown_topics:
            raise PolicyConfigError(f"unknown topics: {sorted(unknown_topics)}")


@dataclass(frozen=True, slots=True)
class PostContext:
    """Operational state outside the verdict that the decision depends on.

    ``consent_failure`` is the pre-computed result of the consent proofs
    (:mod:`streamer.chat.consent`) — ``None`` when all four hold, else the
    failing proof's slug. Pre-computed because consent needs live objects
    (registry, transport, db row) that a pure function must not touch.
    """

    consent_failure: str | None
    muted_until: float | None  # monotonic deadline; None = not muted
    joined_at: float  # monotonic; drives the join grace period
    latched: bool  # PostingLatch active (Twitch told us to stop)
    probation_active: bool  # not yet graduated to auto-posting


class PostHistory(Protocol):
    """What the repetition/rate gates need to know about past posts."""

    def posts_in_window(self, window_s: float, *, now: float) -> int: ...

    def seconds_since_last_post(self, *, now: float) -> float: ...

    def seconds_since_topic_post(self, topic: str, *, now: float) -> float: ...

    def similar_recent_claim(
        self, claim_text: str, *, within_s: float, now: float
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class Decision:
    action: Action
    reason: str  # stable slug -> chat_posts.reason, logs, the panel's chips

    @property
    def posts(self) -> bool:
        return self.action == "post"


class InMemoryPostHistory:
    """The bot's per-channel :class:`PostHistory`; session-scoped, unbounded
    in time but bounded in size (only the newest ``max_entries`` posts are
    remembered — far more than any cap allows in a session anyway).

    Similarity uses ``token_set_ratio`` at threshold **80** — deliberately
    LOWER (stricter) than the pipeline's verify-dedupe at 85, because a
    wasted verify call costs $0.005 and a repeated public accusation costs a
    clip.
    """

    SIMILARITY_THRESHOLD = 80
    MAX_ENTRIES = 200

    def __init__(self) -> None:
        # (posted_at, topic, normalized_claim)
        self._posts: list[tuple[float, str, str]] = []

    def record_post(self, verdict: Verdict, *, now: float) -> None:
        from app.fact_checker import normalize_claim

        self._posts.append((now, verdict.topic, normalize_claim(verdict.claim)))
        if len(self._posts) > self.MAX_ENTRIES:
            del self._posts[: -self.MAX_ENTRIES]

    def posts_in_window(self, window_s: float, *, now: float) -> int:
        cutoff = now - window_s
        return sum(1 for posted_at, _, _ in self._posts if posted_at > cutoff)

    def seconds_since_last_post(self, *, now: float) -> float:
        if not self._posts:
            return float("inf")
        return now - self._posts[-1][0]

    def seconds_since_topic_post(self, topic: str, *, now: float) -> float:
        for posted_at, posted_topic, _ in reversed(self._posts):
            if posted_topic == topic:
                return now - posted_at
        return float("inf")

    def similar_recent_claim(
        self, claim_text: str, *, within_s: float, now: float
    ) -> bool:
        from app.fact_checker import normalize_claim
        from rapidfuzz import fuzz

        normalized = normalize_claim(claim_text)
        cutoff = now - within_s
        return any(
            fuzz.token_set_ratio(normalized, past) >= self.SIMILARITY_THRESHOLD
            for posted_at, _, past in self._posts
            if posted_at > cutoff
        )


def _drop(reason: str) -> Decision:
    return Decision(action="drop", reason=reason)


def decide(
    *,
    verdict: Verdict,
    check_worthiness: float | None,
    claim_age_s: float | None,
    context: PostContext,
    policy: PostingPolicy,
    history: PostHistory,
    now: float,
    self_domains: frozenset[str] = frozenset(),
) -> Decision:
    """The full predicate. Returns the FIRST failing gate's reason."""

    # -- 1. Authorization (consent proofs, pre-computed) ------------------ #
    if context.consent_failure is not None:
        return _drop(context.consent_failure)

    # -- 2. Operational state --------------------------------------------- #
    if policy.mode == "off":
        return _drop("mode_off")
    if context.muted_until is not None and now < context.muted_until:
        return _drop("muted")
    if now - context.joined_at < JOIN_GRACE_S:
        return _drop("grace_period")
    if context.latched:
        return _drop("posting_latched")

    # -- 3. Label ---------------------------------------------------------- #
    if verdict.label not in POSTABLE_LABELS:
        return _drop("label_not_postable")  # UNVERIFIED lands here, always
    if verdict.label not in policy.labels:
        return _drop("label_disabled")

    # -- 4. Verdict integrity ---------------------------------------------- #
    if verdict.used_fallback:
        return _drop("degraded_parse")
    if len(verdict.sources) < policy.min_sources:
        return _drop("too_few_sources")
    summary = summarize_sources(
        verdict.sources,
        extra=policy.source_tiers_extra,
        self_domains=self_domains,
    )
    if summary.has_denylisted:
        return _drop("denylisted_source")
    if summary.distinct_domains < 2:
        return _drop("too_few_domains")
    if summary.best_tier not in ("A", "B"):
        return _drop("low_source_quality")
    if verdict.topic in HIGH_STAKES_TOPICS and summary.best_tier != "A":
        return _drop("topic_needs_tier_a")

    # -- 5. Claim & explanation shape -------------------------------------- #
    claim_ok, claim_reason = claim_is_postable(verdict.claim)
    if not claim_ok:
        return _drop(claim_reason)
    explanation_ok, explanation_reason = explanation_is_postable(verdict.explanation)
    if not explanation_ok:
        return _drop(explanation_reason)
    if check_worthiness is None or check_worthiness < policy.min_check_worthiness:
        return _drop("low_check_worthiness")

    # -- 6. Topic policy ---------------------------------------------------- #
    if verdict.topic not in policy.topics:
        return _drop("topic_disabled")

    # -- 7. Freshness & repetition ------------------------------------------ #
    if claim_age_s is None or claim_age_s > policy.max_claim_age_s:
        return _drop("stale")
    if history.similar_recent_claim(
        verdict.claim, within_s=policy.claim_cooldown_s, now=now
    ):
        return _drop("recently_posted")
    if history.seconds_since_last_post(now=now) < policy.min_gap_s:
        return _drop("burst_guard")
    if (
        history.seconds_since_topic_post(verdict.topic, now=now)
        < policy.topic_cooldown_s
    ):
        return _drop("topic_cooldown")

    # -- 8. Rate caps (LAST, so these drops were otherwise postable) -------- #
    if history.posts_in_window(TEN_MINUTE_WINDOW_S, now=now) >= TEN_MINUTE_CAP:
        return _drop("ten_minute_cap")
    if history.posts_in_window(3600.0, now=now) >= policy.posts_per_hour:
        return _drop("hourly_cap")

    # -- 9. Fully postable -------------------------------------------------- #
    if policy.mode == "review" or context.probation_active:
        return Decision(action="review", reason="review_mode")
    return Decision(action="post", reason="ok")
