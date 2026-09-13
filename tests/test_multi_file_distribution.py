"""
tests/test_multi_file_distribution.py — Tests for leader inheritance and smart shared rate limit worker allocation.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.core.multi_file import (
    MultiFileDraftManager,
    distribute_workers_smart,
)


def test_leader_inheritance():
    mgr = MultiFileDraftManager(paths=["f1.csv", "f2.csv", "f3.csv"])
    assert mgr.leader_index == 0
    assert len(mgr.drafts) == 3

    # Update leader settings
    mgr.update_leader(src_col=2, dst_col=3, model_id="gemini-3-flash", provider_id="ag")
    
    # f2 and f3 inherit from leader because they don't have custom overrides
    assert mgr.get_draft(1)["src_col"] == 2
    assert mgr.get_draft(1)["dst_col"] == 3
    assert mgr.get_draft(1)["model_id"] == "gemini-3-flash"
    assert mgr.get_draft(2)["model_id"] == "gemini-3-flash"

    # Now customize f2 independently (non-leader override)
    mgr.update_file(1, model_id="claude-3-5-haiku", dst_col=4)
    assert mgr.get_draft(1)["model_id"] == "claude-3-5-haiku"
    assert mgr.get_draft(1)["dst_col"] == 4
    # Leader and f3 are unaffected by f2's change!
    assert mgr.get_draft(0)["model_id"] == "gemini-3-flash"
    assert mgr.get_draft(0)["dst_col"] == 3
    assert mgr.get_draft(2)["model_id"] == "gemini-3-flash"

    # Now if leader changes src_col again, f2 keeps its custom dst_col=4, but f3 gets the new setting
    mgr.update_leader(dst_col=5)
    assert mgr.get_draft(0)["dst_col"] == 5
    assert mgr.get_draft(2)["dst_col"] == 5
    assert mgr.get_draft(1)["dst_col"] == 4  # f2 custom preserved!


def test_smart_shared_rate_limit_different_models():
    # File 1 has Gemini (20 workers), File 2 has Claude (10 workers) -> Different models = Individual, NOT shared!
    drafts = [
        {"path": "f1.csv", "provider_id": "ag", "model_id": "gemini-3-flash", "n_workers": 20},
        {"path": "f2.csv", "provider_id": "anthropic", "model_id": "claude-3-5-haiku", "n_workers": 10},
    ]
    allocations = distribute_workers_smart(drafts)
    assert allocations == [20, 10]


def test_smart_shared_rate_limit_same_models():
    # File 1 and File 2 both use Gemini with 20 workers -> Same model = Automatically SHARED!
    drafts = [
        {"path": "f1.csv", "provider_id": "ag", "model_id": "gemini-3-flash", "n_workers": 20},
        {"path": "f2.csv", "provider_id": "ag", "model_id": "gemini-3-flash", "n_workers": 20},
    ]
    allocations = distribute_workers_smart(drafts)
    # 20 workers shared equally across 2 files = 10 each
    assert allocations == [10, 10]


def test_smart_shared_rate_limit_mixed():
    # 4 files: 3 use Gemini (30 total workers desired), 1 uses Claude (8 workers)
    drafts = [
        {"path": "f1.csv", "provider_id": "ag", "model_id": "gemini-3-flash", "n_workers": 30},
        {"path": "f2.csv", "provider_id": "ag", "model_id": "gemini-3-flash", "n_workers": 30},
        {"path": "f3.csv", "provider_id": "ag", "model_id": "gemini-3-flash", "n_workers": 30},
        {"path": "f4.csv", "provider_id": "anthropic", "model_id": "claude", "n_workers": 8},
    ]
    allocations = distribute_workers_smart(drafts)
    # Gemini files share 30 workers -> 10 each; Claude file is individual -> 8
    assert allocations == [10, 10, 10, 8]
