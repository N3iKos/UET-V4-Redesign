from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from translator.core.engine import ProcessingEngine
from translator.core.failed_cache import load_failed_rows, record_failed_rows
from translator.core.models import JobConfig, ProcessStats
from translator.scheduling.scheduler import BatchResult
from translator.tui.screens.run import RunScreen, SummaryScreen, TranslateSession


def test_retranslate_failed_when_unique_temp_file_deleted(tmp_path, monkeypatch):
    """
    Regression test for BUG 1:
    When dedup is used, _final_full_sync removes unique temporary files.
    Retranslate Failed must not crash with FileNotFoundError when unique files are gone;
    it must seamlessly fall back to full_src_path and full_out_path.
    """
    cache_dir = tmp_path / ".uet_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    full_src = tmp_path / "dialog.csv"
    full_src.write_text("id,src,dst\n1,hello,\n2,world,\n3,test,\n", encoding="utf-8")

    full_out = tmp_path / "dialog_Translated.csv"
    # Row 2 failed and was written as [TRANSLATE_FAILED]
    full_out.write_text("id,src,dst\n1,halo,\n2,[TRANSLATE_FAILED],\n3,uji,\n", encoding="utf-8")

    # Record row 2 in failed cache
    record_failed_rows(str(full_out), 3, [2])

    # Deleted temporary files that previously existed
    deleted_unique_src = cache_dir / "dialog.unique.csv"
    deleted_unique_out = cache_dir / "dialog.unique_Translated.csv"
    assert not deleted_unique_src.exists()
    assert not deleted_unique_out.exists()

    job = JobConfig(
        src_path=str(deleted_unique_src),
        out_path=str(deleted_unique_out),
        full_src_path=str(full_src),
        full_out_path=str(full_out),
        map_path=str(cache_dir / "dialog.map.json"),
        src_col=2,
        dst_col=3,
        start_row=2,
        num_rows=3,
        op_mode="retranslate_failed",
    )

    engine = ProcessingEngine(job, connection_pool=MagicMock())

    # Mock scheduler run to return translated row 2
    def mock_scheduler_run(self, tasks, total_rows=0):
        # Emulate successful translation
        engine._write_result(
            BatchResult(results={2: "dunia"}, failed_rows=[])
        )
        return ProcessStats(ok=1, failed=0, total=1)

    monkeypatch.setattr("translator.core.engine.Scheduler.run", mock_scheduler_run)

    stats, scan_results = engine.run_retranslate_failed(close_log=False)

    assert stats.ok == 1
    assert stats.failed == 0
    # Destination file should now be updated with translated text
    content = full_out.read_text(encoding="utf-8")
    assert "dunia" in content
    # Failed cache for row 2 should be cleared
    assert 2 not in load_failed_rows(str(full_out), 3)


def test_summary_screen_multi_file_retranslate_builds_correct_jobs(tmp_path):
    """
    Regression test for BUG 1:
    In multi-file translation where File 2 has failed rows,
    SummaryScreen must show the total failed count and prepare all needed jobs.
    """
    f1_src = tmp_path / "f1.csv"
    f1_out = tmp_path / "f1_Translated.csv"
    f1_src.write_text("id,src\n1,a\n", encoding="utf-8")
    f1_out.write_text("id,src,dst\n1,a,A\n", encoding="utf-8")

    f2_src = tmp_path / "f2.csv"
    f2_out = tmp_path / "f2_Translated.csv"
    f2_src.write_text("id,src\n1,b\n2,c\n", encoding="utf-8")
    f2_out.write_text("id,src,dst\n1,b,B\n2,c,[TRANSLATE_FAILED]\n", encoding="utf-8")

    job1 = JobConfig(src_path=str(f1_src), out_path=str(f1_out), src_col=2, dst_col=3)
    job2 = JobConfig(src_path=str(f2_src), out_path=str(f2_out), src_col=2, dst_col=3)

    session1 = TranslateSession(index=0, job=job1, stats=ProcessStats(ok=1, failed=0, total=1))
    session2 = TranslateSession(index=1, job=job2, stats=ProcessStats(ok=1, failed=1, total=2))

    summary = SummaryScreen(
        job=job1,
        stats=ProcessStats(ok=2, failed=1, total=3),
        events=[],
        jobs=[job1, job2],
        sessions=[session1, session2],
    )

    options = summary.build_options()
    retry_opts = [o for o in options if o.value == "retry"]
    assert len(retry_opts) == 1
    assert "1 failed" in retry_opts[0].label


