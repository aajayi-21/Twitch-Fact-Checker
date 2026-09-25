"""Shared fixtures: offline fakes for the LLM provider plus the speech engine.

The suite runs FULLY OFFLINE: no API keys, no model download, no network.
Tests exercise the real FastAPI app (routes, CORS, pipeline, WebSocket
endpoint) but its lifespan is replaced with one that installs fakes on
``app.state``:

- :class:`FakeOpenRouterClient` mimics the ``AsyncOpenAI`` surface the
  OpenRouter transport uses (``chat.completions.create`` behind
  ``with_options``) with one scriptable queue — the unit tests in
  ``tests/test_llm_openrouter.py`` drive it call by call.
- :class:`FakeLLMClient` is the same surface for whole-app tests, where the
  gate and verify loops run concurrently: it routes each call to a GATE or a
  VERIFY queue by the request's ``model`` (the test settings give the two
  stages different slugs), so a test scripts each stage independently.
- :class:`FakeTranscriber` returns scripted :class:`TranscriptSegment` lists
  so the audio pipeline runs without a real speech model.

Responses are REAL ``openai.types.chat.ChatCompletion`` objects
(``model_validate`` over a dict) and errors are REAL ``openai`` exception
instances (:func:`make_openrouter_status_error` and friends), so citation
extraction and the transport's ``except`` clauses are exercised against the
exact shapes the SDK produces.
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
from openai.types.chat import ChatCompletion

from app.config import Settings
from app.db import Database, DayCounter
from app.events import EventHub
from app.llm_provider import LLMRuntime, create_claim_gate, create_fact_checker
from app.main import create_app
from app.models import GateClaim, GateResult, TranscriptSegment
from app.rate_limit import QuotaCooldown, TokenBucket
from app.sessions import SessionRegistry
from app.stt_supervisor import SttSupervisor
from app.transcriber import SessionTextState

SAMPLE_RATE = 16000


class FakeClock:
    """Injectable monotonic clock for the streamer limiters and policy.

    Those components all take a ``now: Callable[[], float]`` precisely so a
    60-minute sliding window or a 15-minute mute can be tested without real
    sleeps — the suite never waits on wall time for time-window logic.
    """

    def __init__(self, start: float = 1_000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("a monotonic clock cannot go backwards")
        self._now += seconds


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

    async def close(self) -> None:
        """Match ``AsyncOpenAI.close`` so hot-swap close paths work."""


#: The whole-app test settings' stage models (see :func:`make_test_settings`).
TEST_GATE_MODEL = "fake-gate-model"
TEST_VERIFY_MODEL = "fake-verify-model"


class _RoutedChatCompletions:
    """``chat.completions.create`` that routes by ``model`` to a stage queue."""

    def __init__(self, client: "FakeLLMClient") -> None:
        self._client = client

    async def create(self, **kwargs: Any) -> Any:
        if kwargs.get("model") == self._client.gate_model:
            calls, queue, name = (
                self._client.gate_calls,
                self._client.gate_results,
                "gate",
            )
        else:
            calls, queue, name = (
                self._client.verify_calls,
                self._client.verify_results,
                "verify",
            )
        calls.append(kwargs)
        return await _resolve_scripted(queue, f"FakeLLMClient {name}", kwargs)


class _RoutedChat:
    def __init__(self, client: "FakeLLMClient") -> None:
        self.completions = _RoutedChatCompletions(client)


class FakeLLMClient(FakeOpenRouterClient):
    """The OpenRouter fake for whole-app tests, with one queue per stage.

    Calls for :data:`TEST_GATE_MODEL` (claim gate, contradiction judge) go
    to :attr:`gate_results` / :attr:`gate_calls`; everything else (grounded
    verify, its fallbacks) to :attr:`verify_results` / :attr:`verify_calls`.
    The gate and verify loops run concurrently, so separate queues keep a
    test's scripting independent of their interleaving.
    """

    def __init__(self, gate_model: str = TEST_GATE_MODEL) -> None:
        super().__init__()
        self.gate_model = gate_model
        self.gate_results: deque[Any] = deque()
        self.verify_results: deque[Any] = deque()
        self.gate_calls: list[dict[str, Any]] = []
        self.verify_calls: list[dict[str, Any]] = []
        self.chat = _RoutedChat(self)


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


def make_gate_response(
    claims: Sequence[tuple[str, float] | tuple[str, float, str]],
) -> ChatCompletion:
    """A strict-JSON gate completion: ``{"claims": [...]}``.

    Each claim is ``(text, score)`` (topic defaults to ``"other"``) or
    ``(text, score, topic)``.
    """
    return make_chat_completion(
        json.dumps(
            {
                "claims": [
                    {
                        "claim_text": claim[0],
                        "check_worthiness": claim[1],
                        "topic": claim[2] if len(claim) == 3 else "other",
                    }
                    for claim in claims
                ]
            }
        )
    )


def make_judgement_response(
    contradicts: bool, confidence: str = "high", explanation: str = "They clash."
) -> ChatCompletion:
    """A contradiction-judge completion (rides the gate queue)."""
    return make_chat_completion(
        json.dumps(
            {
                "contradicts": contradicts,
                "confidence": confidence,
                "explanation": explanation,
            }
        )
    )


def make_verdict_completion(
    label: str,
    explanation: str,
    citations: Sequence[tuple[str, str | None]] = (
        ("https://example.com/source", "Example Source"),
    ),
    evidence: str | None = "strong",
) -> ChatCompletion:
    """A grounded structured-verify response: flat JSON verdict + citations.

    ``evidence`` defaults to ``"strong"`` (the OpenRouter schema requires it
    and the invariants downgrade anything else); pass ``None`` to omit it.
    """
    payload: dict[str, Any] = {"label": label, "explanation": explanation}
    if evidence is not None:
        payload["evidence"] = evidence
    return make_chat_completion(json.dumps(payload), citations)


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

    Matches the ``BaseTranscriber`` surface the app uses (``load``,
    ``unload``, ``describe``, ``transcribe_window``, the supervisor hooks
    ``warm_up``/``fall_back_to_cpu`` and the ``/healthz`` snapshot
    properties); ``transcribe_window`` is sync because the pipeline runs it
    on the STT executor.
    """

    BACKEND_NAME = "fake"

    def __init__(self) -> None:
        self.segments_script: deque[list[TranscriptSegment]] = deque()
        self.calls: list[dict[str, Any]] = []
        self.unloaded = False
        self.warm_up_calls: list[float | None] = []
        self.fall_back_calls = 0
        # Fault knobs for the STT supervisor tests: the next N windows raise
        # (a poisoned accelerator), and the CPU reload can be made to fail.
        self.fail_next = 0
        self.fallback_error: Exception | None = None
        self._device = "cpu"
        self._degraded_from: str | None = None

    def load(self) -> None:
        """No model to load; present for interface parity."""

    def unload(self) -> None:
        """Interface parity with the real backends' teardown hook."""
        self.unloaded = True

    def warm_up(self, budget_s: float | None = None) -> None:
        """Records the call; the supervisor runs this at startup."""
        self.warm_up_calls.append(budget_s)

    def fall_back_to_cpu(self) -> None:
        """Records the call and mirrors the real hook's bookkeeping."""
        self.fall_back_calls += 1
        if self.fallback_error is not None:
            raise self.fallback_error
        previous = self._device
        self._device = "cpu"
        self._degraded_from = previous if previous != "cpu" else None

    def describe(self) -> str:
        suffix = (
            f" [degraded from {self._degraded_from}]" if self._degraded_from else ""
        )
        return f"fake:fake-whisper.en (device={self._device}){suffix}"

    @property
    def backend_name(self) -> str:
        return self.BACKEND_NAME

    @property
    def model_name(self) -> str:
        return "fake-whisper.en"

    @property
    def device(self) -> str:
        return self._device

    @property
    def degraded_from(self) -> str | None:
        return self._degraded_from

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
        if self.fail_next > 0:
            self.fail_next -= 1
            raise RuntimeError("vectorized gather kernel index out of bounds")
        if self.segments_script:
            return self.segments_script.popleft()
        return []


