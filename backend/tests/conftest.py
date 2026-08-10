"""Shared fixtures: offline fakes for both LLM providers plus Whisper.

The suite runs FULLY OFFLINE: no API keys, no Whisper model download, no
network. Tests exercise the real FastAPI app (routes, CORS, pipeline,
WebSocket endpoint) but its lifespan is replaced with one that installs fakes
on ``app.state``:

- :class:`FakeGenAIClient` covers both google-genai API families the Gemini
  transport uses — ``client.aio.models.generate_content`` (claim gate +
  ungrounded extraction) and ``client.aio.interactions.create`` (grounded
  verify). Responses are scripted per test; an unscripted call fails loudly.
- :class:`FakeOpenRouterClient` mimics the ``AsyncOpenAI`` surface the
  OpenRouter transport uses (``chat.completions.create`` behind
  ``with_options``), with the same scriptable-queue contract.
- :class:`FakeTranscriber` returns scripted :class:`TranscriptSegment` lists
  so the audio pipeline runs without ctranslate2 ever seeing real audio.

Responses are built as REAL SDK objects — ``google.genai.interactions.
Interaction`` and ``openai.types.chat.ChatCompletion`` (``model_validate``
over a dict) — so citation extraction is tested against the exact object
shapes each SDK produces. OpenRouter error paths use REAL ``openai``
exception instances (:func:`make_openrouter_rate_limit_error` and friends)
so the transport's ``except`` clauses are exercised for real.

The app-level fixtures (``client`` / ``open_test_client``) run the GEMINI
provider (``llm_provider="gemini"`` in :func:`make_test_settings`) around
:class:`FakeGenAIClient`; the OpenRouter transport is covered by the unit
tests in ``tests/test_llm_openrouter.py``.
"""

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import inspect
import json
import tempfile
from collections import deque
from collections.abc import AsyncIterator, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from typing import Any
from uuid import uuid4

import httpx
import numpy as np
import openai
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.genai.interactions import Interaction
from openai.types.chat import ChatCompletion

from app import ws as ws_module
from app.config import Settings
from app.db import Database, DayCounter
from app.llm_gemini import GeminiClaimGate, GeminiFactChecker
from app.llm_provider import LLMRuntime
from app.main import create_app
from app.models import GateClaim, GateResult, TranscriptSegment
from app.rate_limit import QuotaCooldown, TokenBucket
from app.transcriber import SessionTextState

SAMPLE_RATE = 16000


async def _resolve_scripted(queue: deque[Any], name: str, call: dict[str, Any]) -> Any:
    """Shared scripted-queue semantics for every fake LLM client.

    - an object            -> returned as-is
    - a ``BaseException``  -> raised
    - a callable           -> called with the call kwargs; awaited if async
      (used for timeout tests via ``asyncio.sleep``)

    An UNSCRIPTED call raises ``AssertionError`` so tests fail loudly instead
    of silently consuming a default.
    """
    if not queue:
        raise AssertionError(
            f"unscripted {name} call (model={call.get('model')!r}); "
            "script a response in the test"
        )
    item = queue.popleft()
    if isinstance(item, BaseException):
        raise item
    if callable(item):
        result = item(**call)
        if inspect.isawaitable(result):
            return await result
        return result
    return item


# --------------------------------------------------------------------------- #
# Fake google-genai client
# --------------------------------------------------------------------------- #


