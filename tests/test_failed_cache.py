# tests/test_failed_cache.py
import os
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
