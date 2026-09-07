"""
tui/screens/file_engine.py — pick the spreadsheet on its own screen.

Layout: the Path input sits FIRST — it is the selection readout, so the clicked
file fills the box directly above the list instead of the eye jumping across
a tall panel. The file box below is adaptive: up to FILE_BOX_MAX files rendered
in full, past that it scrolls with native mouse wheel and cursor-follow.
One clear Confirm and next button at the bottom.
"""

from __future__ import annotations

import os
from typing import Callable

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Label

from ... import config
from ...io.excel_io import ExcelReader
from ..app import BaseScreen
from ..theme import MUTED, OK
from ..widgets.common import LabeledInput, Option, Picker

FILE_BOX_MAX = 10   # adaptive box: full list up to 10 rows, past that it scrolls


def resolve_accept(path: str, on_ok: Callable[[str], None]) -> bool:
    """Validate the path; on success hand path to the caller."""
    path = os.path.expanduser((path or "").strip().strip('"'))
    if not path or not os.path.isfile(path):
        return False
    on_ok(path)
    return True


class FileEngineScreen(BaseScreen):
    def __init__(self, *, title="Select a spreadsheet",
                 confirm_label="Confirm and next",
                 on_accept: Callable[[str], None],
                 next_screen: Callable[[], BaseScreen] | None,
                 existing_path: str = "",
                 sort_translated_first: bool = False,
                 **kwargs) -> None:
        super().__init__(**kwargs)
        self._title = title
        self._confirm_label = confirm_label
        self._on_accept = on_accept
        self._next = next_screen
        self._existing = existing_path
        self._sort_translated = sort_translated_first

    def compose_body(self) -> ComposeResult:
        yield Label(self._title, classes="title")
        self._path = LabeledInput("Path", value=self._existing,
                                  placeholder="/path/to/file.xlsx or .csv")
        yield self._path
        yield Label(f".xlsx / .csv files in {os.getcwd()}", classes="muted")
        with VerticalScroll(id="file-scroll", can_focus=False) as scroll:
            scroll.styles.border = ("round", MUTED)
            self.picker = Picker()
            self.picker.styles.max_height = 10000
            yield self.picker
        yield Label("", id="file-error", classes="error")

    def compose_above_footer(self) -> ComposeResult:
        self._confirm_bar = Picker(
            [Option(self._confirm_label, "confirm", style=OK)],
            id="confirm-bar",
        )
        yield self._confirm_bar

    def on_screen_ready(self) -> None:
        files = ExcelReader.scan_directory(os.getcwd())
        options = []
        for name in files:
            full = os.path.join(os.getcwd(), name)
            try:
                size = os.path.getsize(full) / 1024
                size_text = f"{size / 1024:.1f} MB" if size > 1024 else f"{size:.0f} KB"
            except OSError:
                size_text = "?"
            options.append(Option(name, full, hint=size_text))
        if self._sort_translated:
            options.sort(key=lambda o: (
                0 if config.TRANSLATED_SUFFIX.lower() in o.label.lower() else 1,
                o.label.lower(),
            ))
        if not options:
            options.append(Option("No spreadsheet found here — type a path below", "",
                                  enabled=False, style=MUTED))
        self.picker.set_options(options)
        scroll = self.query_one("#file-scroll", VerticalScroll)
        scroll.styles.height = min(len(options), FILE_BOX_MAX) + 2
        if self._existing:
            for i, opt in enumerate(options):
                if opt.value == self._existing:
                    self.picker.index = i
                    break
        self.picker.focus()

    def on_picker_highlighted(self, event: Picker.Highlighted) -> None:
        if event.picker is self.picker and event.option and event.option.value:
            self._path.input.value = str(event.option.value)
            self._keep_cursor_visible()

    def _keep_cursor_visible(self) -> None:
        """Scroll the box so the cursor row never leaves view."""
        try:
            scroll = self.query_one("#file-scroll", VerticalScroll)
            view = max(1, scroll.size.height - 1)
            y = scroll.scroll_offset.y
            index = self.picker.index
            if index < y:
                scroll.scroll_to(y=index, animate=False)
            elif index >= y + view:
                scroll.scroll_to(y=index - view + 1, animate=False)
        except Exception:
            pass

    def on_picker_selected(self, event: Picker.Selected) -> None:
        if event.picker is getattr(self, "_confirm_bar", None):
            self._confirm()
            return
        if event.picker is self.picker and event.option.value:
            self._path.input.value = str(event.option.value)
            self._confirm_bar.focus()

    def on_input_submitted(self, event) -> None:
        self._confirm()

    def _confirm(self) -> None:
        raw = (self._path.input.value or "").strip().strip('"')
        path = os.path.expanduser(raw)
        error = self.query_one("#file-error", Label)

        def accept(path_: str) -> None:
            self._on_accept(path_)

        if not resolve_accept(path, accept):
            error.update(f"File not found: {path}")
            return
        if self._next is None:
            self.app.pop_screen()
        else:
            self.app.push_screen(self._next())

    def screen_keys(self) -> list[tuple[str, str]]:
        return [("↑↓", "move"), ("⏎", "select"), ("tab", "next field"),
                ("←", "back"), ("esc", "cancel")]


FileSelectScreen = FileEngineScreen
