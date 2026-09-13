"""
tui/widgets/compare.py — The side-by-side comparison grid.

One row per input line, one column per model, so the differences line up
horizontally and the eye can run down a column. Everything is painted as
`rich.text.Text` rather than markup, because every cell here is model output
and a stray `[bracket]` in a translation must not be able to take the app
down (the same reason TranslationFeed does it that way).

Cells are flattened to one line before being padded (`pad_cells`). A grid is
only readable while its columns align, and a spreadsheet cell holding a newline
would otherwise spill its tail onto the next painted row — on top of the gutter
where that row's number goes.

Rows can be filtered without touching the data: `set_row_filter` narrows what
is painted while `set_sources` keeps the full set, so a filter can be changed
or cleared without re-running anything.

Rows can also arrive one batch at a time (`add_rows`), which is what makes a
long evaluation readable while it runs instead of after it: the grid grows from
the top down as replies land, in sheet order, rather than staying blank until
every request is home.

One row can be marked as the cursor (`cursor_row`). It is painted as a
full-width band, the same way the pointer's hover band is, because a screen that
offers "fix the row you are looking at" has to show which row that is.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field

from rich.style import Style as RichStyle
from rich.text import Text
from textual import events
from textual.message import Message
from textual.reactive import reactive
from textual.strip import Strip
from textual.widget import Widget

from ..theme import ACCENT, ERROR, HOVER_BG, MUTED, OK, SELECT_BG, WARN
from .common import flatten_cell, pad_cells, spinner_frame, strip_line

# What a column shows before its model has answered.
PENDING = "pending"


@dataclass
class Column:
    """One model's column. `letter` is what the judge knew it as."""
    letter: str
    label: str
    where: str = ""
    state: str = PENDING
    rows: dict[int, str] = field(default_factory=dict)
    error: str = ""
    stat: str = ""            # the one-line timing/token summary under the name
    score: str = ""           # filled in once a judge has graded it
    winner: bool = False
    # What an absent cell reads as. `(missing)` is right for a column every row
    # should have answered; a column that only applies to *some* rows — the
    # pre-fix text, say — wants a blank, because "(missing)" there would claim a
    # row failed when it simply was never repaired.
    placeholder: str = "(missing)"


@dataclass
class _Line:
    kind: str                 # head | stat | row | note | gap
    number: int = 0
    cells: list[str] = field(default_factory=list)
    text: str = ""
    style: str = ""           # note lines only: the severity's colour


