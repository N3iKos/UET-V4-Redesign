"""
tests/test_fitting_judge_prompt.py — criterion wiring + per-row FITTING line.
"""

from __future__ import annotations

import os
import sys

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
