"""
tests/test_translation_recovery.py — Tests for resilient translation recovery,
placeholders protection, and anti-desync tolerance.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.parsing.placeholders import encode_placeholders, decode_placeholders
from translator.parsing.recovery import ParseRecoveryPipeline
from translator.parsing.alignment import validate_alignment


def test_game_engine_tags_encoded_and_decoded():
    # RPG Maker control symbols that could be stripped as punctuation
    samples = [
        r"Wait\. and stop\| and finish\^ with \$ gold",
        r"Font bigger\{ and smaller\}",
        r"Clear dialog\@ and pause\!",
    ]
    for orig in samples:
        encoded = encode_placeholders(orig)
        assert r"\." not in encoded
        assert r"\|" not in encoded
        # Decoded must restore original byte-for-byte
        decoded = decode_placeholders(encoded)
        assert decoded == orig


def test_parse_recovery_markdown_bold_brackets():
    pipeline = ParseRecoveryPipeline()
    raw = "**[1]** Halo dunia\n**[2]** Selamat pagi\n**[3]** Apa kabar?"
    recovered = pipeline.recover(raw, [1, 2, 3])
    assert recovered == {1: "Halo dunia", 2: "Selamat pagi", 3: "Apa kabar?"}


def test_parse_recovery_bracket_colon_and_dash():
    pipeline = ParseRecoveryPipeline()
    raw = "[1]: Halo dunia\n[2] - Selamat pagi\n[3]: Apa kabar?"
    recovered = pipeline.recover(raw, [1, 2, 3])
    assert recovered == {1: "Halo dunia", 2: "Selamat pagi", 3: "Apa kabar?"}


def test_parse_recovery_loose_colon_and_dash():
    pipeline = ParseRecoveryPipeline()
    raw = "1: Halo dunia\n2 - Selamat pagi\n3: Apa kabar?"
    recovered = pipeline.recover(raw, [1, 2, 3])
    assert recovered == {1: "Halo dunia", 2: "Selamat pagi", 3: "Apa kabar?"}


def test_parse_recovery_paren_and_row_prefix():
    pipeline = ParseRecoveryPipeline()
    raw = "(1) Halo dunia\n[Row 2] Selamat pagi\nRow 3: Apa kabar?"
    recovered = pipeline.recover(raw, [1, 2, 3])
    assert recovered == {1: "Halo dunia", 2: "Selamat pagi", 3: "Apa kabar?"}


def test_anti_desync_does_not_reject_streak_for_natural_exclamations():
    # When natural translation translates JP double exclamation to single or normal period,
    # it must not reject the entire batch when other lines are valid
    source_map = {
        1: "おはよう！！",
        2: "元気ですか！！",
        3: "今日もいい天気ですね",
        4: "はい、そうですね",
    }
    parsed = {
        1: "Selamat pagi!",
        2: "Apa kabar?",
        3: "Hari ini cuacanya bagus ya",
        4: "Iya, benar sekali",
    }
    validated = validate_alignment(parsed, source_map, mode="auto")
    assert len(validated) >= 3, f"Premature rejection: got {validated}"
