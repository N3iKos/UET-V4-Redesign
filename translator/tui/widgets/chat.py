"""
tui/widgets/chat.py — The chat transcript, with text arriving as it streams.

A reply is rendered as Markdown (see `markdown.py`), including any maths it
contains, because that is the form models answer in whether or not anyone
asked. What *you* typed is shown back exactly as typed: this is a tool for
testing prompts, and a prompt containing `[1]` or `**` has to be visible as
itself.

A reasoning model's thinking is shown too, in its own dim block above the
answer, and it is the one place in the app where that happens. Everywhere else
reasoning is a phase and never content (Activity Panel, Judge) — but the
Playground exists to answer "is this model actually thinking, and did my
on/off setting reach it?", and that question cannot be answered from a spinner.
The block is collapsible, because on a long chain of thought the answer is what
you came back for.

Built as `Text` rather than markup: a model that writes `[INST]` or a JSON
blob must not be able to kill a repaint.

Repainting is on a timer rather than per fragment. The worker hands
fragments over as they arrive; rewrapping the transcript on every token
would spend more time laying out than reading. Each turn keeps the lines it
last rendered, keyed on its text and the width, so a tick that changed
nothing costs a dictionary lookup rather than a re-parse of the whole
conversation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.text import Text
from textual.strip import Strip
from textual.widget import Widget

from ..theme import ACCENT, ERROR, MD_MATH, MUTED, OK
from .common import render_console, spinner_frame, strip_line, truncate
from .markdown import render_markdown, render_plain

YOU = "you"
MODEL = "model"
NOTICE = "notice"

# How much thinking to show when the block is collapsed.
THINK_PREVIEW_LINES = 3


@dataclass
class Turn:
    role: str
    text: str = ""
    stat: str = ""            # timing/token line, shown under a finished reply
    streaming: bool = False
    # Reasoning, kept separate from the answer so it can be styled, folded and
    # counted on its own. Only ever populated for a MODEL turn.
    reasoning: str = ""
    reasoning_open: bool = True
    reasoning_note: str = ""  # "thought for 2.1s · 340 tokens", set when done

    # Last render, and what it was rendered from.
    cache_key: tuple = field(default=(), init=False, repr=False)
    cache_lines: list = field(default_factory=list, init=False, repr=False)


@dataclass
class _Line:
    text: Text
    blank: bool = False


class ChatFeed(Widget):
    """A transcript of Turns. Call `refresh_lines()` after mutating one."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.turns: list[Turn] = []
        self._lines: list[_Line] = []
        self._tick = 0
        self._width = 0

    # ── Data ──────────────────────────────────────────────────────────────────

    def add(self, role: str, text: str = "", streaming: bool = False) -> Turn:
        turn = Turn(role=role, text=text, streaming=streaming)
        self.turns.append(turn)
        self.refresh_lines()
        return turn

    def append_to_last(self, fragment: str) -> None:
        if self.turns:
            self.turns[-1].text += fragment

    def append_reasoning_to_last(self, fragment: str) -> None:
        """Thinking arrives before the answer, and on its own channel."""
        if self.turns:
            self.turns[-1].reasoning += fragment

    def toggle_reasoning(self, index: int = -1) -> bool | None:
        """
        Fold or unfold one turn's thinking. Returns the new state, or None when
        that turn has no thinking to fold.
        """
        turn = self._turn_at(index)
        if turn is None or not turn.reasoning.strip():
            return None
        turn.reasoning_open = not turn.reasoning_open
        turn.cache_key = ()
        self.refresh_lines()
        return turn.reasoning_open

    def set_reasoning_open(self, open_: bool) -> None:
        """Fold or unfold every turn at once."""
        for turn in self.turns:
            if turn.reasoning:
                turn.reasoning_open = open_
                turn.cache_key = ()
        self.refresh_lines()

    def last_reasoning(self) -> str:
        turn = self._turn_at(-1)
        return turn.reasoning if turn is not None else ""

    def _turn_at(self, index: int) -> Turn | None:
        if not self.turns:
            return None
        try:
            return self.turns[index]
        except IndexError:
            return None

    def clear(self) -> None:
        self.turns.clear()
        self.refresh_lines()

    def tick(self) -> None:
        """Repaint while something is still arriving — cheap enough at 10fps."""
        if self.turns and self.turns[-1].streaming:
            self._tick += 1
            self.refresh_lines()

    # ── Layout ────────────────────────────────────────────────────────────────

    def refresh_lines(self) -> None:
        width = max(20, (self.size.width or 80) - 2)
        console = render_console()
        lines: list[_Line] = []

        for index, turn in enumerate(self.turns):
            if index:
                lines.append(_Line(Text(""), blank=True))
            lines.append(_Line(self._speaker(turn)))
            lines.extend(_Line(line) for line in self._thinking(turn, width, console))
            body = self._body(turn, width, console)
            if turn.streaming:
                # The spinner rides the end of the text, so it sits wherever
                # the next token will land instead of on a line of its own.
                body = list(body)
                spinner = Text(spinner_frame(self._tick), style=ACCENT)
                if body:
                    tail = body[-1].copy()
                    tail.append_text(spinner)
                    body[-1] = tail
                else:
                    body = [spinner]
            lines.extend(_Line(line) for line in body)
            if turn.stat:
                lines.append(_Line(Text(turn.stat, style=MUTED)))

        self._width = width
        self._lines = lines
        self.refresh(layout=True)

    def _thinking(self, turn: Turn, width: int, console) -> list[Text]:
        """
        The thinking block: a header saying what happened, then the text.

        Dim and clearly labelled, so it can never be mistaken for the answer.
        Rendered as plain text rather than Markdown — a chain of thought is not
        a document, and half a fenced block mid-stream would render as noise.
        """
        if not turn.reasoning.strip():
            return []

        header = Text()
        header.append("▾ " if turn.reasoning_open else "▸ ", style=MUTED)
        header.append("thinking", style=f"bold {MD_MATH}")
        if turn.reasoning_note:
            header.append(f"  {turn.reasoning_note}", style=MUTED)
        elif turn.streaming:
            header.append("  " + spinner_frame(self._tick), style=MD_MATH)
        if not turn.reasoning_open:
            header.append("   (t to unfold)", style=MUTED)

        lines = [header]
        body = render_plain(turn.reasoning, max(20, width - 2), console, MD_MATH)
        if not turn.reasoning_open:
            # Enough to see it is thinking, not enough to bury the answer.
            preview = body[:THINK_PREVIEW_LINES]
            for line in preview:
                lines.append(_indent(line))
            hidden = len(body) - len(preview)
            if hidden > 0:
                lines.append(Text(f"    …{hidden} more line(s)", style=MUTED))
            return lines
        for line in body:
            lines.append(_indent(line))
        return lines

    def _body(self, turn: Turn, width: int, console) -> list[Text]:
        key = (turn.role, turn.text, turn.streaming, width)
        if turn.cache_key == key:
            return turn.cache_lines

        if not turn.text.strip():
            if turn.streaming:
                lines = []
            elif turn.role == MODEL and turn.reasoning.strip():
                # It thought and said nothing — worth stating, because an empty
                # answer with a full chain of thought is a real failure mode.
                lines = [Text("(the model returned reasoning but no answer)",
                              style=MUTED)]
            else:
                lines = [
                    Text("(empty reply)" if turn.role == MODEL else " ", style=MUTED)
                ]
        elif turn.role == MODEL:
            lines = render_markdown(turn.text, width, console)
        else:
            style = ERROR if turn.role == NOTICE else ""
            lines = render_plain(turn.text, width, console, style)

        turn.cache_key, turn.cache_lines = key, lines
        return lines

    def _speaker(self, turn: Turn) -> Text:
        if turn.role == YOU:
            return Text("you", style=f"bold {MUTED}")
        if turn.role == NOTICE:
            return Text("error", style=f"bold {ERROR}")
        return Text("model", style=f"bold {ACCENT if turn.streaming else OK}")

    def get_content_height(self, container, viewport, width: int) -> int:
        # A resize changes the wrap, so relay out before reporting a height.
        if max(20, (width or 80) - 2) != self._width and self.turns:
            self.refresh_lines()
        return max(1, len(self._lines))

    def render_line(self, y: int) -> Strip:
        if y >= len(self._lines):
            return strip_line(Text(""), self.size.width, self.rich_style)
        return strip_line(self._lines[y].text, self.size.width, self.rich_style)


def _indent(line: Text, width: int = 4) -> Text:
    """Indent a rendered line without losing its styling."""
    indented = Text(" " * width)
    indented.append_text(line)
    return indented
