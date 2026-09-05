"""
tests/test_file_engine_screen.py — engine list wiring + path accept logic.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.fitting.profiles import ENGINE_ORDER
from translator.tui.screens.file_engine import engine_options, resolve_accept

def test_engine_options_general_first():
    opts = engine_options()
    assert opts[0][0] == "general"
    assert [v for v, _l in opts] == list(ENGINE_ORDER)

def test_unverified_engines_marked():
    labels = {v: l for v, l in engine_options()}
    assert "(unverified)" in labels["rpgm-2000"]

def test_resolve_accept_rejects_missing_file(tmp_path):
    errors = []
    ok = resolve_accept(str(tmp_path / "nope.csv"), "general", lambda p, e: errors.append((p, e)))
    assert ok is False and errors == []   # rejected silently; caller shows the error

def test_resolve_accept_passes_back_path_and_engine(tmp_path):
    f = tmp_path / "Dialog.csv"
    f.write_text("a,b\n1,2\n", encoding="utf-8")
    seen = []
    ok = resolve_accept(str(f), "rpgm-mv-mz", lambda p, e: seen.append((p, e)))
    assert ok is True
    assert seen == [(str(f), "rpgm-mv-mz")]
