"""Gemini transport for the claim gate and fact checker.

All google-genai touching lines live in this module so SDK drift stays local.

SDK reality (google-genai 2.12.0, introspected):

- The gate uses the classic ``client.aio.models.generate_content`` with
  ``response_schema=GateResult`` and temperature 0.
- Grounded calls use the Interactions API exactly as planned:
  ``await client.aio.interactions.create(model=..., input=<str>,
  tools=[{"type": "google_search"}], response_format={"type": "text",
  "mime_type": "application/json", "schema": <flat JSON schema>},
  generation_config={"temperature": 0.0})``.
- The returned ``Interaction`` exposes ``output_text`` (concatenated model
  output) and ``steps`` -> ``model_output`` steps -> content blocks ->
  ``annotations`` -> ``url_citation`` entries with ``url``/``title`` — the
  ONLY place sources ever come from (models fabricate URLs in prose).
- Interactions errors carry ``status_code`` (compat classes); classic
  ``models.generate_content`` errors carry ``code`` — both handled by
  :func:`_error_status_code`.
"""

import asyncio
import json
import logging
import re
from typing import Any

from google import genai
from google.genai import types as genai_types
from pydantic import ValidationError

from app.claim_gate import ClaimGate, GateError
from app.fact_checker import (
    DEFAULT_RETRY_AFTER_S,
    FLAT_VERDICT_SCHEMA,
    MAX_SOURCES,
    FactChecker,
    QuotaExceededError,
    VerificationError,
    _FallbackNeeded,
    _today,
)
from app.models import (
    ContradictionJudgement,
    GateClaim,
    GateResult,
    Source,
    VerdictPayload,
)
from app.prompts import (
    build_contradiction_prompt,
    build_gate_prompt,
    build_verdict_extraction_prompt,
    build_verify_fallback_prompt,
    build_verify_prompt,
)
from app.rate_limit import QuotaCooldown

logger = logging.getLogger(__name__)

_RETRY_DELAY_RE = re.compile(r"retry.?delay\D*?(\d+(?:\.\d+)?)\s*s", re.IGNORECASE)


