"""
tests/test_scheduler_watchdog.py — Stuck-worker watchdog and reassignment.

The watchdog exists so that one wedged worker costs the run one wave (wave
mode) or one queue slot (parallel mode), never the whole run. These tests pin
the three properties everything else depends on:

  1. A batch taken off a stuck worker is still run — by someone.
  2. The late result of the wedged worker is discarded, not counted twice.
  3. A batch that wedges everyone it lands on fails outright, so the run ends.

Everything provider-shaped is stubbed at the executor boundary: no network, no
pool, no registry. The scheduler is driven through the same `run()` the engine
uses, with `_make_executor` patched to hand out scripted executors.
"""

from __future__ import annotations

import threading
import time

from translator.core.models import BatchResult, JobConfig
from translator.scheduling.scheduler import (
    BatchTask,
    Scheduler,
    _PendingCount,
)


# ── Harness ───────────────────────────────────────────────────────────────────

class _StubPool:
    """The slice of ConnectionPool the scheduler touches outside its executors."""

    def __init__(self) -> None:
        self.wave_index = 0

    def set_wave_size(self, _size: int) -> None:
        pass


class _ExecutionResult:
    """The slice of ExecutionResult the scheduler reads."""

    def __init__(self, content: str | None) -> None:
        self.content = content
        self.tokens = 0
        self.latency = 0.0
        self.attempts = 1
        self.connection = "stub"
        self.error = "" if content is not None else "stub error"
        self.error_kind = ""


class _ScriptedExecutor:
    """
    An executor whose `execute` runs a scripted behaviour per call.

    A behaviour is `worker_id -> str | None` (the reply), and may block —
    that is the wedge under test. The pool is never consulted: the stub
    replaces the whole RetryExecutor, so no lease, no key, no client.
    """

    def __init__(self, behaviours: list) -> None:
        self._behaviours = list(behaviours)
        self._lock = threading.Lock()
        self.calls: list[int] = []          # worker ids that entered execute

    def execute(self, prompt, label="", rows=0, use_repair_model=False,
                response_format=None):
        name = threading.current_thread().name
        worker_id = int(name.rsplit("-", 1)[-1]) if name.startswith("uet-") else 0
        with self._lock:
            self.calls.append(worker_id)
            behaviour = self._behaviours.pop(0) if self._behaviours else None
        if behaviour is None:
            return _ExecutionResult("ok")
        return _ExecutionResult(behaviour(worker_id))


def _job(mode: str = "wave", workers: int = 2) -> JobConfig:
    return JobConfig(
        provider_id="p", model_id="m", connection_id="",
        src_path="x", out_path="y",
        batch_size=1, n_workers=workers, processing_mode=mode,
        max_attempts=1, retry_mode="fixed",
        wave_delay=0, batch_delay=0,
    )


def _scheduler(job: JobConfig, executor: _ScriptedExecutor) -> Scheduler:
    scheduler = Scheduler(
        job=job,
        connection_pool=_StubPool(),       # type: ignore[arg-type]
        base_url="",
        prompt_builder=lambda task: "prompt",
        parse_response=lambda raw, task, report=None: BatchResult(
            results={row: raw for row, _src in task.rows}),
        stop_event=threading.Event(),
    )
    # One scripted executor for every worker: the behaviours list is the
    # script, popped in call order whatever worker makes the call.
    scheduler._make_executor = lambda worker_id: executor  # type: ignore[method-assign]
    return scheduler


def _tasks(n: int) -> list[BatchTask]:
    return [
        BatchTask(batch_num=i + 1, total_batches=n, rows=[(i + 1, f"row {i+1}")])
        for i in range(n)
    ]


def _shorten_stuck_limit(scheduler: Scheduler, seconds: float) -> None:
    scheduler.stuck_limit = lambda: seconds      # type: ignore[method-assign]


# ── _PendingCount ─────────────────────────────────────────────────────────────

class TestPendingCount:
    def test_zero_when_all_settled(self):
        pending = _PendingCount(2)
        assert not pending.done.is_set()
        pending.finished_one()
        assert not pending.done.is_set()
        pending.finished_one()
        assert pending.done.is_set()

    def test_reassignment_bumps_back_up(self):
        pending = _PendingCount(1)
        pending.finished_one()
        assert pending.done.is_set()
        pending.add(1)
        assert not pending.done.is_set()

    def test_empty_is_done(self):
        assert _PendingCount(0).done.is_set()


# ── Generation bookkeeping ─────────────────────────────────────────────────────

class TestGeneration:
    def test_next_generation_bumps(self):
        task = BatchTask(batch_num=1, total_batches=1, rows=[(1, "x")])
        assert task.generation == 0
        task.next_generation()
        assert task.generation == 1
        assert task.next_generation() is task

    def test_stale_check(self):
        task = BatchTask(batch_num=1, total_batches=1, rows=[(1, "x")])
        assert not Scheduler._is_stale(task, 0)
        task.next_generation()
        assert Scheduler._is_stale(task, 0)
        assert not Scheduler._is_stale(task, 1)


