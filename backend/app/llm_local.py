"""Local (Ollama / any OpenAI-compatible server) transport for the claim gate.

Uses the OpenAI SDK pointed at a local base URL — Ollama's officially
documented OpenAI-compatible path, which also covers LM Studio, vLLM, and
llama.cpp's ``llama-server``. GATE ONLY: there is deliberately no local
``FactChecker`` — verification needs web-search grounding, which a local
server cannot provide, and the no-citations⇒UNVERIFIED invariant would
downgrade every local verdict.

The failure ladder is deliberately much simpler than OpenRouter's: strict
``json_schema`` first (Ollama ≥0.5 maps it to grammar-constrained decoding —
a HARD constraint, better than any hosted fallback machinery), and on
structural rejection (400/404/422) a process-wide latch drops to
``json_object`` mode for the rest of the process. No plugins, no reasoning,
no provider-routing extra_body — none of that exists locally.
"""

import logging
from typing import Any

import httpx
import openai
from openai import AsyncOpenAI

from app.claim_gate import (
    CONTRADICTION_JSON_SCHEMA,
    GATE_JSON_SCHEMA,
    ClaimGate,
    GateError,
    parse_contradiction_judgement,
    parse_gate_result,
)
from app.models import ContradictionJudgement, GateClaim
from app.prompts import build_contradiction_prompt, build_gate_prompt

logger = logging.getLogger(__name__)

# Ollama ignores the API key but the SDK requires a non-empty one.
OLLAMA_DEFAULT_API_KEY = "ollama"

LOCAL_CONNECT_TIMEOUT_S = 2.0
LOCAL_READ_TIMEOUT_S = 60.0
LOCAL_GATE_MAX_TOKENS = 1200

# Statuses that prove the server's OpenAI-compat layer lacks json_schema
# support (older Ollama, some llama.cpp builds) — latch json_object mode.
LOCAL_STRUCTURAL_UNSUPPORTED = frozenset({400, 404, 422})


def create_local_client(base_url: str) -> AsyncOpenAI:
    """One process-wide AsyncOpenAI client pointed at the local server.

    Short connect timeout: the server is on localhost, so anything beyond a
    couple of seconds means it is not running. ``max_retries=0`` — the app
    owns its retry policy (a missed gate batch is acceptable, a storm is not).
    """
    return AsyncOpenAI(
        base_url=base_url,
        api_key=OLLAMA_DEFAULT_API_KEY,
        timeout=httpx.Timeout(LOCAL_READ_TIMEOUT_S, connect=LOCAL_CONNECT_TIMEOUT_S),
        max_retries=0,
    )


class LocalClaimGate(ClaimGate):
    """Gate transport for a local OpenAI-compatible server (Ollama et al.)."""

    # Process-wide latch: json_schema support is a property of the local
    # server build, not of any session. Reset by the conftest autouse
    # fixture alongside the OpenRouter latches.
    _json_schema_unsupported: bool = False

    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        base_url: str,
        gate_interval_s: float = 12.0,
        gate_timeout_s: float = 15.0,
    ) -> None:
        super().__init__(gate_interval_s=gate_interval_s, gate_timeout_s=gate_timeout_s)
        self._client = client
        self._model = model
        self._base_url = base_url

    async def _extract(self, context: str, new_text: str) -> list[GateClaim]:
        """Strict json_schema gate call, degrading to json_object on latch."""
        raw = await self._complete_with_ladder(
            build_gate_prompt(context, new_text),
            strict_schema=GATE_JSON_SCHEMA,
            what="gate",
        )
        return parse_gate_result(raw)

    async def judge_contradiction(
        self, current: str, prior: str
    ) -> ContradictionJudgement:
        """Structured judge call; shares the gate's ladder and latch."""
        raw = await self._complete_with_ladder(
            build_contradiction_prompt(current, prior),
            strict_schema=CONTRADICTION_JSON_SCHEMA,
            what="contradiction judge",
        )
        return parse_contradiction_judgement(raw)

    async def _complete_with_ladder(
        self, prompt: str, strict_schema: dict[str, Any], what: str
    ) -> str:
        """Strict attempt -> (on structural rejection) latch + json_object."""
        if not LocalClaimGate._json_schema_unsupported:
            try:
                return await self._complete(
                    prompt,
                    response_format={
                        "type": "json_schema",
                        "json_schema": strict_schema,
                    },
                    what=what,
                )
            except openai.APIStatusError as exc:
                if exc.status_code not in LOCAL_STRUCTURAL_UNSUPPORTED:
                    raise self._translate_error(exc, what) from exc
                LocalClaimGate._json_schema_unsupported = True
                logger.warning(
                    "local server rejected json_schema mode (%s); using "
                    "json_object for the rest of the process",
                    exc.status_code,
                )
            except Exception as exc:
                raise self._translate_error(exc, what) from exc
        try:
            return await self._complete(
                prompt, response_format={"type": "json_object"}, what=what
            )
        except Exception as exc:
            raise self._translate_error(exc, what) from exc

    async def _complete(
        self, prompt: str, response_format: dict[str, Any], what: str
    ) -> str:
        """One completion; returns the stripped message text."""
        response = await self._client.with_options(
            timeout=httpx.Timeout(self._gate_timeout_s, connect=LOCAL_CONNECT_TIMEOUT_S)
        ).chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=LOCAL_GATE_MAX_TOKENS,
            response_format=response_format,  # type: ignore[arg-type]
        )
        if not response.choices:
            raise GateError(f"{what} response contained no choices")
        return (response.choices[0].message.content or "").strip()

    def _translate_error(self, exc: Exception, what: str) -> GateError:
        """Map SDK/local failures to GateError (never leak SDK exceptions)."""
        if isinstance(exc, GateError):
            return exc
        if isinstance(exc, openai.APITimeoutError):
            return GateError(
                f"{what} call timed out after {self._gate_timeout_s:.0f}s "
                "(cold local models can exceed the gate timeout on their "
                "first call — consider raising GATE_TIMEOUT_S)"
            )
        if isinstance(exc, openai.APIConnectionError):
            return GateError(
                f"cannot reach the local LLM server at {self._base_url} — "
                "is `ollama serve` running?"
            )
        if isinstance(exc, openai.APIStatusError):
            return GateError(f"{what} call failed ({exc.status_code}): {exc}")
        return GateError(f"{what} call failed: {exc}")
