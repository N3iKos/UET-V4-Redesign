"""
fitting/wrap.py — simulate the engine's line breaking, mechanically.

RPG Maker vanilla does not wrap: a line runs off the window. So "does it fit"
cannot be answered from the raw string — it can only be answered by simulating
where a sane word-wrap would break, then counting the result against the
window's line budget. This module is that simulation: greedy, deterministic,
and split only on spaces so a control code is never torn in half (the code
travels inside a word, because codes contain no spaces).

`suggest_wrap` re-flows overlong rows with the line-break tag the SOURCE
itself uses (`detect_newline_tag`); when the source has no tag there is
nothing insertable, so the suggestion is returned unchanged — the fix belongs
to the judge's shorten advice instead (spec 2a).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .measure import char_width, detect_newline_tag, strip_tokens
from .profiles import EngineProfile

_SEGMENT_SPLIT = re.compile(r"\n|\[r\]|\[br\]|\[BR\]|\\n")
_SEGMENT_SPLIT_CAPTURE = re.compile(r"(\n|\[r\]|\[br\]|\[BR\]|\\n)")

@dataclass
class WrapMetrics:
    lines: int
    max_width: float
    budget: float
    overflow_lines: list[int] = field(default_factory=list)
    fits: bool = True
    max_ok: bool = True

def _width(text: str, profile: EngineProfile) -> float:
    return sum(char_width(ch, profile.char_widths) for ch in strip_tokens(text, profile))

def wrap_lines(text: str, profile: EngineProfile) -> list[str]:
    budget = profile.window_cols * profile.safe_margin
    out: list[str] = []
    for seg in _SEGMENT_SPLIT.split(text):
        words = seg.split(" ")
        current = ""
        for word in words:
            candidate = word if not current else current + " " + word
            if _width(candidate, profile) <= budget or not current:
                current = candidate
            else:
                out.append(current)
                current = word
        out.append(current)
    return out

def wrap_metrics(text: str, profile: EngineProfile) -> WrapMetrics:
    lines = wrap_lines(text, profile)
    budget = profile.window_cols * profile.safe_margin
    widths = [_width(ln, profile) for ln in lines]
    overflow = [i for i, w in enumerate(widths) if w > budget]
    return WrapMetrics(
        lines=len(lines),
        max_width=max(widths) if widths else 0.0,
        budget=budget,
        overflow_lines=overflow,
        fits=not overflow,
        max_ok=len(lines) <= profile.max_lines,
    )

def suggest_wrap(text: str, profile: EngineProfile, tag: str | None = None) -> str:
    """
    Re-flow overlong rows, inserting `tag` at the new breaks only.

    `tag` defaults to the one detected in THIS text. The caller (audit apply)
    may pass the SOURCE row's tag instead: a translation that dropped the
    source's tags is still wrapped with the tag the file proves — the machine
    never invents one.

    Original separators are preserved exactly: a real 0x0A stays a 0x0A, a
    `[r]` stays a `[r]` — only NEW breaks get the tag. Rebuilding the string
    with `tag.join(...)` would silently convert every existing separator
    (including a raw newline the engine renders but cannot parse back as the
    two-char token) into the tag — a data change on apply, not a fix.
    """
    insert = tag if tag is not None else detect_newline_tag(text)
    if insert is None:
        return text
    budget = profile.window_cols * profile.safe_margin
    out: list[str] = []
    # Capturing split: pieces alternate [seg, sep, seg, sep, ..., seg]; each
    # separator is kept verbatim in its original position.
    pieces = _SEGMENT_SPLIT_CAPTURE.split(text)
    for i, piece in enumerate(pieces):
        if i % 2 == 1:              # an original separator — untouched
            out.append(piece)
            continue
        words = piece.split(" ")
        current = ""
        for word in words:
            candidate = word if not current else current + " " + word
            if _width(candidate, profile) <= budget or not current:
                current = candidate
            else:
                out.append(current + insert)
                current = word
        out.append(current)
    return "".join(out)
