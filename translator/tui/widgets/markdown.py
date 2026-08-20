r"""
tui/widgets/markdown.py — Markdown (and its maths) as painted terminal lines.

Models answer in Markdown whether or not anyone asked them to, so a chat
that shows the source shows `**this**` and `## that` and a wall of
`\frac{}{}`. This turns a reply into what it was meant to look like:
headings, emphasis, lists, quotes, code, tables — and formulas, through
`latex.render_math`.

Two decisions worth knowing about:

* **Line breaks are kept.** Real Markdown folds consecutive lines into one
  paragraph. This is a translation tool: a model listing one rendering per
  line means those lines, and joining them would destroy the only structure
  the answer had.
* **Everything is built as `rich.text.Text`, never markup.** A reply is
  arbitrary text from an arbitrary endpoint. It must not be able to kill a
  repaint by containing something that parses as a tag.

It also has to cope with half a document, because the chat repaints while
the reply is still streaming: an unclosed fence renders as a code block, an
unclosed `**` stays literal until its partner arrives.
"""

from __future__ import annotations

import re

from rich.cells import cell_len
from rich.text import Text

from ..theme import ACCENT, MD_CODE, MD_CODE_BG, MD_LINK, MD_MATH, MD_RULE, MUTED
from .common import render_console
from .latex import render_math

# ── Block patterns ────────────────────────────────────────────────────────────

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*([^\s`]*)")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_RULE = re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^(\s*)(\d{1,9})[.)]\s+(.*)$")
_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
_DISPLAY = re.compile(r"^(\$\$|\\\[)(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?(?:\s*:?-{2,}:?\s*\|)+\s*:?-{2,}:?\s*\|?\s*$")
_INDENT = re.compile(r"^(\s*)(.*)$")

_BULLETS = ("•", "◦", "▪", "·")

# ── Inline patterns ───────────────────────────────────────────────────────────

_ESCAPED = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|~>$])")


def _is_math(match: re.Match) -> bool:
    """
    Is this `$…$` a formula, or is it money?

    Two dollar amounts in one sentence look exactly like a maths span, and
    getting that wrong recolours half a paragraph. The regex already refuses
    a span that starts or ends on whitespace, which throws out `$5 and $10`;
    this throws out what is left by asking for something actually maths-like.
    """
    body = match.group(1).strip()
    if not body or len(body) > 200:
        return False
    if re.search(r"[\\^_{}]", body):
        return True
    if not re.fullmatch(r"[A-Za-z0-9 +\-*/=<>().,'π]{1,40}", body):
        return False
    return bool(re.search(r"[A-Za-z]", body))


_INLINE: tuple[tuple[str, re.Pattern, object], ...] = (
    # Code first, and math second: both are literal, and whatever they
    # contain must not then be read as emphasis. `$a_1$` is not italic.
    ("code", re.compile(r"(`+)([\s\S]+?)\1"), None),
    ("math", re.compile(r"\\\(([\s\S]+?)\\\)"), None),
    ("math", re.compile(r"(?<![\\$])\$(?![\s$])([^$\n]+?)(?<![\s\\])\$"), _is_math),
    ("link", re.compile(r"!?\[([^\]\n]*)\]\(\s*(<?[^)\s]*>?)[^)\n]*\)"), None),
    ("bolditalic", re.compile(r"\*\*\*([\s\S]+?)\*\*\*"), None),
    ("bold", re.compile(r"\*\*([\s\S]+?)\*\*"), None),
    ("bold", re.compile(r"(?<![A-Za-z0-9_])__([\s\S]+?)__(?![A-Za-z0-9_])"), None),
    ("strike", re.compile(r"~~([\s\S]+?)~~"), None),
    ("italic", re.compile(r"(?<![A-Za-z0-9*\\])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![A-Za-z0-9*])"), None),
    ("italic", re.compile(r"(?<![A-Za-z0-9_\\])_(?!\s)([^_\n]+?)(?<!\s)_(?![A-Za-z0-9_])"), None),
)

_MAX_NESTING = 6


def render_markdown(source: str, width: int, console=None) -> list[Text]:
    """A Markdown document as lines already wrapped to `width`."""
    return _Renderer(width, console or render_console()).render(source or "")


def render_plain(source: str, width: int, console=None, style: str = "") -> list[Text]:
    """The same shape, for text that is to be shown exactly as it was typed."""
    console = console or render_console()
    lines: list[Text] = []
    for chunk in (source or "").split("\n"):
        lines.extend(Text(chunk or " ", style=style).wrap(console, max(4, width)))
    return lines


# ── Inline ────────────────────────────────────────────────────────────────────

def _join(base: str, extra: str) -> str:
    return f"{base} {extra}".strip() if base else extra


def _inline(source: str, style: str = "") -> Text:
    text = Text()
    _scan(source, style, text, 0)
    return text