class FakeInteractionsError(Exception):
    """Mimics the Interactions compat error family (carries ``status_code``)."""

    def __init__(
        self,
        status_code: int,
        message: str = "fake interactions error",
        response: Any = None,
        body: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response = response
        self.body = body


class FakeClassicAPIError(Exception):
    """Mimics ``google.genai.errors.APIError`` (carries ``code``)."""

    def __init__(self, code: int, message: str = "fake classic api error") -> None:
        super().__init__(message)
        self.code = code


class _FakeAsyncModels:
    """Async facade over the scripted ``generate_content`` queue."""

    def __init__(self, client: "FakeGenAIClient") -> None:
        self._client = client

    async def generate_content(
        self, *, model: str, contents: str, config: Any = None
    ) -> Any:
        call = {"model": model, "contents": contents, "config": config}
        self._client.generate_calls.append(call)
        return await self._client._resolve(
            self._client.generate_results, "generate_content", call
        )


class _FakeAsyncInteractions:
    """Async facade over the scripted ``interactions.create`` queue."""

    def __init__(self, client: "FakeGenAIClient") -> None:
        self._client = client

    async def create(
        self,
        *,
        model: str,
        input: str,
        tools: Any = None,
        response_format: Any = None,
        generation_config: Any = None,
    ) -> Any:
        call = {
            "model": model,
            "input": input,
            "tools": tools,
            "response_format": response_format,
            "generation_config": generation_config,
        }
        self._client.interaction_calls.append(call)
        return await self._client._resolve(
            self._client.interaction_results, "interactions.create", call
        )


class _FakeAio:
    def __init__(self, client: "FakeGenAIClient") -> None:
        self.models = _FakeAsyncModels(client)
        self.interactions = _FakeAsyncInteractions(client)

    async def aclose(self) -> None:
        """Match ``genai.Client.aio.aclose`` so hot-swap close paths work."""


class FakeGenAIClient:
    """Scriptable stand-in for ``genai.Client``.

    Script by appending to :attr:`generate_results` / :attr:`interaction_results`
    (see :func:`_resolve_scripted` for the queue-item semantics). Every call
    is recorded in :attr:`generate_calls` / :attr:`interaction_calls`.
    """

    def __init__(self) -> None:
        self.generate_results: deque[Any] = deque()
        self.interaction_results: deque[Any] = deque()
        self.generate_calls: list[dict[str, Any]] = []
        self.interaction_calls: list[dict[str, Any]] = []
        self.aio = _FakeAio(self)

    async def _resolve(self, queue: deque[Any], name: str, call: dict[str, Any]) -> Any:
        return await _resolve_scripted(queue, f"FakeGenAIClient.{name}", call)


# --------------------------------------------------------------------------- #
# Response builders
# --------------------------------------------------------------------------- #


class FakeGenerateContentResponse:
    """Just the two attributes the app reads: ``parsed`` and ``text``."""

    def __init__(self, parsed: Any = None, text: str | None = None) -> None:
        self.parsed = parsed
        self.text = text


def make_gate_response(
    claims: Sequence[tuple[str, float] | tuple[str, float, str]],
) -> FakeGenerateContentResponse:
    """A ``generate_content`` response whose ``parsed`` is a real GateResult.

    Each claim is ``(text, score)`` (topic defaults to ``"other"``) or
    ``(text, score, topic)``.
    """
    result = GateResult(
        claims=[
            GateClaim(
                claim_text=claim[0],
                check_worthiness=claim[1],
                topic=claim[2] if len(claim) == 3 else "other",  # type: ignore[arg-type]
            )
            for claim in claims
        ]
    )
    return FakeGenerateContentResponse(parsed=result, text=result.model_dump_json())


def make_interaction(
    output_text: str,
    citations: Sequence[tuple[str, str | None]] = (),
) -> Interaction:
    """A REAL SDK ``Interaction`` with one model_output step + url citations."""
    annotations = [
        {
            "type": "url_citation",
            "url": url,
            "title": title,
            "start_index": 0,
            "end_index": 1,
        }
        for url, title in citations
    ]
    return Interaction.model_validate(
        {
            "id": "fake-interaction",
            "model": "fake-verify-model",
            "status": "completed",
            "steps": [
                {
                    "type": "model_output",
                    "content": [
                        {
                            "type": "text",
                            "text": output_text,
                            "annotations": annotations,
                        }
                    ],
                }
            ],
        }
    )


def make_verdict_interaction(
    label: str,
    explanation: str,
    citations: Sequence[tuple[str, str | None]] = (
        ("https://example.com/source", "Example Source"),
    ),
) -> Interaction:
    """A grounded structured-verify response: flat JSON verdict + citations."""
    return make_interaction(
        json.dumps({"label": label, "explanation": explanation}), citations
    )


# --------------------------------------------------------------------------- #
# Fake OpenRouter (AsyncOpenAI) client
# --------------------------------------------------------------------------- #


class _FakeChatCompletions:
    """Async facade over the scripted ``chat.completions.create`` queue."""

    def __init__(self, client: "FakeOpenRouterClient") -> None:
        self._client = client

    async def create(self, **kwargs: Any) -> Any:
        self._client.completion_calls.append(kwargs)
        return await _resolve_scripted(
            self._client.completion_results,
            "FakeOpenRouterClient.chat.completions.create",
            kwargs,
        )


class _FakeChat:
    def __init__(self, client: "FakeOpenRouterClient") -> None:
        self.completions = _FakeChatCompletions(client)


class FakeOpenRouterClient:
    """Scriptable stand-in for ``openai.AsyncOpenAI`` pointed at OpenRouter.

    Mirrors the exact call surface ``app.llm_openrouter`` uses:
    ``client.with_options(timeout=...).chat.completions.create(**kwargs)``.
    ``with_options`` records its options and returns ``self`` so the scripted
    queue and the recorded calls stay on one object. Script by appending to
    :attr:`completion_results` (see :func:`_resolve_scripted`); every create
    call's kwargs land in :attr:`completion_calls`.
    """

    def __init__(self) -> None:
        self.completion_results: deque[Any] = deque()
        self.completion_calls: list[dict[str, Any]] = []
        self.with_options_calls: list[dict[str, Any]] = []
        self.chat = _FakeChat(self)

    def with_options(self, **options: Any) -> "FakeOpenRouterClient":
        self.with_options_calls.append(options)
        return self


def make_chat_completion(
    content: str,
    citations: Sequence[tuple[str, str | None]] = (),
) -> ChatCompletion:
    """A REAL SDK ``ChatCompletion`` with ``url_citation`` annotations.

    Annotations carry OpenRouter's extra ``content`` excerpt field on top of
    the OpenAI four-field shape, exactly like the live API, so tests prove
    the excerpt is deliberately NOT surfaced in the wire ``Source`` model.
    """
    annotations = [
        {
            "type": "url_citation",
            "url_citation": {
                "url": url,
                "title": title or "",
                "content": f"excerpt for {url}",  # OpenRouter extension field
                "start_index": 0,
                "end_index": 1,
            },
        }
        for url, title in citations
    ]
    return ChatCompletion.model_validate(
        {
            "id": "fake-completion",
            "object": "chat.completion",
            "created": 0,
            "model": "fake-openrouter-model",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "annotations": annotations,
                    },
                }
            ],
        }
    )


