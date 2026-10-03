"""
tests/test_fitting_judge_prompt.py — criterion wiring + per-row FITTING line.
"""

from __future__ import annotations

import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.judge.models import CRITERIA, CRITERION_LABELS
from translator.judge.prompts import (
    build_fix_prompt, build_fix_solo_prompt, build_judge_prompt,
)

ROWS = [(4, "Halo", "Hello world")]

def test_fitting_is_fifth_criterion():
    assert "fitting" in CRITERIA
    assert "fitting" in CRITERION_LABELS

def test_judge_prompt_contains_fitting_line():
    out = build_judge_prompt(
        rows=ROWS, criteria=["fitting"],
        fitting_map={4: "source 1 lines/10% · translation 5 lines/131% · OVERFLOW"},
    )
    assert "FITTING     :" in out
    assert "131%" in out
    assert "OVERFLOW" in out

def test_judge_prompt_without_map_has_no_fitting_noise():
    out = build_judge_prompt(rows=ROWS, criteria=["fitting"])
    assert "FITTING     :" not in out

def test_judge_prompt_fitting_rule_mentions_tags():
    out = build_judge_prompt(rows=ROWS, criteria=["fitting"],
                             fitting_map={4: "x · line-break tag in source: [r]"})
    assert "line-break tag" in out

def test_fix_prompt_carries_fitting_block():
    out = build_fix_prompt(
        rows=ROWS, criteria=["fitting"],
        fitting_map={4: "source 1 lines/10% · translation 5 lines/131% · SINKING · line-break tag in source: [r]"},
    )
    assert "FITTING" in out and "SINKING" in out
    assert "must not exceed" in out

def test_fix_solo_prompt_carries_fitting():
    out = build_fix_solo_prompt(4, "Halo", "Hello world",
                                fitting="5 lines/131% · SINKING · tag: [r]")
    assert "FITTING" in out


def test_fix_prompt_preserves_intentional_glitch_text():
    out = build_fix_prompt(
        rows=[(824, "g l i t c h\\n  t e x t", "[TRANSLATE_FAILED]")],
        comments={824: "Translation failed."},
        criteria=["failed"],
    )
    assert "intentional glitch" in out.lower()
    assert "spacing" in out.lower()
    assert "\\n" in out
    assert "do not sanitize" in out.lower() or "do not normalize" in out.lower() or "do not normalise" in out.lower()


def test_judge_prompt_strict_fitting_rule():
    out = build_judge_prompt(
        rows=ROWS, criteria=["fitting"], fitting_mode="strict", fitting_tolerance=3,
        fitting_map={4: "source 3 chars · translation 8 chars · STRICT_1WORD_LIMIT"},
    )
    assert "STRICT / UI COMPACT" in out
    assert "+3" in out
    assert "1-WORD" in out
    assert "Sunset" in out
    assert "Japanese" in out


def test_fix_prompt_strict_fitting_rule():
    out = build_fix_prompt(
        rows=ROWS, criteria=["fitting"],
        fitting_map={4: "source 3 chars · translation 8 chars · STRICT_1WORD_LIMIT"},
        fitting_mode="strict", fitting_tolerance=3,
    )
    assert "STRICT" in out
    assert "+3" in out
    assert "Sunset" in out


def test_fix_solo_prompt_strict_fitting_rule():
    out = build_fix_solo_prompt(
        4, "Sunset", "Matahari terbenam",
        fitting="source 6 chars (1-word) · translation 17 chars · STRICT_1WORD_LIMIT",
        fitting_tolerance=3,
    )
    assert "STRICT" in out
    assert "+3" in out


def test_settings_fitting_tolerance_and_mode(monkeypatch, tmp_path):
    monkeypatch.setattr("translator.data.app_prefs._prefs_dir", lambda: tmp_path)
    from translator.data.app_prefs import (
        fitting_tolerance, set_fitting_tolerance,
        last_judge_fitting_mode, set_last_judge_fitting_mode,
    )
    assert fitting_tolerance() == 3
    assert last_judge_fitting_mode() == "normal"

    set_fitting_tolerance(5)
    assert fitting_tolerance() == 5

    set_last_judge_fitting_mode("strict")
    assert last_judge_fitting_mode() == "strict"


@pytest.mark.anyio
async def test_judge_criteria_screen_mutual_exclusion(monkeypatch, tmp_path):
    monkeypatch.setattr("translator.data.app_prefs._prefs_dir", lambda: tmp_path)
    from textual.app import App
    from translator.tui.screens.judge import JudgeCriteriaScreen

    app = App()
    async with app.run_test() as pilot:
        screen = JudgeCriteriaScreen()
        screen._test_draft = {"criteria": ["mistranslation", "fitting"], "fitting_mode": "normal"}
        await app.push_screen(screen)
        screen.on_screen_ready()

        opt_normal = next(o for o in screen.picker.options if o.value == "fitting")
        opt_strict = next(o for o in screen.picker.options if o.value == "fitting_strict")

        assert opt_normal.selected is True
        assert opt_strict.selected is False

        # Toggle strict on
        opt_strict.selected = True
        from translator.tui.widgets.common import Picker
        screen.on_picker_toggled(Picker.Toggled(screen.picker, opt_strict))

        # opt_normal must be automatically unselected!
        assert opt_strict.selected is True
        assert opt_normal.selected is False

        # Toggle normal on
        opt_normal.selected = True
        screen.on_picker_toggled(Picker.Toggled(screen.picker, opt_normal))

        # opt_strict must be automatically unselected!
        assert opt_normal.selected is True
        assert opt_strict.selected is False

