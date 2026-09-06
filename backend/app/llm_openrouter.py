"""OpenRouter transport for the claim gate and fact checker.

Uses the OpenAI SDK pointed at the OpenRouter base URL (OpenRouter's
officially documented integration path). Everything OpenRouter-specific —
the ``plugins``/``provider``/``reasoning`` extra-body params, the strict
``json_schema`` response format, the ``url_citation`` annotations, and the
402/429 error shapes — lives in this module so SDK drift stays local.

Transport notes (verified July 2026 against openrouter.ai docs):

- Requests are built from the model's published capabilities
  (:mod:`app.openrouter_catalogue`): ``temperature`` and ``reasoning`` are
  only sent when the catalogue lists them, and strict ``json_schema`` mode is
  only attempted when it lists ``structured_outputs`` (a model with plain
  ``response_format`` support gets ONE grounded ``json_object`` call
  instead). Production lesson behind this: OpenAI's GPT-5.x endpoints accept
  no ``temperature``; sending it under ``require_parameters`` rejected every
  strict call, so every verdict silently took the 2-3-call fallback chain.
  When the catalogue is unreachable every parameter is assumed supported and
  the latches below take over, exactly as before.
- ``provider.require_parameters: true`` routes only to providers that support
  EVERY parameter in the request — including ``reasoning``, not just
  ``response_format`` — so unsupported combinations fail with 400/404/422/503
  instead of silently degrading. A strict-path failure is first retried once
  WITHOUT ``reasoning``: success means reasoning was the disqualifier, so it
  is dropped process-wide while strict ``json_schema`` mode is preserved.
- Verify prompts are split into a ``system`` message (instructions) and a
  ``user`` message holding the bare claim: the ``web`` plugin searches on the
  user message BEFORE the model runs, so instruction text there pollutes the
  query. The model rates ``evidence`` (strong/partial/none) alongside the
  label, and :meth:`app.fact_checker.FactChecker._enforce_invariants`
  downgrades anything but ``strong`` — a search always returns *something*,
  so the citation count alone cannot tell confirmation from adjacency.
- 400/404/422 are structural proof the strict path cannot work and latch the
  gate's ``json_object`` fallback permanently. 503 ("no available model
  provider meets your routing requirements") is AMBIGUOUS: OpenRouter also
  uses it for transient provider overload (documented retryable, carries
  ``Retry-After``), so a single 503 only falls back per-call; a streak
  pauses strict mode for a bounded window, after which it is re-probed.
- The ``web`` plugin (Exa engine) injects search results into the prompt and
  returns citations as OpenAI-style ``message.annotations`` — the ONLY place
  sources ever come from. Its DEFAULT ``search_prompt`` instructs the model
  to cite with inline markdown links, which fights strict JSON output, so a
  custom prompt (:data:`WEB_SEARCH_PROMPT`) tells the model to use the
  results as evidence and never cite inline.
- Web search bills OpenRouter credits ($0.007/request on Exa; the model
  provider's own price with ``OPENROUTER_WEB_ENGINE=native``) EVEN on
  ``:free`` model variants; a 402 therefore usually means "top up", not a
  code bug — hence the loud message and long cooldown.
- ``openai.RateLimitError`` (429) carries a standard ``Retry-After`` header;
  402 has no SDK subclass and surfaces as ``openai.APIStatusError``.
- The SDK's own retries are disabled (``max_retries=0``): a stale fact-check
  is worthless, and the app owns its cooldown/retry ladder.
"""

import asyncio
import json
import logging
import time
from collections import Counter
from typing import Any

import httpx
import openai
from openai import AsyncOpenAI
from pydantic import ValidationError