def make_verdict_completion(
    label: str,
    explanation: str,
    citations: Sequence[tuple[str, str | None]] = (
        ("https://example.com/source", "Example Source"),
    ),
) -> ChatCompletion:
    """A grounded structured-verify response: flat JSON verdict + citations."""
    return make_chat_completion(
        json.dumps({"label": label, "explanation": explanation}), citations
    )


_OPENROUTER_REQUEST = httpx.Request(
    "POST", "https://openrouter.ai/api/v1/chat/completions"
)

# Never performs I/O — used ONLY for ``_make_status_error_from_response`` so
# fake errors are constructed by the SDK itself (exception-class choice AND
# the ``{"error": ...}`` envelope unwrap of ``.body``) and cannot drift from
# what a live OpenRouter call raises.
_OPENROUTER_ERROR_FACTORY = openai.AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1", api_key="offline-test-key"
)


def make_openrouter_status_error(
    status_code: int,
    message: str = "fake openrouter error",
    headers: dict[str, str] | None = None,
) -> openai.APIStatusError:
    """A REAL ``openai`` status error built the way the SDK builds them.

    The ``httpx.Response`` carries OpenRouter's full wire envelope
    ``{"error": {code, message, metadata}}``; the SDK's own
    ``_make_status_error_from_response`` then picks the exception class
    (``RateLimitError`` for 429, ``BadRequestError`` for 400, plain
    ``APIStatusError`` for 402, …) and unwraps the envelope, so ``exc.body``
    is the INNER dict exactly as on a live call.
    """
    envelope = {"error": {"code": status_code, "message": message, "metadata": {}}}
    response = httpx.Response(
        status_code,
        headers=headers or {},
        request=_OPENROUTER_REQUEST,
        json=envelope,
    )
    return _OPENROUTER_ERROR_FACTORY._make_status_error_from_response(response)


