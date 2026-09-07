"""
tui/screens/audit.py — Text Fitting Audit: measure any file, any time.

The audit is the mechanical twin of the judge criterion: no model, no tokens,
no verdicts — the machine wraps, the machine reports, the user applies. It
exists for files that never went through a fitted translate run (spec 2a):
pick a file, pick an engine (General is refused — there is nothing to measure
without a window), see the findings, apply the wrap suggestions wholesale or
one by one. Applying backs the file up once (<stem>.bak) and writes the
Translated column.

Flow: AuditScreen pushes FileEngineScreen; on confirm the picker pops itself
off the stack (next_screen=None) and this screen — which stayed mounted the
whole time — runs the audit in `_file_accepted`. Re-pushing this same instance
as `next_screen` would skip remounting (Textual mounts a screen once), so
`_run` would never fire; popping instead keeps the stack honest.
"""

from __future__ import annotations

import os

from textual.binding import Binding
from textual.widgets import Label

from ...fitting import Finding, apply_wrapped, audit_rows, suggest_wrap
from ...fitting.profiles import EngineProfile, get_profile
from ...io import csv_io
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

    BINDINGS = [
        Binding("a", "apply_all", "apply all", show=False),
        Binding("r", "rerun", "rerun", show=False),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._path = ""
        self._engine = "general"
        self._picking = False
        self._dst_col: int | None = None   # the column the findings audited
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

    def on_screen_resumed(self) -> None:
        # If the user pressed Back / Esc on the file-picker screen without
        # choosing a file, don't leave them stranded on an empty audit screen —
        # return them cleanly back to the menu (UX fix).
        if self._picking and not self._path:
            self.app.pop_screen()

    def _pick(self) -> None:
        self._picking = True
        from .file_engine import FileEngineScreen
        self.app.push_screen(FileEngineScreen(
            title="Audit a spreadsheet",
            confirm_label="Run audit",
            on_accept=self._file_accepted,
            next_screen=None,               # picker pops itself; we stay mounted
            existing_path=self._path,
            sort_translated_first=True,
        ))

    def _file_accepted(self, path: str, engine: str) -> None:
        self._path = path
        self._engine = engine
        # The picker pops itself after this returns; the screen below is live,
        # so the audit runs now and its results are on screen after the pop.
        self._run()

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
        self._dst_col = dst
        # num_rows=0 is NOT an "all rows" sentinel anywhere in this app: it
        # makes load_column's range empty (excel_io computes end = start+0-1).
        # Read the real count, like the judge runner does.
        total = ExcelReader.row_count(self._path)
        num_rows = max(0, total - 2 + 1)
        sources = ExcelReader.load_column(self._path, src, 2, num_rows)
        targets = dict(ExcelReader.load_column(self._path, dst, 2, num_rows))
        return [(r, s, targets.get(r, "")) for r, s in sources if (s or "").strip()]

    def _show_findings(self, findings: list[Finding], profile: EngineProfile, rows) -> None:
        if not rows:
            self.notify_status("No rows found in this file.", WARN)
            return
        has_translations = any((t or "").strip() for _r, _s, t in rows)
        if not has_translations:
            self.notify_status(
                "Target column is completely empty — nothing is translated yet. "
                "Audit checks translated text; run Translate first or audit a *_Translated file.",
                WARN,
            )
            return
        src_map = {r: s for r, s, _t in rows}
        tgt_map = {r: t for r, _s, t in rows}
        grid_rows = [(f.row, src_map.get(f.row, "")) for f in findings]
        styles = {"critical": ERROR, "moderate": WARN, "minor": MUTED}
        with self.grid.batched():
            self.grid.set_sources(grid_rows, [
                Column(letter="S", label="TARGET", rows={f.row: tgt_map.get(f.row, "") for f in findings}),
                Column(letter="W", label="WRAP SUGGESTION",
                       rows={f.row: (suggest_wrap(tgt_map.get(f.row, ""), profile,
                                                  tag=f.newline_tag)
                                     if f.newline_tag else "— shorten via Judge (no tag in source)")
                             for f in findings}),
            ])
            self.grid.set_notes(findings_notes(findings),
                                {f.row: styles.get(f.severity, MUTED) for f in findings})
            self.grid.set_row_styles({f.row: styles.get(f.severity, MUTED) for f in findings})
        if findings:
            self.notify_status(f"{len(findings)} row(s) do not fit the dialog window.", OK)
        else:
            self.notify_status("All translated rows fit within the dialog window! No overflow detected.", OK)

    def action_rerun(self) -> None:
        self._run()

    def action_apply_all(self) -> None:
        profile = get_profile(self._engine)
        if profile is None or not self._path:
            return
        if not csv_io.is_csv(self._path):
            # apply_wrapped is a CSV writer; feeding it an xlsx binary would
            # overwrite the workbook with mojibake text (the reader handles
            # xlsx fine, the writer does not — so the gate is honest here).
            self.notify_status("Apply-all supports CSV/TSV only — xlsx files are read-only in the audit.", WARN)
            return
        rows = self._load_rows()
        tgt_map = {r: t for r, _s, t in rows}
        payload = {}
        for f in audit_rows(rows, profile):
            # Only rows whose SOURCE has a tag are wrappable (spec 2a): the
            # machine never invents a token the file does not prove. The tag
            # comes from the SOURCE, not the translation — a translation that
            # dropped the source's tags is still wrapped with the proven tag.
            if not f.newline_tag:
                continue
            suggestion = suggest_wrap(tgt_map.get(f.row, ""), profile, tag=f.newline_tag)
            if suggestion == (tgt_map.get(f.row) or ""):
                continue    # a no-op "fix" must not count as applied
            payload[f.row] = suggestion
        if not payload:
            self.notify_status("Nothing to apply — no wrappable rows (try the judge's shorten advice).", WARN)
            return
        n = apply_wrapped(self._path, payload, col=self._dst_col)
        if n == 0:
            self.notify_status("Nothing was written (no rows changed).", WARN)
            return
        self.notify_status(f"Applied {n} wrap fix(es). Backup at {os.path.splitext(self._path)[0]}.bak", OK)

    def screen_keys(self) -> list[tuple[str, str]]:
        return [("a", "apply all"), ("r", "rerun"), ("←", "back"), ("esc", "cancel")]
