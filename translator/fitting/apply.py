"""
fitting/apply.py — write mechanical wrap suggestions into the translated file.

The audit screen's apply action. The first write backs the file up as
<stem>.bak and that backup is never overwritten afterwards — "restore to
before the audit" must always mean the original, not an intermediate state.

CSV only, by design: the audit's reader (ExcelReader) handles .xlsx fine, but
this writer is csv_io — feeding it an xlsx binary would sniff a bogus dialect,
read garbage rows, and overwrite the workbook with mojibake text. Callers gate
on `is_csv` before writing; apply_wrapped refuses non-CSV paths itself so the
guard cannot be bypassed by a new caller.

The Translated column is located by header name via the same keyword rules the
rest of the app uses (parsing/columns.py vocabulary, simplified: exact
"Translated" match first, then keyword containment) — or the caller passes the
column it actually audited, so the write lands where the user was looking.
"""

from __future__ import annotations

import os
import shutil

from ..io import csv_io

_KEYWORDS = ("translated", "translation", "target", "output")

def find_translated_col(headers: list[str]) -> int | None:
    for i, h in enumerate(headers):
        if h.strip().lower() == "translated":
            return i
    for i, h in enumerate(headers):
        folded = h.strip().lower()
        if any(k in folded for k in _KEYWORDS):
            return i
    return None

def apply_wrapped(path: str, texts: dict[int, str], col: int | None = None) -> int:
    """
    Write `texts` (sheet row -> new text) into the Translated column.

    `col` (0-based) pins the destination column to the one the caller audited;
    when omitted the header's Translated column is used. Nothing is written —
    not even the backup — when the payload matches zero rows, so a stale
    payload cannot silently rewrite a file it was never computed against.
    """
    if not csv_io.is_csv(path):
        raise ValueError(
            f"apply_wrapped supports CSV/TSV only, not {os.path.splitext(path)[1]!r} — "
            "the audit reads xlsx but its writer would corrupt one."
        )
    fmt = csv_io.sniff(path)
    rows = csv_io.read_rows(path, fmt)
    if not rows:
        return 0
    column = col if col is not None else find_translated_col(rows[0])
    if column is None:
        column = len(rows[0])          # append a new column
        rows[0] = rows[0] + ["Translated"]
    writable = {
        row: text for row, text in texts.items()
        if 1 <= row - 1 < len(rows)
    }
    if not writable:
        return 0
    backup = os.path.splitext(path)[0] + ".bak"
    if not os.path.exists(backup):
        shutil.copy2(path, backup)
    written = 0
    for row_num, text in writable.items():
        idx = row_num - 1           # sheet row 1 = headers
        while len(rows[idx]) <= column:
            rows[idx].append("")
        rows[idx][column] = text
        written += 1
    csv_io.write_rows(path, rows, fmt)
    return written
