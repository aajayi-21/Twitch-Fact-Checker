"""The postable predicate as a table: signal combinations -> (action, reason).

Each CASES row is one behaviour of the policy; the table IS the policy
documentation. Separate classes pin the ordering guarantee, the hard clamps,
and purity.
"""

import time

import pytest

from app.models import Source, Verdict

from streamer.chat.policy import (
    Decision,
    InMemoryPostHistory,
    PolicyConfigError,
    PostContext,
    PostingPolicy,
    decide,
)

NOW = 10_000.0

# Distinct-domain tier-B pair: the baseline "good enough" citation set.
REUTERS = Source(url="https://www.reuters.com/world/a", title="Reuters")
APNEWS = Source(url="https://apnews.com/article/b", title="AP")
# Tier A.
NASA = Source(url="https://www.nasa.gov/c", title="NASA")
WHO = Source(url="https://www.who.int/d", title="WHO")
# Tier C / D.
WIKI = Source(url="https://en.wikipedia.org/wiki/E", title="Wikipedia")
WIKI2 = Source(url="https://en.wikipedia.org/wiki/F", title="Wikipedia")
REDDIT = Source(url="https://reddit.com/r/g", title="Reddit")
REUTERS2 = Source(url="https://www.reuters.com/world/h", title="Reuters again")


def make_verdict(**overrides) -> Verdict:
    defaults = dict(
        claim="The Eiffel Tower is 450 metres tall.",
        label="FALSE",
        explanation="It is 330 m tall including antennas, not 450.",
        sources=[REUTERS, APNEWS],
        topic="other",
    )
    defaults.update(overrides)
    return Verdict(**defaults)  # type: ignore[arg-type]


def make_context(**overrides) -> PostContext:
    defaults = dict(
        consent_failure=None,
        muted_until=None,
        joined_at=NOW - 300.0,  # well past the join grace
        latched=False,
        probation_active=False,
    )
    defaults.update(overrides)
    return PostContext(**defaults)  # type: ignore[arg-type]


class FakeHistory:
    """Scripted PostHistory: every gate input is directly settable."""

    def __init__(
        self,
        *,
        in_window: dict[float, int] | None = None,
        since_last: float = float("inf"),
        since_topic: float = float("inf"),
        similar: bool = False,
    ) -> None:
        self._in_window = in_window or {}
        self._since_last = since_last
        self._since_topic = since_topic
        self._similar = similar

    def posts_in_window(self, window_s: float, *, now: float) -> int:
        return self._in_window.get(window_s, 0)

    def seconds_since_last_post(self, *, now: float) -> float:
        return self._since_last

    def seconds_since_topic_post(self, topic: str, *, now: float) -> float:
        return self._since_topic

    def similar_recent_claim(
        self, claim_text: str, *, within_s: float, now: float
    ) -> bool:
        return self._similar


def run_decide(
    *,
    verdict_overrides: dict | None = None,
    check_worthiness: float = 0.9,
    claim_age_s: float | None = 30.0,
    context_overrides: dict | None = None,
    policy_overrides: dict | None = None,
    history: FakeHistory | None = None,
) -> Decision:
    policy_kwargs = {"mode": "auto"}
    policy_kwargs.update(policy_overrides or {})
    return decide(
        verdict=make_verdict(**(verdict_overrides or {})),
        check_worthiness=check_worthiness,
        claim_age_s=claim_age_s,
        context=make_context(**(context_overrides or {})),
        policy=PostingPolicy(**policy_kwargs),  # type: ignore[arg-type]
        history=history or FakeHistory(),
        now=NOW,
    )