def _scan(source: str, style: str, out: Text, depth: int) -> None:
    if depth > _MAX_NESTING:
        out.append(_ESCAPED.sub(r"\1", source), style=style)
        return

    pos = 0
    while pos < len(source):
        best: tuple[str, re.Match] | None = None
        for kind, pattern, valid in _INLINE:
            match = pattern.search(source, pos)
            while match is not None and valid is not None and not valid(match):
                # A rejected span must not hide a real one further along, so
                # keep looking rather than giving up on the pattern.
                match = pattern.search(source, match.start() + 1)
            if match is not None and (best is None or match.start() < best[1].start()):
                best = (kind, match)

        if best is None:
            out.append(_ESCAPED.sub(r"\1", source[pos:]), style=style)
            return

        kind, match = best
        if match.start() > pos:
            out.append(_ESCAPED.sub(r"\1", source[pos:match.start()]), style=style)
        _emit_inline(kind, match, style, out, depth)
        pos = match.end()


def _emit_inline(kind: str, match: re.Match, style: str, out: Text, depth: int) -> None:
    if kind == "code":
        out.append(match.group(2), style=_join(style, f"{MD_CODE} on {MD_CODE_BG}"))
    elif kind == "math":
        out.append(render_math(match.group(1)), style=_join(style, MD_MATH))
    elif kind == "link":
        label, url = match.group(1), match.group(2).strip("<>")
        _scan(label or url, _join(style, f"underline {MD_LINK}"), out, depth + 1)
        # The address is the half you cannot guess from the label, and it is
        # the half worth checking before clicking. Shown unless it is what
        # the label already says.
        if url and url != label:
            out.append(f" ({url})", style=MUTED)
    elif kind == "bolditalic":
        _scan(match.group(1), _join(style, "bold italic"), out, depth + 1)
    elif kind == "bold":
        _scan(match.group(1), _join(style, "bold"), out, depth + 1)
    elif kind == "strike":
        _scan(match.group(1), _join(style, "strike"), out, depth + 1)
    else:
        _scan(match.group(1), _join(style, "italic"), out, depth + 1)


def _cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith(r"\|"):
        line = line[:-1]
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", line)]


# ── Blocks ────────────────────────────────────────────────────────────────────

