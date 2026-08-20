"""
tui/widgets/activity.py — The Activity Panel (FEATURE-002).

One compact line per worker, so every worker is visible at once without
scrolling: who is running, on which connection, which batch, how many rows, and
how long the current phase has been going. Below a line, on demand, the same
facts spelled out.

Three things it deliberately is not:

  * **not a token stream.** Nothing about a model's output arrives here. A
    batch is either complete or it is not (that is why the translation path
    does not stream at all), so the panel shows *state*, not text.
  * **not a reasoning viewer.** `think` is a phase and nothing more. The
    reasoning content a provider returns never reaches this module, and the
    phase itself is only shown when reasoning is actually active — a
    non-reasoning model never displays it (FR-204/FR-205, AC-202/AC-203).
  * **not its own animation loop.** The panel is painted from `WorkerStatus`
    snapshots by whoever owns the sidebar's repaint timer (≈0.12 s). Several
    status changes inside one interval collapse into the last one, which is
    what keeps a fast parse from costing a repaint each (NFR-201).

Durations are recomputed from `started_at` on every paint rather than read from
the snapshot, so the clock keeps ticking between status updates instead of
freezing at whatever the last callback happened to say.

Everything is painted as `rich.text.Text`, never markup: a connection name is
user data and a stray `[bracket]` in one must not be able to kill a repaint.
"""

from __future__ import annotations

import copy
import time

from rich.style import Style as RichStyle
from rich.text import Text
from textual import events
from textual.message import Message
from textual.reactive import reactive
from textual.strip import Strip
from textual.widget import Widget

from ..theme import ACCENT, ERROR, HOVER_BG, MUTED, OK, WARN
from .common import pad_cells, spinner_frame, strip_line, truncate

# Phase → (label, colour). `think` shares the accent with `generating` because
# it is the same work seen one step earlier, not a different kind of progress.
_PHASES: dict[str, tuple[str, str]] = {
    "idle":       ("idle",     MUTED),
    "waiting":    ("waiting",  WARN),
    "think":      ("think",    ACCENT),
    "generating": ("sending",  ACCENT),
    "parsing":    ("parsing",  ACCENT),
    "retry":      ("retry",    WARN),
    "done":       ("success",  OK),
    "failed":     ("failed",   ERROR),
}

# Phases drawn with a spinner rather than a fixed glyph — the ones that are
# still going. `retry` gets a warning sign instead: it is waiting, not working.
_SPINNING = ("think", "generating", "parsing")

_STATIC_ICONS = {
    "retry": "!",
    "done": "✓",
    "failed": "✗",
    "waiting": "·",
    "idle": "·",
}


# Phases that mark the end of a request (or a retry between requests). They are
# what a viewer needs to *see* — success, a failure, the fact that a retry is
# happening — but the engine leaves them behind almost instantly: a parallel
# worker takes its next batch the moment one lands, so `done` is overwritten by
# the next `generating` within the same repaint interval and the panel, which
# only ever draws the latest snapshot, skips the frame entirely. That is the
# "timer jumps 11s → 0s with nothing in between" and the "Success flickers"
# report both at once.
_HOLD_PHASES = ("done", "failed", "retry")

# Phases that mean "a new request is under way", i.e. the ones a hold suppresses
# until it expires so the completed state stays readable first.
_ACTIVITY_PHASES = ("generating", "think", "parsing", "idle", "waiting")


