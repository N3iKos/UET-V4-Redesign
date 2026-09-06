"""
core/tm.py — Translation Memory (SQLite-backed cross-file cache).

Stores (src_lang, tgt_lang, source) -> target pairs in ~/.config/excel-translator-v4/tm.db.
Fast indexing, atomic transactions, thread-safe.
"""

from __future__ import annotations

import os
import sqlite3
import threading

from .. import config

_LOCK = threading.Lock()


def get_tm_db_path() -> str:
    return os.path.join(config.CONFIG_DIR, "tm.db")


def _get_connection(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or get_tm_db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tm (
                src_lang TEXT NOT NULL,
                tgt_lang TEXT NOT NULL,
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (src_lang, tgt_lang, source)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tm_lookup ON tm (src_lang, tgt_lang, source)")
    return conn


def lookup_tm_batch(src_lang: str, tgt_lang: str, texts: list[str], db_path: str | None = None) -> dict[str, str]:
    """Look up a list of source texts. Returns {source_text: target_text}."""
    if not texts:
        return {}
    results = {}
    with _LOCK:
        try:
            conn = _get_connection(db_path)
            with conn:
                cur = conn.cursor()
                for i in range(0, len(texts), 500):
                    chunk = texts[i:i + 500]
                    placeholders = ",".join("?" for _ in chunk)
                    cur.execute(
                        f"SELECT source, target FROM tm WHERE src_lang = ? AND tgt_lang = ? AND source IN ({placeholders})",
                        [src_lang, tgt_lang] + chunk,
                    )
                    for src, tgt in cur.fetchall():
                        results[src] = tgt
        except Exception:
            return {}
    return results


def store_tm_batch(src_lang: str, tgt_lang: str, pairs: list[tuple[str, str]], db_path: str | None = None) -> None:
    """Store (source, target) pairs into TM."""
    if not pairs:
        return
    valid = [
        (src_lang, tgt_lang, src.strip(), tgt.strip())
        for src, tgt in pairs
        if src.strip() and tgt.strip()
    ]
    if not valid:
        return
    with _LOCK:
        try:
            conn = _get_connection(db_path)
            with conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO tm (src_lang, tgt_lang, source, target) VALUES (?, ?, ?, ?)",
                    valid,
                )
        except Exception:
            pass
