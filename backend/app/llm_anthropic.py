"""Anthropic transport: the direct Claude Messages API (default provider).

Serves all three LLM jobs with one ``AsyncAnthropic`` client:

- :class:`AnthropicClaimGate` — claim extraction and the contradiction judge,
  with strict JSON output (``output_config.format``) and thinking OFF.
- :class:`AnthropicFactChecker` — grounded verification with Anthropic's
  server-side ``web_search`` tool. Unlike OpenRouter's ``web`` plugin, the
  model runs the searches itself, so the prompts tell it to search
  (:func:`app.prompts.build_anthropic_verify_messages`).

Authentication. ``ANTHROPIC_API_KEY`` is optional. With a key, it is passed
explicitly. Without one, the client is built with NO credential argument and
the SDK resolves its own chain: ``ANTHROPIC_AUTH_TOKEN``,
``ANTHROPIC_PROFILE``, Workload Identity Federation, then the active
``ant auth login`` profile (whose token the SDK refreshes itself).

Request rules for Claude Haiku 5.5 (and the current API version):

- never send ``temperature`` (non-default values are rejected) and never
  prefill an assistant turn;
- thinking is ``{"type": "disabled"}`` for effort "none" — valid only at
  effort ``high`` or below, so it pairs with effort ``low`` — or
  ``{"type": "adaptive"}`` plus ``output_config.effort`` for a level;
- the static system prompts carry ``cache_control`` (Haiku 5.5 caches
  prefixes from 512 tokens, so the ~1.7k-token gate instructions are read from
  the cache on every pass).

Citations and anti-hallucination. Sources come only from the search tool's
own result blocks and ``web_search_result_location`` citations. The model may
NAME the result URLs it relied on (``source_urls``), but a named URL survives
only if the search tool actually returned it (:func:`select_sources`). A
verdict reached without any search has no sources, and
:meth:`app.fact_checker.FactChecker._enforce_invariants` downgrades it to
UNVERIFIED.

Error contract. Gate methods raise only :class:`GateError`. The checker raises
only :class:`QuotaExceededError` (after tripping the cooldown),
:class:`VerificationError`, :class:`_FallbackNeeded` (structured path only) or
:class:`asyncio.TimeoutError` — anything else would escape the pipeline and
end the session.
"""

import asyncio
import json
import logging
from collections.abc import Iterable, Sequence
from typing import Any

import anthropic
from anthropic import AsyncAnthropic
from pydantic import ValidationError

from app.claim_gate import (
    CONTRADICTION_JSON_SCHEMA,
    GATE_JSON_SCHEMA,
    ClaimGate,
    GateError,
    parse_contradiction_judgement,
    parse_gate_result,
)
from app.fact_checker import (
    DEFAULT_RETRY_AFTER_S,
    EVIDENCE_LEVELS,
    FLAT_VERDICT_SCHEMA,
    MAX_SOURCES,
    FactChecker,
    QuotaExceededError,
    VerificationError,
    _FallbackNeeded,
    _today,
)
from app.models import ContradictionJudgement, GateClaim, Source, VerdictPayload
from app.prompts import (
    build_anthropic_verify_fallback_messages,
    build_anthropic_verify_messages,
    build_contradiction_prompt,
    build_gate_prompt_parts,
    build_verdict_extraction_messages,
)
from app.rate_limit import QuotaCooldown

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 3.0
DEFAULT_READ_TIMEOUT_S = 30.0

# Output caps. The gate runs with thinking off, so 1200 tokens is ample for
# the claims JSON. Verification thinks (adaptive, low effort) and the thinking
# tokens count against max_tokens, hence the larger cap.
GATE_MAX_TOKENS = 1200
VERIFY_MAX_TOKENS = 4096
EXTRACTION_MAX_TOKENS = 800

