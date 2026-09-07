"""
tests/test_judge_conciseness.py — conciseness & length fitting in JudgeRunner.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.judge.runner import JudgeRunner, JudgeTarget


def _runner(tmp_path):
    return JudgeRunner(
        JudgeTarget(path=str(tmp_path / "x.csv"), src_col=2, dst_col=3),
    )


def test_fitting_criteria_always_available(tmp_path):
    r = _runner(tmp_path)
    assert "fitting" in r.criteria


def test_fitting_map_calculates_conciseness(tmp_path):
    r = _runner(tmp_path)
    # Verbose line with >1.4x length expansion gets a conciseness note
    f_map = r._fitting_map([(1, "cat", "kucing yang sangat besar sekali dan luar biasa panjang")])
    assert 1 in f_map
    assert "CONCISENESS" in f_map[1]
    assert "3 chars" in f_map[1]


def test_fitting_map_ignores_concise_lines(tmp_path):
    r = _runner(tmp_path)
    # 1.1x ratio -> normal translation, no note needed
    f_map = r._fitting_map([(1, "hello world", "halo dunia!")])
    assert 1 not in f_map


def test_revalidate_pass(tmp_path):
    r = _runner(tmp_path)
    assert r.revalidate_fix(1, "Halo", "Hello") is None


def test_revalidate_detects_excessive_length(tmp_path):
    r = _runner(tmp_path)
    overlong = "teks perbaikan ini luar biasa panjang sekali dan sangat bertele-tele melebihi sumber aslinya sampai ratusan karakter panjangnya"
    assert r.revalidate_fix(1, overlong, "cat") == "excessive-length"