# (id, kwargs for run_decide, expected_action, expected_reason)
CASES = [
    ("happy_false", {}, "post", "ok"),
    ("happy_misleading", {"verdict_overrides": {"label": "MISLEADING"}}, "post", "ok"),
    # -- labels ---------------------------------------------------------- #
    (
        "unverified_never_posts",
        {"verdict_overrides": {"label": "UNVERIFIED"}},
        "drop",
        "label_not_postable",
    ),
    (
        "true_off_by_default",
        {"verdict_overrides": {"label": "TRUE"}},
        "drop",
        "label_disabled",
    ),
    (
        "true_postable_when_opted_in",
        {
            "verdict_overrides": {"label": "TRUE"},
            "policy_overrides": {"labels": frozenset({"FALSE", "TRUE"})},
        },
        "post",
        "ok",
    ),
    (
        "config_false_only",
        {
            "verdict_overrides": {"label": "MISLEADING"},
            "policy_overrides": {"labels": frozenset({"FALSE"})},
        },
        "drop",
        "label_disabled",
    ),
    # -- verdict integrity ----------------------------------------------- #
    (
        "fallback_never_posts",
        {"verdict_overrides": {"used_fallback": True}},
        "drop",
        "degraded_parse",
    ),
    (
        "one_source",
        {"verdict_overrides": {"sources": [REUTERS]}},
        "drop",
        "too_few_sources",
    ),
    (
        "two_urls_one_domain",
        {"verdict_overrides": {"sources": [REUTERS, REUTERS2]}},
        "drop",
        "too_few_domains",
    ),
    (
        "wikipedia_only",
        {"verdict_overrides": {"sources": [WIKI, WIKI2]}},
        "drop",
        "too_few_domains",
    ),
    (
        "wiki_plus_tier_a",
        {"verdict_overrides": {"sources": [WIKI, WHO]}},
        "post",
        "ok",
    ),
    (
        "reddit_present_taints_the_set",
        {"verdict_overrides": {"sources": [REUTERS, REDDIT]}},
        "drop",
        "denylisted_source",
    ),
    (
        "politics_needs_tier_a",
        {"verdict_overrides": {"topic": "politics", "sources": [REUTERS, APNEWS]}},
        "drop",
        "topic_needs_tier_a",
    ),
    (
        "politics_with_tier_a_posts",
        {"verdict_overrides": {"topic": "politics", "sources": [NASA, WHO]}},
        "post",
        "ok",
    ),
    (
        "health_needs_tier_a",
        {"verdict_overrides": {"topic": "health", "sources": [REUTERS, APNEWS]}},
        "drop",
        "topic_needs_tier_a",
    ),
    # -- claim & explanation shape ---------------------------------------- #
    (
        "first_person_claim",
        {"verdict_overrides": {"claim": "I have never been to Japan before."}},
        "drop",
        "first_person_claim",
    ),
    (
        "reported_speech",
        {
            "verdict_overrides": {
                "claim": "Musk said Tesla sold two million cars last year."
            }
        },
        "drop",
        "reported_speech",
    ),
    (
        "explanation_too_short",
        {"verdict_overrides": {"explanation": "False."}},
        "drop",
        "explanation_too_short",
    ),
    (
        "worthiness_below_bar",
        {"check_worthiness": 0.62},
        "drop",
        "low_check_worthiness",
    ),
    ("worthiness_at_bar", {"check_worthiness": 0.70}, "post", "ok"),
    (
        "worthiness_missing_fails_closed",
        {"check_worthiness": None},
        "drop",
        "low_check_worthiness",
    ),
    # -- topic policy ------------------------------------------------------ #
    (
        "topic_disabled",
        {"policy_overrides": {"topics": frozenset({"sports"})}},
        "drop",
        "topic_disabled",
    ),
    # -- freshness & repetition ------------------------------------------- #
    ("stale", {"claim_age_s": 91.0}, "drop", "stale"),
    ("fresh_at_boundary", {"claim_age_s": 90.0}, "post", "ok"),
    ("age_missing_fails_closed", {"claim_age_s": None}, "drop", "stale"),
    (
        "recently_posted_similar",
        {"history": FakeHistory(similar=True)},
        "drop",
        "recently_posted",
    ),
    (
        "burst_guard",
        {"history": FakeHistory(since_last=89.0)},
        "drop",
        "burst_guard",
    ),
    (
        "topic_cooldown",
        {"history": FakeHistory(since_topic=299.0)},
        "drop",
        "topic_cooldown",
    ),
    # -- rate caps (checked LAST) ------------------------------------------ #
    (
        "ten_minute_cap",
        {"history": FakeHistory(in_window={600.0: 3})},
        "drop",
        "ten_minute_cap",
    ),
    (
        "hourly_cap",
        {"history": FakeHistory(in_window={3600.0: 6})},
        "drop",
        "hourly_cap",
    ),
    # -- operational state ------------------------------------------------- #
    (
        "mode_off",
        {"policy_overrides": {"mode": "off"}},
        "drop",
        "mode_off",
    ),
    (
        "muted",
        {"context_overrides": {"muted_until": NOW + 600.0}},
        "drop",
        "muted",
    ),
    (
        "mute_expired_posts_again",
        {"context_overrides": {"muted_until": NOW - 1.0}},
        "post",
        "ok",
    ),
    (
        "join_grace",
        {"context_overrides": {"joined_at": NOW - 30.0}},
        "drop",
        "grace_period",
    ),
    (
        "latched",
        {"context_overrides": {"latched": True}},
        "drop",
        "posting_latched",
    ),
    # -- consent ----------------------------------------------------------- #
    (
        "consent_failure_wins",
        {"context_overrides": {"consent_failure": "not_armed"}},
        "drop",
        "not_armed",
    ),
    # -- review & probation ------------------------------------------------ #
    (
        "review_mode_queues",
        {"policy_overrides": {"mode": "review"}},
        "review",
        "review_mode",
    ),
    (
        "probation_forces_review_even_in_auto",
        {"context_overrides": {"probation_active": True}},
        "review",
        "review_mode",
    ),
]


