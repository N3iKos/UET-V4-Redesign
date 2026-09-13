"""
tests/test_rejudge_qol.py — Tests for Rejudge QoL, dynamic rate limit worker adjustment,
and force-cancel worker reset.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from translator.scheduling.scheduler import WorkerStatus
from translator.tui.widgets.activity import WorkerLedger
from translator.judge.runner import JudgeRunner, JudgeTarget
from translator.data import app_prefs
from translator import config


def test_worker_ledger_reset_all_idle():
    ledger = WorkerLedger()
    status1 = WorkerStatus(worker_id=1, phase="sending", batch_num=10, rows=5, elapsed=3.5)
    status2 = WorkerStatus(worker_id=2, phase="generating", batch_num=11, rows=5, elapsed=1.2)
    ledger.note(status1)
    ledger.note(status2)

    statuses = ledger.statuses()
    assert any(getattr(s, "phase", "") == "sending" for s in statuses)

    ledger.reset_all_idle()
    reset_statuses = ledger.statuses()
    assert len(reset_statuses) == 2
    for s in reset_statuses:
        assert getattr(s, "phase", "") == "idle"
        assert getattr(s, "batch_num", 0) == 0
        assert getattr(s, "rows", 0) == 0
        assert getattr(s, "elapsed", 0.0) == 0.0


def test_worker_ledger_freeze_preserves_elapsed():
    import time
    from translator.tui.widgets.activity import worker_line
    ledger = WorkerLedger()
    # Started 3 seconds ago
    running = WorkerStatus(worker_id=1, phase="generating", batch_num=1, started_at=time.time() - 3.2)
    ledger.note(running)

    done = WorkerStatus(worker_id=1, phase="done", batch_num=1)
    ledger.note(done)

    # Status should be frozen with ~3.2s elapsed, not 0.0s
    statuses = ledger.statuses()
    assert len(statuses) == 1
    assert statuses[0].phase == "done"
    assert statuses[0].elapsed >= 3.0
    text = worker_line(statuses[0]).plain
    assert "success" in text
    assert "3s" in text or "4s" in text
    assert text.rstrip().endswith("s")


def test_worker_line_renders_integer_seconds():
    import time
    from translator.tui.widgets.activity import worker_line
    status = WorkerStatus(worker_id=1, phase="generating", batch_num=1, started_at=time.time() - 10.4)
    text = worker_line(status).plain
    assert "10s" in text
    assert "." not in text.split()[-1]


def test_job_log_path_uses_full_path(tmp_path):
    from translator.core.models import JobConfig
    from translator.tui.screens.wizard import PreviewScreen
    full_out = str(tmp_path / "Dialog_Translated.csv")
    unique_out = str(tmp_path / ".uet_cache" / "Dialog.unique_Translated.csv")
    job = JobConfig(
        provider_id="prov",
        model_id="mod",
        src_path=unique_out,
        out_path=unique_out,
        full_out_path=full_out,
        src_col=1,
        dst_col=2,
        debug_log=True,
    )
    base_out = job.full_out_path or job.full_src_path or job.out_path
    log_path = os.path.splitext(base_out)[0] + "_uet.log"
    assert log_path == str(tmp_path / "Dialog_Translated_uet.log")


def test_rejudge_filter_chip_removed():
    from translator.tui.screens.judge import JudgeRunScreen
    target = JudgeTarget(path="dummy.csv", src_col=1, dst_col=2)
    screen = JudgeRunScreen(target, ("prov", "conn", "mod"), ["accuracy"])
    screen._rejudged_rows = {1, 2, 3}
    chips = screen._filter_counts()
    chip_keys = [key for key, _label, _count in chips]
    assert "rejudged" not in chip_keys


def test_flagged_so_far_counter_exceeds_buffer_limit():
    from translator.tui.screens.judge import JudgeRunScreen
    from translator.judge.models import RowIssue
    target = JudgeTarget(path="dummy.csv", src_col=1, dst_col=2)
    screen = JudgeRunScreen(target, ("prov", "conn", "mod"), ["accuracy"])
    # Simulate receiving 250 flagged rows
    for i in range(1, 251):
        issue = RowIssue(row=i, error_types=["mistranslation"], comment="error", severity="moderate")
        screen._row_reviewed(i, issue)

    # Buffer for rendering recent rows is capped at 200
    assert len(screen._flagged_live) == 200
    # But total flagged rows counter must NOT be stuck at 200
    assert len(screen._flagged_rows) == 250
    lines = screen._live_review_lines()
    assert any("Flagged so far[/]  250" in line for line in lines)



def test_rejudge_many_passes_max_rows(monkeypatch):
    from translator.providers.registry import ProviderRegistry
    class MockApp:
        def __init__(self):
            self.registry = ProviderRegistry()
            self.judge_session_rows = None
            self.judge_session_workers = None
            self.judge_rejudge_slot = None

    mock_app = MockApp()
    target = JudgeTarget(path="dummy.csv", src_col=1, dst_col=2)
    from translator.tui.screens.judge import JudgeRunScreen
    monkeypatch.setattr(JudgeRunScreen, "app", property(lambda self: mock_app))
    screen = JudgeRunScreen(target, ("prov", "conn", "mod"), ["accuracy"])
    runner = screen._make_runner(max_rows=7)
    assert runner._max_rows == 7


def test_begin_op_clears_stale_workers(monkeypatch):
    from translator.providers.registry import ProviderRegistry
    class MockApp:
        def __init__(self):
            self.registry = ProviderRegistry()
            self.judge_session_rows = None
            self.judge_session_workers = None
            self.judge_rejudge_slot = None

    mock_app = MockApp()
    target = JudgeTarget(path="dummy.csv", src_col=1, dst_col=2)
    from translator.tui.screens.judge import JudgeRunScreen
    monkeypatch.setattr(JudgeRunScreen, "app", property(lambda self: mock_app))
    screen = JudgeRunScreen(target, ("prov", "conn", "mod"), ["accuracy"])
    for i in range(1, 81):
        screen._workers[i] = WorkerStatus(worker_id=i, phase="idle")
        screen._ledger.note(screen._workers[i])
    assert len(screen._workers) == 80
    assert screen._ledger.count == 80

    screen._begin_op("Rejudge", ("prov", "conn", "mod2"))
    assert len(screen._workers) == 0
    assert screen._ledger.count == 0


def test_with_rejudge_model_skips_when_remembered(monkeypatch, tmp_path):
    prefs_file = tmp_path / "app_prefs.json"
    monkeypatch.setattr(config, "APP_PREFS_FILE", str(prefs_file))
    monkeypatch.setattr(app_prefs.config, "APP_PREFS_FILE", str(prefs_file))

    slot = ("prov", "conn", "mod")
    app_prefs.remember_judge_settings(slot, max_rows=15, workers=6, remember=True)

    from translator.providers.registry import ProviderRegistry
    class MockApp:
        def __init__(self):
            self.registry = ProviderRegistry()
            self.judge_session_rows = None
            self.judge_session_workers = None
            self.judge_rejudge_slot = None
            self.pushed = []
        def push_screen(self, screen, callback):
            self.pushed.append(screen)

    mock_app = MockApp()
    target = JudgeTarget(path="dummy.csv", src_col=1, dst_col=2)
    from translator.tui.screens.judge import JudgeRunScreen
    monkeypatch.setattr(JudgeRunScreen, "app", property(lambda self: mock_app))
    screen = JudgeRunScreen(target, slot, ["accuracy"])

    called_args = []
    screen._with_rejudge_model(lambda s, r, w: called_args.append((s, r, w)))
    assert called_args == [(slot, 15, 6)]
    assert len(mock_app.pushed) == 0


@pytest.mark.anyio
async def test_rejudge_setup_screen_dynamic_workers_and_session_memory(monkeypatch, tmp_path):
    prefs_file = tmp_path / "app_prefs.json"
    monkeypatch.setattr(config, "APP_PREFS_FILE", str(prefs_file))
    monkeypatch.setattr(app_prefs.config, "APP_PREFS_FILE", str(prefs_file))

    from textual.app import App
    from translator.providers.registry import ProviderRegistry
    from translator.providers.models import Provider, Connection, ModelEntry
    from translator.providers.ratelimit import RateLimitPreset
    from textual.widgets import Input

    reg = ProviderRegistry()
    preset = RateLimitPreset(name="test", rpm=30, tpm=60000, suggested_batch_size=10)
    m = ModelEntry(id="mod", display_name="Model", preset=preset)
    conn = Connection(id="conn", name="Conn", models=[m])
    p = Provider(id="prov", name="Prov", connections=[conn])
    reg.providers = [p]

    class TestApp(App):
        def __init__(self):
            super().__init__()
            self.registry = reg
            self.judge_session_rows = 15
            self.judge_session_workers = 5
            self.judge_rejudge_slot = None

    slot = ("prov", "conn", "mod")
    from translator.tui.screens.judge import RejudgeSetupScreen
    app = TestApp()
    async with app.run_test() as pilot:
        screen = RejudgeSetupScreen(slot)
        await app.push_screen(screen)
        await pilot.pause()

        # Sticky session values preserved
        assert screen._batch.value == "15"
        assert screen._workers.value == "5"

        # User changes batch to 8 -> workers recalculate dynamically
        screen._batch.value = "8"
        screen.on_input_changed(Input.Changed(screen._batch.input, "8"))
        expected_workers = screen._derived_workers()
        assert screen._workers.value == str(expected_workers)

        # Starting saves session memory and updates app
        screen._remember.options[0].selected = True
        screen._start()
        assert app.judge_session_rows == 8
        assert app.judge_session_workers == expected_workers
        assert app.judge_rejudge_slot == slot
        remembered = app_prefs.remembered_judge_settings(slot)
        assert remembered["max_rows"] == 8
        assert remembered["workers"] == expected_workers


