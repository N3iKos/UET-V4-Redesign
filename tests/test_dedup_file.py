import json
from pathlib import Path

import pytest
from openpyxl import Workbook

from translator.io import csv_io
from translator.io.excel_io import load_workbook


def api():
    from translator.core import dedup_file
    return dedup_file


def make_file(path, rows):
    if path.suffix == ".xlsx":
        wb = Workbook()
        for row in rows:
            wb.active.append(row)
        wb.save(path)
        wb.close()
    else:
        csv_io.write_rows(str(path), rows, csv_io.CsvFormat(bom=True))
    return str(path)


def values(path):
    wb = load_workbook(str(path), data_only=False)
    try:
        return list(wb.active.iter_rows(values_only=True))
    finally:
        wb.close()


@pytest.mark.parametrize("ext", [".csv", ".xlsx"])
def test_prepare_and_sync_preserve_rows_columns_and_prefix(tmp_path, ext):
    source = make_file(tmp_path / ("dialog" + ext), [
        ["source", "target", "id"], ["prefix", "keep", "0"],
        ["Hello", "", "1"], ["", "untouched", "2"],
        ["World", "", "3"], ["Hello", "", "4"],
    ])
    unique, mapping, total, count = api().prepare_unique_file(source, 1, 2, 3)
    assert (total, count) == (3, 2)
    data = json.loads(Path(mapping).read_text())
    assert data["rep_to_full"] == {"3": [3, 6], "4": [5]}
    assert values(unique)[:2] == values(source)[:2]
    wb = load_workbook(unique, data_only=False)
    wb.active.cell(3, 2, "Halo")
    wb.active.cell(4, 2, "Dunia")
    output = str(Path(unique).with_name(Path(unique).stem + "_Translated" + ext))
    wb.save(output)
    wb.close()
    full = str(tmp_path / ("full" + ext))
    assert api().sync_unique_to_full(mapping, output, full, 2) == 3
    rows = values(full)
    assert [r[1] for r in rows] == ["target", "keep", "Halo", "untouched", "Dunia", "Halo"]
    assert [r[2] for r in rows] == ["id", "0", "1", "2", "3", "4"]
    assert values(source)[2][1] in (None, "")


def test_no_duplicates_and_blank_rows_return_original(tmp_path):
    source = make_file(tmp_path / "plain.csv", [["s", "t"], ["a", ""], [], ["b", ""]])
    assert api().prepare_unique_file(source, 1, 2) == (source, "", 2, 2)


def test_cache_reuse_and_stale_mapping_rejected(tmp_path):
    source = make_file(tmp_path / "dialog.csv", [["s", "t"], ["a", ""], ["a", ""]])
    first = api().prepare_unique_file(source, 1, 2)
    assert api().prepare_unique_file(source, 1, 2) == first
    before = Path(first[0]).read_bytes()
    make_file(Path(source), [["s", "t"], ["b", ""], ["b", ""]])
    second = api().prepare_unique_file(source, 1, 2)
    assert second[0] != first[0]
    assert Path(first[0]).read_bytes() == before
    with pytest.raises(ValueError, match="(?i)stale|mapping"):
        api().sync_unique_to_full(first[1], first[0], str(tmp_path / "full.csv"), 2)
    assert not (tmp_path / "full.csv").exists()


def test_sync_validates_unique_order_and_mapping_indices(tmp_path):
    source = make_file(tmp_path / "dialog.csv", [["s", "t"], ["a", ""], ["b", ""], ["a", ""]])
    unique, mapping, _, _ = api().prepare_unique_file(source, 1, 2)
    make_file(Path(unique), [["s", "t"], ["b", "B"], ["a", "A"]])
    with pytest.raises(ValueError):
        api().sync_unique_to_full(mapping, unique, str(tmp_path / "full.csv"), 2)


def test_preparation_resumes_full_output_without_overwriting_completed_cells(tmp_path):
    source = make_file(tmp_path / "dialog.csv", [["s", "t"], ["a", ""], ["a", ""], ["b", ""]])
    full = make_file(tmp_path / "dialog_Translated.csv", [["s", "t"], ["a", ""], ["a", "Approved"], ["b", "Done"]])
    unique, mapping, _, _ = api().prepare_unique_file(source, 1, 2, full_out_path=full)
    assert values(unique)[1][1] == "Approved"
    api().sync_unique_to_full(mapping, unique, full, 2)
    assert [r[1] for r in values(full)[1:]] == ["Approved", "Approved", "Done"]


def test_range_does_not_broadcast_outside_selected_rows(tmp_path):
    source = make_file(tmp_path / "dialog.csv", [["s", "t"], ["a", ""], ["a", ""], ["a", "outside"]])
    unique, mapping, total, count = api().prepare_unique_file(source, 1, 2, num_rows=2)
    assert (total, count) == (2, 1)
    wb = load_workbook(unique)
    wb.active.cell(2, 2, "A")
    wb.save(unique)
    wb.close()
    full = str(tmp_path / "full.csv")
    api().sync_unique_to_full(mapping, unique, full, 2)
    assert values(full)[3][1] == "outside"


def test_sync_failure_is_atomic(tmp_path, monkeypatch):
    source = make_file(tmp_path / "dialog.csv", [["s", "t"], ["a", "A"], ["a", ""]])
    unique, mapping, _, _ = api().prepare_unique_file(source, 1, 2)
    full = make_file(tmp_path / "full.csv", [["s", "t"], ["a", "A"], ["a", ""]])
    before = Path(full).read_bytes()
    def fail(*args):
        raise PermissionError("locked")
    monkeypatch.setattr(api().os, "replace", fail)
    with pytest.raises(PermissionError):
        api().sync_unique_to_full(mapping, unique, full, 2)
    assert Path(full).read_bytes() == before