# The server runs web searches in a sampling loop and may hand back
# ``stop_reason="pause_turn"`` mid-turn; the request is re-sent with the
# paused content to resume. Bounded so a stuck turn cannot loop forever (the
# FactChecker's verify timeout bounds it in time as well).
MAX_PAUSE_CONTINUATIONS = 2

# Out of credit (or over the organization's monthly spend limit). Unlike a
# 429 this will not clear in a minute, so the cooldown is long enough to stop
# a doomed request loop.
CREDITS_EXHAUSTED_COOLDOWN_S = 15 * 60.0
CREDITS_EXHAUSTED_MESSAGE = (
    "Anthropic credit exhausted or spend limit reached — check Billing in the "
    "Claude Console (platform.claude.com) for the organization this backend "
    "authenticates to"
)
CREDENTIALS_REJECTED_HINT = (
    "if you use `ant auth login`, run it again (sign-ins expire after some "
    "weeks); if you use ANTHROPIC_API_KEY, check the key in backend/.env"
)

#: ``ANTHROPIC_WEB_SEARCH_TOOL`` -> server tool version.
WEB_SEARCH_TOOL_TYPES: dict[str, str] = {
    "basic": "web_search_20250305",
    "dynamic": "web_search_20260209",
}

# Strict verify schema: the shared label/explanation fields plus the evidence
# rating and the model's choice of result URLs (filtered in code). Evidence
# and explanation come before the label so the model states what it found
# before it commits to a verdict.
ANTHROPIC_VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "evidence": {"type": "string", "enum": list(EVIDENCE_LEVELS)},
        "explanation": {"type": "string"},
        "label": FLAT_VERDICT_SCHEMA["properties"]["label"],
        "source_urls": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["evidence", "explanation", "label", "source_urls"],
    "additionalProperties": False,
}

# Last-resort extraction of a free-text verdict (no search, no evidence
# rating — the text chain's evidence line is optional anyway).
EXTRACTION_SCHEMA: dict[str, Any] = {
    **FLAT_VERDICT_SCHEMA,
    "additionalProperties": False,
}

# Content block types that end a "segment" of model prose: anything the
# server tool loop inserted. The verdict text is the prose AFTER the last one.
_TOOL_BLOCK_SUFFIXES = ("tool_use", "tool_result")


