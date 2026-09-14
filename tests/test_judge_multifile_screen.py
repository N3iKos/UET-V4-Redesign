"""
tests/test_judge_multifile_screen.py — Multi-tab session state and switching in JudgeRunScreen.
"""

from __future__ import annotations

import os
import sys

import pytest

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


def test_judge_workers_not_divided_across_multi_tab(tmp_path, monkeypatch):
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
        workers=20,
    )
    class FakeApp:
        class FakeRegistry:
            providers = []
        registry = FakeRegistry()
    screen._app = FakeApp()
    monkeypatch.setattr(JudgeRunScreen, "app", property(lambda self: FakeApp()))
    runner = screen._make_runner(workers=20)
    # In Judge mode, workers must remain 100% individual (20 workers, NOT 10 or 2!)
    assert runner._job.n_workers == 20


def test_operation_callbacks_stay_with_origin_and_ignore_stale_generation(tmp_path):
    from translator.judge.models import JudgeReport, RowIssue

    paths = [tmp_path / "a.csv", tmp_path / "b.csv"]
    for path in paths:
        path.write_text("id,src,dst\n1,x,y\n", encoding="utf-8")
    targets = [JudgeTarget(path=str(path), src_col=2, dst_col=3) for path in paths]
    screen = JudgeRunScreen(targets[0], ("p", "c", "m"), ["mistranslation"], targets=targets)
    reports = [JudgeReport(rows=[(1, "a", "A")]), JudgeReport(rows=[(1, "b", "B")])]
    screen.sessions[0].report, screen.sessions[1].report = reports

    old = screen._start_operation(0, "Rejudge", ("p", "c", "m"), report=reports[0])
    current = screen._start_operation(0, "Rejudge", ("p", "c", "m"), report=reports[0])
    screen.action_switch_tab(1)
    issue = RowIssue(1, ["mistranslation"], "wrong", "critical")
    screen._operation_rejudge_row_landed(old, 1, issue)
    assert not screen.sessions[0].rejudge_flagged_rows
    screen._operation_rejudge_row_landed(current, 1, issue)
    assert screen.sessions[0].rejudge_flagged_rows == {1}
    assert reports[1].issues == {}


def test_make_runner_explicit_target_owns_job_paths_and_columns(tmp_path, monkeypatch):
    from translator.providers.registry import ProviderRegistry

    a = JudgeTarget(path=str(tmp_path / "a.csv"), src_col=1, dst_col=2)
    b = JudgeTarget(path=str(tmp_path / "b.csv"), src_col=3, dst_col=4, ctx_col=5)
    class App:
        registry = ProviderRegistry()
    monkeypatch.setattr(JudgeRunScreen, "app", property(lambda self: App()))
    screen = JudgeRunScreen(a, ("p", "c", "m"), ["mistranslation"], targets=[a, b])
    runner = screen._make_runner(target=b)
    assert runner.target is b
    assert runner._job.src_path == b.path
    assert (runner._job.src_col, runner._job.dst_col, runner._job.ctx_col) == (3, 4, 5)


def test_operation_completion_retires_worker_snapshots(tmp_path):
    from translator.scheduling.scheduler import WorkerStatus

    target = JudgeTarget(path=str(tmp_path / "a.csv"), src_col=1, dst_col=2)
    screen = JudgeRunScreen(target, ("p", "c", "m"), ["mistranslation"])
    op = screen._start_operation(0, "Judge", ("p", "c", "m"))
    screen._operation_status(op, WorkerStatus(worker_id=1, phase="generating"))
    screen._finish_operation(op, "completed")
    assert screen.sessions[0].run_state == "completed"
    assert all(status.phase == "idle" for status in screen.sessions[0].ledger.statuses())


def test_staged_approval_rejects_rows_outside_origin_report(tmp_path):
    from translator.judge.models import JudgeReport, StagedFix
    from translator.tui.screens.judge import JudgeFixReviewScreen

    class Writer:
        def __init__(self):
            self.calls = []
        def apply_fix(self, texts, approved):
            self.calls.append((texts, approved))

    target = JudgeTarget(path=str(tmp_path / "a.csv"), src_col=1, dst_col=2)
    writer = Writer()
    report = JudgeReport(rows=[(1, "a", "A")])
    modal = JudgeFixReviewScreen(writer, report, StagedFix(texts={1: "fixed", 2: "wrong file"}), target=target)
    assert modal._valid_approved_rows([1, 2]) == [1]


