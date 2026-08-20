"""
scheduling/scheduler.py — Wave/batch execution loop.

Drives ConnectionPool -> KeyPool -> RetryExecutor -> ProviderClient in one of
three modes. v3.6's `adaptive` mode is deliberately not ported
(03_PROVIDER_SYSTEM.md §6):

  sequential  one batch at a time, `batch_delay` between them
  parallel    N workers pulling from a shared queue, each taking the next
              batch as soon as it is free (the default; unchanged)
  wave        N workers start a wave together and the next wave waits for all
              of them, so a cooldown is applied once per wave rather than once
              per worker (FEATURE-003)

Each mode is a *strategy object* rather than a branch in `run()`: a new mode is
then a new class, and the callback contract (`on_status`/`on_event`/`on_result`)
stays identical, so the feed and the Activity Panel never need to know which
mode is running (ADR-002).

The scheduler owns *pacing and orchestration only*. Prompt building and
response parsing are injected by the engine, so this module needs to know
nothing about Excel, prompts, placeholders or anti-desync — matching the
responsibility table in 01_ARCHITECTURE.md §6.

Nothing here prints: progress is surfaced through WorkerStatus/ScheduleEvent
callbacks for the TUI to render.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Literal

from .. import config
from ..core.models import BatchResult, JobConfig, ProcessStats
from ..providers.ratelimit import RateLimitPreset
from .connection_pool import AllConnectionsUnavailable, ConnectionPool
from .retry import ExecutionResult, RetryEvent, RetryExecutor


# ── Work units ────────────────────────────────────────────────────────────────

@dataclass
class BatchTask:
    batch_num:     int
    total_batches: int
    rows:          list[tuple[int, str]]
    is_repair:     bool = False
    context_map:   dict[int, str] | None = None
    extra:         dict = field(default_factory=dict)

    @property
    def size(self) -> int:
        return len(self.rows)

    @property
    def row_numbers(self) -> list[int]:
        return [row for row, _ in self.rows]


WorkerPhase = Literal[
    "idle", "waiting", "think", "generating", "parsing", "retry", "done", "failed"
]

# Phases whose duration is still running, so the panel's clock keeps ticking
# rather than freezing at the snapshot value.
_LIVE_PHASES = ("think", "generating", "parsing", "retry", "waiting")


@dataclass
class WorkerStatus:
    """
    One line of the Activity Panel (FEATURE-002 / 04_TUI_SPEC.md §4).

    `reasoning_active` is the *only* thing the panel learns about reasoning:
    it decides whether a `think` phase may be shown at all (FR-204). The
    reasoning content itself never reaches this record.
    """
    worker_id:    int
    phase:        WorkerPhase = "idle"
    connection:   str = ""
    batch_num:    int = 0
    total_batches: int = 0
    rows:         int = 0        # rows in the batch being processed
    wave_index:   int = 0        # which wave this batch belongs to (wave mode)
    reasoning_active: bool = False
    started_at:   float = 0.0    # when the current phase began
    elapsed:      float = 0.0
    message:      str = ""

    def snapshot(self) -> "WorkerStatus":
        copy = WorkerStatus(**self.__dict__)
        if self.started_at and self.phase in _LIVE_PHASES:
            copy.elapsed = time.time() - self.started_at
        return copy


@dataclass
class ScheduleEvent:
    kind:    Literal[
        "wave_start", "wave_done", "batch_done", "batch_failed", "retry", "aborted"
    ]
    message: str = ""
    batch_num: int = 0
    wave_index: int = 0
    worker_id: int = 0
    # Rows this event concerns, so the UI can attach the raw provider error to
    # the exact rows that failed rather than only to the batch.
    rows: list[int] = field(default_factory=list)
    # UET's classification of the failure (ProviderError.KIND_*), so the UI can
    # tell a temporary rate limit apart from a confirmed quota wall without
    # re-parsing the message. Empty for non-error events.
    error_kind: str = ""


# ── Stats ─────────────────────────────────────────────────────────────────────

class StatsAccumulator:
    """Thread-safe running totals (adapted from v3.6, minus adaptive hooks)."""

    def __init__(self, total: int) -> None:
        self._lock = threading.Lock()
        self.total = total
        self.ok = 0
        self.repaired = 0
        self.skipped = 0
        self.failed = 0
        self.tokens = 0

    def add_rows(self, ok: int, failed: int, repaired: bool = False) -> None:
        with self._lock:
            self.ok += ok
            self.failed += failed
            if repaired:
                self.repaired += ok

    def add_skipped(self, n: int) -> None:
        with self._lock:
            self.skipped += n

    def add_tokens(self, tokens: int) -> None:
        with self._lock:
            self.tokens += tokens

    def done(self) -> int:
        with self._lock:
            return self.ok + self.failed + self.skipped

    def to_process_stats(self, elapsed: float = 0.0) -> ProcessStats:
        with self._lock:
            return ProcessStats(
                total=self.total,
                ok=self.ok,
                repaired=self.repaired,
                skipped=self.skipped,
                failed=self.failed,
                elapsed=elapsed,
                tokens_consumed=self.tokens,
            )


# ── Scheduler ─────────────────────────────────────────────────────────────────

PromptBuilder = Callable[[BatchTask], str]
ResponseParser = Callable[[str, BatchTask], BatchResult]
ResultSink = Callable[[BatchTask, BatchResult], None]
SoloPromptBuilder = Callable[[int, BatchTask], str]
SoloParser = Callable[[str, int, BatchTask], str]
RawLogSink = Callable[[str, str, str | None, float], None]
ResponseFormatBuilder = Callable[[BatchTask], dict | None]


class Scheduler:
    """
    Runs a list of BatchTasks to completion.

    `prompt_builder` and `parse_response` are supplied by the engine
    (parsing/ modules in Phase 3); `on_result` receives each finished batch
    so the engine can hand rows to the async Excel writer.
    """

    def __init__(
        self,
        job: JobConfig,
        connection_pool: ConnectionPool,
        base_url: str,
        prompt_builder: PromptBuilder,
        parse_response: ResponseParser,
        on_result: ResultSink | None = None,
        on_status: Callable[[WorkerStatus], None] | None = None,
        on_event: Callable[[ScheduleEvent], None] | None = None,
        on_retry_event: Callable[[RetryEvent], None] | None = None,
        stop_event: threading.Event | None = None,
        solo_prompt_builder: SoloPromptBuilder | None = None,
        solo_parser: SoloParser | None = None,
        raw_log: RawLogSink | None = None,
        response_format_builder: ResponseFormatBuilder | None = None,
        on_batch_start: Callable[[BatchTask, int], None] | None = None,
    ) -> None:
        self._job = job
        self._pool = connection_pool
        self._base_url = base_url
        self._build_prompt = prompt_builder
        self._parse = parse_response
        self._on_result = on_result
        self._on_status = on_status
        self._on_event = on_event
        self._on_retry_event = on_retry_event
        self._stop = stop_event or threading.Event()
        self._build_solo_prompt = solo_prompt_builder
        self._parse_solo = solo_parser
        self._raw_log = raw_log
        self._response_format_for = response_format_builder
        self._on_batch_start = on_batch_start

        self._workers: dict[int, WorkerStatus] = {}
        self._workers_lock = threading.Lock()
        self._result_lock = threading.Lock()
        # Set once when the run is aborted because every connection is gone, so
        # parallel workers in flight do not each emit the same "aborted" event —
        # one clear stop, not one per worker.
        self._aborted = False
        # Consecutive whole-batch failures with no success in between. A run
        # where every request fails — provider down, content blocked, key
        # rejected — would otherwise march through the entire queue re-issuing
        # (and re-reporting) the same failure after each batch's retries are
        # spent. Past a threshold that is a systemic failure, not bad luck, so
        # the run is paused; any success resets it to zero.
        self._consecutive_failures = 0
        self._failure_lock = threading.Lock()

    # ── Pacing derived from the model's preset ────────────────────────────────

    @staticmethod
    def apply_preset_defaults(job: JobConfig, preset: RateLimitPreset) -> JobConfig:
        """
        Fill batch size / worker count / wave delay from the model's rate limit.

        Quick mode takes these wholesale; Full mode has already set explicit
        values, so only the pacing delays are derived (03_PROVIDER_SYSTEM.md §6).

        The worker count comes from `workers_for(batch)` rather than from the
        preset's cached figure, because the batch may not be the one the preset
        was derived at — and how many requests a TPM allowance affords depends
        entirely on how big each request is. Asking with the batch actually in
        use is the difference between pacing a run correctly and hoping.

        The mode Quick mode lands on is the *saved default* (Settings), not a
        fixed choice: someone who set `wave` as their default asked for their
        quick runs to be paced that way (FR-303/AC-303). A single worker still
        collapses to sequential, because a one-worker wave is a wave of one.
        """
        if job.mode == "quick":
            job.batch_size = preset.suggested_batch_size
            job.n_workers = preset.workers_for(job.batch_size, job.tok_per_row)
            job.processing_mode = _quick_mode(job.n_workers)
        job.wave_delay = preset.wave_delay
        job.batch_delay = round(preset.delay_per_request, 3)
        return job

    def wave_size(self) -> int:
        workers = self._effective_workers()
        return max(1, self._job.batch_size) * max(1, workers)

    def _effective_workers(self) -> int:
        if self._job.processing_mode == "sequential":
            return 1
        return max(1, self._job.n_workers)

    # ── Run ───────────────────────────────────────────────────────────────────

    def run(self, tasks: list[BatchTask], total_rows: int = 0) -> ProcessStats:
        stats = StatsAccumulator(total_rows or sum(t.size for t in tasks))
        if not tasks:
            return stats.to_process_stats()

        self._pool.set_wave_size(self.wave_size())
        started = time.time()

        self._strategy().run(tasks, stats)

        return stats.to_process_stats(elapsed=time.time() - started)

    def _strategy(self) -> "ExecutionStrategy":
        """
        The strategy for this job's mode (ADR-002).

        One worker means sequential whatever the mode says — a parallel run
        with a single worker *is* a sequential run, and saying so here keeps
        the strategies themselves free of that special case. `wave` is exempt:
        a one-worker wave still applies its per-wave cooldown, which is the
        whole reason someone picks it (FR-301 business rule 1).
        """
        mode = self._job.processing_mode
        if mode == "wave":
            return WaveStrategy(self)
        if mode == "sequential" or self._effective_workers() == 1:
            return SequentialStrategy(self)
        return ParallelStrategy(self)

    # ── One batch ─────────────────────────────────────────────────────────────

    def _process_task(
        self,
        worker_id: int,
        task: BatchTask,
        executor: RetryExecutor,
        stats: StatsAccumulator,
    ) -> None:
        self._set_status(
            worker_id,
            phase="generating",
            batch_num=task.batch_num,
            total_batches=task.total_batches,
            rows=task.size,
            wave_index=self._pool.wave_index,
            reasoning_active=False,
            started_at=time.time(),
            message="",
        )
        if self._on_batch_start is not None:
            self._on_batch_start(task, worker_id)

        label = f"batch {task.batch_num}/{task.total_batches}"
        prompt = self._build_prompt(task)
        try:
            outcome: ExecutionResult = executor.execute(
                prompt, label=label, rows=task.size, use_repair_model=task.is_repair,
                response_format=self._response_format(task),
            )
        except AllConnectionsUnavailable as exc:
            # Every connection is genuinely gone. Stop the whole run and announce
            # it exactly once — not once per worker still in flight, which is the
            # duplicate-error cascade. Later workers see `_stop` and exit quietly.
            self._fail_task(worker_id, task, stats, str(exc), announce=False)
            self._stop.set()
            with self._workers_lock:
                first = not self._aborted
                self._aborted = True
            if first:
                self._emit_event("aborted", str(exc), task.batch_num, worker_id=worker_id)
            return

        stats.add_tokens(outcome.tokens)
        self._set_status(worker_id, connection=outcome.connection)
        self._log_raw(label, prompt, outcome.content, outcome.latency)

        if outcome.content is None:
            self._fail_task(worker_id, task, stats, outcome.error,
                            error_kind=getattr(outcome, "error_kind", ""))
            self._note_failure(task, worker_id, outcome.error,
                               getattr(outcome, "error_kind", ""))
            return

        self._set_status(worker_id, phase="parsing", started_at=time.time())
        try:
            result = self._parse(outcome.content, task)
        except Exception as exc:
            self._fail_task(worker_id, task, stats, f"parse error: {exc}")
            self._note_failure(task, worker_id, f"parse error: {exc}", "")
            return

        # Recovery ladder: mini-batch the rows that didn't come back, then
        # solo-retry whatever is still missing (1 row per request is
        # shift-proof). Same ladder as v3.6, now driven by BatchResult.
        missing = [rn for rn in result.failed_rows if rn not in result.results]
        if missing:
            self._recover_missing(worker_id, task, missing, result, executor, stats)

        ok_rows = sum(1 for text in result.results.values() if text)
        failed_rows = task.size - ok_rows
        stats.add_rows(ok=ok_rows, failed=failed_rows, repaired=task.is_repair)

        if self._on_result is not None:
            with self._result_lock:
                self._on_result(task, result)

        self._set_status(
            worker_id,
            phase="done",
            message=f"{ok_rows}/{task.size} rows in {outcome.latency:.1f}s",
        )
        self._emit_event(
            "batch_done",
            f"batch {task.batch_num}: {ok_rows}/{task.size} ok",
            task.batch_num,
            worker_id=worker_id,
        )
        # A batch that produced at least one row is real progress: the provider
        # is answering, so the circuit-breaker counter resets. A batch that
        # recovered nothing counts as a failure towards the pause threshold.
        if ok_rows > 0:
            self._note_success()
        else:
            self._note_failure(task, worker_id, outcome.error or "no rows returned",
                               getattr(outcome, "error_kind", ""))


    def _recover_missing(
        self,
        worker_id: int,
        task: BatchTask,
        missing: list[int],
        result: BatchResult,
        executor: RetryExecutor,
        stats: StatsAccumulator,
    ) -> None:
        """Mini-batch retry, then per-row solo retry for anything still absent."""
        label = f"batch {task.batch_num}/{task.total_batches}"
        self._emit_event(
            "retry", f"{len(missing)} row(s) missing -> mini-batch retry",
            task.batch_num, worker_id=worker_id,
        )

        mini_task = BatchTask(
            batch_num=task.batch_num,
            total_batches=task.total_batches,
            rows=[row for row in task.rows if row[0] in set(missing)],
            is_repair=task.is_repair,
            context_map=task.context_map,
            extra=task.extra,
        )
        mini_prompt = self._build_prompt(mini_task)
        mini = executor.execute(
            mini_prompt, label=f"mini-{label}", rows=0, use_repair_model=task.is_repair,
            response_format=self._response_format(mini_task),
        )
        stats.add_tokens(mini.tokens)
        self._log_raw(f"mini-{label}", mini_prompt, mini.content, mini.latency)

        if mini.content is not None:
            try:
                mini_result = self._parse(mini.content, mini_task)
                result.results.update(
                    {rn: text for rn, text in mini_result.results.items() if text}
                )
            except Exception as exc:
                # Don't swallow it — the rows fall through to solo retry, but
                # the user still needs to see why the mini-batch was useless.
                self._emit_event(
                    "retry", f"mini-batch parse failed: {exc}",
                    task.batch_num, worker_id=worker_id, rows=list(missing),
                )
        elif mini.error:
            self._emit_event(
                "retry", f"mini-batch failed: {mini.error}",
                task.batch_num, worker_id=worker_id, rows=list(missing),
            )

        still_missing = [rn for rn in missing if not result.results.get(rn)]
        if not still_missing:
            result.failed_rows = []
            return
        if self._build_solo_prompt is None:
            result.failed_rows = still_missing
            return

        self._emit_event(
            "retry", f"{len(still_missing)} row(s) -> solo retry",
            task.batch_num, worker_id=worker_id,
        )
        unresolved: list[int] = []
        for row_num in still_missing:
            if self._stop.is_set():
                unresolved.append(row_num)
                continue
            solo_prompt = self._build_solo_prompt(row_num, task)
            solo = executor.execute(
                solo_prompt, label=f"solo {row_num}", rows=0,
                use_repair_model=task.is_repair,
            )
            stats.add_tokens(solo.tokens)
            self._log_raw(f"solo {row_num}", solo_prompt, solo.content, solo.latency)
            text = ""
            if solo.content is not None and self._parse_solo is not None:
                try:
                    text = self._parse_solo(solo.content, row_num, task)
                except Exception as exc:
                    self._emit_event(
                        "batch_failed", f"row {row_num}: solo parse failed: {exc}",
                        task.batch_num, worker_id=worker_id, rows=[row_num],
                    )
            if text:
                result.results[row_num] = text
            else:
                unresolved.append(row_num)
                if solo.error:
                    # The last thing that went wrong for this specific row.
                    self._emit_event(
                        "batch_failed", f"row {row_num}: {solo.error}",
                        task.batch_num, worker_id=worker_id, rows=[row_num],
                    )

        result.failed_rows = unresolved

    def _response_format(self, task: BatchTask) -> dict | None:
        """Structured output, only when the model explicitly opted in."""
        if self._job.prompt_format != "json" or self._response_format_for is None:
            return None
        return self._response_format_for(task)

    def _log_raw(self, label: str, prompt: str, response: str | None, latency: float) -> None:
        if self._raw_log is not None:
            self._raw_log(label, prompt, response, latency)

    def _fail_task(
        self, worker_id: int, task: BatchTask, stats: StatsAccumulator,
        error: str, announce: bool = True, error_kind: str = "",
    ) -> None:
        stats.add_rows(ok=0, failed=task.size)
        if self._on_result is not None:
            with self._result_lock:
                self._on_result(task, BatchResult(failed_rows=list(task.row_numbers)))
        self._set_status(worker_id, phase="failed", message=error)
        # `announce=False` when the caller will emit its own single aborted
        # event: a per-batch `batch_failed` on top of that is the duplicate the
        # UI must not receive.
        if announce:
            self._emit_event(
                "batch_failed", error or "batch failed", task.batch_num,
                worker_id=worker_id, rows=list(task.row_numbers),
                error_kind=error_kind,
            )

    # ── Circuit breaker ───────────────────────────────────────────────────────

    def _note_success(self) -> None:
        """A batch produced rows — the provider is answering, reset the counter."""
        with self._failure_lock:
            self._consecutive_failures = 0

    def _note_failure(
        self, task: BatchTask, worker_id: int, error: str, error_kind: str
    ) -> None:
        """
        A whole batch failed after its retries were spent.

        Past `MAX_CONSECUTIVE_FAILURES` in a row with no success in between, the
        failure is systemic (provider down, content blocked, bad key) rather than
        an isolated flaky request, so the run is *paused* rather than left to
        plough through the rest of the queue reissuing the same doomed request
        and reprinting the same error. The stop is announced once, on the same
        `aborted` channel `AllConnectionsUnavailable` uses, so the screen already
        knows how to move to Paused and keep the progress already written.
        """
        with self._failure_lock:
            self._consecutive_failures += 1
            tripped = self._consecutive_failures >= config.MAX_CONSECUTIVE_FAILURES
        if not tripped or self._stop.is_set():
            return
        self._stop.set()
        with self._workers_lock:
            first = not self._aborted
            self._aborted = True
        if first:
            reason = error or "repeated request failures"
            self._emit_event(
                "aborted",
                f"Paused after {config.MAX_CONSECUTIVE_FAILURES} consecutive "
                f"failures — {reason}",
                task.batch_num,
                worker_id=worker_id,
                error_kind=error_kind,
            )

    # ── Plumbing ──────────────────────────────────────────────────────────────

    def _make_executor(self, worker_id: int) -> RetryExecutor:
        def on_event(event: RetryEvent) -> None:
            # Surface which connection is serving this worker as soon as the
            # lease is taken, not only when the batch finishes.
            if event.connection:
                self._set_status(worker_id, connection=event.connection)
            if event.kind in ("rate_limit", "cooldown", "server_error", "error"):
                self._emit_event(
                    "retry",
                    f"{event.kind.replace('_', ' ')}: {event.message or event.label}",
                    worker_id=worker_id,
                )
            if self._on_retry_event is not None:
                self._on_retry_event(event)

        def on_phase(phase: str, reasoning_active: bool) -> None:
            # The executor is the only layer that knows whether this request
            # will make the model think, because it is the only one holding the
            # ModelEntry. `think` is a phase, never content (FR-204/FR-205).
            self._set_status(
                worker_id,
                phase=phase,                       # type: ignore[arg-type]
                reasoning_active=reasoning_active,
                started_at=time.time(),
            )

        return RetryExecutor(
            connection_pool=self._pool,
            job=self._job,
            base_url=self._base_url,
            on_event=on_event,
            stop_event=self._stop,
            on_phase=on_phase,
        )

    def _pace(self, seconds: float) -> None:
        if seconds > 0:
            self._stop.wait(timeout=seconds)

    def worker_statuses(self) -> list[WorkerStatus]:
        with self._workers_lock:
            return [status.snapshot() for status in self._workers.values()]

    def _set_status(self, worker_id: int, **fields) -> None:
        with self._workers_lock:
            status = self._workers.get(worker_id)
            if status is None:
                status = WorkerStatus(worker_id=worker_id)
                self._workers[worker_id] = status
            for name, value in fields.items():
                setattr(status, name, value)
            snapshot = status.snapshot()
        if self._on_status is not None:
            self._on_status(snapshot)

    def _emit_event(
        self,
        kind: str,
        message: str = "",
        batch_num: int = 0,
        worker_id: int = 0,
        rows: list[int] | None = None,
        error_kind: str = "",
    ) -> None:
        if self._on_event is not None:
            self._on_event(
                ScheduleEvent(
                    kind=kind,           # type: ignore[arg-type]
                    message=message,
                    batch_num=batch_num,
                    wave_index=self._pool.wave_index,
                    worker_id=worker_id,
                    rows=list(rows or []),
                    error_kind=error_kind,
                )
            )

    def request_stop(self) -> None:
        self._stop.set()


# ── Execution strategies (ADR-002) ────────────────────────────────────────────

class ExecutionStrategy:
    """
    One way of getting through a list of batches.

    A strategy borrows the scheduler's machinery — `_process_task`,
    `_make_executor`, `_pace`, `_set_status`, `_emit_event` — rather than
    reimplementing any of it, so every mode produces the same callbacks, the
    same recovery ladder and the same stats. What differs between modes is only
    *who runs what, when*.
    """

    def __init__(self, scheduler: Scheduler) -> None:
        self._s = scheduler

    def run(self, tasks: list[BatchTask], stats: StatsAccumulator) -> None:
        raise NotImplementedError


class SequentialStrategy(ExecutionStrategy):
    """One batch at a time, `batch_delay` between them."""

    def run(self, tasks: list[BatchTask], stats: StatsAccumulator) -> None:
        scheduler = self._s
        executor = scheduler._make_executor(1)
        for index, task in enumerate(tasks):
            if scheduler._stop.is_set():
                scheduler._emit_event("aborted", "Cancelled by user.")
                return
            scheduler._process_task(1, task, executor, stats)
            if index < len(tasks) - 1:
                scheduler._pace(scheduler._job.batch_delay)


class ParallelStrategy(ExecutionStrategy):
    """
    N workers pulling from one shared queue — the existing behaviour, moved
    rather than rewritten. Each worker takes the next batch the moment it is
    free, so a slow batch never holds the others up.
    """

    def run(self, tasks: list[BatchTask], stats: StatsAccumulator) -> None:
        scheduler = self._s
        workers = scheduler._effective_workers()
        task_queue: queue.Queue[BatchTask | None] = queue.Queue()
        for task in tasks:
            task_queue.put(task)
        for _ in range(workers):
            task_queue.put(None)

        threads = [
            threading.Thread(
                target=self._worker_loop,
                args=(worker_id, task_queue, stats),
                name=f"uet-worker-{worker_id}",
                daemon=True,
            )
            for worker_id in range(1, workers + 1)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    def _worker_loop(
        self,
        worker_id: int,
        task_queue: "queue.Queue[BatchTask | None]",
        stats: StatsAccumulator,
    ) -> None:
        # One executor per worker: no shared mutable state between workers,
        # same property v3.6 got from DedicatedKeyExecutor.
        scheduler = self._s
        executor = scheduler._make_executor(worker_id)
        while not scheduler._stop.is_set():
            task = task_queue.get()
            if task is None:
                break
            scheduler._process_task(worker_id, task, executor, stats)
            scheduler._pace(scheduler._job.wave_delay)
        scheduler._set_status(worker_id, phase="idle", message="")


class WaveStrategy(ExecutionStrategy):
    """
    Lockstep waves: every worker starts a batch together and the next wave
    waits for all of them (FEATURE-003 FR-302).

    The point is the cooldown. In parallel mode each worker paces itself, so N
    workers apply N delays at N different moments and a rate limit is hit at an
    unpredictable time. Here the delay happens once, between waves, when
    nothing is in flight — which is both easier to reason about and easier on a
    provider that counts requests per minute.

    The cost is real and deliberate: one slow batch holds its whole wave. That
    trade is the reason the mode is opt-in and `parallel` stays the default.

    Executors are created once per worker slot rather than once per wave, so a
    worker keeps its connection lease and client across waves exactly as it
    does in parallel mode.
    """

    def run(self, tasks: list[BatchTask], stats: StatsAccumulator) -> None:
        scheduler = self._s
        workers = scheduler._effective_workers()
        waves = split_waves(tasks, workers)
        executors = {
            worker_id: scheduler._make_executor(worker_id)
            for worker_id in range(1, workers + 1)
        }

        for wave_number, wave in enumerate(waves, start=1):
            if scheduler._stop.is_set():
                scheduler._emit_event("aborted", "Cancelled by user.")
                return

            scheduler._emit_event(
                "wave_start",
                f"wave {wave_number}/{len(waves)}: {len(wave)} batch(es)",
                wave[0].batch_num,
            )
            # Workers not in this wave are waiting, and should say so rather
            # than showing a stale `done` from the previous wave.
            for worker_id in range(len(wave) + 1, workers + 1):
                scheduler._set_status(
                    worker_id, phase="waiting", message="next wave",
                    started_at=time.time(),
                )

            threads = [
                threading.Thread(
                    target=scheduler._process_task,
                    args=(worker_id, task, executors[worker_id], stats),
                    name=f"uet-worker-{worker_id}",
                    daemon=True,
                )
                for worker_id, task in enumerate(wave, start=1)
            ]
            for thread in threads:
                thread.start()
            # The lockstep itself: nothing starts until every worker is done.
            for thread in threads:
                thread.join()

            scheduler._emit_event(
                "wave_done",
                f"wave {wave_number}/{len(waves)} complete",
                wave[-1].batch_num,
            )

            if wave_number < len(waves) and not scheduler._stop.is_set():
                # One delay for the whole wave, not one per worker (AC-302).
                for worker_id in range(1, workers + 1):
                    scheduler._set_status(
                        worker_id, phase="waiting", message="wave cooldown",
                        started_at=time.time(),
                    )
                scheduler._pace(scheduler._job.wave_delay)

        for worker_id in range(1, workers + 1):
            scheduler._set_status(worker_id, phase="idle", message="")


def split_waves(tasks: list, n_workers: int) -> list[list]:
    """
    Split batches into waves of at most `n_workers`, order preserved.

    Exposed as a plain function so the split can be tested without a provider,
    a pool or a thread: `split_waves(range(10), 3)` → sizes `[3, 3, 3, 1]`
    (AC-301). A worker count below 1 is treated as 1 rather than refused —
    a clamp belongs closer to the UI than to arithmetic.
    """
    size = max(1, int(n_workers or 1))
    return [list(tasks[i:i + size]) for i in range(0, len(tasks), size)]


def _quick_mode(workers: int) -> str:
    """
    The processing mode a Quick run uses.

    Read from the saved default (Settings) so `wave` survives into Quick mode
    (AC-303), and collapsed to sequential when the rate limit only affords one
    worker. Never raises: prefs are a convenience and a run must not depend on
    them being readable.
    """
    if workers <= 1:
        return "sequential"
    try:
        from ..data.app_prefs import default_processing_mode

        mode = default_processing_mode()
    except Exception:
        mode = config.DEFAULT_PROCESSING_MODE
    return mode if mode in ("sequential", "parallel", "wave") else "parallel"


def build_batches(
    rows: list[tuple[int, str]],
    batch_size: int,
    is_repair: bool = False,
    context_map: dict[int, str] | None = None,
) -> list[BatchTask]:
    """Split rows into BatchTasks. Pure helper — the engine calls this."""
    size = max(1, batch_size)
    chunks = [rows[i:i + size] for i in range(0, len(rows), size)]
    total = len(chunks)
    return [
        BatchTask(
            batch_num=index + 1,
            total_batches=total,
            rows=chunk,
            is_repair=is_repair,
            context_map=context_map,
        )
        for index, chunk in enumerate(chunks)
    ]
