"""
tests/test_format_retry_phase.py — Honest phases for the Judge format-retry ladder.

Pins the two properties the "parsing 250s" bug was made of:

  1. While a format-retry request is in flight, the worker's phase flips to a
     live "generating" (sending) phase with a fresh timer, and back to
     "parsing" when the reply lands — the panel never again shows one ever-
     growing "parsing" counter across whole hidden requests.
  2. A format-retry request runs a SINGLE attempt: the executor's inner retry
     loop is switched off for it, so one visible "retry" is bounded by one
     request timeout instead of three hidden attempts plus cooldown parks.

Everything provider-shaped is stubbed at the scheduler boundary (no network,
no pool): the scheduler is driven through the same `run()` the judge path
uses, with `_make_executor` patched to hand out scripted executors.
"""

from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.core.models import BatchResult, JobConfig
from translator.scheduling.scheduler import BatchTask, Scheduler


# ── Harness (same shape as test_scheduler_watchdog.py) ────────────────────────

class _StubPool:
    def __init__(self) -> None:
        self.wave_index = 0

    def set_wave_size(self, _size: int) -> None:
        pass


class _ExecutionResult:
    def __init__(self, content: str | None) -> None:
        self.content = content
        self.tokens = 0
        self.latency = 0.0
        self.attempts = 1
        self.connection = "stub"
        self.error = "" if content is not None else "stub error"
        self.error_kind = ""


class _ReplyThenProse:
    """
    Executor that always answers — the parse side does the retrying.

    The parse callback below is what stands in for the Judge ladder: on every
    batch it calls `report("request")`, performs a stub re-ask, then
    `report("parse")`, exactly the way `JudgeRunner._format_retry` does.
    """

    def execute(self, prompt, label="", rows=0, use_repair_model=False,
                response_format=None):
        return _ExecutionResult("content")


def _job() -> JobConfig:
    return JobConfig(
        provider_id="p", model_id="m", connection_id="",
        src_path="x", out_path="y",
        batch_size=1, n_workers=1, processing_mode="sequential",
        max_attempts=1, retry_mode="fixed",
        wave_delay=0, batch_delay=0,
    )


def _tasks(n: int) -> list[BatchTask]:
    return [
        BatchTask(batch_num=i + 1, total_batches=n, rows=[(i + 1, f"row {i+1}")])
        for i in range(n)
    ]


# ── 1. Phase flips and the timer re-anchors during a format-retry ─────────────

def test_report_flips_phase_and_resets_timer():
    phases: list[tuple[str, float]] = []          # (phase, started_at)

    def parse(raw: str, task: BatchTask, report=None) -> BatchResult:
        # The ladder's shape: report("request") before the re-ask,
        # report("parse") when the reply is being parsed again — exactly the
        # way JudgeRunner._format_retry does it.
        if report is not None:
            report("request")
            report("parse")
        return BatchResult(results={row: raw for row, _src in task.rows})

    def on_status(status) -> None:
        phases.append((status.phase, status.started_at))

    scheduler = Scheduler(
        job=_job(),
        connection_pool=_StubPool(),       # type: ignore[arg-type]
        base_url="",
        prompt_builder=lambda task: "prompt",
        parse_response=parse,
        on_status=on_status,
        stop_event=threading.Event(),
    )
    scheduler._make_executor = lambda worker_id: _ReplyThenProse()  # type: ignore[method-assign]
    scheduler.run(_tasks(1), total_rows=1)

    # The panel saw: generating (the first request), parsing (its reply),
    # then generating again (the format-retry request) with a NEW timer, and
    # parsing once more — never one ever-growing "parsing" counter.
    sequence = [phase for phase, _started in phases]
    first_parsing = sequence.index("parsing")
    retry_generating = sequence.index("generating", first_parsing + 1)
    assert sequence.index("parsing", retry_generating + 1) > retry_generating

    # The generating report re-anchored the timer: the retry's "parsing" is
    # newer than the "generating" that preceded it.
    retry_parsing_started = [
        started for phase, started in phases[first_parsing + 1:]
        if phase == "parsing"
    ][0]
    generating_started = [
        started for phase, started in phases[first_parsing + 1:]
        if phase == "generating"
    ][0]
    assert retry_parsing_started >= generating_started


def test_report_is_none_safe():
    """A parser wired with no reporter (translate path) must work unchanged."""
    def parse(raw: str, task: BatchTask, report=None) -> BatchResult:
        return BatchResult(results={row: raw for row, _src in task.rows})

    scheduler = Scheduler(
        job=_job(),
        connection_pool=_StubPool(),       # type: ignore[arg-type]
        base_url="",
        prompt_builder=lambda task: "prompt",
        parse_response=parse,
        stop_event=threading.Event(),
    )
    scheduler._make_executor = lambda worker_id: _ReplyThenProse()  # type: ignore[method-assign]
    stats = scheduler.run(_tasks(1), total_rows=1)
    assert stats.ok == 1


def test_stale_generation_report_is_ignored():
    """A late report from a reassigned batch must not repaint the worker."""

    def parse(raw: str, task: BatchTask, report=None) -> BatchResult:
        # Simulate the watchdog reassigning the batch *during* the parse: the
        # task's generation moves on, so the reporter bound to the old
        # generation must become a no-op.
        task.generation += 1
        if report is not None:
            report("request")          # stale by construction
        return BatchResult(results={row: raw for row, _src in task.rows})

    phases: list[str] = []

    def on_status(status) -> None:
        phases.append(status.phase)

    scheduler = Scheduler(
        job=_job(),
        connection_pool=_StubPool(),       # type: ignore[arg-type]
        base_url="",
        prompt_builder=lambda task: "prompt",
        parse_response=parse,
        on_status=on_status,
        stop_event=threading.Event(),
    )
    scheduler._make_executor = lambda worker_id: _ReplyThenProse()  # type: ignore[method-assign]
    scheduler.run(_tasks(1), total_rows=1)

    # Once the batch enters "parsing", a stale report must not flip the phase
    # back to "generating". (Snapshots before that may legitimately repeat
    # "generating" — e.g. the connection update after the reply arrives —
    # which is why the check is anchored on the first "parsing".)
    first_parsing = phases.index("parsing")
    assert "generating" not in phases[first_parsing:]


# ── 2. Format-retry requests are single-attempt ───────────────────────────────

def test_single_attempt_job_disables_inner_loop():
    """JudgeRunner._single_attempt_job: one attempt, no cooldown mode."""
    import dataclasses

    from translator.judge.runner import JudgeRunner, JudgeTarget

    runner = JudgeRunner(
        target=JudgeTarget(path="", src_col=1, dst_col=2),
        criteria=["mistranslation"],
        job=JobConfig(
            provider_id="p", model_id="m",
            max_attempts=3, retry_mode="cooldown",
        ),
    )
    single = runner._single_attempt_job()
    assert single.max_attempts == 1
    assert single.retry_mode == "fixed"
    # Everything else is untouched — it is the same job, not a new opinion.
    assert dataclasses.astuple(single)[0:2] == dataclasses.astuple(runner._job)[0:2]
    assert single.model_id == runner._job.model_id
    assert single.temperature == runner._job.temperature