def test_cancel_is_bound_to_origin_session(tmp_path):
    targets = [JudgeTarget(path=str(tmp_path / name), src_col=1, dst_col=2) for name in ("a.csv", "b.csv")]
    screen = JudgeRunScreen(targets[0], ("p", "c", "m"), ["mistranslation"], targets=targets)
    op_a = screen._start_operation(0, "Judge", ("p", "c", "m"))
    op_b = screen._start_operation(1, "Judge", ("p", "c", "m"))
    screen._cancel_operation(op_a)
    assert op_a.stop.is_set()
    assert not op_b.stop.is_set()
    screen._finish_operation(op_a, "cancelled")
    assert screen.sessions[0].run_state == "cancelled"
    assert screen.sessions[1].operation is op_b


def test_operation_start_sets_busy_atomically(tmp_path):
    target = JudgeTarget(path=str(tmp_path / "a.csv"), src_col=1, dst_col=2)
    screen = JudgeRunScreen(target, ("p", "c", "m"), ["mistranslation"])
    op = screen._start_operation(0, "Fix All", ("p", "c", "m"), busy_batch=True)
    assert screen.sessions[0].busy_batch
    assert screen._busy_batch
    assert screen.sessions[0].run_state == "running"
    assert screen.sessions[0].started_at > 0
    assert screen._stop is op.stop


def test_modal_origin_token_rejects_rows_after_generation_changes(tmp_path):
    from translator.judge.models import JudgeReport, RowIssue
    targets = [JudgeTarget(path=str(tmp_path / name), src_col=1, dst_col=2) for name in ("a.csv", "b.csv")]
    screen = JudgeRunScreen(targets[0], ("p", "c", "m"), ["mistranslation"], targets=targets)
    report = JudgeReport(rows=[(1, "a", "A")], issues={1: RowIssue(1, ["mistranslation"], "bad", "critical")})
    screen.sessions[0].report = report
    token = screen._capture_origin(rows=[1])
    screen._start_operation(0, "Judge", ("p", "c", "m"))
    assert not screen._origin_current(token, [1])
    assert not screen._origin_current(token, [2])


@pytest.mark.anyio
async def test_fix_all_modal_callback_after_tab_switch_uses_origin(tmp_path, monkeypatch):
    from translator.judge.models import JudgeReport, RowIssue
    from translator.tui.screens.judge import FixAllPlan

    targets = [JudgeTarget(path=str(tmp_path / name), src_col=1, dst_col=2) for name in ("a.csv", "b.csv")]
    screen = JudgeRunScreen(targets[0], ("p", "c", "m"), ["mistranslation"], targets=targets)
    report_a = JudgeReport(rows=[(1, "a", "A")], issues={1: RowIssue(1, ["mistranslation"], "bad", "critical")})
    report_b = JudgeReport(rows=[(1, "b", "B")])
    screen.sessions[0].report = screen._report = report_a
    screen.sessions[1].report = report_b
    from translator.tui.widgets.compare import Column, CompareGrid
    screen.grid = CompareGrid()
    screen.grid.set_sources([(1, "a")], [Column("J", "TRANSLATION", "model", "done", {1: "A"})])
    captured = {}
    class App:
        judge_fix_slot = None
        registry = type("Registry", (), {"providers": []})()
        def push_screen(self, modal, callback):
            captured["modal"] = modal
            captured["callback"] = callback
    monkeypatch.setattr(JudgeRunScreen, "app", property(lambda self: App()))
    screen.action_fix_all(force=True)
    screen.grid = None
    screen.action_switch_tab(1)
    monkeypatch.setattr(screen, "refresh_keys", lambda: None)
    monkeypatch.setattr(screen, "notify_status", lambda *args: None)
    monkeypatch.setattr(screen, "_begin_op", lambda *args: None)
    monkeypatch.setattr(screen, "_fix_many", lambda plan, operation: captured.update(operation=operation))
    captured["callback"](FixAllPlan(("p", "c", "m"), rows=[1], criteria=["mistranslation"]))
    assert captured["operation"].target is targets[0]
    assert captured["operation"].report is report_a
    assert screen.sessions[1].report is report_b


def test_row_fix_modal_callback_routes_to_origin_after_tab_switch(tmp_path, monkeypatch):
    from translator.judge.models import JudgeReport, RowIssue
    targets = [JudgeTarget(path=str(tmp_path / name), src_col=1, dst_col=2) for name in ("a.csv", "b.csv")]
    screen = JudgeRunScreen(targets[0], ("p", "c", "m"), ["mistranslation"], targets=targets)
    report_a = JudgeReport(rows=[(1, "a", "A")], issues={1: RowIssue(1, ["mistranslation"], "bad", "critical")})
    report_b = JudgeReport(rows=[(1, "b", "B")])
    screen.sessions[0].report = screen._report = report_a
    screen.sessions[1].report = report_b
    captured = {}
    class App:
        def push_screen(self, modal, callback):
            captured["callback"] = callback
    monkeypatch.setattr(JudgeRunScreen, "app", property(lambda self: App()))
    screen._open_row(1)
    screen._load_session(1)
    monkeypatch.setattr(screen, "_edited", lambda row, text: captured.update(row=row, text=text, target=screen._target))
    captured["callback"](("edit", "fixed A"))
    assert captured["target"] is targets[0]
    assert report_b.translation_for(1) == "B"


