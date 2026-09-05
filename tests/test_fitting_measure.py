"""
tests/test_fitting_measure.py — token stripping, em measurement, tag detection.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.fitting.profiles import get_profile
from translator.fitting.measure import (
    char_width, detect_newline_tag, measure_lines, strip_tokens,
)

MV = get_profile("rpgm-mv-mz")

def test_strip_removes_rpgm_codes():
    raw = r"\C[2]Halo, \V[5]koin\C[0]!"
    assert strip_tokens(raw, MV) == "Halo, koin!"

def test_strip_keeps_normal_text():
    assert strip_tokens("Biasa saja", MV) == "Biasa saja"

def test_measure_per_segment():
    # One newline = two segments; each measured separately (spec 1.2).
    lines = measure_lines("oke\nsip", MV)
    assert len(lines) == 2
    assert lines[0] > 0 and lines[1] > 0

def test_measure_cjk_wider_than_latin():
    assert char_width("月", MV.char_widths) > char_width("a", MV.char_widths)

def test_measure_kirikiri_tags():
    KK = get_profile("kirikiri")
    assert strip_tokens("Hehehe... [w]Good morning.[p]", KK) == "Hehehe... Good morning."

def test_detect_newline_tag_backslash_n():
    assert detect_newline_tag(r"Halo\nDunia") == r"\n"

def test_detect_newline_tag_brackets():
    assert detect_newline_tag("Halo[r]Dunia") == "[r]"
    assert detect_newline_tag("Halo[br]Dunia") == "[br]"
    assert detect_newline_tag("Halo[BR]Dunia") == "[BR]"

def test_detect_newline_tag_priority():
    # backslash-n wins over a later [r] (first priority in the order).
    assert detect_newline_tag(r"a\nb[r]c") == r"\n"

def test_detect_newline_tag_none_when_absent():
    assert detect_newline_tag("Halo Dunia") is None

def test_detect_newline_tag_real_newline_char_is_not_a_tag():
    # A literal 0x0A in the cell is a *segment separator*, not an insertable
    # tag (spec 1.2b): the model cannot emit it through the [N] protocol.
    assert detect_newline_tag("Halo\nDunia") is None

def test_detect_newline_tag_backslash_n_beats_bracket():
    # backslash-n is text; a 0x0A before it does not make it absent.
    assert detect_newline_tag("Halo\nDunia\\nAkhir") == r"\n"
