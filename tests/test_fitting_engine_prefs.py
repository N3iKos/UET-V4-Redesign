"""
tests/test_fitting_engine_prefs.py — engine-per-file memory (LRU 50).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from translator import config
from translator.data import app_prefs

@pytest.fixture(autouse=True)
def scratch_prefs(tmp_path, monkeypatch):
    scratch = tmp_path / "app_prefs.json"
    monkeypatch.setattr(config, "APP_PREFS_FILE", str(scratch))
    monkeypatch.setattr(app_prefs.config, "APP_PREFS_FILE", str(scratch))
    yield scratch

def test_unknown_file_defaults_general():
    assert app_prefs.remembered_engine("D:/x/Dialog.csv") == "general"

def test_remember_and_recall():
    app_prefs.remember_engine("D:/x/Dialog.csv", "rpgm-mv-mz")
    assert app_prefs.remembered_engine("D:/x/Dialog.csv") == "rpgm-mv-mz"

def test_lru_cap_50():
    for i in range(60):
        app_prefs.remember_engine(f"D:/g{i}/file.csv", "kirikiri")
    assert app_prefs.remembered_engine("D:/g0/file.csv") == "general"   # evicted
    assert app_prefs.remembered_engine("D:/g59/file.csv") == "kirikiri"

def test_only_real_engines_stored():
    app_prefs.remember_engine("D:/x/Unique-madeup.csv", "made-up-engine")
    assert app_prefs.remembered_engine("D:/x/Unique-madeup.csv") == "general"