from app.claim_gate import (
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
from app.openrouter_catalogue import ModelCapabilities, lookup_model_capabilities
from app.prompts import (
    build_contradiction_prompt,
    build_gate_prompt,
    build_verdict_extraction_messages,
    build_verify_fallback_messages,
    build_verify_messages,
)
from app.rate_limit import QuotaCooldown

logger = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# App attribution (optional but recommended by OpenRouter); set once on the
# client instead of per-call extra_headers.
ATTRIBUTION_HEADERS: dict[str, str] = {
    "HTTP-Referer": "https://github.com/local/twitch-fact-checker",
    "X-OpenRouter-Title": "Twitch Fact-Checker",
}

CONNECT_TIMEOUT_S = 3.0
DEFAULT_READ_TIMEOUT_S = 30.0

# Explicit output caps: free endpoints often cap max completion tokens lower
# than the headline, and a JSON verdict needs ~1-2K tokens at most.
GATE_MAX_TOKENS = 1200
VERIFY_MAX_TOKENS = 1500
EXTRACTION_MAX_TOKENS = 800

# 402 = out of credits / negative balance. Unlike a 429 this will not clear
# on its own, so the cooldown is LONG to stop a doomed request loop.
CREDITS_EXHAUSTED_COOLDOWN_S = 15 * 60.0
CREDITS_EXHAUSTED_MESSAGE = (
    "OpenRouter credits exhausted or negative balance — web search costs "
    "credits even on :free models; top up at openrouter.ai"
)

# Statuses that PROVE the json_schema/params routing path is structurally
# unsupported (400 bad param, 404 no endpoint, 422 validation): the gate
# latches json_object mode permanently on these.
STRUCTURAL_UNSUPPORTED_STATUS_CODES = frozenset({400, 404, 422})
# 503 ("no available model provider meets your routing requirements") is
# ambiguous: transient provider overload OR structural require_parameters
# exhaustion. The gate handles it per-call with a consecutive-failure
# counter and a time-bounded latch instead of a permanent one.
ROUTING_UNAVAILABLE_STATUS_CODE = 503
# The union is what the verify path maps to a per-call fallback, and what
# triggers the one-shot no-reasoning retry on both strict paths.
UNSUPPORTED_PARAMS_STATUS_CODES = STRUCTURAL_UNSUPPORTED_STATUS_CODES | {
    ROUTING_UNAVAILABLE_STATUS_CODE
}

# After this many CONSECUTIVE strict-mode 503s the gate pauses strict mode
# for JSON_SCHEMA_RETRY_WINDOW_S, then re-probes it (full recovery when the
# 503s were transient; one cheap failed probe per window when structural).
CONSECUTIVE_503_LATCH_THRESHOLD = 3
JSON_SCHEMA_RETRY_WINDOW_S = 900.0

# Custom web-plugin search prompt: the default one instructs inline markdown
# -link citations, which fights strict JSON output. Citations are read from
# message.annotations instead, so the model must never cite inline.
WEB_SEARCH_PROMPT = (
    "The following web search results are evidence for the fact-check. "
    "Base your verdict strictly on them. Judge how directly each result "
    "addresses the exact claim; results that are merely on the same topic "
    "are not evidence. Do NOT cite them inline: no markdown links, no URLs, "
    "and no source names in your response — citations are collected "
    "separately from metadata."
)

#: Web-search engines the verify plugin accepts (``OPENROUTER_WEB_ENGINE``);
#: ``auto`` omits the key so OpenRouter picks native-if-available, else Exa.
WEB_ENGINES: frozenset[str] = frozenset({"exa", "native", "auto"})

# The strict gate schema lives in app.claim_gate (GATE_JSON_SCHEMA, imported
# above): it is provider-neutral and shared with the local (Ollama) gate.

# The verify schema: the shared flat label+explanation schema plus the
# OpenRouter-only ``evidence`` rating, all required, strict-mode
# additionalProperties=False.
OPENROUTER_VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **FLAT_VERDICT_SCHEMA["properties"],
        "evidence": {"type": "string", "enum": list(EVIDENCE_LEVELS)},
    },
    "required": ["label", "explanation", "evidence"],
    "additionalProperties": False,
}
VERDICT_JSON_SCHEMA: dict[str, Any] = {
    "name": "verdict",
    "strict": True,
    "schema": OPENROUTER_VERDICT_SCHEMA,
}

#: How each verification was produced: ``strict`` (json_schema + web),
#: ``json_object`` (plain response_format + web, for models without
#: structured outputs) or ``fallback`` (the LABEL:/EXPLANATION: text chain).
VERIFY_MODES: tuple[str, ...] = ("strict", "json_object", "fallback")


class _VerifyModeStats:
    """Process-wide per-model tally of verify modes (``/healthz``).

    Exists because the production fallback storm (every verdict on one model
    took the text chain for weeks) was invisible outside the database.
    """

    counts: dict[str, Counter[str]] = {}

    @classmethod
    def record(cls, model: str, mode: str) -> None:
        cls.counts.setdefault(model, Counter())[mode] += 1

    @classmethod
    def snapshot(cls) -> dict[str, dict[str, int]]:
        return {model: dict(counter) for model, counter in cls.counts.items()}

    @classmethod
    def reset(cls) -> None:
        cls.counts.clear()


def verify_mode_snapshot() -> dict[str, dict[str, int]]:
    """``{model: {"strict": n, "json_object": n, "fallback": n}}`` so far."""
    return _VerifyModeStats.snapshot()


