"""
tests/test_fitting_profiles.py — Engine profiles: registry, metrics, gating.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.fitting.profiles import (
    ENGINE_LABELS, ENGINE_ORDER, get_profile,
)

def test_general_is_not_a_profile():
    # "general" is an explicit OFF mode, not an EngineProfile object (spec 2.2).
    assert get_profile("general") is None

def test_unknown_is_none():
    assert get_profile("nope") is None

def test_rpgm_mv_mz_shape():
    p = get_profile("rpgm-mv-mz")
    assert p is not None
    assert p.max_lines == 4
    assert p.safe_margin == 0.90
    assert p.window_cols > 0
    assert p.verified is True
    # CJK must be measured — JP sources are the majority case (spec 1.1).
    assert p.char_widths["cjk"] == 1.0
    assert p.char_widths["latin"] == 0.5

def test_every_order_entry_has_label_and_inverse():
    for name in ENGINE_ORDER:
        assert name in ENGINE_LABELS
        assert get_profile(name) is not None or name == "general"
    assert set(ENGINE_LABELS) == set(ENGINE_ORDER)

def test_token_patterns_match_rpgm_codes():
    p = get_profile("rpgm-mv-mz")
    import re
    for sample in (r"\C[12]", r"\V[3]", r"\N[1]", r"\FS[24]", r"\.", r"\|",
                   r"\!", r"\>", r"\^", r"\{", r"\}"):
        assert any(re.search(pat, sample) for pat in p.token_patterns), sample

def test_unverified_profiles_flagged():
    # rpgm-2000 ships best-effort; UI marks it so users are not lied to (spec 1.1).
    assert get_profile("rpgm-2000").verified is False
    assert get_profile("kirikiri").verified is True