class WorkerLedger:
    """
    Keeps a worker's transient end-of-request states visible long enough to read.

    A UI-only layer between the scheduler's per-transition `on_status` callbacks
    and the Activity Panel. It changes nothing about timing: the engine is never
    held, no sleep is added, the next batch starts exactly when it always did.
    What it does is remember that a worker just finished (or failed, or is
    retrying) and keep showing that for ~1s even after the worker has moved on,
    so `Sending → Success → Sending` and `Sending → Retrying → Sending` are
    actually seen rather than coalesced away between two repaints.

    Every callback goes through `note()` (so the terminal frame is observed even
    when painting is throttled); the panel is fed from `statuses()`.
    """

    HOLD_SECONDS = 1.0

    def __init__(self) -> None:
        # worker_id -> latest real status from the scheduler.
        self._real: dict[int, object] = {}
        # worker_id -> (held snapshot, monotonic deadline).
        self._hold: dict[int, tuple[object, float]] = {}

    def note(self, status) -> None:
        wid = getattr(status, "worker_id", 0)
        phase = getattr(status, "phase", "") or ""
        now = time.monotonic()
        if phase in _HOLD_PHASES:
            snapshot = self._freeze(status, wid)
            self._hold[wid] = (snapshot, now + self.HOLD_SECONDS)
        self._real[wid] = status

    def _freeze(self, status, wid: int):
        """
        A copy of the terminal status with a sensible frozen duration.

        A `done`/`failed` phase carries no live clock, so on its own it would
        read `0.0s`. The elapsed time of the request that just ended is far more
        useful, so it is carried over from the previous running snapshot.
        """
        held = copy.copy(status)
        prev = self._real.get(wid)
        if getattr(status, "phase", "") in ("done", "failed"):
            started = getattr(prev, "started_at", 0.0) if prev else 0.0
            if started:
                held.elapsed = max(0.0, time.monotonic() - started)
        return held

    def statuses(self) -> list:
        """The statuses to draw now, with any live hold standing in for the real one."""
        now = time.monotonic()
        out = []
        for wid, real in self._real.items():
            hold = self._hold.get(wid)
            if hold is not None and now < hold[1] \
                    and (getattr(real, "phase", "") or "") in _ACTIVITY_PHASES:
                out.append(hold[0])
            else:
                if hold is not None and now >= hold[1]:
                    del self._hold[wid]
                out.append(real)
        return out

    @property
    def count(self) -> int:
        return len(self._real)

    def clear(self) -> None:
        self._real.clear()
        self._hold.clear()



def phase_label(status) -> str:
    """
    The phase to *show* for a worker, which is not always the phase it is in.

    `think` is only real when reasoning is active. A non-reasoning model that
    somehow reported it is displayed as `generating`, because a think phase on
    a model that cannot think would be a lie about where the time went
    (FR-204 / AC-202).
    """
    phase = getattr(status, "phase", "idle") or "idle"
    if phase == "think" and not getattr(status, "reasoning_active", False):
        return "generating"
    return phase if phase in _PHASES else "idle"


def phase_style(status) -> str:
    return _PHASES.get(phase_label(status), _PHASES["idle"])[1]


def phase_text(status) -> str:
    return _PHASES.get(phase_label(status), _PHASES["idle"])[0]


def phase_icon(status, tick: int) -> str:
    phase = phase_label(status)
    if phase in _SPINNING:
        return spinner_frame(tick)
    return _STATIC_ICONS.get(phase, "·")


def running_seconds(status) -> float:
    """
    How long the current phase has been going.

    Live phases are measured from `started_at` so the number keeps moving
    between callbacks; a finished phase keeps the elapsed time it ended with.
    """
    import time

    phase = getattr(status, "phase", "idle")
    started = getattr(status, "started_at", 0.0) or 0.0
    if started and phase in ("think", "generating", "parsing", "retry", "waiting"):
        return max(0.0, time.time() - started)
    return float(getattr(status, "elapsed", 0.0) or 0.0)


