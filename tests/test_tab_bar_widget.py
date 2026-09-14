"""
tests/test_tab_bar_widget.py — FileTabBar widget state and rendering.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.tui.widgets.tab_bar import FileTabBar


def test_file_tab_bar_state():
    bar = FileTabBar(tabs=["Part01.csv", "Part02.csv"])
    assert bar.active_tab == 0
    assert len(bar.tabs) == 2
    bar.update_tab(0, progress_text="45%")
    assert "45%" in bar.get_tab_label(0)
    assert "Part01.csv" in bar.get_tab_label(0)

    bar.switch_tab(1)
    assert bar.active_tab == 1

    bar.update_tab(1, status="done")
    assert "✓" in bar.get_tab_label(1) or "done" in bar.get_tab_label(1).lower()


def test_four_unicode_tabs_fit_and_keep_fourth_clickable():
    bar = FileTabBar(tabs=[
        "第一章_非常に長いファイル名.csv",
        "第二章_非常に長いファイル名.csv",
        "第三章_非常に長いファイル名.csv",
        "第四章_非常に長いファイル名.csv",
    ])
    rendered = bar.render_for_width(48)
    assert len(bar._click_regions) == 4
    assert all(start < end <= 48 for start, end, _index in bar._click_regions)
    assert [index for _start, _end, index in bar._click_regions] == [0, 1, 2, 3]
    assert "1." in rendered.plain and "4." in rendered.plain
    assert "…" in rendered.plain


def test_file_tab_bar_cycling():
    bar = FileTabBar(tabs=["A.csv", "B.csv", "C.csv"])
    bar.next_tab()
    assert bar.active_tab == 1
    bar.next_tab()
    assert bar.active_tab == 2
    bar.next_tab()
    assert bar.active_tab == 0
    bar.prev_tab()
    assert bar.active_tab == 2
