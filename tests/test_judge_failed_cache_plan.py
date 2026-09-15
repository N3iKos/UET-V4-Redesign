from translator.judge.runner import JudgeRunner, JudgeTarget
from translator.core.failed_cache import record_failed_rows


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
