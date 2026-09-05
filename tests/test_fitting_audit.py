"""
tests/test_fitting_audit.py — findings, severity anchors, prompt line.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.fitting.profiles import get_profile
from translator.fitting.audit import audit_rows, fitting_line, is_ratio_flag

MV = get_profile("rpgm-mv-mz")

def _long_id(n=40):
    return "kata " * n   # ~200em

def _unbreakable(n=60):
    # One word wider than the budget: cannot be split (no spaces), so its
    # single line must overflow the right edge.
    return "a" * n

def test_clean_rows_produce_no_findings():
    findings = audit_rows([(1, "oke", "sip")], MV)
    assert findings == []

def test_overflow_flagged_moderate():
    rows = [(5, "oke", _unbreakable())]
    f = audit_rows(rows, MV)[0]
    assert "overflow" in f.kinds
    assert f.severity == "moderate"

def test_sinking_is_critical():
    target = "\n".join(_long_id() for _ in range(4))   # wraps to > 4 lines
    f = audit_rows([(7, "oke", target)], MV)[0]
    assert "sinking" in f.kinds
    assert f.severity == "critical"

def test_ratio_uses_threshold_not_flat():
    # A JP source expanding to 150% must NOT flag (under 1.8); 200% must.
    src = "月" * 10                      # 10 em
    mild = "a" * 30                      # 30 * 0.5 = 15 em -> ratio 1.5
    heavy = "a" * 42                     # 21 em -> ratio 2.1
    assert audit_rows([(1, src, mild)], MV) == []
    assert is_ratio_flag(audit_rows([(2, src, heavy)], MV)[0])

def test_empty_translation_skipped():
    assert audit_rows([(3, "oke", "")], MV) == []

def test_newline_tag_recorded():
    f = audit_rows([(9, r"a\nb", _long_id())], MV)[0]
    assert f.newline_tag == r"\n"

def test_fitting_line_shape():
    f = audit_rows([(9, r"a\nb", _unbreakable())], MV)[0]
    line = fitting_line(f)
    assert "OVERFLOW" in line
    assert "lines" in line and "tag" in line

def test_empty_source_not_ratio_flagged():
    # Empty source + fitting translation: ratio would degenerate to
    # target/1.0em and flag a healthy line LONG (QA bug). Row is skipped.
    assert audit_rows([(1, "", "Halo dunia ini adalah terjemahan yang cukup panjang sekali")], MV) == []