class TestDecideTable:
    @pytest.mark.parametrize(
        ("kwargs", "action", "reason"),
        [case[1:] for case in CASES],
        ids=[case[0] for case in CASES],
    )
    def test_case(self, kwargs: dict, action: str, reason: str) -> None:
        decision = run_decide(**kwargs)
        assert (decision.action, decision.reason) == (action, reason)


class TestOrdering:
    def test_consent_beats_everything(self) -> None:
        """A verdict failing consent AND rate caps reports consent — the
        earliest broken link, which is also the one a human must fix first."""
        decision = run_decide(
            context_overrides={"consent_failure": "not_moderator"},
            history=FakeHistory(in_window={3600.0: 6}),
        )
        assert decision.reason == "not_moderator"

    def test_rate_caps_are_evaluated_last(self) -> None:
        """An hourly_cap drop means "otherwise fully postable" — the only
        honest input to tuning the cap. A verdict failing content AND rate
        must therefore report the content reason."""
        decision = run_decide(
            verdict_overrides={"used_fallback": True},
            history=FakeHistory(in_window={3600.0: 6}),
        )
        assert decision.reason == "degraded_parse"


class TestClamps:
    """Configuration can tighten the policy but never cross these lines."""

    def test_unverified_cannot_be_configured_postable(self) -> None:
        with pytest.raises(PolicyConfigError, match="UNVERIFIED"):
            PostingPolicy(labels=frozenset({"FALSE", "UNVERIFIED"}))

    def test_empty_label_set_is_rejected(self) -> None:
        with pytest.raises(PolicyConfigError, match="at least one"):
            PostingPolicy(labels=frozenset())

    def test_hourly_cap_ceiling(self) -> None:
        with pytest.raises(PolicyConfigError, match="posts_per_hour"):
            PostingPolicy(posts_per_hour=50)

    def test_min_gap_floor(self) -> None:
        with pytest.raises(PolicyConfigError, match="min_gap_s"):
            PostingPolicy(min_gap_s=5.0)

    def test_claim_cooldown_floor(self) -> None:
        with pytest.raises(PolicyConfigError, match="claim_cooldown_s"):
            PostingPolicy(claim_cooldown_s=60.0)

    def test_staleness_ceiling(self) -> None:
        with pytest.raises(PolicyConfigError, match="max_claim_age_s"):
            PostingPolicy(max_claim_age_s=600.0)

    def test_worthiness_floor(self) -> None:
        with pytest.raises(PolicyConfigError, match="min_check_worthiness"):
            PostingPolicy(min_check_worthiness=0.10)

    def test_min_sources_floor(self) -> None:
        with pytest.raises(PolicyConfigError, match="min_sources"):
            PostingPolicy(min_sources=1)

    def test_unknown_topics_rejected(self) -> None:
        with pytest.raises(PolicyConfigError, match="unknown topics"):
            PostingPolicy(topics=frozenset({"astrology"}))

    def test_default_mode_is_review(self) -> None:
        """Probation by default: nothing auto-posts until earned."""
        assert PostingPolicy().mode == "review"


