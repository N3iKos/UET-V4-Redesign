"""
tests/test_retry_status_transition.py — Verify worker status phase transitions during retries and recovery.
"""

from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.core.models import BatchResult, JobConfig, ProviderError
from translator.providers.client import GenerateResult, ProviderClient
from translator.providers.models import ModelEntry
from translator.scheduling.retry import ExecutionResult, RetryExecutor
from translator.scheduling.scheduler import BatchTask, Scheduler, WorkerStatus


class _StubKeyPool:
    def __init__(self):
        self.stop_event = None

    def acquire(self, timeout=600.0):
        class _State:
            key = "key1"
        return _State()

    def record_success(self, *a, **k): pass
    def record_error(self, *a, **k): pass
    def record_503(self, *a, **k): pass
    def record_latency(self, *a, **k): pass
    def record_rate_limit(self, *a, **k): pass
    def cooldown_remaining(self): return 0.0


class _StubLease:
    name = "PrimaryConn"
    connection_id = "c1"
    base_url = "http://primary.com"
    model_id = "model-primary"
    key_pool = _StubKeyPool()

    class connection:
        @staticmethod
        def find_model(model_id):
            return ModelEntry(
                id="model-primary",
                reasoning_supported=True,
                reasoning_control="toggle",
                reasoning_default=False,
            )


class _StubReasoningLease(_StubLease):
    class connection:
        @staticmethod
        def find_model(model_id):
            return ModelEntry(
                id="model-primary",
                reasoning_supported=True,
                reasoning_control="toggle",
                reasoning_default=True,
            )


class _StubPool:
    def __init__(self, reasoning: bool = False) -> None:
        self.wave_index = 0
        self._reasoning = reasoning

    def acquire(self, rows=0):
        if self._reasoning:
            return _StubReasoningLease()
        return _StubLease()

    def set_wave_size(self, _size: int) -> None:
        pass

    def report_error(self, *a, **k): pass
    def report_success(self, *a, **k): pass
    def report_cooldown(self, *a, **k): pass
    def report_exhausted(self, *a, **k): pass


def _job(max_attempts: int = 2) -> JobConfig:
    return JobConfig(
        provider_id="p", model_id="model-primary", connection_id="c1",
        src_path="x", out_path="y",
        batch_size=2, n_workers=1, processing_mode="sequential",
        max_attempts=max_attempts, retry_mode="fixed",
        retry_delay_err=0,
        retry_delay_503=0,
        wave_delay=0, batch_delay=0,
    )


def test_recovery_phase_transitions_to_generating():
    """
    During recovery ladder (mini-batch retry), on_phase must transition
    the worker phase to 'generating' instead of locking it to 'retry'.
    """
    status_snapshots: list[WorkerStatus] = []
    call_count = 0

    def on_status(status: WorkerStatus) -> None:
        status_snapshots.append(status)

    def parse_response(raw: str, task: BatchTask, report=None) -> BatchResult:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First attempt: row 1 succeeds, row 2 is missing -> triggers recovery
            return BatchResult(
                results={1: "line 1 translated"},
                failed_rows=[2],
            )
        # Mini-batch recovery attempt: returns row 2
        return BatchResult(
            results={2: "line 2 translated"},
            failed_rows=[],
        )

    scheduler = Scheduler(
        job=_job(),
        connection_pool=_StubPool(),
        base_url="http://primary.com",
        prompt_builder=lambda task: "prompt",
        parse_response=parse_response,
        on_status=on_status,
        stop_event=threading.Event(),
    )

    class _RecoverExecutor:
        def execute(self, prompt, label="", rows=0, use_repair_model=False, response_format=None):
            # Trigger the executor's on_phase callback created by scheduler._make_executor
            executor_instance._on_phase("generating", False)
            return ExecutionResult(
                content="dummy content",
                tokens=10,
                latency=0.01,
                attempts=1,
                connection="PrimaryConn",
            )

    executor_instance = scheduler._make_executor(1)
    fake_executor = _RecoverExecutor()
    scheduler._make_executor = lambda wid: fake_executor

    task = BatchTask(
        batch_num=1,
        total_batches=1,
        rows=[(1, "source 1"), (2, "source 2")],
    )

    stats = scheduler.run([task], total_rows=2)
    assert stats.ok == 2

    phases = [s.phase for s in status_snapshots]
    assert "retry" in phases
    retry_idx = phases.index("retry")
    recovery_generating = [p for p in phases[retry_idx:] if p == "generating"]
    assert len(recovery_generating) > 0, f"Expected 'generating' after 'retry' in phases: {phases}"


