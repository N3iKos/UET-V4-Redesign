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


def test_file_engine_multiple_selection(tmp_path):
    f1 = tmp_path / "f1.csv"
    f2 = tmp_path / "f2.csv"
    f3 = tmp_path / "f3.csv"
    f1.write_text("a,b\n1,2\n", encoding="utf-8")
    f2.write_text("a,b\n3,4\n", encoding="utf-8")
    f3.write_text("a,b\n5,6\n", encoding="utf-8")

    from translator.tui.screens.file_engine import FileEngineScreen
    selected = []
    screen = FileEngineScreen(
        on_accept=lambda p: None,
        on_accept_multiple=lambda paths: selected.extend(paths),
        max_files=4,
        next_screen=None,
    )
    assert hasattr(screen, "toggle_selection")
    screen.toggle_selection(str(f1))
    screen.toggle_selection(str(f2))
    assert screen.selected_files == [str(f1), str(f2)]
    # Toggling an already selected file unselects it
    screen.toggle_selection(str(f2))
    assert screen.selected_files == [str(f1)]
    screen.toggle_selection(str(f2))
    screen.toggle_selection(str(f3))
    assert screen.selected_files == [str(f1), str(f2), str(f3)]
    screen.confirm_selection()
    assert selected == [str(f1), str(f2), str(f3)]


def test_file_engine_custom_manual_paths(tmp_path):
    f1 = tmp_path / "custom1.csv"
    f2 = tmp_path / "custom2.csv"
    f1.write_text("a,b\n1,2\n", encoding="utf-8")
    f2.write_text("a,b\n3,4\n", encoding="utf-8")

    from translator.tui.screens.file_engine import FileEngineScreen
    selected = []
    screen = FileEngineScreen(
        on_accept=lambda p: None,
        on_accept_multiple=lambda paths: selected.extend(paths),
        max_files=4,
        next_screen=None,
    )
    # User types custom multiple paths separated by comma
    manual_input = f'"{f1}", "{f2}"'
    assert hasattr(screen, "parse_manual_paths")
    paths = screen.parse_manual_paths(manual_input)
    assert paths == [str(f1), str(f2)]


def test_file_engine_ignores_cache_path(tmp_path):
    from translator.tui.screens.file_engine import FileEngineScreen
    cache_dir = tmp_path / ".uet_cache"
    cache_dir.mkdir()
    cache_file = cache_dir / "test.unique.csv"
    cache_file.write_text("id,src\n1,cat\n", encoding="utf-8")
    valid_file = tmp_path / "valid.csv"
    valid_file.write_text("id,src\n1,cat\n", encoding="utf-8")

    screen = FileEngineScreen(
        on_accept=lambda p: None,
        existing_path=str(cache_file),
        next_screen=None,
    )
    assert screen._existing == ""
    assert screen.selected_files == []

    # parse_manual_paths should reject cache_file
    parsed = screen.parse_manual_paths(f"{cache_file}, {valid_file}")
    assert parsed == [str(valid_file)]




