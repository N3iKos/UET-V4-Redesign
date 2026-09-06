"""
tests/test_tm.py — Persistent Translation Memory (SQLite) unit tests.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.core.tm import lookup_tm_batch, store_tm_batch


def test_tm_store_and_lookup(tmp_path):
    db_file = str(tmp_path / "test_tm.db")
    pairs = [
        ("Hello", "Halo"),
        ("Good morning", "Selamat pagi"),
        ("Yes", "Ya"),
    ]
    store_tm_batch("English", "Indonesian", pairs, db_path=db_file)

    res = lookup_tm_batch("English", "Indonesian", ["Hello", "Yes", "Unknown"], db_path=db_file)
    assert res == {"Hello": "Halo", "Yes": "Ya"}


def test_tm_different_languages(tmp_path):
    db_file = str(tmp_path / "test_tm.db")
    store_tm_batch("English", "Indonesian", [("Attack", "Serang")], db_path=db_file)
    store_tm_batch("English", "Japanese", [("Attack", "攻撃")], db_path=db_file)

    res_id = lookup_tm_batch("English", "Indonesian", ["Attack"], db_path=db_file)
    assert res_id == {"Attack": "Serang"}

    res_jp = lookup_tm_batch("English", "Japanese", ["Attack"], db_path=db_file)
    assert res_jp == {"Attack": "攻撃"}


def test_tm_empty_and_duplicate(tmp_path):
    db_file = str(tmp_path / "test_tm.db")
    assert lookup_tm_batch("English", "Indonesian", [], db_path=db_file) == {}
    store_tm_batch("English", "Indonesian", [], db_path=db_file)
    # Updating existing key
    store_tm_batch("English", "Indonesian", [("Hello", "Halo baru")], db_path=db_file)
    res = lookup_tm_batch("English", "Indonesian", ["Hello"], db_path=db_file)
    assert res == {"Hello": "Halo baru"}
