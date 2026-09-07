"""SttSupervisor: the circuit breaker + CPU fallback around the STT engine.

Everything runs against ``FakeTranscriber`` subclasses on a real
single-worker executor, so the serialization argument (the reload runs on
the same thread as the windows) is exercised for real.
"""

import asyncio
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import numpy as np
import pytest

from app.stt_supervisor import SttEngineFailed, SttSupervisor, SttWindowFailed
from app.transcriber import SessionTextState
from tests.conftest import FakeTranscriber

AUDIO = np.zeros(16000, dtype=np.float32)


class FailingTranscriber(FakeTranscriber):
    """FakeTranscriber whose next ``fail_next`` windows raise.

    Failures model a poisoned accelerator: by default they only happen while
    the engine is still on ``xpu``; set ``fail_on_cpu`` to make the recovered
    CPU engine break as well.
    """

    def __init__(self) -> None:
        super().__init__()
        self.fail_next = 0
        self.fail_on_cpu = False
        self.before_fail: Callable[[], None] | None = None
        self.fallback_raises: Exception | None = None
        self.fallback_delay_s = 0.0
        self.warm_up_raises: Exception | None = None
        self.thread_names: list[str] = []
        self._device = "xpu"

    def transcribe_window(self, *args: Any, **kwargs: Any) -> Any:
        self.thread_names.append(threading.current_thread().name)
        if self.fail_next > 0 and (self._device != "cpu" or self.fail_on_cpu):
            self.fail_next -= 1
            if self.before_fail is not None:
                self.before_fail()
            raise RuntimeError("vectorized gather kernel index out of bounds")
        return super().transcribe_window(*args, **kwargs)

    def fall_back_to_cpu(self) -> None:
        self.thread_names.append(threading.current_thread().name)
        if self.fallback_delay_s:
            time.sleep(self.fallback_delay_s)
        if self.fallback_raises is not None:
            raise self.fallback_raises
        super().fall_back_to_cpu()

    def warm_up(self, budget_s: float | None = None) -> None:
        super().warm_up(budget_s)
        if self.warm_up_raises is not None:
            raise self.warm_up_raises


@pytest.fixture()
def executor() -> Iterator[ThreadPoolExecutor]:
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt-test")
    try:
        yield pool
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def make_supervisor(
    executor: ThreadPoolExecutor,
    transcriber: FailingTranscriber | None = None,
    *,
    threshold: int = 3,
    cpu_fallback: bool = True,
) -> tuple[SttSupervisor, FailingTranscriber]:
    transcriber = transcriber or FailingTranscriber()
    supervisor = SttSupervisor(
        transcriber, executor, failure_threshold=threshold, cpu_fallback=cpu_fallback
    )
    return supervisor, transcriber


async def run(supervisor: SttSupervisor) -> Any:
    return await supervisor.run_window(AUDIO, 0.0, 0.0, SessionTextState())


class TestConstruction:
    def test_threshold_below_one_rejected(self, executor: ThreadPoolExecutor) -> None:
        with pytest.raises(ValueError):
            SttSupervisor(FailingTranscriber(), executor, failure_threshold=0)

    def test_snapshot_shape(self, executor: ThreadPoolExecutor) -> None:
        supervisor, _ = make_supervisor(executor)
        assert supervisor.snapshot() == {
            "state": "ok",
            "backend": "fake",
            "model": "fake-whisper.en",
            "device": "xpu",
            "degraded_from": None,
            "consecutive_failures": 0,
            "failure_threshold": 3,
            "last_error": None,
            "cpu_fallback": True,
        }
        assert supervisor.status_word == "ok"


