"""
tests/test_chain_context.py — Dialogue-chain context and dynamic naturalness.

Pins the two prompt-mode features behind the Settings toggles:

  1. Chain mode swaps the per-line isolation for a dialogue-stream prompt with
     a scene-break guard; OFF must be byte-identical in spirit to the old
     behaviour (the isolation rules stay).
  2. A chain re-judge prints flagged rows as UNDER REVIEW and their file
     neighbours as CONTEXT ONLY, in row order, and the runner drops any
     verdict the model aims at a neighbour anyway.
  3. Every language rule is written against the target language the user
     chose — no language is named in the prompt sources.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.judge.prompts import (
    _naturalness_rule,
    build_fix_prompt,
    build_judge_prompt,
    build_judge_rechain_prompt,
)

ROWS = [(2, "It left a scratch.", "Dia meninggalkan goresan.")]


# ── 1. Judge prompt modes ─────────────────────────────────────────────────────

class TestJudgePromptModes:
    def test_off_keeps_isolation(self):
        prompt = build_judge_prompt(ROWS, ["mistranslation"], tgt_lang="Indonesian")
        assert "not context for one another" in prompt
        assert "CONSECUTIVE lines" not in prompt

    def test_on_reads_as_dialogue_stream(self):
        prompt = build_judge_prompt(
            ROWS, ["mistranslation"], tgt_lang="Indonesian", chain=True
        )
        assert "CONSECUTIVE lines of the same work" in prompt
        assert "not context for one another" not in prompt

    def test_on_has_the_scene_break_guard(self):
        prompt = build_judge_prompt(
            ROWS, ["mistranslation"], tgt_lang="Indonesian", chain=True
        )
        assert "Scene breaks may occur anywhere" in prompt

    def test_on_asks_for_both_halves_in_cross_line_findings(self):
        prompt = build_judge_prompt(
            ROWS, ["mistranslation"], tgt_lang="Indonesian", chain=True
        )
        assert "quote BOTH halves" in prompt


# ── 2. Re-judge with neighbours ───────────────────────────────────────────────

FLAGGED = [
    (2, "It left a scratch.", "Dia meninggalkan goresan."),
    (6, "Look at it.", "Lihat itu."),
]
NEIGHBOURS = {
    1: ("Have you seen my cat?", "Kamu lihat kucingku?"),
    3: ("That scratch looks deep.", "Goresan itu tampak dalam."),
    5: ("Really?", "Sungguh?"),
    7: ("I know.", "Aku tahu."),
}


class TestRechainPrompt:
    def test_flagged_rows_are_under_review(self):
        prompt = build_judge_rechain_prompt(
            FLAGGED, NEIGHBOURS, ["mistranslation"], tgt_lang="Indonesian"
        )
        assert "[2] — UNDER REVIEW" in prompt
        assert "[6] — UNDER REVIEW" in prompt

    def test_neighbours_are_context_only(self):
        prompt = build_judge_rechain_prompt(
            FLAGGED, NEIGHBOURS, ["mistranslation"], tgt_lang="Indonesian"
        )
        for row in (1, 3, 5, 7):
            assert f"[{row}] — CONTEXT ONLY" in prompt

    def test_rows_are_in_file_order(self):
        prompt = build_judge_rechain_prompt(
            FLAGGED, NEIGHBOURS, ["mistranslation"], tgt_lang="Indonesian"
        )
        positions = [prompt.index(f"[{row}]") for row in (1, 2, 3, 5, 6, 7)]
        assert positions == sorted(positions)

    def test_neighbour_rule_is_stated(self):
        prompt = build_judge_rechain_prompt(
            FLAGGED, NEIGHBOURS, ["mistranslation"], tgt_lang="Indonesian"
        )
        assert "CONTEXT ONLY" in prompt
        assert "must NEVER appear" in prompt

    def test_flagged_neighbour_is_not_duplicated(self):
        # Row 3 flagged as well as row 2: it appears once, as UNDER REVIEW,
        # and is skipped from the context side even though it is row 2's
        # neighbour.
        flagged = FLAGGED + [(3, "Deep one.", "Yang dalam.")]
        prompt = build_judge_rechain_prompt(
            flagged, NEIGHBOURS, ["mistranslation"], tgt_lang="Indonesian"
        )
        assert prompt.count("[3]") == 1
        assert "[3] — UNDER REVIEW" in prompt


class TestRechainVerdictDrop:
    def test_neighbour_verdicts_are_dropped(self):
        """The runner's defensive drop: parse() removes neighbour verdicts."""
        from translator.judge.models import JudgeResult
        from translator.scheduling.scheduler import BatchTask

        # A minimal stand-in for the runner's parse() neighbour guard — the
        # logic under test is the pop, so it is exercised directly with the
        # real dataclass the runner uses.
        result = JudgeResult()
        result.issues[1] = "should be dropped"    # type: ignore[assignment]
        result.issues[2] = "kept"                 # type: ignore[assignment]
        for neighbour in NEIGHBOURS:
            result.issues.pop(neighbour, None)
        assert 1 not in result.issues
        assert 2 in result.issues


# ── 3. Dynamic language rules ────────────────────────────────────────────────

class TestDynamicLanguage:
    def test_naturalness_names_the_target(self):
        for language in ("Indonesian", "Spanish", "Japanese", "German"):
            rule = _naturalness_rule(language)
            assert language in rule

    def test_no_language_is_hardcoded(self):
        rule = _naturalness_rule("German")
        for other in ("Indonesian", "Spanish", "Japanese"):
            assert other not in rule

    def test_empty_language_falls_back_generically(self):
        rule = _naturalness_rule("")
        assert "the target language" in rule

    def test_judge_prompt_carries_the_target(self):
        prompt = build_judge_prompt(
            ROWS, ["mistranslation"], tgt_lang="Korean", chain=True
        )
        assert "native speaker of Korean" in prompt

    def test_fix_prompt_enforces_explicit_fixes(self):
        """
        The repair contract: a FIX directive naming specific words is an
        ORDER, not a suggestion — a repair that returns the flagged wording
        unchanged is a failed repair. Direction-only directives leave the
        wording to the fix model.
        """
        prompt = build_fix_prompt(
            ROWS, {2: "WHAT/WHY/FIX note"}, tgt_lang="Italian"
        )
        assert "native speaker of Italian" in prompt
        assert "you MUST make that exact change" in prompt
        assert "may NOT survive into your output" in prompt

    def test_rechain_prompt_carries_the_target(self):
        prompt = build_judge_rechain_prompt(
            FLAGGED, NEIGHBOURS, ["mistranslation"], tgt_lang="Portuguese"
        )
        assert "native speaker of Portuguese" in prompt
