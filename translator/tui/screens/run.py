"""
tui/screens/run.py — Run screen and summary.

Two-column layout (TUI_PREVIEW.md §8):
  left  — the translation feed: source line, translation indented under it,
          rows appearing as their batch starts and filling in when it lands
  right — a fixed sidebar of process state that updates in place and never
          scrolls: progress, row counts, the Activity Panel, model, events

The engine runs in a worker thread; every UI update crosses back through
`call_from_thread`. Nothing streams tokens — a row shows a spinner until its
batch completes, then the whole result appears.

The sidebar owns a single repaint timer and drives everything in the rail from
it, the Activity Panel included: a worker changing phase four times inside one
interval costs one paint, not four (FEATURE-002 NFR-201).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import threading
import time

from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Label, ProgressBar, Static

from ... import config
from ...core.engine import ProcessingEngine
from ...io.excel_io import ExcelReader
from ...core.models import BatchResult, JobConfig, ProcessStats
from ...scheduling.connection_pool import AllConnectionsUnavailable, SwitchEvent
from ...scheduling.pools import build_pool
from ...scheduling.scheduler import BatchTask, ScheduleEvent, WorkerStatus
from ..app import BaseScreen, MenuScreen
from ..theme import ACCENT, ERROR, MUTED, OK, WARN
from ..widgets.activity import (
    ActivityHeader,
    ActivityPanel,
    EventsHeader,
    EventsPanel,
    WorkerLedger,
)
from ..widgets.tab_bar import FileTabBar
from ..widgets.common import (
    Option,
    TranslationFeed,
    escape_markup,
    spinner_frame,
)

SIDEBAR_WIDTH = 36
NARROW_TERMINAL = 100

# The rail's repaint interval. Every animated thing in the sidebar — spinners,
# running durations, the Activity Panel — is driven from this one timer, so the
# cost of the sidebar does not grow with the number of workers.
SIDEBAR_INTERVAL = 0.12


class Sidebar(Static):
    """
    The right rail. It owns its own repaint timer: a timer created on a
    *widget* keeps ticking, whereas one created on the Screen during mount
    does not, so the animation lives here rather than on RunScreen.
    """

    def __init__(self, build, **kwargs) -> None:
        super().__init__(**kwargs)
        self._build = build
        self.tick = 0

    def on_mount(self) -> None:
        self.set_interval(SIDEBAR_INTERVAL, self._repaint)

    def _repaint(self) -> None:
        self.tick += 1
        try:
            self.update(self._build(self.tick))
        except Exception:
            pass      # never let a paint error kill the run screen


class FeedPane(VerticalScroll):
    """
    The scroller around the feed. Wheeling it has to mean what the arrow keys
    already mean — scrolling back detaches follow, reaching the bottom
    re-attaches it — or the next batch would yank the view out from under
    whoever is reading it.

    Textual dispatches this handler before the built-in one that does the
    scrolling, so the position read here is the pre-scroll one, exactly as in
    `action_scroll_feed`.
    """

    def on_mouse_scroll_up(self, _event: events.MouseScrollUp) -> None:
        detach = getattr(self.screen, "_detach_follow", None)
        if detach is not None:
            detach()

    def on_mouse_scroll_down(self, _event: events.MouseScrollDown) -> None:
        attach = getattr(self.screen, "_attach_follow", None)
        if attach is not None and self.scroll_target_y >= self.max_scroll_y - 3:
            attach()


@dataclass
class TranslateSession:
    index: int
    job: JobConfig
    engine: ProcessingEngine | None = None
    started: float = field(default_factory=time.time)
    workers: dict[int, WorkerStatus] = field(default_factory=dict)
    ledger: WorkerLedger = field(default_factory=WorkerLedger)
    events: list[str] = field(default_factory=list)
    last_log_body: str = ""
    last_log_count: int = 0
    error_tally: dict[str, int] = field(default_factory=dict)
    switches: list[str] = field(default_factory=list)
    done_rows: int = 0
    stats: ProcessStats | None = None
    plan_total: int | None = None
    finished: bool = False
    output_path: str = ""
    file_total_count: int = 0
    stop: threading.Event = field(default_factory=threading.Event)
    feed_lines: list[tuple[int, str, str]] = field(default_factory=list)


class RunScreen(BaseScreen):
    breadcrumb = "Running"

    BINDINGS = [
        Binding("f", "toggle_follow", "follow", show=False),
        Binding("p", "pause", "pause", show=False),
        Binding("c", "pause", "stop", show=False),
        Binding("a", "toggle_workers", "workers", show=False),
        Binding("e", "toggle_events", "events", show=False),
        Binding("d", "worker_detail", "worker detail", show=False),
        Binding("escape", "noop", "", show=False),
        Binding("left", "noop", "", show=False),
        # Scrolling back through the feed detaches follow, like tail -f.
        Binding("up,k", "scroll_feed(-3)", "scroll up", show=False),
        Binding("down,j", "scroll_feed(3)", "scroll down", show=False),
        Binding("pageup", "scroll_feed_page(-1)", "page up", show=False),
        Binding("pagedown", "scroll_feed_page(1)", "page down", show=False),
        Binding("home", "scroll_feed_home", "top", show=False),
        Binding("end", "scroll_feed_end", "bottom", show=False),
        Binding("1", "switch_tab(0)", "tab 1", show=False),
        Binding("2", "switch_tab(1)", "tab 2", show=False),
        Binding("3", "switch_tab(2)", "tab 3", show=False),
        Binding("4", "switch_tab(3)", "tab 4", show=False),
        Binding("bracketleft", "prev_tab", "prev tab", show=False),
        Binding("bracketright", "next_tab", "next tab", show=False),
    ]

    def __init__(self, job: JobConfig, jobs: list[JobConfig] | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.jobs = list(jobs) if jobs else [job]
        self.is_multi_tab = len(self.jobs) > 1
        self.sessions = [
            TranslateSession(
                index=i,
                job=j,
                stats=ProcessStats(total=j.num_rows),
                output_path=j.out_path,
            )
            for i, j in enumerate(self.jobs)
        ]
        self.active_session_index = 0
        self.tab_bar = FileTabBar(
            tabs=[j.src_path for j in self.jobs],
            id="translate-tab-bar",
            on_tab_switch=self.action_switch_tab,
        ) if self.is_multi_tab else None
        self.job = job
        self._stop = threading.Event()
        # When the first `c` (graceful stop) landed; a second press within
        # the window force-severs the in-flight request instead of waiting
        # for the current batch to finish. Same contract as Judge's `c`.
        self._cancel_requested_at = 0.0
        self._engine: ProcessingEngine | None = None
        self._started = time.time()
        self._workers: dict[int, WorkerStatus] = {}
        # A UI-only layer that keeps a worker's Success / Failed / Retrying state
        # on screen ~1s (bug #5/#6/#7). It never holds the engine — the next
        # batch starts immediately — it only stops the panel from skipping the
        # end-of-request frame between two repaints.
        self._ledger = WorkerLedger()
        self._events: list[str] = []
        # The last events-rail line and how many times it has repeated, so an
        # identical error collapses to one line with a count (bug #8).
        self._last_log_body = ""
        self._last_log_count = 0
        self._error_tally: dict[str, int] = {}
        self._switches: list[str] = []
        self._done_rows = 0
        self._stats = ProcessStats(total=job.num_rows)
        # The denominator for the progress bar. `job.num_rows` is the *range* the
        # wizard asked for, which is only the same thing as the work queue on a
        # fresh translate; the engine announces the real figure before the first
        # request and this is replaced then (BUG-001).
        self._plan_total: int | None = None
        self._tick = 0
        self._finished = False
        # Where results actually landed. Normally job.out_path, but a locked
        # output can redirect the writer to a numbered sibling, and the summary
        # must report the real file. Set by the engine thread on completion.
        self._output_path = job.out_path
        self._file_total_count = 0
        if getattr(job, "map_path", "") and os.path.exists(job.map_path):
            try:
                import json
                with open(job.map_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._file_total_count = int(data.get("total_rows", 0))
            except Exception:
                pass

    # ── Layout ────────────────────────────────────────────────────────────────

    def _compose_tab_bar(self):
        if getattr(self, "is_multi_tab", False) and getattr(self, "tab_bar", None) is not None:
            return self.tab_bar
        return None

    def compose_body(self) -> ComposeResult:
        self.feed = TranslationFeed()
        # Right rail, from top: the fixed state block (progress, rows, model),
        # then the worker panel and the events panel — Worker above Events —
        # each in its own bounded scroll area, so a long run can never grow
        # the rail without bound (NFR-201). One timer owns all three: the
        # `Sidebar` paints itself and feeds both panels (NFR-201).
        self.sidebar = Sidebar(self._sidebar_text, id="sidebar")
        self.activity = ActivityPanel(title="Workers", id="activity")
        self.activity_header = ActivityHeader(self.activity, id="activity-header")
        self.activity.header_widget = self.activity_header
        self.events = EventsPanel(id="events")
        self.events_header = EventsHeader(self.events, id="events-header")
        self.events.header_widget = self.events_header
        self.narrow_bar = Static("", id="narrow-bar")
        self.narrow_bar.display = False

        yield self.narrow_bar
        with Horizontal(id="run-columns"):
            with FeedPane(id="feed-pane"):
                yield self.feed
            with Vertical(id="sidebar-pane"):
                yield self.sidebar
                yield self.activity_header
                with VerticalScroll(id="workers-scroll"):
                    yield self.activity
                yield self.events_header
                with VerticalScroll(id="events-scroll"):
                    yield self.events

    DEFAULT_CSS = f"""
    #run-columns {{ height: 1fr; }}
    #feed-pane {{ width: 1fr; padding-right: 1; }}
    #sidebar-pane {{
        width: {SIDEBAR_WIDTH};
        border-left: solid {MUTED};
        padding-left: 1;
        height: 1fr;
    }}
    #sidebar {{ height: auto; }}
    #activity-header {{ height: 1; }}
    #activity {{ height: auto; width: 1fr; }}
    #workers-scroll {{ height: 1fr; width: 1fr; scrollbar-size: 1 1; }}
    #events-header {{ height: 1; }}
    #events-scroll {{ height: 1fr; width: 1fr; scrollbar-size: 1 1; }}
    #events {{ height: auto; width: 1fr; }}
    #narrow-bar {{ height: auto; color: {MUTED}; }}
    """

    def screen_keys(self) -> list[tuple[str, str]]:
        if self._finished:
            return [("↑↓", "scroll"), ("⏎", "summary")]
        follow = "unfollow" if getattr(self, "feed", None) and self.feed.follow \
            else "follow"
        workers = "hide workers" if not self._workers_collapsed() \
            else "show workers"
        events = "hide events" if not self._events_collapsed() \
            else "show events"
        return [("↑↓", "scroll"), ("f", follow), ("a", workers), ("e", events),
                ("d", "worker detail"), ("c", "stop & save")]

    def on_screen_ready(self) -> None:
        # The scheduler spawns its own worker threads and calls back from
        # them; those threads have no Textual active-app context, so grab a
        # direct reference now rather than touching self.app from there.
        self._app = self.app
        self.app.on_shutdown_hook = self._stop.set
        self._render_sidebar()
        self._run_job()

    def _workers_collapsed(self) -> bool:
        panel = getattr(self, "activity", None)
        return bool(panel is not None and panel.collapsed)

    def _events_collapsed(self) -> bool:
        panel = getattr(self, "events", None)
        return bool(panel is not None and panel.collapsed)

    def _apply_responsive(self) -> None:
        narrow = (self.size.width or 120) < NARROW_TERMINAL
        pane = self.query_one("#sidebar-pane")
        if pane.display == narrow:
            pane.display = not narrow
            self.narrow_bar.display = narrow

    # ── Multi-tab session management ──────────────────────────────────────────

    def _save_current_session(self) -> None:
        if not getattr(self, "is_multi_tab", False) or not (0 <= self.active_session_index < len(self.sessions)):
            return
        s = self.sessions[self.active_session_index]
        s.job = self.job
        s.engine = self._engine
        s.started = self._started
        s.workers = dict(self._workers)
        s.ledger = self._ledger
        s.events = list(self._events)
        s.last_log_body = self._last_log_body
        s.last_log_count = self._last_log_count
        s.error_tally = dict(self._error_tally)
        s.switches = list(self._switches)
        s.done_rows = self._done_rows
        s.stats = self._stats
        s.plan_total = self._plan_total
        s.finished = self._finished
        s.output_path = self._output_path
        s.file_total_count = self._file_total_count
        s.stop = self._stop

    def _load_session(self, index: int) -> None:
        if not (0 <= index < len(self.sessions)):
            return
        s = self.sessions[index]
        self.job = s.job
        self._engine = s.engine
        self._started = s.started
        self._workers = dict(s.workers)
        self._ledger = s.ledger
        self._events = list(s.events)
        self._last_log_body = s.last_log_body
        self._last_log_count = s.last_log_count
        self._error_tally = dict(s.error_tally)
        self._switches = list(s.switches)
        self._done_rows = s.done_rows
        self._stats = s.stats or ProcessStats(total=s.job.num_rows)
        self._plan_total = s.plan_total
        self._finished = s.finished
        self._output_path = s.output_path
        self._file_total_count = s.file_total_count
        self._stop = s.stop
        self.active_session_index = index

    def action_switch_tab(self, index: int) -> None:
        if not getattr(self, "is_multi_tab", False) or not (0 <= index < len(self.sessions)):
            return
        if index == self.active_session_index:
            return
        self._save_current_session()
        self._load_session(index)
        if getattr(self, "tab_bar", None) is not None:
            self.tab_bar.switch_tab(index)
        s = self.sessions[index]
        if hasattr(self, "feed") and self.feed is not None:
            self.feed.clear()
            for rn, src, tr in s.feed_lines:
                self.feed.add(rn, src, tr)
        if hasattr(self, "_app") and getattr(self, "sidebar", None) is not None:
            try:
                self._render_sidebar()
            except Exception:
                pass
        try:
            self.refresh()
        except Exception:
            pass

    def action_next_tab(self) -> None:
        if getattr(self, "is_multi_tab", False) and self.sessions:
            self.action_switch_tab((self.active_session_index + 1) % len(self.sessions))

    def action_prev_tab(self) -> None:
        if getattr(self, "is_multi_tab", False) and self.sessions:
            self.action_switch_tab((self.active_session_index - 1) % len(self.sessions))

    # ── Engine thread ─────────────────────────────────────────────────────────

    @work(thread=True)
    def _run_job(self) -> None:
        if getattr(self, "is_multi_tab", False):
            for i in range(len(self.sessions)):
                self._run_single_session(i)
            return

        app = self._app
        post = app.call_from_thread

        registry = app.registry
        # A combo job has no single provider; `build_pool` is the one place that
        # decides which pool a job runs on, so the screen does not have to.
        provider = None
        if not self.job.uses_combo:
            try:
                provider = registry.get_provider(self.job.provider_id)
            except Exception as exc:
                post(self._on_notice, "error", str(exc))
                post(self._job_finished, ProcessStats(total=self.job.num_rows))
                return
        try:
            pool = build_pool(
                self.job,
                registry,
                provider=provider,
                on_switch=lambda event: post(self._on_switch, event),
                on_notice=lambda kind, msg: post(self._on_notice, kind, msg),
            )
        except AllConnectionsUnavailable as exc:
            post(self._on_notice, "error", str(exc))
            post(self._job_finished, ProcessStats(total=self.job.num_rows))
            return
        engine = ProcessingEngine(
            self.job,
            provider,
            connection_pool=pool,
            on_status=lambda status: post(self._on_status, status),
            on_event=lambda event: post(self._on_event, event),
            on_notice=lambda kind, msg: post(self._on_notice, kind, msg),
            on_batch_start=lambda task, worker: post(self._on_batch_start, task),
            on_result=lambda task, result: post(self._on_result, task, result),
            stop_event=self._stop,
            registry=registry,
        )
        self._engine = engine

        try:
            if self.job.op_mode == "retranslate_failed":
                stats, _ = engine.run_retranslate_failed()
            elif self.job.op_mode.startswith("repair"):
                stats = engine.run_repair()
            else:
                stats = engine.run_translate()
        except Exception as exc:
            post(self._on_notice, "error", f"{type(exc).__name__}: {exc}")
            stats = ProcessStats(total=self.job.num_rows)

        try:
            self._output_path = engine.output_path
        except Exception:
            self._output_path = self.job.out_path

        post(self._job_finished, stats)

    @work(thread=True)
    def _run_single_session(self, session_idx: int) -> None:
        app = self._app
        post = app.call_from_thread
        session = self.sessions[session_idx]
        registry = app.registry

        provider = None
        if not session.job.uses_combo:
            try:
                provider = registry.get_provider(session.job.provider_id)
            except Exception as exc:
                if session_idx == self.active_session_index:
                    post(self._on_notice, "error", str(exc))
                return
        try:
            pool = build_pool(
                session.job,
                registry,
                provider=provider,
                on_switch=lambda event: post(self._session_switch, session_idx, event),
                on_notice=lambda kind, msg: post(self._session_notice, session_idx, kind, msg),
            )
        except AllConnectionsUnavailable as exc:
            if session_idx == self.active_session_index:
                post(self._on_notice, "error", str(exc))
            return

        engine = ProcessingEngine(
            session.job,
            provider,
            connection_pool=pool,
            on_status=lambda status: post(self._session_status, session_idx, status),
            on_event=lambda event: post(self._session_event, session_idx, event),
            on_notice=lambda kind, msg: post(self._session_notice, session_idx, kind, msg),
            on_batch_start=lambda task, worker: post(self._session_batch_start, session_idx, task),
            on_result=lambda task, result: post(self._session_result, session_idx, task, result),
            stop_event=session.stop,
            registry=registry,
        )
        session.engine = engine
        if session_idx == self.active_session_index:
            self._engine = engine

        try:
            if session.job.op_mode == "retranslate_failed":
                stats, _ = engine.run_retranslate_failed()
            elif session.job.op_mode.startswith("repair"):
                stats = engine.run_repair()
            else:
                stats = engine.run_translate()
        except Exception as exc:
            stats = ProcessStats(total=session.job.num_rows)

        try:
            session.output_path = engine.output_path
        except Exception:
            session.output_path = session.job.out_path

        post(self._session_finished, session_idx, stats)

    def _session_status(self, session_idx: int, status: WorkerStatus) -> None:
        session = self.sessions[session_idx]
        session.workers[status.worker_id] = status
        session.ledger.note(status)
        if session_idx == self.active_session_index:
            self._on_status(status)

    def _session_batch_start(self, session_idx: int, task: BatchTask) -> None:
        if session_idx == self.active_session_index:
            self._on_batch_start(task)

    def _session_result(self, session_idx: int, task: BatchTask, result: BatchResult) -> None:
        session = self.sessions[session_idx]
        for row, text in result.results.items():
            source = dict(task.rows).get(row, "")
            session.feed_lines.append((row, source, text))
            if len(session.feed_lines) > 200:
                session.feed_lines = session.feed_lines[-200:]
        session.done_rows += task.size
        total = session.plan_total or session.job.num_rows or max(1, session.done_rows)
        pct = min(100, int(session.done_rows / total * 100))
        if getattr(self, "tab_bar", None) is not None:
            self.tab_bar.update_tab(session_idx, progress_text=f"{pct}%")
        if session_idx == self.active_session_index:
            self._on_result(task, result)

    def _session_event(self, session_idx: int, event: ScheduleEvent) -> None:
        session = self.sessions[session_idx]
        session.events.append(getattr(event, "message", "") or "")
        if session_idx == self.active_session_index:
            self._on_event(event)

    def _session_notice(self, session_idx: int, kind: str, msg: str) -> None:
        if session_idx == self.active_session_index:
            self._on_notice(kind, msg)

    def _session_switch(self, session_idx: int, event: SwitchEvent) -> None:
        if session_idx == self.active_session_index:
            self._on_switch(event)

    def _session_finished(self, session_idx: int, stats: ProcessStats) -> None:
        session = self.sessions[session_idx]
        session.finished = True
        session.stats = stats
        if getattr(self, "tab_bar", None) is not None:
            self.tab_bar.update_tab(session_idx, progress_text="100%", status="done")
        if session_idx == self.active_session_index:
            self._job_finished(stats)

    # ── Callbacks (main thread) ───────────────────────────────────────────────

    def _on_status(self, status: WorkerStatus) -> None:
        self._workers[status.worker_id] = status
        # Observe every transition, even the ones a throttled repaint would miss:
        # the ledger is what keeps a `done`/`failed`/`retry` frame visible.
        self._ledger.note(status)
        self._render_sidebar()

    def _on_batch_start(self, task: BatchTask) -> None:
        """Rows appear in the feed the moment their batch goes out."""
        self.feed.add_pending(task.batch_num, task.rows)
        self._scroll_feed()
        self._render_sidebar()

    def _on_result(self, task: BatchTask, result: BatchResult) -> None:
        self.feed.add_pending(task.batch_num, task.rows)
        failed = set(result.failed_rows)
        for row_num, _source in task.rows:
            text = result.results.get(row_num, "")
            if text:
                self.feed.resolve(row_num, text, "ok")
                self._stats.ok += 1
            elif row_num in failed:
                self.feed.resolve(row_num, config.FAIL_MARKER, "failed")
                self._stats.failed += 1

        self._done_rows += len(task.rows)

        if self._engine is not None:
            self._stats.tokens_consumed = self._engine._pool.total_tokens_consumed()
        self._scroll_feed()
        self._render_sidebar()

    def _on_event(self, event: ScheduleEvent) -> None:
        if event.kind == "retry":
            self._log(event.message, WARN)
            for row_num in event.rows:
                self.feed.resolve(row_num, "retrying…", "retry")
        elif event.kind == "batch_failed":
            self._log(f"batch {event.batch_num}: {event.message}", ERROR)
            self._remember_error(event.message)
            # Attach the provider's real text to the rows it actually broke.
            for row_num in event.rows:
                self.feed.resolve(
                    row_num, config.FAIL_MARKER, "failed", error=event.message
                )
        elif event.kind == "aborted":
            self._log(event.message, ERROR)
            self._remember_error(event.message)

    def _remember_error(self, message: str) -> None:
        """Keep a tally of distinct failure reasons for the summary screen."""
        key = (message or "").strip()[:160]
        if key:
            self._error_tally[key] = self._error_tally.get(key, 0) + 1

    def _on_switch(self, event: SwitchEvent) -> None:
        detail = f" · {event.detail}" if event.detail else ""
        self._log(f"{event.from_name} → {event.to_name} ({event.reason}){detail}", ACCENT)

    def _on_notice(self, kind: str, message: str) -> None:
        if kind == "plan":
            # The engine has classified its rows and says how many it queued.
            # Taken as the denominator so the numerator and denominator count the
            # same population — `_done_rows` only ever counts rows that went into
            # a batch (BUG-001 / AC-601).
            planned = self._engine.planned_rows if self._engine is not None else None
            if planned is not None:
                self._plan_total = planned
            self._log(message, MUTED)
            return
        style = {"error": ERROR, "warn": WARN, "desync": WARN,
                 "locked": WARN, "recovered": OK,
                 "sweep": ACCENT, "resume": MUTED}.get(kind, MUTED)
        self._log(message, style)

    def _log(self, message: str, style: str = "") -> None:
        # Provider errors routinely contain [brackets] and JSON — escape them
        # or Rich would try to parse them as markup and blow up the timer.
        stamp = time.strftime("%H:%M:%S")
        body = escape_markup(str(message).replace("\n", " "))
        # Collapse an identical message repeated back-to-back into one line with
        # a count instead of a wall of the same error (bug #8). The raw text is
        # unchanged in the log file; this is only the on-screen events rail.
        if body == self._last_log_body:
            self._last_log_count += 1
            self._events[-1] = (
                f"[{style or MUTED}]{stamp} {body}  ×{self._last_log_count}[/]"
            )
        else:
            self._last_log_body = body
            self._last_log_count = 1
            self._events.append(f"[{style or MUTED}]{stamp} {body}[/]")
            del self._events[:-40]
        self._render_sidebar()

    def _scroll_feed(self) -> None:
        """Follow the newest line; when detached, leave the view alone."""
        if self.feed.follow:
            self.query_one("#feed-pane", VerticalScroll).scroll_end(animate=False)

    # ── Sidebar ───────────────────────────────────────────────────────────────

    def _render_sidebar(self) -> None:
        """Immediate repaint after a state change (the timer handles frames)."""
        try:
            self.sidebar.update(self._sidebar_text(self._tick))
        except Exception:
            pass
        self._apply_responsive()

    def _total_rows(self) -> int:
        """
        The denominator: the queue the engine announced, else the range asked for.

        The fallback only covers the moments before the first `plan` notice —
        after that the two populations must agree, because `_done_rows` counts
        rows that entered a batch and nothing else. Rows skipped by resume, and
        rows with an empty source, are in neither figure (BUG-001 / AC-604).
        """
        if self._plan_total is not None:
            return max(0, self._plan_total)
        return max(0, self.job.num_rows)

    def _sidebar_text(self, tick: int) -> str:
        """
        The top block of the rail — and the frame everything else in it uses.

        Called from the sidebar's own timer, so it is also where the Activity
        Panel and the bottom block are refreshed: one interval, one pass over
        the worker snapshots, whatever happened in between (NFR-201).
        """
        self._tick = tick
        stats = self._stats

        total = max(1, self._total_rows())
        done = min(total, max(self._done_rows, stats.ok + stats.failed))
        file_total = getattr(self, "_file_total_count", 0)
        is_dedup = bool(file_total and file_total > total)

        pct = min(100, int(done / total * 100))
        elapsed = time.time() - self._started
        eta = ""
        if done > 0 and not self._finished:
            remaining = (elapsed / done) * max(0, total - done)
            eta = f" · ~{self._fmt_short(remaining)} left"

        bar_width = 20
        filled = int(bar_width * pct / 100)
        bar = "█" * filled + "░" * (bar_width - filled)

        lines = [
            f"[bold {ACCENT}]Progress[/]",
        ]
        if is_dedup:
            lines.append(f"{done:,} / {total:,} unique rows")
            lines.append(f"[{ACCENT}]{bar}[/]  {pct}%")
            lines.append(f"[{MUTED}]{file_total:,} in file · {file_total - total:,} duplicates skipped[/]")
        else:
            lines.append(f"{done:,} / {total:,} rows")
            lines.append(f"[{ACCENT}]{bar}[/]  {pct}%")
        lines.append(f"{self._fmt_short(elapsed)} elapsed{eta}")
        lines.append("")
        lines.append(f"[bold {ACCENT}]Rows[/]")
        lines.append(f"[{OK}]ok[/]        {stats.ok:>8,}")
        lines.append(f"[{ERROR}]failed[/]    {stats.failed:>8,}")
        lines.append(f"[{MUTED}]skipped[/]   {stats.skipped:>8,}")
        lines.append(f"[{MUTED}]repaired[/]  {stats.repaired:>8,}")
        lines.append("")
        lines.append(f"[bold {ACCENT}]Model[/]")
        lines.append(escape_markup(self.job.model_id[:32]))
        lines.append(
            f"[{MUTED}]{escape_markup(self.job.processing_mode)}"
            f" ×{self.job.n_workers} · tokens {stats.tokens_consumed:,}[/]"
        )

        self._paint_activity(tick)
        self._paint_foot(stats)

        if getattr(self, "narrow_bar", None) and self.narrow_bar.display:
            self.narrow_bar.update(
                f"{pct}% · {done:,}/{total:,} · ok {stats.ok:,} · "
                f"fail {stats.failed:,} · {len(self._workers)} workers{eta}"
            )
        return "\n".join(lines)

    def _paint_activity(self, tick: int) -> None:
        """Hand the panel the latest snapshots. Errors never reach the screen."""
        try:
            # From the ledger, not the raw map: a worker that just finished keeps
            # showing Success/Failed/Retrying for ~1s so the transition is seen.
            self.activity.set_statuses(self._ledger.statuses(), tick)
        except Exception:
            pass

    def _paint_foot(self, stats: ProcessStats) -> None:
        # The Model block lives on the fixed part of the rail (in
        # `_sidebar_text`), above the panels, so it is never scrolled away by a
        # long events list. This feeds the events panel only.
        try:
            self.events.set_events(
                self._events[-40:] or [f"[{MUTED}]—[/]"]
            )
        except Exception:
            pass

    @staticmethod
    def _fmt_short(seconds: float) -> str:
        seconds = int(max(0, seconds))
        if seconds < 60:
            return f"{seconds}s"
        if seconds < 3600:
            return f"{seconds // 60}m {seconds % 60:02d}s"
        return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"

    # ── Actions ───────────────────────────────────────────────────────────────

    def action_toggle_follow(self) -> None:
        if self.feed.follow:
            self._detach_follow()
        else:
            self._attach_follow()
            self._scroll_feed()

    def _feed_pane(self) -> FeedPane:
        return self.query_one("#feed-pane", FeedPane)

    def _detach_follow(self) -> None:
        """
        Any manual scroll stops the view from being yanked to the bottom.

        Detaching also freezes the feed's trim. The feed keeps only its last 400
        rows, and dropping one off the top slides everything below it up a line —
        which, on a parked viewport, walks the view forward through the output on
        its own. Holding the trim is what makes "scrolled back" actually stay put.
        """
        if self.feed.follow:
            self.feed.follow = False
            self.feed.hold_trim(True)
            self.notify_status("scroll freely — f to resume following", WARN)
            self.refresh_keys()

    def _attach_follow(self) -> None:
        """Coming back to the bottom resumes live output, like tail -f."""
        if not self.feed.follow:
            self.feed.follow = True
            # Give back the rows the hold was keeping: the view is about to jump
            # to the end, so nothing on screen depends on them any more.
            self.feed.hold_trim(False)
            self.notify_status("following live output", ACCENT)
            self.refresh_keys()

    def action_scroll_feed(self, lines: int) -> None:
        pane = self._feed_pane()
        if lines < 0:
            self._detach_follow()
        elif pane.scroll_target_y >= pane.max_scroll_y - 1:
            # Scrolling down to the very bottom means "follow again".
            self._attach_follow()
        pane.scroll_to(y=pane.scroll_target_y + lines, animate=False)

    def action_scroll_feed_page(self, direction: int) -> None:
        self.action_scroll_feed(direction * max(1, self._feed_pane().size.height - 2))

    def action_scroll_feed_home(self) -> None:
        self._detach_follow()
        self._feed_pane().scroll_home(animate=False)

    def action_scroll_feed_end(self) -> None:
        self._attach_follow()
        self._feed_pane().scroll_end(animate=False)

    def action_pause(self) -> None:
        """
        `c` — stop the run.

        First press is graceful: the stop flag is set and the batch in flight
        finishes — its rows are written and kept, nothing new is sent. Second
        press (while the first is still pending) is a force stop: the sockets
        under every in-flight request are closed, so the run ends now.
        """
        now = time.monotonic()
        pending = not self._finished and self._stop.is_set()
        if pending and (now - self._cancel_requested_at) <= 5.0:
            from ...providers.client import abort_all_for
            self._stop.set()
            try:
                abort_all_for(self._stop)
            except Exception:
                pass
            if hasattr(self, "_ledger"):
                self._ledger.reset_all_idle()
            self.notify_status(
                "Force-stopping — aborting immediately…", ERROR,
            )
            def _force_finish_if_needed() -> None:
                if not self._finished:
                    self._job_finished(self._stats)
            self.set_timer(1.0, _force_finish_if_needed)
            return

        self._cancel_requested_at = now
        self._stop.set()
        self.notify_status(
            "Stopping after the current batch — press c again to abort "
            "immediately.", WARN,
        )

    def action_toggle_workers(self) -> None:
        """
        Show or hide the worker panel (FR-201).

        Hiding it does not stop anything: the panel is a view of state the
        scheduler reports either way, so closing it on a narrow terminal only
        buys feed height back (AC-204).
        """
        open_now = self.activity.toggle()
        self.refresh_keys()
        self.notify_status(
            "worker panel shown" if open_now else "worker panel hidden — a to show",
            ACCENT if open_now else MUTED,
        )

    def action_toggle_activity(self) -> None:
        self.action_toggle_workers()

    def action_toggle_events(self) -> None:
        """Show or hide the events rail — `e`, or click its header."""
        open_now = self.events.toggle()
        self.refresh_keys()
        self.notify_status(
            "events panel shown" if open_now else "events panel hidden — e to show",
            ACCENT if open_now else MUTED,
        )

    def on_events_panel_header_clicked(
        self, _event: EventsPanel.HeaderClicked
    ) -> None:
        """Clicking the events header is the same gesture as pressing `e`."""
        self.refresh_keys()

    def action_worker_detail(self) -> None:
        """Expand one worker's batch / connection / row count, then the next."""
        worker_id = self.activity.cycle_detail()
        if self.activity.collapsed and worker_id:
            self.activity.collapsed = False
            self.refresh_keys()
        self.notify_status(
            f"worker {worker_id} detail" if worker_id else "detail closed", MUTED
        )

    def on_activity_panel_header_clicked(
        self, _event: ActivityPanel.HeaderClicked
    ) -> None:
        """Clicking the header is the same gesture as pressing `a`."""
        self.refresh_keys()

    def action_noop(self) -> None:
        """Back/Esc are disabled mid-run — use c to stop & save."""

    def _job_finished(self, stats: ProcessStats) -> None:
        self._finished = True
        self._stats = stats
        self.app.on_shutdown_hook = None
        self._render_sidebar()
        self.refresh_keys()
        self.app.push_screen(
            SummaryScreen(
                self.job, stats, self._events, self._error_tally,
                output_path=self._output_path,
            )
        )


