from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from translator.io.excel_io import ExcelReader
from translator.tui.screens.judge import _targets_from
from translator.tui.screens.wizard import build_jobs_from_draft


def _make_csv(path: Path, num_rows: int) -> str:
    lines = ["id,src,dst"]
    for i in range(1, num_rows + 1):
        lines.append(f"{i},text_{i},")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def test_excel_reader_load_column_zero_means_all_rows(tmp_path):
    csv_file = tmp_path / "sample.csv"
    _make_csv(csv_file, 8)

    # 1 header + 8 data rows = 9 rows total
    total_rows = ExcelReader.row_count(str(csv_file))
    assert total_rows == 9

    # load_column with num_rows=0 starting at row 2 must return all 8 data rows
    rows = ExcelReader.load_column(str(csv_file), col_idx=2, start_row=2, num_rows=0)
    assert len(rows) == 8
    assert rows[0] == (2, "text_1")
    assert rows[-1] == (9, "text_8")


@pytest.mark.parametrize(
    "file_order",
    [
        ("small", "medium", "large"),
        ("large", "small", "medium"),
        ("medium", "large", "small"),
    ],
)
def test_multifile_unlimited_row_limit_all_orders(tmp_path, file_order):
    """
    BUG 2 Test: row_limit = 0 must process ALL rows of each file independently,
    regardless of which file is first/leader.
    small = 3 rows, medium = 7 rows, large = 15 rows.
    Header = 1 row, so total data rows starting at row 2:
    small: 3, medium: 7, large: 15.
    """
    files = {
        "small": _make_csv(tmp_path / "small.csv", 3),
        "medium": _make_csv(tmp_path / "medium.csv", 7),
        "large": _make_csv(tmp_path / "large.csv", 15),
    }

    ordered_paths = [files[k] for k in file_order]

    draft = {
        "src_path": ordered_paths[0],
        "src_paths": ordered_paths,
        "src_col": 2,
        "dst_col": 3,
        "start_row": 2,
        "user_row_limit": 0,
        "num_rows": 0,
    }

    jobs = build_jobs_from_draft(draft)
    assert len(jobs) == 3

    job_rows_by_file = {
        os.path.basename(j.src_path): j.num_rows for j in jobs
    }

    assert job_rows_by_file["small.csv"] == 3
    assert job_rows_by_file["medium.csv"] == 7
    assert job_rows_by_file["large.csv"] == 15


@pytest.mark.parametrize(
    "file_order",
    [
        ("small", "medium", "large"),
        ("large", "small", "medium"),
        ("medium", "large", "small"),
    ],
)
def test_multifile_explicit_row_limit_all_orders(tmp_path, file_order):
    """
    BUG 2 Test: row_limit = 5 must process min(actual_rows, 5) for each file.
    small (3 rows) -> 3
    medium (7 rows) -> 5
    large (15 rows) -> 5
    Regardless of which file is first/leader!
    """
    files = {
        "small": _make_csv(tmp_path / "small.csv", 3),
        "medium": _make_csv(tmp_path / "medium.csv", 7),
        "large": _make_csv(tmp_path / "large.csv", 15),
    }

    ordered_paths = [files[k] for k in file_order]

    draft = {
        "src_path": ordered_paths[0],
        "src_paths": ordered_paths,
        "src_col": 2,
        "dst_col": 3,
        "start_row": 2,
        "user_row_limit": 5,
        "num_rows": 5,
    }

    jobs = build_jobs_from_draft(draft)
    assert len(jobs) == 3

    job_rows_by_file = {
        os.path.basename(j.src_path): j.num_rows for j in jobs
    }

    assert job_rows_by_file["small.csv"] == 3
    assert job_rows_by_file["medium.csv"] == 5
    assert job_rows_by_file["large.csv"] == 5


def test_judge_targets_from_respects_per_file_row_limits(tmp_path):
    """Ensure JudgeTarget also processes all rows per file independently."""
    small = _make_csv(tmp_path / "small.csv", 3)
    large = _make_csv(tmp_path / "large.csv", 15)

    draft = {
        "path": small,
        "paths": [small, large],
        "src_col": 2,
        "dst_col": 3,
        "start_row": 2,
        "user_row_limit": 0,
        "num_rows": 0,
    }

    targets = _targets_from(draft)
    assert len(targets) == 2
    assert targets[0].num_rows == 3
    assert targets[1].num_rows == 15


@pytest.mark.anyio
async def test_wizard_columns_screen_defaults_rows_to_zero_for_multifile(tmp_path):
    from textual.app import App
    from translator.tui.screens.wizard import ColumnsScreen
    small = _make_csv(tmp_path / "small.csv", 3)
    large = _make_csv(tmp_path / "large.csv", 15)

    app = App()
    async with app.run_test() as pilot:
        screen = ColumnsScreen()
        screen._test_draft = {
            "src_path": small,
            "src_paths": [small, large],
            "num_rows": 3,  # previous leader row count
            "user_row_limit": 0,
        }
        await app.push_screen(screen)
        screen._restore_fields()
        assert screen._rows.value == "0"


@pytest.mark.anyio
async def test_judge_columns_screen_defaults_rows_to_zero_for_multifile(tmp_path):
    from textual.app import App
    from translator.tui.screens.judge import JudgeColumnsScreen
    small = _make_csv(tmp_path / "small.csv", 3)
    large = _make_csv(tmp_path / "large.csv", 15)

    app = App()
    async with app.run_test() as pilot:
        screen = JudgeColumnsScreen()
        screen._test_draft = {
            "path": small,
            "paths": [small, large],
            "num_rows": 3,
            "user_row_limit": 0,
        }
        await app.push_screen(screen)
        assert screen._rows.value == "0"


