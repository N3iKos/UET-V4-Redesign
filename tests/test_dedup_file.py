"""
tests/test_dedup_file.py — Unit tests for unique working file generation and synchronization.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.core.dedup_file import prepare_unique_file, sync_unique_to_full
from translator.io.csv_io import read_rows, sniff, write_rows


def _create_sample_csv(path: str) -> None:
    content = [
        ["No", "Source_File", "ID", "String", "Original_text", "Translated"],
        ["1", "sys.json", "S1", "", "Hello", ""],
        ["2", "sys.json", "S2", "", "Yes", ""],
        ["3", "sys.json", "S3", "", "Hello", ""],
        ["4", "sys.json", "S4", "", "Attack", ""],
        ["5", "sys.json", "S5", "", "Hello", ""],
        ["6", "sys.json", "S6", "", "Yes", ""],
    ]
    fmt = sniff(path) if os.path.exists(path) else None
    from translator.io.csv_io import CsvFormat
    fmt = fmt or CsvFormat(delimiter=",", encoding="utf-8")
    write_rows(path, content, fmt)


def test_prepare_unique_file_with_duplicates(tmp_path):
    src_csv = str(tmp_path / "Dialog.csv")
    _create_sample_csv(src_csv)

    active_path, map_path, total_rows, unique_rows = prepare_unique_file(
        src_path=src_csv, src_col=5, dst_col=6, start_row=2
    )

    assert total_rows == 6
    assert unique_rows == 3  # "Hello", "Yes", "Attack"
    assert active_path != src_csv
    assert active_path.endswith("Dialog.unique.csv")
    assert os.path.exists(active_path)
    assert os.path.exists(map_path)

    # Read unique CSV rows
    fmt = sniff(active_path)
    rows = read_rows(active_path, fmt)
    assert len(rows) == 4  # Header + 3 unique rows
    unique_sources = [r[4] for r in rows[1:]]
    assert unique_sources == ["Hello", "Yes", "Attack"]

    # Verify map content
    with open(map_path, "r", encoding="utf-8") as f:
        map_data = json.load(f)
    assert map_data["total_rows"] == 6
    assert map_data["unique_rows"] == 3
    # Row numbers are sheet row numbers (header is row 1)
    rep_to_full = map_data["rep_to_full"]
    # unique row 2 ("Hello") corresponds to original rows [2, 4, 6] in sheet (index 1, 3, 5)
    assert rep_to_full["2"] == [2, 4, 6]
    assert rep_to_full["3"] == [3, 7]
    assert rep_to_full["4"] == [5]


def test_prepare_unique_file_without_duplicates(tmp_path):
    src_csv = str(tmp_path / "UniqueOnly.csv")
    from translator.io.csv_io import CsvFormat
    fmt = CsvFormat(delimiter=",", encoding="utf-8")
    write_rows(
        src_csv,
        [
            ["No", "Text", "Translated"],
            ["1", "Apple", ""],
            ["2", "Banana", ""],
            ["3", "Orange", ""],
        ],
        fmt,
    )

    active_path, map_path, total_rows, unique_rows = prepare_unique_file(
        src_path=src_csv, src_col=2, dst_col=3, start_row=2
    )

    # When no duplicates, original file is used directly
    assert total_rows == 3
    assert unique_rows == 3
    assert active_path == src_csv
    assert map_path == ""