class SummaryScreen(MenuScreen):
    breadcrumb = "Done"
    title_text = "Job finished"

    def __init__(
        self,
        job: JobConfig,
        stats: ProcessStats,
        events: list[str],
        errors: dict[str, int] | None = None,
        output_path: str | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.job = job
        self.stats = stats
        self.events = events
        self.errors = errors or {}
        # The file results landed in — a numbered sibling if the target was
        # locked, otherwise job.out_path.
        self.output_path = output_path or job.out_path

    def compose_extra(self) -> ComposeResult:
        yield Label("")
        yield Static(self._summary(), classes="panel")

    def _summary(self) -> str:
        stats = self.stats
        import os

        sweeps = [e for e in self.events if "Auto-sweep" in e]
        saved_target = self.job.full_out_path or self.output_path
        lines = [
            f"Finished in {RunScreen._fmt_short(stats.elapsed)}",
            "",
            f"[{OK}]ok[/]         {stats.ok:>8,}",
            f"[{MUTED}]repaired[/]   {stats.repaired:>8,}",
            f"[{MUTED}]skipped[/]    {stats.skipped:>8,}   already filled",
            f"[{ERROR}]failed[/]     {stats.failed:>8,}   written as {config.FAIL_MARKER}",
            f"[{MUTED}]───────────────────[/]",
            f"total      {stats.total:>8,}",
            "",
            f"tokens     {stats.tokens_consumed:>8,}",
            "",
            f"[{OK}]saved to[/]   {escape_markup(saved_target)}",
            f"[{MUTED}]column {ExcelReader.col_letter(self.job.dst_col)} · "
            f"source file untouched[/]",
        ]
        if self.job.full_out_path and self.job.map_path:
            lines.insert(
                -1,
                f"[{MUTED}]synced from[/] {escape_markup(os.path.basename(self.output_path))}",
            )
        if os.path.abspath(self.output_path) != os.path.abspath(self.job.out_path):
            lines.insert(
                -1,
                f"[{WARN}]note[/]       {escape_markup(os.path.basename(self.job.out_path))} "
                f"was open elsewhere — results went to a new file[/]",
            )
        if sweeps:
            lines += ["", f"[{ACCENT}]Auto-sweep ran and reported:[/]"]
            lines += [f"  {line}" for line in sweeps[-3:]]

        if self.errors:
            # The run screen scrolls away — the reasons for failure must not
            # disappear with it (03_PROVIDER_SYSTEM.md §7).
            lines += ["", f"[{ERROR}]Why rows failed[/]"]
            ranked = sorted(self.errors.items(), key=lambda kv: -kv[1])
            for message, count in ranked[:4]:
                lines.append(f"  [{MUTED}]×{count}[/]  {escape_markup(message)[:88]}")
            if len(ranked) > 4:
                lines.append(f"  [{MUTED}]…and {len(ranked) - 4} other error(s)[/]")
            if self.job.debug_log and self.job.log_path:
                lines.append(f"  [{MUTED}]full detail: {escape_markup(self.job.log_path)}[/]")
        return "\n".join(lines)

    def build_options(self) -> list[Option]:
        options = []
        if self.stats.failed:
            options.append(
                Option(f"Retranslate the {self.stats.failed} failed rows now",
                       "retry", style=WARN)
            )
        options += [
            # The second Judge entry point (FR-101): the file and every column
            # are already known here, so a review is one keystroke away instead
            # of a form to fill in again.
            Option("Evaluate this translation (Judge)", "judge",
                   hint="review the rows that were just written"),
            Option("Translate another file", "another",
                   hint="start a fresh translate job"),
            Option("Back to main menu", "home"),
            Option("Quit", "quit"),
        ]
        return options

    def screen_keys(self) -> list[tuple[str, str]]:
        return [("↑↓", "move"), ("⏎", "select")]

    def action_back(self) -> None:
        pass

    def action_cancel(self) -> None:
        self.app.go_home()

    def handle_choice(self, option: Option) -> None:
        if option.value == "quit":
            self.app.exit()
        elif option.value == "home":
            self.app.go_home()
        elif option.value == "another":
            from .wizard import ModelSelectScreen

            self.app.draft = {"mode": "full"}
            self.app.go_home()
            self.app.push_screen(ModelSelectScreen())
        elif option.value == "judge":
            from .judge import JudgeSetupScreen

            # The finished job carries the file, the columns and the languages,
            # so the setup screen only has to ask for criteria and a model.
            self.app.push_screen(JudgeSetupScreen(job=self.job))
        else:
            import copy

            job = copy.copy(self.job)
            job.op_mode = "retranslate_failed"
            self.app.pop_screen()
            self.app.push_screen(RunScreen(job))
