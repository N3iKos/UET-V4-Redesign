"""
tests/test_failover_prefs.py — Priority failover settings in app_prefs.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from translator import config
from translator.data import app_prefs


@pytest.fixture(autouse=True)
def scratch_prefs(tmp_path, monkeypatch):
    scratch = tmp_path / "app_prefs.json"
    monkeypatch.setattr(config, "APP_PREFS_FILE", str(scratch))
    monkeypatch.setattr(app_prefs.config, "APP_PREFS_FILE", str(scratch))
    yield scratch


def test_failover_defaults():
    assert app_prefs.priority_failover_enabled() is True
    assert app_prefs.failover_retry_threshold() == 3


def test_failover_setters_and_clamping():
    app_prefs.set_priority_failover_enabled(False)
    assert app_prefs.priority_failover_enabled() is False

    app_prefs.set_failover_retry_threshold(10)
    assert app_prefs.failover_retry_threshold() == 5  # clamped max 5

    app_prefs.set_failover_retry_threshold(0)
    assert app_prefs.failover_retry_threshold() == 1  # clamped min 1

    app_prefs.set_failover_retry_threshold(2)
    assert app_prefs.failover_retry_threshold() == 2