def worker_line(status, tick: int = 0, connection_width: int = 11) -> Text:
    """
    One worker as a single line:

        3  Main Key    b12  ⠹ think   4.2s

    Compact on purpose — with eight workers this has to fit a 36-cell sidebar
    without wrapping, and `pad_cells` keeps a CJK connection name from shifting
    the columns.
    """
    style = phase_style(status)
    icon = phase_icon(status, tick)
    connection = pad_cells(str(getattr(status, "connection", "") or "—"),
                           connection_width)
    batch = getattr(status, "batch_num", 0) or 0
    seconds = running_seconds(status)

    line = Text()
    line.append(f"{getattr(status, 'worker_id', 0)} ", style=MUTED)
    line.append(connection + " ")
    line.append(f"b{batch:<4}", style=MUTED)
    line.append(f"{icon} ", style=style)
    line.append(pad_cells(phase_text(status), 8), style=style)
    line.append(f"{seconds:>5.1f}s", style=MUTED)
    return line


def detail_lines(status, width: int = 34) -> list[Text]:
    """
    The expanded facts for one worker (FR-206): batch, connection, row count.

    A message is included when there is one — a retry reason, or the row tally
    a finished batch reported. Provider text arrives here verbatim and is
    truncated rather than parsed.
    """
    batch = getattr(status, "batch_num", 0) or 0
    total = getattr(status, "total_batches", 0) or 0
    rows = getattr(status, "rows", 0) or 0
    connection = str(getattr(status, "connection", "") or "—")
    wave = getattr(status, "wave_index", 0) or 0

    facts = [
        ("batch", f"{batch}/{total}" if total else str(batch)),
        ("rows", str(rows) if rows else "—"),
        ("connection", connection),
    ]
    if wave:
        facts.append(("wave", str(wave)))
    message = str(getattr(status, "message", "") or "")
    if message:
        facts.append(("last", message))

    lines: list[Text] = []
    for name, value in facts:
        line = Text("    ")
        line.append(f"{name:<11}", style=MUTED)
        line.append(truncate(value, max(8, width - 15)))
        lines.append(line)
    return lines


