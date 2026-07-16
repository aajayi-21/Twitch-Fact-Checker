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
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ValidationError

from app.claim_gate import ClaimGate, GateError
from app.config import SENSITIVITY_THRESHOLDS, Settings
from app.fact_checker import FactChecker, QuotaExceededError, VerificationError
from app.models import (
    ClientConfig,
    ClientHello,
    ErrorFrame,
    GateClaim,
    Sensitivity,
    StatusFrame,
    TranscriptFrame,
    VerdictFrame,
    resolve_enabled_topics,
)
from app.rate_limit import QuotaCooldown, TokenBucket
from app.transcriber import AudioRingBuffer, SessionTextState, Transcriber

logger = logging.getLogger(__name__)

VERIFY_QUEUE_MAXSIZE = 3
OUTBOUND_QUEUE_MAXSIZE = 100
GATE_TICK_S = 1.0
QUEUE_POLL_S = 0.25
OVERLOAD_FRAME_INTERVAL_S = 10.0
FLUSH_MIN_AUDIO_S = 0.5

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
    ) -> None:
        self._websocket = websocket
        self._settings = settings
        self._transcriber = transcriber
        self._stt_executor = stt_executor
        self._gate = claim_gate
        self._checker = fact_checker
        self._bucket = verify_bucket
        self._cooldown = quota_cooldown
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
        fatal_error = False
        try:
            async with asyncio.TaskGroup() as task_group:
                task_group.create_task(self._recv_loop(), name="recv")
                task_group.create_task(self._stt_loop(), name="stt")
                task_group.create_task(self._gate_loop(), name="gate")
                task_group.create_task(self._verify_loop(), name="verify")
                task_group.create_task(self._send_loop(), name="send")
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
            try:
                claims = await self._gate.run()
            except GateError as exc:
                logger.warning("gate pass failed (batch dropped): %s", exc)
                continue
            for claim in await self._filter_claims(claims, self._emit_queued):
                self._enqueue_claim(claim)
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

    # ------------------------------------------------------------------ #
    # Frame handling
    # ------------------------------------------------------------------ #

    def _handle_audio(self, pcm_bytes: bytes) -> None:
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
        elif frame_type == "stop":
            logger.info("client requested graceful stop; flushing")
            self._graceful_stop = True
            self._stop_requested.set()
        else:
            logger.warning("ignoring unknown client frame type: %r", frame_type)

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
        for segment in segments:
            self._gate.add_transcript(segment)
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
                continue
            if claim.topic not in self._enabled_topics:
                logger.info(
                    "claim topic %r disabled by filter: %r",
                    claim.topic,
                    claim.claim_text,
                )
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
                continue
            accepted.append(claim)
        return accepted

    def _enqueue_claim(self, claim: GateClaim) -> None:
        """Put on the verify queue; when full, drop the OLDEST claim.

        The newest claim is the most relevant to the live conversation.
        No await between the ``full()`` check and the put, so this is
        race-free on a single event loop.
        """
        if self._verify_queue.full():
            dropped = self._verify_queue.get_nowait()
            logger.warning(
                "verify queue full; dropped oldest claim: %r", dropped.claim_text
            )
        self._verify_queue.put_nowait(claim)

    async def _verify_claim(self, claim: GateClaim, emit: FrameEmitter) -> None:
        """Cooldown check -> token bucket -> grounded check -> verdict frame."""
        if self._cooldown.active:
            remaining_s = self._cooldown.remaining_s
            logger.warning(
                "quota cooldown active (%.0fs left); dropping claim: %r",
                remaining_s,
                claim.claim_text,
            )
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
        try:
            verdict = await self._checker.check(claim.claim_text, topic=claim.topic)
        except QuotaExceededError as exc:
            logger.warning("quota exceeded: %s", exc)
            await emit(ErrorFrame(code="quota_cooldown", message=str(exc), fatal=False))
            return
        except VerificationError as exc:
            logger.warning("verification failed for %r: %s", claim.claim_text, exc)
            await emit(
                ErrorFrame(
                    code="llm_failure",
                    message=f"Fact-check failed: {exc}",
                    fatal=False,
                )
            )
            return
        await emit(VerdictFrame.from_verdict(verdict))

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
        return await self._filter_claims(claims, self._send_direct)

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
