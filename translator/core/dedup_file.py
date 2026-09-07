"""
core/dedup_file.py — Unique Working File Architecture for Deduplication & TM.

Extracts unique source rows from a spreadsheet into a working file in `.uet_cache/`
and provides atomic broadcast synchronization from the unique working file back
into the full target spreadsheet.
"""

from __future__ import annotations

import json
import os
import shutil
from typing import Any

from .. import config
from ..io import csv_io
from ..io import excel_io


def _get_cache_dir(src_path: str) -> str:
    parent = os.path.dirname(os.path.abspath(src_path))
    cache_dir = os.path.join(parent, ".uet_cache")
    try:
        os.makedirs(cache_dir, exist_ok=True)
        # Test write permission
        test_file = os.path.join(cache_dir, ".test_write")
        with open(test_file, "w") as f:
            f.write("ok")
        os.remove(test_file)
        return cache_dir
    except (OSError, PermissionError):
        # Fallback to user config directory
        fallback = os.path.join(config.CONFIG_DIR, "cache")
        os.makedirs(fallback, exist_ok=True)
        return fallback


def prepare_unique_file(
    src_path: str, src_col: int, dst_col: int, start_row: int = 2
) -> tuple[str, str, int, int]:
    """
    Inspects src_path. If duplicate source texts exist, creates:
      - .uet_cache/<stem>.unique.<ext> (containing only unique rows)
      - .uet_cache/<stem>.map.json (mapping unique row numbers -> all original row numbers)

    Returns:
      (active_file_path, map_file_path, total_rows, unique_rows)

    If no duplicates exist, returns (src_path, "", total_rows, total_rows).
    """
    src_path = os.path.abspath(src_path)
    is_csv_file = csv_io.is_csv(src_path)
    src_idx = src_col - 1
    start_idx = start_row - 1

    if is_csv_file:
        fmt = csv_io.sniff(src_path)
        all_rows = csv_io.read_rows(src_path, fmt)
    else:
        wb = excel_io.load_workbook(src_path)
        ws = wb.active
        all_rows = [
            [str(cell.value) if cell.value is not None else "" for cell in row]
            for row in ws.iter_rows()
        ]

    if not all_rows or len(all_rows) <= start_idx:
        return src_path, "", 0, 0

    header_rows = all_rows[:start_idx]
    data_rows = all_rows[start_idx:]
    total_rows = len(data_rows)

    # Group rows by source text
    groups: dict[str, list[int]] = {}
    row_data_map: dict[int, list[str]] = {}

    for offset, row in enumerate(data_rows):
        sheet_row = start_row + offset
        row_data_map[sheet_row] = list(row)
        src_val = row[src_idx] if len(row) > src_idx else ""
        text = src_val.strip()
        groups.setdefault(text, []).append(sheet_row)

    # If every non-empty row is already unique, no deduplication needed
    has_duplicates = any(len(sheet_rows) > 1 for text, sheet_rows in groups.items() if text)
    if not has_duplicates:
        return src_path, "", total_rows, total_rows

    cache_dir = _get_cache_dir(src_path)
    stem, ext = os.path.splitext(os.path.basename(src_path))
    unique_file_path = os.path.join(cache_dir, f"{stem}.unique{ext}")
    map_file_path = os.path.join(cache_dir, f"{stem}.map.json")

    unique_data_rows: list[list[str]] = []
    rep_to_full: dict[str, list[int]] = {}

    # Build unique data rows in order of first appearance
    curr_unique_sheet_row = start_row
    for text, full_sheet_rows in groups.items():
        if not text:
            # Empty rows: keep each empty row as an individual row
            for empty_row_num in full_sheet_rows:
                unique_data_rows.append(row_data_map[empty_row_num])
                rep_to_full[str(curr_unique_sheet_row)] = [empty_row_num]
                curr_unique_sheet_row += 1
        else:
            rep_sheet_row = full_sheet_rows[0]
            unique_data_rows.append(row_data_map[rep_sheet_row])
            rep_to_full[str(curr_unique_sheet_row)] = full_sheet_rows
            curr_unique_sheet_row += 1

    final_unique_rows = header_rows + unique_data_rows
    unique_rows_count = len(unique_data_rows)

    # Write unique file
    if is_csv_file:
        csv_io.write_rows(unique_file_path, final_unique_rows, fmt)
    else:
        import openpyxl
        out_wb = openpyxl.Workbook()
        out_ws = out_wb.active
        for r_idx, row in enumerate(final_unique_rows, start=1):
            for c_idx, val in enumerate(row, start=1):
                out_ws.cell(row=r_idx, column=c_idx, value=val)
        out_wb.save(unique_file_path)

    # Write map JSON
    map_payload = {
        "version": 1,
        "original_file": src_path,
        "unique_file": unique_file_path,
        "src_col": src_col,
        "dst_col": dst_col,
        "start_row": start_row,
        "total_rows": total_rows,
        "unique_rows": unique_rows_count,
        "rep_to_full": rep_to_full,
    }
    with open(map_file_path, "w", encoding="utf-8") as f:
        json.dump(map_payload, f, indent=2)

    return unique_file_path, map_file_path, total_rows, unique_rows_count


