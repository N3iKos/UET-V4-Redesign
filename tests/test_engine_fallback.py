import os
from unittest.mock import MagicMock
from translator.core.engine import ProcessingEngine
from translator.core.models import JobConfig, BatchResult
from translator.core.failed_cache import load_failed_rows, record_failed_rows


def test_engine_writes_source_and_records_cache_on_failure(tmp_path, monkeypatch):
    csv_file = tmp_path / "game.csv"
    csv_file.write_text("id,src,dst\n1,nothing is real?,[TRANSLATE_FAILED]\n", encoding="utf-8")

    out_file = tmp_path / "game_Translated.csv"
    out_file.write_text("id,src,dst\n1,nothing is real?,\n", encoding="utf-8")

    monkeypatch.setattr("translator.data.app_prefs.failed_fallback_mode", lambda: "copy_source")

    job = JobConfig(
        src_path=str(csv_file),
        out_path=str(out_file),
        src_col=2,
        dst_col=3,
        start_row=2,
        num_rows=1,
    )
    engine = ProcessingEngine(job)
    # Simulate a failed batch
    source_map = {2: "nothing is real?"}
    res = BatchResult(results={}, failed_rows=[2])
    engine._write_result(res, source_map)

    # 1. Output file must contain source text "nothing is real?", NOT [TRANSLATE_FAILED]
    content = out_file.read_text(encoding="utf-8")
    assert "nothing is real?" in content
    assert "[TRANSLATE_FAILED]" not in content

    # 2. Failed cache must have row 2
    assert 2 in load_failed_rows(str(out_file), 3)


def test_engine_clears_cache_on_success(tmp_path):
    csv_file = tmp_path / "game.csv"
    csv_file.write_text("id,src,dst\n1,nothing is real?,\n", encoding="utf-8")

    out_file = tmp_path / "game_Translated.csv"
    out_file.write_text("id,src,dst\n1,nothing is real?,\n", encoding="utf-8")

    record_failed_rows(str(out_file), 3, [2])
    assert 2 in load_failed_rows(str(out_file), 3)

    job = JobConfig(
        src_path=str(csv_file),
        out_path=str(out_file),
        src_col=2,
        dst_col=3,
        start_row=2,
        num_rows=1,
    )
    engine = ProcessingEngine(job)
    res = BatchResult(results={2: "tidak ada yang nyata?"}, failed_rows=[])
    engine._write_result(res, {2: "nothing is real?"})

    assert 2 not in load_failed_rows(str(out_file), 3)


def test_engine_retranslate_failed_picks_up_cached_rows(tmp_path, monkeypatch):
    csv_file = tmp_path / "game.csv"
    csv_file.write_text("id,src,dst\n1,nothing is real?,nothing is real?\n2,hello,halo\n", encoding="utf-8")

    out_file = tmp_path / "game_Translated.csv"
    out_file.write_text("id,src,dst\n1,nothing is real?,nothing is real?\n2,hello,halo\n", encoding="utf-8")

    # Record row 2 as cached failed
    record_failed_rows(str(out_file), 3, [2])

    job = JobConfig(
        src_path=str(csv_file),
        out_path=str(out_file),
        src_col=2,
        dst_col=3,
        start_row=2,
        num_rows=2,
    )
    engine = ProcessingEngine(job)

    # Mock _run_translate_rows so it doesn't do network calls
    captured_failed = []
    def mock_run_translate_rows(rows, context_map, dst_col, total):
        captured_failed.extend(rows)
        from translator.core.models import ProcessStats
        return ProcessStats(total=len(rows), ok=len(rows))

    monkeypatch.setattr(engine, "_run_translate_rows", mock_run_translate_rows)

    stats, scan_results = engine.run_retranslate_failed(close_log=False)
    assert any(rn == 2 for rn, _ in captured_failed)
    assert not any(rn == 3 for rn, _ in captured_failed)


def test_engine_writes_fail_marker_when_configured(tmp_path, monkeypatch):
    csv_file = tmp_path / "game.csv"
    csv_file.write_text("id,src,dst\n1,nothing is real?,[TRANSLATE_FAILED]\n", encoding="utf-8")

    out_file = tmp_path / "game_Translated.csv"
    out_file.write_text("id,src,dst\n1,nothing is real?,\n", encoding="utf-8")

    monkeypatch.setattr("translator.data.app_prefs.failed_fallback_mode", lambda: "fail_marker")

    job = JobConfig(
        src_path=str(csv_file),
        out_path=str(out_file),
        src_col=2,
        dst_col=3,
        start_row=2,
        num_rows=1,
    )
    engine = ProcessingEngine(job)
    source_map = {2: "nothing is real?"}
    res = BatchResult(results={}, failed_rows=[2])
    engine._write_result(res, source_map)

    content = out_file.read_text(encoding="utf-8")
    assert "[TRANSLATE_FAILED]" in content
    assert 2 in load_failed_rows(str(out_file), 3)

