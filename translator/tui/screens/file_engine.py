"""
tui/screens/file_engine.py — pick the file AND the engine, on one screen.

Replaces the two near-identical file screens (wizard.FileSelectScreen /
judge.JudgeFileScreen) with one shared component, per the standing rule that
same-function screens use shared components. Layout (spec 2.1): a scrolled
file box (5 visible), a path input the box feeds on click, an engine select
under it, one Confirm and next gate. The click no longer commits — the user
may want to change the engine after picking the file, and the confirm bar is
where the commit belongs.

Keyboard contract: Enter on the list fills the path box and moves focus
there; Enter in the path box is Confirm and next. Two enters, file done.

Textual note: screens are instantiated inside a running app only. This module
is therefore import-safe (no screen instantiation at import time) and its
pure helpers (engine_options, resolve_accept) are unit-tested without an app.
"""

from __future__ import annotations

import os
from typing import Callable

from textual.widgets import Label

from ... import config
from ...data import app_prefs
from ...fitting.profiles import ENGINE_LABELS, ENGINE_ORDER, get_profile
from ...io.excel_io import ExcelReader
from ..app import BaseScreen
from ..theme import MUTED
from ..widgets.common import LabeledInput, Option, Picker

MAX_VISIBLE_FILES = 5

def engine_options() -> list[tuple[str, str]]:
    """(value, label) pairs; unverified profiles are marked so the user is told."""
    out = []
    for name in ENGINE_ORDER:
        label = ENGINE_LABELS[name]
        p = get_profile(name)
        if p is not None and not p.verified:
            label += " (unverified)"
        out.append((name, label))
    return out

def resolve_accept(path: str, engine: str, on_ok: Callable[[str, str], None]) -> bool:
    """Validate the path; on success hand (path, engine) to the caller."""
    path = os.path.expanduser((path or "").strip().strip('"'))
    if not path or not os.path.isfile(path):
        return False
    on_ok(path, engine)
    return True

class FileEngineScreen(BaseScreen):
    def __init__(self, *, title="Select a spreadsheet",
                 confirm_label="Confirm and next",
                 on_accept: Callable[[str, str], None],
                 next_screen: Callable[[], BaseScreen],
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

    def compose_body(self):
        yield Label(self._title, classes="title")
        yield Label(f".xlsx / .csv files in {os.getcwd()}", classes="muted")
        self.picker = Picker(max_visible=MAX_VISIBLE_FILES)
        yield self.picker
        yield Label("")
        self._path = LabeledInput("Path", value=self._existing,
                                  placeholder="/path/to/file.xlsx or .csv")
        yield self._path
        yield Label("Engine (text fitting)", classes="muted")
        self._engine = Picker()          # single-select, click_activates=False
        yield self._engine
        yield Label("Fitting measures the engine's default message window — "
                    "wordwrap plugins make it inaccurate.", classes="muted")
        yield Label("", id="file-engine-error", classes="error")

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
            # Judge habit: translated outputs first (ported from JudgeFileScreen).
            options.sort(key=lambda o: (
                0 if config.TRANSLATED_SUFFIX.lower() in o.label.lower() else 1,
                o.label.lower(),
            ))
        if not options:
            options.append(Option("No spreadsheet found here — type a path below", "",
                                  enabled=False, style=MUTED))
        self.picker.set_options(options)
        if self._existing:
            for i, opt in enumerate(options):
                if opt.value == self._existing:
                    self.picker.index = i
                    break
        self._engine.set_options([
            Option(label, value, selected=value == "general")
            for value, label in engine_options()
        ])
        self.picker.focus()

    def on_picker_highlighted(self, event: Picker.Highlighted) -> None:
        # The file box feeds the path box as the cursor moves (and the
        # remembered engine follows the highlighted file).
        if event.picker is self.picker and event.option and event.option.value:
            self._path.input.value = str(event.option.value)
            self._recall_engine(str(event.option.value))

    def _recall_engine(self, path: str) -> None:
        engine = app_prefs.remembered_engine(path)
        for i, opt in enumerate(self._engine.options):
            if opt.value == engine:
                self._engine.index = i
                break

    def on_picker_selected(self, event: Picker.Selected) -> None:
        # Enter/click on a file: fill the path, move to the engine select.
        if event.picker is self.picker and event.option.value:
            self._path.input.value = str(event.option.value)
            self._recall_engine(str(event.option.value))
            self._engine.focus()

    def on_input_submitted(self, event) -> None:
        self._confirm()

    def _confirm(self) -> None:
        path = (self._path.input.value or "").strip().strip('"')
        engine = str(self._engine.current.value) if self._engine.current else "general"
        error = self.query_one("#file-engine-error", Label)
        def accept(path_: str, engine_: str) -> None:
            self._on_accept(path_, engine_)
        if not resolve_accept(path, engine, accept):
            error.update(f"File not found: {path}")
            return
        app_prefs.remember_engine(path, engine)
        self.app.push_screen(self._next())

    def screen_keys(self) -> list[tuple[str, str]]:
        return [("↑↓", "move"), ("⏎", "select"), ("tab", "next field"),
                ("←", "back"), ("esc", "cancel")]