class ActivityPanel(Widget):
    """
    The panel itself: a toggleable header, one line per worker, and an
    optionally expanded detail block under one of them.

    It holds no timer and starts no work. A screen hands it snapshots on the
    interval it already repaints its sidebar on, which is what makes the
    coalescing free: whatever the workers did between two paints, only the
    latest state is drawn.

    Open by default (FR-201), because the panel is the answer to "why is this
    slow" and someone who has to go and find it will not ask until later.
    """

    ALLOW_SELECT = False

    collapsed = reactive(False)
    detail_worker = reactive(0)      # worker_id whose detail is expanded, 0 = none
    hover_row = reactive(-1)

    class HeaderClicked(Message):
        """The header was clicked — the screen decides what that means."""

    def __init__(self, title: str = "Activity", **kwargs) -> None:
        super().__init__(**kwargs)
        self._title = title
        self._statuses: list = []
        self._tick = 0
        # (kind, worker_id) per rendered line, so a click knows what it hit.
        self._rows: list[tuple[str, int]] = []

    # ── Data ──────────────────────────────────────────────────────────────────

    def set_statuses(self, statuses, tick: int = 0) -> None:
        """
        Replace the panel's view of the world. Cheap and idempotent: called on
        every sidebar repaint, so it must not allocate anything expensive.
        """
        self._statuses = sorted(
            list(statuses or []), key=lambda s: getattr(s, "worker_id", 0)
        )
        self._tick = tick
        self.refresh(layout=True)

    @property
    def worker_ids(self) -> list[int]:
        return [getattr(status, "worker_id", 0) for status in self._statuses]

    def toggle(self) -> bool:
        self.collapsed = not self.collapsed
        return not self.collapsed

    def cycle_detail(self) -> int:
        """
        Move the expanded detail to the next worker, then off again.

        A cycle rather than a cursor: the panel is never focused (the feed keeps
        the arrow keys), so one key has to be able to reach every worker and
        then get out of the way.
        """
        ids = self.worker_ids
        if not ids:
            self.detail_worker = 0
            return 0
        if self.detail_worker not in ids:
            self.detail_worker = ids[0]
            return self.detail_worker
        index = ids.index(self.detail_worker) + 1
        self.detail_worker = ids[index] if index < len(ids) else 0
        return self.detail_worker

    # ── Reactives ─────────────────────────────────────────────────────────────

    def watch_collapsed(self, _old: bool, _new: bool) -> None:
        self.refresh(layout=True)

    def watch_detail_worker(self, _old: int, _new: int) -> None:
        self.refresh(layout=True)

    def watch_hover_row(self, _old: int, _new: int) -> None:
        self.refresh()

    # ── Mouse ─────────────────────────────────────────────────────────────────

    def _row_at(self, event: events.MouseEvent) -> int | None:
        offset = event.get_content_offset(self)
        if offset is None:
            return None
        row = offset.y + self.scroll_offset.y
        return row if 0 <= row < len(self._rows) else None

    def on_mouse_move(self, event: events.MouseMove) -> None:
        row = self._row_at(event)
        self.hover_row = row if row is not None else -1

    def on_leave(self, _event: events.Leave) -> None:
        self.hover_row = -1

    def on_click(self, event: events.Click) -> None:
        row = self._row_at(event)
        if row is None:
            return
        event.stop()
        kind, worker_id = self._rows[row]
        if kind == "header":
            self.toggle()
            self.post_message(self.HeaderClicked())
        elif kind == "worker":
            # Clicking the open one closes it again, which is the only gesture
            # anyone tries after opening it.
            self.detail_worker = 0 if self.detail_worker == worker_id else worker_id

    # ── Render ────────────────────────────────────────────────────────────────

    def _build(self) -> list[Text]:
        width = max(24, self.size.width or 34)
        connection_width = max(6, min(14, width - 22))

        header = Text()
        header.append("▾ " if not self.collapsed else "▸ ", style=MUTED)
        header.append(self._title, style=f"bold {ACCENT}")
        header.append(f"  {self._summary()}", style=MUTED)
        lines = [header]
        self._rows = [("header", 0)]

        if self.collapsed:
            return lines

        if not self._statuses:
            lines.append(Text("  starting…", style=MUTED))
            self._rows.append(("empty", 0))
            return lines

        for status in self._statuses:
            worker_id = getattr(status, "worker_id", 0)
            lines.append(worker_line(status, self._tick, connection_width))
            self._rows.append(("worker", worker_id))
            if worker_id and worker_id == self.detail_worker:
                for line in detail_lines(status, width):
                    lines.append(line)
                    self._rows.append(("detail", worker_id))
        return lines

    def _summary(self) -> str:
        """Enough state to be useful while the panel is closed."""
        if not self._statuses:
            return ""
        active = sum(
            1 for status in self._statuses
            if phase_label(status) in ("think", "generating", "parsing")
        )
        retrying = sum(
            1 for status in self._statuses if phase_label(status) == "retry"
        )
        parts = [f"{len(self._statuses)} worker" + ("s" if len(self._statuses) != 1 else "")]
        if active:
            parts.append(f"{active} busy")
        if retrying:
            parts.append(f"{retrying} retrying")
        return " · ".join(parts)

    def get_content_height(self, container, viewport, width: int) -> int:
        return max(1, len(self._build()))

    def render_line(self, y: int) -> Strip:
        # Rebuilt per paint rather than cached: the durations move every frame,
        # so a cache would only ever be one frame stale and cost a copy.
        try:
            lines = self._build()
        except Exception:
            # A paint error must never take the run screen down (NFR-202).
            return Strip.blank(self.size.width)
        row = y + self.scroll_offset.y
        text = lines[row] if 0 <= row < len(lines) else Text("")
        return strip_line(text, self.size.width, self._row_style(row))

    def _row_style(self, row: int) -> RichStyle:
        if row == self.hover_row and 0 <= row < len(self._rows):
            return self.rich_style + RichStyle(bgcolor=HOVER_BG)
        return self.rich_style