class CompareGrid(Widget):
    """
    Set the sources once, then update columns as their models land.

    Column widths come from `fit_width`, the same helper the wizard's sheet
    preview uses, so a narrow terminal degrades the same way in both places.
    """

    ALLOW_SELECT = False

    GUTTER = 5                # the line-number column

    hover_row = reactive(-1)
    # The row a keyboard action would act on, painted as a band. -1 = none.
    cursor_row = reactive(-1)

    class RowClicked(Message):
        def __init__(self, row_num: int, source: str, translations: dict[str, str]) -> None:
            super().__init__()
            self.row_num = row_num
            self.source = source
            self.translations = translations

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._sources: list[tuple[int, str]] = []
        self._columns: list[Column] = []
        self._notes: dict[int, str] = {}
        self._note_styles: dict[int, str] = {}
        # row -> style for the whole data line (source + cells). This is how a
        # Judge grid colours a row by severity or "fixed" without a note under
        # it: the colour is on the text itself, so the report is scannable at a
        # glance. Empty = the default foreground.
        self._row_styles: dict[int, str] = {}
        self._tick = 0
        self._lines: list[_Line] = []
        # None = show everything. A set narrows what is painted without
        # discarding anything, so a filter is reversible and costs no re-run.
        self._visible: set[int] | None = None
        # >0 while a `batched()` block is open: rebuilds are deferred to its end.
        self._held = 0

    def watch_hover_row(self, _old: int, _new: int) -> None:
        self.refresh()

    def watch_cursor_row(self, _old: int, _new: int) -> None:
        self.refresh()

    # ── Mouse ─────────────────────────────────────────────────────────────────

    def _line_at(self, event: events.MouseEvent) -> int | None:
        offset = event.get_content_offset(self)
        if offset is None:
            return None
        y = offset.y + self.scroll_offset.y
        return y if 0 <= y < len(self._lines) else None

    def on_mouse_move(self, event: events.MouseMove) -> None:
        y = self._line_at(event)
        if y is not None:
            line = self._lines[y]
            if line.number > 0 and line.kind in ("row", "note"):
                self.hover_row = line.number
                return
        self.hover_row = -1

    def on_leave(self, _event: events.Leave) -> None:
        self.hover_row = -1

    def on_click(self, event: events.Click) -> None:
        y = self._line_at(event)
        if y is None:
            return
        line = self._lines[y]
        if line.number > 0 and line.kind in ("row", "note"):
            event.stop()
            if event.chain == 1:
                row_num = line.number
                row_line = next((l for l in self._lines if l.kind == "row" and l.number == row_num), None)
                if row_line:
                    source = row_line.cells[0]
                    translations = {}
                    for col_idx, col in enumerate(self._columns):
                        if col_idx + 1 < len(row_line.cells):
                            translations[col.label] = row_line.cells[col_idx + 1]
                    self.post_message(self.RowClicked(row_num, source, translations))

    # ── Data ──────────────────────────────────────────────────────────────────

    @contextmanager
    def batched(self):
        """
        Group several updates into one rebuild.

        A live evaluation sets the columns, adds the rows and replaces the notes
        on every flush. Each of those rebuilds the whole line list, so without
        this the grid is rebuilt three times to show one change — and the rebuild
        is O(rows), which on a few thousand rows is exactly where a repaint budget
        goes.
        """
        self._held += 1
        try:
            yield self
        finally:
            self._held -= 1
            if self._held <= 0:
                self._held = 0
                self._rebuild()

    def clear(self) -> None:
        """Clear all rows, notes, and styles from the grid."""
        self._sources.clear()
        self._notes.clear()
        self._note_styles.clear()
        self._row_styles.clear()
        self._visible = None
        self._rebuild()

    def set_sources(self, rows: list[tuple[int, str]], columns: list[Column]) -> None:
        self._sources = list(rows)
        self._columns = list(columns)
        self._notes = {}
        self._note_styles = {}
        self._visible = None
        self._rebuild()

    def set_columns(self, columns: list[Column]) -> None:
        """
        Swap the columns, keeping the rows, the notes and the filter.

        `set_sources` resets all three, which is wrong for a live run: a column
        whose cells have just been filled in must not also clear the notes that
        arrived with them, or drop the filter the user is reading through.
        """
        self._columns = list(columns)
        self._rebuild()

    def add_rows(self, rows: list[tuple[int, str]]) -> None:
        """
        Merge in rows that were not in the grid yet, keeping sheet order.

        This is what makes a live evaluation readable: sections come back in
        whatever order the workers finish, so rows are merged by number rather
        than appended blindly — otherwise the grid would read 41, 12, 27 and be
        useless for scanning. A row already present is left alone, since its
        source text cannot have changed.
        """
        if not rows:
            return
        known = {number for number, _source in self._sources}
        fresh = [
            (int(number), source) for number, source in rows
            if int(number) not in known
        ]
        if not fresh:
            return
        self._sources.extend(fresh)
        self._sources.sort(key=lambda pair: pair[0])
        self._rebuild()

    def set_row_filter(self, rows: "set[int] | list[int] | None") -> None:
        """
        Narrow the grid to these row numbers, or `None` for all of them.

        The sources are untouched, so clearing the filter restores the full grid
        immediately — nothing is re-read and no request is repeated.
        """
        self._visible = None if rows is None else {int(row) for row in rows}
        self._rebuild()

    @property
    def visible_rows(self) -> list[int]:
        """The row numbers currently painted, in sheet order."""
        return [
            number for number, _source in self._sources
            if self._visible is None or number in self._visible
        ]

    @property
    def filtered(self) -> bool:
        return self._visible is not None

    def line_of_row(self, row: int) -> int:
        """
        Which painted line a row sits on, or -1 if it is not painted.

        A row's y is not its index: two header lines come first and any row with
        commentary hangs an extra line under it, so a caller that wants to scroll
        a row into view has to ask rather than calculate.
        """
        for y, line in enumerate(self._lines):
            if line.kind == "row" and line.number == row:
                return y
        return -1

    def row_at_line(self, y: int) -> int:
        """
        The row number painted at line `y`, or 0 for a header/empty line.

        The inverse of `line_of_row`, and it exists for the same reason: a live
        evaluation merges rows in *sheet* order, so a section that finishes late
        inserts lines above whatever is on screen and slides the view down. Asking
        which row is at the top before a rebuild, and where that row went after, is
        what lets a reader who has scrolled back stay on the lines they were
        reading.
        """
        if 0 <= y < len(self._lines):
            return self._lines[y].number
        return 0

    def update_column(self, letter: str, column: Column) -> None:
        for index, existing in enumerate(self._columns):
            if existing.letter == letter:
                self._columns[index] = column
                break
        self._rebuild()

    def set_notes(self, notes: dict[int, str], styles: dict[int, str] | None = None) -> None:
        """
        The commentary hung under each row, and optionally the colour to use.

        `styles` is how severity reaches the grid: a critical row's note is red
        and a minor one's is grey, so the worst rows are findable by scanning a
        column rather than by reading every comment.
        """
        self._notes = dict(notes)
        self._note_styles = dict(styles or {})
        self._rebuild()

    def set_row_styles(self, styles: dict[int, str]) -> None:
        """
        Colour whole rows by status, independent of any note under them.

        The Judge grid uses this to paint severity and "fixed" onto the row
        itself: critical red, moderate yellow, minor grey, fixed green, a clean
        row the default. A colour on the row is faster to scan than a note, and
        it survives a row that carries no comment at all.
        """
        self._row_styles = dict(styles)
        self.refresh()

    def tick(self) -> None:
        """Advance the spinner on any column still waiting."""
        if any(c.state == PENDING for c in self._columns):
            self._tick += 1
            self.refresh()

    @property
    def waiting(self) -> bool:
        return any(c.state == PENDING for c in self._columns)

    # ── Layout ────────────────────────────────────────────────────────────────

    def _rebuild(self) -> None:
        if self._held:
            return              # a batched() block is open; it will rebuild once
        lines: list[_Line] = [
            _Line("head", cells=["SOURCE"] + [c.label for c in self._columns]),
            _Line("stat", cells=[""] + [c.stat for c in self._columns]),
        ]
        for number, source in self._sources:
            if self._visible is not None and number not in self._visible:
                continue
            cells = [source]
            for column in self._columns:
                cells.append(self._cell_for(column, number))
            lines.append(_Line("row", number=number, cells=cells))
            note = self._notes.get(number)
            if note:
                lines.append(_Line(
                    "note", number=number, text=note,
                    style=self._note_styles.get(number, ""),
                ))
        if len(lines) == 2 and self._sources:
            lines.append(_Line("empty", text="No rows match this filter."))
        self._lines = lines
        self.refresh(layout=True)

    def _cell_for(self, column: Column, number: int) -> str:
        if column.state == PENDING:
            return ""            # the spinner is drawn in _line_text
        if column.error:
            return "—"
        value = (column.rows.get(number) or "").strip()
        return value or column.placeholder

    def _widths(self) -> tuple[list[int], int]:
        num_models = len(self._columns)
        columns = 1 + num_models
        available = max(40, self.size.width or 100) - self.GUTTER

        # Sane weighted split (~38% of available width to source, at least 15, max 80)
        src_width = max(15, min(80, int(available * 0.38) - 1))
        remaining = available - src_width

        # Distribute remaining width to models (leaving 1 char gap per column)
        model_width = max(8, (remaining - num_models) // num_models) if num_models else 8

        widths = [src_width] + [model_width] * num_models
        return widths, columns

    def get_content_height(self, container, viewport, width: int) -> int:
        return max(1, len(self._lines))

    # ── Render ────────────────────────────────────────────────────────────────

    def _row_style(self, y: int) -> RichStyle:
        if y >= len(self._lines):
            return self.rich_style
        line = self._lines[y]
        # The cursor wins over the hover band: the pointer is transient and the
        # cursor is what a keystroke will act on, so it must stay visible while
        # the mouse wanders over other rows.
        if self.cursor_row > 0 and line.number == self.cursor_row:
            return self.rich_style + RichStyle(bgcolor=SELECT_BG)
        if self.hover_row > 0 and line.number == self.hover_row:
            return self.rich_style + RichStyle(bgcolor=HOVER_BG)
        return self.rich_style

    def render_line(self, y: int) -> Strip:
        return strip_line(self._line_text(y), self.size.width, self._row_style(y))

    def _line_text(self, y: int, widths: list[int] | None = None) -> Text:
        if y >= len(self._lines):
            return Text("")
        line = self._lines[y]
        if widths is None:
            widths, _columns = self._widths()

        if line.kind == "note":
            # Judge commentary, hung under the row it is about. Flattened: a
            # comment carrying a newline would push the rest of it onto the next
            # painted row, over the gutter where a row number goes.
            style = line.style or WARN
            text = Text(" " * self.GUTTER)
            text.append("↳ ", style=style)
            text.append(flatten_cell(line.text), style=style)
            return text

        if line.kind == "empty":
            text = Text(" " * self.GUTTER)
            text.append(line.text, style=MUTED)
            return text

        if line.kind == "head":
            text = Text(f"{'#':>{self.GUTTER - 1}} ", style=MUTED)
            for index, cell in enumerate(line.cells):
                w = widths[index]
                if index == 0:
                    text.append(pad_cells(cell, w) + " ", style=MUTED)
                    continue
                column = self._columns[index - 1]
                style = f"bold {OK}" if column.winner else f"bold {ACCENT}"
                mark = "★ " if column.winner else ""
                text.append(
                    pad_cells(f"{column.letter} {mark}{cell}", w) + " ", style=style
                )
            return text

        if line.kind == "stat":
            text = Text(" " * self.GUTTER)
            for index, cell in enumerate(line.cells):
                w = widths[index]
                if index == 0:
                    text.append(pad_cells(cell, w) + " ")
                    continue
                column = self._columns[index - 1]
                style = ERROR if column.error else MUTED
                shown = cell
                if column.score:
                    shown = f"{column.score}  {cell}".strip()
                    style = OK if not column.error else ERROR
                text.append(pad_cells(shown, w) + " ", style=style)
            return text

        row_style = self._row_styles.get(line.number, "")
        text = Text(f"{line.number:>{self.GUTTER - 1}} ", style=MUTED)
        for index, cell in enumerate(line.cells):
            w = widths[index]
            if index == 0:
                text.append(pad_cells(cell, w) + " ", style=row_style)
                continue
            column = self._columns[index - 1]
            if column.state == PENDING:
                text.append(
                    pad_cells(spinner_frame(self._tick) + " …", w) + " ", style=ACCENT
                )
                continue
            if column.error:
                text.append(pad_cells("—", w) + " ", style=ERROR)
                continue
            # A row style (severity, fixed) wins over the per-cell default, so
            # the whole line reads as one colour; "(missing)" still warns.
            style = row_style
            if cell == "(missing)":
                style = WARN
            text.append(pad_cells(cell, w) + " ", style=style)
        return text
