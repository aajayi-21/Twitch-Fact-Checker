"""TokenBucket refill timing and QuotaCooldown latch behavior."""

import asyncio
import time

import pytest

from app.rate_limit import DEFAULT_COOLDOWN_REASON, QuotaCooldown, TokenBucket


class TestTokenBucketConstruction:
    def test_rejects_non_positive_rate(self) -> None:
        with pytest.raises(ValueError, match="rate_per_min"):
            TokenBucket(rate_per_min=0.0)

    def test_rejects_zero_burst(self) -> None:
        with pytest.raises(ValueError, match="burst"):
            TokenBucket(rate_per_min=8.0, burst=0)


class TestTokenBucketTiming:
    async def test_burst_tokens_are_immediate(self) -> None:
        bucket = TokenBucket(rate_per_min=600.0, burst=2)  # 10 tokens/s
        started = time.monotonic()
        await bucket.acquire()
        await bucket.acquire()
        assert time.monotonic() - started < 0.05

    async def test_acquire_beyond_burst_waits_for_refill(self) -> None:
        bucket = TokenBucket(rate_per_min=600.0, burst=2)  # 10 tokens/s
        await bucket.acquire()
        await bucket.acquire()
        started = time.monotonic()
        await bucket.acquire()  # bucket empty -> ~0.1 s refill wait
        elapsed = time.monotonic() - started
        assert 0.05 <= elapsed < 1.0

    async def test_idle_time_refills_tokens(self) -> None:
        bucket = TokenBucket(rate_per_min=600.0, burst=2)  # 10 tokens/s
        await bucket.acquire()
        await bucket.acquire()
        await asyncio.sleep(0.15)  # refill >= 1 token
        started = time.monotonic()
        await bucket.acquire()
        assert time.monotonic() - started < 0.05

    async def test_tokens_cap_at_burst(self) -> None:
        bucket = TokenBucket(rate_per_min=6000.0, burst=2)  # 100 tokens/s
        await asyncio.sleep(0.1)  # would refill ~10 tokens without the cap
        started = time.monotonic()
        await bucket.acquire()
        await bucket.acquire()
        await bucket.acquire()  # third must wait: cap really is 2
        elapsed = time.monotonic() - started
        assert elapsed >= 0.005


class TestQuotaCooldown:
    def test_inactive_initially(self) -> None:
        cooldown = QuotaCooldown()
        assert cooldown.active is False
        assert cooldown.remaining_s == 0.0

    def test_trip_activates_with_bounded_remaining(self) -> None:
        cooldown = QuotaCooldown()
        cooldown.trip(0.5)
        assert cooldown.active is True
        assert 0.0 < cooldown.remaining_s <= 0.5

    async def test_expires_after_deadline(self) -> None:
        cooldown = QuotaCooldown()
        cooldown.trip(0.05)
        await asyncio.sleep(0.1)
        assert cooldown.active is False
        assert cooldown.remaining_s == 0.0

    def test_shorter_trip_never_shortens_active_cooldown(self) -> None:
        cooldown = QuotaCooldown()
        cooldown.trip(5.0)
        cooldown.trip(1.0)
        assert cooldown.remaining_s > 4.0

    def test_longer_trip_extends(self) -> None:
        cooldown = QuotaCooldown()
        cooldown.trip(1.0)
        cooldown.trip(10.0)
        assert cooldown.remaining_s > 5.0

    def test_negative_retry_after_is_clamped(self) -> None:
        cooldown = QuotaCooldown()
        cooldown.trip(-30.0)
        assert cooldown.active is False

    def test_reason_defaults_to_provider_neutral_text(self) -> None:
        cooldown = QuotaCooldown()
        cooldown.trip(5.0)
        assert cooldown.reason == DEFAULT_COOLDOWN_REASON
        assert "Gemini" not in cooldown.reason
        assert "OpenRouter" not in cooldown.reason

    def test_trip_records_reason_for_replay(self) -> None:
        cooldown = QuotaCooldown()
        cooldown.trip(5.0, reason="OpenRouter credits exhausted; top up.")
        assert cooldown.reason == "OpenRouter credits exhausted; top up."

    def test_trip_without_reason_keeps_previous_reason(self) -> None:
        cooldown = QuotaCooldown()
        cooldown.trip(5.0, reason="OpenRouter credits exhausted; top up.")
        cooldown.trip(1.0)
        assert cooldown.reason == "OpenRouter credits exhausted; top up."