def test_run_screen_worker_crash_is_logged_to_persistent_log(tmp_path, monkeypatch):
    """
    Ensure that unexpected exceptions inside RunScreen._run_job or _run_single_session
    are caught and logged to persistent crash log without crashing Textual.
    """
    from translator.core.crash_logger import set_crash_log_path

    crash_log = tmp_path / "crash_test.log"
    set_crash_log_path(str(crash_log))

    job = JobConfig(src_path="nonexistent.csv", out_path="nonexistent_out.csv", src_col=1, dst_col=2)
    screen = RunScreen(job)
    # Mock app
    app_mock = MagicMock()
    screen._app = app_mock

    def bad_build_pool(*args, **kwargs):
        raise RuntimeError("Simulated unhandled worker crash")

    monkeypatch.setattr("translator.tui.screens.run.build_pool", bad_build_pool)

    # Calling _run_job_impl (inner worker function) should catch, log, and notify
    screen._run_job_impl()

    assert crash_log.exists()
    content = crash_log.read_text(encoding="utf-8")
    assert "Simulated unhandled worker crash" in content
    assert "translation_worker" in content


def test_retranslate_failed_scans_empty_rows_beyond_old_num_rows(tmp_path, monkeypatch):
    """
    Ensure that retranslate_failed scans all remaining rows in the file even if
    job.num_rows was previously capped (e.g. at 3 rows on an 8-row file).
    """
    src_file = tmp_path / "game.csv"
    src_file.write_text(
        "id,src,dst\n"
        "1,apple,\n"
        "2,banana,\n"
        "3,orange,\n"
        "4,cat,\n"
        "5,dog,\n"
        "6,bird,\n"
        "7,fish,\n"
        "8,rat,\n",
        encoding="utf-8",
    )

    out_file = tmp_path / "game_Translated.csv"
    # First 3 rows were translated in col 3 (dst), rows 4 to 8 are empty
    out_file.write_text(
        "id,src,dst\n"
        "1,apple,apel\n"
        "2,banana,pisang\n"
        "3,orange,jeruk\n"
        "4,cat,\n"
        "5,dog,\n"
        "6,bird,\n"
        "7,fish,\n"
        "8,rat,\n",
        encoding="utf-8",
    )

    job = JobConfig(
        src_path=str(src_file),
        out_path=str(out_file),
        src_col=2,
        dst_col=3,
        start_row=2,
        num_rows=3,  # old limit from previous run
        user_row_limit=0,
        op_mode="retranslate_failed",
    )

    engine = ProcessingEngine(job, connection_pool=MagicMock())

    translated_records = {}

    def mock_scheduler_run(self, tasks, total_rows=0):
        for task in tasks:
            for rn, txt in task.rows:
                translated_records[rn] = f"id_{txt}"
        engine._write_result(
            BatchResult(results=translated_records, failed_rows=[])
        )
        return ProcessStats(ok=len(translated_records), failed=0, total=len(translated_records))

    monkeypatch.setattr("translator.core.engine.Scheduler.run", mock_scheduler_run)

    stats, scan_results = engine.run_retranslate_failed(close_log=False)

    # Must have found and translated all 5 empty rows (rows 5 to 9), not stopped at row 4!
    assert stats.ok == 5
    assert 5 in translated_records  # row 5 (cat)
    assert 9 in translated_records  # row 9 (rat)
    assert len(scan_results) == 5


def test_summary_screen_shows_retry_when_failed_zero_but_empty_cells_exist(tmp_path):
    """
    Regression test for Issue 1:
    Even if total_failed == 0, if there are empty rows in the destination file,
    SummaryScreen must show the retry option with the count of missing rows.
    """
    src_file = tmp_path / "game.csv"
    src_file.write_text("id,src,dst\n1,hello,\n2,world,\n3,test,\n", encoding="utf-8")

    out_file = tmp_path / "game_Translated.csv"
    # Row 1 (header), Row 2 translated, Row 3 & 4 empty
    out_file.write_text("id,src,dst\n1,hello,halo\n2,world,\n3,test,\n", encoding="utf-8")

    job = JobConfig(
        src_path=str(src_file),
        out_path=str(out_file),
        src_col=2,
        dst_col=3,
        start_row=2,
        num_rows=3,
    )

    summary = SummaryScreen(
        job=job,
        stats=ProcessStats(ok=1, failed=0, total=3),
        events=[],
        jobs=[job],
    )

    assert summary._count_job_empty(job) == 2

    options = summary.build_options()
    retry_opts = [o for o in options if o.value == "retry"]
    assert len(retry_opts) == 1
    assert "Translate 2 missing / failed rows now" in retry_opts[0].label


