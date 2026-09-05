"""
tui/screens/audit.py — Text Fitting Audit: measure any file, any time.

The audit is the mechanical twin of the judge criterion: no model, no tokens,
no verdicts — the machine wraps, the machine reports, the user applies. It
exists for files that never went through a fitted translate run (spec 2a):
pick a file, pick an engine (General is refused — there is nothing to measure
without a window), see the findings, apply the wrap suggestions wholesale or
one by one. Applying backs the file up once (<stem>.bak) and writes the
Translated column.
"""

from __future__ import annotations

import os

from textual.widgets import Label

from ...fitting import Finding, apply_wrapped, audit_rows, suggest_wrap
from ...fitting.profiles import get_profile
from ...io.excel_io import ExcelReader
from ...parsing.columns import detect_columns
from ..app import BaseScreen
from ..theme import ERROR, MUTED, OK, WARN
from ..widgets.compare import Column, CompareGrid

def general_refusal() -> str:
    return ("Audit needs an engine profile — General has no dialog window to "
            "measure. Pick an engine on the previous screen.")

def findings_notes(findings: list[Finding]) -> dict[int, str]:
    """One-line note per finding row, the grid's under-row commentary."""
    out: dict[int, str] = {}
    for f in findings:
        bits = [k.upper() for k in f.kinds]
        out[f.row] = (
            f"{f.severity.upper()} · {' · '.join(bits)} · "
            f"{f.target_wrap.lines} lines (max {f.target_wrap.budget:.0f} em budget)"
        )
    return out

class AuditScreen(BaseScreen):
    """File+engine (via FileEngineScreen) -> findings grid -> apply."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._path = ""
        self._engine = "general"
        self.grid = CompareGrid(id="audit-grid")

    def compose_body(self):
        yield Label("Text Fitting Audit", classes="title")
        yield Label("Pick a translated file and its engine — UET measures every "
                    "row against the dialog window.", classes="muted")
        yield self.grid

    def on_screen_ready(self) -> None:
        if self._path:
            self._run()
            return
        self._pick()

    def _pick(self) -> None:
        from .file_engine import FileEngineScreen
        self.app.push_screen(FileEngineScreen(
            title="Audit a spreadsheet",
            confirm_label="Run audit",
            on_accept=self._file_accepted,
            next_screen=lambda: self,
            existing_path=self._path,
            sort_translated_first=True,
        ))

    def _file_accepted(self, path: str, engine: str) -> None:
        self._path = path
        self._engine = engine

    def _run(self) -> None:
        profile = get_profile(self._engine)
        if profile is None:
            self.notify_status(general_refusal(), WARN)
            self._pick()
            return
        rows = self._load_rows()
        findings = audit_rows(rows, profile)
        self._show_findings(findings, profile, rows)

    def _load_rows(self) -> list[tuple[int, str, str]]:
        letters, headers, sample = ExcelReader.grid(self._path, 1, 1, 26)
        src, dst = detect_columns(headers)
        if src is None or dst is None:
            self.notify_status("Could not find Source/Translated columns by header.", WARN)
            return []
        sources = ExcelReader.load_column(self._path, src, 2, 0)
        targets = dict(ExcelReader.load_column(self._path, dst, 2, 0))
        return [(r, s, targets.get(r, "")) for r, s in sources if (s or "").strip()]

    def _show_findings(self, findings: list[Finding], profile, rows) -> None:
        src_map = {r: s for r, s, _t in rows}
        tgt_map = {r: t for r, _s, t in rows}
        grid_rows = [(f.row, src_map.get(f.row, "")) for f in findings]
        styles = {"critical": ERROR, "moderate": WARN, "minor": MUTED}
        with self.grid.batched():
            self.grid.set_sources(grid_rows, [
                Column(letter="S", label="TARGET", rows={f.row: tgt_map.get(f.row, "") for f in findings}),
                Column(letter="W", label="WRAP SUGGESTION",
                       rows={f.row: (suggest_wrap(tgt_map.get(f.row, ""), profile)
                                     if f.newline_tag else "— shorten via Judge (no tag in source)")
                             for f in findings}),
            ])
            self.grid.set_notes(findings_notes(findings),
                                {f.row: styles.get(f.severity, MUTED) for f in findings})
            self.grid.set_row_styles({f.row: styles.get(f.severity, MUTED) for f in findings})
        self.notify_status(f"{len(findings)} row(s) do not fit the dialog window.", OK)

    def action_apply_all(self) -> None:
        profile = get_profile(self._engine)
        if profile is None:
            return
        rows = self._load_rows()
        tgt_map = {r: t for r, _s, t in rows}
        from ...fitting import audit_rows as _ar
        payload = {}
        for f in _ar(rows, profile):
            # Only rows whose SOURCE has a tag are wrappable (spec 2a): the
            # machine never invents a token the file does not prove.
            if f.newline_tag:
                payload[f.row] = suggest_wrap(tgt_map.get(f.row, ""), profile)
        if not payload:
            self.notify_status("Nothing to apply — no wrappable rows (try the judge's shorten advice).", WARN)
            return
        n = apply_wrapped(self._path, payload)
        self.notify_status(f"Applied {n} wrap fix(es). Backup at {os.path.splitext(self._path)[0]}.bak", OK)

    def screen_keys(self) -> list[tuple[str, str]]:
        return [("a", "apply all"), ("r", "rerun"), ("←", "back"), ("esc", "cancel")]
