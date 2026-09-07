"""
tests/test_dedup.py — Translation & Judge deduplication and TM tests.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.core.engine import ProcessingEngine
from translator.core.models import JobConfig
from translator.core.tm import lookup_tm_batch, store_tm_batch
from translator.data import app_prefs
from translator.judge.models import JudgeReport, JudgeResult, RowIssue
from translator.judge.runner import JudgeRunner, JudgeTarget


def test_dedup_prefs():
    assert app_prefs.dedup_mode() in ("unique_file", "persistent", "disabled")
    app_prefs.set_dedup_mode("unique_file")
    assert app_prefs.dedup_mode() == "unique_file"


def test_judge_plan_deduplication():
    # 5 rows with 2 unique pairs
    rows = [
        (1, "Hello", "Halo"),
        (2, "Hello", "Halo"),
        (3, "Good morning", "Selamat pagi"),
        (4, "Hello", "Halo"),
        (5, "Good morning", "Selamat pagi"),
    ]
    target = JudgeTarget(path="dummy.csv", src_col=1, dst_col=2)
    runner = JudgeRunner(target)

    # Deduplication active
    app_prefs.set_dedup_mode("unique_file")
    plan = runner.plan(rows)

    # Total rows in plan should still be 5
    assert len(plan.rows) == 5

    # But sections should only contain the 2 unique rows!
    batched_rows = [r[0] for section in plan.sections for r in section]
    assert len(batched_rows) == 2
    assert set(batched_rows) == {1, 3}

    # Verify siblings were mapped
    assert hasattr(runner, "_judge_siblings")
    assert runner._judge_siblings[1] == [2, 4]
    assert runner._judge_siblings[3] == [5]


def test_judge_dedup_disabled():
    rows = [
        (1, "Hello", "Halo"),
        (2, "Hello", "Halo"),
    ]
    target = JudgeTarget(path="dummy.csv", src_col=1, dst_col=2)
    runner = JudgeRunner(target)

    app_prefs.set_dedup_mode("disabled")
    plan = runner.plan(rows)

    batched_rows = [r[0] for section in plan.sections for r in section]
    assert len(batched_rows) == 2
    assert getattr(runner, "_judge_siblings", {}) == {}

    app_prefs.set_dedup_mode("unique_file")


def test_engine_sibling_expansion(tmp_path):
    # Test that _write_result expands sibling rows
    csv_in = str(tmp_path / "in.csv")
    csv_out = str(tmp_path / "out.csv")
    open(csv_in, "w").write("header\na\n")
    open(csv_out, "w").write("header\na\n")
    job = JobConfig(
        provider_id="p", model_id="m", connection_id="c",
        src_path=csv_in, out_path=csv_out,
        src_col=1, dst_col=2, start_row=1, num_rows=4, batch_size=10,
    )

    class DummyWriter:
        def __init__(self):
            self.flushed = {}
        def flush(self, payload, dst_col):
            self.flushed.update(payload)
        def close(self): pass

    engine = ProcessingEngine(job, provider=None, connection_pool=object())
    engine._writer = DummyWriter()
    engine._dst_col = 2
    engine._sibling_map = {1: [1, 2, 4]}

    from translator.core.models import BatchResult
    from translator.scheduling.scheduler import BatchTask
    task = BatchTask(batch_num=1, total_batches=1, rows=[(1, "Hello")])
    result = BatchResult(results={1: "Halo"})

    engine._write_result(task, result)

    # Sibling rows 2 and 4 must have received the same translation!
    assert engine._writer.flushed[1] == "Halo"
    assert engine._writer.flushed[2] == "Halo"
    assert engine._writer.flushed[4] == "Halo"
