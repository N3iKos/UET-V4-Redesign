"""
tests/test_judge_multifile_screen.py — Multi-tab session state and switching in JudgeRunScreen.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.judge.runner import JudgeTarget
from translator.tui.screens.judge import JudgeRunScreen


def test_judge_run_screen_multi_tab_initialization(tmp_path):
    f1 = tmp_path / "a.csv"
    f2 = tmp_path / "b.csv"
    f1.write_text("id,src,dst\n1,cat,kucing\n", encoding="utf-8")
    f2.write_text("id,src,dst\n1,dog,anjing\n", encoding="utf-8")

    t1 = JudgeTarget(path=str(f1), src_col=2, dst_col=3)
    t2 = JudgeTarget(path=str(f2), src_col=2, dst_col=3)

    screen = JudgeRunScreen(
        target=t1,
        targets=[t1, t2],
        model_slot=("p", "c", "m"),
        criteria=["mistranslation"],
    )

    assert screen.is_multi_tab is True
    assert len(screen.sessions) == 2
    assert screen.active_session_index == 0
    assert screen._target.path == str(f1)

    # Switch to tab 2
    screen.action_switch_tab(1)
    assert screen.active_session_index == 1
    assert screen._target.path == str(f2)

    # Switch back to tab 1
    screen.action_switch_tab(0)
    assert screen.active_session_index == 0
    assert screen._target.path == str(f1)


def test_judge_run_screen_session_isolation(tmp_path):
    f1 = tmp_path / "a.csv"
    f2 = tmp_path / "b.csv"
    f1.write_text("id,src,dst\n1,cat,kucing\n", encoding="utf-8")
    f2.write_text("id,src,dst\n1,dog,anjing\n", encoding="utf-8")

    from translator.judge.models import JudgeReport, RowIssue
    t1 = JudgeTarget(path=str(f1), src_col=2, dst_col=3)
    t2 = JudgeTarget(path=str(f2), src_col=2, dst_col=3)

    screen = JudgeRunScreen(
        target=t1,
        targets=[t1, t2],
        model_slot=("p", "c", "m"),
        criteria=["mistranslation"],
    )

    # Simulate tab 1 having 1 issue
    rep1 = JudgeReport(rows=[(1, "cat", "kucing")], issues={1: RowIssue(1, ["mistranslation"], "bad", "critical")})
    screen._report = rep1
    screen._flagged_rows = {1}

    # Switch to tab 2
    screen.action_switch_tab(1)
    assert screen._report is None
    assert screen._flagged_rows == set()

    # Simulate tab 2 having 2 issues
    rep2 = JudgeReport(rows=[(1, "dog", "anjing"), (2, "bone", "tulang")], issues={
        1: RowIssue(1, ["mistranslation"], "bad", "moderate"),
        2: RowIssue(2, ["style"], "informal", "minor")
    })
    screen._report = rep2
    screen._flagged_rows = {1, 2}

    # Switch back to tab 1: tab 1's report and flagged rows are restored!
    screen.action_switch_tab(0)
    assert screen._report is rep1
    assert screen._flagged_rows == {1}
    assert screen._target.path == str(f1)

    # Switch back to tab 2: tab 2's report and flagged rows are restored!
    screen.action_switch_tab(1)
    assert screen._report is rep2
    assert screen._flagged_rows == {1, 2}
    assert screen._target.path == str(f2)



def test_judge_run_screen_single_target_not_multi_tab(tmp_path):
    f1 = tmp_path / "single.csv"
    f1.write_text("id,src,dst\n1,cat,kucing\n", encoding="utf-8")
    t1 = JudgeTarget(path=str(f1), src_col=2, dst_col=3)

    screen = JudgeRunScreen(
        target=t1,
        targets=[t1],
        model_slot=("p", "c", "m"),
        criteria=["mistranslation"],
    )
    assert screen.is_multi_tab is False