def test_stale_delayed_force_unlock_does_not_finish_new_operation(tmp_path):
    target = JudgeTarget(path=str(tmp_path / "a.csv"), src_col=1, dst_col=2)
    screen = JudgeRunScreen(target, ("p", "c", "m"), ["mistranslation"])
    old = screen._start_operation(0, "Fix All", ("p", "c", "m"), busy_batch=True)
    current = screen._start_operation(0, "Rejudge", ("p", "c", "m"), busy_batch=True)
    assert not screen._force_unlock_operation(old)
    assert screen.sessions[0].operation is current
    assert screen.sessions[0].busy_batch


def test_multifile_summary_lists_every_session(tmp_path):
    from translator.judge.models import JudgeReport, RowIssue
    from translator.tui.screens.judge import JudgeSummaryScreen

    a = JudgeTarget(path=str(tmp_path / "alpha.csv"), src_col=1, dst_col=2)
    b = JudgeTarget(path=str(tmp_path / "beta.csv"), src_col=1, dst_col=2)
    reports = [
        JudgeReport(rows=[(1, "a", "A")]),
        JudgeReport(rows=[(1, "b", "B")], issues={1: RowIssue(1, ["style"], "bad", "minor")}),
    ]
    summary = JudgeSummaryScreen(reports[0], a, sessions=[(a, reports[0], "completed"), (b, reports[1], "paused")])
    text = summary._summary()
    assert "alpha.csv" in text and "beta.csv" in text
    assert "completed" in text and "paused" in text
    assert "flagged" in text


def test_fix_all_respects_dont_ask_and_rejudge_isolated(tmp_path, monkeypatch):
    from translator.data import app_prefs
    from translator.judge.models import JudgeReport, RowIssue

    f1 = tmp_path / "a.csv"
    f1.write_text("id,src,dst\n1,cat,kucing\n", encoding="utf-8")
    t1 = JudgeTarget(path=str(f1), src_col=2, dst_col=3)

    slot_fix = ("prov1", "conn1", "model-fix")
    slot_judge = ("prov1", "conn1", "model-judge")

    # Set Fix All to "Don't ask again" with 15 rows, 8 workers
    app_prefs.remember_fix_settings(slot_fix, max_rows=15, workers=8, remember=True)

    screen = JudgeRunScreen(
        target=t1,
        model_slot=slot_judge,
        criteria=["mistranslation"],
    )
    rep = JudgeReport(rows=[(1, "cat", "kucing")], issues={1: RowIssue(1, ["mistranslation"], "wrong", "critical")})
    screen._report = rep
    screen.grid = type("G", (), {"visible_rows": [1]})()

    pushed_screens = []
    class FakeApp:
        def __init__(self):
            self.judge_fix_slot = slot_fix
            self.judge_rejudge_slot = None
        def push_screen(self, s, *a, **k):
            pushed_screens.append(s)

    fake_app = FakeApp()
    monkeypatch.setattr(JudgeRunScreen, "app", property(lambda self: fake_app))

    confirmed_plans = []
    screen._fix_all_confirmed = lambda origin, plan: confirmed_plans.append(plan)

    # 1. action_fix_all() should respect Don't ask and NOT push FixAllSetupScreen!
    screen.action_fix_all(force=False)
    assert len(pushed_screens) == 0
    assert len(confirmed_plans) == 1
    assert confirmed_plans[0].model_slot == slot_fix
    assert confirmed_plans[0].batch_size == 15
    assert confirmed_plans[0].workers == 8

    # 2. action_fix_all(force=True) should force push FixAllSetupScreen
    screen.action_fix_all(force=True)
    assert len(pushed_screens) == 1
    assert pushed_screens[0].__class__.__name__ == "FixAllSetupScreen"

    # 3. Rejudge MUST NOT be hijacked by Fix All! Rejudge should push RejudgeSetupScreen
    rejudge_run = []
    screen._with_rejudge_model(lambda slot, rows, workers: rejudge_run.append((slot, rows, workers)))
    # Because Rejudge was NOT remembered, it must push RejudgeSetupScreen!
    assert any(s.__class__.__name__ == "RejudgeSetupScreen" for s in pushed_screens)
    assert len(rejudge_run) == 0