def test_summary_screen_shows_combined_count_when_both_failed_and_empty_exist(tmp_path):
    """
    When total_failed > 0 and empty_count > 0,
    SummaryScreen must show the sum of failed and empty rows.
    """
    src_file = tmp_path / "game.csv"
    src_file.write_text("id,src,dst\n1,a,\n2,b,\n3,c,\n4,d,\n", encoding="utf-8")

    out_file = tmp_path / "game_Translated.csv"
    # Row 2 (a) translated, Row 3 (b) failed, Row 4 (c) empty, Row 5 (d) empty
    out_file.write_text("id,src,dst\n1,a,A\n2,b,[TRANSLATE_FAILED]\n3,c,\n4,d,\n", encoding="utf-8")

    job = JobConfig(
        src_path=str(src_file),
        out_path=str(out_file),
        src_col=2,
        dst_col=3,
        start_row=2,
        num_rows=4,
    )

    summary = SummaryScreen(
        job=job,
        stats=ProcessStats(ok=1, failed=1, total=4),
        events=[],
        jobs=[job],
    )

    assert summary._count_job_empty(job) == 2

    options = summary.build_options()
    retry_opts = [o for o in options if o.value == "retry"]
    assert len(retry_opts) == 1
    # 1 failed + 2 empty = 3
    assert "Translate 3 missing / failed rows now" in retry_opts[0].label


def test_summary_screen_multi_file_does_not_skip_sessions_with_empty_cells(tmp_path, monkeypatch):
    """
    Regression test for Issue 1 in multi-file jobs:
    If File 1 has failed=1 (empty=0) and File 2 has failed=0 (empty=2),
    retrying must NOT skip File 2; both files must be included in retranslate_jobs.
    """
    f1_src = tmp_path / "f1.csv"
    f1_out = tmp_path / "f1_Translated.csv"
    f1_src.write_text("id,src,dst\n1,a,\n2,b,\n", encoding="utf-8")
    f1_out.write_text("id,src,dst\n1,a,A\n2,b,[TRANSLATE_FAILED]\n", encoding="utf-8")

    f2_src = tmp_path / "f2.csv"
    f2_out = tmp_path / "f2_Translated.csv"
    f2_src.write_text("id,src,dst\n1,c,\n2,d,\n", encoding="utf-8")
    # File 2 has 2 empty rows in dst_col
    f2_out.write_text("id,src,dst\n1,c,\n2,d,\n", encoding="utf-8")

    job1 = JobConfig(src_path=str(f1_src), out_path=str(f1_out), src_col=2, dst_col=3, start_row=2)
    job2 = JobConfig(src_path=str(f2_src), out_path=str(f2_out), src_col=2, dst_col=3, start_row=2)

    session1 = TranslateSession(index=0, job=job1, stats=ProcessStats(ok=1, failed=1, total=2))
    session2 = TranslateSession(index=1, job=job2, stats=ProcessStats(ok=0, failed=0, total=2))

    summary = SummaryScreen(
        job=job1,
        stats=ProcessStats(ok=1, failed=1, total=4),
        events=[],
        jobs=[job1, job2],
        sessions=[session1, session2],
    )

    assert summary._count_job_empty(job1) == 0
    assert summary._count_job_empty(job2) == 2

    options = summary.build_options()
    retry_opts = [o for o in options if o.value == "retry"]
    assert len(retry_opts) == 1
    # 1 failed + 2 empty = 3
    assert "Translate 3 missing / failed rows now" in retry_opts[0].label

    # Verify handle_choice keeps job2 because of its empty rows!
    pushed = []
    fake_app = MagicMock()
    fake_app.push_screen = lambda s: pushed.append(s)
    fake_app.pop_screen = MagicMock()
    monkeypatch.setattr(SummaryScreen, "app", property(lambda self: fake_app))

    summary.handle_choice(retry_opts[0])

    assert len(pushed) == 1
    run_screen = pushed[0]
    assert len(run_screen.jobs) == 2
    assert run_screen.jobs[0].src_path == str(f1_src)
    assert run_screen.jobs[1].src_path == str(f2_src)
    assert run_screen.jobs[0].op_mode == "retranslate_failed"
    assert run_screen.jobs[1].op_mode == "retranslate_failed"


