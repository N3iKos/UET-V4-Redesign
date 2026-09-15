"""
judge/runner.py — Running an evaluation, and repairing what it found.

This is the module ADR-001 is about. Nothing here is a new AI pipeline: an
evaluation is a batch of prompts, so it goes through the `Scheduler` the
translation path uses, over the same `RetryExecutor`, the same
`ConnectionPool`/`KeyPool`, and the same `ProviderClient`. Fix All gets the
recovery ladder (mini-batch, then solo) for free, and the Activity Panel shows
its workers without a line of extra code (FR-115 / FR-207).

Two paths exist, and the difference is honest rather than incidental:

  * **pooled** — a connection pool was supplied, so batches run through the
    Scheduler with retries, cooldowns and connection rotation. This is what the
    UI uses.
  * **direct** — a `client_factory` was supplied instead, so requests are made
    one at a time against that client. This is what an offline test uses, and
    what a single-row Fix It uses when there is no pool to borrow.

Writing is the part to be careful about. `fix_all` returns a `StagedFix` and
touches no file at all; `apply_fix` is the only function in the package that
writes, it writes through the atomic `ExcelWriter`, and it writes only the rows
it was explicitly told were approved (FR-111 / AC-103). Stopping halfway
through a Fix All therefore leaves the spreadsheet exactly as it was.

Nothing here prints. Progress and failures travel by callback or come back in
the result object.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime
from dataclasses import dataclass, field
from typing import Callable, TextIO

from .. import config
from ..core.models import BatchResult, JobConfig
from ..data import app_prefs
from ..io.excel_io import ExcelReader, ExcelWriter
from ..logging_.vlogging import RawLogger
from ..scheduling.connection_pool import AllConnectionsUnavailable, ConnectionPool
from ..scheduling.scheduler import BatchTask, Scheduler, ScheduleEvent
from . import estimator
from .models import (
    CRITERIA,
    GRADING_MODES,
    JudgeReport,
    JudgeResult,
    JudgeSection,
    JudgeSummary,
    RejudgeVerdict,
    RowIssue,
    SingleFix,
    StagedFix,
)
from .parser import parse_evaluation, parse_fixed_line, parse_single
from .prompts import (
    build_fix_prompt,
    build_fix_solo_prompt,
    build_judge_prompt,
    build_judge_rechain_prompt,
    build_judge_single_prompt,
)

# What a repaired reply may cost. A fix is one line per row, so this is far
# smaller than a translate batch's allowance and keeps a runaway generation
# from burning the budget the rest of the section needed.
FIX_MAX_TOKENS = 4096

# How many times a section whose reply is not Judge JSON is re-asked before its
# rows are counted as unevaluated. Three, matching the translate path's retry
# budget: after that the model has been asked plainly four times and a smaller
# (sub-batch / solo) recovery is the cheaper next step.
MAX_FORMAT_RETRIES = 3

# Rows per repair request, by default. Raised from 5 to 20 (CR-002 FR-611): the
# old figure was chosen for reliability rather than from measurement, and 5 rows a
# request means twenty times the requests — each carrying the same instruction
# block again — for the same work. It is still a *starting* number: the Fix All
# wizard asks per run and Settings owns the default, so a model that merges lines
# can be dropped back to 1.
DEFAULT_FIX_BATCH_ROWS = config.DEFAULT_FIX_BATCH_ROWS

Notice = Callable[[str, str], None]      # (kind, message)


@dataclass
class JudgeTarget:
    """
    The file and columns under review.

    `path` is the *output* file (`*_Translated.*`) — the source file is never
    written to by this feature either, and the translation being judged only
    exists in the output.
    """

    path: str
    src_col: int
    dst_col: int
    start_row: int = 2
    num_rows: int = 0            # 0 = every remaining row
    ctx_col: int = 0             # 0 = no context column, so no context criterion
    src_lang: str = ""
    tgt_lang: str = ""
    style_prompt: str = ""
    custom_placeholders: str = ""
    full_path: str = ""
    map_path: str = ""

    @property
    def has_context(self) -> bool:
        return bool(self.ctx_col)


@dataclass
class EvaluationPlan:
    """
    What an evaluation is about to do, before it does any of it.

    The setup screen shows this and asks for confirmation when it is large
    (FR-106), so it has to be computable without sending anything.

    `rechain` marks a chain-mode re-judge plan: the sections' rows are the
    flagged rows under review, and `rechain_neighbours` carries their physical
    neighbours from the file as CONTEXT ONLY rows the prompt shows but the
    verdicts must never touch.
    """

    rows: list[tuple[int, str, str]] = field(default_factory=list)
    sections: list[list[tuple[int, str, str]]] = field(default_factory=list)
    prejudged: dict[int, RowIssue] = field(default_factory=dict)
    estimated_tokens: int = 0
    max_tokens: int = config.MAX_TOKENS_PER_REQUEST
    rechain: bool = False
    rechain_neighbours: dict[int, tuple[str, str]] = field(default_factory=dict)
    unique_count: int = 0
    history_map: dict[int, str] = field(default_factory=dict)

    @property
    def total_rows(self) -> int:
        return len(self.rows)

    @property
    def unique_rows(self) -> int:
        if self.unique_count:
            return self.unique_count
        counted = sum(len(s) for s in self.sections) + len(self.prejudged)
        return counted or len(self.rows)

    @property
    def duplicate_count(self) -> int:
        return max(0, self.total_rows - self.unique_rows)

    @property
    def request_count(self) -> int:
        return len(self.sections)

    @property
    def warned(self) -> bool:
        return estimator.is_warned(self.estimated_tokens)

    def describe(self) -> str:
        return estimator.summarise(self.sections, self.max_tokens)


class JudgeRunner:
    """
    One evaluation session: one file, one set of columns, one Judge model.

    The model is chosen per session by the caller and used for everything —
    evaluating, fixing and re-judging (ADR-001). There is no separate fix model
    and no remembered default (FR-102 / AC-107).

    A combo may be chosen in place of a model (FEATURE-005). Then `provider` is
    None and `registry` is given instead, because the members span providers; the
    pool comes from `scheduling/pools.build_pool` and the base URL travels on each
    lease rather than on the job. Nothing else about this class changes, which is
    the test of whether a combo is really just a way of picking a ModelEntry.
    """

    def __init__(
        self,
        target: JudgeTarget,
        job: JobConfig | None = None,
        provider=None,
        connection_pool: ConnectionPool | None = None,
        client_factory: Callable[[], object] | None = None,
        registry=None,
        max_tokens: int | None = None,
        max_rows: int | None = None,
        criteria: list[str] | None = None,
        grading_mode: str | None = None,
        on_status=None,
        on_event=None,
        on_notice: Notice | None = None,
        on_row: Callable[[int, "RowIssue | None"], None] | None = None,
        on_fix: Callable[[int, str, bool], None] | None = None,
        on_section: Callable[[int, int, list[int], bool], None] | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self._target = target
        self._job = job
        self._provider = provider
        self._registry = registry
        self._pool = connection_pool
        self._client_factory = client_factory
        self._criteria = list(criteria or CRITERIA)
        self._grading_mode = _clean_grading_mode(grading_mode)
        self._on_status = on_status
        self._on_event = on_event
        self._on_notice = on_notice
        # Fires per row as a section lands, so a screen can fill in as it goes
        # instead of showing nothing until the whole evaluation finishes.
        self._on_row = on_row
        # The same, for Fix All: `(row, text, changed)` — `changed=False` means
        # the model deliberately left the line alone.
        self._on_fix = on_fix
        # `(index, total, rows, ok)` per finished section. Rows are reported one
        # by one for the grid; this is the *request* granularity, which is what a
        # progress figure has to count when a section fails and reports no rows
        # at all — otherwise the bar stops moving and reads as a hang.
        self._on_section = on_section
        self._stop = stop_event or threading.Event()
        # Section indices already reported, so a section that both fails and
        # partially recovers is still counted once.
        self._reported_sections: set[int] = set()
        # How many requests the current operation is made of, for progress.
        self._section_total = 0
        self._max_tokens = int(max_tokens or _saved_max_tokens())
        # Rows per request. The caller's figure wins — the wizard's pacing step
        # asks for it explicitly — and Settings supplies the default when it does
        # not (FR-104 / the Judge pacing step).
        self._max_rows = int(max_rows) if max_rows else _saved_max_rows()
        self._client = None
        self._tokens_used = 0
        # Rows a valid reply has actually given a verdict for, across every
        # attempt and every recovery of the current operation. Reset per
        # operation so a later Fix All / re-judge starts from an empty ledger.
        self._rows_seen: set[int] = set()

        # Debug and Raw API logging setup
        self._log_file: TextIO | None = self._open_debug_log()
        self._raw_logger: RawLogger | None = self._open_raw_log()

        if self._pool is None and job is not None:
            # A combo job has no single provider, so the pool comes from the
            # shared factory rather than from `provider` — the same decision the
            # translate path makes, in the same place (FEATURE-005).
            if job.uses_combo and registry is not None:
                from ..scheduling.pools import build_pool

                self._pool = build_pool(
                    job, registry, on_notice=self._on_notice
                )
            elif provider is not None:
                self._pool = ConnectionPool(
                    provider,
                    connection_ids=[job.connection_id] if job.connection_id else None,
                )

    # ── Logging helpers ───────────────────────────────────────────────────────

    def write_log(self, message: str) -> None:
        """Append one line to the debug log with a timestamp."""
        if not self._log_file:
            return
        ts = datetime.now().strftime("%H:%M:%S")
        try:
            self._log_file.write(f"[{ts}] {message}\n")
            self._log_file.flush()
        except Exception:
            pass

    def _log_raw(self, label: str, prompt: str, response: str | None, latency: float) -> None:
        if self._raw_logger is not None:
            self._raw_logger.log(label=label, prompt=prompt, response=response, latency=latency)

    def _open_debug_log(self) -> TextIO | None:
        job = self._job
        if not job or not job.debug_log or not job.log_path:
            return None
        try:
            log_dir = os.path.dirname(job.log_path)
            if log_dir:
                os.makedirs(log_dir, exist_ok=True)
            handle = open(job.log_path, "a", encoding="utf-8")
            handle.write(
                f"\n{'=' * 60}\n"
                f"JUDGE RUNNER LOG — Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Target: {self._target.path}\n"
                f"Columns: src={self._target.src_col}, dst={self._target.dst_col}, ctx={self._target.ctx_col}\n"
                f"Criteria: {', '.join(self.criteria)}  Grading: {self._grading_mode}\n"
                f"{'=' * 60}\n\n"
            )
            handle.flush()
            return handle
        except Exception as exc:
            self._notify("warn", f"Could not open Judge debug log: {exc}")
            return None

    def _open_raw_log(self) -> RawLogger | None:
        job = self._job
        if not job or not job.raw_log:
            return None
        try:
            base = job.log_path or self._target.path
            return RawLogger(os.path.splitext(base)[0] + "_judge_raw.log")
        except Exception as exc:
            self._notify("warn", f"Could not open Judge raw API log: {exc}")
            return None

    def close_logs(self) -> None:
        if self._log_file:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None
        if self._raw_logger:
            self._raw_logger.close()
            self._raw_logger = None

    # ── Reading the file ──────────────────────────────────────────────────────

    @property
    def target(self) -> JudgeTarget:
        return self._target

    @property
    def grading_mode(self) -> str:
        return self._grading_mode

    @property
    def graded(self) -> bool:
        return self._grading_mode == "severity"

    @property
    def criteria(self) -> list[str]:
        """The criteria in play — `context` is dropped without a context column."""
        return self._criteria_for(None)

    def _criteria_for(self, criteria: list[str] | None) -> list[str]:
        """
        The criteria one operation should use, without changing the session's.

        `criteria=None` means "whatever this runner was built with". Anything
        else applies to that call only: a re-judge narrowed to `style` must not
        leave the runner narrowed for everything after it, which is what a
        `self._criteria = ...` assignment on a per-call argument used to do.
        """
        source = self._criteria if criteria is None else criteria
        chosen = [name for name in source if name in CRITERIA]
        if not self._target.has_context and "context" in chosen:
            chosen.remove("context")
        return chosen or ["mistranslation"]

    def _fitting_map(self, rows: list[tuple[int, str, str]]) -> dict[int, str]:
        """Per-row FITTING prompt line — calculate source vs translation conciseness."""
        out: dict[int, str] = {}
        for row, source, translation in rows:
            src = (source or "").strip()
            tgt = (translation or "").strip()
            if not src or not tgt:
                continue
            ratio = len(tgt) / max(len(src), 1)
            # Highlight lines with significant text expansion (>1.4x) for conciseness review
            if ratio >= 1.4:
                out[row] = (
                    f"source {len(src)} chars · translation {len(tgt)} chars "
                    f"({ratio:.1f}x length) · CONCISENESS"
                )
        return out

    def revalidate_fix(self, row: int, fixed_text: str, source: str) -> str | None:
        """Post-fix gate: check if the fix is excessively verbose compared to source."""
        if len(fixed_text or "") > max(len(source or "") * 2.5, 80):
            return "excessive-length"
        return None

    def load_rows(self) -> tuple[list[tuple[int, str, str]], dict[int, str]]:
        """
        (row, source, translation) for every row with a source, plus context.

        Rows whose *source* is empty are not translations at all and are left
        out entirely; rows whose *translation* is empty are kept, because an
        empty target cell is one of the problems Judge exists to report.
        """
        target = self._target
        count = target.num_rows
        if count <= 0:
            total = ExcelReader.row_count(target.path)
            count = max(0, total - target.start_row + 1)
        if count <= 0:
            return [], {}

        sources = dict(
            ExcelReader.load_column(target.path, target.src_col, target.start_row, count)
        )
        translations = dict(
            ExcelReader.load_column(target.path, target.dst_col, target.start_row, count)
        )
        contexts: dict[int, str] = {}
        if target.ctx_col:
            contexts = {
                row: text
                for row, text in ExcelReader.load_column(
                    target.path, target.ctx_col, target.start_row, count
                )
                if (text or "").strip()
            }

        rows = [
            (row, sources[row], translations.get(row, ""))
            for row in sorted(sources)
            if (sources.get(row) or "").strip()
        ]
        return rows, contexts

    def plan(
        self,
        rows: list[tuple[int, str, str]] | None = None,
        history_map: dict[int, str] | None = None,
    ) -> EvaluationPlan:
        """
        Split the work and cost it, without sending anything.

        Rows that are already known to be wrong — empty, or carrying
        `FAIL_MARKER` — are flagged here and *excluded from the requests*: no
        model is needed to confirm an empty cell, and paying for that would be
        absurd (FR-105 / business rule 2).
        """
        if rows is None:
            rows, _contexts = self.load_rows()

        from ..core.failed_cache import load_failed_rows
        failed_set = load_failed_rows(self._target.path, self._target.dst_col)
        if getattr(self._target, "full_path", None):
            failed_set.update(load_failed_rows(self._target.full_path, self._target.dst_col))

        prejudged: dict[int, RowIssue] = {}
        askable: list[tuple[int, str, str]] = []
        for row, source, translation in rows:
            text = (translation or "").strip()
            if not text:
                prejudged[row] = RowIssue(
                    row=row, error_types=["empty"],
                    comment="The target cell is empty — nothing was translated.",
                )
            elif text == config.FAIL_MARKER or (row in failed_set and text == (source or "").strip()):
                comment = (
                    f"Translation failed and was written as {config.FAIL_MARKER}."
                    if text == config.FAIL_MARKER
                    else "Translation failed and was preserved from source."
                )
                prejudged[row] = RowIssue(
                    row=row, error_types=["failed"],
                    comment=comment,
                )
            else:
                askable.append((row, source, translation))

        sections = estimator.split_sections(askable, max_tokens=self._max_tokens, max_rows=self._max_rows)
        return EvaluationPlan(
            rows=rows,
            sections=sections,
            prejudged=prejudged,
            estimated_tokens=estimator.estimate_sections(sections),
            max_tokens=self._max_tokens,
            unique_count=len(askable) + len(prejudged),
            history_map=dict(history_map or {}),
        )

    def rechain_plan(
        self,
        rows: list[tuple[int, str, str]],
        history_map: dict[int, str] | None = None,
    ) -> EvaluationPlan:
        """
        A re-judge plan in dialogue-chain mode: flagged rows under review,
        their file neighbours injected as CONTEXT ONLY.

        The sections are built from the flagged rows exactly as `plan` builds
        them — same splitter, same budget — so a chain re-judge is paced like
        the evaluation it follows. The neighbours are the rows physically
        adjacent in the file (row ± window), read from the same loaded set,
        and a neighbour that is itself flagged appears once, as an under-
        review row: `build_judge_rechain_prompt` skips it from the context
        side, so it is never printed twice.

        Only meaningful when the chain setting is on; the caller decides, the
        plan simply carries the mode.
        """
        from ..core.failed_cache import load_failed_rows
        failed_set = load_failed_rows(self._target.path, self._target.dst_col)
        if getattr(self._target, "full_path", None):
            failed_set.update(load_failed_rows(self._target.full_path, self._target.dst_col))

        prejudged: dict[int, RowIssue] = {}
        askable: list[tuple[int, str, str]] = []
        for row, source, translation in rows:
            text = (translation or "").strip()
            if not text:
                prejudged[row] = RowIssue(
                    row=row, error_types=["empty"],
                    comment="The target cell is empty — nothing was translated.",
                )
            elif text == config.FAIL_MARKER or (row in failed_set and text == (source or "").strip()):
                comment = (
                    f"Translation failed and was written as {config.FAIL_MARKER}."
                    if text == config.FAIL_MARKER
                    else "Translation failed and was preserved from source."
                )
                prejudged[row] = RowIssue(
                    row=row, error_types=["failed"],
                    comment=comment,
                )
            else:
                askable.append((row, source, translation))

        sections = estimator.split_sections(
            askable, max_tokens=self._max_tokens, max_rows=self._max_rows
        )
        window = self._chain_window()
        flagged = {row for row, _s, _t in askable}
        neighbours: dict[int, tuple[str, str]] = {}
        try:
            all_rows, _contexts = self.load_rows()
            by_number = {row: (source, translation) for row, source, translation in all_rows}
            numbers = sorted(by_number)
            for row in flagged:
                position = numbers.index(row)
                for offset in range(-window, window + 1):
                    if offset == 0:
                        continue
                    index = position + offset
                    if 0 <= index < len(numbers):
                        neighbour = numbers[index]
                        if neighbour not in flagged:
                            neighbours[neighbour] = by_number[neighbour]
        except Exception:
            neighbours = {}       # no file to read neighbours from: plain re-judge
        return EvaluationPlan(
            rows=rows,
            sections=sections,
            prejudged=prejudged,
            estimated_tokens=estimator.estimate_sections(sections),
            max_tokens=self._max_tokens,
            rechain=True,
            rechain_neighbours=neighbours,
            history_map=dict(history_map or {}),
        )

    # ── Evaluation ────────────────────────────────────────────────────────────

    def evaluate(
        self,
        criteria: list[str] | None = None,
        plan: EvaluationPlan | None = None,
        grading_mode: str | None = None,
    ) -> JudgeReport:
        """
        Review the file and return one report (FR-107).

        Every section is a separate request, and a section that fails does not
        stop the others: its rows are reported as unevaluated and kept out of
        the score, because counting an unreviewed row as correct would hide
        exactly the thing the user needs to see.
        """
        if criteria is not None:
            self._criteria = list(criteria)
        if grading_mode is not None:
            self._grading_mode = _clean_grading_mode(grading_mode)
        rows, contexts = self.load_rows()
        plan = plan or self.plan(rows)
        report = JudgeReport(
            rows=plan.rows,
            criteria=self.criteria,
            grading_mode=self._grading_mode,
        )
        report.issues.update(plan.prejudged)
        # The rows detected without a request are already known, so the screen
        # can show them before the first reply lands.
        for row, issue in plan.prejudged.items():
            self._emit_row(row, issue)

        if plan.sections:
            self._run_evaluation(plan, contexts, report)

        report.summary = self._summarise(plan, report)
        report.unparsed_sections = sum(
            1 for section in report.sections if section.unparsed
        )
        if report.failed_sections:
            self._notify(
                "warn",
                f"{len(report.failed_sections)} of {len(report.sections)} request(s) "
                f"failed — those rows are reported as unevaluated.",
            )
        self.write_log(
            f"Evaluation finished: score={report.summary.percent:.1f}%, evaluated={report.summary.evaluated}, "
            f"problems={report.summary.problem_rows}, unevaluated={report.summary.unevaluated}, tokens={self._tokens_used}"
        )
        return report

    def _run_evaluation(
        self,
        plan: EvaluationPlan,
        contexts: dict[int, str],
        report: JudgeReport,
    ) -> None:
        merged = JudgeResult()
        sections: dict[int, JudgeSection] = {}

        def build_prompt(task: BatchTask) -> str:
            rows = task.extra["rows"]
            h_map = plan.history_map if _saved_context_aware_rejudge() else None
            if plan.rechain:
                # Chain-mode re-judge: the section's rows are the flagged ones
                # under review, and their physical neighbours from the file are
                # printed as CONTEXT ONLY rows around them. The fitting rule
                # lists measured data per reviewed row — a criterion that says
                # "quote the measurement" with no measurement is bait.
                return build_judge_rechain_prompt(
                    rows=rows,
                    neighbours=plan.rechain_neighbours,
                    criteria=self.criteria,
                    src_lang=self._target.src_lang,
                    tgt_lang=self._target.tgt_lang,
                    style_prompt=self._target.style_prompt,
                    custom_placeholders=self._target.custom_placeholders,
                    grading_mode=self._grading_mode,
                    comment_style=self._comment_style(),
                    fitting_map=self._fitting_map(rows),
                    history_map=h_map,
                )
            return build_judge_prompt(
                rows=rows,
                criteria=self.criteria,
                src_lang=self._target.src_lang,
                tgt_lang=self._target.tgt_lang,
                style_prompt=self._target.style_prompt,
                context_map=self._section_context(rows, contexts),
                custom_placeholders=self._target.custom_placeholders,
                grading_mode=self._grading_mode,
                chain=self._chain_enabled(),
                comment_style=self._comment_style(),
                fitting_map=self._fitting_map(rows),
                history_map=h_map,
            )

        def apply_result(index: int, task: BatchTask, result: JudgeResult) -> None:
            """Fold one parsed reply into the shared report state."""
            merged.merge(result)
            if result.unparsed:
                self._mark_seen(result)
            else:
                # A valid reply means every row it was asked about was judged:
                # an empty `lines` array is the model saying they all passed,
                # so the rows it was sent for are *seen* even when none of
                # them were flagged.
                self._rows_seen.update(task.row_numbers)
            section = sections.get(index)
            if section is not None:
                if not result.unparsed:
                    # A later attempt of this section succeeded, so whatever a
                    # first attempt marked it as is stale: the section is not
                    # a failure any more.
                    section.error = ""
                section.unparsed = result.unparsed

        def parse(raw: str, task: BatchTask, report=None) -> BatchResult:
            index = task.extra["index"]
            result = parse_evaluation(raw, graded=self.graded)
            if plan.rechain:
                # Defensive: the prompt forbids verdicts on CONTEXT ONLY rows,
                # but a row number is a real sheet number and a model that
                # reports a neighbour anyway must not corrupt the report —
                # its verdict is dropped, not folded in.
                for neighbour in plan.rechain_neighbours:
                    result.issues.pop(neighbour, None)
            # A reply that is not Judge JSON is re-asked before anything is
            # written off: the model was told the format, it may simply have
            # drifted, and a re-ask costs far less than leaving the section
            # unevaluated. Three attempts, then the section's rows fall through
            # to the scheduler's own recovery ladder (mini-batch, then solo)
            # before any of them is counted as unevaluated.
            if result.unparsed:
                result = self._format_retry(task, index, result, contexts, report)
            apply_result(index, task, result)
            if result.unparsed:
                # Every row of this section came back with no verdict even
                # after its retries were spent. Reported as a section — not a
                # wall of identical row lines — and the summary marks its rows
                # unevaluated, so the screen only needs the one concise line.
                self._emit_section(
                    index, len(plan.sections), list(task.row_numbers), ok=False
                )
                # Hand the rows to the scheduler's recovery ladder so the
                # section gets the smaller-batch / solo re-asks UET already
                # has: those are the last retries before `unevaluated`.
                return BatchResult(failed_rows=list(task.row_numbers))
            # Report each row of this section the moment it is parsed, so a
            # long evaluation reads as progress rather than a blank screen.
            # A row the model did not flag is reported as passing (`None`).
            for row in task.row_numbers:
                self._emit_row(row, result.issues.get(row))
            self._emit_section(
                index, len(plan.sections), list(task.row_numbers), ok=True
            )
            return BatchResult(results={row: "ok" for row in task.row_numbers})

        def build_solo(row: int, task: BatchTask) -> str:
            # The solo recovery for an evaluation re-asks one row with the
            # judge prompt, so a row that never produced a usable verdict still
            # gets a second chance before it is counted as unevaluated.
            source = ""
            translation = ""
            for number, src, dst in task.extra["rows"]:
                if number == row:
                    source, translation = src, dst
                    break
            fitting_info = self._fitting_map([(row, source, translation)]).get(row, "")
            return build_judge_single_prompt(
                row=row,
                source=source,
                translation=translation,
                criteria=self.criteria,
                src_lang=self._target.src_lang,
                tgt_lang=self._target.tgt_lang,
                style_prompt=self._target.style_prompt,
                context=contexts.get(row, ""),
                custom_placeholders=self._target.custom_placeholders,
                grading_mode=self._grading_mode,
                fitting=fitting_info,
            )

        def parse_solo(raw: str, row: int, task: BatchTask) -> str:
            # One row re-checked: a flagged row stays flagged, a clean one is
            # reported as passing. `parse_single` trusts the caller's row when
            # the model numbered nothing, which is the usual solo answer.
            solo = parse_single(raw, row, graded=self.graded)
            apply_result(task.extra.get("index", task.batch_num), task, solo)
            self._emit_row(row, solo.issues.get(row))
            # The value a recovery ladder reads is a presence check, so the
            # text only needs to be non-empty when a verdict actually landed.
            return "ok" if not solo.unparsed else ""

        for index, rows in enumerate(plan.sections, start=1):
            sections[index] = JudgeSection(
                index=index,
                rows=[row for row, _s, _t in rows],
                estimated_tokens=estimator.estimate_total(rows),
            )

        tasks = [
            BatchTask(
                batch_num=index,
                total_batches=len(plan.sections),
                rows=[(row, source) for row, source, _t in rows],
                extra={"rows": rows, "index": index},
            )
            for index, rows in enumerate(plan.sections, start=1)
        ]

        self._run_tasks(
            tasks, build_prompt, parse, sections,
            solo_prompt_builder=build_solo, solo_parser=parse_solo,
        )

        report.sections = [sections[index] for index in sorted(sections)]
        for row, issue in merged.issues.items():
            # A section that failed cannot have contributed an issue, and a row
            # already flagged as empty/failed keeps that reason.
            if row not in report.issues:
                report.issues[row] = issue

    def _summarise(self, plan: EvaluationPlan, report: JudgeReport) -> JudgeSummary:
        # Rows a valid reply actually carried a verdict for. A section that
        # answered in prose on its first attempt but was re-asked until a valid
        # reply landed still gets its recovered rows counted as evaluated — only
        # the rows that never produced a verdict are unevaluated.
        unevaluated: set[int] = set()
        for section in report.sections:
            # `ok` covers a section whose batch genuinely failed (provider,
            # quota, aborted) — its rows were never judged and count as
            # unevaluated. `unparsed` is the format-retry case: rows recovered
            # by any later attempt are evaluated, the rest are not.
            for row in section.rows:
                if section.error:
                    unevaluated.add(row)
                elif row not in self._rows_seen:
                    unevaluated.add(row)

        for row in unevaluated:
            if row not in report.issues:
                report.issues[row] = RowIssue(
                    row=row,
                    error_types=["unjudged"],
                    severity="moderate",
                    comment="Request timed out or failed to evaluate. Re-judge to retry.",
                )

        total = len(plan.rows)
        evaluated = total - len(unevaluated)
        problems = list(report.issues.keys())

        distribution: dict[str, int] = {}
        severity_counts: dict[str, int] = {}
        for row in problems:
            issue = report.issues[row]
            for error_type in issue.error_types:
                distribution[error_type] = distribution.get(error_type, 0) + 1
            # Only rows the model actually graded are counted, so a `none`-mode
            # run reports an empty spread rather than a fabricated one.
            if issue.severity:
                severity_counts[issue.severity] = (
                    severity_counts.get(issue.severity, 0) + 1
                )

        return JudgeSummary(
            total=total,
            evaluated=max(0, evaluated),
            problem_rows=len(problems),
            unevaluated=len(unevaluated),
            distribution=distribution,
            severity_counts=severity_counts,
            estimated_tokens=plan.estimated_tokens,
            tokens_used=self._tokens_used,
        )

    # ── Format retry ───────────────────────────────────────────────────────────

    def _format_retry(
        self, task: BatchTask, index: int, first: JudgeResult,
        contexts: dict[int, str], report=None,
    ) -> JudgeResult:
        """
        Re-ask a section whose reply was not Judge JSON, up to three times.

        A model that answered in prose was told exactly what shape to return
        and simply drifted; re-asking the *same* batch — not re-running the
        whole evaluation — is the cheap way to get a usable reply. Each attempt
        re-issues the section's own prompt, so nothing outside this section is
        touched (no other section, no earlier result).

        `report` is the scheduler's phase channel: each re-ask flips the
        worker back to a live "sending" phase with a fresh timer, and back to
        "parsing" when the reply lands — without it the panel would show
        "parsing 250s" while whole requests are in flight.

        Returns the last result whether it parsed or not. The caller reports
        the section; the events rail gets one short line per attempt so the
        user sees the retry without a wall of provider text.
        """
        attempts = 0
        result = first
        prompt = build_judge_prompt(
            rows=task.extra["rows"],
            criteria=self.criteria,
            src_lang=self._target.src_lang,
            tgt_lang=self._target.tgt_lang,
            style_prompt=self._target.style_prompt,
            context_map=self._section_context(task.extra["rows"], contexts),
            custom_placeholders=self._target.custom_placeholders,
            grading_mode=self._grading_mode,
            # Same data the first attempt had: a retry whose prompt drops the
            # FITTING lines (but keeps the criterion) would re-judge without
            # the measurements the verdict is required to quote.
            fitting_map=self._fitting_map(task.extra["rows"]),
        )
        while attempts < MAX_FORMAT_RETRIES and result.unparsed:
            if self._stop.is_set():
                break
            attempts += 1
            retry_msg = f"retry {attempts}/{MAX_FORMAT_RETRIES} (invalid format)"
            self._retry_event(
                f"Request {index} · invalid Judge format · retry {attempts}/{MAX_FORMAT_RETRIES}"
            )
            self._report_phase(report, "retry", message=retry_msg)
            self._report_phase(report, "request", message=retry_msg)
            content, error = self._one_request(
                prompt, label=f"judge retry {index}",
                on_phase=lambda phase, active: self._report_phase(report, "request", message=retry_msg),
                on_event=self._retry_exec_event(index),
            )
            self._report_phase(report, "parse")
            if error:
                result.unparsed = True
                result.error = error
                self._retry_event(f"Request {index} · retry failed")
                break
            retried = parse_evaluation(content or "", graded=self.graded)
            if not retried.unparsed:
                self._retry_event(f"Request {index} · recovered")
                return retried
            result = retried
        if result.unparsed:
            self._retry_event(f"Request {index} · unevaluated after retries")
        return result

    @staticmethod
    def _report_phase(report, kind: str, message: str = "") -> None:
        """Tell the scheduler's panel a format-retry phase change, if wired."""
        if report is None:
            return
        try:
            report(kind, message=message)
        except TypeError:
            try:
                report(kind)
            except Exception:
                pass
        except Exception:
            pass

    def _retry_exec_event(self, index: int):
        """
        Executor events from a format-retry request, onto the events rail.

        Without this the retry request's own trouble (rate limit, cooldown
        park, server error) was invisible: the attempt just took however long
        it took. One concise line per event, same shape as the ladder's own
        messages, so the rail reads as one story.
        """

        def on_event(event) -> None:
            kind = getattr(event, "kind", "")
            if kind not in ("rate_limit", "cooldown", "server_error", "error"):
                return
            message = getattr(event, "message", "") or getattr(event, "label", "")
            self._retry_event(f"Request {index} · {kind.replace('_', ' ')}: {message}")

        return on_event

    def _retry_event(self, message: str) -> None:
        """One concise retry line on the events rail — never a raw response."""
        self.write_log(f"[retry] {message}")
        if self._on_event is None:
            return
        try:
            self._on_event(
                ScheduleEvent(kind="retry", message=message, batch_num=0)
            )
        except Exception:
            pass

    def _mark_seen(self, result: JudgeResult) -> None:
        """Every row a valid reply gave a verdict for, across all attempts."""
        if not result.unparsed:
            self._rows_seen.update(result.rows_seen)

    def _staged_cache_path(self) -> str:
        base_dir = os.path.dirname(self._target.full_path or self._target.path) or "."
        cache_dir = os.path.join(base_dir, ".uet_cache")
        base_name = os.path.splitext(os.path.basename(self._target.full_path or self._target.path))[0]
        return os.path.join(cache_dir, f"{base_name}_staged_fix.json")

    def _save_staged_cache(self, staged: StagedFix) -> None:
        try:
            path = self._staged_cache_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            data = {
                "target": self._target.path,
                "timestamp": datetime.now().isoformat(),
                "texts": staged.texts,
                "unchanged": staged.unchanged,
                "failed": staged.failed,
                "errors": staged.errors,
            }
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                import json
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception:
            pass

    def load_staged_cache(self) -> StagedFix | None:
        try:
            path = self._staged_cache_path()
            if not os.path.exists(path):
                return None
            import json
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            staged = StagedFix()
            staged.texts = {int(k): v for k, v in data.get("texts", {}).items()}
            staged.unchanged = [int(x) for x in data.get("unchanged", [])]
            staged.failed = [int(x) for x in data.get("failed", [])]
            staged.errors = list(data.get("errors", []))
            return staged
        except Exception:
            return None

    def _clear_staged_cache(self) -> None:
        try:
            path = self._staged_cache_path()
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

    # ── Fix All (staged) ──────────────────────────────────────────────────────

    def fix_all(
        self,
        rows: list[int] | None = None,
        comments: dict[int, str] | None = None,
        report: JudgeReport | None = None,
        severities: list[str] | None = None,
        criteria: list[str] | None = None,
        batch_size: int | None = None,
    ) -> StagedFix:
        """
        Repair the flagged rows and return the results *unsaved* (FR-110/111).

        Only the rows given are touched — never a row that passed — and nothing
        is written: the caller reviews the staged text and calls `apply_fix`
        with whatever it approves. Stopping midway leaves the file untouched by
        construction, because no writer is opened here at all.

        `criteria` and `severities` narrow the work twice over: the row list is
        filtered here, and the surviving categories are stated in the prompt, so
        a run sent to fix placeholders does not quietly rewrite anybody's
        register on the way past.

        `rows` may be omitted when a `report` is given, in which case every
        flagged row matching the filters is repaired.
        """
        staged = StagedFix()

        wanted = [row for row in dict.fromkeys(rows or [])]
        if not wanted and report is not None:
            wanted = report.rows_matching(criteria, severities)
        elif wanted and report is not None and (criteria or severities):
            # An explicit list still respects the filters, so a caller cannot
            # accidentally repair a severity the user excluded.
            allowed = set(report.rows_matching(criteria, severities))
            wanted = [row for row in wanted if row in allowed]
        if not wanted:
            return staged

        all_rows, contexts = self.load_rows()
        by_row = {row: (source, translation) for row, source, translation in all_rows}
        if report is not None:
            for row in wanted:
                if row in by_row:
                    source, _old = by_row[row]
                    by_row[row] = (source, report.translation_for(row) or _old)

        targets = [
            (row, by_row[row][0], by_row[row][1]) for row in wanted if row in by_row
        ]
        if not targets:
            return staged

        notes = dict(comments or {})
        if report is not None:
            for row in wanted:
                notes.setdefault(row, report.comment_for(row))

        # A repair batch is sized by the caller when it says so, and by the saved
        # Settings default when it does not: the wizard passes its own figure, and
        # a scripted call gets the same number the user configured rather than a
        # constant that ignores it (CR-002 FR-612/FR-613).
        rows_per_request = max(1, int(batch_size or _saved_fix_rows()))
        sections = estimator.split_sections(
            targets, max_tokens=self._max_tokens, max_rows=rows_per_request
        )
        # What the original said, so an unchanged reply can be told from a repair.
        before = {row: translation for row, _s, translation in targets}

        # Spec 3.5: a fix the re-measure rejects is retried (the reason is fed
        # back into the note), at most MAX_FITTING_RETRIES rounds, before the
        # row lands in staged.failed with a [FITTING_STILL_FAIL] reason.
        MAX_FITTING_RETRIES = 2
        fitting_retries: dict[int, tuple[int, str]] = {}
        retry_rows: set[int] = set()

        def build_prompt(task: BatchTask) -> str:
            return build_fix_prompt(
                rows=task.extra["rows"],
                comments=notes,
                criteria=criteria or self.criteria,
                src_lang=self._target.src_lang,
                tgt_lang=self._target.tgt_lang,
                style_prompt=self._target.style_prompt,
                context_map=self._section_context(task.extra["rows"], contexts),
                custom_placeholders=self._target.custom_placeholders,
                severities=severities,
                fitting_map=self._fitting_map(task.extra["rows"]),
            )

        def build_solo(row: int, task: BatchTask) -> str:
            source, translation = by_row.get(row, ("", ""))
            fitting_info = self._fitting_map([(row, source, translation)]).get(row, "")
            return build_fix_solo_prompt(
                row=row,
                source=source,
                translation=translation,
                comment=notes.get(row, ""),
                src_lang=self._target.src_lang,
                tgt_lang=self._target.tgt_lang,
                style_prompt=self._target.style_prompt,
                context=contexts.get(row, ""),
                custom_placeholders=self._target.custom_placeholders,
                criteria=criteria or self.criteria,
                severities=severities,
                fitting=fitting_info,
            )

        def parse(raw: str, task: BatchTask, report=None) -> BatchResult:
            # Local-only parse (the fix path has no format-retry ladder), so
            # the scheduler's `report` channel is accepted and unused.
            expected = task.row_numbers
            from ..parsing.recovery import ParseRecoveryPipeline

            batch_source = {rn: by_row.get(rn, ("", ""))[0] for rn in expected}
            parsed = ParseRecoveryPipeline().recover(raw, expected, source_map=batch_source)
            results = {
                row: text.strip() for row, text in parsed.items() if (text or "").strip()
            }
            missing = [row for row in expected if row not in results]
            return BatchResult(results=results, failed_rows=missing)

        def parse_solo(raw: str, row: int, task: BatchTask) -> str:
            source, _ = by_row.get(row, ("", ""))
            return parse_fixed_line(raw, source=source)

        def collect(task: BatchTask, result: BatchResult) -> None:
            for row, text in result.results.items():
                text = (text or "").strip()
                if not text:
                    continue
                if text == (before.get(row) or "").strip():
                    # The model was told to return a line untouched rather than
                    # guess when the fix would change what the source means, so
                    # an identical reply is a decision. Staging it as a repair
                    # would put "Repaired and approved" on a row nobody fixed.
                    staged.unchanged.append(row)
                    self._emit_fix(row, text, changed=False)
                    continue
                # Fitting gate (spec 3.5): check the proposed fix against the
                # window and the token whitelist before staging. A rejected fix
                # is not a failure yet — it is queued for a solo retry with the
                # gate's reason in its correction note (up to 2 rounds below).
                if "fitting" in (criteria or self.criteria):
                    fail = self.revalidate_fix(row, text, by_row.get(row, ("", ""))[0])
                    if fail:
                        prev_attempts = fitting_retries.get(row, (MAX_FITTING_RETRIES, ""))[0]
                        retries_left = prev_attempts - 1
                        fitting_retries[row] = (retries_left, fail)
                        if retries_left >= 0:
                            retry_rows.add(row)
                            continue
                        staged.failed.append(row)
                        staged.errors.append(f"row {row}: [FITTING_STILL_FAIL] {fail}")
                        continue
                staged.texts[row] = text
                self._emit_fix(row, text, changed=True)
            staged.failed.extend(
                row for row in result.failed_rows
                if row not in staged.texts and row not in staged.unchanged
            )
            # Autosave staged fixes to disk cache in real-time so large runs (e.g. 80k rows) never lose work
            self._save_staged_cache(staged)
            # Counted per request, not per repaired row: a batch where the model
            # left every line alone produces no `_emit_fix` at all, and a bar fed
            # only by those would sit at 0% through a run that is working
            # correctly.
            self._emit_section(
                task.extra.get("index", task.batch_num), len(sections),
                list(task.row_numbers), ok=not result.failed_rows,
            )

        tasks = [
            BatchTask(
                batch_num=index,
                total_batches=len(sections),
                rows=[(row, translation) for row, _s, translation in section],
                extra={"rows": section, "index": index},
            )
            for index, section in enumerate(sections, start=1)
        ]

        self._run_tasks(
            tasks, build_prompt, parse, None,
            on_result=collect, solo_prompt_builder=build_solo, solo_parser=parse_solo,
            errors=staged.errors,
        )

        # Fitting retry rounds (spec 3.5): each rejected fix is re-requested on
        # its own with the gate's reason in the note, so the model knows WHY it
        # must compress harder. The gate re-runs on every round.
        round_num = 0
        while retry_rows and round_num < MAX_FITTING_RETRIES:
            round_num += 1
            self._notify(
                "info",
                f"Fitting re-measure rejected {len(retry_rows)} fix(es) — "
                f"retrying with the reason fed back (round {round_num}/{MAX_FITTING_RETRIES}).",
            )
            current = sorted(retry_rows)
            retry_rows.clear()
            retry_tasks = []
            for index, row in enumerate(current, start=1):
                source, translation = by_row.get(row, ("", ""))
                fail_kind = fitting_retries.get(row, (0, ""))[1]
                retry_notes = dict(notes)
                retry_notes[row] = (
                    f"{notes.get(row, '')} "
                    f"[FITTING RETRY {round_num}/{MAX_FITTING_RETRIES}: the previous fix "
                    f"failed length validation ({fail_kind}) — compress the line so it "
                    f"matches the original length, "
                    f"or insert the source's line-break tag at a natural pause]"
                ).strip()
                retry_tasks.append(BatchTask(
                    batch_num=index,
                    total_batches=len(current),
                    rows=[(row, translation)],
                    extra={
                        "rows": [(row, source, translation)],
                        "index": index,
                        "note_override": retry_notes,
                    },
                ))

            def retry_build_prompt(task: BatchTask) -> str:
                return build_fix_prompt(
                    rows=task.extra["rows"],
                    comments=task.extra["note_override"],
                    criteria=criteria or self.criteria,
                    src_lang=self._target.src_lang,
                    tgt_lang=self._target.tgt_lang,
                    style_prompt=self._target.style_prompt,
                    context_map=self._section_context(task.extra["rows"], contexts),
                    custom_placeholders=self._target.custom_placeholders,
                    severities=severities,
                    fitting_map=self._fitting_map(task.extra["rows"]),
                )

            def retry_build_solo(row: int, task: BatchTask) -> str:
                source, translation = by_row.get(row, ("", ""))
                fitting_info = self._fitting_map([(row, source, translation)]).get(row, "")
                return build_fix_solo_prompt(
                    row=row,
                    source=source,
                    translation=translation,
                    comment=task.extra["note_override"].get(row, ""),
                    src_lang=self._target.src_lang,
                    tgt_lang=self._target.tgt_lang,
                    style_prompt=self._target.style_prompt,
                    context=contexts.get(row, ""),
                    custom_placeholders=self._target.custom_placeholders,
                    criteria=criteria or self.criteria,
                    severities=severities,
                    fitting=fitting_info,
                )

            self._run_tasks(
                retry_tasks, retry_build_prompt, parse, None,
                on_result=collect, solo_prompt_builder=retry_build_solo,
                solo_parser=parse_solo, errors=staged.errors,
            )

        staged.tokens_used = self._tokens_used
        staged.unchanged = [row for row in dict.fromkeys(staged.unchanged)]
        staged.failed = [row for row in dict.fromkeys(staged.failed)]
        if staged.unchanged:
            self._notify(
                "info",
                f"{len(staged.unchanged)} row(s) were left unchanged — the model "
                f"could not fix them without changing what the source means.",
            )
        self.write_log(
            f"Fix All staged: {len(staged.texts)} fixed, {len(staged.unchanged)} unchanged, {len(staged.failed)} failed"
        )
        self._save_staged_cache(staged)
        return staged

    def apply_fix(
        self,
        texts: dict[int, str],
        approved: list[int] | None = None,
    ) -> int:
        """
        Write the approved rows to the destination column. Returns how many.

        The only writer in the package, and the only thing in the whole feature
        that changes a file. `approved` is required in spirit even though it
        defaults to "everything staged": the caller passes the rows a human
        ticked, and a row that is not in that list is never written, even when
        the model produced something for it (AC-103).
        """
        rows = list(approved) if approved is not None else sorted(texts)
        payload = {
            row: texts[row]
            for row in rows
            if row in texts and (texts[row] or "").strip()
        }
        if not payload:
            return 0

        writer = ExcelWriter(
            self._target.path,
            mode="safe",
            on_event=self._writer_event,
            fallback_range=app_prefs.fallback_save_range(),
        )
        try:
            writer.flush(payload, self._target.dst_col)
            self.write_log(f"Applied fixes: wrote {len(payload)} approved rows to col {self._target.dst_col}")
        finally:
            # commit() joins the writer thread and re-raises whatever it hit,
            # so a lock or a permission error surfaces here rather than being
            # lost in a background thread.
            writer.commit()
        if self._target.map_path and self._target.full_path and os.path.exists(self._target.map_path):
            if os.path.abspath(writer.active_path) != os.path.abspath(self._target.full_path):
                from ..core.dedup_file import sync_unique_to_full
                sync_unique_to_full(
                    self._target.map_path, writer.active_path,
                    self._target.full_path, self._target.dst_col,
                )
        from ..core.failed_cache import clear_failed_rows
        fixed_rows = list(payload.keys())
        clear_failed_rows(self._target.path, self._target.dst_col, fixed_rows)
        if getattr(self._target, "full_path", None) and self._target.full_path != self._target.path:
            full_rows = list(fixed_rows)
            if getattr(self._target, "map_path", None) and os.path.exists(self._target.map_path):
                try:
                    import json
                    with open(self._target.map_path, "r", encoding="utf-8") as f:
                        mapping = json.load(f)
                    rep_to_full = mapping.get("rep_to_full", {})
                    mapped_keys: list[int] = []
                    for k in fixed_rows:
                        mapped = rep_to_full.get(str(k)) or rep_to_full.get(k)
                        if mapped:
                            mapped_keys.extend(mapped)
                        else:
                            mapped_keys.append(k)
                    full_rows = mapped_keys
                except Exception:
                    full_rows = list(fixed_rows)
            clear_failed_rows(self._target.full_path, self._target.dst_col, set(full_rows) | set(fixed_rows))
        self._clear_staged_cache()
        return len(payload)

    # ── Single row ────────────────────────────────────────────────────────────

    def fix_single(
        self,
        row: int,
        comment: str = "",
        source: str | None = None,
        translation: str | None = None,
    ) -> SingleFix:
        """
        Repair one row (FR-109). One request, and nothing is written.

        The caller shows a small indicator rather than the worker panel while
        this runs — a panel with one line in it is not a panel (FR-208).
        """
        if source is None or translation is None:
            rows, _contexts = self.load_rows()
            found = next(((s, t) for r, s, t in rows if r == row), None)
            if found is None:
                return SingleFix(row=row, error=f"Row {row} is not in the range.")
            source, translation = found

        contexts = self._context_for(row)
        fitting_info = self._fitting_map([(row, source, translation)]).get(row, "")
        prompt = build_fix_solo_prompt(
            row=row,
            source=source,
            translation=translation,
            comment=comment,
            src_lang=self._target.src_lang,
            tgt_lang=self._target.tgt_lang,
            style_prompt=self._target.style_prompt,
            context=contexts,
            custom_placeholders=self._target.custom_placeholders,
            criteria=self.criteria,
            fitting=fitting_info,
        )
        content, error = self._one_request(prompt, label=f"fix row {row}")
        if error:
            return SingleFix(row=row, error=error)
        text = parse_fixed_line(content or "", source=source)
        if not text:
            return SingleFix(
                row=row, error="The model returned nothing usable for this row."
            )
        if "fitting" in self.criteria:
            fail = self.revalidate_fix(row, text, source)
            if fail:
                return SingleFix(row=row, error=f"[FITTING_STILL_FAIL] {fail}")
        return SingleFix(row=row, text=text)

    def judge_again(
        self,
        row: int,
        criteria: list[str] | None = None,
        text: str | None = None,
        source: str | None = None,
        history: str | None = None,
    ) -> RejudgeVerdict:
        """
        Re-check one row after a fix (FR-112).

        `text` is the candidate to judge — usually the staged fix, which has not
        been written yet, so it has to be passed in rather than read back from
        the file.

        `criteria` narrows this one call and nothing else. It used to be assigned
        onto the runner, so re-judging a row for `style` alone quietly narrowed
        every later evaluation and Fix All in the same session to `style` too.
        """
        wanted = self._criteria_for(criteria)
        if source is None or text is None:
            rows, _contexts = self.load_rows()
            found = next(((s, t) for r, s, t in rows if r == row), None)
            if found is None:
                return RejudgeVerdict(row=row, error=f"Row {row} is not in the range.")
            file_source, file_text = found
            source = source if source is not None else file_source
            text = text if text is not None else file_text

        fitting_info = self._fitting_map([(row, source or "", text or "")]).get(row, "")
        h_text = history if (history and _saved_context_aware_rejudge()) else ""
        prompt = build_judge_single_prompt(
            row=row,
            source=source,
            translation=text,
            criteria=wanted,
            src_lang=self._target.src_lang,
            tgt_lang=self._target.tgt_lang,
            style_prompt=self._target.style_prompt,
            context=self._context_for(row),
            custom_placeholders=self._target.custom_placeholders,
            grading_mode=self._grading_mode,
            fitting=fitting_info,
            history=h_text,
        )
        content, error = self._one_request(prompt, label=f"judge row {row}")
        if error:
            return RejudgeVerdict(row=row, error=error)

        result = parse_single(content or "", row, graded=self.graded)
        if result.unparsed:
            # Prose is not a pass. The row stays flagged and the reply is kept
            # so the user can read what the model actually said.
            return RejudgeVerdict(row=row, passed=False, unparsed=True,
                                  raw=result.raw)
        issue = result.issues.get(row)
        return RejudgeVerdict(row=row, passed=issue is None, issue=issue,
                              raw=result.raw)

    # ── Execution ─────────────────────────────────────────────────────────────

    def _run_tasks(
        self,
        tasks: list[BatchTask],
        build_prompt,
        parse,
        sections: dict[int, JudgeSection] | None,
        on_result=None,
        solo_prompt_builder=None,
        solo_parser=None,
        errors: list[str] | None = None,
    ) -> None:
        """
        Run a list of sections, pooled if possible and directly if not.

        The pooled path is the reason ADR-001 exists: retries, cooldowns,
        connection rotation and the Activity Panel all come from machinery that
        already works. The direct path exists so a single request — or a test —
        does not need a pool at all.
        """
        # Each call is one operation — an evaluation, or a Fix All — and each
        # numbers its sections from 1, so the dedupe set has to start empty or the
        # second operation would report nothing.
        self._reported_sections = set()
        self._rows_seen = set()
        # How many requests this operation is, for the failure path: an event
        # carries a batch number but not the total, and `sections` is None on the
        # Fix All path.
        self._section_total = len(tasks)
        if self._pool is not None and self._job is not None:
            self._run_pooled(tasks, build_prompt, parse, sections, on_result,
                             solo_prompt_builder, solo_parser, errors)
        else:
            self._run_direct(tasks, build_prompt, parse, sections, on_result,
                             solo_prompt_builder, solo_parser, errors)

    def _run_pooled(
        self,
        tasks: list[BatchTask],
        build_prompt,
        parse,
        sections,
        on_result,
        solo_prompt_builder,
        solo_parser,
        errors,
    ) -> None:
        def result_sink(task: BatchTask, result: BatchResult) -> None:
            if on_result is not None:
                on_result(task, result)

        scheduler = Scheduler(
            job=self._job,
            connection_pool=self._pool,
            base_url=getattr(self._provider, "base_url", ""),
            prompt_builder=build_prompt,
            parse_response=parse,
            on_result=result_sink,
            on_status=self._on_status,
            on_event=self._forward_event(sections, errors),
            stop_event=self._stop,
            solo_prompt_builder=solo_prompt_builder,
            solo_parser=solo_parser,
            raw_log=self._log_raw if self._raw_logger else None,
            registry=self._registry,
        )
        try:
            stats = scheduler.run(tasks, total_rows=sum(task.size for task in tasks))
            self._tokens_used += stats.tokens_consumed
        except AllConnectionsUnavailable as exc:
            # Consistent with translate: the batch stops and says why.
            message = str(exc)
            if errors is not None:
                errors.append(message)
            if sections:
                for section in sections.values():
                    if not section.error:
                        section.error = message
            self._notify("error", message)

    def _forward_event(self, sections, errors):
        """Record per-section failures while passing events on to the screen."""

        def handler(event) -> None:
            if event.kind == "aborted" and event.message:
                # Every connection — or every model in this combo — has failed.
                # It ends the operation, so it goes out on the notice channel as
                # well as the event rail: the user must not have to infer it from
                # a run that simply stopped (FEATURE-005 total-failure rule).
                self._notify("error", event.message)
            if event.kind in ("batch_failed", "aborted"):
                if errors is not None and event.message:
                    errors.append(event.message)
                if sections is not None:
                    section = sections.get(event.batch_num)
                    if section is not None and not section.error:
                        section.error = event.message or "request failed"
                # A failed request is finished work, so it counts towards the
                # progress figure. A bar that only advances on success sits still
                # through a run that is failing, which is the one time it most
                # needs to be moving.
                self._emit_section(
                    event.batch_num, self._section_total,
                    list(getattr(event, "rows", []) or []), ok=False,
                )
            if self._on_event is not None:
                self._on_event(event)

        return handler

    def _run_direct(
        self,
        tasks: list[BatchTask],
        build_prompt,
        parse,
        sections,
        on_result,
        errors,
        solo_prompt_builder=None,
        solo_parser=None,
    ) -> None:
        for task in tasks:
            if self._stop.is_set():
                break
            index = task.extra.get("index", task.batch_num)
            content, error = self._one_request(
                build_prompt(task), label=f"section {task.batch_num}"
            )
            if error:
                if errors is not None:
                    errors.append(error)
                if sections is not None:
                    section = sections.get(index)
                    if section is not None:
                        section.error = error
                if on_result is not None:
                    on_result(task, BatchResult(failed_rows=list(task.row_numbers)))
                else:
                    # `parse` never ran, so nothing else will report this section
                    # — and a progress figure that skips failures stalls.
                    self._emit_section(
                        index, len(tasks), list(task.row_numbers), ok=False
                    )
                continue
            try:
                result = parse(content or "", task, None)
            except Exception as exc:            # a parse must not sink the run
                message = f"parse error: {exc}"
                if errors is not None:
                    errors.append(message)
                if sections is not None:
                    section = sections.get(index)
                    if section is not None:
                        section.error = message
                self._emit_section(
                    index, len(tasks), list(task.row_numbers), ok=False
                )
                continue
            # Direct path's counterpart to the pooled recovery ladder: rows the
            # section-level retry left without a verdict are re-asked one at a
            # time (the same solo prompts the pooled path would have used).
            if result.failed_rows and solo_prompt_builder is not None \
                    and solo_parser is not None:
                for row_num in list(result.failed_rows):
                    if self._stop.is_set():
                        break
                    solo_content, solo_error = self._one_request(
                        solo_prompt_builder(row_num, task), label=f"solo {row_num}"
                    )
                    if solo_error:
                        continue
                    if solo_parser(solo_content or "", row_num, task):
                        result.failed_rows.remove(row_num)
            if on_result is not None:
                on_result(task, result)

    def _one_request(
        self, prompt: str, label: str = "", on_phase=None, on_event=None
    ) -> tuple[str | None, str]:
        """
        One request, on whichever path is available.

        Returns `(content, error)` rather than raising: a failed Judge request
        is an answer to show, exactly as it is in the playground, and the
        provider's own words are what gets shown.

        `on_phase` / `on_event` are the scheduler's panel channels, wired for
        the format-retry ladder so its requests are visible as a live
        "sending" phase and its rate-limit / cooldown events reach the rail.

        The pooled branch runs a **single attempt**: the callers here are
        already retry loops of their own (the format-retry ladder re-asks up
        to three times; solo recovery walks the rows), and letting the
        executor loop inside each step multiplied one visible "retry" into
        up to three hidden ones plus connection cooldowns — one step could
        outlast three request timeouts. One attempt per call keeps every
        step bounded by a single timeout, which is what the panel and the
        user both expect a "retry" to mean.
        """
        from ..core.models import ProviderError

        if self._pool is not None and self._job is not None:
            from ..scheduling.retry import RetryExecutor

            executor = RetryExecutor(
                connection_pool=self._pool,
                job=self._single_attempt_job(),
                base_url=getattr(self._provider, "base_url", ""),
                stop_event=self._stop,
                on_phase=on_phase,
                on_event=on_event,
            )
            outcome = executor.execute(prompt, label=label, rows=1)
            self._tokens_used += outcome.tokens
            self._log_raw(label, prompt, outcome.content, outcome.latency)
            if self.write_log:
                self.write_log(f"direct request: {label} (tokens: {outcome.tokens}, latency: {outcome.latency:.2f}s)")
            if outcome.content is None:
                return None, outcome.error or "The request failed."
            return outcome.content, ""

        client = self._direct_client()
        if client is None:
            return None, "No provider is configured for this evaluation."
        started = datetime.now().timestamp()
        try:
            reply = client.generate(
                model=self._model_id(),
                prompt=prompt,
                temperature=self._temperature(),
                reasoning=self._reasoning(),
                max_completion_tokens=FIX_MAX_TOKENS,
            )
            latency = datetime.now().timestamp() - started
            tokens = getattr(reply, "tokens", 0) or 0
            content = getattr(reply, "content", "") or ""
            self._tokens_used += tokens
            self._log_raw(label, prompt, content, latency)
            self.write_log(f"client request: {label} (tokens: {tokens}, latency: {latency:.2f}s)")
            return content, ""
        except ProviderError as exc:
            latency = datetime.now().timestamp() - started
            err_text = exc.response_text.strip() or str(exc)
            self._log_raw(label, prompt, None, latency)
            self.write_log(f"client error: {label} - {err_text}")
            return None, err_text
        except Exception as exc:
            latency = datetime.now().timestamp() - started
            err_text = f"{type(exc).__name__}: {exc}"
            self._log_raw(label, prompt, None, latency)
            self.write_log(f"client exception: {label} - {err_text}")
            return None, err_text

    def _single_attempt_job(self) -> JobConfig:
        """
        This job with the executor's inner retry loop switched off.

        Only the retry knobs change: the single-attempt contract lives here
        while the job itself (model, temperature, reasoning, pacing) stays
        exactly what the operation was configured with, so a format-retry
        request is the same request the first attempt was — just not wrapped
        in another ladder.
        """
        import dataclasses

        return dataclasses.replace(
            self._job, max_attempts=1, retry_mode="fixed",
        )

    # ── Plumbing ──────────────────────────────────────────────────────────────

    def _direct_client(self):
        if self._client is None and self._client_factory is not None:
            self._client = self._client_factory()
            # The direct path must honour a force-cancel too. The pooled path
            # gets this for free (its executor builds clients with the stop
            # event); a factory-built client knows nothing about it, so a real
            # ProviderClient gets the event attached and registered here. A
            # client that already carries an event — or a test fake that is
            # not a ProviderClient at all — is left exactly as built.
            client = self._client
            if self._stop is not None:
                from ..providers.client import ProviderClient, _register_abortable

                if isinstance(client, ProviderClient) and client.abort_event is None:
                    client.abort_event = self._stop
                    _register_abortable(self._stop, client)
        return self._client

    def _model_id(self) -> str:
        return getattr(self._job, "model_id", "") if self._job else ""

    def _temperature(self) -> float:
        if self._job and getattr(self._job, "temperature", None) is not None:
            return float(self._job.temperature)
        return 0.0

    def _chain_enabled(self) -> bool:
        """The dialogue-chain setting, read once per prompt build."""
        return _saved_chain_context()

    def _chain_window(self) -> int:
        return _saved_chain_window()

    def _comment_style(self) -> str:
        """The comment-style setting: auto / explicit / critical."""
        return _saved_comment_style()

    def _reasoning(self):
        """
        The reasoning spec for a direct request.

        Resolved from the model's own metadata plus whatever the Judge setup
        chose; with no job at all (a test) nothing is sent, which is the safe
        fallback the adapter guarantees.
        """
        if self._job is None:
            return None
        from ..providers.reasoning import resolve_for

        model = None
        if self._provider is not None:
            for connection in getattr(self._provider, "connections", []):
                found = connection.find_model(self._job.model_id)
                if found is not None:
                    model = found
                    break
        return resolve_for(
            model,
            override=self._job.reasoning_enabled,
            level=self._job.reasoning_effort,
        )

    def _section_context(self, rows, contexts: dict[int, str]) -> dict[int, str] | None:
        if not contexts:
            return None
        subset = {
            row: contexts[row] for row, _s, _t in rows if contexts.get(row)
        }
        return subset or None

    def _context_for(self, row: int) -> str:
        if not self._target.ctx_col:
            return ""
        try:
            values = ExcelReader.load_rows_at_offsets(
                self._target.path, self._target.ctx_col, row, [0]
            )
        except Exception:
            return ""
        return values[0][1] if values else ""

    def _writer_event(self, kind: str, message: str) -> None:
        self._notify("warn" if kind == "locked" else "info", message)

    def _notify(self, kind: str, message: str) -> None:
        self.write_log(f"[{kind}] {message}")
        if self._on_notice is not None:
            self._on_notice(kind, message)

    def _emit_row(self, row: int, issue: "RowIssue | None") -> None:
        """
        One reviewed row, as it lands.

        Swallows callback errors: a screen that throws while painting must not
        take down the evaluation that was feeding it.
        """
        if self._on_row is None:
            return
        try:
            self._on_row(row, issue)
        except Exception:
            pass

    def _emit_fix(self, row: int, text: str, changed: bool) -> None:
        """One repaired row, as it lands, for a live Fix All preview."""
        if self._on_fix is None:
            return
        try:
            self._on_fix(row, text, changed)
        except Exception:
            pass

    def _emit_section(
        self, index: int, total: int, rows: list[int], ok: bool
    ) -> None:
        """
        One finished request, however it went — reported at most once.

        The request-level counterpart to `_emit_row`, and the only progress signal
        that survives a section which returns nothing: a failed or prose reply
        produces no rows, so a screen counting only rows would show a bar that
        stopped moving.

        The dedupe is not defensive tidiness. `batch_failed` is emitted per solo
        row as well as per batch by the recovery ladder, so a section that limps
        home after three solo retries would otherwise be counted four times and
        push the figure past its own total.
        """
        if self._on_section is None or index in self._reported_sections:
            return
        self._reported_sections.add(index)
        try:
            self._on_section(index, total, list(rows), ok)
        except Exception:
            pass

    def request_stop(self) -> None:
        self._stop.set()


