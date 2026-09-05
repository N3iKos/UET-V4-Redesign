"""
fitting/measure.py — how wide a row is, and which line-break tag it uses.

Two things live here that must never be conflated (spec 1.2b):

1. Segment separators — anything that already breaks a line (a literal 0x0A
   in the cell, or an engine's own tag like [r]). Measurement splits on them;
   the wrap simulation respects them.
2. Insertable tags — a *textual* tag that literally occurs in the source cell
   (backslash-n, [r], [br], [BR]). Only those may be suggested to a model,
   because they survive the [N]-per-line output protocol and the
   encode_placeholders round trip. A raw 0x0A is never "the tag".

`detect_newline_tag` reads the SOURCE row, not the config: the tag the file
actually uses is the tag a suggestion must use, and a file without any tag
gets no line-break advice at all — conservative by design.
"""

from __future__ import annotations

import re
import unicodedata

from .profiles import EngineProfile

# Priority order matters: backslash-n first (most common dump form).
_TAG_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\n", r"\\n"),
    ("[r]",   r"\[r\]"),
    ("[br]",  r"\[br\]"),
    ("[BR]",  r"\[BR\]"),
)

_NARROW = set("iljtIfI'’|!,.:;")
_WIDE = set("mw")
_UPPER_WIDE = set("WM")

def char_width(ch: str, widths: dict[str, float]) -> float:
    if ch == " ":
        return widths["space"]
    if ch.isdigit():
        return widths["digit"]
    if ch in _NARROW:
        return widths["latin_narrow"]
    if ch in _UPPER_WIDE:
        return widths["latin_upper_wide"]
    if ch in _WIDE:
        return widths["latin_wide"]
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return widths["cjk"]
    if ch.isascii() and ch.isprintable() and not ch.isalpha():
        return widths["punct"]
    return widths["latin"]

def strip_tokens(text: str, profile: EngineProfile) -> str:
    out = text
    for pattern in profile.token_patterns:
        out = re.sub(pattern, "", out)
    return out

def _segments(text: str) -> list[str]:
    # A literal 0x0A and the textual tags both split segments for measuring.
    return re.split(r"\n|\[r\]|\[br\]|\[BR\]|\\n", text)

def measure_lines(text: str, profile: EngineProfile) -> list[float]:
    stripped = [strip_tokens(seg, profile) for seg in _segments(text)]
    return [
        sum(char_width(ch, profile.char_widths) for ch in seg)
        for seg in stripped
    ]

def detect_newline_tag(source_row: str) -> str | None:
    for tag, pattern in _TAG_PATTERNS:
        if re.search(pattern, source_row):
            return tag
    return None