def make_openrouter_rate_limit_error(
    retry_after: str | None = None,
) -> openai.RateLimitError:
    """A REAL ``RateLimitError`` (429), optionally with a Retry-After header."""
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    error = make_openrouter_status_error(429, "You are being rate limited", headers)
    assert isinstance(error, openai.RateLimitError)
    return error


def make_openrouter_timeout_error() -> openai.APITimeoutError:
    """A REAL ``APITimeoutError`` as raised on an SDK request timeout."""
    return openai.APITimeoutError(request=_OPENROUTER_REQUEST)


# --------------------------------------------------------------------------- #
# Fake transcriber
# --------------------------------------------------------------------------- #


class FakeTranscriber:
    """Scripted transcriber: pops one segment list per window, ``[]`` after.

    Matches the ``Transcriber`` surface the pipeline uses (``load``,
    ``transcribe_window``); ``transcribe_window`` is sync because the
    pipeline runs it on the STT executor.
    """

    def __init__(self) -> None:
        self.segments_script: deque[list[TranscriptSegment]] = deque()
        self.calls: list[dict[str, Any]] = []

    def load(self) -> None:
        """No model to load; present for interface parity."""

    def transcribe_window(
        self,
        audio: np.ndarray,
        window_start_s: float,
        last_emitted_end: float,
        text_state: SessionTextState,
    ) -> list[TranscriptSegment]:
        self.calls.append(
            {
                "samples": len(audio),
                "window_start_s": window_start_s,
                "last_emitted_end": last_emitted_end,
                "text_state": text_state,
            }
        )
        if self.segments_script:
            return self.segments_script.popleft()
        return []


# --------------------------------------------------------------------------- #
# Settings / app / client fixtures
# --------------------------------------------------------------------------- #


def make_test_settings(**overrides: Any) -> Settings:
    """Test-tuned settings; explicit kwargs override any .env/environment."""
    base: dict[str, Any] = {
        # The app-level fakes are Gemini-shaped (FakeGenAIClient), so the
        # provider factory must build the Gemini gate/checker around them.
        "llm_provider": "gemini",
        "gemini_api_key": "offline-test-key",
        "gemini_gate_model": "fake-gate-model",
        "gemini_verify_model": "fake-verify-model",
        "whisper_model": "fake-whisper.en",
        "stt_window_s": 1.0,
        "stt_hop_s": 0.5,
        "max_audio_buffer_s": 30.0,
        "audio_high_watermark_s": 12.0,
        "audio_low_watermark_s": 8.0,
        # Huge interval: in WS tests the gate only runs on the unconditional
        # final flush pass, which keeps LLM-call counts deterministic.
        "gate_interval_s": 999.0,
        "gate_timeout_s": 5.0,
        # Effectively unthrottled so tests never sleep on the bucket.
        "verify_rpm": 6000.0,
        "verify_timeout_s": 5.0,
        "send_transcripts": True,
        "debug_endpoints": True,
        # Per-call unique temp path: safe for callers that never open a
        # client (the file is only created by Database.open); the fake
        # lifespan unlinks it (plus WAL sidecars) on teardown.
        "db_path": str(
            Path(tempfile.gettempdir()) / f"fact-checker-test-{uuid4().hex}.db"
        ),
        # Dead port: an instant connection refusal on every machine, so
        # ollama-touching paths (embeddings, reachability probes) degrade
        # deterministically and the suite stays genuinely offline even on
        # dev boxes running a real Ollama.
        "ollama_base_url": "http://127.0.0.1:1/v1",
    }
    base.update(overrides)
    return Settings(**base)


