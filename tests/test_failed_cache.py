# tests/test_failed_cache.py
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.core.failed_cache import (
    failed_cache_path,
    load_failed_rows,
    record_failed_rows,
    clear_failed_rows,
)

def test_failed_cache_lifecycle(tmp_path):
    out_file = tmp_path / "game_Translated.csv"
    out_file.write_text("id,src,dst\n", encoding="utf-8")
    path_str = str(out_file)

    assert load_failed_rows(path_str, 3) == set()

    record_failed_rows(path_str, 3, [824, 825])
    assert load_failed_rows(path_str, 3) == {824, 825}

    # Adding more
    record_failed_rows(path_str, 3, [826])
    assert load_failed_rows(path_str, 3) == {824, 825, 826}

    # Clearing resolved
    clear_failed_rows(path_str, 3, [824])
    assert load_failed_rows(path_str, 3) == {825, 826}


def test_failed_cache_path_no_nesting(tmp_path):
    cache_dir = tmp_path / ".uet_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    unique_file = cache_dir / "Dialog.1234abcd.unique.csv"
    p = failed_cache_path(str(unique_file), 3)
    assert ".uet_cache/.uet_cache" not in p.replace("\\", "/")
    assert p == str(cache_dir / "Dialog_col3_failed.json")

    unique_no_hash = cache_dir / "Dialog.unique.csv"
    p2 = failed_cache_path(str(unique_no_hash), 3)
    assert ".uet_cache/.uet_cache" not in p2.replace("\\", "/")
    assert p2 == str(cache_dir / "Dialog_col3_failed.json")


def test_atomic_write_no_leftover_tmp(tmp_path):
    out_file = tmp_path / "game_Translated.csv"
    out_file.write_text("id,src,dst\n", encoding="utf-8")
    path_str = str(out_file)

    record_failed_rows(path_str, 3, [10, 20])
    cache_dir = tmp_path / ".uet_cache"
    assert cache_dir.exists()
    tmp_files = list(cache_dir.glob("*.tmp"))
    assert len(tmp_files) == 0

    clear_failed_rows(path_str, 3, [10])
    tmp_files = list(cache_dir.glob("*.tmp"))
    assert len(tmp_files) == 0
    assert load_failed_rows(path_str, 3) == {20}