#: Models whose first strict-mode rejection has already been explained.
_WARNED_FALLBACK_MODELS: set[str] = set()


def create_openrouter_client(api_key: str) -> AsyncOpenAI:
    """One process-wide AsyncOpenAI client pointed at OpenRouter.

    Built once in the app lifespan and closed on shutdown with
    ``await client.close()``. Per-call timeouts are applied via
    ``client.with_options`` (the SDK default of 10 minutes is far too long).
    """
    return AsyncOpenAI(
        base_url=OPENROUTER_BASE_URL,
        api_key=api_key,
        timeout=httpx.Timeout(DEFAULT_READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
        max_retries=0,
        default_headers=dict(ATTRIBUTION_HEADERS),
    )


def _openrouter_error_message(exc: openai.APIStatusError) -> str:
    """The OpenRouter error message from ``exc.body`` (falls back to str).

    The openai SDK unwraps the wire ``{"error": {...}}`` envelope before
    constructing the exception (``_make_status_error`` does
    ``body.get("error", body)``), so on a live call ``exc.body`` is already
    the inner ``{code, message, metadata}`` dict — read ``message`` from it
    directly. The nested ``body["error"]["message"]`` lookup is kept only as
    a defensive secondary against SDK/body-shape drift.
    """
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        message = body.get("message")
        if message:
            return str(message)
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
    return str(exc)


class _ReasoningSupport:
    """Process-wide latch: True once a strict call succeeded only after
    dropping ``reasoning``.

    Under ``require_parameters`` the ``reasoning`` field joins the routing
    filter, so a model whose providers lack reasoning support fails strict
    calls with the same 400/404/422/503 statuses as a missing json_schema
    path. Once the no-reasoning retry proves reasoning is the disqualifier,
    every subsequent call (gate AND verify) omits it up front. Support is a
    property of the model/provider routing, not of any session, so this
    mirrors the gate's process-wide ``_json_schema_unsupported`` pattern.
    """

    unsupported: bool = False


def _reasoning_body(
    effort: str | None, capabilities: ModelCapabilities | None = None
) -> dict[str, Any] | None:
    """The ``reasoning`` extra-body field, or ``None`` when it must be omitted
    (effort disabled via config, reasoning latched unsupported, or the
    catalogue says the model's endpoints do not accept it)."""
    if not effort or _ReasoningSupport.unsupported:
        return None
    if capabilities is not None and not capabilities.supports("reasoning"):
        return None
    return {"effort": effort}


def _mark_reasoning_unsupported(status_code: int, model: str) -> None:
    """Latch ``reasoning`` off process-wide after a successful no-reasoning
    retry proved it was the routing disqualifier."""
    if not _ReasoningSupport.unsupported:
        _ReasoningSupport.unsupported = True
        logger.warning(
            "reasoning effort rejected under require_parameters (%s) for %s; "
            "omitting reasoning from all further OpenRouter calls",
            status_code,
            model,
        )


def _retry_after_seconds(exc: openai.RateLimitError) -> float:
    """``Retry-After`` header from a 429 response, default 60 s."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is not None:
        header_value = headers.get("Retry-After")
        if header_value:
            try:
                return float(header_value)
            except ValueError:
                pass
    return DEFAULT_RETRY_AFTER_S


def _gate_api_error(exc: openai.APIStatusError) -> GateError:
    """Map an OpenRouter status error to a :class:`GateError` (batch dropped)."""
    message = _openrouter_error_message(exc)
    if exc.status_code == 402:
        return GateError(
            f"gate call failed (402): {CREDITS_EXHAUSTED_MESSAGE} [{message}]"
        )
    return GateError(f"gate call failed ({exc.status_code}): {message}")


class OpenRouterClaimGate(ClaimGate):
    """Gate transport: strict json_schema call with a json_object fallback."""

    # Class-level latches: structured-output support is a property of the
    # account/model routing, not of the session, so all three survive the
    # per-session gate rebuilds.
    #
    # - _json_schema_unsupported: permanent json_object mode, set only by
    #   the structural 400/404/422 statuses.
    # - _consecutive_strict_503s: streak of strict-mode 503s (ambiguous:
    #   transient overload or structural exhaustion); reset by any
    #   strict-mode success.
    # - _json_schema_retry_at: time.monotonic() deadline of the TIME-BOUNDED
    #   latch started after CONSECUTIVE_503_LATCH_THRESHOLD strict-mode
    #   503s; strict mode is re-probed once the deadline passes.
    _json_schema_unsupported: bool = False
    _consecutive_strict_503s: int = 0
    _json_schema_retry_at: float = 0.0

    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        gate_interval_s: float = 12.0,
        gate_timeout_s: float = 15.0,
        reasoning_effort: str | None = "low",
        capabilities: ModelCapabilities | None = None,
    ) -> None:
        super().__init__(gate_interval_s=gate_interval_s, gate_timeout_s=gate_timeout_s)
        self._client = client
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._capabilities = capabilities

    @property
    def _caps(self) -> ModelCapabilities:
        """Explicit capabilities, else the process cache (read per call so a
        prime after construction still applies)."""
        return self._capabilities or lookup_model_capabilities(self._model)

    @classmethod
    def _latch_allows_strict(cls) -> bool:
        """The runtime latches' verdict on the strict json_schema path."""
        if cls._json_schema_unsupported:
            return False
        return time.monotonic() >= cls._json_schema_retry_at

    def _strict_mode_available(self) -> bool:
        """True when the strict json_schema path should be attempted.

        The catalogue is consulted first: a model whose endpoints publish no
        ``structured_outputs`` goes straight to json_object mode without
        spending a failing call or touching the latches.
        """
        if not self._caps.supports("structured_outputs"):
            return False
        return self._latch_allows_strict()

    @classmethod
    def _record_strict_failure(cls, exc: openai.APIStatusError) -> None:
        """Latch bookkeeping for an unsupported-params strict-mode failure."""
        message = _openrouter_error_message(exc)
        if exc.status_code in STRUCTURAL_UNSUPPORTED_STATUS_CODES:
            cls._json_schema_unsupported = True
            logger.warning(
                "gate json_schema mode unsupported (%s: %s); switching to "
                "json_object mode for the rest of the process",
                exc.status_code,
                message,
            )
            return
        # 503: transient until proven otherwise. The streak deliberately
        # survives a latch window so a failed re-probe re-latches after ONE
        # cheap call instead of three.
        cls._consecutive_strict_503s += 1
        if cls._consecutive_strict_503s >= CONSECUTIVE_503_LATCH_THRESHOLD:
            cls._json_schema_retry_at = time.monotonic() + JSON_SCHEMA_RETRY_WINDOW_S
            logger.warning(
                "gate json_schema mode hit %d consecutive 503s (%s); pausing "
                "strict mode for %.0fs before re-probing",
                cls._consecutive_strict_503s,
                message,
                JSON_SCHEMA_RETRY_WINDOW_S,
            )
        else:
            logger.warning(
                "gate json_schema call got a 503 (%s); using json_object for "
                "this call only (%d/%d consecutive)",
                message,
                cls._consecutive_strict_503s,
                CONSECUTIVE_503_LATCH_THRESHOLD,
            )

    async def _extract(self, context: str, new_text: str) -> list[GateClaim]:
        """Strict-schema gate call with per-status degradation to json_object.

        - 400/404/422 prove the strict path can never work for this
          model/account -> permanent process-wide json_object latch.
        - 503 is ambiguous (transient provider overload OR structural
          routing exhaustion), so one 503 only falls back to json_object for
          THIS call; a streak of CONSECUTIVE_503_LATCH_THRESHOLD pauses
          strict mode for JSON_SCHEMA_RETRY_WINDOW_S, after which it is
          re-probed. Any strict-mode success resets the streak.
        """
        prompt = build_gate_prompt(context, new_text)
        if self._strict_mode_available():
            try:
                raw = await self._complete_strict(prompt)
            except GateError:
                raise
            except openai.APITimeoutError as exc:
                raise GateError(
                    f"gate call timed out after {self._gate_timeout_s:.0f}s"
                ) from exc
            except openai.APIStatusError as exc:
                if exc.status_code not in UNSUPPORTED_PARAMS_STATUS_CODES:
                    raise _gate_api_error(exc) from exc
                self._record_strict_failure(exc)
            except Exception as exc:
                raise GateError(f"gate call failed: {exc}") from exc
            else:
                OpenRouterClaimGate._consecutive_strict_503s = 0
                return self._parse_gate_json(raw)
        # json_object mode: the schema is already embedded in the gate prompt
        # ("Return JSON matching the schema: ..."), and the parse is robust
        # against fences/prose. Malformed JSON still means GateError.
        try:
            raw = await self._complete(
                prompt,
                response_format={"type": "json_object"},
                require_parameters=False,
            )
        except openai.APITimeoutError as exc:
            raise GateError(
                f"gate call timed out after {self._gate_timeout_s:.0f}s"
            ) from exc
        except openai.APIStatusError as exc:
            raise _gate_api_error(exc) from exc
        except Exception as exc:
            raise GateError(f"gate call failed: {exc}") from exc
        return self._parse_gate_json(raw)

    async def _complete_strict(self, prompt: str) -> str:
        """One strict json_schema completion, retried once without ``reasoning``.

        Under ``require_parameters`` the ``reasoning`` field participates in
        provider routing, so it can 400/404/422/503 a model whose providers
        support json_schema but not reasoning. Retrying without it
        disambiguates: success latches reasoning off process-wide and keeps
        strict mode; failure propagates to the caller's latch logic.
        """
        response_format: dict[str, Any] = {
            "type": "json_schema",
            "json_schema": GATE_JSON_SCHEMA,
        }
        try:
            return await self._complete(
                prompt, response_format=response_format, require_parameters=True
            )
        except openai.APIStatusError as exc:
            if (
                exc.status_code not in UNSUPPORTED_PARAMS_STATUS_CODES
                or _reasoning_body(self._reasoning_effort, self._caps) is None
            ):
                raise
            raw = await self._complete(
                prompt,
                response_format=response_format,
                require_parameters=True,
                include_reasoning=False,
            )
            _mark_reasoning_unsupported(exc.status_code, self._model)
            return raw

    async def _complete(
        self,
        prompt: str,
        response_format: dict[str, Any],
        require_parameters: bool,
        include_reasoning: bool = True,
    ) -> str:
        """One gate completion; returns the stripped message text.

        ``temperature`` and ``reasoning`` are sent only when the model's
        catalogue entry lists them (unknown = send, as before).
        """
        caps = self._caps
        extra_body: dict[str, Any] = {"plugins": [{"id": "response-healing"}]}
        reasoning = (
            _reasoning_body(self._reasoning_effort, caps) if include_reasoning else None
        )
        if reasoning is not None:
            extra_body["reasoning"] = reasoning
        if require_parameters:
            extra_body["provider"] = {"require_parameters": True}
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": GATE_MAX_TOKENS,
            "response_format": response_format,
            "extra_body": extra_body,
        }
        if caps.supports("temperature"):
            kwargs["temperature"] = 0.0
        response = await self._client.with_options(
            timeout=httpx.Timeout(self._gate_timeout_s, connect=CONNECT_TIMEOUT_S)
        ).chat.completions.create(**kwargs)
        if not response.choices:
            raise GateError("gate response contained no choices")
        return (response.choices[0].message.content or "").strip()

    @staticmethod
    def _parse_gate_json(raw: str) -> list[GateClaim]:
        """Robust parse into ``GateResult.claims`` (shared, provider-neutral)."""
        return parse_gate_result(raw)

    async def judge_contradiction(
        self, current: str, prior: str
    ) -> ContradictionJudgement:
        """One ``json_object`` judge call on the gate model.

        Deliberately does NOT touch the strict-latch machinery: three flat
        fields need no schema enforcement, and keeping the latch semantics
        purely gate-owned avoids cross-talk between the two call kinds.
        """
        prompt = build_contradiction_prompt(current, prior)
        try:
            raw = await self._complete(
                prompt,
                response_format={"type": "json_object"},
                require_parameters=False,
            )
        except GateError:
            raise
        except openai.APITimeoutError as exc:
            raise GateError(
                f"contradiction judge timed out after {self._gate_timeout_s:.0f}s"
            ) from exc
        except openai.APIStatusError as exc:
            raise _gate_api_error(exc) from exc
        except Exception as exc:
            raise GateError(f"contradiction judge failed: {exc}") from exc
        return parse_contradiction_judgement(raw)


