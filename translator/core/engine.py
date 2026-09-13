"""
core/engine.py — ProcessingEngine.

Thin orchestrator, exactly as in v3.6: load data, filter for resume, build
tasks, run the scheduler, collect stats. No retry logic, no parse logic, no
write logic lives here — those belong to scheduling/, parsing/ and io/.

Adapted from v3.6's engine.py. What changed:
  * provider/model come from the registry (ConnectionPool), not a hardcoded
    provider adapter,
  * the parse + anti-desync step is injected into the scheduler instead of
    living inside it,
  * nothing prints; progress is reported through callbacks.

**How many rows a job is about to do is announced before the first request**
(BUG-001). Every entry point classifies its own rows — a retranslate queues only
the empty/`FAIL_MARKER`/too-short ones — so the number a progress bar has to
count against is knowable here and nowhere else. It travels on the existing
`on_notice` channel (`kind="plan"`) with the figure also readable as
`planned_rows`, rather than on a callback invented for it, and it is announced
once per job: the auto-sweep is part of the run that spawned it, so it must not
move the denominator halfway through.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from typing import Callable, TextIO

from .. import config
from ..data import app_prefs
from ..io.excel_io import ExcelReader, ExcelWriter, prepare_output
from ..logging_.vlogging import RawLogger
from ..parsing.alignment import validate_alignment
from ..parsing.placeholders import (
    collect_source_symbols,
    decode_placeholders,
    strip_runaway,
    symbol_diff_sanitize,
)
from ..parsing.prompts import (
    build_repair_prompt,
    build_repair_solo_prompt,
    build_translate_prompt,
    build_translate_solo_prompt,
)
from ..parsing.recovery import ParseRecoveryPipeline, build_json_schema, parse_json_response
from ..providers.models import Provider
from ..scheduling.connection_pool import ConnectionPool
from ..scheduling.scheduler import (
    BatchTask,
    ScheduleEvent,
    Scheduler,
    WorkerStatus,
    build_batches,
)
from .models import BatchResult, JobConfig, ProcessStats

MAX_SWEEP_PASSES = 2

EngineNotice = Callable[[str, str], None]     # (kind, message)


class ProcessingEngine:
    """Coordinates one translate / repair / retranslate job."""

    def __init__(
        self,
        job: JobConfig,
        provider: Provider | None,
        connection_pool: ConnectionPool | None = None,
        on_status: Callable[[WorkerStatus], None] | None = None,
        on_event: Callable[[ScheduleEvent], None] | None = None,
        on_notice: EngineNotice | None = None,
        on_batch_start: Callable[[BatchTask, int], None] | None = None,
        on_result: Callable[[BatchTask, BatchResult], None] | None = None,
        stop_event: threading.Event | None = None,
        registry=None,
    ) -> None:
        self._job = job
        self._provider = provider
        self._registry = registry
        self._on_status = on_status
        self._on_event = on_event
        self._on_notice = on_notice
        self._on_batch_start = on_batch_start
        self._on_result_hook = on_result
        self._stop = stop_event or threading.Event()

        if connection_pool is not None:
            self._pool = connection_pool
        elif provider is not None:
            self._pool = ConnectionPool(
                provider,
                connection_ids=[job.connection_id] if job.connection_id else None,
            )
        else:
            # A combo job has no single provider, so the pool has to be supplied
            # by the caller that holds the registry (`scheduling/pools.py` builds
            # it). Refusing here beats running against a pool that does not exist.
            raise ValueError(
                "ProcessingEngine needs either a provider or a connection pool."
            )

        self._parser = ParseRecoveryPipeline()
        # Seed the output file before the writer opens it: results go to
        # <name>_Translated.<ext> and the source is never written to. An
        # existing output is kept so its filled rows drive resume.
        if job.out_path and job.src_path:
            self._resumed_output, _ = prepare_output(job.src_path, job.out_path)
        else:
            self._resumed_output = False
        self._writer = ExcelWriter(
            job.out_path,
            mode=job.write_mode,
            buffer_size=job.buffer_size,
            on_event=self._notice,
            fallback_range=app_prefs.fallback_save_range(),
        )
        self._log_file: TextIO | None = self._open_log()
        self._raw_logger: RawLogger | None = self._open_raw_log()
        self._dst_col = job.dst_col
        self._source_symbols: set[str] = set()
        # How many rows this job actually queued, announced once before the first
        # request goes out (BUG-001). `None` until then: a screen that has not
        # been told yet has to fall back to the range it asked for rather than
        # show a denominator of zero.
        self._planned_rows: int | None = None
        # Full-file broadcast sync (unique → full) is far too expensive to run
        # on every batch completion — a 124k-row target takes seconds to load
        # and rewrite, which blocked the worker and showed up as a fake
        # "parsing 6s". Throttled to a background thread instead; `_finish`
        # always runs one final synchronous pass so nothing is lost.
        self._sync_lock = threading.Lock()
        self._syncing = False
        self._last_sync = 0.0
        self._sync_thread: threading.Thread | None = None

    @property
    def planned_rows(self) -> int | None:
        """
        The size of the work queue, or None before it is known.

        Read by the run screen when the `plan` notice arrives, so the progress
        bar counts against the rows that were classified into the queue rather
        than every row in the file.
        """
        return self._planned_rows

    def _output_path(self) -> str:
        """
        Where results are actually being written.

        Equals `job.out_path` until the target turned out to be locked and the
        writer fell back to a numbered sibling. Every pass that re-reads what was
        written — resume, auto-sweep, the repair phase — must read from here, or
        it would rescan the original file the run has stopped writing to and see
        stale (or empty) cells.
        """
        return self._writer.active_path

    @property
    def output_path(self) -> str:
        """
        The file this job's results ended up in, for the summary to report.

        The same as the requested `out_path` unless a locked target forced a
        fallback to a numbered sibling, in which case telling the user the
        original name would send them to a file that never received the run.
        """
        return self._job.full_out_path or self._writer.active_path

    def _announce_plan(self, rows: int) -> None:
        """
        State the queue size once, on the notice channel that already exists.

        Once per engine, deliberately: the auto-sweep runs a second
        classification pass inside the same job, and letting it re-announce
        would move the denominator under a bar that is already half full
        (AC-605).
        """
        if self._planned_rows is not None:
            return
        self._planned_rows = max(0, int(rows))
        self._notice("plan", f"{self._planned_rows} row(s) queued for this job.")

    # ── Public entry points ───────────────────────────────────────────────────

    def run_translate(self) -> ProcessStats:
        job = self._job
        if self._resumed_output:
            self._notice(
                "resume",
                f"Continuing {os.path.basename(job.out_path)} — "
                f"rows already translated there will be skipped.",
            )
        source_rows = ExcelReader.load_column(
            job.src_path, job.src_col, job.start_row, job.num_rows
        )

        context_map: dict[int, str] | None = None
        if job.op_mode == "translate_context" and job.ctx_col:
            context_map = dict(
                ExcelReader.load_column(job.src_path, job.ctx_col, job.start_row, job.num_rows)
            )

        to_process = [(rn, txt) for rn, txt in source_rows if txt.strip()]
        # The population this job is about: rows that actually carry something to
        # translate. A row with an empty source was never a translation, so
        # counting it as "skipped" would inflate both figures (BUG-001).
        translatable = len(to_process)

        # Resume: skip rows already filled, but never skip FAIL_MARKER rows —
        # those must be retried on every re-run.
        if job.skip_filled:
            dst_values = dict(
                ExcelReader.load_column(
                    job.out_path, job.dst_col, job.start_row, job.num_rows
                )
            )
            filled = {
                rn for rn, val in dst_values.items()
                if val.strip() and val.strip() != config.FAIL_MARKER
            }
            skipped = [rn for rn, _ in to_process if rn in filled]
            to_process = [(rn, txt) for rn, txt in to_process if rn not in filled]
            if skipped:
                self._notice("resume", f"{len(skipped)} row(s) already done — skipped.")

        if not to_process:
            self._notice("info", "Nothing to process.")
            self._announce_plan(0)
            stats = ProcessStats(total=translatable, skipped=translatable)
            self._finish(stats)
            return stats

        # Announced before the first request: the queue, not the range.
        self._announce_plan(len(to_process))
        stats = self._run_translate_rows(
            to_process, context_map, dst_col=job.dst_col, total=translatable
        )
        stats.skipped = translatable - len(to_process)

        # Auto-sweep: recover empty / FAIL_MARKER rows left behind.
        for sweep_pass in range(1, MAX_SWEEP_PASSES + 1):
            if stats.failed <= 0 or self._stop.is_set():
                break
            self._notice(
                "sweep",
                f"Auto-sweep pass {sweep_pass}/{MAX_SWEEP_PASSES}: "
                f"{stats.failed} failed row(s) — rescanning.",
            )
            # The sweep re-reads the output file, so pending async writes must
            # be on disk first or it would rescan stale cells.
            self._writer.sync()
            sweep_stats, _ = self.run_retranslate_failed(
                include_too_short=False, close_log=False
            )
            if sweep_stats.ok > 0:
                stats.ok += sweep_stats.ok
                stats.failed = max(0, stats.failed - sweep_stats.ok)
                self._notice("sweep", f"Auto-sweep recovered {sweep_stats.ok} row(s).")
            else:
                self._notice("sweep", "Auto-sweep recovered nothing — stopping.")
                break

        if stats.failed > 0:
            self._notice(
                "warn",
                f"{stats.failed} row(s) still failed. "
                f"Tip: run 'Retranslate Failed/Empty/Short rows' later.",
            )

        if job.with_repair and job.repair_col:
            self._run_repair_phase()

        self._finish(stats)
        return stats

    def run_repair(self) -> ProcessStats:
        job = self._job
        translated = ExcelReader.load_column(
            job.out_path, job.dst_col, job.start_row, job.num_rows
        )

        orig_map: dict[int, str] = {}
        ctx_map: dict[int, str] = {}
        if "compare" in job.op_mode and job.src_col:
            orig_map = dict(
                ExcelReader.load_column(job.out_path, job.src_col, job.start_row, job.num_rows)
            )
        if "context" in job.op_mode and job.ctx_col:
            ctx_map = dict(
                ExcelReader.load_column(job.out_path, job.ctx_col, job.start_row, job.num_rows)
            )

        rows_data: list[dict] = []
        for rn, text in translated:
            if not text.strip():
                continue
            entry: dict = {"row_num": rn, "translated": text}
            if orig_map:
                entry["original"] = orig_map.get(rn, "")
            if ctx_map:
                entry["context"] = ctx_map.get(rn, "")
            rows_data.append(entry)

        # Rows that carry a translation worth repairing. As with translate, an
        # empty cell was never a candidate, so it is not counted as skipped.
        repairable = len(rows_data)

        if job.skip_filled and job.repair_col:
            filled = ExcelReader.filled_rows(
                job.out_path, job.repair_col, job.start_row, job.num_rows
            )
            skipped = [d for d in rows_data if d["row_num"] in filled]
            rows_data = [d for d in rows_data if d["row_num"] not in filled]
            if skipped:
                self._notice("resume", f"{len(skipped)} repair row(s) already done — skipped.")

        if not rows_data:
            self._notice("info", "Nothing to repair.")
            self._announce_plan(0)
            stats = ProcessStats(total=repairable, skipped=repairable)
            self._finish(stats)
            return stats

        self._announce_plan(len(rows_data))
        stats = self._run_repair_rows(rows_data, job.repair_col or job.dst_col, job.op_mode)
        # The queue, not the column: a bar counting rows that were never sent
        # cannot reach 100% (AC-603).
        stats.total = len(rows_data)
        stats.skipped = repairable - len(rows_data)
        self._finish(stats)
        return stats

    def run_retranslate_failed(
        self,
        include_too_short: bool = True,
        close_log: bool = True,
    ) -> tuple[ProcessStats, list[tuple[int, str, str]]]:
        """
        Rescan the destination column and retranslate anything empty,
        FAIL_MARKER-ed, or (optionally) suspiciously short.

        The figures describe the *classification*, not the file: `total` is the
        number of problematic rows queued and `skipped` the number of rows that
        already held a reasonable translation. Rows with no source at all appear
        in neither — they were never translations, so calling them skipped would
        put a denominator of 12,000 in front of a queue of 6,000 (BUG-001).
        """
        job = self._job
        dst_rows = ExcelReader.load_column(
            self._output_path(), job.dst_col, job.start_row, job.num_rows
        )
        src_rows = dict(
            ExcelReader.load_column(job.src_path, job.src_col, job.start_row, job.num_rows)
        )

        failed_rows: list[tuple[int, str]] = []
        scan_results: list[tuple[int, str, str]] = []
        # Rows with a source and an acceptable translation: the only rows this
        # operation genuinely passes over.
        healthy = 0

        for rn, dst_val in dst_rows:
            src_val = src_rows.get(rn, "")
            if not src_val.strip():
                continue

            issue = None
            if not dst_val or not dst_val.strip():
                issue = "empty"
            elif dst_val.strip() == config.FAIL_MARKER:
                issue = "TRANSLATE_FAILED"
            elif include_too_short and len(dst_val.strip()) < 3 and len(src_val.strip()) >= 3:
                issue = "too_short"

            if issue:
                failed_rows.append((rn, src_val))
                scan_results.append((rn, issue, src_val[:60]))
            else:
                healthy += 1

        if not failed_rows:
            self._notice("info", "No problematic rows found.")
            self._announce_plan(0)
            stats = ProcessStats(total=healthy, skipped=healthy)
            if close_log:
                self._finish(stats)
            return stats, scan_results

        self._notice(
            "info",
            f"Retranslate: {len(failed_rows)} problematic row(s) detected, "
            f"{healthy} OK.",
        )

        # Announced before the first request, and only when this is the job
        # itself — during an auto-sweep the plan was already stated by
        # `run_translate` and must not be restated (AC-605).
        self._announce_plan(len(failed_rows))
        stats = self._run_translate_rows(
            failed_rows, context_map=None, dst_col=job.dst_col,
            total=len(failed_rows),
        )
        stats.skipped = healthy
        if close_log:
            self._finish(stats)
        return stats, scan_results

    # ── Shared run paths ──────────────────────────────────────────────────────

    def _run_translate_rows(
        self,
        rows: list[tuple[int, str]],
        context_map: dict[int, str] | None,
        dst_col: int,
        total: int,
    ) -> ProcessStats:
        job = self._job
        self._dst_col = dst_col
        self._source_symbols = collect_source_symbols([text for _, text in rows])
        source_map = dict(rows)
        self._source_map = source_map

        from ..data.app_prefs import dedup_mode
        current_dedup = dedup_mode()
        self._dedup_mode = current_dedup

        rows_to_batch = rows
        if current_dedup == "persistent":
            from .tm import lookup_tm_batch
            all_texts = [text for _, text in rows if text.strip()]
            tm_cache = lookup_tm_batch(job.src_lang, job.tgt_lang, all_texts)
            if tm_cache:
                tm_hits = {r: tm_cache[text] for r, text in rows if text in tm_cache}
                if tm_hits:
                    self._writer.flush(tm_hits, dst_col)
                    rows_to_batch = [(r, text) for r, text in rows if text not in tm_cache]
                    self._notice("info", f"Translation Memory: {len(tm_hits)} row(s) filled from persistent cache.")

        tasks = build_batches(rows_to_batch, job.batch_size, is_repair=False, context_map=context_map)
        self._notice(
            "info",
            f"Translate: {len(rows)} row(s) ({len(rows_to_batch)} queued) in {len(tasks)} batch(es), "
            f"{job.processing_mode} x{job.n_workers}.",
        )

        def prompt_builder(task: BatchTask) -> str:
            context_rows = None
            if context_map and job.op_mode == "translate_context":
                context_rows = [(rn, context_map.get(rn, "")) for rn, _ in task.rows]
            # The Settings "batch context" switch: the rows of a batch are
            # consecutive lines of the file either way; this only decides
            # whether the prompt says so and asks for continuity across them.
            from ..data.app_prefs import translate_batch_context

            try:
                batch_context = translate_batch_context()
            except Exception:
                batch_context = False
            return build_translate_prompt(
                rows=task.rows,
                src_lang=job.src_lang,
                tgt_lang=job.tgt_lang,
                style_prompt=job.style_prompt,
                context_rows=context_rows,
                global_context=job.global_context,
                custom_placeholders=job.custom_placeholders,
                prompt_format=job.prompt_format,
                batch_context=batch_context,
            )

        def solo_prompt_builder(row_num: int, task: BatchTask) -> str:
            return build_translate_solo_prompt(
                text=source_map.get(row_num, ""),
                src_lang=job.src_lang,
                tgt_lang=job.tgt_lang,
                style_prompt=job.style_prompt,
                context=(task.context_map or {}).get(row_num, ""),
                global_context=job.global_context,
                custom_placeholders=job.custom_placeholders,
            )

        scheduler = self._make_scheduler(
            prompt_builder=prompt_builder,
            parse_response=self._make_translate_parser(source_map),
            solo_prompt_builder=solo_prompt_builder,
        )
        stats = scheduler.run(tasks, total_rows=len(rows))
        stats.total = total
        return stats

    def _run_repair_rows(
        self, rows_data: list[dict], dst_col: int, repair_mode: str
    ) -> ProcessStats:
        job = self._job
        self._dst_col = dst_col
        self._source_symbols = collect_source_symbols(
            [d.get("translated", "") for d in rows_data]
        )
        by_row = {d["row_num"]: d for d in rows_data}

        size = max(1, job.batch_size)
        chunks = [rows_data[i:i + size] for i in range(0, len(rows_data), size)]
        tasks = [
            BatchTask(
                batch_num=index + 1,
                total_batches=len(chunks),
                rows=[(d["row_num"], d.get("translated", "")) for d in chunk],
                is_repair=True,
                extra={"rows_data": chunk},
            )
            for index, chunk in enumerate(chunks)
        ]
        self._notice(
            "info",
            f"Repair: {len(rows_data)} row(s) in {len(tasks)} batch(es), mode {repair_mode}.",
        )

        def prompt_builder(task: BatchTask) -> str:
            data = task.extra.get("rows_data") or [
                by_row[rn] for rn, _ in task.rows if rn in by_row
            ]
            return build_repair_prompt(
                rows_data=data,
                repair_mode=repair_mode,
                src_lang=job.src_lang,
                tgt_lang=job.tgt_lang,
            )

        def solo_prompt_builder(row_num: int, task: BatchTask) -> str:
            entry = by_row.get(row_num, {})
            return build_repair_solo_prompt(
                translated=entry.get("translated", ""),
                repair_mode=repair_mode,
                original=entry.get("original", ""),
                context=entry.get("context", ""),
                src_lang=job.src_lang,
                tgt_lang=job.tgt_lang,
            )

        scheduler = self._make_scheduler(
            prompt_builder=prompt_builder,
            parse_response=self._make_repair_parser(),
            solo_prompt_builder=solo_prompt_builder,
        )
        return scheduler.run(tasks, total_rows=len(rows_data))

    def _run_repair_phase(self) -> None:
        """Repair pass chained straight after translate, reading what was written."""
        job = self._job
        self._writer.sync()          # read-after-write barrier
        translated = ExcelReader.load_column(
            self._output_path(), job.dst_col, job.start_row, job.num_rows
        )
        src_map = dict(
            ExcelReader.load_column(job.src_path, job.src_col, job.start_row, job.num_rows)
        )
        rows_data = [
            {"row_num": rn, "translated": text, "original": src_map.get(rn, "")}
            for rn, text in translated
            if text.strip() and text != config.FAIL_MARKER
        ]
        if not rows_data:
            return
        mode = "repair_compare_context" if job.ctx_col else "repair_compare"
        self._notice("info", "Starting repair phase.")
        self._run_repair_rows(rows_data, job.repair_col, mode)

    # ── Parsing callbacks handed to the scheduler ─────────────────────────────

    def _make_translate_parser(self, source_map: dict[int, str]):
        job = self._job

        def parse(raw: str, task: BatchTask, report=None) -> BatchResult:
            # `report` is the Judge path's channel for saying "this parse is
            # re-asking the model"; translate parsing is purely local, so it
            # is accepted and ignored rather than wired to anything.
            expected = task.row_numbers
            batch_source = {rn: source_map.get(rn, "") for rn in expected}
            if job.prompt_format == "json":
                parsed = parse_json_response(raw, expected)
            else:
                parsed = self._parser.recover(raw, expected, source_map=batch_source)
            validated = validate_alignment(parsed, batch_source, mode=job.anti_desync)
            rejected = len(parsed) - len(validated)
            if rejected > 0:
                self._notice(
                    "desync",
                    f"batch {task.batch_num}: {rejected} row(s) rejected by anti-desync "
                    f"— will retry",
                )

            results = {
                rn: self._clean_output(text)
                for rn, text in validated.items()
                if self._clean_output(text)
            }
            missing = [rn for rn in expected if rn not in results]
            return BatchResult(results=results, failed_rows=missing)

        return parse

    def _make_repair_parser(self):
        def parse(raw: str, task: BatchTask, report=None) -> BatchResult:
            # Same contract as the translate parser: local only, `report`
            # unused (see _make_translate_parser).
            expected = task.row_numbers
            parsed = self._parser.recover(raw, expected)
            results = {
                rn: self._clean_output(text)
                for rn, text in parsed.items()
                if rn in set(expected) and self._clean_output(text)
            }
            missing = [rn for rn in expected if rn not in results]
            return BatchResult(results=results, failed_rows=missing)

        return parse

    def _parse_solo(self, raw: str, row_num: int, task: BatchTask) -> str:
        import re

        text = raw.strip().split("\n")[0].strip()
        text = re.sub(r"^\[?\d+\]?[.)]?\s*", "", text)
        return self._clean_output(decode_placeholders(text))

    def _clean_output(self, text: str) -> str:
        if not text:
            return ""
        text = strip_runaway(text)
        if self._source_symbols:
            text = symbol_diff_sanitize(text, self._source_symbols)
        return text.strip()

    # ── Scheduler wiring ──────────────────────────────────────────────────────

    def _make_scheduler(self, prompt_builder, parse_response, solo_prompt_builder) -> Scheduler:
        return Scheduler(
            job=self._job,
            connection_pool=self._pool,
            # Empty for a combo: each lease names its own endpoint, because the
            # members can sit on different providers.
            base_url=getattr(self._provider, "base_url", ""),
            prompt_builder=prompt_builder,
            parse_response=parse_response,
            on_result=self._write_result,
            on_status=self._on_status,
            on_event=self._forward_event,
            stop_event=self._stop,
            solo_prompt_builder=solo_prompt_builder,
            solo_parser=self._parse_solo,
            raw_log=self._log_raw if self._raw_logger else None,
            response_format_builder=self._response_format_for,
            on_batch_start=self._on_batch_start,
            registry=self._registry,
        )

    def _forward_event(self, event: ScheduleEvent) -> None:
        """
        Pass scheduler events on, and promote a total failure to a notice.

        `aborted` is what "every connection, or every model in this combo, has
        failed" arrives as. It already reaches the events rail, but it is the one
        event that ends the run, so it also goes out on `on_notice` — the channel
        the rest of the app uses to say something the user must not miss
        (FEATURE-005 total-failure rule). It must never stop quietly.
        """
        if event.kind == "aborted" and event.message:
            self._notice("error", event.message)
        if self._on_event is not None:
            self._on_event(event)

    @staticmethod
    def _response_format_for(task: BatchTask) -> dict | None:
        """Only reached when the model opted into prompt_format='json'."""
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "translation",
                "strict": True,
                "schema": build_json_schema(task.row_numbers),
            },
        }

    def _write_result(self, task: BatchTask, result: BatchResult) -> None:
        """Hand finished rows to the async writer. Never blocks on disk."""
        payload = dict(result.results)
        for row_num in result.failed_rows:
            payload.setdefault(row_num, config.FAIL_MARKER)
        if payload:
            self._writer.flush(payload, self._dst_col)
            self._maybe_sync_full()
        if getattr(self, "_dedup_mode", "unique_file") == "persistent" and result.results:
            from .tm import store_tm_batch
            source_map = getattr(self, "_source_map", {})
            pairs = [
                (source_map.get(r, ""), t)
                for r, t in result.results.items()
                if source_map.get(r) and t != config.FAIL_MARKER
            ]
            store_tm_batch(self._job.src_lang, self._job.tgt_lang, pairs)
        if self._on_result_hook is not None:
            self._on_result_hook(task, result)
        if self._job.debug_log:
            self.write_log(
                f"batch {task.batch_num}: wrote {len(payload)} row(s), "
                f"{len(result.failed_rows)} failed"
            )

    def request_stop(self) -> None:
        self._stop.set()

    FULL_SYNC_INTERVAL = 30.0

    def _maybe_sync_full(self) -> None:
        """
        Broadcast unique-file rows to the full output — throttled, off-thread.

        A 124k-row target costs seconds per sync; on the worker thread it
        stalled the run (disguised as "parsing"), and per batch it meant
        re-writing the whole file thousands of times. The broadcast now runs
        at most every 30s in a background thread; `_finish` always performs
        the final synchronous pass, so no completed row is ever left out of
        the full file when the job ends.
        """
        if not (self._job.map_path and self._job.full_out_path):
            return
        now = time.monotonic()
        with self._sync_lock:
            if self._syncing or now - self._last_sync < self.FULL_SYNC_INTERVAL:
                return
            self._syncing = True

        def _run() -> None:
            try:
                from .dedup_file import sync_unique_to_full
                self._writer.sync()
                sync_unique_to_full(
                    self._job.map_path, self._output_path(),
                    self._job.full_out_path, self._dst_col,
                )
            except Exception as exc:
                self._notice("error", f"Full-file sync failed: {exc}")
            finally:
                with self._sync_lock:
                    self._last_sync = time.monotonic()
                    self._syncing = False

        self._sync_thread = threading.Thread(
            target=_run, daemon=True, name="uet-full-sync",
        )
        self._sync_thread.start()

    def _final_full_sync(self) -> None:
        """One last synchronous broadcast so the full file is complete."""
        if not (self._job.map_path and self._job.full_out_path):
            return
        thread = self._sync_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=120.0)
        try:
            from .dedup_file import sync_unique_to_full
            synced = sync_unique_to_full(
                self._job.map_path, self._output_path(),
                self._job.full_out_path, self._dst_col,
            )
            self.write_log(f"[sync] broadcast {synced} cell(s) to full output")
            # Clean up temporary unique files in .uet_cache after successful broadcast
            try:
                unique_out = self._output_path()
                if unique_out and os.path.exists(unique_out) and unique_out != self._job.full_out_path and ".uet_cache" in unique_out:
                    os.remove(unique_out)
                if self._job.src_path and os.path.exists(self._job.src_path) and self._job.src_path != self._job.full_src_path and ".uet_cache" in self._job.src_path:
                    os.remove(self._job.src_path)
            except OSError:
                pass
        except Exception as exc:
            self._notice("error", f"Final full-file sync failed: {exc}")

    # ── Logging ───────────────────────────────────────────────────────────────

    def _notice(self, kind: str, message: str) -> None:
        if self._job.debug_log:
            self.write_log(f"[{kind}] {message}")
        if self._on_notice is not None:
            self._on_notice(kind, message)

    def _log_raw(self, label: str, prompt: str, response: str | None, latency: float) -> None:
        if self._raw_logger is not None:
            self._raw_logger.log(label=label, prompt=prompt, response=response, latency=latency)

    def _provider_label(self) -> str:
        """
        What the log calls the thing serving this job.

        A combo has no single provider, so it names the combo and its strategy —
        which is the useful line to read back anyway, since the models actually
        used are the leases the pool handed out.
        """
        if self._provider is not None:
            return self._provider.name
        combo = getattr(self._pool, "combo", None)
        if combo is not None:
            return f"combo {combo.label} ({combo.strategy})"
        return "(combo)"

    def _open_log(self) -> TextIO | None:
        job = self._job
        if not job.debug_log or not job.log_path:
            return None
        try:
            log_dir = os.path.dirname(job.log_path)
            if log_dir:
                os.makedirs(log_dir, exist_ok=True)
            handle = open(job.log_path, "a", encoding="utf-8")
            handle.write(
                f"\n{'=' * 60}\n"
                f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Mode: {job.op_mode}  Processing: {job.processing_mode} x{job.n_workers}\n"
                f"Provider: {self._provider_label()}  Model: {job.model_id}\n"
                f"{'=' * 60}\n"
            )
            return handle
        except Exception as exc:
            # Asked for a log and didn't get one — say so rather than
            # silently running without it.
            if self._on_notice is not None:
                self._on_notice("warn", f"Could not open debug log: {exc}")
            return None

    def _open_raw_log(self) -> RawLogger | None:
        job = self._job
        if not job.raw_log:
            return None
        try:
            base = job.log_path or job.out_path
            return RawLogger(os.path.splitext(base)[0] + "_raw.log")
        except Exception as exc:
            if self._on_notice is not None:
                self._on_notice("warn", f"Could not open raw API log: {exc}")
            return None

    def write_log(self, line: str) -> None:
        if self._log_file:
            try:
                ts = datetime.now().strftime("%H:%M:%S")
                self._log_file.write(f"[{ts}] {line}\n")
                self._log_file.flush()
            except Exception:
                pass

    def _finish(self, stats: ProcessStats) -> None:
        """Commit pending writes and close logs. Safe to call once per job."""
        try:
            self._writer.commit()
            self._final_full_sync()
        except Exception as exc:
            self._notice("error", f"Final Excel write failed: {exc}")

        stats.tokens_consumed = max(stats.tokens_consumed, self._pool.total_tokens_consumed())

        if self._log_file:
            try:
                self._log_file.write(
                    f"\nEnded: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"Stats: ok={stats.ok} repaired={stats.repaired} "
                    f"skipped={stats.skipped} failed={stats.failed} total={stats.total}\n"
                )
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None
        if self._raw_logger:
            self._raw_logger.close()
            self._raw_logger = None
