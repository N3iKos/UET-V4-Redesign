from __future__ import annotations

import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.parsing.recovery import is_error_output, ParseRecoveryPipeline
from translator.judge.parser import parse_fixed_line
from translator.judge.models import JudgeReport, RowIssue, JudgeSummary
from translator.judge.prompts import (
    _JUDGE_SYS,
    _JUDGE_SYS_CHAIN,
    build_judge_rechain_prompt,
    build_fix_prompt,
    build_fix_solo_prompt,
)
from translator.tui.screens.judge import FixAllSetupScreen
from translator.judge.runner import JudgeTarget, JudgeRunner
from translator import config
from translator.data import app_prefs


def test_is_error_output_detects_common_errors():
    assert is_error_output("Error: 504 Gateway Timeout")
    assert is_error_output("Request timed out or failed to evaluate. Re-judge to retry.")
    assert is_error_output("Rate limit reached. Please wait.")
    assert is_error_output("[TRANSLATE_FAILED]")
    assert is_error_output("500 Internal Server Error")
    assert is_error_output("502 Bad Gateway")
    assert is_error_output("I cannot fulfill this request due to safety guidelines")
    assert is_error_output('{"error": {"message": "Invalid API key"}}')

    # Valid translations must not be flagged as errors
    assert not is_error_output("Tidak ada cukup waktu untuk mencabutnya.")
    assert not is_error_output("Meskipun ekspresi Xao tidak berubah, bicaranya cepat.")
    assert not is_error_output("Plok... Crot Crot")


def test_parser_ignores_error_outputs():
    pipeline = ParseRecoveryPipeline()
    # When single row is an error, recover returns empty dict
    res_err = pipeline.recover("[1] Error: Request timed out", [1])
    assert res_err == {}

    # recover_best_effort also excludes the error row
    raw = "[1] Error: Request timed out\n[2] Valid translation line"
    res = pipeline.recover_best_effort(raw, [1, 2])
    assert 1 not in res
    assert res[2] == "Valid translation line"

    # parse_fixed_line returns empty if model output is an error message
    assert parse_fixed_line("Request timed out or failed to evaluate. Re-judge to retry.") == ""
    assert parse_fixed_line("504 Gateway Timeout") == ""
    assert parse_fixed_line("[1] Corrected text") == "Corrected text"


def test_fix_all_matches_by_severity_alone():
    report = JudgeReport()
    report.issues[962] = RowIssue(row=962, error_types=["unjudged"], severity="moderate", comment="Req timed out")
    report.issues[963] = RowIssue(row=963, error_types=["mistranslation"], severity="moderate", comment="Mistranslated")
    report.issues[964] = RowIssue(row=964, error_types=["style"], severity="minor", comment="Style issue")

    # Match by severity Moderate with empty/None criteria
    matching = report.rows_matching(criteria=None, severities=["moderate"])
    assert set(matching) == {962, 963}

    matching_empty_crit = report.rows_matching(criteria=[], severities=["moderate"])
    assert set(matching_empty_crit) == {962, 963}


def test_rejudge_prefs_isolated_from_fix_prefs(tmp_path, monkeypatch):
    test_prefs_file = str(tmp_path / "app_prefs.json")
    monkeypatch.setattr(config, "APP_PREFS_FILE", test_prefs_file)

    slot = ("openai", "conn1", "gpt-4o")

    # Remember fix settings
    app_prefs.remember_fix_settings(slot, max_rows=15, workers=3, remember=True)

    # Rejudge settings must still be None
    assert app_prefs.remembered_rejudge_settings(slot) is None
    fix_st = app_prefs.remembered_fix_settings(slot)
    assert fix_st is not None
    assert fix_st["max_rows"] == 15
    assert fix_st["workers"] == 3

    # Remember rejudge settings separately
    app_prefs.remember_rejudge_settings(slot, max_rows=25, workers=5, remember=True)
    rejudge_st = app_prefs.remembered_rejudge_settings(slot)
    assert rejudge_st is not None
    assert rejudge_st["max_rows"] == 25
    assert rejudge_st["workers"] == 5

    # Fix settings must remain unchanged
    fix_st_after = app_prefs.remembered_fix_settings(slot)
    assert fix_st_after["max_rows"] == 15
    assert fix_st_after["workers"] == 3