def _saved_max_tokens() -> int:
    """The Settings value, or the shipped default if prefs are unreadable."""
    try:
        from ..data.app_prefs import max_tokens_per_request

        return max_tokens_per_request()
    except Exception:
        return config.MAX_TOKENS_PER_REQUEST


def _saved_max_rows() -> int:
    try:
        from ..data.app_prefs import max_rows_per_request

        return max_rows_per_request()
    except Exception:
        return config.DEFAULT_MAX_ROWS_PER_REQUEST


def _saved_fix_rows() -> int:
    """The Settings default for Fix All, or the shipped one if prefs are unreadable."""
    try:
        from ..data.app_prefs import fix_batch_rows

        return fix_batch_rows()
    except Exception:
        return DEFAULT_FIX_BATCH_ROWS


def _saved_chain_context() -> bool:
    """Whether Judge runs in dialogue-chain mode (Settings, default off)."""
    try:
        from ..data.app_prefs import judge_chain_context

        return judge_chain_context()
    except Exception:
        return False


def _saved_chain_window() -> int:
    """Neighbour rows per side for a chain-mode re-judge, 1–5."""
    try:
        from ..data.app_prefs import judge_chain_window

        return judge_chain_window()
    except Exception:
        return 1


def _saved_context_aware_rejudge() -> bool:
    """Whether Rejudge passes prior critique/finding context (Settings, default True)."""
    try:
        from ..data.app_prefs import judge_context_aware_rejudge

        return judge_context_aware_rejudge()
    except Exception:
        return True


def _saved_comment_style() -> str:
    """The comment-style setting (auto / explicit / critical)."""
    try:
        from ..data.app_prefs import judge_comment_style

        return judge_comment_style()
    except Exception:
        return "auto"


def _clean_grading_mode(mode: str | None) -> str:
    """
    The grading mode to use: the caller's, else Settings, else `severity`.

    Never raises and never returns something a prompt could not render — prefs
    are a convenience, and an evaluation must not fail because one is unreadable.
    """
    if mode in GRADING_MODES:
        return mode           # type: ignore[return-value]
    try:
        from ..data.app_prefs import judge_grading_mode

        stored = judge_grading_mode()
    except Exception:
        stored = None
    return stored if stored in GRADING_MODES else GRADING_MODES[0]
