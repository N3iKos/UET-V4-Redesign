import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.judge.runner import JudgeRunner, JudgeTarget
from translator.core.failed_cache import load_failed_rows, record_failed_rows


def test_judge_plan_flags_cache_recorded_failed_rows(tmp_path):
    csv_file = tmp_path / "game_Translated.csv"
    csv_file.write_text("id,src,dst\n1,nothing is real?,nothing is real?\n", encoding="utf-8")

    record_failed_rows(str(csv_file), 3, [2])

    target = JudgeTarget(path=str(csv_file), src_col=2, dst_col=3, start_row=2, num_rows=1)
    runner = JudgeRunner(target)
    plan = runner.plan()

    assert 2 in plan.prejudged
    assert "failed" in plan.prejudged[2].error_types
    # Row 2 is excluded from askable sections!
    assert len(plan.sections) == 0


def test_judge_rechain_plan_flags_cache_recorded_failed_rows(tmp_path):
    csv_file = tmp_path / "game_Translated.csv"
    csv_file.write_text("id,src,dst\n1,nothing is real?,nothing is real?\n", encoding="utf-8")

    record_failed_rows(str(csv_file), 3, [2])

    target = JudgeTarget(path=str(csv_file), src_col=2, dst_col=3, start_row=2, num_rows=1)
    runner = JudgeRunner(target)
    plan = runner.rechain_plan([(2, "nothing is real?", "nothing is real?")])

    assert 2 in plan.prejudged
    assert "failed" in plan.prejudged[2].error_types
    assert len(plan.sections) == 0


def test_judge_plan_does_not_flag_if_translation_differs(tmp_path):
    csv_file = tmp_path / "game_Translated.csv"
    csv_file.write_text("id,src,dst\n1,nothing is real?,tidak ada yang nyata?\n", encoding="utf-8")

    record_failed_rows(str(csv_file), 3, [2])

    target = JudgeTarget(path=str(csv_file), src_col=2, dst_col=3, start_row=2, num_rows=1)
    runner = JudgeRunner(target)
    plan = runner.plan()

    assert 2 not in plan.prejudged
    assert len(plan.sections) == 1
    assert plan.sections[0][0][0] == 2


def test_judge_rechain_plan_does_not_flag_if_translation_differs(tmp_path):
    csv_file = tmp_path / "game_Translated.csv"
    csv_file.write_text("id,src,dst\n1,nothing is real?,tidak ada yang nyata?\n", encoding="utf-8")

    record_failed_rows(str(csv_file), 3, [2])

    target = JudgeTarget(path=str(csv_file), src_col=2, dst_col=3, start_row=2, num_rows=1)
    runner = JudgeRunner(target)
    plan = runner.rechain_plan([(2, "nothing is real?", "tidak ada yang nyata?")])

    assert 2 not in plan.prejudged
    assert len(plan.sections) == 1


def test_judge_apply_fix_clears_failed_cache(tmp_path):
    csv_file = tmp_path / "game_Translated.csv"
    csv_file.write_text("id,src,dst\n1,nothing is real?,nothing is real?\n", encoding="utf-8")

    record_failed_rows(str(csv_file), 3, [2])
    assert load_failed_rows(str(csv_file), 3) == {2}

    target = JudgeTarget(path=str(csv_file), src_col=2, dst_col=3, start_row=2, num_rows=1)
    runner = JudgeRunner(target)
    count = runner.apply_fix({2: "tidak ada yang nyata?"})
    assert count == 1
    assert load_failed_rows(str(csv_file), 3) == set()


def test_judge_apply_fix_clears_full_path_cache_with_dedup_mapping(tmp_path, monkeypatch):
    cache_dir = tmp_path / ".uet_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    unique_file = cache_dir / "game.1234abcd.unique.csv"
    unique_file.write_text("id,src,dst\n1,apple,apple\n", encoding="utf-8")

    full_file = tmp_path / "game_Translated.csv"
    full_file.write_text("id,src,dst\n1,apple,apple\n2,banana,pisang\n3,orange,jeruk\n4,apple,apple\n", encoding="utf-8")

    map_file = cache_dir / "game.1234abcd.map.json"
    map_file.write_text(
        json.dumps({
            "version": 1,
            "original_file": str(full_file),
            "source_fingerprint": "fake",
            "unique_file": str(unique_file),
            "src_col": 2,
            "dst_col": 3,
            "source_texts": ["apple"],
            "rep_to_full": {
                "2": [2, 5],
            },
        }),
        encoding="utf-8",
    )

    record_failed_rows(str(unique_file), 3, [2])
    record_failed_rows(str(full_file), 3, [2, 5])
    assert load_failed_rows(str(unique_file), 3) == {2}
    assert load_failed_rows(str(full_file), 3) == {2, 5}

    monkeypatch.setattr("translator.core.dedup_file.sync_unique_to_full", lambda *args, **kwargs: 1)

    target = JudgeTarget(
        path=str(unique_file),
        full_path=str(full_file),
        map_path=str(map_file),
        src_col=2,
        dst_col=3,
        start_row=2,
        num_rows=1,
    )
    runner = JudgeRunner(target)
    count = runner.apply_fix({2: "apel"})
    assert count == 1
    assert load_failed_rows(str(unique_file), 3) == set()
    assert load_failed_rows(str(full_file), 3) == set()
