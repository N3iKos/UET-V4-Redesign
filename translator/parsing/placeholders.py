"""
parsing/placeholders.py — Placeholder protection + symbol sanitisation.

Ported from v3.6's parser.py with no behavioural change: this logic is
already pure and well-tested in real use, and 00_OVERVIEW.md §3 explicitly
says to port rather than rewrite it.

Problem it solves: LLMs normalise backslash sequences (\\@ -> @, \\" -> ")
because they treat \\ as an escape character. Game engines break when that
happens. So problematic sequences are encoded into inert tokens before the
request and decoded afterwards.
"""

from __future__ import annotations

import re
import unicodedata

# Order matters: longer patterns first, so "\\\\" can't be half-matched by "\\".
_ENCODE_MAP: list[tuple[str, str]] = [
    ("\\\\", "[BR][BR]"),   # literal backslash — must be first
    ("\\n",  "[BR][NL]"),
    ("\\t",  "[BR][TB]"),
    ("\\r",  "[BR][CR]"),
    ("\\@",  "[BR][AT]"),   # AliceSoft: clear dialog box
    ("\\!",  "[BR][EX]"),   # AliceSoft: pause/wait input
    ("\\\"", "[BR][DQ]"),
    ("\\'",  "[BR][SQ]"),
    ("\\[",  "[BR][LB]"),
    ("\\]",  "[BR][RB]"),
    ("「",   "[BR][LQ]"),
    ("」",   "[BR][RQ]"),
    ("『",   "[BR][LD]"),
    ("』",   "[BR][RD]"),
]

# Additional game engine control codes (RPG Maker, Wolf, VN)
_GAME_SYMBOLS_MAP: list[tuple[str, str]] = [
    ("\\.",  "[BR][DOT]"),   # RPG Maker: wait 15 frames
    ("\\|",  "[BR][BAR]"),   # RPG Maker: wait 60 frames
    ("\\^",  "[BR][HAT]"),   # RPG Maker: auto-continue without wait
    ("\\$",  "[BR][DLR]"),   # RPG Maker: open gold window
    ("\\{",  "[BR][LCB]"),   # RPG Maker: increase font size
    ("\\}",  "[BR][RCB]"),   # RPG Maker: decrease font size
    ("\\<",  "[BR][LT]"),
    ("\\>",  "[BR][GT]"),
    ("\\#",  "[BR][HSH]"),
    ("\\%",  "[BR][PCT]"),
    ("\\_",  "[BR][UND]"),
]

_DECODE_MAP: list[tuple[str, str]] = [(tok, orig) for orig, tok in reversed(_ENCODE_MAP)]
_DECODE_SYMBOLS_MAP: list[tuple[str, str]] = [(tok, orig) for orig, tok in reversed(_GAME_SYMBOLS_MAP)]


def encode_placeholders(text: str) -> str:
    """'Hello\\@World' -> 'Hello[BR][AT]World'."""
    if not text:
        return ""
    # Protect dynamic game tags with brackets: \N[1], \V[20], \C[3], \TRN[999], etc.
    text = re.sub(r"\\([A-Za-z]+\[[^\]\r\n]+\])", r"[BR][TAG:\1]", text)
    # Standalone game flags like \G (gold)
    text = re.sub(r"\\([Gg])(?![A-Za-z])", r"[BR][TAG:\1]", text)

    for original, token in _GAME_SYMBOLS_MAP:
        text = text.replace(original, token)
    for original, token in _ENCODE_MAP:
        text = text.replace(original, token)
    return text


def decode_placeholders(text: str) -> str:
    """'Halo[BR][AT]Dunia' -> 'Halo\\@Dunia'."""
    if not text:
        return ""
    for token, original in _DECODE_MAP:
        text = text.replace(token, original)
    for token, original in _DECODE_SYMBOLS_MAP:
        text = text.replace(token, original)
    # Restore dynamic game tags
    text = re.sub(r"\[BR\]\[TAG:([^\]]+)\]", r"\\\1", text)
    return text


# ── Symbol diff sanitisation ──────────────────────────────────────────────────
# Only normalise characters the model invented; anything already present in the
# source is legal by definition.

_UNICODE_FALLBACK: dict[str, str] = {
    "\u2014": "--",   # em-dash
    "\u2013": "-",    # en-dash
    "\u201C": '"',
    "\u201D": '"',
    "\u2018": "'",
    "\u2019": "'",
    "\u2026": "...",
    "\u00AB": '"',
    "\u00BB": '"',
    "\u2015": "--",
    "\u2012": "-",
    "\u2010": "-",
}


def collect_source_symbols(texts: list[str]) -> set[str]:
    charset: set[str] = set()
    for text in texts:
        charset.update(text)
    return charset


def symbol_diff_sanitize(text: str, source_symbols: set[str]) -> str:
    """
    Replace "fancy" characters the model introduced, but only when the
    replacement is itself legal for this source. If the source already uses
    em-dashes, em-dashes are left alone.
    """
    if not text:
        return text

    parts: list[str] = []
    for ch in text:
        if ch in source_symbols or ord(ch) < 128:
            parts.append(ch)
            continue

        fallback = _UNICODE_FALLBACK.get(ch)
        if fallback is not None and all(
            fc in source_symbols or ord(fc) < 128 for fc in fallback
        ):
            parts.append(fallback)
            continue

        nfkd = unicodedata.normalize("NFKD", ch)
        base = "".join(c for c in nfkd if not unicodedata.combining(c))
        if base and base != ch and all(
            bc in source_symbols or ord(bc) < 128 for bc in base
        ):
            parts.append(base)
            continue

        parts.append(ch)   # keep — could be valid CJK etc.

    return "".join(parts)


def strip_runaway(text: str, threshold: int = 50) -> str:
    """
    Truncate degenerate repetition ('||||||...', 'ssssss...').

    Threshold is deliberately high: game dialogue legitimately contains
    'Waaaaaaa!' style repetition.
    """
    match = re.search(r"(.)\1{" + str(threshold - 1) + r",}", text)
    if match:
        return text[: match.start()].rstrip()
    return text