# --------------------------------------------------------------------------- #
# Settings / app / client fixtures
# --------------------------------------------------------------------------- #


def make_test_settings(**overrides: Any) -> Settings:
    """Test-tuned settings; explicit kwargs override any .env/environment."""
    base: dict[str, Any] = {
        # OpenRouter around FakeLLMClient, which routes calls to a gate or a
        # verify queue by these two DISTINCT model slugs.
        "_env_file": None,
        "llm_provider": "openrouter",
        "gate_provider": "openrouter",
        "verify_provider": "openrouter",
        "openrouter_api_key": "offline-test-key",
        "openrouter_gate_model": TEST_GATE_MODEL,
        "openrouter_verify_model": TEST_VERIFY_MODEL,
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
    settings: Settings, llm_client: FakeLLMClient, cooldown: QuotaCooldown
) -> LLMRuntime:
    """An :class:`LLMRuntime` built around a fake client (mirrors build_llm_runtime).

    The gate and checker come from the REAL provider factories, exactly as
    the app builds them. When ``settings`` is UNCONFIGURED (key empty or a
    placeholder) the runtime is the keyless None-container, exactly like the
    real factory.
    """
    if not settings.is_configured:
        return LLMRuntime(settings=settings)
    return LLMRuntime(
        settings=settings,
        # Same fake object for both stages (mirrors build_llm_runtime when
        # gate and verify resolve to the same provider).
        gate_client=llm_client,
        verify_client=llm_client,
        gate=create_claim_gate(settings, llm_client),
        checker=create_fact_checker(settings, llm_client, cooldown),
    )