def create_anthropic_client(api_key: str | None) -> AsyncAnthropic:
    """One process-wide ``AsyncAnthropic`` client.

    ``api_key`` given -> used explicitly. ``None`` -> no credential argument,
    so the SDK resolves ``ANTHROPIC_AUTH_TOKEN``, ``ANTHROPIC_PROFILE``,
    federation, or the active ``ant auth login`` profile. ``max_retries=0``:
    the pipeline owns retries and cooldowns. Per-call timeouts are applied with
    ``client.with_options``.

    Raises:
        RuntimeError: when no key is given and the SDK finds no credentials.
    """
    common: dict[str, Any] = {
        "timeout": anthropic.Timeout(DEFAULT_READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
        "max_retries": 0,
    }
    if api_key:
        return AsyncAnthropic(api_key=api_key, **common)
    try:
        return AsyncAnthropic(**common)
    except anthropic.CredentialsError as exc:
        raise RuntimeError(
            "No Anthropic credentials found: run `ant auth login` (no API key "
            f"needed) or set ANTHROPIC_API_KEY in backend/.env ({exc})"
        ) from exc


def thinking_params(effort: str) -> tuple[dict[str, Any] | None, str | None]:
    """``(thinking, output_config.effort)`` for a configured effort.

    - ``"none"`` -> thinking disabled at effort ``low`` (disabled thinking is
      only accepted at effort ``high`` or below);
    - ``""``     -> neither field (the model's defaults);
    - a level    -> adaptive thinking at that effort.
    """
    effort = effort.strip()
    if not effort:
        return None, None
    if effort == "none":
        return {"type": "disabled"}, "low"
    return {"type": "adaptive"}, effort


def _request_timeout(seconds: float) -> anthropic.Timeout:
    return anthropic.Timeout(seconds, connect=CONNECT_TIMEOUT_S)


def _cached_system(text: str) -> list[dict[str, Any]]:
    """A system prompt as one cacheable text block."""
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def _error_message(exc: anthropic.APIStatusError) -> str:
    """The API's own error message, falling back to the SDK's."""
    return (getattr(exc, "api_message", None) or str(exc.message) or "").strip()


def is_credit_exhausted(exc: anthropic.APIStatusError) -> bool:
    """402 / ``billing_error``, a monthly spend limit, or the classic
    "Your credit balance is too low" 400."""
    if exc.status_code == 402 or getattr(exc, "type", None) == "billing_error":
        return True
    if getattr(exc, "error_code", None) == "enforced_spend_limit_reached":
        return True
    return exc.status_code == 400 and "credit balance" in _error_message(exc).lower()


def _retry_after_seconds(exc: anthropic.APIStatusError) -> float:
    """``Retry-After`` header from a 429 response, default 60 s."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is not None:
        header_value = headers.get("retry-after")
        if header_value:
            try:
                return float(header_value)
            except ValueError:
                pass
    return DEFAULT_RETRY_AFTER_S


def _is_tool_block(block: Any) -> bool:
    return str(getattr(block, "type", "")).endswith(_TOOL_BLOCK_SUFFIXES)


def final_text(content: Iterable[Any]) -> str:
    """The model's prose after the last server-tool block, joined.

    With web search the content is ``[text?, server_tool_use,
    web_search_tool_result, ..., text, text...]``: text before a search is
    narration ("I'll search for..."), and the verdict is the text after the
    last tool block. Thinking blocks are skipped.
    """
    segment: list[str] = []
    for block in content:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            segment.append(getattr(block, "text", "") or "")
        elif _is_tool_block(block):
            segment = []
    return "".join(segment)


def _url_key(url: str) -> str:
    """Loose match key so a copied URL with/without a trailing slash matches."""
    return url.strip().rstrip("/")


def select_sources(
    content: Sequence[Any], picked_urls: Sequence[str] = ()
) -> list[Source]:
    """Sources for a verdict, taken ONLY from the search tool's output.

    1. ``picked_urls`` (the model's ``source_urls``) that the search tool
       actually returned or cited — fabricated URLs are dropped;
    2. otherwise the URLs cited in the answer text;
    3. otherwise the retrieved results, in search order.

    Deduplicated by URL and capped at :data:`MAX_SOURCES`. No search at all
    -> no sources (and the invariants force UNVERIFIED).
    """
    retrieved: dict[str, Source] = {}
    cited: dict[str, Source] = {}
    for block in content:
        block_type = getattr(block, "type", None)
        if block_type == "web_search_tool_result":
            results = getattr(block, "content", None)
            if not isinstance(results, list):  # an error object, e.g. max_uses
                continue
            for result in results:
                url = getattr(result, "url", None)
                if getattr(result, "type", None) == "web_search_result" and url:
                    retrieved.setdefault(
                        _url_key(url),
                        Source(url=url, title=getattr(result, "title", None)),
                    )
        elif block_type == "text":
            for citation in getattr(block, "citations", None) or ():
                url = getattr(citation, "url", None)
                if (
                    getattr(citation, "type", None) == "web_search_result_location"
                    and url
                ):
                    cited.setdefault(
                        _url_key(url),
                        Source(url=url, title=getattr(citation, "title", None)),
                    )

    known = {**cited, **retrieved}
    picked: dict[str, Source] = {}
    for url in picked_urls:
        key = _url_key(url) if isinstance(url, str) else ""
        if key in known:
            picked.setdefault(key, known[key])
        elif key:
            logger.info("dropping model-named source not returned by search: %s", url)
    for candidates in (picked, cited, retrieved):
        if candidates:
            return list(candidates.values())[:MAX_SOURCES]
    return []


def _log_usage(response: Any, stage: str, model: str) -> None:
    """Token and web-search counts at DEBUG (for watching credit spend)."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    server_tools = getattr(usage, "server_tool_use", None)
    logger.debug(
        "anthropic %s (%s): stop=%s searches=%s in=%s cache_read=%s out=%s",
        stage,
        model,
        getattr(response, "stop_reason", None),
        getattr(server_tools, "web_search_requests", 0) if server_tools else 0,
        getattr(usage, "input_tokens", None),
        getattr(usage, "cache_read_input_tokens", None),
        getattr(usage, "output_tokens", None),
    )


class _SchemaRejected(GateError):
    """Internal: a 400 on a gate call that sent a JSON schema."""


class AnthropicClaimGate(ClaimGate):
    """Gate transport: strict JSON output, thinking off, cached instructions.

    A 400 on a schema call is retried once without the schema; when that
    retry succeeds the schema was the problem, and a process-wide latch stops
    sending it (the gate prompt spells the JSON shape out in words anyway).
    """

    # Process-wide: structured-output support is a property of the model and
    # API, not of any session. Reset by the conftest autouse fixture.
    _json_schema_unsupported: bool = False

    def __init__(
        self,
        client: AsyncAnthropic,
        model: str,
        gate_interval_s: float = 12.0,
        gate_timeout_s: float = 15.0,
        reasoning_effort: str = "none",
    ) -> None:
        super().__init__(gate_interval_s=gate_interval_s, gate_timeout_s=gate_timeout_s)
        self._client = client
        self._model = model
        self._reasoning_effort = reasoning_effort

    async def _extract(self, context: str, new_text: str) -> list[GateClaim]:
        """One gate pass: cached instructions + this batch's transcript."""
        instructions, transcript = build_gate_prompt_parts(context, new_text)
        raw = await self._complete_json(
            instructions, transcript, GATE_JSON_SCHEMA["schema"], what="gate"
        )
        return parse_gate_result(raw)

    async def judge_contradiction(
        self, current: str, prior: str
    ) -> ContradictionJudgement:
        """Same-speaker contradiction judgement (no search)."""
        raw = await self._complete_json(
            None,
            build_contradiction_prompt(current, prior),
            CONTRADICTION_JSON_SCHEMA["schema"],
            what="contradiction judge",
        )
        return parse_contradiction_judgement(raw)

    async def _complete_json(
        self, system: str | None, user: str, schema: dict[str, Any], what: str
    ) -> str:
        """Schema call, with the one-shot no-schema retry described above."""
        if AnthropicClaimGate._json_schema_unsupported:
            return await self._call(system, user, None, what)
        try:
            return await self._call(system, user, schema, what)
        except _SchemaRejected as rejected:
            raw = await self._call(system, user, None, what)
            AnthropicClaimGate._json_schema_unsupported = True
            logger.warning(
                "Anthropic rejected structured output for %s (%s); sending "
                "prompt-only JSON from now on",
                self._model,
                rejected,
            )
            return raw

    async def _call(
        self,
        system: str | None,
        user: str,
        schema: dict[str, Any] | None,
        what: str,
    ) -> str:
        """One Messages call; every failure becomes a :class:`GateError`."""
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": GATE_MAX_TOKENS,
            "messages": [{"role": "user", "content": user}],
        }
        if system:
            kwargs["system"] = _cached_system(system)
        thinking, effort = thinking_params(self._reasoning_effort)
        output_config: dict[str, Any] = {}
        if thinking is not None:
            kwargs["thinking"] = thinking
        if effort is not None:
            output_config["effort"] = effort
        if schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        if output_config:
            kwargs["output_config"] = output_config

        try:
            response = await self._client.with_options(
                timeout=_request_timeout(self._gate_timeout_s)
            ).messages.create(**kwargs)
        except anthropic.APITimeoutError as exc:
            raise GateError(
                f"{what} call timed out after {self._gate_timeout_s:g}s"
            ) from exc
        except anthropic.APIStatusError as exc:
            raise self._status_error(exc, what, schema_sent=schema is not None) from exc
        except anthropic.APIConnectionError as exc:
            raise GateError(
                f"{what} call could not reach the Anthropic API: {exc}"
            ) from exc
        except anthropic.AnthropicError as exc:
            raise GateError(f"{what} call failed: {exc}") from exc

        _log_usage(response, what, self._model)
        if response.stop_reason == "refusal":
            raise GateError(f"{what} call declined by the model (refusal)")
        if response.stop_reason == "max_tokens":
            raise GateError(
                f"{what} output hit the {GATE_MAX_TOKENS}-token cap before the "
                "JSON was complete (keep ANTHROPIC_GATE_REASONING_EFFORT=none)"
            )
        text = final_text(response.content)
        if not text.strip():
            raise GateError(f"{what} call returned no text")
        return text

    def _status_error(
        self, exc: anthropic.APIStatusError, what: str, schema_sent: bool
    ) -> GateError:
        """Map a status error to a :class:`GateError` (the batch is dropped)."""
        message = _error_message(exc)
        if is_credit_exhausted(exc):
            return GateError(
                f"{what} call failed ({exc.status_code}): "
                f"{CREDITS_EXHAUSTED_MESSAGE} [{message}]"
            )
        if isinstance(
            exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)
        ):
            return GateError(
                f"{what} call failed ({exc.status_code}): Anthropic rejected the "
                f"credentials — {CREDENTIALS_REJECTED_HINT} [{message}]"
            )
        if schema_sent and isinstance(exc, anthropic.BadRequestError):
            return _SchemaRejected(f"{what} call failed (400): {message}")
        if isinstance(exc, anthropic.OverloadedError):
            return GateError(f"{what} call failed (529): Anthropic is overloaded")
        return GateError(f"{what} call failed ({exc.status_code}): {message}")


