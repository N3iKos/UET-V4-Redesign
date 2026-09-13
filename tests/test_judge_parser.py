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


def test_literal_newlines_in_comment_parsed():
    # LLMs frequently emit literal unescaped newlines in JSON strings
    raw = '{\n  "lines": [\n    {\n      "row": 12,\n      "error_types": ["style"],\n      "comment": "WHAT: foo\\nWHY: bar\\nFIX: baz"\n    }\n  ]\n}'
    # Replace the escaped \\n with actual unescaped literal newline
    raw_with_literal_newlines = '{\n  "lines": [\n    {\n      "row": 12,\n      "error_types": ["style"],\n      "comment": "WHAT: foo\nWHY: bar\nFIX: baz"\n    }\n  ]\n}'
    result = parse_evaluation(raw_with_literal_newlines)
    assert not result.unparsed
    assert 12 in result.issues
    assert "WHAT: foo" in result.issues[12].comment


def test_think_tag_with_curly_braces_stripped():
    raw = (
        "<think>\n"
        "Let's look at row 1. The placeholder {name} is preserved.\n"
        "Row 2 has issues: {test}.\n"
        "</think>\n"
        '{"lines": [{"row": 2, "error_types": ["mistranslation"], "comment": "wrong meaning"}]}'
    )
    result = parse_evaluation(raw)
    assert not result.unparsed
    assert 2 in result.issues


def test_natural_language_acceptance():
    phrases = [
        "All lines are acceptable. No issues found.",
        "Semua baris sudah akurat dan tidak ada kesalahan.",
        "Every line is acceptable.",
        "No translation errors detected in this batch.",
    ]
    for phrase in phrases:
        result = parse_evaluation(phrase)
        assert not result.unparsed, f"Failed for phrase: {phrase}"
        assert not result.issues


def test_truncated_json_recovered():
    # Truncated before closing brackets
    raw = '{"lines": [{"row": 10, "error_types": ["placeholder"], "comment": "missing tag"'
    result = parse_evaluation(raw)
    assert not result.unparsed
    assert 10 in result.issues


def test_unescaped_double_quotes_in_comment_recovered():
    raw = '{"lines": [{"row": 8, "comment": "SOURCE says "black cat" but TRANSLATION says "kucing merah"", "severity": "critical"}]}'
    result = parse_evaluation(raw)
    assert not result.unparsed
    assert 8 in result.issues


def test_unescaped_game_code_backslashes_recovered():
    # Model returns unescaped game engine tags \N[1], \V[2], \TRN[123] inside JSON string
    raw = r'{"lines": [{"row": 15, "comment": "WHAT: SOURCE says \'\N[1] and \V[2] and \TRN[3]\' but TRANSLATION says \'foo\'"}]}'
    result = parse_evaluation(raw)
    assert not result.unparsed
    assert 15 in result.issues
    assert r"\N[1]" in result.issues[15].comment or "\\N[1]" in result.issues[15].comment


def test_multiple_json_self_correction_recovered():
    # Model wrote an initial finding, reconsidered in prose, and outputted {"lines": []} at the end
    raw = (
        '{"lines": [{"row": 378, "comment": "WHAT: bad"}]}\n\n'
        "Wait, looking closer at the source, the translation is actually faithful.\n"
        "I find no actual errors.\n\n"
        '{"lines": []}'
    )
    result = parse_evaluation(raw)
    assert not result.unparsed
    assert not result.issues



