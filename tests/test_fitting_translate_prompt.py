"""
tests/test_fitting_translate_prompt.py — soft budget injection into translate prompts.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.parsing.prompts import build_translate_prompt, build_translate_solo_prompt

ROWS = [(1, "Hello")]

def test_no_budget_unchanged():
    assert "Text fitting" not in build_translate_prompt(
        ROWS, "English", "Indonesian", "", prompt_format="plain")

def test_budget_present_in_prompt():
    out = build_translate_prompt(
        ROWS, "English", "Indonesian", "",
        fitting_budget="4 lines x 28.8 em", prompt_format="plain")
    assert "4 lines x 28.8 em" in out
    assert "NEVER sacrifice meaning" in out

def test_budget_forbids_self_linebreaks():
    out = build_translate_prompt(
        ROWS, "English", "Indonesian", "",
        fitting_budget="4 lines x 28.8 em", prompt_format="plain")
    assert "Do NOT insert line breaks" in out

def test_solo_prompt_gets_budget():
    out = build_translate_solo_prompt("Hello", "English", "Indonesian", "",
                                      fitting_budget="4 lines x 28.8 em")
    assert "4 lines x 28.8 em" in out
