"""Verification-call throttling: a token bucket plus a quota cooldown latch."""

import asyncio
import time

# Shown while a cooldown is active but no tripping provider recorded a
# reason (e.g. a cooldown tripped manually in tests or debug tooling).
DEFAULT_COOLDOWN_REASON = "LLM quota or credits exhausted."


class TokenBucket:
    """Async token bucket; ``acquire`` awaits refill and never busy-waits.

    Waiters are serialized behind one lock, which is exactly what the serial
    verify loop wants: FIFO-ish ordering and trivially correct RPM accounting.
    """

    def __init__(self, rate_per_min: float = 8.0, burst: int = 2) -> None:
        if rate_per_min <= 0:
            raise ValueError(f"rate_per_min must be > 0, got {rate_per_min}")
        if burst < 1:
            raise ValueError(f"burst must be >= 1, got {burst}")
        self._rate_per_s = rate_per_min / 60.0
        self._burst = float(burst)
        self._tokens = float(burst)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._last_refill = now
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate_per_s)

    async def acquire(self) -> None:
        """Take one token, sleeping exactly until one is available."""
        async with self._lock:
            self._refill()
            if self._tokens < 1.0:
                wait_s = (1.0 - self._tokens) / self._rate_per_s
                await asyncio.sleep(wait_s)
                self._refill()
            self._tokens -= 1.0


class QuotaCooldown:
    """Latched pause tripped by quota failures (429/402); claims are dropped
    while active."""

    def __init__(self) -> None:
        self._until = 0.0
        self._reason: str | None = None

    def trip(self, retry_after_s: float, reason: str | None = None) -> None:
        """Start (or extend) the cooldown for ``retry_after_s`` seconds.

        ``reason`` is the tripping provider's human-readable cause (e.g. the
        OpenRouter "top up at openrouter.ai" 402 message); it is replayed in
        every cooldown-active error frame so users are pointed at the RIGHT
        provider's dashboard for the whole cooldown, not just the first frame.
        """
        deadline = time.monotonic() + max(retry_after_s, 0.0)
        self._until = max(self._until, deadline)
        if reason:
            self._reason = reason

    @property
    def active(self) -> bool:
        return time.monotonic() < self._until

    @property
    def remaining_s(self) -> float:
        """Seconds left on the cooldown (0.0 when inactive)."""
        return max(0.0, self._until - time.monotonic())

    @property
    def reason(self) -> str:
        """Human-readable cause of the cooldown (provider-neutral default)."""
        return self._reason or DEFAULT_COOLDOWN_REASON
