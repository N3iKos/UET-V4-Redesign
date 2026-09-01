"""
tests/test_model_priority_rank.py — Model priority rank data model and uniqueness validation.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.providers.models import Connection, ModelEntry, Provider
from translator.providers.registry import ProviderRegistry


def test_model_entry_default_priority():
    model = ModelEntry(id="m1", display_name="Model 1")
    assert getattr(model, "priority_rank", 0) == 0


def test_model_entry_dict_serialization():
    model = ModelEntry(id="m1", display_name="Model 1", priority_rank=2)
    d = model.to_dict()
    assert d.get("priority_rank") == 2
    loaded = ModelEntry.from_dict(d)
    assert loaded.priority_rank == 2


def test_validate_priority_rank_helper():
    from translator.tui.screens.providers import validate_priority_rank

    m1 = ModelEntry(id="m1", display_name="Model 1", priority_rank=1)
    m2 = ModelEntry(id="m2", display_name="Model 2", priority_rank=2)
    conn = Connection(id="c1", name="Conn 1", models=[m1, m2])
    prov = Provider(id="p1", name="Prov 1", connections=[conn])
    reg = ProviderRegistry()
    reg.providers = [prov]

    # Rank 0 is always allowed (means not a failover model)
    assert validate_priority_rank(reg, 0, current_model_id="m3") is None
    # Rank 3 is available
    assert validate_priority_rank(reg, 3, current_model_id="m3") is None
    # Rank 1 is already taken by Model 1
    err = validate_priority_rank(reg, 1, current_model_id="m3")
    assert err is not None
    assert "already assigned" in err or "Model 1" in err
    # Updating model 1 with its own rank 1 is allowed
    assert validate_priority_rank(reg, 1, current_model_id="m1") is None
