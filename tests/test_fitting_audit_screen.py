"""
tests/test_fitting_audit_screen.py — audit grid assembly (pure helpers).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.fitting.profiles import get_profile
from translator.tui.screens.audit import findings_notes, general_refusal

MV = get_profile("rpgm-mv-mz")

def test_notes_show_kind_and_severity():
    from translator.fitting import audit_rows
    # Unbreakable word to force an overflow finding
    rows = [(3, "oke", "a" * 60)]
    notes = findings_notes(audit_rows(rows, MV))
    assert 3 in notes
    assert "OVERFLOW" in notes[3] or "SINKING" in notes[3]

def test_general_refusal_message():
    msg = general_refusal()
    assert "engine" in msg.lower()
