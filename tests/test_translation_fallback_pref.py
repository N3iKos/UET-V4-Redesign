import pytest
from translator.data.app_prefs import (
    failed_fallback_mode,
    set_failed_fallback_mode,
)

def test_failed_fallback_mode_defaults_to_copy_source(monkeypatch, tmp_path):
    monkeypatch.setattr("translator.data.app_prefs._prefs_dir", lambda: tmp_path)
    assert failed_fallback_mode() == "copy_source"

def test_set_failed_fallback_mode_persists(monkeypatch, tmp_path):
    monkeypatch.setattr("translator.data.app_prefs._prefs_dir", lambda: tmp_path)
    set_failed_fallback_mode("fail_marker")
    assert failed_fallback_mode() == "fail_marker"
    set_failed_fallback_mode("copy_source")
    assert failed_fallback_mode() == "copy_source"
