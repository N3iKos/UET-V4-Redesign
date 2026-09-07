"""
tests/test_file_engine_screen.py — file selection and accept logic.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.tui.screens.file_engine import resolve_accept


def test_resolve_accept_rejects_missing_file(tmp_path):
    errors = []
    ok = resolve_accept(str(tmp_path / "nope.csv"), lambda p: errors.append(p))
    assert ok is False and errors == []


def test_resolve_accept_passes_back_path(tmp_path):
    f = tmp_path / "Dialog.csv"
    f.write_text("a,b\n1,2\n", encoding="utf-8")
    seen = []
    ok = resolve_accept(str(f), lambda p: seen.append(p))
    assert ok is True
    assert seen == [str(f)]
