"""
tests/test_wizard_multifile.py — Multi-file draft and target handling in Judge and Wizard.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.judge.runner import JudgeTarget
from translator.tui.screens.judge import _target_from, _targets_from


def test_single_target_fallback():
    draft = {
        "path": "test.csv",
        "src_col": 1,
        "dst_col": 2,
    }
    targets = _targets_from(draft)
    assert len(targets) == 1
    assert targets[0].path == "test.csv"


def test_multiple_targets_built_from_paths():
    draft = {
        "path": "test1.csv",
        "paths": ["test1.csv", "test2.csv", "test3.csv"],
        "src_col": 1,
        "dst_col": 2,
        "src_lang": "Japanese",
        "tgt_lang": "Indonesian",
    }
    targets = _targets_from(draft)
    assert len(targets) == 3
    assert [t.path for t in targets] == ["test1.csv", "test2.csv", "test3.csv"]
    assert all(t.src_col == 1 for t in targets)
    assert all(t.src_lang == "Japanese" for t in targets)


def test_build_jobs_from_translate_draft():
    from translator.tui.screens.wizard import build_jobs_from_draft
    draft = {
        "provider_id": "ag",
        "model_id": "gemini-3-flash",
        "src_paths": ["f1.csv", "f2.csv"],
        "src_path": "f1.csv",
        "src_col": 1,
        "dst_col": 2,
        "n_workers": 20,
    }
    jobs = build_jobs_from_draft(draft)
    assert len(jobs) == 2
    assert jobs[0].src_path == os.path.abspath("f1.csv")
    assert jobs[1].src_path == os.path.abspath("f2.csv")
    # Same model = automatically shared workers! 20 // 2 = 10 each
    assert jobs[0].n_workers == 10
    assert jobs[1].n_workers == 10

