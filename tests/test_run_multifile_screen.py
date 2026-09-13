"""
tests/test_run_multifile_screen.py — Multi-tab session state and switching in RunScreen (Translate mode).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.core.models import JobConfig
from translator.tui.screens.run import RunScreen


def test_run_screen_multi_tab_initialization(tmp_path):
    f1 = tmp_path / "a.csv"
    f2 = tmp_path / "b.csv"
    f1.write_text("id,src\n1,cat\n", encoding="utf-8")
    f2.write_text("id,src\n1,dog\n", encoding="utf-8")

    j1 = JobConfig(provider_id="p", model_id="m", src_path=str(f1), out_path=str(tmp_path / "a_out.csv"))
    j2 = JobConfig(provider_id="p", model_id="m", src_path=str(f2), out_path=str(tmp_path / "b_out.csv"))

    screen = RunScreen(
        job=j1,
        jobs=[j1, j2],
    )

    assert screen.is_multi_tab is True
    assert len(screen.sessions) == 2
    assert screen.active_session_index == 0
    assert screen.job.src_path == str(f1)

    # Switch to tab 2
    screen.action_switch_tab(1)
    assert screen.active_session_index == 1
    assert screen.job.src_path == str(f2)

    # Switch back to tab 1
    screen.action_switch_tab(0)
    assert screen.active_session_index == 0
    assert screen.job.src_path == str(f1)


def test_run_screen_session_isolation(tmp_path):
    f1 = tmp_path / "a.csv"
    f2 = tmp_path / "b.csv"
    f1.write_text("id,src\n1,cat\n", encoding="utf-8")
    f2.write_text("id,src\n1,dog\n", encoding="utf-8")

    from translator.core.models import ProcessStats
    j1 = JobConfig(provider_id="p", model_id="m", src_path=str(f1), out_path=str(tmp_path / "a_out.csv"))
    j2 = JobConfig(provider_id="p", model_id="m", src_path=str(f2), out_path=str(tmp_path / "b_out.csv"))

    screen = RunScreen(job=j1, jobs=[j1, j2])

    # Tab 0: 50 rows done
    screen._done_rows = 50
    screen._stats = ProcessStats(ok=50, total=100)

    # Switch to tab 1
    screen.action_switch_tab(1)
    assert screen._done_rows == 0
    assert screen._stats.ok == 0

    # Tab 1: 20 rows done
    screen._done_rows = 20
    screen._stats = ProcessStats(ok=20, total=100)

    # Switch back to tab 0: tab 0's progress is preserved!
    screen.action_switch_tab(0)
    assert screen._done_rows == 50
    assert screen._stats.ok == 50
    assert screen.job.src_path == str(f1)

    # Switch to tab 1: tab 1's progress is preserved!
    screen.action_switch_tab(1)
    assert screen._done_rows == 20
    assert screen._stats.ok == 20
    assert screen.job.src_path == str(f2)

