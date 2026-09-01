"""
tests/test_comment_style_and_convergence.py — Comment-style setting and the
convergence layers.

Pins the two halves of the "problems never resolve" fix:

  1. The comment-style setting (auto / explicit / critical) shapes the FIX
     clause of judge comments, and the fix prompt obeys explicit directives.
  2. The programmatic convergence guards — verdict persistence (a passed row
     is only re-flaggable by a critical finding) and the 3-cycle settled
     budget — are state machines on the screen, tested through their folding
     logic in isolation.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.judge.prompts import (
    build_fix_prompt,
    build_judge_prompt,
    build_judge_rechain_prompt,
)

ROWS = [(2, "source text", "translation text")]


# ── 1. Comment style ─────────────────────────────────────────────────────────

class TestCommentStyle:
    def test_auto_keeps_per_finding_choice(self):
        prompt = build_judge_prompt(
            ROWS, ["mistranslation"], tgt_lang="Indonesian", comment_style="auto"
        )
        assert "when the fix is unambiguous" in prompt
        assert "Do NOT dictate exact replacement words" not in prompt

    def test_explicit_demands_exact_words(self):
        prompt = build_judge_prompt(
            ROWS, ["mistranslation"], tgt_lang="Indonesian",
            comment_style="explicit",
        )
        assert "you MUST give the exact replacement wording" in prompt
        assert "Do NOT dictate exact replacement words" not in prompt

    def test_critical_forbids_word_suggestions(self):
        prompt = build_judge_prompt(
            ROWS, ["mistranslation"], tgt_lang="Indonesian",
            comment_style="critical",
        )
        assert "Do NOT dictate exact replacement words" in prompt
        assert "never write \"replace X with Y\"" in prompt
        # And the exact-words clause is gone.
        assert "you MUST give the exact replacement wording" not in prompt

    def test_rechain_prompt_honours_the_style(self):
        prompt = build_judge_rechain_prompt(
            ROWS, {1: ("a", "b"), 3: ("c", "d")}, ["mistranslation"],
            tgt_lang="Indonesian", comment_style="critical",
        )
        assert "Do NOT dictate exact replacement words" in prompt

    def test_unknown_style_falls_back_to_auto(self):
        # The prefs cleaner maps garbage to auto; the builder must not choke
        # on a raw bad value either (defensive: the prompt stays usable).
        prompt = build_judge_prompt(
            ROWS, ["mistranslation"], tgt_lang="Indonesian",
            comment_style="garbage",
        )
        assert "WHAT:" in prompt and "WHY:" in prompt     # still a valid contract


class TestFixObeysExplicitDirectives:
    def test_fix_prompt_orders_exact_changes(self):
        prompt = build_fix_prompt(ROWS, {2: "note"}, tgt_lang="Italian")
        assert "you MUST make that exact change" in prompt
        assert "may NOT survive into your output" in prompt

    def test_fix_prompt_keeps_freedom_for_directions(self):
        prompt = build_fix_prompt(ROWS, {2: "note"}, tgt_lang="Italian")
        assert "choose the wording yourself" in prompt


# ── 2. The omit contract (anti self-contradictory verdicts) ─────────────────

class TestOmitContract:
    def test_judge_prompt_states_omit_means_omit(self):
        prompt = build_judge_prompt(
            ROWS, ["mistranslation"], tgt_lang="Indonesian"
        )
        assert "means OMIT, not report" in prompt

    def test_rechain_prompt_states_omit_means_omit(self):
        prompt = build_judge_rechain_prompt(
            ROWS, {1: ("a", "b")}, ["mistranslation"], tgt_lang="Indonesian"
        )
        assert "means OMIT, not report" in prompt


# ── 3. Convergence layers as pure state machines ─────────────────────────────

class _Issue:
    """A RowIssue stand-in: only the fields the folding logic reads."""

    def __init__(self, severity: str) -> None:
        self.severity = severity
        self.error_types = ["mistranslation"]


class _Report:
    """The slice of JudgeReport the folding logic touches."""

    def __init__(self) -> None:
        self.issues: dict[int, _Issue] = {}
        self.problem_rows: set[int] = set()

    def drop(self, row: int) -> None:
        self.issues.pop(row, None)
        self.problem_rows.discard(row)

    def restore(self, issue: _Issue, row: int) -> None:
        self.issues[row] = issue
        self.problem_rows.add(row)


def _fold(report: _Report, rows, fresh_issues, stable, cycles, settled):
    """
    The folding logic of _rejudged_many, lifted verbatim in shape: fold fresh
    verdicts into the report under the convergence guards.
    """
    still, cleared, held = [], [], []
    for row in rows:
        report.drop(row)
        issue = fresh_issues.get(row)
        if issue is None:
            cleared.append(row)
            stable.add(row)
            continue
        if row in stable and issue.severity != "critical":
            held.append(row)
            continue
        report.restore(issue, row)
        still.append(row)
    newly_settled = []
    for row in still:
        cycles[row] = cycles.get(row, 0) + 1
        if cycles[row] >= 3 and row not in settled:
            settled.add(row)
            newly_settled.append(row)
    return still, cleared, held, newly_settled


class TestVerdictPersistence:
    def test_passed_row_survives_a_minor_reflag(self):
        report = _Report()
        stable, cycles, settled = set(), {}, set()
        # Round 1: row passes.
        _fold(report, [5], {}, stable, cycles, settled)
        assert 5 in stable
        # Round 2: the judge flips to minor — suppressed, row stays clean.
        still, cleared, held, _ = _fold(
            report, [5], {5: _Issue("minor")}, stable, cycles, settled
        )
        assert held == [5] and not still and not cleared
        assert 5 not in report.issues

    def test_critical_overturns_a_pass(self):
        report = _Report()
        stable, cycles, settled = set(), {}, set()
        _fold(report, [5], {}, stable, cycles, settled)
        still, cleared, held, _ = _fold(
            report, [5], {5: _Issue("critical")}, stable, cycles, settled
        )
        assert still == [5] and not held
        assert 5 in report.issues

    def test_unstable_row_is_flagged_normally(self):
        # A row that never passed has no verdict to protect it.
        report = _Report()
        stable, cycles, settled = set(), {}, set()
        still, cleared, held, _ = _fold(
            report, [7], {7: _Issue("minor")}, stable, cycles, settled
        )
        assert still == [7] and not held


class TestCycleBudget:
    def test_row_settles_after_three_still_flagged_rounds(self):
        report = _Report()
        stable, cycles, settled = set(), {}, set()
        for round_no in range(3):
            still, _, _, newly = _fold(
                report, [9], {9: _Issue("moderate")}, stable, cycles, settled
            )
            assert still == [9]
            if round_no < 2:
                assert newly == []
            else:
                assert newly == [9]
        assert 9 in settled

    def test_passing_resets_the_pressure(self):
        # A row that passes does not accumulate cycles at all.
        report = _Report()
        stable, cycles, settled = set(), {}, set()
        for _ in range(5):
            _fold(report, [11], {}, stable, cycles, settled)
        assert cycles.get(11) is None and 11 not in settled
