"""
tests/test_fitting_apply.py — audit write-back: backup, column, round trip.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.fitting.apply import apply_wrapped, find_translated_col

_HEADER = "No,Source_File,ID,String,Original_text,Translated\n"

def _make_csv(tmp_path, body):
    csv = tmp_path / "Dialog_Translated.csv"
    csv.write_text(_HEADER + body, encoding="utf-8")
    return str(csv)

def test_find_translated_col():
    assert find_translated_col("No,Source_File,Original_text,Translated".split(",")) == 3
    assert find_translated_col("a,b".split(",")) is None

def test_apply_writes_and_backs_up(tmp_path):
    path = _make_csv(tmp_path, "1,x,y,z,Hello,\n")
    before = open(path, encoding="utf-8").read()
    n = apply_wrapped(path, {2: "Halo"})
    assert n == 1
    after = open(path, encoding="utf-8").read()
    assert "Halo" in after
    assert after != before
    bak = os.path.splitext(path)[0] + ".bak"
    assert os.path.exists(bak)
    assert open(bak, encoding="utf-8").read() == before

def test_apply_second_run_no_double_backup(tmp_path):
    path = _make_csv(tmp_path, "1,x,y,z,Hello,\n")
    apply_wrapped(path, {2: "Halo"})
    bak = os.path.splitext(path)[0] + ".bak"
    first = open(bak, encoding="utf-8").read()
    apply_wrapped(path, {2: "Halo dunia"})
    assert open(bak, encoding="utf-8").read() == first   # original kept, not overwritten

def test_apply_skips_missing_rows(tmp_path):
    path = _make_csv(tmp_path, "1,x,y,z,Hello,\n")
    assert apply_wrapped(path, {99: "nope"}) == 0
