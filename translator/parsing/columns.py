"""
parsing/columns.py — Header-based auto-detection of Source and Output columns.

The rules (UET_UX_TASKS Phase 5):
  * Source keywords : original, source, input, src
  * Output keywords : translation, translated, target, output
  * Matching is case-insensitive and ignores spaces, `_`, `-`, and joined
    spellings — `Original Text`, `original_text`, `OriginalText` are all Source
    candidates.
  * When several columns tie, the left-most wins.
  * When nothing is clear enough, guess nothing — return None and let the user
    pick by hand. A wrong guess that silently runs a job over the wrong column
    is worse than no guess at all.

Pure functions only: no I/O, no openpyxl. The caller hands in the header row it
already read (ExcelReader.grid gives it for free), so this module stays a leaf.
"""

from __future__ import annotations

import re

# Keyword → role. Ordered so a header carrying several keywords is scored by the
# strongest match; the tuples keep the plan's exact vocabulary.
SOURCE_KEYWORDS: tuple[str, ...] = ("original", "source", "input", "src")
OUTPUT_KEYWORDS: tuple[str, ...] = ("translation", "translated", "target", "output")


def _normalise(text: str) -> str:
    """
    Fold a header to its comparable form.

    Everything that is not a letter or a digit is dropped and the rest is
    lower-cased, so `Original Text`, `original_text`, `ORIGINAL-TEXT` and
    `OriginalText` all become `originaltext`. That is what lets the keyword test
    ignore spacing, punctuation and case in one step.
    """
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _match_role(header: str, keywords: tuple[str, ...]) -> int:
    """
    How strongly a header reads as this role: 0 = no match, higher = better.

    An exact whole-header match (`source`) beats a header that merely contains
    the keyword (`source column`), so a plain `Source` column is preferred over
    a `Data Source Notes` column when both are present. Among containment
    matches the earliest keyword in the list scores highest, so `original` beats
    `src` on a header that somehow carries both.
    """
    norm = _normalise(header)
    if not norm:
        return 0
    for rank, keyword in enumerate(keywords):
        key = _normalise(keyword)
        if norm == key:
            return 1000 - rank            # exact match, strongest
    for rank, keyword in enumerate(keywords):
        key = _normalise(keyword)
        if key and key in norm:
            return 100 - rank             # containment, weaker
    return 0


def detect_columns(headers: list[str]) -> tuple[int | None, int | None]:
    """
    (source_col, output_col) as 1-based column indices, or None where unclear.

    `headers` is the sheet's header row, index 0 = column A. The two roles are
    resolved independently, the left-most best candidate winning each, and a
    column already taken by Source cannot also be Output (or the two would
    collapse onto one column on a sheet that labelled only one of them).
    """
    source = _best_column(headers, SOURCE_KEYWORDS)
    output = _best_column(headers, OUTPUT_KEYWORDS, exclude=source)
    return source, output


def _best_column(
    headers: list[str],
    keywords: tuple[str, ...],
    exclude: int | None = None,
) -> int | None:
    """
    The 1-based column that best matches these keywords, or None.

    Ties on score go to the left-most column (the plan's duplicate-table rule):
    the loop only replaces the incumbent on a strictly higher score, so an equal
    match further right never displaces one already found.
    """
    best_index: int | None = None
    best_score = 0
    for index, header in enumerate(headers, start=1):
        if exclude is not None and index == exclude:
            continue
        score = _match_role(header, keywords)
        if score > best_score:
            best_score = score
            best_index = index
    return best_index
