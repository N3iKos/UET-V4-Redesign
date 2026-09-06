"""
tests/test_judge_model_prefs.py — Per-model remembered Judge settings.

The store behind "Don't ask again for this model": each model slot keeps its
own rows/request and workers figures, persisted in the app preferences, so a
model whose settings were remembered runs immediately while an unremembered
one still asks. These tests pin the store's contract against a scratch file,
never the user's real preferences.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from translator import config
from translator.data import app_prefs

SLOT_A = ("provider-x", "conn-1", "model-a")
SLOT_B = ("provider-x", "conn-1", "model-b")
SLOT_A_ALT_CONN = ("provider-x", "conn-2", "model-a")


@pytest.fixture(autouse=True)
def scratch_prefs(tmp_path, monkeypatch):
    """Point the prefs file at a scratch path so no test touches the real one."""
    scratch = tmp_path / "app_prefs.json"
    monkeypatch.setattr(config, "APP_PREFS_FILE", str(scratch))
    monkeypatch.setattr(app_prefs.config, "APP_PREFS_FILE", str(scratch))
    yield scratch


class TestKey:
    def test_key_is_the_full_slot(self):
        key = app_prefs.judge_model_prefs_key(SLOT_A)
        assert key == "provider-x\nconn-1\nmodel-a"

    def test_same_model_on_another_connection_differs(self):
        # A model id is not unique across connections; remembering settings
        # for one must not apply them to the other.
        assert app_prefs.judge_model_prefs_key(SLOT_A) != \
            app_prefs.judge_model_prefs_key(SLOT_A_ALT_CONN)

    def test_unusable_slot_is_an_empty_key(self):
        assert app_prefs.judge_model_prefs_key(None) == ""


class TestRemember:
    def test_unremembered_model_has_no_settings(self):
        assert app_prefs.remembered_judge_settings(SLOT_A) is None

    def test_remembering_one_model_leaves_another_alone(self):
        app_prefs.remember_judge_settings(
            SLOT_A, max_rows=20, workers=8, remember=True
        )
        assert app_prefs.remembered_judge_settings(SLOT_A) == {
            "max_rows": 20, "workers": 8,
        }
        assert app_prefs.remembered_judge_settings(SLOT_B) is None

    def test_two_models_keep_separate_settings(self):
        app_prefs.remember_judge_settings(
            SLOT_A, max_rows=20, workers=8, remember=True
        )
        app_prefs.remember_judge_settings(
            SLOT_B, max_rows=5, workers=2, remember=True
        )
        assert app_prefs.remembered_judge_settings(SLOT_A)["max_rows"] == 20
        assert app_prefs.remembered_judge_settings(SLOT_B)["max_rows"] == 5

    def test_unremember_removes_the_entry(self):
        app_prefs.remember_judge_settings(
            SLOT_A, max_rows=20, workers=8, remember=True
        )
        app_prefs.remember_judge_settings(
            SLOT_A, max_rows=20, workers=8, remember=False
        )
        assert app_prefs.remembered_judge_settings(SLOT_A) is None

    def test_settings_survive_a_reload(self):
        # Persistence, not memory: a fresh process (or a reopen of the app)
        # must find the same settings.
        app_prefs.remember_judge_settings(
            SLOT_A, max_rows=20, workers=8, remember=True
        )
        # load_prefs reads from disk; nothing is cached between calls here.
        assert app_prefs.remembered_judge_settings(SLOT_A) == {
            "max_rows": 20, "workers": 8,
        }

    def test_values_are_clamped(self):
        app_prefs.remember_judge_settings(
            SLOT_A, max_rows=10_000, workers=999, remember=True
        )
        stored = app_prefs.remembered_judge_settings(SLOT_A)
        assert stored["workers"] <= 50
        assert stored["max_rows"] <= config.MAX_MAX_ROWS

    def test_hand_mangled_file_starts_empty(self):
        with open(config.APP_PREFS_FILE, "w", encoding="utf-8") as handle:
            handle.write('{"judge_model_prefs": "not a dict"}')
        assert app_prefs.remembered_judge_settings(SLOT_A) is None
        # And saving from that state does not crash.
        app_prefs.remember_judge_settings(
            SLOT_A, max_rows=10, workers=2, remember=True
        )
        assert app_prefs.remembered_judge_settings(SLOT_A)["workers"] == 2

    def test_other_prefs_are_untouched(self):
        app_prefs.remember_judge_settings(
            SLOT_A, max_rows=20, workers=8, remember=True
        )
        prefs = app_prefs.load_prefs()
        assert "request_timeout" in prefs
        assert prefs["request_timeout"] == app_prefs.request_timeout()


class TestRememberedJudgeCriteria:
    def test_default_is_four_criteria(self):
        # Default starts with 4 core criteria
        assert app_prefs.last_judge_criteria() == ["mistranslation", "context", "placeholder", "style"]

    def test_remember_five_criteria(self):
        app_prefs.set_last_judge_criteria(["mistranslation", "context", "placeholder", "style", "fitting"])
        assert len(app_prefs.last_judge_criteria()) == 5
        assert "fitting" in app_prefs.last_judge_criteria()

    def test_remember_three_criteria(self):
        app_prefs.set_last_judge_criteria(["mistranslation", "style", "fitting"])
        assert app_prefs.last_judge_criteria() == ["mistranslation", "style", "fitting"]

    def test_invalid_criteria_filtered_and_fallback(self):
        # Unknown criteria dropped
        assert app_prefs.set_last_judge_criteria(["mistranslation", "bogus"]) == ["mistranslation"]
        # Empty criteria falls back to default 4
        assert app_prefs.set_last_judge_criteria([]) == ["mistranslation", "context", "placeholder", "style"]