class OpenRouterFactChecker(FactChecker):
    """Grounded verification via the ``web`` plugin with a fallback chain.

    Chain: strict json_schema + web plugin (or, for a model whose catalogue
    entry has ``response_format`` but no ``structured_outputs``, ONE
    grounded ``json_object`` call) -> same web-plugin call without
    ``response_format`` demanding the three-line
    ``LABEL:``/``EVIDENCE:``/``EXPLANATION:`` format -> lenient parse -> one
    last-resort ``json_object`` extraction pass over the raw text (no web
    plugin).
    """

    def __init__(
        self,
        client: AsyncOpenAI,
        verify_model: str,
        cooldown: QuotaCooldown,
        web_max_results: int = 5,
        verify_timeout_s: float = 45.0,
        reasoning_effort: str | None = "low",
        web_engine: str = "exa",
        capabilities: ModelCapabilities | None = None,
    ) -> None:
        super().__init__(cooldown=cooldown, verify_timeout_s=verify_timeout_s)
        if web_engine not in WEB_ENGINES:
            raise ValueError(
                f"web_engine must be one of {sorted(WEB_ENGINES)}, got {web_engine!r}"
            )
        self._client = client
        self._verify_model = verify_model
        self._web_max_results = web_max_results
        self._reasoning_effort = reasoning_effort
        self._web_engine = web_engine
        self._capabilities = capabilities

    @property
    def _caps(self) -> ModelCapabilities:
        """Explicit capabilities, else the process cache (read per call)."""
        return self._capabilities or lookup_model_capabilities(self._verify_model)

    def _web_plugin(self) -> dict[str, Any]:
        """The web-search plugin config (custom prompt: never cite inline).

        ``auto`` omits ``engine`` so OpenRouter uses the model provider's
        native search when it has one and Exa otherwise.
        """
        plugin: dict[str, Any] = {
            "id": "web",
            "max_results": self._web_max_results,
            "search_prompt": WEB_SEARCH_PROMPT,
        }
        if self._web_engine != "auto":
            plugin["engine"] = self._web_engine
        return plugin

    async def _grounded_structured(
        self, claim: str, image_b64: str | None = None
    ) -> tuple[VerdictPayload, list[Source]]:
        """Web search + structured output in a single call.

        Mode follows the catalogue: ``structured_outputs`` -> strict
        json_schema; plain ``response_format`` -> one json_object call with
        response healing; neither -> straight to the text fallback chain.
        """
        caps = self._caps
        system, user = build_verify_messages(
            claim, _today(), with_image=image_b64 is not None
        )
        if caps.supports("structured_outputs"):
            mode = "strict"
            try:
                response = await self._strict_grounded_completion(
                    system, user, image_b64=image_b64
                )
            except Exception as exc:
                raise self._translate_api_error(
                    exc, stage="grounded structured", fallback_on_unsupported=True
                ) from exc
        elif caps.supports("response_format"):
            mode = "json_object"
            try:
                response = await self._create_completion(
                    system,
                    user,
                    response_format={"type": "json_object"},
                    extra_body=self._extra_body_with_reasoning(
                        {"plugins": [self._web_plugin(), {"id": "response-healing"}]}
                    ),
                    max_tokens=VERIFY_MAX_TOKENS,
                    image_b64=image_b64,
                )
            except Exception as exc:
                raise self._translate_api_error(
                    exc, stage="grounded json_object", fallback_on_unsupported=True
                ) from exc
        else:
            raise _FallbackNeeded(
                f"{self._verify_model} publishes no response_format support"
            )
        if not response.choices:
            raise _FallbackNeeded(f"grounded {mode} response had no choices")
        message = response.choices[0].message
        sources = self._extract_citations(message)
        raw = (message.content or "").strip()
        try:
            payload = self._parse_verdict_json(raw)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise _FallbackNeeded(f"{mode} output unparseable: {exc}") from exc
        _VerifyModeStats.record(self._verify_model, mode)
        return payload, sources

    async def _strict_grounded_completion(
        self, system: str, user: str, image_b64: str | None = None
    ) -> Any:
        """One strict verify completion, retried once without ``reasoning``.

        Same disambiguation as the gate's :meth:`OpenRouterClaimGate.
        _complete_strict`: under ``require_parameters`` the ``reasoning``
        field joins the routing filter, so a no-reasoning retry separates
        "providers lack reasoning" (retry succeeds -> latch reasoning off,
        keep strict mode) from "providers lack json_schema" (retry fails ->
        the caller maps it to the per-call fallback chain).
        """
        response_format: dict[str, Any] = {
            "type": "json_schema",
            "json_schema": VERDICT_JSON_SCHEMA,
        }

        def build_extra_body(include_reasoning: bool) -> dict[str, Any]:
            extra_body: dict[str, Any] = {
                "provider": {"require_parameters": True},
                "plugins": [self._web_plugin()],
            }
            reasoning = (
                _reasoning_body(self._reasoning_effort, self._caps)
                if include_reasoning
                else None
            )
            if reasoning is not None:
                extra_body["reasoning"] = reasoning
            return extra_body

        first_extra_body = build_extra_body(include_reasoning=True)
        try:
            return await self._create_completion(
                system,
                user,
                response_format=response_format,
                extra_body=first_extra_body,
                max_tokens=VERIFY_MAX_TOKENS,
                image_b64=image_b64,
            )
        except openai.APIStatusError as exc:
            if (
                exc.status_code not in UNSUPPORTED_PARAMS_STATUS_CODES
                or "reasoning" not in first_extra_body
            ):
                raise
            response = await self._create_completion(
                system,
                user,
                response_format=response_format,
                extra_body=build_extra_body(include_reasoning=False),
                max_tokens=VERIFY_MAX_TOKENS,
                image_b64=image_b64,
            )
            _mark_reasoning_unsupported(exc.status_code, self._verify_model)
            return response

    async def _grounded_fallback(
        self, claim: str, image_b64: str | None = None
    ) -> tuple[VerdictPayload, list[Source]]:
        """Web-plugin plain-text call -> lenient parse -> json_object extraction.

        Citations still come from the grounded step regardless of which parse
        succeeds.
        """
        _VerifyModeStats.record(self._verify_model, "fallback")
        system, user = build_verify_fallback_messages(
            claim, _today(), with_image=image_b64 is not None
        )
        try:
            response = await self._create_completion(
                system,
                user,
                response_format=None,
                extra_body=self._extra_body_with_reasoning(
                    {"plugins": [self._web_plugin()]}
                ),
                max_tokens=VERIFY_MAX_TOKENS,
                image_b64=image_b64,
            )
        except Exception as exc:
            raise self._translate_api_error(
                exc, stage="grounded fallback", fallback_on_unsupported=False
            ) from exc
        if not response.choices:
            raise VerificationError(
                f"grounded fallback returned no choices for claim: {claim!r}"
            )
        message = response.choices[0].message
        sources = self._extract_citations(message)
        raw = (message.content or "").strip()
        if not raw:
            raise VerificationError(
                f"grounded fallback returned no text for claim: {claim!r}"
            )
        payload = self._parse_label_explanation(raw)
        if payload is None:
            logger.warning(
                "LABEL/EXPLANATION parse failed; extracting with a json_object pass"
            )
            payload = await self._extract_verdict_json_object(raw)
        return payload, sources

    def _extra_body_with_reasoning(self, extra_body: dict[str, Any]) -> dict[str, Any]:
        """Add the ``reasoning`` effort cap unless disabled or latched off.

        These non-strict calls never set ``require_parameters``, so providers
        that lack reasoning simply ignore the field — no retry needed here.
        """
        reasoning = _reasoning_body(self._reasoning_effort, self._caps)
        if reasoning is not None:
            extra_body["reasoning"] = reasoning
        return extra_body

    async def _extract_verdict_json_object(self, raw_text: str) -> VerdictPayload:
        """Last resort: one ``json_object`` extraction pass (no web plugin)."""
        system, user = build_verdict_extraction_messages(raw_text)
        try:
            response = await self._create_completion(
                system,
                user,
                response_format={"type": "json_object"},
                extra_body=self._extra_body_with_reasoning(
                    {"plugins": [{"id": "response-healing"}]}
                ),
                max_tokens=EXTRACTION_MAX_TOKENS,
            )
        except Exception as exc:
            raise self._translate_api_error(
                exc, stage="json_object extraction", fallback_on_unsupported=False
            ) from exc
        if not response.choices:
            raise VerificationError("json_object extraction returned no choices")
        raw = (response.choices[0].message.content or "").strip()
        try:
            return self._parse_verdict_json(raw)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise VerificationError(
                f"verdict extraction failed at the end of the fallback chain: {exc}"
            ) from exc

    async def _create_completion(
        self,
        system: str,
        user: str,
        response_format: dict[str, Any] | None,
        extra_body: dict[str, Any],
        max_tokens: int,
        image_b64: str | None = None,
    ) -> Any:
        """One verify-model completion with the per-call SDK timeout applied.

        ``system`` carries the instructions; ``user`` is the bare claim (the
        web plugin's search query). With ``image_b64`` the user content
        becomes OpenAI-style parts (text + ``image_url`` data URI).
        ``temperature`` is sent only when the catalogue lists it.
        """
        content: Any = user
        if image_b64 is not None:
            content = [
                {"type": "text", "text": user},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                },
            ]
        kwargs: dict[str, Any] = {
            "model": self._verify_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "max_tokens": max_tokens,
            "extra_body": extra_body,
        }
        if self._caps.supports("temperature"):
            kwargs["temperature"] = 0.0
        if response_format is not None:
            kwargs["response_format"] = response_format
        return await self._client.with_options(
            timeout=httpx.Timeout(self._verify_timeout_s, connect=CONNECT_TIMEOUT_S)
        ).chat.completions.create(**kwargs)

    def _translate_api_error(
        self, exc: Exception, stage: str, fallback_on_unsupported: bool
    ) -> BaseException:
        """Map an SDK exception to the failure-ladder exception for ``stage``."""
        if isinstance(exc, openai.APITimeoutError):
            # Existing timeout semantics: check()'s ladder catches
            # asyncio.TimeoutError and retries once.
            return asyncio.TimeoutError(
                f"{stage} call timed out after {self._verify_timeout_s:.0f}s"
            )
        if isinstance(exc, openai.RateLimitError):
            retry_after_s = _retry_after_seconds(exc)
            self._cooldown.trip(
                retry_after_s, reason="OpenRouter rate limit hit (429)."
            )
            return QuotaExceededError(
                f"OpenRouter rate limit hit (429) during {stage}; "
                f"cooling down for {retry_after_s:.0f}s"
            )
        if isinstance(exc, openai.APIStatusError):
            message = _openrouter_error_message(exc)
            if exc.status_code == 402:
                self._cooldown.trip(
                    CREDITS_EXHAUSTED_COOLDOWN_S, reason=CREDITS_EXHAUSTED_MESSAGE
                )
                return QuotaExceededError(
                    f"{CREDITS_EXHAUSTED_MESSAGE} (402 during {stage}: {message}; "
                    f"fact-checks paused for "
                    f"{CREDITS_EXHAUSTED_COOLDOWN_S / 60:.0f} min)"
                )
            if (
                fallback_on_unsupported
                and exc.status_code in UNSUPPORTED_PARAMS_STATUS_CODES
            ):
                self._warn_first_fallback(exc.status_code, message)
                return _FallbackNeeded(f"{exc.status_code} during {stage}: {message}")
            return VerificationError(
                f"{stage} call failed ({exc.status_code}): {message}"
            )
        return VerificationError(f"{stage} call failed: {exc}")

    def _warn_first_fallback(self, status_code: int, message: str) -> None:
        """Explain the FIRST structured-mode rejection per model, loudly.

        Every later one is a per-claim WARNING from the checker ladder; this
        is the one that says what to look at.
        """
        if self._verify_model in _WARNED_FALLBACK_MODELS:
            return
        _WARNED_FALLBACK_MODELS.add(self._verify_model)
        logger.warning(
            "verify structured mode rejected for %s (%s: %s); using the text "
            "fallback chain for this model — check its supported_parameters at "
            "https://openrouter.ai/api/v1/models/%s/endpoints",
            self._verify_model,
            status_code,
            message,
            self._verify_model,
        )

    @staticmethod
    def _extract_citations(message: Any) -> list[Source]:
        """Sources from ``message.annotations`` ONLY; dedupe by URL, cap 5.

        Reads the OpenAI-typed ``url_citation`` annotations. OpenRouter adds
        an untyped ``content`` excerpt field, but the wire ``Source`` model is
        frozen (url + title), so the excerpt is deliberately not surfaced.
        """
        sources: list[Source] = []
        seen_urls: set[str] = set()
        for annotation in getattr(message, "annotations", None) or []:
            if getattr(annotation, "type", None) != "url_citation":
                continue
            url_citation = getattr(annotation, "url_citation", None)
            if url_citation is None:
                continue
            url = getattr(url_citation, "url", None)
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            sources.append(Source(url=url, title=getattr(url_citation, "title", None)))
            if len(sources) >= MAX_SOURCES:
                break
        return sources


def reset_openrouter_capability_latches() -> None:
    """Forget everything learned about the PREVIOUS model's capabilities.

    ``OpenRouterClaimGate``'s strict-mode latches and ``_ReasoningSupport``
    are process-wide on purpose: structured-output and reasoning support are
    properties of the model/account routing, not of a session, so they must
    survive the per-session gate rebuilds.

    They must NOT survive a model change. A latch set because model A's
    providers lacked ``json_schema`` would silently downgrade a freshly
    chosen model B to ``json_object`` mode forever — with no error and no way
    for the user to tell. ``POST /setup/stages`` calls this whenever a slug
    actually changes.
    """
    OpenRouterClaimGate._json_schema_unsupported = False
    OpenRouterClaimGate._consecutive_strict_503s = 0
    OpenRouterClaimGate._json_schema_retry_at = 0.0
    _ReasoningSupport.unsupported = False
    _VerifyModeStats.reset()
    _WARNED_FALLBACK_MODELS.clear()
    logger.info("reset OpenRouter capability latches after a model change")
