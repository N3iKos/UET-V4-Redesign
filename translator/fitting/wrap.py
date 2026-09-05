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

def suggest_wrap(text: str, profile: EngineProfile) -> str:
    tag = detect_newline_tag(text)
    if tag is None:
        return text
    budget = profile.window_cols * profile.safe_margin
    segments: list[str] = []
    for seg in _SEGMENT_SPLIT.split(text):
        words = seg.split(" ")
        current = ""
        for word in words:
            candidate = word if not current else current + " " + word
            if _width(candidate, profile) <= budget or not current:
                current = candidate
            else:
                segments.append(current)
                current = word
        segments.append(current)
    return tag.join(segments)
