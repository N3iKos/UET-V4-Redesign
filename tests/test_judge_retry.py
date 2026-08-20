"""
tests/test_judge_retry.py — Judge format-retry behaviour.

Pins requirement: a section that answers in prose (non-JSON) is re-asked up to
three times before any of its rows are counted as unevaluated, and a row that
eventually parses on a later attempt is counted as evaluated rather than thrown
away with the section.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.judge.runner import JudgeRunner, JudgeTarget


class FakeReply:
    def __init__(self, content: str, tokens: int = 10) -> None:
        self.content = content
        self.tokens = tokens
        self.latency_ms = 0.0
        self.headers = {}


class FakeClient:
    """Returns a canned reply per call, then the good JSON on the next call."""

    def __init__(self, bad_count: int, good: str) -> None:
        self.bad_count = bad_count
        self.good = good
        self.calls = 0

    def generate(self, model, prompt, **kwargs) -> FakeReply:
        self.calls += 1
        if self.calls <= self.bad_count:
            return FakeReply("This is prose, not JSON.")
        return FakeReply(self.good)


def _runner(rows, client, graded=True):
    return JudgeRunner(
        target=JudgeTarget(path="", src_col=1, dst_col=2),
        criteria=["mistranslation"],
        grading_mode="severity" if graded else "none",
        client_factory=lambda: client,
        max_rows=50,
    )


def _plan(runner, rows):
    return runner.plan(rows)


def test_prose_reply_retries_then_recovers():
    good = (
        '{"lines": [{"row": 2, "error_types": ["mistranslation"], '
        '"severity": "critical", "comment": "negation dropped"}]}'
    )
    client = FakeClient(bad_count=1, good=good)
    rows = [(2, "source text", "translation text")]
    runner = _runner(rows, client)
    report = runner.evaluate(plan=_plan(runner, rows))

    # One bad reply, then one good one → the section recovered, its row is
    # evaluated, and it is reported as a problem — not unevaluated.
    assert client.calls == 2
    assert report.summary.unevaluated == 0
    assert report.summary.evaluated == 1
    assert 2 in report.issues
    assert report.issues[2].error_types == ["mistranslation"]


def test_persistent_prose_marks_rows_unevaluated():
    client = FakeClient(bad_count=99, good="")
    rows = [(1, "src", "dst"), (2, "src", "dst")]
    runner = _runner(rows, client)
    report = runner.evaluate(plan=_plan(runner, rows))

    # Retried 3 times (plus the initial attempt = 4 calls), gave up, and the
    # rows are unevaluated — never invented a verdict out of prose.
    assert client.calls == 4
    assert report.summary.unevaluated == 2
    assert report.summary.evaluated == 0
    assert not report.issues
    assert all(section.unparsed for section in report.sections)


def test_recovered_row_is_not_unevaluated():
    good = '{"lines": []}'
    client = FakeClient(bad_count=2, good=good)
    rows = [(5, "src", "dst")]
    runner = _runner(rows, client)
    report = runner.evaluate(plan=_plan(runner, rows))

    # Two prose replies, then a clean JSON → the row passed, not unevaluated.
    assert client.calls == 3
    assert report.summary.unevaluated == 0
    assert report.summary.evaluated == 1
    assert report.summary.problem_rows == 0