class _StructuredRejected(Exception):
    """Internal: the structured verify call got a 400 (schema + search)."""


class AnthropicFactChecker(FactChecker):
    """Verify transport: Claude + the server-side ``web_search`` tool.

    Structured path: strict :data:`ANTHROPIC_VERDICT_SCHEMA` output with
    search. A 400 there (for example if structured output cannot be combined
    with web search's citations) latches the structured path off
    process-wide and every claim takes the fallback: the same search with the
    LABEL:/EVIDENCE:/EXPLANATION: text format, then one schema-constrained
    extraction pass (no search) if that text does not parse.
    """

    # Process-wide latch; reset by the conftest autouse fixture.
    _structured_unsupported: bool = False

    def __init__(
        self,
        client: AsyncAnthropic,
        verify_model: str,
        cooldown: QuotaCooldown,
        verify_timeout_s: float = 45.0,
        reasoning_effort: str = "low",
        web_max_uses: int = 2,
        web_search_tool: str = "basic",
    ) -> None:
        super().__init__(cooldown=cooldown, verify_timeout_s=verify_timeout_s)
        if web_search_tool not in WEB_SEARCH_TOOL_TYPES:
            raise ValueError(
                f"unknown web search tool {web_search_tool!r}; expected one of "
                f"{sorted(WEB_SEARCH_TOOL_TYPES)}"
            )
        self._client = client
        self._model = verify_model
        self._reasoning_effort = reasoning_effort
        self._web_max_uses = web_max_uses
        self._web_search_tool_type = WEB_SEARCH_TOOL_TYPES[web_search_tool]

    def _web_search_tool(self) -> dict[str, Any]:
        return {
            "type": self._web_search_tool_type,
            "name": "web_search",
            "max_uses": self._web_max_uses,
        }

    @staticmethod
    def _user_content(claim: str, image_b64: str | None) -> str | list[dict[str, Any]]:
        """The bare claim, with the stream frame (if any) as an image block."""
        if image_b64 is None:
            return claim
        return [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": image_b64,
                },
            },
            {"type": "text", "text": claim},
        ]

    async def _grounded_structured(
        self, claim: str, image_b64: str | None = None
    ) -> tuple[VerdictPayload, list[Source]]:
        if AnthropicFactChecker._structured_unsupported:
            raise _FallbackNeeded("structured output with web search is latched off")
        system, user = build_anthropic_verify_messages(
            claim, _today(), self._web_max_uses, with_image=image_b64 is not None
        )
        try:
            content, stop_reason = await self._search_turn(
                system,
                self._user_content(user, image_b64),
                schema=ANTHROPIC_VERDICT_SCHEMA,
                stage="structured verify",
            )
        except _StructuredRejected as exc:
            AnthropicFactChecker._structured_unsupported = True
            logger.warning(
                "Anthropic rejected structured output with web search for %s "
                "(%s); using the text fallback for all further claims",
                self._model,
                exc,
            )
            raise _FallbackNeeded(str(exc)) from exc
        if stop_reason == "max_tokens":
            raise _FallbackNeeded("structured verify hit the output cap")
        try:
            payload, picked_urls = parse_structured_verdict(final_text(content))
        except (ValueError, ValidationError) as exc:
            raise _FallbackNeeded(f"unparseable structured verdict: {exc}") from exc
        return payload, select_sources(content, picked_urls)

    async def _grounded_fallback(
        self, claim: str, image_b64: str | None = None
    ) -> tuple[VerdictPayload, list[Source]]:
        system, user = build_anthropic_verify_fallback_messages(
            claim, _today(), self._web_max_uses, with_image=image_b64 is not None
        )
        content, _stop_reason = await self._search_turn(
            system,
            self._user_content(user, image_b64),
            schema=None,
            stage="fallback verify",
        )
        raw = final_text(content)
        if not raw.strip():
            raise VerificationError("fallback verify returned no text")
        sources = select_sources(content)
        payload = self._parse_label_explanation(raw)
        if payload is None:
            payload = await self._extract_verdict(raw)
        return payload, sources

    async def _search_turn(
        self,
        system: str,
        user_content: str | list[dict[str, Any]],
        schema: dict[str, Any] | None,
        stage: str,
    ) -> tuple[list[Any], str | None]:
        """One grounded turn, resuming ``pause_turn`` up to the bound.

        Returns every content block of the turn (across continuations) and
        the final stop reason.
        """
        content: list[Any] = []
        for _ in range(MAX_PAUSE_CONTINUATIONS + 1):
            messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]
            if content:
                messages.append({"role": "assistant", "content": list(content)})
            response = await self._create(
                system=system,
                messages=messages,
                schema=schema,
                stage=stage,
                tools=[self._web_search_tool()],
                effort=self._reasoning_effort,
                max_tokens=VERIFY_MAX_TOKENS,
            )
            content.extend(response.content)
            if response.stop_reason == "refusal":
                raise VerificationError(
                    f"{stage}: the model declined to answer (refusal)"
                )
            if response.stop_reason != "pause_turn":
                return content, response.stop_reason
        raise VerificationError(
            f"{stage}: the search turn was still paused after "
            f"{MAX_PAUSE_CONTINUATIONS} continuations"
        )

    async def _extract_verdict(self, raw: str) -> VerdictPayload:
        """Last resort: schema-constrained extraction of a free-text verdict."""
        system, user = build_verdict_extraction_messages(raw)
        response = await self._create(
            system=system,
            messages=[{"role": "user", "content": user}],
            schema=EXTRACTION_SCHEMA,
            stage="verdict extraction",
            tools=None,
            effort="none",
            max_tokens=EXTRACTION_MAX_TOKENS,
            structured_fallback=False,
        )
        try:
            return FactChecker._parse_verdict_json(final_text(response.content))
        except (ValueError, ValidationError) as exc:
            raise VerificationError(
                f"fallback chain exhausted: could not extract a verdict ({exc})"
            ) from exc

    async def _create(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None,
        stage: str,
        tools: list[dict[str, Any]] | None,
        effort: str,
        max_tokens: int,
        structured_fallback: bool = True,
    ) -> Any:
        """One Messages call with the checker's error mapping."""
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": _cached_system(system),
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
        thinking, effort_level = thinking_params(effort)
        output_config: dict[str, Any] = {}
        if thinking is not None:
            kwargs["thinking"] = thinking
        if effort_level is not None:
            output_config["effort"] = effort_level
        if schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        if output_config:
            kwargs["output_config"] = output_config
        try:
            response = await self._client.with_options(
                timeout=_request_timeout(self._verify_timeout_s)
            ).messages.create(**kwargs)
        except anthropic.AnthropicError as exc:
            raise self._translate_error(
                exc, stage, structured=schema is not None and structured_fallback
            ) from exc
        _log_usage(response, stage, self._model)
        return response

    def _translate_error(
        self, exc: anthropic.AnthropicError, stage: str, structured: bool
    ) -> BaseException:
        """Map an SDK error onto the FactChecker exception contract."""
        if isinstance(exc, anthropic.APITimeoutError):
            return asyncio.TimeoutError(f"{stage} timed out")
        if isinstance(exc, anthropic.APIStatusError):
            message = _error_message(exc)
            if is_credit_exhausted(exc):
                self._cooldown.trip(
                    CREDITS_EXHAUSTED_COOLDOWN_S, reason=CREDITS_EXHAUSTED_MESSAGE
                )
                return QuotaExceededError(
                    f"{CREDITS_EXHAUSTED_MESSAGE} ({exc.status_code} during {stage}: "
                    f"{message}; cooling down {CREDITS_EXHAUSTED_COOLDOWN_S:.0f}s)"
                )
            if isinstance(exc, anthropic.RateLimitError):
                retry_after = _retry_after_seconds(exc)
                self._cooldown.trip(
                    retry_after, reason=f"Anthropic rate limit (429): {message}"
                )
                return QuotaExceededError(
                    f"Anthropic rate limit during {stage}: {message} "
                    f"(cooling down {retry_after:.0f}s)"
                )
            if isinstance(
                exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)
            ):
                return VerificationError(
                    f"{stage} failed ({exc.status_code}): Anthropic rejected the "
                    f"credentials — {CREDENTIALS_REJECTED_HINT} [{message}]"
                )
            if structured and isinstance(exc, anthropic.BadRequestError):
                return _StructuredRejected(f"400 during {stage}: {message}")
            if isinstance(exc, anthropic.OverloadedError):
                return VerificationError(
                    f"{stage} failed (529): Anthropic is overloaded; claim skipped"
                )
            return VerificationError(f"{stage} failed ({exc.status_code}): {message}")
        if isinstance(exc, anthropic.APIConnectionError):
            return VerificationError(
                f"{stage} could not reach the Anthropic API: {exc}"
            )
        return VerificationError(f"{stage} failed: {exc}")


def reset_anthropic_capability_latches() -> None:
    """Forget learned structured-output support (after a model change)."""
    AnthropicClaimGate._json_schema_unsupported = False
    AnthropicFactChecker._structured_unsupported = False


def parse_structured_verdict(raw: str) -> tuple[VerdictPayload, list[str]]:
    """Split the strict verify JSON into the payload and the named URLs.

    Raises:
        ValueError / ValidationError: when the text is not the expected JSON.
    """
    cleaned = raw.strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"no JSON object found in: {cleaned[:120]!r}")
    data = json.loads(cleaned[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("verdict JSON is not an object")
    urls = data.pop("source_urls", None) or []
    picked = (
        [url for url in urls if isinstance(url, str)] if isinstance(urls, list) else []
    )
    return VerdictPayload.model_validate(data), picked
