"""
tests/test_fitting_wrap.py — greedy wrap, metrics, wrap suggestion.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.fitting.profiles import get_profile
from translator.fitting.wrap import suggest_wrap, wrap_lines, wrap_metrics

MV = get_profile("rpgm-mv-mz")

def test_wrap_short_text_one_line():
    assert wrap_lines("Halo", MV) == ["Halo"]

def test_wrap_respects_existing_segments():
    lines = wrap_lines("oke\nsip", MV)
    assert lines == ["oke", "sip"]

def test_wrap_breaks_on_spaces_only():
    text = "Kalimat yang cukup panjang sekali untuk dipaksa turun ke baris berikutnya di jendela"
    lines = wrap_lines(text, MV)
    assert len(lines) >= 2
    for line in lines:
        assert " " not in line.strip()[:1]  # lines start with words, not fragments

def test_wrap_never_splits_control_code():
    # Even an overlong line containing a code keeps the code intact on one line.
    text = r"teks \C[2]berwarna\C[0] sangat panjang sekali di sini memang benar"
    lines = wrap_lines(text, MV)
    assert any(r"\C[2]berwarna\C[0]" in line for line in lines)

def test_wrap_metrics_fit():
    m = wrap_metrics("Halo", MV)
    assert m.fits and m.max_ok
    assert m.lines == 1
    assert m.budget == MV.window_cols * MV.safe_margin

def test_wrap_metrics_overflow_flag():
    # A single unbreakable word wider than the budget forces an overflowing
    # line: "aaaa..." x 40 = 20 em is fine, but one word wider than 28.8 em
    # cannot be split (no spaces), so its line must flag overflow.
    wide_word = "a" * 60    # 60 latin chars * 0.5 = 30 em > 28.8 budget
    m = wrap_metrics(wide_word, MV)
    assert m.lines == 1
    assert m.overflow_lines == [0]
    assert not m.fits

def test_wrap_metrics_sinking():
    text = "\n".join("baris panjang yang pasti memakan dua baris render karena sangat panjang" for _ in range(4))
    m = wrap_metrics(text, MV)
    assert not m.max_ok   # 4 segments x 2 wrapped lines = 8 > 4

def test_suggest_wrap_no_tag_no_injection():
    # No textual tag in the source -> nothing insertable (spec 1.2b). The
    # suggestion stays the same text; shortening belongs to the judge pass.
    text = "Kalimat panjang yang harus turun ke baris kedua karena tidak muat di jendela"
    out = suggest_wrap(text, MV)
    assert out == text

def test_suggest_wrap_with_tag():
    text = r"Kalimat panjang\nyang harus turun ke baris kedua karena sangat tidak muat di jendela"
    out = suggest_wrap(text, MV)
    assert out.count(r"\n") >= 2   # re-flows using the tag it found in source