class TestPurity:
    def test_decide_never_reads_the_clock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom() -> float:
            raise AssertionError("decide() must not read the wall clock")

        monkeypatch.setattr(time, "monotonic", boom)
        decision = run_decide()
        assert decision == run_decide()  # and it is deterministic

    def test_same_inputs_same_output(self) -> None:
        first = run_decide(history=FakeHistory(in_window={3600.0: 6}))
        second = run_decide(history=FakeHistory(in_window={3600.0: 6}))
        assert first == second


class TestInMemoryPostHistory:
    def test_counts_posts_in_windows(self) -> None:
        history = InMemoryPostHistory()
        history.record_post(make_verdict(), now=100.0)
        history.record_post(make_verdict(topic="sports"), now=700.0)
        assert history.posts_in_window(3600.0, now=1000.0) == 2
        assert history.posts_in_window(600.0, now=1000.0) == 1

    def test_seconds_since_last_and_topic(self) -> None:
        history = InMemoryPostHistory()
        assert history.seconds_since_last_post(now=50.0) == float("inf")
        history.record_post(make_verdict(topic="sports"), now=100.0)
        assert history.seconds_since_last_post(now=160.0) == 60.0
        assert history.seconds_since_topic_post("sports", now=160.0) == 60.0
        assert history.seconds_since_topic_post("health", now=160.0) == float("inf")

    def test_similarity_catches_paraphrases(self) -> None:
        history = InMemoryPostHistory()
        history.record_post(
            make_verdict(claim="The Eiffel Tower is 450 metres tall."), now=100.0
        )
        assert (
            history.similar_recent_claim(
                "The Eiffel Tower is 450 meters tall.", within_s=1800.0, now=200.0
            )
            is True
        )
        assert (
            history.similar_recent_claim(
                "Brazil has won five FIFA World Cups.", within_s=1800.0, now=200.0
            )
            is False
        )

    def test_similarity_expires_with_the_cooldown_window(self) -> None:
        history = InMemoryPostHistory()
        history.record_post(make_verdict(), now=100.0)
        assert (
            history.similar_recent_claim(
                "The Eiffel Tower is 450 metres tall.",
                within_s=600.0,
                now=800.0,  # 700 s later: outside the window
            )
            is False
        )


class TestSerialization:
    def test_roundtrip_is_lossless(self) -> None:
        policy = PostingPolicy(
            mode="auto",
            labels=frozenset({"FALSE", "TRUE"}),
            topics=frozenset({"sports", "other"}),
            posts_per_hour=3,
            source_tiers_extra={"localpaper.example": "B"},
        )
        restored = PostingPolicy().with_config(policy.to_config())
        assert restored == policy

    def test_with_config_is_partial(self) -> None:
        updated = PostingPolicy(mode="auto").with_config({"posts_per_hour": 3})
        assert updated.posts_per_hour == 3
        assert updated.mode == "auto"  # untouched keys survive

    def test_unknown_keys_are_rejected_not_ignored(self) -> None:
        """A typo'd knob silently doing nothing is worse than an error."""
        with pytest.raises(PolicyConfigError, match="unknown settings"):
            PostingPolicy().with_config({"post_per_hour": 3})

    def test_clamps_rerun_on_every_path(self) -> None:
        """with_config is the single entry point for the panel, !fc, and the
        persisted config — none of them can skip a clamp."""
        with pytest.raises(PolicyConfigError, match="posts_per_hour"):
            PostingPolicy().with_config({"posts_per_hour": 50})
        with pytest.raises(PolicyConfigError, match="UNVERIFIED"):
            PostingPolicy().with_config({"labels": ["FALSE", "UNVERIFIED"]})

    def test_tier_overrides_are_validated(self) -> None:
        with pytest.raises(PolicyConfigError, match="A/B/C/D"):
            PostingPolicy(source_tiers_extra={"example.com": "S"})  # type: ignore[dict-item]
        with pytest.raises(PolicyConfigError, match="lowercase bare domain"):
            PostingPolicy(source_tiers_extra={"Example.Com": "B"})
        with pytest.raises(PolicyConfigError, match="lowercase bare domain"):
            PostingPolicy(source_tiers_extra={"https://example.com/x": "B"})

    def test_config_is_json_safe(self) -> None:
        import json

        assert json.loads(json.dumps(PostingPolicy().to_config()))