# ── Stuck limit ───────────────────────────────────────────────────────────────

class TestStuckLimit:
    def test_is_positive(self):
        scheduler = _scheduler(_job(), _ScriptedExecutor([]))
        assert scheduler.stuck_limit() > 0


# ── Wave reassignment ─────────────────────────────────────────────────────────

class TestWaveReassignment:
    def test_stuck_batch_is_requeued_and_run_by_another_worker(self):
        """The property the whole feature exists for: the batch survives."""
        release = threading.Event()

        def wedged(_worker_id):
            release.wait(10)          # hold the wave hostage
            return "late"

        def fine(_worker_id):
            return "ok"

        executor = _ScriptedExecutor([wedged, fine])
        scheduler = _scheduler(_job("wave", workers=2), executor)
        _shorten_stuck_limit(scheduler, 0.5)

        stats = scheduler.run(_tasks(2))
        try:
            assert stats.ok == 2, f"both rows settled, got ok={stats.ok}"
            # Batch 1 was wedged on worker 1; the takeover by another worker
            # is what settled it. (Which worker, and whether worker 1 later
            # rejoined for fresh work, is not the property under test.)
            assert executor.calls[0] == 1, "batch 1 started on worker 1"
            assert 2 in executor.calls, \
                f"another worker took over the stuck batch: {executor.calls}"
        finally:
            release.set()

    def test_wedged_workers_late_result_is_discarded(self):
        """Rows are written exactly once despite both workers replying."""
        release = threading.Event()

        def wedged(_worker_id):
            release.wait(10)
            return "late"

        def fine(_worker_id):
            return "ok"

        executor = _ScriptedExecutor([wedged, fine])
        scheduler = _scheduler(_job("wave", workers=2), executor)
        writes: dict[int, int] = {}

        def on_result(task, result):
            for row in result.results:
                writes[row] = writes.get(row, 0) + 1

        scheduler._on_result = on_result
        _shorten_stuck_limit(scheduler, 0.5)
        stats = scheduler.run(_tasks(2))
        release.set()

        assert all(count == 1 for count in writes.values()), \
            f"rows written more than once: {writes}"
        assert stats.ok == 2


# ── Parallel reassignment ─────────────────────────────────────────────────────

class TestParallelReassignment:
    def test_run_finishes_with_a_wedged_worker(self):
        """
        Parallel shutdown cannot depend on every thread exiting: the wedged
        worker is a daemon. The run must still settle every batch.
        """
        release = threading.Event()

        def wedged(_worker_id):
            release.wait(10)
            return "late"

        def fine(_worker_id):
            return "ok"

        executor = _ScriptedExecutor([wedged, fine])
        scheduler = _scheduler(_job("parallel", workers=2), executor)
        _shorten_stuck_limit(scheduler, 0.5)
        stats = scheduler.run(_tasks(1))
        release.set()
        assert stats.ok == 1, f"the batch was run by the healthy worker, ok={stats.ok}"

    def test_reassigned_batch_counts_once(self):
        release = threading.Event()

        def wedged(_worker_id):
            release.wait(10)
            return "late"

        def fine(_worker_id):
            return "ok"

        executor = _ScriptedExecutor([wedged, fine])
        scheduler = _scheduler(_job("parallel", workers=2), executor)
        writes: dict[int, int] = {}

        def on_result(task, result):
            for row in result.results:
                writes[row] = writes.get(row, 0) + 1

        scheduler._on_result = on_result
        _shorten_stuck_limit(scheduler, 0.5)
        stats = scheduler.run(_tasks(1))
        release.set()
        assert all(c == 1 for c in writes.values()), f"double write: {writes}"
        assert stats.ok == 1


# ── Exhausted reassignment ────────────────────────────────────────────────────

class TestReassignmentExhaustion:
    def test_batch_fails_after_max_reassigns(self):
        """
        A batch that wedges every worker it lands on must be failed, not
        requeued forever — the run has to be able to finish without it.
        """
        from translator import config

        gates = [threading.Event() for _ in range(config.MAX_TASK_REASSIGNS + 2)]

        def always_wedge(_worker_id):
            # Each wedge holds a different gate, so the finally-block in the
            # test can release every thread that ever started.
            with _counter_lock:
                index = next(_counter)
            gates[min(index, len(gates) - 1)].wait(10)
            return "late"

        _counter_lock = threading.Lock()
        _counter = iter(range(100))

        executor = _ScriptedExecutor(
            [always_wedge for _ in range(config.MAX_TASK_REASSIGNS + 2)]
        )
        scheduler = _scheduler(_job("wave", workers=2), executor)
        _shorten_stuck_limit(scheduler, 0.3)
        stats = scheduler.run(_tasks(1))
        for gate in gates:
            gate.set()
        assert stats.failed == 1, \
            f"batch failed after exhausting reassignments, failed={stats.failed}"