def make_fake_llm_runtime(
    settings: Settings, genai_client: FakeGenAIClient, cooldown: QuotaCooldown
) -> LLMRuntime:
    """An :class:`LLMRuntime` built from fakes (mirrors build_llm_runtime).

    When ``settings`` is UNCONFIGURED (active provider key empty/placeholder)
    the runtime is the keyless None-container, exactly like the real factory.
    """
    if not settings.is_configured:
        return LLMRuntime(settings=settings)
    return LLMRuntime(
        settings=settings,
        # Same fake object for both stages (mirrors build_llm_runtime when
        # gate and verify resolve to the same provider).
        gate_client=genai_client,
        verify_client=genai_client,
        gate=GeminiClaimGate(
            client=genai_client,
            model=settings.gemini_gate_model,
            gate_interval_s=settings.gate_interval_s,
            gate_timeout_s=settings.gate_timeout_s,
        ),
        checker=GeminiFactChecker(
            client=genai_client,
            verify_model=settings.gemini_verify_model,
            extraction_model=settings.gemini_gate_model,
            cooldown=cooldown,
            verify_timeout_s=settings.verify_timeout_s,
        ),
    )


def _install_fake_state(
    application: FastAPI,
    settings: Settings,
    genai_client: FakeGenAIClient,
    transcriber: FakeTranscriber,
) -> None:
    """Swap the real lifespan for one that builds ``app.state`` from fakes."""

    @asynccontextmanager
    async def fake_lifespan(app: FastAPI) -> AsyncIterator[None]:
        cooldown = QuotaCooldown()
        app.state.settings = settings
        app.state.quota_cooldown = cooldown
        app.state.verify_bucket = TokenBucket(
            rate_per_min=settings.verify_rpm, burst=10
        )
        # The hot-swappable slot ws.py/debug.py fetch the provider stack
        # through (same contract as the real lifespan).
        app.state.llm_runtime = make_fake_llm_runtime(settings, genai_client, cooldown)
        app.state.transcriber = transcriber
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt-test")
        app.state.stt_executor = executor
        # Real Database on the per-test temp path (same contract as the real
        # lifespan); torn down together with its WAL sidecars.
        db = Database(settings.db_path)
        await db.open()
        app.state.db = db
        app.state.verify_counter = DayCounter(initial=await db.count_checks_today())
        try:
            yield
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
            await db.close()
            for suffix in ("", "-wal", "-shm"):
                Path(f"{settings.db_path}{suffix}").unlink(missing_ok=True)

    application.router.lifespan_context = fake_lifespan


@contextmanager
def open_test_client(
    settings: Settings,
    genai_client: FakeGenAIClient,
    transcriber: FakeTranscriber,
) -> Iterator[TestClient]:
    """Build the real app, install fake state, and run its (fake) lifespan."""
    application = create_app()
    _install_fake_state(application, settings, genai_client, transcriber)
    # The app only trusts localhost Hosts (TrustedHostMiddleware), so the
    # TestClient default "testserver" would 400. The explicit default host
    # header also covers websocket_connect, whose URL is hard-coded to
    # ws://testserver inside starlette's TestClient.
    with TestClient(
        application,
        base_url="http://127.0.0.1",
        headers={"host": "127.0.0.1"},
    ) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _reset_current_pipeline() -> Iterator[None]:
    """Isolate the module-global live-session slot between tests."""
    ws_module.current_pipeline = None
    yield
    ws_module.current_pipeline = None


