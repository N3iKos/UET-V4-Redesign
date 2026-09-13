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


def test_judge_run_screen_live_tab_switch_and_progress_percentage(tmp_path):
    f1 = tmp_path / "a.csv"
    f2 = tmp_path / "b.csv"
    f1.write_text("id,src,dst\n1,cat,kucing\n", encoding="utf-8")
    f2.write_text("id,src,dst\n1,dog,anjing\n", encoding="utf-8")

    from translator.judge.models import RowIssue
    t1 = JudgeTarget(path=str(f1), src_col=2, dst_col=3, num_rows=100)
    t2 = JudgeTarget(path=str(f2), src_col=2, dst_col=3, num_rows=100)

    screen = JudgeRunScreen(
        target=t1,
        targets=[t1, t2],
        model_slot=("p", "c", "m"),
        criteria=["mistranslation"],
    )
    # Set targets total rows
    screen.sessions[0].unique_rows_count = 100
    screen.sessions[1].unique_rows_count = 100

    # Review row 1 out of 100
    screen._session_row_reviewed(0, 1, RowIssue(1, ["mistranslation"], "wrong", "critical"))
    # Tab 0 must show 1%, NOT 100%!
    label0 = screen.tab_bar.get_tab_label(0)
    assert "1%" in label0
    assert "100%" not in label0

    # Switch to tab 1 during live evaluation (without finished report)
    # Must not raise AttributeError: 'JudgeRunScreen' object has no attribute '_rebuild_grid'
    screen.grid = object()
    screen.action_switch_tab(1)
    assert screen.active_session_index == 1


def test_judge_run_screen_live_tab_switch_with_compare_grid(tmp_path):
    from translator.tui.widgets.compare import CompareGrid

    f1 = tmp_path / "a.csv"
    f2 = tmp_path / "b.csv"
    f1.write_text("id,src,dst\n1,cat,kucing\n", encoding="utf-8")
    f2.write_text("id,src,dst\n1,dog,anjing\n", encoding="utf-8")

    t1 = JudgeTarget(path=str(f1), src_col=2, dst_col=3, num_rows=100)
    t2 = JudgeTarget(path=str(f2), src_col=2, dst_col=3, num_rows=100)

    screen = JudgeRunScreen(
        target=t1,
        targets=[t1, t2],
        model_slot=("p", "c", "m"),
        criteria=["mistranslation"],
    )
    screen.grid = CompareGrid()
    # Live switch to tab 1 must not crash with AttributeError clear
    screen.action_switch_tab(1)
    assert screen.active_session_index == 1
    screen.action_switch_tab(0)
    assert screen.active_session_index == 0


def test_judge_run_screen_plan_rows_and_unfinished_rows(tmp_path):
    from translator.judge.models import JudgeReport
    f1 = tmp_path / "a.csv"
    f1.write_text("id,src,dst\n1,cat,kucing\n2,dog,anjing\n", encoding="utf-8")
    t1 = JudgeTarget(path=str(f1), src_col=2, dst_col=3, num_rows=2)

    screen = JudgeRunScreen(
        target=t1,
        model_slot=("p", "c", "m"),
        criteria=["mistranslation"],
    )
    screen._expected_rows = 2
    screen._by_row = {1: ("cat", "kucing"), 2: ("dog", "anjing")}
    screen._report = JudgeReport(rows=[(1, "cat", "kucing"), (2, "dog", "anjing")])

    # _plan_rows must return iterable tuples
    planned = screen._plan_rows()
    assert len(planned) == 2
    assert (1, "cat", "kucing") in planned

    # _unfinished_rows must not raise TypeError when run is paused
    screen._run_state = "paused"
    screen._reviewed_rows = {1}
    unfinished = screen._unfinished_rows()
    assert unfinished == [2]


def test_judge_setup_screen_cleans_uet_cache_without_name_error(tmp_path, monkeypatch):
    from translator.core.models import JobConfig
    from translator.tui.screens.judge import JudgeSetupScreen

    game_dir = tmp_path / "game"
    cache_dir = game_dir / ".uet_cache"
    cache_dir.mkdir(parents=True)
    real_out = game_dir / "dialog_Translated.csv"
    real_out.write_text("id,src,dst\n1,cat,kucing\n", encoding="utf-8")
    cache_unique = cache_dir / "dialog.12345.unique_Translated.csv"
    cache_unique.write_text("id,src,dst\n1,cat,kucing\n", encoding="utf-8")

    j = JobConfig(
        provider_id="p", model_id="m",
        src_path=str(game_dir / "dialog.csv"),
        out_path=str(cache_unique),
        full_out_path="",
        src_col=2, dst_col=3,
    )

    class FakeApp:
        def __init__(self):
            self.judge_draft = {}
            self.screen_stack = []
        def pop_screen(self):
            pass
        def push_screen(self, s):
            self.screen_stack.append(s)

    fake_app = FakeApp()
    monkeypatch.setattr(JudgeSetupScreen, "app", property(lambda self: fake_app))
    screen = JudgeSetupScreen(job=j, jobs=[j])
    # Must run on_screen_ready without NameError: name 're' is not defined
    screen.on_screen_ready()
    assert fake_app.judge_draft.get("path") == str(real_out.resolve())


def test_judge_session_isolates_settled_rows_and_fix_cycles(tmp_path):
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

    # Tab 0 has row 10 settled
    screen._settled_rows = {10}
    screen._fix_cycles = {10: 3}

    # Switch to tab 1
    screen.action_switch_tab(1)
    assert screen._settled_rows == set()
    assert screen._fix_cycles == {}

    # Switch back to tab 0
    screen.action_switch_tab(0)
    assert screen._settled_rows == {10}
    assert screen._fix_cycles == {10: 3}


