"""Per-connection session pipeline: five tasks around bounded queues (§3.1).

Task graph — one ``asyncio.TaskGroup`` (Python 3.11: an unexpected failure in
any task cancels the whole session; every exception is logged with traceback
and surfaced as an error frame or close 1011):

    _recv_loop   ── ws.receive(); binary -> ring.append() (returns seconds
                    dropped); text -> config/stop. Never blocks on STT.
    _stt_loop    ── when ring.pending_seconds >= window (4.0 s): read the
                    4.0 s window, consume only the 3.5 s hop (0.5 s overlap)
                    -> run_in_executor(stt_executor) -> filtered segments ->
                    gate.add_transcript() + optional transcript frames.
    _gate_loop   ── 1 s tick; gate.should_run() -> gate.run() -> sensitivity
                    threshold -> topic filter (disabled topic -> status
                    stage:"topic_skipped" frame, claim dropped) -> dedupe ->
                    verify_queue.put (maxsize 3, drop OLDEST if full) ->
                    status stage:"verifying" frame.
    _verify_loop ── verify_queue -> cooldown check -> bucket.acquire() ->
                    checker.check() -> verdict frame. Per-item try/except:
                    an LLM failure is a non-fatal error frame + continue —
                    the session never dies because one check failed.
    _send_loop   ── outbound asyncio.Queue[dict] (maxsize 100) ->
                    ws.send_json() (the single writer).

Graceful stop (client ``{"type":"stop"}``) runs a second, *sequential* phase
after the TaskGroup has exited: drain the outbound queue, flush remaining
ring audio through STT, run one final gate pass, verify whatever is left,
close 1000. Direct sends are safe in that phase because the send loop has
already exited — writers stay temporally exclusive.
"""

import asyncio
import json
import logging
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, NamedTuple

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ValidationError

from uuid import uuid4

from app.claim_gate import ClaimGate, GateError
from app.config import SENSITIVITY_THRESHOLDS, Settings
from app.contradiction import ContradictionDetector
from app.db import Database, DayCounter
from app.fact_checker import FactChecker, QuotaExceededError, VerificationError
from app.logging_setup import session_id_var
from app.models import (
    TOPICS,
    ClientConfig,
    ClientFrameMessage,
    ClientHello,
    ErrorFrame,
    GateClaim,
    Sensitivity,
    StatusFrame,
    TranscriptFrame,
    Verdict,
    VerdictFrame,
    resolve_enabled_topics,
)
from app.rate_limit import QuotaCooldown, TokenBucket
from app.transcriber import AudioRingBuffer, SessionTextState, Transcriber

logger = logging.getLogger(__name__)


def _format_duration(seconds: float) -> str:
    """Compact human duration for log lines (e.g. "2m14s", "1h03m")."""
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m{total % 60:02d}s"
    return f"{total // 3600}h{(total % 3600) // 60:02d}m"


VERIFY_QUEUE_MAXSIZE = 3
OUTBOUND_QUEUE_MAXSIZE = 100
GATE_TICK_S = 1.0
QUEUE_POLL_S = 0.25
OVERLOAD_FRAME_INTERVAL_S = 10.0
FLUSH_MIN_AUDIO_S = 0.5
CONTRADICTION_QUEUE_MAXSIZE = 8

# --------------------------------------------------------------------------- #
# Vision (report §5): frame ring + visual-cue gating
# --------------------------------------------------------------------------- #

# Base64 length cap (~1.5 MB decoded JPEG) — anything bigger is not a 480p
# stream frame and is dropped with a warning.
MAX_FRAME_B64_LEN = 2_000_000
# A frame older than this cannot be what the claim was pointing at.
FRAME_MAX_AGE_S = 20.0
MAX_STORED_FRAMES = 3

# Deictic/visual cues: a claim containing one plausibly references something
# ON SCREEN, so the temporally-closest captured frame is attached to its
# verification. Known limitation (documented in the report): the gate
# rewrites claims into self-contained sentences, so cues may not always
# survive into claim_text — vision under-triggers rather than over-triggers.
VISUAL_CUE_PHRASES: tuple[str, ...] = (
    "look at this",
    "look at that",
    "looking at this",
    "as you can see",
    "you can see",
    "on screen",
    "on the screen",
    "the screen says",
    "it says here",
    "says right here",
    "right here",
    "this chart",
    "this graph",
    "this article",
    "this headline",
    "this screenshot",
    "this image",
    "this picture",
    "this clip",
    "reading this",
    "shown here",
)
_VISUAL_CUE_RE = re.compile(
    "|".join(re.escape(phrase) for phrase in VISUAL_CUE_PHRASES), re.IGNORECASE
)


