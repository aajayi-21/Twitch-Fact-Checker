"""STT supervisor: circuit breaker and CPU fallback around the shared engine.

The speech engine (``app.transcriber.BaseTranscriber``) and its single-worker
executor are process-wide; every live session's STT loop funnels its windows
through them. Before this module, a window failure was logged by the session
and the next window was requested immediately — harmless for a one-off, but
an accelerator whose device context has been poisoned by an asynchronous
kernel assert (Intel XPU: ``vectorized gather kernel index out of bounds``)
fails *every* subsequent kernel, so the session degraded into an endless run
of dropped windows and ``stt_overload`` frames with nothing ever escalating.

``SttSupervisor`` wraps each ``transcribe_window`` call and turns that into a
state machine:

- ``ok``          — windows are succeeding (a success resets the streak).
- ``recovering``  — ``failure_threshold`` consecutive windows failed; the
  engine is being reloaded on the CPU. The reload runs **on the same
  single-worker executor**, so it is serialized behind any in-flight window
  and no job can be mid-inference while the model object is swapped.
- ``degraded``    — the CPU reload worked. Sessions continue (captions may
  lag); the pipeline tells the client once via a non-fatal ``stt_degraded``
  frame, and ``/healthz`` reports ``status: degraded``.
- ``failed``      — the reload failed too, was disabled (``STT_CPU_FALLBACK``),
  or the CPU engine itself then broke. Sessions end with a fatal
  ``stt_failure`` frame and new connections are rejected until a restart.

Recovery happens at most once per process: the only fallback target is the
CPU, so a second breakage has nowhere left to go.

Exceptions for the pipeline:

- :class:`SttWindowFailed` — this window is lost; keep the session.
- :class:`SttEngineFailed` — the engine is unrecoverable; end the session.

The supervisor also runs the startup :meth:`warm_up` (first-call kernel
compilation belongs at boot, not inside a live session) and exposes a
:meth:`snapshot` for ``/healthz``. ``inject_failures`` is a fault-injection
seam (``POST /debug/stt/fail``) so the whole breaker can be rehearsed on a
real machine without breaking a GPU.
"""

import asyncio
import logging
import time
from concurrent.futures import Executor
from typing import Any, Literal

import numpy as np

from app.models import TranscriptSegment
from app.transcriber import SessionTextState

logger = logging.getLogger(__name__)

SttState = Literal["ok", "recovering", "degraded", "failed"]


class SttWindowFailed(RuntimeError):
    """One transcription window failed; the session may continue."""


class SttEngineFailed(RuntimeError):
    """The speech engine is unrecoverable; sessions must end."""