def test_rejudge_prompt_strictly_requires_flagging_remaining_defects():
    for sys_prompt in (_JUDGE_SYS, _JUDGE_SYS_CHAIN):
        assert "typo" in sys_prompt.lower()
        assert "YOU MUST FLAG IT AGAIN" in sys_prompt
        assert "OBJECTIVE ERROR THRESHOLD" in sys_prompt

    rechain = build_judge_rechain_prompt(
        rows=[(1, "source", "trans")],
        neighbours={},
        criteria=["mistranslation"],
    )
    assert "RE-JUDGE STRICTNESS" in rechain


def test_build_fix_prompt_handles_empty_translation_rows():
    rows = [(1, "Hello adventurer!", "")]
    comments = {1: "The target cell is empty — translate from original source."}
    prompt = build_fix_prompt(
        rows=rows,
        comments=comments,
        tgt_lang="Indonesian",
        criteria=["empty"],
    )
    assert "Empty cell — translate the original SOURCE into Indonesian." in prompt
    assert "- If a line has an empty TRANSLATION (or note indicates an empty cell or failed translation), translate the SOURCE text directly into Indonesian following the style and context rules." in prompt
    assert "- For empty rows (or rows where translation is missing): translate the SOURCE directly into the target language." in prompt


def test_build_fix_solo_prompt_handles_empty_translation():
    prompt = build_fix_solo_prompt(
        row=5,
        source="Good morning!",
        translation="",
        comment="",
        tgt_lang="Indonesian",
        criteria=["empty"],
    )
    assert "Empty cell — translate the original SOURCE into Indonesian." in prompt
    assert "- If a line has an empty TRANSLATION (or note indicates an empty cell or failed translation), translate the SOURCE text directly into Indonesian following the style and context rules." in prompt
    assert "Leave anything else about the line exactly as it is." not in prompt


def test_fix_all_setup_screen_includes_empty_criteria():
    report = JudgeReport()
    report.issues[1] = RowIssue(
        row=1,
        error_types=["empty"],
        severity="critical",
        comment="The target cell is empty — translate from original source.",
    )
    report.summary.distribution["empty"] = 1
    report.summary.severity_counts["critical"] = 1

    target = JudgeTarget(path="dummy_Translated.csv", src_col=1, dst_col=2)
    screen = FixAllSetupScreen(
        report=report,
        rows=[1],
        model_slot=("openai", "conn", "gpt-4o"),
        target=target,
    )
    options = screen._criteria_options()
    empty_opt = next((opt for opt in options if opt.value == "empty"), None)
    assert empty_opt is not None
    assert empty_opt.label == "Empty rows — translate from original source"
    assert empty_opt.selected is True


def test_judge_plan_empty_row_comment(tmp_path):
    csv_file = tmp_path / "test_empty_Translated.csv"
    csv_file.write_text("id,src,dst\n1,Hello,\n", encoding="utf-8")

    target = JudgeTarget(path=str(csv_file), src_col=2, dst_col=3, start_row=2, num_rows=1)
    runner = JudgeRunner(target)
    plan = runner.plan()
    assert 2 in plan.prejudged
    assert plan.prejudged[2].error_types == ["empty"]
    assert plan.prejudged[2].comment == "The target cell is empty — translate from original source."

    rechain = runner.rechain_plan([(2, "Hello", "")])
    assert 2 in rechain.prejudged
    assert rechain.prejudged[2].error_types == ["empty"]
    assert rechain.prejudged[2].comment == "The target cell is empty — translate from original source."


def test_judge_fix_single_ensures_empty_criterion(monkeypatch):
    target = JudgeTarget(path="dummy_Translated.csv", src_col=1, dst_col=2, tgt_lang="Indonesian")
    runner = JudgeRunner(target, criteria=["mistranslation"])

    captured_prompt = None

    def fake_one_request(prompt, label=""):
        nonlocal captured_prompt
        captured_prompt = prompt
        return "[1] Halo dunia!", None

    monkeypatch.setattr(runner, "_one_request", fake_one_request)

    res = runner.fix_single(
        row=1,
        source="Hello world!",
        translation="",
        comment="The target cell is empty — translate from original source.",
    )
    assert not res.error
    assert res.text == "Halo dunia!"
    assert captured_prompt is not None
    assert "- For empty rows (or rows where translation is missing): translate the SOURCE directly into the target language." in captured_prompt