def has_visual_cue(text: str) -> bool:
    """True when the text plausibly references something on screen."""
    return _VISUAL_CUE_RE.search(text) is not None


class StoredFrame(NamedTuple):
    """One captured frame in the in-memory ring (NEVER persisted)."""

    image_b64: str
    captured_at_ms: int
    received_at: float  # time.monotonic()

# How a frame leaves the pipeline: queued (live phase) or sent directly
# (flush phase, after the send loop has exited).
FrameEmitter = Callable[[BaseModel], Awaitable[None]]


class SessionPipeline:
    """One live /ws/audio session: audio in, transcript/status/verdict out.

    The claim gate and fact checker are session-scoped (fresh transcript
    buffer and dedupe memory per connection); the transcriber, STT executor,
    token bucket, and quota cooldown are process-wide and shared.
    """

    def __init__(
        self,
        websocket: WebSocket,
        hello: ClientHello,
        settings: Settings,
        transcriber: Transcriber,
        stt_executor: ThreadPoolExecutor,
        claim_gate: ClaimGate,
        fact_checker: FactChecker,
        verify_bucket: TokenBucket,
        quota_cooldown: QuotaCooldown,
        db: Database | None = None,
        verify_counter: DayCounter | None = None,
        contradiction_detector: ContradictionDetector | None = None,
    ) -> None:
        self._websocket = websocket
        self._settings = settings
        self._transcriber = transcriber
        self._stt_executor = stt_executor
        self._gate = claim_gate
        self._checker = fact_checker
        self._bucket = verify_bucket
        self._cooldown = quota_cooldown
        # Analytics (None in direct unit-test construction): every db call is
        # fire-and-forget inside app.db — a persistence failure never touches
        # the session.
        self._db = db
        self._verify_counter = verify_counter
        self._session_id = uuid4().hex
        self._platform = hello.platform
        self._channel = hello.channel
        self._stream_title = hello.stream_title
        self._speech_seconds = 0.0
        self._audio_seconds = 0.0
        self._verify_calls = 0
        # Funnel tally for the end-of-session log line.
        self._claim_outcomes: dict[str, int] = {}
        self._sensitivity: Sensitivity = hello.sensitivity
        # Seeded from hello, updated by config frames; "other" always enabled.
        self._enabled_topics: frozenset[str] = resolve_enabled_topics(
            hello.enabled_topics
        )
        # The server-side SEND_TRANSCRIPTS setting is the master switch; the
        # hello flag opts a client in beneath it.
        self._send_transcripts = settings.send_transcripts and hello.send_transcripts
        self._ring = AudioRingBuffer(
            sample_rate=hello.sample_rate,
            max_seconds=settings.max_audio_buffer_s,
            high_wm_s=settings.audio_high_watermark_s,
            low_wm_s=settings.audio_low_watermark_s,
        )
        self._verify_queue: asyncio.Queue[GateClaim] = asyncio.Queue(
            maxsize=VERIFY_QUEUE_MAXSIZE
        )
        # Same-session contradiction detection (None = feature disabled,
        # e.g. direct unit-test construction).
        self._detector = contradiction_detector
        self._contradiction_queue: asyncio.Queue[GateClaim] = asyncio.Queue(
            maxsize=CONTRADICTION_QUEUE_MAXSIZE
        )
        # Captured stream frames, in-memory ONLY (privacy rule: frames are
        # never persisted anywhere).
        self._frames: deque[StoredFrame] = deque(maxlen=MAX_STORED_FRAMES)
        self._outbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=OUTBOUND_QUEUE_MAXSIZE
        )
        self._stop_requested = asyncio.Event()
        self._graceful_stop = False
        self._preempted = False
        self._client_disconnected = False
        self._last_emitted_end = 0.0
        self._last_overload_frame_at = float("-inf")
        # Per-session STT dedupe memory: owned here (not on the shared
        # Transcriber) so a preempted session's still-running executor job
        # can never pollute the next session's overlap/suffix filters.
        self._text_state = SessionTextState()

    # ------------------------------------------------------------------ #
    # Public interface
    # ------------------------------------------------------------------ #

    async def run(self) -> None:
        """Run the live phase; on graceful stop, run the flush phase."""
        # Stamp every log line this session emits (see app.logging_setup).
        session_id_var.set(self._session_id)
        started_at = time.monotonic()
        source = " · ".join(
            part
            for part in (
                self._platform,
                self._channel,
                f"{self._stream_title!r}" if self._stream_title else None,
            )
            if part
        )
        logger.info(
            "session start: %s (sensitivity=%s topics=%d/%d transcripts=%s)",
            source or "unidentified source",
            self._sensitivity,
            len(self._enabled_topics),
            len(TOPICS),
            "on" if self._send_transcripts else "off",
        )
        if self._db is not None:
            await self._db.record_session_start(
                session_id=self._session_id,
                platform=self._platform,
                channel=self._channel,
                title=self._stream_title,
            )
        try:
            await self._run_phases()
        finally:
            self._log_session_summary(time.monotonic() - started_at)
            # Runs for graceful stop, preemption, disconnect, AND fatal
            # paths; app.db swallows write failures, so this can only lose
            # the row (with a warning), never mask the real outcome.
            if self._db is not None:
                await self._db.record_session_end(
                    session_id=self._session_id,
                    speech_seconds=self._speech_seconds,
                    audio_seconds=self._audio_seconds,
                    gate_calls=self._gate.calls_made,
                    verify_calls=self._verify_calls,
                    est_cost_usd=round(
                        self._verify_calls * self._settings.cost_per_verify_usd, 4
                    ),
                    stt_drop_counts=self._text_state.drop_counts,
                )

    def _log_session_summary(self, elapsed_s: float) -> None:
        """One end-of-session line with the whole funnel.

        This is the number people actually want after a stream: how much
        speech was heard, how many claims survived each stage, what it cost.
        Everything here is already tracked for the analytics row.
        """
        outcomes = ", ".join(
            f"{outcome}={count}"
            for outcome, count in sorted(self._claim_outcomes.items())
        )
        drops = ", ".join(
            f"{reason}={count}"
            for reason, count in sorted(self._text_state.drop_counts.items())
        )
        logger.info(
            "session end after %s: audio=%.0fs speech=%.0fs | gate calls=%d "
            "claims=%d [%s] | verify calls=%d ~$%.3f%s",
            _format_duration(elapsed_s),
            self._audio_seconds,
            self._speech_seconds,
            self._gate.calls_made,
            sum(self._claim_outcomes.values()),
            outcomes or "none",
            self._verify_calls,
            self._verify_calls * self._settings.cost_per_verify_usd,
            f" | stt drops: {drops}" if drops else "",
        )

    async def _run_phases(self) -> None:
        """The pre-analytics body of :meth:`run` (live phase, then flush)."""
        fatal_error = False
        try:
            async with asyncio.TaskGroup() as task_group:
                task_group.create_task(self._recv_loop(), name="recv")
                task_group.create_task(self._stt_loop(), name="stt")
                task_group.create_task(self._gate_loop(), name="gate")
                task_group.create_task(self._verify_loop(), name="verify")
                task_group.create_task(self._send_loop(), name="send")
                if self._detector is not None:
                    task_group.create_task(
                        self._contradiction_loop(), name="contradiction"
                    )
        except* WebSocketDisconnect:
            self._client_disconnected = True
            logger.info("client disconnected mid-session")
        except* Exception as group:
            for exc in group.exceptions:
                logger.error("session task failed", exc_info=exc)
            fatal_error = True
        if fatal_error:
            await self._close_quietly(code=1011)
            return
        if self._preempted or self._client_disconnected:
            return
        if self._graceful_stop:
            await self._flush_and_finish()

    def enqueue_frame(self, frame: dict[str, Any]) -> None:
        """Queue one outbound JSON frame (the contract consumed by debug.py).

        Raises:
            RuntimeError: if the session is shutting down.
            asyncio.QueueFull: if the outbound queue is saturated (a stalled
                or dead client); the frame is not enqueued.
        """
        if self._stop_requested.is_set():
            raise RuntimeError("session is shutting down; frame not enqueued")
        self._outbound.put_nowait(frame)

    async def preempt(
        self,
        code: str = "superseded",
        message: str = "A newer connection replaced this session.",
    ) -> None:
        """Kick this session out with a fatal error frame + 1000 close.

        The defaults describe the §2.1 new-connection preemption; other
        preemptors override them for honest client copy — the setup
        credentials hot-swap passes ``code="credentials_updated"`` so the
        extension can say "key updated" instead of "a newer connection".
        """
        if self._preempted:
            return
        self._preempted = True
        self._stop_requested.set()
        logger.info("session preempted (%s)", code)
        try:
            await self._websocket.send_json(
                ErrorFrame(code=code, message=message, fatal=True).model_dump()
            )
        except Exception as exc:
            logger.debug("could not send %s frame: %s", code, exc)
        await self._close_quietly(code=1000)

    def close(self) -> None:
        """Idempotent final cleanup; called from the endpoint's ``finally``."""
        self._stop_requested.set()

    # ------------------------------------------------------------------ #
    # Live-phase loops
    # ------------------------------------------------------------------ #

    async def _recv_loop(self) -> None:
        """Receive audio + control frames; the only reader of the socket."""
        while not self._stop_requested.is_set():
            message = await self._websocket.receive()
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(code=message.get("code") or 1000)
            payload_bytes = message.get("bytes")
            if payload_bytes is not None:
                self._handle_audio(payload_bytes)
                continue
            payload_text = message.get("text")
            if payload_text is not None:
                self._handle_text_frame(payload_text)

    async def _stt_loop(self) -> None:
        """Feed ring-buffer windows through the single-worker STT executor."""
        hop_s = self._settings.stt_hop_s
        window_s = self._settings.stt_window_s
        loop = asyncio.get_running_loop()
        while not self._stop_requested.is_set():
            # Wait for a FULL window, not just the hop: gating on the hop
            # would read ~hop-sized windows and consume nearly all of each,
            # destroying the (window - hop) overlap the transcriber's
            # trim/dedupe machinery needs to recover boundary words.
            if self._ring.pending_seconds < window_s:
                await self._sleep_or_stop(QUEUE_POLL_S)
                continue
            audio, window_start_s = self._ring.read_window(window_s)
            # Consume only the hop: the trailing (window - hop) seconds are
            # re-read next window; the transcriber trims the duplicate text.
            self._ring.consume(hop_s)
            await self._transcribe_and_route(
                loop, audio, window_start_s, self._emit_queued
            )

    async def _gate_loop(self) -> None:
        """Tick every second; run the throttled gate when it is due."""
        while not self._stop_requested.is_set():
            await self._sleep_or_stop(GATE_TICK_S)
            if self._stop_requested.is_set():
                return
            if not self._gate.should_run(time.monotonic()):
                continue
            gate_started = time.monotonic()
            try:
                claims = await self._gate.run()
            except GateError as exc:
                logger.warning("gate pass failed (batch dropped): %s", exc)
                continue
            logger.info(
                "gate pass #%d in %.1fs -> %d claim(s)%s",
                self._gate.calls_made,
                time.monotonic() - gate_started,
                len(claims),
                "".join(
                    f"\n    {claim.check_worthiness:.2f} [{claim.topic}] "
                    f"{claim.claim_text!r}"
                    for claim in claims
                ),
            )
            # ALL gate claims feed the contradiction detector, PRE-filter: a
            # contradiction between two low-stakes claims is still a
            # contradiction (report §4.2), and the gate's hard exclusions
            # already keep this to real assertions.
            if self._detector is not None:
                for claim in claims:
                    self._enqueue_contradiction(claim)
            for claim in await self._filter_claims(claims, self._emit_queued):
                await self._enqueue_claim(claim)
                await self._emit_queued(StatusFrame(claim=claim.claim_text))

    async def _verify_loop(self) -> None:
        """Serially verify queued claims; per-item failures never kill us."""
        while not self._stop_requested.is_set():
            try:
                claim = await asyncio.wait_for(
                    self._verify_queue.get(), timeout=QUEUE_POLL_S
                )
            except TimeoutError:
                continue
            await self._verify_claim(claim, self._emit_queued)

    async def _send_loop(self) -> None:
        """Drain the outbound queue onto the socket — the single writer."""
        while not self._stop_requested.is_set():
            try:
                frame = await asyncio.wait_for(
                    self._outbound.get(), timeout=QUEUE_POLL_S
                )
            except TimeoutError:
                continue
            await self._send_direct_dict(frame)

    async def _contradiction_loop(self) -> None:
        """Serially run gated claims through the contradiction detector.

        NOTHING here is ever fatal: an embedding/judge failure loses one
        look, never the session. The flush phase deliberately does not feed
        this loop — it is already dead by then, and a same-session
        contradiction alert after the stream stopped has no audience.
        """
        while not self._stop_requested.is_set():
            try:
                claim = await asyncio.wait_for(
                    self._contradiction_queue.get(), timeout=QUEUE_POLL_S
                )
            except TimeoutError:
                continue
            try:
                frame = await self._detector.add(claim)
            except Exception as exc:
                logger.warning(
                    "contradiction check failed for %r: %s", claim.claim_text, exc
                )
                continue
            if frame is None:
                continue
            logger.info(
                "contradiction (%s): earlier %r vs now %r — %s",
                frame.confidence,
                frame.prior_claim,
                frame.current_claim,
                frame.explanation,
            )
            await self._emit_queued(frame)
            if self._db is not None:
                await self._db.record_contradiction(
                    session_id=self._session_id,
                    current_claim=frame.current_claim,
                    prior_claim=frame.prior_claim,
                    prior_claimed_at=frame.prior_claimed_at,
                    confidence=frame.confidence,
                    explanation=frame.explanation,
                    emitted=True,
                )

    def _enqueue_contradiction(self, claim: GateClaim) -> None:
        """Drop-OLDEST mirror of :meth:`_enqueue_claim` (newest is most
        relevant to the live conversation)."""
        if self._contradiction_queue.full():
            dropped = self._contradiction_queue.get_nowait()
            logger.debug(
                "contradiction queue full; dropped oldest claim: %r",
                dropped.claim_text,
            )
        self._contradiction_queue.put_nowait(claim)

    # ------------------------------------------------------------------ #
    # Frame handling
    # ------------------------------------------------------------------ #

    def _handle_audio(self, pcm_bytes: bytes) -> None:
        self._audio_seconds += len(pcm_bytes) / 2 / 16000
        try:
            dropped_s = self._ring.append(pcm_bytes)
        except ValueError as exc:
            logger.warning("dropping malformed audio frame: %s", exc)
            return
        if dropped_s <= 0.0:
            return
        logger.warning(
            "audio buffer overflow: dropped %.2fs (STT falling behind)", dropped_s
        )
        now = time.monotonic()
        if now - self._last_overload_frame_at >= OVERLOAD_FRAME_INTERVAL_S:
            self._last_overload_frame_at = now
            self._enqueue_or_drop(
                ErrorFrame(
                    code="stt_overload",
                    message=(
                        "Transcription is falling behind; dropped "
                        f"{dropped_s:.1f}s of audio."
                    ),
                    fatal=False,
                )
            )

    def _handle_text_frame(self, raw: str) -> None:
        """Mid-session control frames: config updates and graceful stop."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("ignoring unparseable text frame: %s", exc)
            return
        frame_type = data.get("type") if isinstance(data, dict) else None
        if frame_type == "config":
            try:
                config = ClientConfig.model_validate(data)
            except ValidationError as exc:
                logger.warning("ignoring invalid config frame: %s", exc)
                return
            # Each field is optional and applied independently: an absent
            # field must never reset the other setting.
            if config.sensitivity is not None:
                self._sensitivity = config.sensitivity
                logger.info("sensitivity updated to %s", config.sensitivity)
            if config.enabled_topics is not None:
                self._enabled_topics = resolve_enabled_topics(config.enabled_topics)
                logger.info(
                    "enabled topics updated to %s", sorted(self._enabled_topics)
                )
        elif frame_type == "frame":
            self._handle_video_frame(data)
        elif frame_type == "stop":
            logger.info("client requested graceful stop; flushing")
            self._graceful_stop = True
            self._stop_requested.set()
        else:
            logger.warning("ignoring unknown client frame type: %r", frame_type)

    def _handle_video_frame(self, data: dict[str, Any]) -> None:
        """Ring-buffer one captured frame; invalid/oversize frames drop."""
        try:
            frame = ClientFrameMessage.model_validate(data)
        except ValidationError as exc:
            logger.warning("ignoring invalid frame message: %s", exc)
            return
        if len(frame.image_b64) > MAX_FRAME_B64_LEN:
            logger.warning(
                "ignoring oversize frame (%d b64 chars > %d)",
                len(frame.image_b64),
                MAX_FRAME_B64_LEN,
            )
            return
        self._frames.append(
            StoredFrame(
                image_b64=frame.image_b64,
                captured_at_ms=frame.captured_at_ms,
                received_at=time.monotonic(),
            )
        )

    def _select_frame(self) -> str | None:
        """The newest fresh frame, or None — a stale frame (captured well
        before the claim was spoken) is worse than no frame."""
        if not self._frames:
            return None
        newest = self._frames[-1]
        if time.monotonic() - newest.received_at > FRAME_MAX_AGE_S:
            return None
        return newest.image_b64

    # ------------------------------------------------------------------ #
    # Shared pipeline steps (used by both the live and flush phases)
    # ------------------------------------------------------------------ #

    async def _transcribe_and_route(
        self,
        loop: asyncio.AbstractEventLoop,
        audio: Any,
        window_start_s: float,
        emit: FrameEmitter,
    ) -> None:
        """Executor-run STT for one window; route segments to gate + client."""
        try:
            segments = await loop.run_in_executor(
                self._stt_executor,
                self._transcriber.transcribe_window,
                audio,
                window_start_s,
                self._last_emitted_end,
                self._text_state,
            )
        except Exception:
            logger.exception(
                "transcription window failed; dropping %.1fs of audio",
                len(audio) / 16000,
            )
            return
        if segments and logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "stt +%d segment(s): %s",
                len(segments),
                " | ".join(segment.text for segment in segments),
            )
        for segment in segments:
            self._gate.add_transcript(segment)
            self._speech_seconds += max(0.0, segment.end - segment.start)
            self._last_emitted_end = max(self._last_emitted_end, segment.end)
            if self._send_transcripts:
                await emit(
                    TranscriptFrame(
                        text=segment.text, start=segment.start, end=segment.end
                    )
                )

    async def _filter_claims(
        self, claims: list[GateClaim], emit: FrameEmitter
    ) -> list[GateClaim]:
        """Sensitivity threshold -> topic filter -> check-and-register dedupe.

        The topic filter sits BEFORE dedupe so a skipped claim never registers
        in the dedupe memory; each topic-dropped claim emits one
        ``topic_skipped`` status frame (transparency: the client can show
        what was suppressed) and no verdict follows.
        """
        threshold = SENSITIVITY_THRESHOLDS[self._sensitivity]
        accepted: list[GateClaim] = []
        for claim in claims:
            if claim.check_worthiness < threshold:
                logger.info(
                    "claim below %s threshold (%.2f < %.2f): %r",
                    self._sensitivity,
                    claim.check_worthiness,
                    threshold,
                    claim.claim_text,
                )
                await self._record_claim(claim, "below_threshold")
                continue
            if claim.topic not in self._enabled_topics:
                logger.info(
                    "claim topic %r disabled by filter: %r",
                    claim.topic,
                    claim.claim_text,
                )
                await self._record_claim(claim, "topic_skipped")
                await emit(
                    StatusFrame(
                        stage="topic_skipped",
                        claim=claim.claim_text,
                        topic=claim.topic,
                    )
                )
                continue
            if self._checker.is_duplicate(claim.claim_text):
                logger.info("dropping duplicate claim: %r", claim.claim_text)
                await self._record_claim(claim, "duplicate")
                continue
            accepted.append(claim)
        return accepted

    async def _enqueue_claim(self, claim: GateClaim) -> None:
        """Put on the verify queue; when full, drop the OLDEST claim.

        The newest claim is the most relevant to the live conversation.
        No await between the ``full()`` check and the put, so this is
        race-free on a single event loop (the analytics awaits come after
        the queue is consistent again).
        """
        dropped: GateClaim | None = None
        if self._verify_queue.full():
            dropped = self._verify_queue.get_nowait()
            logger.warning(
                "verify queue full; dropped oldest claim: %r", dropped.claim_text
            )
        self._verify_queue.put_nowait(claim)
        if dropped is not None:
            await self._record_claim(dropped, "queue_dropped")
        await self._record_claim(claim, "pending")

    async def _verify_claim(self, claim: GateClaim, emit: FrameEmitter) -> None:
        """Cooldown check -> token bucket -> grounded check -> verdict frame."""
        verify_started_at = time.monotonic()
        if self._cooldown.active:
            remaining_s = self._cooldown.remaining_s
            logger.warning(
                "quota cooldown active (%.0fs left); dropping claim: %r",
                remaining_s,
                claim.claim_text,
            )
            # The funnel enum has no cooldown slot; a cooldown drop is
            # recorded as verify_failed (documented mapping).
            await self._record_claim(claim, "verify_failed")
            await emit(
                ErrorFrame(
                    code="quota_cooldown",
                    # Replay the reason recorded by whichever provider tripped
                    # the cooldown (e.g. OpenRouter's "top up" 402 message);
                    # falls back to provider-neutral text when none was set.
                    message=(
                        f"Fact-checks paused for {remaining_s:.0f}s: "
                        f"{self._cooldown.reason}"
                    ),
                    fatal=False,
                )
            )
            return
        await self._bucket.acquire()
        if self._preempted or self._client_disconnected:
            logger.info(
                "session ended while throttled; dropping claim %r", claim.claim_text
            )
            return
        # Count the ATTEMPT (cost is incurred whether or not it succeeds).
        self._verify_calls += 1
        if self._verify_counter is not None:
            self._verify_counter.increment()
        # Vision (report §5): attach the freshest captured frame ONLY when
        # the claim plausibly references something on screen. The checker's
        # ladder guarantees an image can never fail a check that would
        # succeed without it.
        image_b64: str | None = None
        if self._settings.vision_enabled and has_visual_cue(claim.claim_text):
            image_b64 = self._select_frame()
        logger.info(
            "verifying %r%s",
            claim.claim_text,
            " with a captured frame" if image_b64 else "",
        )
        try:
            verdict = await self._checker.check(
                claim.claim_text, topic=claim.topic, image_b64=image_b64
            )
        except QuotaExceededError as exc:
            logger.warning("quota exceeded: %s", exc)
            await self._record_claim(claim, "verify_failed")
            await emit(ErrorFrame(code="quota_cooldown", message=str(exc), fatal=False))
            return
        except VerificationError as exc:
            logger.warning("verification failed for %r: %s", claim.claim_text, exc)
            await self._record_claim(claim, "verify_failed")
            await emit(
                ErrorFrame(
                    code="llm_failure",
                    message=f"Fact-check failed: {exc}",
                    fatal=False,
                )
            )
            return
        # Emit FIRST so persistence never adds wire latency.
        await emit(VerdictFrame.from_verdict(verdict))
        logger.info(
            "verdict %s in %.1fs%s: %r -> %s [%s]",
            verdict.label,
            time.monotonic() - verify_started_at,
            " (vision)" if image_b64 else "",
            claim.claim_text,
            verdict.explanation,
            ", ".join(source.url for source in verdict.sources) or "no sources",
        )
        await self._record_claim(claim, "verified")
        await self._record_verdict(claim, verdict, verify_started_at)

    # ------------------------------------------------------------------ #
    # Graceful-stop flush phase
    # ------------------------------------------------------------------ #

    async def _flush_and_finish(self) -> None:
        """Flush STT, run a final gate pass, verify leftovers, close 1000."""
        try:
            await self._drain_outbound_direct()
            await self._flush_stt()
            pending_claims = self._drain_verify_queue()
            pending_claims.extend(await self._final_gate_pass())
            for claim in pending_claims:
                await self._send_direct(StatusFrame(claim=claim.claim_text))
                await self._verify_claim(claim, self._send_direct)
        except WebSocketDisconnect:
            logger.info("client disconnected during final flush")
            return
        await self._close_quietly(code=1000)

    async def _drain_outbound_direct(self) -> None:
        while True:
            try:
                frame = self._outbound.get_nowait()
            except asyncio.QueueEmpty:
                return
            await self._send_direct_dict(frame)

    async def _flush_stt(self) -> None:
        """Transcribe whatever audio is still buffered (no overlap needed)."""
        loop = asyncio.get_running_loop()
        window_s = self._settings.stt_window_s
        while self._ring.pending_seconds >= FLUSH_MIN_AUDIO_S:
            audio, window_start_s = self._ring.read_window(window_s)
            self._ring.consume(window_s)
            await self._transcribe_and_route(
                loop, audio, window_start_s, self._send_direct
            )

    def _drain_verify_queue(self) -> list[GateClaim]:
        claims: list[GateClaim] = []
        while True:
            try:
                claims.append(self._verify_queue.get_nowait())
            except asyncio.QueueEmpty:
                return claims

    async def _final_gate_pass(self) -> list[GateClaim]:
        """One unconditional gate run over any not-yet-gated transcript text."""
        try:
            claims = await self._gate.run()
        except GateError as exc:
            logger.warning("final gate pass failed (batch dropped): %s", exc)
            return []
        logger.info("final gate pass -> %d claim(s)", len(claims))
        return await self._filter_claims(claims, self._send_direct)

    # ------------------------------------------------------------------ #
    # Analytics recording (no-ops when the pipeline has no Database)
    # ------------------------------------------------------------------ #

    async def _record_claim(self, claim: GateClaim, outcome: str) -> None:
        """Fire-and-forget funnel row (app.db swallows all write failures).

        The in-memory tally is kept even without a database so the
        end-of-session log line is always complete. "pending" is not counted:
        it is a placeholder that a terminal outcome later overwrites.
        """
        if outcome != "pending":
            self._claim_outcomes[outcome] = self._claim_outcomes.get(outcome, 0) + 1
        if self._db is None:
            return
        await self._db.record_claim(
            claim=claim,
            session_id=self._session_id,
            outcome=outcome,
            # Persisted for the report's §5.1 measurement: how often claims
            # reference on-screen content (the frame itself is NEVER stored).
            has_visual_cue=has_visual_cue(claim.claim_text),
        )

    async def _record_verdict(
        self, claim: GateClaim, verdict: Verdict, verify_started_at: float
    ) -> None:
        if self._db is None:
            return
        await self._db.record_verdict(
            verdict=verdict,
            claim_id=claim.id,
            session_id=self._session_id,
            latency_ms=int((time.monotonic() - verify_started_at) * 1000),
            provider=self._settings.resolved_verify_provider,
            model=self._settings.active_verify_model,
        )

    # ------------------------------------------------------------------ #
    # Emission + small utilities
    # ------------------------------------------------------------------ #

    def _enqueue_or_drop(self, frame: BaseModel) -> None:
        """Best-effort enqueue for internally generated frames.

        Deliberately bypasses the shutdown guard in :meth:`enqueue_frame`
        (which exists for the external debug.py contract): on a graceful
        stop, in-flight STT/verification work may complete after
        ``_stop_requested`` is set, and its frames must still reach the
        queue so ``_drain_outbound_direct`` delivers them in the flush phase
        instead of silently dropping an already-paid-for verdict.
        """
        try:
            self._outbound.put_nowait(frame.model_dump())
        except asyncio.QueueFull:
            logger.warning(
                "outbound queue full; dropping %s frame", frame.__class__.__name__
            )

    async def _emit_queued(self, frame: BaseModel) -> None:
        """Live-phase emitter: hand the frame to the send loop's queue."""
        self._enqueue_or_drop(frame)

    async def _send_direct(self, frame: BaseModel) -> None:
        """Flush-phase emitter: the send loop is dead, write the socket."""
        await self._send_direct_dict(frame.model_dump())

    async def _send_direct_dict(self, frame: dict[str, Any]) -> None:
        try:
            await self._websocket.send_json(frame)
        except WebSocketDisconnect:
            raise
        except Exception as exc:
            # A localhost send only fails when the client is gone; translate
            # so callers treat it as a disconnect, never an internal error.
            logger.info("websocket send failed (client gone?): %s", exc)
            raise WebSocketDisconnect(code=1006) from exc

    async def _sleep_or_stop(self, seconds: float) -> None:
        """Sleep, waking immediately if a stop is requested meanwhile."""
        try:
            await asyncio.wait_for(self._stop_requested.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def _close_quietly(self, code: int) -> None:
        try:
            await self._websocket.close(code=code)
        except Exception as exc:
            logger.debug("websocket close failed (already closed?): %s", exc)