def _error_status_code(exc: BaseException) -> int | None:
    """HTTP status from either SDK error family (``.status_code`` or ``.code``)."""
    for attr in ("status_code", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    return None


def _parse_retry_after_s(exc: BaseException) -> float:
    """Best-effort retry delay from a 429 (RetryInfo/headers), default 60 s."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            header_value = headers.get("retry-after")
        except (AttributeError, TypeError):
            header_value = None
        if header_value:
            try:
                return float(header_value)
            except ValueError:
                pass
    for blob in (str(getattr(exc, "body", "")), str(exc)):
        match = _RETRY_DELAY_RE.search(blob)
        if match:
            return float(match.group(1))
    return DEFAULT_RETRY_AFTER_S


class GeminiClaimGate(ClaimGate):
    """Gate transport on the flash-lite model with ``response_schema``."""

    def __init__(
        self,
        client: genai.Client,
        model: str,
        gate_interval_s: float = 12.0,
        gate_timeout_s: float = 15.0,
    ) -> None:
        super().__init__(gate_interval_s=gate_interval_s, gate_timeout_s=gate_timeout_s)
        self._client = client
        self._model = model

    async def _extract(self, context: str, new_text: str) -> list[GateClaim]:
        """One structured flash-lite call over ``CONTEXT`` + ``NEW TRANSCRIPT``."""
        prompt = build_gate_prompt(context, new_text)
        try:
            response = await asyncio.wait_for(
                self._client.aio.models.generate_content(
                    model=self._model,
                    contents=prompt,
                    config=genai_types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=GateResult,
                        temperature=0.0,
                    ),
                ),
                timeout=self._gate_timeout_s,
            )
        except asyncio.TimeoutError as exc:
            raise GateError(
                f"gate call timed out after {self._gate_timeout_s:.0f}s"
            ) from exc
        except Exception as exc:
            raise GateError(f"gate call failed: {exc}") from exc
        return self._parse_gate_response(response)

    async def judge_contradiction(
        self, current: str, prior: str
    ) -> ContradictionJudgement:
        """One structured judge call on the gate model (no search)."""
        try:
            response = await asyncio.wait_for(
                self._client.aio.models.generate_content(
                    model=self._model,
                    contents=build_contradiction_prompt(current, prior),
                    config=genai_types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=ContradictionJudgement,
                        temperature=0.0,
                    ),
                ),
                timeout=self._gate_timeout_s,
            )
        except asyncio.TimeoutError as exc:
            raise GateError(
                f"contradiction judge timed out after {self._gate_timeout_s:.0f}s"
            ) from exc
        except Exception as exc:
            raise GateError(f"contradiction judge failed: {exc}") from exc
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, ContradictionJudgement):
            return parsed
        raw = (getattr(response, "text", None) or "").strip()
        if not raw:
            raise GateError("contradiction judge response contained no text")
        try:
            return ContradictionJudgement.model_validate_json(raw)
        except ValidationError as exc:
            raise GateError(
                f"contradiction judgement failed schema validation: {exc}"
            ) from exc

    @staticmethod
    def _parse_gate_response(
        response: genai_types.GenerateContentResponse,
    ) -> list[GateClaim]:
        """Prefer the SDK-parsed ``GateResult``; fall back to raw-text JSON."""
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, GateResult):
            return parsed.claims
        raw = (getattr(response, "text", None) or "").strip()
        if not raw:
            raise GateError("gate response contained no text to parse")
        try:
            return GateResult.model_validate_json(raw).claims
        except ValidationError as exc:
            raise GateError(f"gate response failed schema validation: {exc}") from exc


class GeminiFactChecker(FactChecker):
    """Grounded verification via the Interactions API with a 400-fallback chain."""

    def __init__(
        self,
        client: genai.Client,
        verify_model: str,
        extraction_model: str,
        cooldown: QuotaCooldown,
        verify_timeout_s: float = 45.0,
    ) -> None:
        super().__init__(cooldown=cooldown, verify_timeout_s=verify_timeout_s)
        self._client = client
        self._verify_model = verify_model
        self._extraction_model = extraction_model

    async def _grounded_structured(
        self, claim: str
    ) -> tuple[VerdictPayload, list[Source]]:
        """Grounded search + flat structured output in a single Interactions call."""
        try:
            interaction = await self._client.aio.interactions.create(
                model=self._verify_model,
                input=build_verify_prompt(claim, _today()),
                tools=[{"type": "google_search"}],
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": FLAT_VERDICT_SCHEMA,
                },
                generation_config={"temperature": 0.0},
            )
        except Exception as exc:
            raise self._translate_api_error(
                exc, stage="grounded structured", fallback_on_400=True
            ) from exc
        sources = self._extract_citations(interaction)
        raw = (getattr(interaction, "output_text", None) or "").strip()
        try:
            payload = self._parse_verdict_json(raw)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise _FallbackNeeded(f"structured output unparseable: {exc}") from exc
        return payload, sources

    async def _grounded_fallback(
        self, claim: str
    ) -> tuple[VerdictPayload, list[Source]]:
        """Grounded plain-text call -> lenient parse -> ungrounded extraction.

        Citations still come from the grounded step regardless of which parse
        succeeds.
        """
        try:
            interaction = await self._client.aio.interactions.create(
                model=self._verify_model,
                input=build_verify_fallback_prompt(claim, _today()),
                tools=[{"type": "google_search"}],
                generation_config={"temperature": 0.0},
            )
        except Exception as exc:
            raise self._translate_api_error(
                exc, stage="grounded fallback", fallback_on_400=False
            ) from exc
        sources = self._extract_citations(interaction)
        raw = (getattr(interaction, "output_text", None) or "").strip()
        if not raw:
            raise VerificationError(
                f"grounded fallback returned no text for claim: {claim!r}"
            )
        payload = self._parse_label_explanation(raw)
        if payload is None:
            logger.warning(
                "LABEL/EXPLANATION parse failed; extracting with %s",
                self._extraction_model,
            )
            payload = await self._extract_verdict_ungrounded(raw)
        return payload, sources

    async def _extract_verdict_ungrounded(self, raw_text: str) -> VerdictPayload:
        """Last resort: one ungrounded flash-lite structured extraction pass."""
        try:
            response = await self._client.aio.models.generate_content(
                model=self._extraction_model,
                contents=build_verdict_extraction_prompt(raw_text),
                config=genai_types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=VerdictPayload,
                    temperature=0.0,
                ),
            )
        except Exception as exc:
            raise self._translate_api_error(
                exc, stage="ungrounded extraction", fallback_on_400=False
            ) from exc
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, VerdictPayload):
            return parsed
        raw = (getattr(response, "text", None) or "").strip()
        try:
            return self._parse_verdict_json(raw)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise VerificationError(
                f"verdict extraction failed at the end of the fallback chain: {exc}"
            ) from exc

    def _translate_api_error(
        self, exc: Exception, stage: str, fallback_on_400: bool
    ) -> Exception:
        """Map an SDK exception to the failure-ladder exception for ``stage``."""
        status = _error_status_code(exc)
        if status == 429:
            retry_after_s = _parse_retry_after_s(exc)
            self._cooldown.trip(retry_after_s, reason="Gemini quota exhausted (429).")
            return QuotaExceededError(
                f"Gemini quota exhausted (429) during {stage}; "
                f"cooling down for {retry_after_s:.0f}s"
            )
        if status == 400 and fallback_on_400:
            return _FallbackNeeded(f"400 InvalidArgument during {stage}: {exc}")
        return VerificationError(f"{stage} call failed: {exc}")

    @staticmethod
    def _extract_citations(interaction: Any) -> list[Source]:
        """Sources from ``url_citation`` annotations ONLY; dedupe by URL, cap 5."""
        sources: list[Source] = []
        seen_urls: set[str] = set()
        for step in getattr(interaction, "steps", None) or []:
            if getattr(step, "type", None) != "model_output":
                continue
            for block in getattr(step, "content", None) or []:
                for annotation in getattr(block, "annotations", None) or []:
                    if getattr(annotation, "type", None) != "url_citation":
                        continue
                    url = getattr(annotation, "url", None)
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    sources.append(
                        Source(url=url, title=getattr(annotation, "title", None))
                    )
                    if len(sources) >= MAX_SOURCES:
                        return sources
        return sources