class SttSupervisor:
    """Owns the process-wide transcriber + executor; see the module docstring."""

    def __init__(
        self,
        transcriber: Any,
        executor: Executor,
        *,
        failure_threshold: int = 3,
        cpu_fallback: bool = True,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError(f"failure_threshold must be >= 1, got {failure_threshold}")
        self._transcriber = transcriber
        self._executor = executor
        self._failure_threshold = failure_threshold
        self._cpu_fallback = cpu_fallback
        self._state: SttState = "ok"
        self._consecutive_failures = 0
        self._last_error: str | None = None
        self._degrade_events = 0
        self._recoveries = 0
        # Bumped by every recovery. A window submitted against an older
        # generation that fails afterwards belongs to the engine that was
        # replaced, so it must not count against the recovered one.
        self._generation = 0
        self._lock = asyncio.Lock()
        #: Fault injection: the next N windows raise before touching the
        #: engine (``POST /debug/stt/fail``). Zero in normal operation.
        self.inject_failures = 0

    # ------------------------------------------------------------------ #
    # Read-only surface
    # ------------------------------------------------------------------ #

    @property
    def transcriber(self) -> Any:
        return self._transcriber

    @property
    def executor(self) -> Executor:
        return self._executor

    @property
    def state(self) -> SttState:
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def degrade_events(self) -> int:
        """How many times the engine has fallen back (0 or 1 today).

        Sessions compare this against what they have already told their
        client, so each session announces a degradation exactly once.
        """
        return self._degrade_events

    @property
    def failure_threshold(self) -> int:
        return self._failure_threshold

    @property
    def status_word(self) -> str:
        """``/healthz`` status: ``ok`` | ``degraded`` | ``unhealthy``."""
        if self._state == "failed":
            return "unhealthy"
        if self._state in ("recovering", "degraded"):
            return "degraded"
        return "ok"

    def snapshot(self) -> dict[str, Any]:
        """The ``/healthz`` ``stt`` block."""
        transcriber = self._transcriber
        device = getattr(transcriber, "effective_device", None)
        if device is None:
            device = getattr(transcriber, "device", None)
        return {
            "state": self._state,
            "backend": getattr(transcriber, "backend_name", None),
            "model": getattr(transcriber, "model_name", None),
            "device": device,
            "degraded_from": getattr(transcriber, "degraded_from", None),
            "consecutive_failures": self._consecutive_failures,
            "failure_threshold": self._failure_threshold,
            "last_error": self._last_error,
            "cpu_fallback": self._cpu_fallback,
        }

    # ------------------------------------------------------------------ #
    # Startup
    # ------------------------------------------------------------------ #

    async def warm_up(self, budget_s: float | None = None) -> None:
        """Run the engine's warm-up on the executor; recover if it faults.

        A GPU that fails during warm-up starts the server degraded on the
        CPU instead of not at all. If that recovery fails as well, startup
        aborts loudly.

        Raises:
            RuntimeError: when warm-up failed and the CPU fallback failed too.
        """
        loop = asyncio.get_running_loop()
        started = time.perf_counter()
        try:
            await loop.run_in_executor(
                self._executor, self._transcriber.warm_up, budget_s
            )
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            logger.error(
                "STT warm-up failed on %s: %s",
                self._transcriber.describe(),
                exc,
                exc_info=exc,
            )
            self._consecutive_failures = self._failure_threshold
            try:
                await self._recover()
            except SttEngineFailed as failure:
                raise RuntimeError(
                    "speech engine failed during warm-up and could not be "
                    f"recovered: {failure}"
                ) from failure
            return
        logger.info(
            "STT warm-up finished in %.1fs: %s",
            time.perf_counter() - started,
            self._transcriber.describe(),
        )

    # ------------------------------------------------------------------ #
    # Per-window entry point
    # ------------------------------------------------------------------ #

    async def run_window(
        self,
        audio: np.ndarray,
        window_start_s: float,
        last_emitted_end: float,
        text_state: SessionTextState,
    ) -> list[TranscriptSegment]:
        """``transcribe_window`` on the executor, with the breaker around it.

        Raises:
            SttWindowFailed: the window failed; the engine is still usable
                (possibly after a CPU fallback that happened inside this call).
            SttEngineFailed: the engine is unrecoverable.
        """
        if self._state == "failed":
            raise SttEngineFailed(self._last_error or "speech engine failed")
        loop = asyncio.get_running_loop()
        generation = self._generation
        try:
            if self.inject_failures > 0:
                self.inject_failures -= 1
                raise RuntimeError("injected STT fault (POST /debug/stt/fail)")
            segments = await loop.run_in_executor(
                self._executor,
                self._transcriber.transcribe_window,
                audio,
                window_start_s,
                last_emitted_end,
                text_state,
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if generation != self._generation:
                # Queued behind a recovery that has since completed: this is
                # the OLD engine failing, already accounted for.
                logger.info(
                    "dropping a window that failed on the replaced engine: %s", error
                )
                raise SttWindowFailed(error) from exc
            self._consecutive_failures += 1
            self._last_error = error
            logger.exception(
                "transcription window failed (%d/%d) on %s",
                self._consecutive_failures,
                self._failure_threshold,
                self._transcriber.describe(),
            )
            if self._consecutive_failures >= self._failure_threshold:
                await self._recover()
            raise SttWindowFailed(error) from exc
        self._consecutive_failures = 0
        return segments

    async def _recover(self) -> None:
        """One CPU reload, serialized; raises when there is nothing left to try.

        Idempotent under concurrency: several sessions can hit the threshold
        while one recovery is in flight. Whoever gets the lock second sees a
        reset streak and returns; a failed state is re-raised for everyone.

        Raises:
            SttEngineFailed: fallback disabled, already used, or failed.
        """
        async with self._lock:
            if self._state == "failed":
                raise SttEngineFailed(self._last_error or "speech engine failed")
            if self._consecutive_failures < self._failure_threshold:
                # Another session's recovery already reset the streak.
                return
            if not self._cpu_fallback or self._recoveries >= 1:
                reason = (
                    "CPU fallback is disabled (STT_CPU_FALLBACK=false)"
                    if not self._cpu_fallback
                    else "it is already running on the CPU fallback"
                )
                self._state = "failed"
                logger.error(
                    "STT engine broken after %d consecutive failures on %s and %s; "
                    "giving up — restart the backend. Last error: %s",
                    self._consecutive_failures,
                    self._transcriber.describe(),
                    reason,
                    self._last_error,
                )
                raise SttEngineFailed(
                    f"speech engine failed ({reason}); last error: "
                    f"{self._last_error}"
                )
            self._state = "recovering"
            self._recoveries += 1
            self._degrade_events += 1
            logger.error(
                "STT engine broken after %d consecutive failures on %s; "
                "reloading on the CPU (fp32). Last error: %s",
                self._consecutive_failures,
                self._transcriber.describe(),
                self._last_error,
            )
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(
                    self._executor, self._transcriber.fall_back_to_cpu
                )
            except Exception as exc:
                self._state = "failed"
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.error(
                    "CPU fallback failed (%s); restart the backend",
                    exc,
                    exc_info=exc,
                )
                raise SttEngineFailed(
                    f"speech engine failed and the CPU fallback failed too: {exc}"
                ) from exc
            self._state = "degraded"
            self._consecutive_failures = 0
            self._generation += 1
            logger.warning(
                "STT recovered on the CPU: %s — captions may lag behind live audio",
                self._transcriber.describe(),
            )