class TestBreaker:
    async def test_success_resets_the_streak(
        self, executor: ThreadPoolExecutor
    ) -> None:
        supervisor, transcriber = make_supervisor(executor)
        transcriber.fail_next = 2
        for _ in range(2):
            with pytest.raises(SttWindowFailed):
                await run(supervisor)
        assert supervisor.consecutive_failures == 2
        assert await run(supervisor) == []
        assert supervisor.consecutive_failures == 0
        assert supervisor.state == "ok"
        assert transcriber.fall_back_calls == 0

    async def test_below_threshold_is_a_lost_window_only(
        self, executor: ThreadPoolExecutor
    ) -> None:
        supervisor, transcriber = make_supervisor(executor)
        transcriber.fail_next = 1
        with pytest.raises(SttWindowFailed, match="gather kernel"):
            await run(supervisor)
        assert supervisor.state == "ok"
        assert supervisor.last_error is not None
        assert transcriber.fall_back_calls == 0

    async def test_threshold_trips_exactly_one_cpu_fallback_on_the_executor(
        self, executor: ThreadPoolExecutor
    ) -> None:
        supervisor, transcriber = make_supervisor(executor)
        transcriber.fail_next = 3
        for _ in range(3):
            with pytest.raises(SttWindowFailed):
                await run(supervisor)
        assert supervisor.state == "degraded"
        assert supervisor.status_word == "degraded"
        assert supervisor.degrade_events == 1
        assert supervisor.consecutive_failures == 0
        assert transcriber.fall_back_calls == 1
        # The reload ran on the single STT worker, serialized with windows.
        assert all(name.startswith("stt-test") for name in transcriber.thread_names)
        assert supervisor.snapshot()["device"] == "cpu"
        assert supervisor.snapshot()["degraded_from"] == "xpu"
        # Degraded but alive: windows keep flowing.
        assert await run(supervisor) == []

    async def test_fallback_failure_is_fatal(
        self, executor: ThreadPoolExecutor
    ) -> None:
        supervisor, transcriber = make_supervisor(executor, threshold=1)
        transcriber.fail_next = 1
        transcriber.fallback_raises = RuntimeError("cpu load failed")
        with pytest.raises(SttEngineFailed, match="CPU fallback failed"):
            await run(supervisor)
        assert supervisor.state == "failed"
        assert supervisor.status_word == "unhealthy"
        # Later windows fail fast without touching the engine.
        before = len(transcriber.thread_names)
        with pytest.raises(SttEngineFailed):
            await run(supervisor)
        assert len(transcriber.thread_names) == before

    async def test_second_breakage_after_degraded_is_fatal(
        self, executor: ThreadPoolExecutor
    ) -> None:
        supervisor, transcriber = make_supervisor(executor, threshold=1)
        transcriber.fail_next = 1
        with pytest.raises(SttWindowFailed):
            await run(supervisor)
        assert supervisor.state == "degraded"
        transcriber.fail_next = 1
        transcriber.fail_on_cpu = True
        with pytest.raises(SttEngineFailed, match="already running on the CPU"):
            await run(supervisor)
        assert supervisor.state == "failed"
        assert transcriber.fall_back_calls == 1

    async def test_disabled_fallback_fails_at_threshold(
        self, executor: ThreadPoolExecutor
    ) -> None:
        supervisor, transcriber = make_supervisor(
            executor, threshold=2, cpu_fallback=False
        )
        transcriber.fail_next = 2
        with pytest.raises(SttWindowFailed):
            await run(supervisor)
        with pytest.raises(SttEngineFailed, match="STT_CPU_FALLBACK"):
            await run(supervisor)
        assert transcriber.fall_back_calls == 0
        assert supervisor.state == "failed"

    async def test_concurrent_failures_share_one_recovery(
        self, executor: ThreadPoolExecutor
    ) -> None:
        """Two sessions trip the breaker in the same loop step: one reload.

        Injected faults raise before the executor is involved, and the
        reload is slowed down, so the second failure is processed while the
        first recovery is still in flight — the second caller must find the
        lock held and then a reset streak.
        """
        supervisor, transcriber = make_supervisor(executor, threshold=1)
        supervisor.inject_failures = 2
        transcriber.fallback_delay_s = 0.1
        results = await asyncio.gather(
            run(supervisor), run(supervisor), return_exceptions=True
        )
        assert all(isinstance(result, SttWindowFailed) for result in results)
        assert transcriber.fall_back_calls == 1
        assert supervisor.state == "degraded"
        assert supervisor.degrade_events == 1

    async def test_concurrent_sessions_on_the_executor_recover_once(
        self, executor: ThreadPoolExecutor
    ) -> None:
        """Real executor race: a window queued either side of the reload.

        Whichever order the single worker picks, the outcome is one reload,
        a degraded (not failed) engine, and each window either succeeding on
        the CPU or being reported as a lost window — never a fatal error.
        """
        supervisor, transcriber = make_supervisor(executor, threshold=1)
        transcriber.fail_next = 2
        results = await asyncio.gather(
            run(supervisor), run(supervisor), return_exceptions=True
        )
        assert all(
            result == [] or isinstance(result, SttWindowFailed) for result in results
        )
        assert transcriber.fall_back_calls == 1
        assert supervisor.state == "degraded"

    async def test_failure_from_the_replaced_engine_does_not_count(
        self, executor: ThreadPoolExecutor
    ) -> None:
        """A window submitted before a recovery fails on the OLD engine."""
        supervisor, transcriber = make_supervisor(executor, threshold=1)
        # Recover once, then hand the supervisor a stale-generation failure:
        # the window raises AFTER "another recovery" bumped the generation
        # while it was in flight.
        transcriber.fail_next = 1
        with pytest.raises(SttWindowFailed):
            await run(supervisor)
        assert supervisor.state == "degraded"
        transcriber.fail_next = 1
        transcriber.fail_on_cpu = True

        def bump_generation() -> None:
            supervisor._generation += 1

        transcriber.before_fail = bump_generation
        with pytest.raises(SttWindowFailed):
            await run(supervisor)
        assert supervisor.consecutive_failures == 0
        assert supervisor.state == "degraded"

    async def test_injected_failures_skip_the_engine(
        self, executor: ThreadPoolExecutor
    ) -> None:
        supervisor, transcriber = make_supervisor(executor)
        supervisor.inject_failures = 1
        with pytest.raises(SttWindowFailed, match="injected"):
            await run(supervisor)
        assert transcriber.thread_names == []
        assert supervisor.inject_failures == 0
        assert await run(supervisor) == []