class EventsPanel(Widget):
    """
    The Events rail — a bounded, scrollable list of event lines.

    Sits below the Activity Panel and keeps its own scroll area: a long run
    must not push the events off the panel or grow the right rail without
    bound. Lines are passed in by whichever screen owns the sidebar timer
    (`set_events`), exactly as the Activity Panel is fed worker snapshots, so
    one timer drives both panels (NFR-201).

    Painted as `rich.text.Text`, never markup: events carry provider text and
    JSON, and a stray `[bracket]` must not be able to kill a repaint.
    """

    ALLOW_SELECT = False

    collapsed: reactive = reactive(False)

    def __init__(self, title: str = "Events", **kwargs) -> None:
        super().__init__(**kwargs)
        self._title = title
        self._lines: list[str] = []

    # ── Data ──────────────────────────────────────────────────────────────────

    def set_events(self, lines: list[str]) -> None:
        """Replace the panel's lines. Called from the screen's own timer."""
        if lines != self._lines:
            self._lines = list(lines)
            self.refresh(layout=True)

    def toggle(self) -> bool:
        self.collapsed = not self.collapsed
        return not self.collapsed

    def watch_collapsed(self, _old: bool, _new: bool) -> None:
        self.refresh(layout=True)

    # ── Render ────────────────────────────────────────────────────────────────

    def _build(self) -> list[Text]:
        header = Text()
        header.append("▾ " if not self.collapsed else "▸ ", style=MUTED)
        header.append(self._title, style=f"bold {ACCENT}")
        if self._lines:
            header.append(f"  {len(self._lines)}", style=MUTED)
        lines = [header]
        if self.collapsed:
            return lines
        if not self._lines:
            lines.append(Text("  —", style=MUTED))
        for line in self._lines[-40:]:
            lines.append(truncate_line(line))
        return lines

    def get_content_height(self, container, viewport, width: int) -> int:
        return max(1, len(self._build()))

    def render_line(self, y: int) -> Strip:
        try:
            lines = self._build()
        except Exception:
            return Strip.blank(self.size.width)
        row = y + self.scroll_offset.y
        text = lines[row] if 0 <= row < len(lines) else Text("")
        return strip_line(text, self.size.width, self.rich_style)


def truncate_line(line: str) -> Text:
    """One event line, capped so a long provider message never wraps the rail."""
    body = str(line or "")
    if len(body) > 64:
        body = body[:64] + "…"
    try:
        return Text.from_markup(body)
    except Exception:
        return Text(body.replace("[", "\\["))


class SmallIndicator(Widget):
    """
    The one-line spinner a single-row operation gets instead of the panel
    (FR-208 / AC-206).

    Fix It and Judge Again are one request about one row. A worker panel for
    that would be a table with a single line in it, so they get a spinner next
    to the button and nothing else. Hidden until something is actually running.
    """

    ALLOW_SELECT = False

    tick = reactive(0)

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._label = ""
        # Not `_running`: `MessagePump` owns that attribute and sets it True on
        # mount, so a widget storing its own state there would spin from birth.
        self._spinning = False
        self._timer = None
        self.display = False

    def on_mount(self) -> None:
        self._timer = self.set_interval(0.1, self._advance)

    def _advance(self) -> None:
        if self._spinning:
            self.tick += 1

    def watch_tick(self, _old: int, _new: int) -> None:
        self.refresh()

    def start(self, label: str) -> None:
        self._label = label
        self._spinning = True
        self.display = True
        self.refresh()

    def stop(self, label: str = "") -> None:
        self._spinning = False
        self._label = label
        self.display = bool(label)
        self.refresh()

    def get_content_height(self, container, viewport, width: int) -> int:
        return 1

    def render(self) -> Text:
        text = Text()
        if self._spinning:
            text.append(spinner_frame(self.tick) + " ", style=ACCENT)
        text.append(self._label, style=MUTED)
        return text
