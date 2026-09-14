"""
tests/test_judge_conciseness.py — conciseness & length fitting in JudgeRunner.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.judge.runner import JudgeRunner, JudgeTarget


def _runner(tmp_path):
    return JudgeRunner(
        JudgeTarget(path=str(tmp_path / "x.csv"), src_col=2, dst_col=3),
    )


def test_fitting_criteria_always_available(tmp_path):
    r = _runner(tmp_path)
    assert "fitting" in r.criteria


def test_fitting_map_calculates_conciseness(tmp_path):
    r = _runner(tmp_path)
    # Verbose line with >1.4x length expansion gets a conciseness note
    f_map = r._fitting_map([(1, "cat", "kucing yang sangat besar sekali dan luar biasa panjang")])
    assert 1 in f_map
    assert "CONCISENESS" in f_map[1]
    assert "3 chars" in f_map[1]


def test_fitting_map_ignores_concise_lines(tmp_path):
    r = _runner(tmp_path)
    # 1.1x ratio -> normal translation, no note needed
    f_map = r._fitting_map([(1, "hello world", "halo dunia!")])
    assert 1 not in f_map


def test_revalidate_pass(tmp_path):
    r = _runner(tmp_path)
    assert r.revalidate_fix(1, "Halo", "Hello") is None


def test_revalidate_detects_excessive_length(tmp_path):
    r = _runner(tmp_path)
    overlong = "teks perbaikan ini luar biasa panjang sekali dan sangat bertele-tele melebihi sumber aslinya sampai ratusan karakter panjangnya"
    assert r.revalidate_fix(1, overlong, "cat") == "excessive-length"


def test_fix_all_fitting_retry_does_not_crash_on_profile(tmp_path):
    # Tests that when a fitting retry occurs in fix_all, it does not crash on self._profile
    csv_file = tmp_path / "x.csv"
    csv_file.write_text("id,source,trans\n1,cat,kucing\n", encoding="utf-8")

    class DummyClient:
        def __init__(self):
            self.calls = 0
        def generate(self, model, prompt, **kwargs):
            self.calls += 1
            if self.calls == 1:
                # Return excessively long text to trigger fitting rejection
                return type("R", (), {"content": "[2] " + "a" * 200, "tokens": 10})()
            # Second call returns concise text
            return type("R", (), {"content": "[2] short text", "tokens": 10})()

    target = JudgeTarget(path=str(csv_file), src_col=2, dst_col=3, start_row=2)
    runner = JudgeRunner(
        target,
        criteria=["fitting"],
        client_factory=lambda: DummyClient(),
    )
    # Run fix_all with row 2
    staged = runner.fix_all(
        rows=[2],
        comments={2: "WHAT: long · WHY: bad · FIX: shorten"},
        criteria=["fitting"],
    )
    assert 2 in staged.texts
    assert staged.texts[2] == "short text"


def test_staged_fix_cache_autosaves_and_loads(tmp_path):
    csv_file = tmp_path / "test_cache.csv"
    csv_file.write_text("id,source,trans\n1,dog,anjing\n", encoding="utf-8")

    class DummyClient:
        def generate(self, model, prompt, **kwargs):
            return type("R", (), {"content": "[2] anjing setia", "tokens": 10})()

    target = JudgeTarget(path=str(csv_file), src_col=2, dst_col=3, start_row=2)
    runner = JudgeRunner(target, client_factory=lambda: DummyClient())
    staged = runner.fix_all(rows=[2], comments={2: "note"})
    assert 2 in staged.texts

    # Verify autosave cache was created
    loaded = runner.load_staged_cache()
    assert loaded is not None
    assert loaded.texts.get(2) == "anjing setia"


def test_judge_runner_apply_fix_without_unique_mapping_writes_successfully(tmp_path):
    csv_file = tmp_path / "Main_Story_Translated.csv"
    csv_file.write_text("id,source,trans\n1,scars,luka\n", encoding="utf-8")

    # Target has path == full_path, and map_path is empty or points to a non-existent map
    target = JudgeTarget(
        path=str(csv_file),
        full_path=str(csv_file),
        map_path="",
        src_col=2,
        dst_col=3,
        start_row=2,
    )
    runner = JudgeRunner(target)
    # Apply fix should write directly to the CSV file without ValueError: Unique file does not match mapping
    applied_count = runner.apply_fix({2: "bekas luka"})
    assert applied_count == 1
    content = csv_file.read_text(encoding="utf-8")
    assert "bekas luka" in content


def test_judge_runner_apply_fix_ignores_stale_leaked_map_path(tmp_path):
    # If target has a leaked map_path from another file, but target.path == target.full_path,
    # it must NOT crash with ValueError: Unique file does not match mapping!
    csv_file = tmp_path / "Main_Story_Translated.csv"
    csv_file.write_text("id,source,trans\n1,scars,luka\n", encoding="utf-8")

    fake_map = tmp_path / "other_file.map.json"
    fake_map.write_text("{}", encoding="utf-8")

    target = JudgeTarget(
        path=str(csv_file),
        full_path=str(csv_file),
        map_path=str(fake_map),
        src_col=2,
        dst_col=3,
        start_row=2,
    )
    runner = JudgeRunner(target)
    applied_count = runner.apply_fix({2: "bekas luka"})
    assert applied_count == 1
    content = csv_file.read_text(encoding="utf-8")
    assert "bekas luka" in content





