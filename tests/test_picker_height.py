"""
tests/test_fitting_picker_height.py — Picker max_visible clamps the box.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.tui.widgets.common import Option, Picker

def test_max_visible_clamps_height():
    picker = Picker(max_visible=5)
    picker.set_options([Option(f"f{i}", i) for i in range(40)])
    # get_content_height(container, viewport, width) — pass dummies; Picker
    # only uses the option count.
    assert picker.get_content_height(None, None, 80) == 5

def test_no_max_visible_keeps_auto():
    picker = Picker()
    picker.set_options([Option(f"f{i}", i) for i in range(9)])
    assert picker.get_content_height(None, None, 80) == 9

def test_fewer_options_shrinks():
    picker = Picker(max_visible=5)
    picker.set_options([Option("a", 1), Option("b", 2)])
    assert picker.get_content_height(None, None, 80) == 2
