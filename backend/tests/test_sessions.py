"""Unit tests for the live-session registry (report §6.2).

The endpoint-level behaviour (preempt-on-connect, superseded frames, close
codes) is covered by ``test_ws_protocol.py``; this module tests the registry
in isolation with a minimal pipeline double, so the preemption scopes and the
capacity cap can be exercised without a WebSocket.
"""

import pytest

from app.sessions import (
    SessionLimitExceeded,
    SessionRegistry,
    channel_key,
)


class FakePipeline:
    """The slice of SessionPipeline the registry actually touches."""

    def __init__(
        self, session_id: str, platform: str | None = None, channel: str | None = None
    ) -> None:
        self.session_id = session_id
        self.platform = platform
        self.channel = channel
        self.preempted = False
        self.preempt_calls: list[str] = []

    @property
    def channel_key(self) -> tuple[str | None, str | None]:
        return channel_key(self.platform, self.channel)

    async def preempt(self, code: str = "superseded", message: str = "") -> None:
        self.preempted = True
        self.preempt_calls.append(code)


class TestChannelKey:
    def test_lowercases_the_channel(self) -> None:
        assert channel_key("twitch", "TestStreamer") == ("twitch", "teststreamer")

    def test_strips_surrounding_whitespace(self) -> None:
        assert channel_key("twitch", "  ade  ") == ("twitch", "ade")

    def test_empty_channel_collapses_to_none(self) -> None:
        """A client sending channel="" lands in the same bucket as one that
        omits it — otherwise "" and None would be two distinct scopes."""
        assert channel_key("twitch", "") == ("twitch", None)
        assert channel_key("twitch", "   ") == ("twitch", None)

    def test_platform_is_not_folded(self) -> None:
        """Platform comes from a Literal on the wire, so it is already
        canonical; only the free-text channel needs folding."""
        assert channel_key(None, None) == (None, None)


class TestGlobalScope:
    """The default: today's single-session behaviour, unchanged."""

    async def test_new_session_preempts_any_existing(self) -> None:
        registry = SessionRegistry(scope="global")
        first = FakePipeline("a", "twitch", "alice")
        second = FakePipeline("b", "twitch", "bob")
        await registry.register(first)
        await registry.register(second)

        assert first.preempted is True
        assert second.preempted is False

    async def test_preempt_on_accept_kills_the_incumbent(self) -> None:
        """Only the global scope can preempt before the hello parses — this
        is the §3.2 promptness rule the extension's reconnect relies on."""
        registry = SessionRegistry(scope="global")
        live = FakePipeline("a")
        await registry.register(live)

        await registry.preempt_on_accept()

        assert live.preempted is True


class TestChannelScope:
    async def test_same_channel_preempts(self) -> None:
        registry = SessionRegistry(scope="channel")
        first = FakePipeline("a", "twitch", "alice")
        second = FakePipeline("b", "twitch", "alice")
        await registry.register(first)
        await registry.register(second)

        assert first.preempted is True

    async def test_different_channels_coexist(self) -> None:
        registry = SessionRegistry(scope="channel")
        alice = FakePipeline("a", "twitch", "alice")
        bob = FakePipeline("b", "twitch", "bob")
        await registry.register(alice)
        await registry.register(bob)

        assert alice.preempted is False
        assert bob.preempted is False
        assert len(registry) == 2

    async def test_channel_match_is_case_insensitive(self) -> None:
        registry = SessionRegistry(scope="channel")
        first = FakePipeline("a", "twitch", "Alice")
        second = FakePipeline("b", "twitch", "alice")
        await registry.register(first)
        await registry.register(second)

        assert first.preempted is True

    async def test_same_channel_on_different_platforms_coexist(self) -> None:
        registry = SessionRegistry(scope="channel")
        twitch = FakePipeline("a", "twitch", "alice")
        kick = FakePipeline("b", "kick", "alice")
        await registry.register(twitch)
        await registry.register(kick)

        assert twitch.preempted is False

    async def test_identityless_sessions_share_one_bucket(self) -> None:
        """Clients that send no platform/channel keep the old global-slot
        behaviour rather than accumulating unbounded anonymous sessions."""
        registry = SessionRegistry(scope="channel")
        first = FakePipeline("a")
        second = FakePipeline("b")
        await registry.register(first)
        await registry.register(second)

        assert first.preempted is True

    async def test_preempt_on_accept_is_a_noop(self) -> None:
        registry = SessionRegistry(scope="channel")
        live = FakePipeline("a", "twitch", "alice")
        await registry.register(live)

        await registry.preempt_on_accept()

        assert live.preempted is False


class TestNoneScope:
    async def test_never_preempts(self) -> None:
        registry = SessionRegistry(scope="none", max_sessions=10)
        first = FakePipeline("a", "twitch", "alice")
        second = FakePipeline("b", "twitch", "alice")
        await registry.register(first)
        await registry.register(second)

        assert first.preempted is False
        assert len(registry) == 2


class TestCapacity:
    async def test_registering_past_the_cap_raises(self) -> None:
        registry = SessionRegistry(scope="none", max_sessions=2)
        await registry.register(FakePipeline("a"))
        await registry.register(FakePipeline("b"))

        with pytest.raises(SessionLimitExceeded) as exc:
            await registry.register(FakePipeline("c"))
        assert exc.value.limit == 2

    async def test_a_preempted_session_does_not_occupy_a_slot(self) -> None:
        """A same-channel reconnect must not spuriously hit the cap: the
        victim's unregister races its replacement's register, so capacity
        counts only sessions that survived preemption."""
        registry = SessionRegistry(scope="channel", max_sessions=1)
        first = FakePipeline("a", "twitch", "alice")
        await registry.register(first)

        second = FakePipeline("b", "twitch", "alice")
        await registry.register(second)  # must not raise

        assert first.preempted is True
        assert registry.get("b") is second

    async def test_unregistering_frees_a_slot(self) -> None:
        registry = SessionRegistry(scope="none", max_sessions=1)
        first = FakePipeline("a")
        await registry.register(first)
        registry.unregister(first)

        await registry.register(FakePipeline("b"))  # must not raise


class TestBookkeeping:
    async def test_unregister_is_identity_checked(self) -> None:
        """A preempted pipeline's ``finally`` can run after its replacement
        claimed the id-keyed slot; it must not evict the newcomer."""
        registry = SessionRegistry(scope="none")
        original = FakePipeline("same-id")
        replacement = FakePipeline("same-id")
        await registry.register(original)
        await registry.register(replacement)

        registry.unregister(original)

        assert registry.get("same-id") is replacement

    async def test_unregister_of_an_unknown_session_is_a_noop(self) -> None:
        registry = SessionRegistry(scope="none")
        registry.unregister(FakePipeline("ghost"))  # must not raise

    async def test_all_returns_a_snapshot_not_a_live_view(self) -> None:
        """Callers await inside their loop over all(), and each preempt
        eventually mutates the dict — so a live view would risk "changed
        size during iteration"."""
        registry = SessionRegistry(scope="none")
        live = FakePipeline("a")
        await registry.register(live)

        snapshot = registry.all()
        registry.unregister(live)

        assert snapshot == [live]
        assert registry.all() == []

    async def test_for_channel_filters_by_normalized_key(self) -> None:
        registry = SessionRegistry(scope="none")
        alice = FakePipeline("a", "twitch", "Alice")
        bob = FakePipeline("b", "twitch", "bob")
        await registry.register(alice)
        await registry.register(bob)

        assert registry.for_channel(("twitch", "alice")) == [alice]
        assert registry.for_channel(("twitch", "nobody")) == []
