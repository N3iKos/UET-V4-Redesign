"""
fitting/apply.py — write mechanical wrap suggestions into the translated file.

The audit screen's apply action. The first write backs the file up as
<stem>.bak and that backup is never overwritten afterwards — "restore to
before the audit" must always mean the original, not an intermediate state.
The Translated column is located by header name via the same keyword rules the
rest of the app uses (parsing/columns.py vocabulary, simplified: exact
"Translated" match first, then keyword containment) so a 2-column or 6-column
dump both work.
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

def apply_wrapped(path: str, texts: dict[int, str]) -> int:
    fmt = csv_io.sniff(path)
    rows = csv_io.read_rows(path, fmt)
    if not rows:
        return 0
    col = find_translated_col(rows[0])
    if col is None:
        col = len(rows[0])          # append a new column
        rows[0] = rows[0] + ["Translated"]
    backup = os.path.splitext(path)[0] + ".bak"
    if not os.path.exists(backup):
        shutil.copy2(path, backup)
    written = 0
    for row_num, text in texts.items():
        idx = row_num - 1           # sheet row 1 = headers
        if 1 <= idx < len(rows):
            while len(rows[idx]) <= col:
                rows[idx].append("")
            rows[idx][col] = text
            written += 1
    csv_io.write_rows(path, rows, fmt)
    return written
