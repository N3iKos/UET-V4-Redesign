"""
tests/test_judge_parser.py — Judge response parsing.

Covers the two regressions the output-format work guards:
  * a reply that is valid Judge JSON *inside* prose/wrappers is recovered, and
  * free prose is never turned into a verdict — it is unparsed, and the caller
    retries instead of scoring anything.

Run: python -m pytest tests/test_judge_parser.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.judge.parser import parse_evaluation
from translator.judge.models import JudgeResult


def _plain():
    return (
        '{"lines": [{"row": 7, "error_types": ["mistranslation"], '
        '"severity": "critical", "comment": "negation dropped"}]}'
    )


def test_plain_json_parses():
    result = parse_evaluation(_plain())
    assert not result.unparsed
    assert set(result.rows_seen) == {7}
    assert 7 in result.issues


def test_clean_reply_marks_rows_seen_and_no_issues():
    result = parse_evaluation('{"lines": []}')
    assert not result.unparsed
    assert result.rows_seen == set()
    assert not result.issues


def test_prose_is_unparsed_not_guessed():
    result = parse_evaluation(
        "Row 7 is a mistranslation, the negation was dropped. "
        "Row 9 should be replaced."
    )
    assert result.unparsed
    assert not result.issues


def test_fenced_json_recovered():
    result = parse_evaluation("```json\n" + _plain() + "\n```")
    assert not result.unparsed
    assert 7 in result.issues


def test_prose_wrapped_json_recovered():
    result = parse_evaluation(
        "Here is my review.\n" + _plain() + "\nHope that helps!"
    )
    assert not result.unparsed
    assert 7 in result.issues


def test_trailing_commas_recovered():
    reply = (
        '{"lines": ['
        '{"row": 3, "error_types": ["placeholder"], "comment": "tag dropped"},'
        '],}'
    )
    result = parse_evaluation(reply)
    assert not result.unparsed
    assert 3 in result.issues


def test_single_quoted_json_recovered():
    reply = (
        "{'lines': [{'row': 5, 'error_types': ['style'], "
        "'severity': 'minor', 'comment': 'register shifted'}]}"
    )
    result = parse_evaluation(reply)
    assert not result.unparsed
    assert 5 in result.issues


def test_empty_lines_is_a_real_answer():
    result = parse_evaluation("")
    assert result.unparsed


def test_row_number_always_seen_when_flagged():
    result = parse_evaluation(_plain())
    assert 7 in result.rows_seen
    assert result.issues[7].row == 7


def test_parse_fixed_line_preserves_source_number():
    from translator.judge.parser import parse_fixed_line
    # When source text starts with a number like "1. ", it must NOT be stripped!
    res = parse_fixed_line("1. Tim Kelinci dapat dipanggil.", source="1. The Rabbit Team...")
    assert res == "1. Tim Kelinci dapat dipanggil."


def test_parse_fixed_line_strips_hallucinated_number():
    from translator.judge.parser import parse_fixed_line
    # When source text does NOT have a number, strip hallucinated bullet number
    res = parse_fixed_line("1. Tim Kelinci dapat dipanggil.", source="The Rabbit Team...")
    assert res == "Tim Kelinci dapat dipanggil."


def test_recovery_pipeline_preserves_source_number():
    from translator.parsing.recovery import ParseRecoveryPipeline
    pipeline = ParseRecoveryPipeline()
    reply = "[5364] 1. Tim Kelinci dapat dipanggil sekali sehari.\n[100] 2. Teks lain"
    src_map = {5364: "1. The Rabbit Team...", 100: "No numbers here"}
    recovered = pipeline.recover(reply, [5364, 100], source_map=src_map)
    assert recovered[5364] == "1. Tim Kelinci dapat dipanggil sekali sehari."
    assert recovered[100] == "Teks lain"
