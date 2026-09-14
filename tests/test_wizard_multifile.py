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


def test_wizard_screen_tab_bar_composition():
    from translator.tui.screens.wizard import ColumnsScreen
    screen = ColumnsScreen()
    # Mock draft via _test_draft
    screen._test_draft = {
        "src_paths": ["f1.csv", "f2.csv"],
        "src_path": "f1.csv",
    }
    assert hasattr(screen, "_compose_tab_bar")
    tab_bar = screen._compose_tab_bar()
    assert tab_bar is not None
    assert len(tab_bar.tabs) == 2
    assert "f1.csv" in tab_bar.tabs[0]
    assert "Leader" in tab_bar.tabs[0]
    assert "f2.csv" in tab_bar.tabs[1]


def test_judge_wizard_screen_tab_bar_composition():
    from translator.tui.screens.judge import JudgeColumnsScreen
    screen = JudgeColumnsScreen()
    screen._test_draft = {
        "paths": ["test1.csv", "test2.csv"],
        "path": "test1.csv",
    }
    assert hasattr(screen, "_compose_tab_bar")
    tab_bar = screen._compose_tab_bar()
    assert tab_bar is not None
    assert len(tab_bar.tabs) == 2
    assert "test1.csv" in tab_bar.tabs[0]
    assert "Leader" in tab_bar.tabs[0]
    assert "test2.csv" in tab_bar.tabs[1]


def test_targets_from_does_not_leak_map_path_to_unmapped_files():
    # File 1 has deduplication mapping, File 2 has NO duplicates (no mapping)
    draft = {
        "path": "file1.csv",
        "paths": ["file1.csv", "file2.csv"],
        "src_col": 1,
        "dst_col": 2,
        "map_path": "/cache/file1.map.json",
        "multi_targets_info": {
            os.path.abspath("file1.csv"): {
                "full_path": os.path.abspath("file1.csv"),
                "path": "/cache/file1.unique.csv",
                "map_path": "/cache/file1.map.json",
            }
            # Notice file2.csv has NO map_path because it has 0 duplicates
        }
    }
    targets = _targets_from(draft)
    assert len(targets) == 2
    # File 1 has map_path
    assert targets[0].map_path == "/cache/file1.map.json"
    # File 2 MUST NOT inherit File 1's map_path! It must have empty map_path!
    assert targets[1].map_path == ""
    assert targets[1].path == "file2.csv"





