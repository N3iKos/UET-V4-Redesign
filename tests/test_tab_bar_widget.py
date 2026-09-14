"""
tests/test_tab_bar_widget.py — FileTabBar widget state and rendering.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from textual.app import App, ComposeResult

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


def test_tab_bar_width_smaller_than_minimum_keeps_regions_inside_width():
    bar = FileTabBar(tabs=["alpha.csv", "beta.csv", "gamma.csv", "delta.csv"])
    rendered = bar.render_for_width(12)
    assert rendered.cell_len <= 12
    assert len(bar._click_regions) == 4
    assert all(0 <= start < end <= 12 for start, end, _index in bar._click_regions)
    assert all(f"{index}." in rendered.plain for index in range(1, 5))


@pytest.mark.anyio
async def test_mounted_unicode_tab_four_clicks_after_resize():
    switched = []
    class TabApp(App):
        def compose(self) -> ComposeResult:
            yield FileTabBar(
                tabs=[f"第{i}章_非常に長い名前.csv" for i in range(1, 5)],
                on_tab_switch=switched.append,
                id="tabs",
            )

    app = TabApp()
    async with app.run_test(size=(40, 8)) as pilot:
        bar = app.query_one("#tabs", FileTabBar)
        await pilot.pause()
        bar.render()
        start, end, index = bar._click_regions[3]
        assert index == 3 and end <= bar.size.width
        await pilot.click("#tabs", offset=((start + end - 1) // 2, 0))
        await pilot.resize_terminal(28, 8)
        await pilot.pause()
        bar.render()
        assert bar.active_tab == 3
        assert bar._click_regions[-1][1] <= bar.size.width
        assert switched == [3]


def test_tab_bar_middle_ellipsize_keeps_progress_visible():
    bar = FileTabBar(tabs=[
        "Forestiaa_part08_Translated.csv",
        "Forestiaa_part09_Translated.csv",
        "Forestiaa_part10_Translated.csv",
        "Forestiaa_part11_Translated.csv",
    ])
    bar.update_tab(0, progress_text="10%")
    bar.update_tab(1, progress_text="11%")
    bar.update_tab(2, progress_text="7%")
    bar.update_tab(3, progress_text="7%")

    # Render for width 100 (25 cells per tab)
    rendered = bar.render_for_width(100)
    plain = rendered.plain
    # All 4 tabs must keep their progress and tab numbers visible!
    assert "1." in plain and "10%" in plain
    assert "2." in plain and "11%" in plain
    assert "3." in plain and "7%" in plain
    assert "4." in plain and "7%" in plain
    # Filename in the middle must be middle-ellipsized with '...'
    assert "..." in plain or "…" in plain

