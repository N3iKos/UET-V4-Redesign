"""
tests/test_fitting_exports.py — public surface of the fitting package.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def test_exports():
    from translator import fitting
    for name in ("EngineProfile", "audit_rows", "apply_wrapped",
                 "detect_newline_tag", "fitting_line", "get_profile",
                 "wrap_metrics", "wrap_lines", "suggest_wrap"):
        assert hasattr(fitting, name), name

def test_calibration_documented():
    import inspect
    from translator.fitting import profiles
    assert "calibrat" in profiles.__doc__.lower()
