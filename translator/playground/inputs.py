"""
playground/inputs.py — Turn pasted text into numbered rows.

Two shapes are accepted, and which one you used is worked out rather than
asked about:

    [1] first line          plain first line
    [2] second line   or    plain second line

Numbered input keeps its own numbers, so a batch copied straight out of a raw
API log lines up with the log. Plain input is numbered 1..n. Either way the
result is the `[(row_number, text)]` list the translation prompt builders
already take, so the playground sends exactly what a real run would send.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Same shape recovery.py parses model output with: an entry runs until the
# next [N] at the start of a line, so one entry may span several lines.
_ENTRY = re.compile(r"\[(\d+)\]\s*(.*?)(?=\n\s*\[\d+\]|\Z)", re.DOTALL)
_LEADING_NUMBER = re.compile(r"^\s*\[\d+\]")


@dataclass
class ParsedInput:
    rows: list[tuple[int, str]] = field(default_factory=list)
    numbered: bool = False       # the paste carried its own [N] prefixes
    renumbered: bool = False     # those numbers clashed, so they were replaced

    def __bool__(self) -> bool:
        return bool(self.rows)

    @property
    def count(self) -> int:
        return len(self.rows)

    def describe(self) -> str:
        """One line for the UI, so the guess is never silent."""
        if not self.rows:
            return "nothing to send"
        shape = "numbered [N] lines" if self.numbered else "plain lines"
        note = " · numbers clashed, renumbered" if self.renumbered else ""
        return f"{self.count} line{'s' if self.count != 1 else ''} · {shape}{note}"


def parse_input(text: str) -> ParsedInput:
    """
    Detection rule: if the first non-blank line opens with `[N]`, the whole
    paste is treated as numbered. Anything after that which lacks a prefix is
    a continuation of the entry above it, which is what makes a multi-line
    cell survive the round trip.
    """
    body = (text or "").strip()
    if not body:
        return ParsedInput()

    first = next((line for line in body.splitlines() if line.strip()), "")
    if _LEADING_NUMBER.match(first):
        return _parse_numbered(body)
    return _parse_plain(body)


def _parse_numbered(body: str) -> ParsedInput:
    rows: list[tuple[int, str]] = []
    for match in _ENTRY.finditer(body):
        value = match.group(2).strip()
        if value:
            rows.append((int(match.group(1)), value))
    if not rows:
        return _parse_plain(body)

    # The numbers become dict keys on the way back out of the model, so a
    # duplicate would quietly swallow a row. Renumber rather than lose one.
    numbers = [number for number, _text in rows]
    if len(set(numbers)) != len(numbers):
        return ParsedInput(
            rows=[(index, text) for index, (_old, text) in enumerate(rows, start=1)],
            numbered=True,
            renumbered=True,
        )
    return ParsedInput(rows=rows, numbered=True)


def _parse_plain(body: str) -> ParsedInput:
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    return ParsedInput(rows=list(enumerate(lines, start=1)))