def test_retranslate_failed_processes_empty_cells_fail_markers_and_cached_failed(tmp_path, monkeypatch):
    """
    Ensure run_retranslate_failed detects empty rows, FAIL_MARKER rows,
    and rows stored in failed_cache, queuing all of them for retranslation.
    """
    src_file = tmp_path / "dialog.csv"
    src_file.write_text(
        "id,src,dst\n"
        "1,hello,\n"
        "2,world,\n"
        "3,foo,\n"
        "4,bar,\n"
        "5,done,\n",
        encoding="utf-8",
    )

    out_file = tmp_path / "dialog_Translated.csv"
    # Row 2 (hello) -> empty
    # Row 3 (world) -> [TRANSLATE_FAILED]
    # Row 4 (foo) -> "foo" (copied source fallback, recorded in cached_failed)
    # Row 5 (bar) -> empty
    # Row 6 (done) -> "selesai" (healthy)
    out_file.write_text(
        "id,src,dst\n"
        "1,hello,\n"
        "2,world,[TRANSLATE_FAILED]\n"
        "3,foo,foo\n"
        "4,bar,\n"
        "5,done,selesai\n",
        encoding="utf-8",
    )

    # Record row 4 in failed cache
    record_failed_rows(str(out_file), 3, [4])

    job = JobConfig(
        src_path=str(src_file),
        out_path=str(out_file),
        src_col=2,
        dst_col=3,
        start_row=2,
        op_mode="retranslate_failed",
    )

    engine = ProcessingEngine(job, connection_pool=MagicMock())

    translated_records = {}

    def mock_scheduler_run(self, tasks, total_rows=0):
        for task in tasks:
            for rn, txt in task.rows:
                translated_records[rn] = f"translated_{txt}"
        engine._write_result(
            BatchResult(results=translated_records, failed_rows=[])
        )
        return ProcessStats(ok=len(translated_records), failed=0, total=len(translated_records))

    monkeypatch.setattr("translator.core.engine.Scheduler.run", mock_scheduler_run)

    stats, scan_results = engine.run_retranslate_failed(close_log=False)

    # Must detect:
    # Row 2: empty
    # Row 3: TRANSLATE_FAILED
    # Row 4: TRANSLATE_FAILED (from cached_failed)
    # Row 5: empty
    # Total = 4 rows
    assert stats.ok == 4
    assert len(scan_results) == 4
    scanned_rows = [rn for rn, _, _ in scan_results]
    assert scanned_rows == [2, 3, 4, 5]
    assert 6 not in scanned_rows  # Row 6 was healthy


def test_preview_screen_offers_resume_and_retranslate_options_for_existing_output(tmp_path):
    """
    Ensure PreviewScreen inspects existing output file and offers
    'Translate missing rows only (Resume)' or 'Retranslate failed/missing rows'
    when output file has filled and empty/failed rows.
    """
    from translator.tui.screens.wizard import PreviewScreen

    src_file = tmp_path / "game.csv"
    src_file.write_text(
        "id,src,dst\n"
        "1,one,\n"
        "2,two,\n"
        "3,three,\n"
        "4,four,\n",
        encoding="utf-8",
    )

    out_file = tmp_path / "game_Translated.csv"
    # Row 2 (one) -> satu (filled)
    # Row 3 (two) -> [TRANSLATE_FAILED] (failed)
    # Row 4 (three) -> empty
    # Row 5 (four) -> empty
    out_file.write_text(
        "id,src,dst\n"
        "1,one,satu\n"
        "2,two,[TRANSLATE_FAILED]\n"
        "3,three,\n"
        "4,four,\n",
        encoding="utf-8",
    )

    draft = {
        "src_path": str(src_file),
        "out_path": str(out_file),
        "src_col": 2,
        "dst_col": 3,
        "start_row": 2,
        "num_rows": 4,
        "user_row_limit": 0,
        "skip_filled": True,
        "op_mode": "translate",
        "batch_size": 15,
        "n_workers": 1,
        "processing_mode": "parallel",
    }

    screen = PreviewScreen()
    screen._test_draft = draft

    info = screen._inspect_existing_outputs()
    assert info["exists"] is True
    assert info["filled"] == 1
    assert info["failed"] == 1
    assert info["empty"] == 2

    options = screen.build_options()
    option_values = [o.value for o in options]
    assert "retranslate_failed" in option_values
    assert "resume" in option_values
    assert "start_scratch" in option_values

    # Check labels
    retrans_opt = [o for o in options if o.value == "retranslate_failed"][0]
    assert "3 rows" in retrans_opt.label  # 1 failed + 2 empty = 3

    resume_opt = [o for o in options if o.value == "resume"][0]
    assert "2 rows" in resume_opt.label  # 2 empty