class TestWarmUp:
    async def test_runs_the_engine_hook_with_the_budget(
        self, executor: ThreadPoolExecutor, caplog: pytest.LogCaptureFixture
    ) -> None:
        supervisor, transcriber = make_supervisor(executor)
        with caplog.at_level("INFO", logger="app.stt_supervisor"):
            await supervisor.warm_up(3.5)
        assert transcriber.warm_up_calls == [3.5]
        assert supervisor.state == "ok"
        assert any("warm-up finished" in r.message for r in caplog.records)

    async def test_warm_up_fault_starts_degraded_on_cpu(
        self, executor: ThreadPoolExecutor
    ) -> None:
        supervisor, transcriber = make_supervisor(executor)
        transcriber.warm_up_raises = RuntimeError("Native API failed")
        await supervisor.warm_up(3.5)
        assert supervisor.state == "degraded"
        assert transcriber.fall_back_calls == 1
        assert supervisor.degrade_events == 1

    async def test_warm_up_fault_with_failed_fallback_aborts_startup(
        self, executor: ThreadPoolExecutor
    ) -> None:
        supervisor, transcriber = make_supervisor(executor)
        transcriber.warm_up_raises = RuntimeError("Native API failed")
        transcriber.fallback_raises = RuntimeError("cpu load failed")
        with pytest.raises(RuntimeError, match="could not be recovered"):
            await supervisor.warm_up(3.5)
        assert supervisor.state == "failed"
