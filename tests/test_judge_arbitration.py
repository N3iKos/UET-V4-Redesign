"""
tests/test_judge_arbitration.py — Tests for Senior Linguistic Arbiter system prompt,
context-aware re-judge with prior findings, and toggleable history settings.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator import config
from translator.data import app_prefs
from translator.judge.models import RowIssue
from translator.judge.prompts import (
    build_judge_prompt,
    build_judge_single_prompt,
    build_judge_rechain_prompt,
    _JUDGE_SYS,
    _JUDGE_SYS_CHAIN,
)
from translator.judge.runner import JudgeRunner, JudgeTarget


def test_system_prompt_contains_arbiter_principles():
    for sys_prompt in (_JUDGE_SYS, _JUDGE_SYS_CHAIN):
        assert "senior translation arbiter" in sys_prompt.lower()
        assert "PRESUMPTION OF ADEQUACY" in sys_prompt
        assert "SYNONYM AND REGISTER TOLERANCE" in sys_prompt
        assert "ARBITRATION OF PREVIOUS FINDINGS" in sys_prompt
        assert "OBJECTIVE ERROR THRESHOLD" in sys_prompt
        assert "circular critique" in sys_prompt.lower()


def test_build_judge_prompt_includes_previous_finding():
    rows = [(102, "あんまり…焦らさんといて…っ♡", "Jangan... bikin aku makin gemes...♡")]
    history_map = {
        102: "WHAT: SOURCE says '焦らさんといて' but TRANSLATION says 'gemes' · WHY: should be teasing · FIX: Gunakan 'Jangan goda aku...'"
    }
    prompt = build_judge_prompt(
        rows=rows,
        criteria=["mistranslation"],
        src_lang="Japanese",
        tgt_lang="Indonesian",
        history_map=history_map,
    )
    assert "[102]" in prompt
    assert "SOURCE      : あんまり…焦らさんといて…っ♡" in prompt
    assert "PREVIOUS FINDING : WHAT: SOURCE says" in prompt
    assert "TRANSLATION : Jangan... bikin aku makin gemes...♡" in prompt


def test_build_judge_single_prompt_includes_history():
    prompt = build_judge_single_prompt(
        row=2266,
        source="あんまり…焦らさんといて…っ♡",
        translation="Jangan goda aku...♡",
        criteria=["mistranslation"],
        history="WHAT: SOURCE says '焦らさんといて' but TRANSLATION says 'gemes' · FIX: Gunakan 'Jangan goda aku...'",
    )
    assert "[2266]" in prompt
    assert "PREVIOUS FINDING : WHAT: SOURCE says" in prompt
    assert "Gunakan 'Jangan goda aku...'" in prompt
    assert "TRANSLATION : Jangan goda aku...♡" in prompt


def test_build_judge_rechain_prompt_includes_history_for_reviewed_row():
    rows = [(2266, "あんまり…焦らさんといて…っ♡", "Jangan goda aku...♡")]
    neighbours = {2265: ("前", "Sebelumnya"), 2267: ("次", "Berikutnya")}
    history_map = {
        2266: "WHAT: previous mistake note · FIX: consider X"
    }
    prompt = build_judge_rechain_prompt(
        rows=rows,
        neighbours=neighbours,
        criteria=["mistranslation"],
        history_map=history_map,
    )
    assert "[2266] — UNDER REVIEW" in prompt
    assert "PREVIOUS FINDING : WHAT: previous mistake note" in prompt
    # Neighbours must not have PREVIOUS FINDING
    assert "[2265] — CONTEXT ONLY" in prompt
    assert "Sebelumnya" in prompt


def test_context_aware_rejudge_toggle_setting(tmp_path, monkeypatch):
    scratch = tmp_path / "app_prefs.json"
    monkeypatch.setattr(config, "APP_PREFS_FILE", str(scratch))
    monkeypatch.setattr(app_prefs.config, "APP_PREFS_FILE", str(scratch))

    # Default is ON (True)
    assert app_prefs.judge_context_aware_rejudge() is True

    # Can be toggled OFF
    app_prefs.set_judge_context_aware_rejudge(False)
    assert app_prefs.judge_context_aware_rejudge() is False

    # Can be toggled back ON
    app_prefs.set_judge_context_aware_rejudge(True)
    assert app_prefs.judge_context_aware_rejudge() is True


def test_judge_runner_temperature_is_zero_deterministic(tmp_path):
    csv_file = tmp_path / "dummy.csv"
    csv_file.write_text("id,src,dst\n1,cat,kucing\n", encoding="utf-8")
    target = JudgeTarget(path=str(csv_file), src_col=2, dst_col=3)
    runner = JudgeRunner(target=target)
    assert runner._temperature() == 0.0


def test_fix_sys_contains_mandatory_defect_and_placeholder_preservation():
    from translator.judge.prompts import _FIX_SYS, build_fix_prompt
    assert "MANDATORY DEFECT RESOLUTION" in _FIX_SYS
    assert "ABSOLUTE PLACEHOLDER PRESERVATION" in _FIX_SYS
    assert "code tokens, NOT prose" in _FIX_SYS

    prompt = build_fix_prompt([(4494, "src", "dst")], {4494: "note"}, tgt_lang="Indonesian")
    assert "Each line MUST be on its own separate line as: [ROW_NUMBER]" in prompt
    assert "Always put a real newline before every [ROW_NUMBER]" in prompt


def test_judge_fix_review_screen_unselects_identical_before_after():
    from translator.judge.models import StagedFix, JudgeReport, RowIssue
    from translator.tui.screens.judge import JudgeFixReviewScreen
    staged = StagedFix()
    # Row 1 has identical before & after -> should start UNCHECKED
    # Row 2 has changed after -> should start CHECKED
    staged.texts = {1: "sudah terbiasa", 2: "baru diubah"}
    report = JudgeReport(rows=[(1, "src1", "sudah terbiasa"), (2, "src2", "lama")])
    runner = object()
    screen = JudgeFixReviewScreen(runner=runner, report=report, staged=staged)
    options = screen._build_review_options()
    opt1 = next(o for o in options if o.value == 1)
    opt2 = next(o for o in options if o.value == 2)
    assert opt1.selected is False
    assert opt2.selected is True


def test_judge_runner_plan_accepts_history_map(tmp_path):
    csv_file = tmp_path / "dummy.csv"
    csv_file.write_text("id,src,dst\n1,cat,kucing\n", encoding="utf-8")
    target = JudgeTarget(path=str(csv_file), src_col=2, dst_col=3)
    runner = JudgeRunner(target=target)
    history = {1: "WHAT: previous error · FIX: do X"}
    plan = runner.plan([(1, "cat", "kucing")], history_map=history)
    assert plan.history_map == history


