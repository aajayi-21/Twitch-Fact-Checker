"""Per-call provider usage, collected so the viewer can see what a session cost.

Every provider call the pipeline makes (claim gate, verification) reports its
token and web-search counts here. The pipeline wraps each call in
:func:`collect_usage` and forwards what was collected to the client as
:class:`app.models.UsageFrame` frames; the extension prices them and keeps the
running totals in its own ledger.

Why a context variable rather than a return value: usage is produced several
layers below the pipeline (a verify can make up to three provider calls —
strict, fallback, extraction — inside one ``FactChecker.check``), and the
provider-neutral ``ClaimGate`` / ``FactChecker`` interfaces return domain
objects, not responses. A context variable lets every call report without
widening those interfaces. ``asyncio`` copies the context into each task, so
concurrent verifies collect independently.

Token convention (identical to the extension's ``Usage`` shape in
``extension/engine/providers/common.mjs``):

- ``input_tokens``: input billed at the full rate — uncached input plus any
  tokens written to the cache.
- ``cached_input_tokens``: input read back from the provider's prompt cache.
- ``cost_usd``: the provider's own figure for the call when it reports one
  (OpenRouter does); ``None`` means "estimate it from a price table".
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal

UsageStage = Literal["gate", "verify"]


@dataclass(frozen=True)
class UsageEvent:
    """What one provider call consumed."""

    stage: UsageStage
    provider: str
    model: str
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    web_searches: int = 0
    cost_usd: float | None = None


_collector: ContextVar[list[UsageEvent] | None] = ContextVar(
    "usage_collector", default=None
)


def report_usage(event: UsageEvent) -> None:
    """Hand one call's usage to the enclosing :func:`collect_usage`, if any.

    A no-op outside a collection scope (debug endpoints, unit tests), so
    providers can report unconditionally.
    """
    collector = _collector.get()
    if collector is not None:
        collector.append(event)


@contextmanager
def collect_usage() -> Iterator[list[UsageEvent]]:
    """Collect every usage event reported while the block runs.

    The yielded list fills as calls complete and stays valid after the block,
    including when the block raises — a failed verify still spent tokens.
    """
    events: list[UsageEvent] = []
    token = _collector.set(events)
    try:
        yield events
    finally:
        _collector.reset(token)


def _count(value: Any) -> int:
    """A non-negative int from an SDK usage field that may be missing."""
    return value if isinstance(value, int) and value > 0 else 0


def anthropic_usage(response: Any, stage: UsageStage, model: str) -> UsageEvent | None:
    """Read a Messages API response's ``usage`` (``None`` when absent)."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    server_tools = getattr(usage, "server_tool_use", None)
    return UsageEvent(
        stage=stage,
        provider="anthropic",
        model=model,
        input_tokens=_count(getattr(usage, "input_tokens", None))
        + _count(getattr(usage, "cache_creation_input_tokens", None)),
        cached_input_tokens=_count(getattr(usage, "cache_read_input_tokens", None)),
        output_tokens=_count(getattr(usage, "output_tokens", None)),
        web_searches=_count(getattr(server_tools, "web_search_requests", None)),
    )


def chat_completion_usage(
    response: Any,
    stage: UsageStage,
    provider: str,
    model: str,
    *,
    assumed_web_searches: int = 0,
) -> UsageEvent | None:
    """Read an OpenAI-style chat completion's ``usage`` (``None`` when absent).

    ``prompt_tokens`` includes cached tokens in this API family, so they are
    subtracted to match the token convention above. OpenRouter adds two
    fields the OpenAI SDK model does not declare — ``cost`` (exact USD for the
    request) and ``server_tool_use`` — which pydantic keeps as extras, hence
    the ``getattr`` reads. ``assumed_web_searches`` is used when the response
    does not count searches itself (the ``web`` plugin runs one per request).
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    details = getattr(usage, "prompt_tokens_details", None)
    cached_tokens = _count(getattr(details, "cached_tokens", None))
    server_tools = getattr(usage, "server_tool_use", None)
    reported_searches = (
        server_tools.get("web_search_requests")
        if isinstance(server_tools, dict)
        else getattr(server_tools, "web_search_requests", None)
    )
    cost = getattr(usage, "cost", None)
    return UsageEvent(
        stage=stage,
        provider=provider,
        model=model,
        input_tokens=max(
            _count(getattr(usage, "prompt_tokens", None)) - cached_tokens, 0
        ),
        cached_input_tokens=cached_tokens,
        output_tokens=_count(getattr(usage, "completion_tokens", None)),
        web_searches=(
            _count(reported_searches)
            if isinstance(reported_searches, int)
            else assumed_web_searches
        ),
        cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
    )


def report_response_usage(event: UsageEvent | None) -> None:
    """:func:`report_usage` that tolerates a response with no usage block."""
    if event is not None:
        report_usage(event)
