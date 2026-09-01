"""
tests/test_worker_line_model_display.py — Test rendering model names in the workers panel.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.scheduling.scheduler import WorkerStatus
from translator.tui.widgets.activity import detail_lines, worker_line


def test_worker_line_shows_model_name():
    status = WorkerStatus(worker_id=1, model_name="gemma4:31b", batch_num=5, connection="Main")
    line = worker_line(status)
    assert "gemma4:31b" in line.plain or "gemma4" in line.plain


def test_worker_line_shows_failover_badge():
    status = WorkerStatus(
        worker_id=2, model_name="deepseek-v4", failover_rank=1, batch_num=5, connection="FailoverConn"
    )
    line = worker_line(status)
    assert "P1" in line.plain or "[P1]" in line.plain


def test_detail_lines_shows_model_and_failover():
    status = WorkerStatus(
        worker_id=2, model_name="deepseek-v4", failover_rank=1, batch_num=5, connection="FailoverConn"
    )
    details = detail_lines(status)
    plain_text = "\n".join(l.plain for l in details)
    assert "deepseek-v4" in plain_text
    assert "Failover" in plain_text or "P1" in plain_text
