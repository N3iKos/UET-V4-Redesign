from __future__ import annotations

import json

from translator.core.tm import lookup_tm_batch, store_tm_batch
from translator.data import app_prefs
from translator.judge.runner import JudgeRunner, JudgeTarget


def test_dedup_preferences_default_and_legacy_migration(monkeypatch, tmp_path):
    prefs = tmp_path / "prefs.json"
    prefs.write_text(json.dumps({"dedup_mode": "in_memory"}), encoding="utf-8")
    monkeypatch.setattr(app_prefs.config, "APP_PREFS_FILE", str(prefs))
    assert app_prefs.dedup_mode() == "unique_file"
    app_prefs.set_dedup_mode("disabled")
    assert app_prefs.dedup_mode() == "disabled"


def test_judge_plan_uses_every_row_of_active_file():
    rows = [(1, "Hello", "Halo"), (2, "Hello", "Halo")]
    runner = JudgeRunner(JudgeTarget(path="dummy.csv", src_col=1, dst_col=2))
    plan = runner.plan(rows)
    assert [row[0] for section in plan.sections for row in section] == [1, 2]
    assert not hasattr(runner, "_judge_siblings")


def test_persistent_tm_survives_unique_file_architecture(monkeypatch, tmp_path):
    database = tmp_path / "tm.db"
    from translator.core import tm
    store_tm_batch("ja", "en", [("こんにちは", "Hello")], str(database))
    assert lookup_tm_batch("ja", "en", ["こんにちは", "missing"], str(database)) == {"こんにちは": "Hello"}


def test_run_screen_sidebar_text_with_dedup(tmp_path):
    from translator.core.models import JobConfig
    from translator.tui.screens.run import RunScreen

    map_file = tmp_path / "test.map.json"
    map_file.write_text(json.dumps({"total_rows": 100, "unique_rows": 10}), encoding="utf-8")

    job = JobConfig(
        provider_id="mock",
        model_id="mock-model",
        src_path=str(tmp_path / "unique.csv"),
        out_path=str(tmp_path / "unique_out.csv"),
        full_out_path=str(tmp_path / "full_out.csv"),
        map_path=str(map_file),
        src_col=1,
        dst_col=2,
        num_rows=10,
    )
    screen = RunScreen(job)
    screen._done_rows = 5
    text = screen._sidebar_text(1)
    assert "5 / 10 unique rows" in text
    assert "100 in file · 90 duplicates skipped" in text


def test_judge_run_screen_live_review_lines_with_dedup(tmp_path):
    from translator.judge.runner import JudgeTarget
    from translator.tui.screens.judge import JudgeRunScreen

    map_file = tmp_path / "test.map.json"
    map_file.write_text(json.dumps({"total_rows": 500, "unique_rows": 50}), encoding="utf-8")

    target = JudgeTarget(
        path=str(tmp_path / "unique.csv"),
        src_col=1,
        dst_col=2,
        num_rows=50,
        map_path=str(map_file),
    )
    screen = JudgeRunScreen(target, ("provider", "conn", "model"), ["accuracy"])
    screen._unique_rows_count = 50
    screen._file_total_count = 500
    screen._reviewed_rows = set(range(1, 26))
    lines = screen._live_review_lines()
    assert any("25 / 50 unique rows (50%)" in l for l in lines)
    assert any("500 in file · 450 duplicates skipped" in l for l in lines)