def _install_fake_state(
    application: FastAPI,
    settings: Settings,
    llm_client: FakeLLMClient,
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
        app.state.llm_runtime = make_fake_llm_runtime(settings, llm_client, cooldown)
        # Live-session registry (same contract as the real lifespan). Being
        # per-app is what replaced the old autouse global-reset fixture.
        app.state.sessions = SessionRegistry(
            scope=settings.session_preempt_scope, max_sessions=settings.max_sessions
        )
        # Fan-out hub (same contract as the real lifespan). Inert until a test
        # subscribes, so it changes nothing for the suite at large.
        app.state.events = EventHub(settings.event_queue_maxsize)
        app.state.transcriber = transcriber
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt-test")
        app.state.stt_executor = executor
        # Breaker around the fake engine (same contract as the real lifespan).
        app.state.stt_supervisor = SttSupervisor(
            transcriber,
            executor,
            failure_threshold=settings.stt_failure_threshold,
            cpu_fallback=settings.stt_cpu_fallback,
        )
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
    llm_client: FakeLLMClient,
    transcriber: FakeTranscriber,
) -> Iterator[TestClient]:
    """Build the real app, install fake state, and run its (fake) lifespan."""
    application = create_app()
    _install_fake_state(application, settings, llm_client, transcriber)
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
def _reset_llm_process_latches() -> Iterator[None]:
    """Isolate the process-wide provider latches between tests.

    RULE: every new process-wide latch (class attribute surviving session
    rebuilds) needs a reset here.
    """
    from app import openrouter_catalogue
    from app.llm_local import LocalClaimGate
    from app.llm_openrouter import (
        _WARNED_FALLBACK_MODELS,
        OpenRouterClaimGate,
        _ReasoningSupport,
        _VerifyModeStats,
    )

    def reset() -> None:
        OpenRouterClaimGate._json_schema_unsupported = False
        OpenRouterClaimGate._consecutive_strict_503s = 0
        OpenRouterClaimGate._json_schema_retry_at = 0.0
        _ReasoningSupport.unsupported = False
        LocalClaimGate._json_schema_unsupported = False
        _VerifyModeStats.reset()
        _WARNED_FALLBACK_MODELS.clear()
        openrouter_catalogue.clear_model_capabilities()

    reset()
    yield
    reset()


@pytest.fixture(autouse=True)
def _offline_openrouter_catalogue(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the suite offline: the catalogue fetch always "fails".

    ``prime_openrouter_capabilities`` then logs its one warning and leaves
    the cache empty (every parameter assumed supported), which is exactly the
    pre-lookup request shape the OpenRouter tests were written against.
    Tests that need real capabilities call ``set_model_capabilities`` or pass
    ``capabilities=`` explicitly.
    """
    from app import openrouter_catalogue

    async def offline(*args: Any, **kwargs: Any) -> Any:
        raise openrouter_catalogue.ProviderUnreachable("offline test suite")

    monkeypatch.setattr(openrouter_catalogue, "fetch_openrouter_catalogue", offline)


@pytest.fixture()
def fake_llm_client() -> FakeLLMClient:
    return FakeLLMClient()


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
    fake_llm_client: FakeLLMClient,
    fake_transcriber: FakeTranscriber,
) -> Iterator[TestClient]:
    with open_test_client(app_settings, fake_llm_client, fake_transcriber) as c:
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


def pcm_tone(
    seconds: float, sample_rate: int = SAMPLE_RATE, frequency: float = 220.0
) -> bytes:
    """``seconds`` of an Int16LE mono sine tone — "speech" to :func:`energy_spans`."""
    count = int(seconds * sample_rate)
    wave = 0.3 * np.sin(2 * np.pi * frequency * np.arange(count) / sample_rate)
    return (wave * 32767).astype("<i2").tobytes()


def energy_spans(audio: np.ndarray, frame: int = 512) -> list[tuple[int, int]]:
    """Deterministic stand-in for Silero: 512-sample frames with energy.

    Returns buffer-relative ``(start, end)`` sample spans, unpadded, with an
    open span ending at ``len(audio)`` — the contract VadSegmenter expects.
    """
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for offset in range(0, len(audio), frame):
        loud = float(np.abs(audio[offset : offset + frame]).max(initial=0.0)) > 0.01
        if loud and start is None:
            start = offset
        elif not loud and start is not None:
            spans.append((start, offset))
            start = None
    if start is not None:
        spans.append((start, len(audio)))
    return spans


def pcm_ramp(seconds: float, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Int16LE mono PCM whose sample values ramp (position-identifiable)."""
    count = int(seconds * sample_rate)
    return (np.arange(count, dtype=np.int64) % 32000).astype("<i2").tobytes()
