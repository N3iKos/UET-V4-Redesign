import os
import sys
import pytest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.tui.screens.settings import SettingsScreen, FailedFallbackModal
from translator.data.app_prefs import set_failed_fallback_mode, failed_fallback_mode

def test_settings_screen_has_fallback_option(monkeypatch, tmp_path):
    monkeypatch.setattr("translator.data.app_prefs._prefs_dir", lambda: tmp_path)
    screen = SettingsScreen()
    # verify option is rendered or accessible in settings screen options
    assert hasattr(screen, "_failed_fallback_picker") or any(
        "fallback" in str(getattr(opt, "value", "")) for opt in getattr(screen, "_options", [])
    )

def test_settings_screen_build_options_has_failed_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr("translator.data.app_prefs._prefs_dir", lambda: tmp_path)
    screen = SettingsScreen()
    options = screen.build_options()
    
    fallback_opts = [opt for opt in options if opt.value == "failed_fallback"]
    assert len(fallback_opts) == 1
    opt = fallback_opts[0]
    assert "Failed rows output" in opt.label

    # Verify it is in the TRANSLATION section (before _grp_judge)
    val_list = [o.value for o in options]
    assert val_list.index("failed_fallback") < val_list.index("_grp_judge")
    assert val_list.index("failed_fallback") > val_list.index("_grp_translate")

def test_settings_screen_fallback_hint_updates(monkeypatch, tmp_path):
    monkeypatch.setattr("translator.data.app_prefs._prefs_dir", lambda: tmp_path)
    set_failed_fallback_mode("copy_source")
    screen = SettingsScreen()
    opt = next(o for o in screen.build_options() if o.value == "failed_fallback")
    assert "Copy source (Game-safe)" in opt.hint

    set_failed_fallback_mode("fail_marker")
    opt2 = next(o for o in screen.build_options() if o.value == "failed_fallback")
    assert "Fail marker ([TRANSLATE_FAILED])" in opt2.hint

@pytest.mark.anyio
async def test_failed_fallback_modal_structure(monkeypatch, tmp_path):
    monkeypatch.setattr("translator.data.app_prefs._prefs_dir", lambda: tmp_path)
    from textual.app import App
    app = App()
    async with app.run_test() as pilot:
        modal = FailedFallbackModal("copy_source")
        await app.push_screen(modal)
        assert len(modal.picker.options) == 2
        assert modal.picker.options[0].value == "copy_source"
        assert "Copy source (Game-safe)" in modal.picker.options[0].label
        assert modal.picker.options[1].value == "fail_marker"
        assert "Fail marker ([TRANSLATE_FAILED])" in modal.picker.options[1].label

def test_settings_screen_done_callback(monkeypatch, tmp_path):
    monkeypatch.setattr("translator.data.app_prefs._prefs_dir", lambda: tmp_path)
    screen = SettingsScreen()
    screen.picker = MagicMock()
    screen.notify_status = MagicMock()

    screen._failed_fallback_done("fail_marker")
    assert failed_fallback_mode() == "fail_marker"
    screen.picker.set_options.assert_called_once()