def test_retry_executor_phase_transitions_between_attempts(monkeypatch):
    """
    When RetryExecutor fails on attempt 1 and succeeds on attempt 2,
    phase transitions generating -> retry -> generating -> done,
    and started_at updates when attempt 2 begins.
    """
    phases: list[tuple[str, float]] = []

    def on_status(status: WorkerStatus) -> None:
        phases.append((status.phase, status.started_at))

    scheduler = Scheduler(
        job=_job(max_attempts=2),
        connection_pool=_StubPool(),
        base_url="http://primary.com",
        prompt_builder=lambda task: "prompt",
        parse_response=lambda raw, task, report=None: BatchResult(results={row: raw for row, _ in task.rows}),
        on_status=on_status,
        stop_event=threading.Event(),
    )

    attempt_count = 0

    def fake_generate(self, model, prompt, **kwargs):
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count == 1:
            raise ProviderError("Rate limit", status_code=429, is_rate_limit=True, retry_delay=0, kind=ProviderError.KIND_RATE_LIMIT)
        time.sleep(0.01)
        return GenerateResult(content="translated text", tokens=10)

    monkeypatch.setattr(ProviderClient, "generate", fake_generate)

    task = BatchTask(batch_num=1, total_batches=1, rows=[(1, "src 1")])
    stats = scheduler.run([task], total_rows=1)

    assert stats.ok == 1
    phase_names = [p[0] for p in phases]
    assert "generating" in phase_names
    assert "retry" in phase_names

    first_generating_idx = phase_names.index("generating")
    first_retry_idx = phase_names.index("retry", first_generating_idx)
    second_generating_idx = phase_names.index("generating", first_retry_idx)

    assert second_generating_idx > first_retry_idx

    retry_started_at = phases[first_retry_idx][1]
    second_generating_started_at = phases[second_generating_idx][1]
    assert second_generating_started_at >= retry_started_at


def test_retry_executor_reasoning_phase_transition(monkeypatch):
    """
    When reasoning is active, attempt transitions:
    think -> retry -> think
    """
    phases: list[str] = []

    def on_status(status: WorkerStatus) -> None:
        phases.append(status.phase)

    job = _job(max_attempts=2)
    job.reasoning_enabled = True

    scheduler = Scheduler(
        job=job,
        connection_pool=_StubPool(reasoning=True),
        base_url="http://primary.com",
        prompt_builder=lambda task: "prompt",
        parse_response=lambda raw, task, report=None: BatchResult(results={row: raw for row, _ in task.rows}),
        on_status=on_status,
        stop_event=threading.Event(),
    )

    attempt_count = 0

    def fake_generate(self, model, prompt, **kwargs):
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count == 1:
            raise ProviderError("Server error", status_code=500, retry_delay=0, kind=ProviderError.KIND_SERVER)
        return GenerateResult(content="translated text", tokens=10)

    monkeypatch.setattr(ProviderClient, "generate", fake_generate)

    task = BatchTask(batch_num=1, total_batches=1, rows=[(1, "src 1")])
    stats = scheduler.run([task], total_rows=1)

    assert stats.ok == 1
    assert "think" in phases
    assert "retry" in phases

    first_think = phases.index("think")
    first_retry = phases.index("retry", first_think)
    second_think = phases.index("think", first_retry)
    assert second_think > first_retry


def test_parse_reporter_phase_transitions():
    """
    _parse_reporter properly updates phase and started_at on kind transitions.
    """
    phases: list[tuple[str, float]] = []

    def on_status(status: WorkerStatus) -> None:
        phases.append((status.phase, status.started_at))

    scheduler = Scheduler(
        job=_job(),
        connection_pool=_StubPool(),
        base_url="http://primary.com",
        prompt_builder=lambda task: "prompt",
        parse_response=lambda raw, task, report=None: BatchResult(results={}),
        on_status=on_status,
        stop_event=threading.Event(),
    )

    task = BatchTask(batch_num=1, total_batches=1, rows=[(1, "src")])
    reporter = scheduler._parse_reporter(worker_id=1, task=task, generation=task.generation)

    reporter("retry")
    assert scheduler.worker_statuses()[0].phase == "retry"

    time.sleep(0.005)
    reporter("request")
    assert scheduler.worker_statuses()[0].phase == "generating"

    time.sleep(0.005)
    reporter("parse")
    assert scheduler.worker_statuses()[0].phase == "parsing"

    reported_phases = [p[0] for p in phases]
    assert reported_phases == ["retry", "generating", "parsing"]
    assert phases[1][1] >= phases[0][1]
    assert phases[2][1] >= phases[1][1]
