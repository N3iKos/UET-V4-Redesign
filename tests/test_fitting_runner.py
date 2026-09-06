"""
tests/test_fitting_runner.py — machine flags, fix revalidation, token whitelist.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.fitting.profiles import get_profile
from translator.judge.runner import JudgeRunner

MV = get_profile("rpgm-mv-mz")

def _runner(tmp_path, engine="rpgm-mv-mz"):
    from translator.judge.runner import JudgeTarget
    return JudgeRunner(
        JudgeTarget(path=str(tmp_path / "x.csv"), src_col=2, dst_col=3),
        fitting_profile=engine,
    )

def test_general_profile_supports_fitting(tmp_path):
    r = _runner(tmp_path, engine="general")
    assert "fitting" in r.criteria
    # In general mode, verbose lines get a conciseness note
    f_map = r._fitting_map([(1, "cat", "kucing yang sangat besar sekali dan luar biasa panjang")])
    assert 1 in f_map
    assert "CONCISENESS" in f_map[1]

def test_mv_profile_keeps_fitting(tmp_path):
    r = _runner(tmp_path, engine="rpgm-mv-mz")
    assert "fitting" in r.criteria

def test_revalidate_pass(tmp_path):
    r = _runner(tmp_path)
    assert r.revalidate_fix(1, "Halo", "Halo") is None

def test_revalidate_detects_invented_token(tmp_path):
    r = _runner(tmp_path)
    # Source has no [br]; the fixed text invented one -> whitelist rejects.
    assert r.revalidate_fix(1, "Halo[br]Dunia", "Halo Dunia") == "invented-token"

def test_revalidate_detects_sinking(tmp_path):
    r = _runner(tmp_path)
    long_text = "\n".join("kata " * 20 for _ in range(4))
    assert r.revalidate_fix(1, long_text, "oke") == "sinking"

def test_revalidate_allows_source_tag(tmp_path):
    r = _runner(tmp_path)
    # Source itself uses \n: the fix may re-use it.
    assert r.revalidate_fix(1, "Halo\\nDunia", "Halo\\nDunia") is None
