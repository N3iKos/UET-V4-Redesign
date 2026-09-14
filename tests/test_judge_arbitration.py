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