def sync_unique_to_full(
    map_file_path: str, unique_out_path: str, full_out_path: str, dst_col: int
) -> int:
    """
    Reads unique translations from unique_out_path, loads mapping from map_file_path,
    and updates all corresponding sibling rows in full_out_path at dst_col (1-based index).
    Returns the total number of cells updated in full_out_path.
    """
    if not map_file_path or not os.path.exists(map_file_path) or not os.path.exists(unique_out_path):
        return 0

    try:
        with open(map_file_path, "r", encoding="utf-8") as f:
            map_data = json.load(f)
    except Exception:
        return 0

    rep_to_full: dict[str, list[int]] = map_data.get("rep_to_full", {})
    if not rep_to_full:
        return 0

    start_row = map_data.get("start_row", 2)
    dst_idx = dst_col - 1

    # Load translations from unique file
    is_csv_unique = csv_io.is_csv(unique_out_path)
    if is_csv_unique:
        fmt_u = csv_io.sniff(unique_out_path)
        u_rows = csv_io.read_rows(unique_out_path, fmt_u)
    else:
        wb_u = excel_io.load_workbook(unique_out_path)
        ws_u = wb_u.active
        u_rows = [
            [str(cell.value) if cell.value is not None else "" for cell in row]
            for row in ws_u.iter_rows()
        ]

    translations_by_unique_row: dict[int, str] = {}
    for offset, row in enumerate(u_rows[start_row - 1:]):
        u_sheet_row = start_row + offset
        if len(row) > dst_idx:
            val = row[dst_idx].strip()
            if val:
                translations_by_unique_row[u_sheet_row] = val

    if not translations_by_unique_row:
        return 0

    # Build writes map for full file: full_sheet_row -> text
    payload_to_write: dict[int, str] = {}
    for u_row, full_rows_list in rep_to_full.items():
        u_row_int = int(u_row)
        if u_row_int in translations_by_unique_row:
            text_to_sync = translations_by_unique_row[u_row_int]
            for full_row_num in full_rows_list:
                payload_to_write[full_row_num] = text_to_sync

    if not payload_to_write:
        return 0

    # Write into full_out_path
    is_csv_full = csv_io.is_csv(full_out_path)
    if is_csv_full:
        fmt_f = csv_io.sniff(full_out_path)
        full_rows = csv_io.read_rows(full_out_path, fmt_f)
        for full_row_num, text in payload_to_write.items():
            f_idx = full_row_num - 1
            if 0 <= f_idx < len(full_rows):
                while len(full_rows[f_idx]) <= dst_idx:
                    full_rows[f_idx].append("")
                full_rows[f_idx][dst_idx] = text
        csv_io.write_rows(full_out_path, full_rows, fmt_f)
    else:
        import openpyxl
        wb_f = openpyxl.load_workbook(full_out_path)
        ws_f = wb_f.active
        for full_row_num, text in payload_to_write.items():
            ws_f.cell(row=full_row_num, column=dst_col, value=text)
        wb_f.save(full_out_path)

    return len(payload_to_write)