@pytest.fixture(autouse=True)
def _reset_llm_process_latches() -> Iterator[None]:
    """Isolate the process-wide provider latches between tests.

    RULE: every new process-wide latch (class attribute surviving session
    rebuilds) needs a reset here.
    """
    from app.llm_local import LocalClaimGate
    from app.llm_openrouter import OpenRouterClaimGate, _ReasoningSupport

    def reset() -> None:
        OpenRouterClaimGate._json_schema_unsupported = False
        OpenRouterClaimGate._consecutive_strict_503s = 0
        OpenRouterClaimGate._json_schema_retry_at = 0.0
        _ReasoningSupport.unsupported = False
        LocalClaimGate._json_schema_unsupported = False

    reset()
    yield
    reset()


@pytest.fixture()
def fake_genai_client() -> FakeGenAIClient:
    return FakeGenAIClient()


@pytest.fixture()
def fake_openrouter_client() -> FakeOpenRouterClient:
    return FakeOpenRouterClient()


# The local (Ollama) transport talks through the identical AsyncOpenAI
# surface, so the OpenRouter fake covers it verbatim; the alias keeps test
# intent readable.
FakeLocalClient = FakeOpenRouterClient


@pytest.fixture()
def fake_local_client() -> FakeLocalClient:
    return FakeLocalClient()


def make_judgement_response(
    contradicts: bool, confidence: str = "high", explanation: str = "They clash."
) -> "FakeGenerateContentResponse":
    """A scripted Gemini judge result (rides the generate_results queue)."""
    from app.models import ContradictionJudgement

    return FakeGenerateContentResponse(
        parsed=ContradictionJudgement(
            contradicts=contradicts,
            confidence=confidence,  # type: ignore[arg-type]
            explanation=explanation,
        )
    )


class FakeEmbedder:
    """Scripted stand-in for app.embeddings.OllamaEmbedder.

    ``results`` items: a list of vectors (one per input text), or an
    exception instance to raise. Unscripted call -> AssertionError.
    """

    def __init__(self) -> None:
        self.results: deque[Any] = deque()
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[Any]:
        self.calls.append(list(texts))
        if not self.results:
            raise AssertionError("unscripted FakeEmbedder.embed call")
        item = self.results.popleft()
        if isinstance(item, BaseException):
            raise item
        return item


def make_frame_message(
    image_b64: str = "aGVsbG8=", captured_at_ms: int = 1_700_000_000_000
) -> dict[str, Any]:
    """A valid client->server video-frame message (wire contract §Phase 6)."""
    return {
        "type": "frame",
        "image_b64": image_b64,
        "captured_at_ms": captured_at_ms,
    }


@pytest.fixture()
def fake_transcriber() -> FakeTranscriber:
    return FakeTranscriber()


@pytest.fixture()
def app_settings() -> Settings:
    return make_test_settings()


@pytest.fixture()
def client(
    app_settings: Settings,
    fake_genai_client: FakeGenAIClient,
    fake_transcriber: FakeTranscriber,
) -> Iterator[TestClient]:
    with open_test_client(app_settings, fake_genai_client, fake_transcriber) as c:
        yield c


# --------------------------------------------------------------------------- #
# Wire-protocol helpers
# --------------------------------------------------------------------------- #


def make_hello(**overrides: Any) -> dict[str, Any]:
    """A valid §2.1 hello frame; override fields to make it invalid."""
    hello: dict[str, Any] = {
        "type": "hello",
        "version": 1,
        "format": "pcm_s16le",
        "sample_rate": 16000,
        "channels": 1,
        "sensitivity": "medium",
        "send_transcripts": True,
    }
    hello.update(overrides)
    return hello


def pcm_silence(seconds: float, sample_rate: int = SAMPLE_RATE) -> bytes:
    """``seconds`` of Int16LE mono silence."""
    return b"\x00\x00" * int(seconds * sample_rate)


def pcm_ramp(seconds: float, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Int16LE mono PCM whose sample values ramp (position-identifiable)."""
    count = int(seconds * sample_rate)
    return (np.arange(count, dtype=np.int64) % 32000).astype("<i2").tobytes()