class _Renderer:
    def __init__(self, width: int, console) -> None:
        self.width = max(12, width)
        self.console = console
        self.out: list[Text] = []

    def render(self, source: str) -> list[Text]:
        text = source.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4)
        self._walk(text.split("\n"))
        while self.out and not self.out[-1].plain.strip():
            self.out.pop()
        return self.out

    def _walk(self, lines: list[str]) -> None:
        index = 0
        while index < len(lines):
            line = lines[index].rstrip()
            stripped = line.strip()

            if not stripped:
                self._blank()
                index += 1
                continue

            fence = _FENCE.match(line)
            if fence is not None:
                index = self._code(lines, index, fence)
                continue

            display = _DISPLAY.match(stripped)
            if display is not None:
                index = self._display_math(lines, index, display)
                continue

            if _RULE.match(line):
                self._rule()
                index += 1
                continue

            heading = _HEADING.match(line)
            if heading is not None:
                self._heading(heading)
                index += 1
                continue

            if _QUOTE.match(line):
                index = self._quote(lines, index)
                continue

            if "|" in line and index + 1 < len(lines) and _TABLE_SEP.match(lines[index + 1]):
                index = self._table(lines, index)
                continue

            ordered = _ORDERED.match(line)
            if ordered is not None:
                self._item(ordered.group(1), f"{ordered.group(2)}. ", ordered.group(3))
                index += 1
                continue

            bullet = _BULLET.match(line)
            if bullet is not None:
                indent = bullet.group(1)
                marker = _BULLETS[min(len(indent) // 2, len(_BULLETS) - 1)] + " "
                self._item(indent, marker, bullet.group(2))
                index += 1
                continue

            self._paragraph(line)
            index += 1

    # ── Emitting ──────────────────────────────────────────────────────────────

    def _emit(
        self,
        body: Text,
        first: str = "",
        rest: str = "",
        prefix_style: str = "",
        fold: bool = False,
    ) -> None:
        available = max(4, self.width - cell_len(first))
        wrapped = list(body.wrap(self.console, available,
                                overflow="fold" if fold else None))
        for number, piece in enumerate(wrapped or [Text("")]):
            # Rich leaves the space it folded on at the end of the line. It
            # is invisible in plain text and not invisible at all behind
            # inline code, where it drags the tint one cell past the word.
            piece.rstrip()
            prefix = first if number == 0 else rest
            line = Text()
            if prefix:
                line.append(prefix, style=prefix_style)
            line.append_text(piece)
            self.out.append(line)

    def _blank(self) -> None:
        # One blank between blocks, never a run of them, and never one at the
        # very top: a reply that opens on `## Heading` should not open on a
        # gap where the heading's breathing room would have been.
        if self.out and self.out[-1].plain.strip():
            self.out.append(Text(""))

    # ── Block kinds ───────────────────────────────────────────────────────────

    def _paragraph(self, line: str) -> None:
        indent, body = _INDENT.match(line).groups()
        pad = " " * min(len(indent), 8)
        self._emit(_inline(body), pad, pad)

    def _heading(self, match: re.Match) -> None:
        level = len(match.group(1))
        self._blank()
        self._emit(_inline(match.group(2), f"bold {ACCENT}" if level <= 2 else "bold"))

    def _rule(self) -> None:
        self._blank()
        self.out.append(Text("─" * min(self.width, 48), style=MD_RULE))
        self._blank()

    def _item(self, indent: str, marker: str, body: str) -> None:
        level = min(len(indent) // 2, len(_BULLETS) - 1)
        first = "  " * level + marker
        self._emit(_inline(body), first, " " * cell_len(first), prefix_style=ACCENT)

    def _quote(self, lines: list[str], index: int) -> int:
        body: list[str] = []
        while index < len(lines):
            match = _QUOTE.match(lines[index].rstrip())
            if match is None:
                break
            body.append(match.group(1))
            index += 1
        # Rendered recursively, so a quote can hold a list or a formula and
        # still look like one.
        inner = _Renderer(self.width - 2, self.console).render("\n".join(body))
        self._blank()
        for line in inner:
            bar = Text("▌ ", style=MD_RULE)
            bar.append_text(line)
            self.out.append(bar)
        self._blank()
        return index

    def _code(self, lines: list[str], index: int, fence: re.Match) -> int:
        marker, language = fence.group(1), fence.group(2)
        closing = re.compile(rf"^\s*{re.escape(marker[0])}{{{len(marker)},}}\s*$")
        index += 1
        body: list[str] = []
        while index < len(lines) and not closing.match(lines[index]):
            body.append(lines[index])
            index += 1
        index += 1                      # step over the fence, or off the end

        self._blank()
        if language:
            self.out.append(Text(f"  {language}", style=MUTED))
        for line in body or [""]:
            # Folded, not truncated: code is the one thing where the tail of
            # a long line is as load-bearing as the head.
            self._emit(Text(line or " ", style=MD_CODE), "  │ ", "  │ ",
                       prefix_style=MD_RULE, fold=True)
        self._blank()
        return index

    def _display_math(self, lines: list[str], index: int, match: re.Match) -> int:
        closer = "$$" if match.group(1) == "$$" else "\\]"
        rest = match.group(2).strip()
        body: list[str] = []

        if rest.endswith(closer):
            body.append(rest[: -len(closer)])
            index += 1
        else:
            if rest:
                body.append(rest)
            index += 1
            while index < len(lines):
                line = lines[index]
                if closer in line:
                    head = line[: line.index(closer)]
                    if head.strip():
                        body.append(head)
                    index += 1
                    break
                body.append(line)
                index += 1

        self._blank()
        for line in render_math("\n".join(body)).split("\n"):
            self._emit(Text(line, style=MD_MATH), "  ", "    ")
        self._blank()
        return index

    def _table(self, lines: list[str], index: int) -> int:
        header = _cells(lines[index])
        index += 2                      # the header, then its `---` rule
        rows: list[list[str]] = []
        while index < len(lines) and "|" in lines[index] and lines[index].strip():
            rows.append(_cells(lines[index]))
            index += 1

        columns = max([len(header)] + [len(row) for row in rows])
        widths = []
        for column in range(columns):
            longest = max(
                cell_len(_inline(_at(row, column)).plain) for row in [header] + rows
            )
            widths.append(max(3, min(longest, 40)))

        gaps = 2 * (columns - 1)
        if sum(widths) + gaps > self.width:
            room = max(columns * 3, self.width - gaps)
            scale = room / sum(widths)
            widths = [max(3, int(width * scale)) for width in widths]

        self._blank()
        self._row(header, widths, columns, "bold")
        self.out.append(Text("──".join("─" * width for width in widths), style=MD_RULE))
        for row in rows:
            self._row(row, widths, columns, "")
        self._blank()
        return index

    def _row(self, cells: list[str], widths: list[int], columns: int, style: str) -> None:
        line = Text()
        for column in range(columns):
            if column:
                line.append("  ")
            cell = _inline(_at(cells, column), style)
            cell.truncate(widths[column], overflow="ellipsis", pad=True)
            line.append_text(cell)
        line.rstrip()               # the last column's padding pads nothing
        self.out.append(line)


def _at(cells: list[str], index: int) -> str:
    return cells[index] if index < len(cells) else ""
