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


def test_parse_recovery_preserves_leading_asterisks_for_dialogue():
    pipeline = ParseRecoveryPipeline()
    raw = (
        "[3622] *Hela napas*\n"
        "[3961] *Helaan napas panjang*\n"
        "[4003] *Pfft* \"Konyol sekali.\"\n"
        "[5278] *Endus* *Endus*\n"
        "[6264] *Menguap* \"Aku mau istirahat sebentar.\""
    )
    source_map = {
        3622: "*Sigh*",
        3961: "*Deep sigh*",
        4003: "*Pfft* \"That's absurd.\"",
        5278: "*Sniff* *Sniff*",
        6264: "*Yawn* \"I'll take a small break.\"",
    }
    recovered = pipeline.recover(raw, [3622, 3961, 4003, 5278, 6264], source_map=source_map)
    assert recovered[3622] == "*Hela napas*"
    assert recovered[3961] == "*Helaan napas panjang*"
    assert recovered[4003] == "*Pfft* \"Konyol sekali.\""
    assert recovered[5278] == "*Endus* *Endus*"
    assert recovered[6264] == "*Menguap* \"Aku mau istirahat sebentar.\""


def test_parse_recovery_preserves_action_asterisks_even_without_source_map():
    pipeline = ParseRecoveryPipeline()
    raw = "[1] *Hela napas*\n[2] *abc*"
    recovered = pipeline.recover(raw, [1, 2])
    assert recovered[1] == "*Hela napas*"
    assert recovered[2] == "*abc*"


def test_parse_recovery_strips_markdown_bullets_with_space():
    pipeline = ParseRecoveryPipeline()
    raw = "[1] * Terjemahan baris 1\n[2] ** Terjemahan baris 2"
    source_map = {1: "Source line 1", 2: "Source line 2"}
    recovered = pipeline.recover(raw, [1, 2], source_map=source_map)
    assert recovered[1] == "Terjemahan baris 1"
    assert recovered[2] == "Terjemahan baris 2"

