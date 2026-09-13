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
                 on_accept_multiple: Callable[[list[str]], None] | None = None,
                 max_files: int = 4,
                 next_screen: Callable[[], BaseScreen] | None,
                 existing_path: str = "",
                 sort_translated_first: bool = False,
                 **kwargs) -> None:
        super().__init__(**kwargs)
        self._title = title
        if existing_path and ".uet_cache" in existing_path:
            existing_path = ""
        self._confirm_label = confirm_label
        self._on_accept = on_accept
        self._on_accept_multiple = on_accept_multiple
        self._max_files = max_files
        self._next = next_screen
        self._existing = existing_path
        self._sort_translated = sort_translated_first
        self.selected_files: list[str] = (
            [existing_path] if existing_path and os.path.isfile(existing_path) and ".uet_cache" not in existing_path else []
        )

    def parse_manual_paths(self, text: str) -> list[str]:
        """Parse single or multiple comma/semicolon-separated paths, checking existence."""
        import re
        parts = re.split(r"[,;]+", text or "")
        paths: list[str] = []
        for p in parts:
            cleaned = os.path.abspath(os.path.expanduser(p.strip().strip('"').strip("'")))
            if cleaned and os.path.isfile(cleaned):
                if cleaned not in paths:
                    paths.append(cleaned)
        return paths[:self._max_files]

    def toggle_selection(self, path: str) -> None:
        """Toggle a file in or out of multi-selection up to max_files."""
        canon = os.path.abspath(os.path.expanduser((path or "").strip().strip('"').strip("'")))
        if not canon or not os.path.isfile(canon):
            return
        # Compare canonical paths
        existing_matches = [p for p in self.selected_files if os.path.abspath(p) == canon]
        if existing_matches:
            for p in existing_matches:
                self.selected_files.remove(p)
        else:
            if len(self.selected_files) < self._max_files:
                self.selected_files.append(canon)
            else:
                try:
                    error = self.query_one("#file-error", Label)
                    error.update(f"Maximum {self._max_files} files allowed")
                except Exception:
                    pass
        self._sync_picker_checkboxes()
        self._update_selection_display()

    def _sync_picker_checkboxes(self) -> None:
        if not hasattr(self, "picker") or self.picker is None:
            return
        selected_set = {os.path.abspath(p) for p in self.selected_files}
        for opt in getattr(self.picker, "options", []):
            if opt.value:
                opt.selected = os.path.abspath(str(opt.value)) in selected_set
        self.picker.refresh()

    def confirm_selection(self) -> bool:
        """Confirm selected files or the manual path."""
        valid_paths = [p for p in self.selected_files if os.path.isfile(p)]
        if not valid_paths:
            raw = (getattr(self, "_path", None) and self._path.input.value or "").strip()
            manual = self.parse_manual_paths(raw)
            if manual:
                valid_paths = manual
            else:
                path = os.path.abspath(os.path.expanduser(raw.strip('"').strip("'")))
                if os.path.isfile(path):
                    valid_paths = [path]
        if not valid_paths:
            return False
        if self._on_accept_multiple is not None:
            self._on_accept_multiple(valid_paths)
        self._on_accept(valid_paths[0])
        return True

    def _update_selection_display(self) -> None:
        if not hasattr(self, "_path") or self._path is None:
            return
        count = len(self.selected_files)
        if count == 0:
            pass
        elif count == 1:
            self._path.input.value = self.selected_files[0]
        else:
            names = ", ".join(os.path.basename(p) for p in self.selected_files)
            self._path.input.value = f"Selected ({count}/{self._max_files} files): {names}"

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
        scanned_paths = {os.path.abspath(os.path.join(os.getcwd(), name)) for name in files}
        self.selected_files = [p for p in self.selected_files if os.path.abspath(p) in scanned_paths]
        options = []
        selected_set = {os.path.abspath(p) for p in self.selected_files}
        for name in files:
            full = os.path.join(os.getcwd(), name)
            try:
                size = os.path.getsize(full) / 1024
                size_text = f"{size / 1024:.1f} MB" if size > 1024 else f"{size:.0f} KB"
            except OSError:
                size_text = "?"
            opt = Option(name, full, hint=size_text)
            if os.path.abspath(full) in selected_set:
                opt.selected = True
            options.append(opt)
        if self._sort_translated:
            options.sort(key=lambda o: (
                0 if config.TRANSLATED_SUFFIX.lower() in o.label.lower() else 1,
                o.label.lower(),
            ))
        if not options:
            options.append(Option("No spreadsheet found here — type a path below", "",
                                  enabled=False, style=MUTED))
        self.picker.multi_select = True
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
            if not self.selected_files:
                self._path.input.value = str(event.option.value)
            self._keep_cursor_visible()

    def on_picker_toggled(self, event: Picker.Toggled) -> None:
        if event.picker is self.picker and event.option and event.option.value:
            val = str(event.option.value)
            canon = os.path.abspath(val)
            if event.option.selected:
                if len(self.selected_files) >= self._max_files:
                    event.option.selected = False
                    self.picker.refresh()
                    try:
                        error = self.query_one("#file-error", Label)
                        error.update(f"Maximum {self._max_files} files allowed")
                    except Exception:
                        pass
                    return
                if canon not in [os.path.abspath(p) for p in self.selected_files]:
                    self.selected_files.append(canon)
            else:
                self.selected_files = [p for p in self.selected_files if os.path.abspath(p) != canon]
            self._update_selection_display()

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

    def on_key(self, event) -> None:
        if event.key == "space":
            opt = self.picker.selected_option
            if opt and opt.value:
                self.toggle_selection(str(opt.value))
                event.stop()

    def _confirm(self) -> None:
        if self.selected_files:
            if not self.confirm_selection():
                error = self.query_one("#file-error", Label)
                error.update("Selected files are not valid")
                return
            if self._next is None:
                self.app.pop_screen()
            else:
                self.app.push_screen(self._next())
            return

        raw = (self._path.input.value or "").strip()
        manual = self.parse_manual_paths(raw)
        if manual:
            if self._on_accept_multiple is not None:
                self._on_accept_multiple(manual)
            self._on_accept(manual[0])
            if self._next is None:
                self.app.pop_screen()
            else:
                self.app.push_screen(self._next())
            return

        path = os.path.abspath(os.path.expanduser(raw.strip('"').strip("'")))
        error = self.query_one("#file-error", Label)

        def accept(path_: str) -> None:
            if self._on_accept_multiple is not None:
                self._on_accept_multiple([path_])
            self._on_accept(path_)

        if not resolve_accept(path, accept):
            error.update(f"File not found: {path}")
            return
        if self._next is None:
            self.app.pop_screen()
        else:
            self.app.push_screen(self._next())

    def screen_keys(self) -> list[tuple[str, str]]:
        return [("↑↓", "move"), ("space", "toggle (up to 4)"), ("⏎", "confirm"),
                ("tab", "next field"), ("←", "back"), ("esc", "cancel")]


FileSelectScreen = FileEngineScreen
