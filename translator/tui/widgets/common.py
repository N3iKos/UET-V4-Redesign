"""
tui/widgets/common.py — Shared widgets used by every screen.

Picker       : arrow-key list, Enter selects, optional multi-select
KeyHints     : a row of `key action` chips — each one a real click target
Spinner      : braille loading animation (no token streaming, anywhere)
LabeledInput : text field with a default applied when Enter is pressed empty
StatusBar    : the footer line, showing only the keys valid right now
ErrorBox     : renders a provider's raw error verbatim
ColumnPreview: the spreadsheet grid in the wizard — columns are click targets

MOUSE: keyboard remains the primary interface — nothing here needs a mouse —
but everything the keyboard can reach, the mouse can too. Picker rows hover
and click; KeyHints turns the keys a screen advertises into buttons that
press those keys for you, which is what lets a pointer finish a form the
keyboard would submit with Enter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from rich.cells import cell_len, set_cell_size
from rich.segment import Segment
from rich.style import Style as RichStyle
from rich.text import Text
from textual import events
from textual.binding import Binding, NoBinding
from textual.containers import Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Input, Label, Static, TextArea

from ..theme import (
    ACCENT,
    ERROR,
    HOVER_BG,
    MUTED,
    OK,
    SELECT_BG,
    SELECT_DIM_BG,
    SPINNER_FRAMES,
    WARN,
)


_UNSET = object()


@dataclass
class Option:
    """One row in a Picker."""
    label: str
    value: Any = _UNSET
    hint: str = ""              # dim text shown after the label
    enabled: bool = True
    selected: bool = False      # multi-select state
    style: str = ""             # optional colour override for the label

    def __post_init__(self) -> None:
        if self.value is _UNSET:
            self.value = self.label


class Picker(Widget, can_focus=True):
    """
    Arrow-key list. Up/Down move, Enter selects, Space toggles when
    multi_select is on. Disabled options are skipped by navigation but stay
    visible (e.g. an inactive strategy selector).

    The mouse does the same job: the row under the pointer lights up, and a
    click moves the cursor there and then does what Enter would — except on
    multi-select lists, where a click toggles the row instead, because that
    is what Space means there.

    `click_activates=False` for lists where a click should only *set* a
    value, not commit the screen (radio groups, and the provider/connection
    columns that merely narrow the model list). Those still commit on a
    double-click.
    """

    ALLOW_SELECT = False        # rows are click targets, not selectable text

    BINDINGS = [
        Binding("up,k", "cursor_up", "up", show=False),
        Binding("down,j", "cursor_down", "down", show=False),
        Binding("home", "cursor_first", "first", show=False),
        Binding("end", "cursor_last", "last", show=False),
        Binding("enter", "select", "select", show=False),
        Binding("space", "toggle", "toggle", show=False),
    ]

    index = reactive(0)
    hover_index = reactive(-1)

    class Selected(Message):
        def __init__(self, picker: "Picker", option: Option, index: int) -> None:
            super().__init__()
            self.picker = picker
            self.option = option
            self.index = index

    class Highlighted(Message):
        def __init__(self, picker: "Picker", option: Option, index: int) -> None:
            super().__init__()
            self.picker = picker
            self.option = option
            self.index = index

    class Toggled(Message):
        def __init__(self, picker: "Picker", option: Option) -> None:
            super().__init__()
            self.picker = picker
            self.option = option

    def __init__(
        self,
        options: Iterable[Option] | None = None,
        multi_select: bool = False,
        click_activates: bool = True,
        max_visible: int | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.options: list[Option] = list(options or [])
        self.multi_select = multi_select
        self.click_activates = click_activates
        # A scrolled list instead of a screen-growing one: the file screen
        # with dozens of spreadsheets must stay compact (spec 2.1). None keeps
        # the old grow-to-fit behaviour for every other picker.
        self.max_visible = max_visible

    # ── Data ──────────────────────────────────────────────────────────────────

    def set_options(self, options: Iterable[Option], keep_index: bool = False) -> None:
        previous = self.index
        self.options = list(options)
        self.index = min(previous, max(0, len(self.options) - 1)) if keep_index else 0
        if self.options and not self.options[self.index].enabled:
            self._advance(1)
        self.hover_index = -1       # the row under the pointer is a different one now
        self.refresh(layout=True)

    @property
    def current(self) -> Option | None:
        if 0 <= self.index < len(self.options):
            return self.options[self.index]
        return None

    def selected_values(self) -> list[Any]:
        return [o.value for o in self.options if o.selected]

    # ── Navigation ────────────────────────────────────────────────────────────

    def _advance(self, step: int) -> None:
        if not self.options:
            return
        total = len(self.options)
        index = self.index
        for _ in range(total):
            index = (index + step) % total
            if self.options[index].enabled:
                break
        self.index = index

    def action_cursor_up(self) -> None:
        self._advance(-1)

    def action_cursor_down(self) -> None:
        self._advance(1)

    def action_cursor_first(self) -> None:
        self.index = 0
        if self.options and not self.options[0].enabled:
            self._advance(1)

    def action_cursor_last(self) -> None:
        self.index = max(0, len(self.options) - 1)
        if self.options and not self.options[self.index].enabled:
            self._advance(-1)

    def action_select(self) -> None:
        option = self.current
        if option is not None and option.enabled:
            self.post_message(self.Selected(self, option, self.index))

    def action_toggle(self) -> None:
        if not self.multi_select:
            return
        option = self.current
        if option is not None and option.enabled:
            option.selected = not option.selected
            self.post_message(self.Toggled(self, option))
            self.refresh()

    def watch_index(self, _old: int, new: int) -> None:
        option = self.current
        if option is not None:
            self.post_message(self.Highlighted(self, option, new))
        self.refresh()

    def watch_hover_index(self, _old: int, _new: int) -> None:
        self.refresh()

    # ── Mouse ─────────────────────────────────────────────────────────────────

    def _row_at(self, event: events.MouseEvent) -> int | None:
        """
        Pointer -> option index, or None if it is not over one.

        `get_content_offset` takes off any border and padding — none today,
        but the rows are painted in content space and this keeps the two from
        drifting apart if the CSS ever grows some.
        """
        offset = event.get_content_offset(self)
        if offset is None:
            return None
        row = offset.y + self.scroll_offset.y
        return row if 0 <= row < len(self.options) else None

    def on_mouse_move(self, event: events.MouseMove) -> None:
        row = self._row_at(event)
        self.hover_index = row if row is not None and self.options[row].enabled else -1

    def on_leave(self, _event: events.Leave) -> None:
        self.hover_index = -1

    def on_click(self, event: events.Click) -> None:
        row = self._row_at(event)
        if row is None:
            return
        event.stop()
        option = self.options[row]
        if not option.enabled:
            return
        self.focus()
        self.index = row

        if self.multi_select:
            # A row on these lists is a checkbox, so a click means Space, not
            # Enter; the footer's ⏎ chip is what commits. First click of a
            # chain only — a double-click would otherwise toggle straight back.
            if event.chain == 1:
                self.action_toggle()
        elif event.chain == (1 if self.click_activates else 2):
            self.post_message(self.Selected(self, option, row))

    # ── Render ────────────────────────────────────────────────────────────────

    def get_content_height(self, container, viewport, width: int) -> int:
        height = max(1, len(self.options))
        if self.max_visible is not None:
            height = min(height, self.max_visible)
        return height

    def render_line(self, y: int) -> Strip:
        row = y + self.scroll_offset.y
        return _strip(self._line_text(row), self.size.width, self._row_style(row))

    def _row_style(self, y: int) -> RichStyle:
        """
        Full-width band behind the cursor row and the hovered row. Rendering
        it as the row's *base* style — rather than styling each fragment —
        is what makes it reach across the padding to the widget edge.
        """
        if y >= len(self.options):
            return self.rich_style
        if y == self.index:
            return self.rich_style + RichStyle(
                bgcolor=SELECT_BG if self.has_focus else SELECT_DIM_BG
            )
        if y == self.hover_index:
            return self.rich_style + RichStyle(bgcolor=HOVER_BG)
        return self.rich_style

    def _line_text(self, y: int) -> Text:
        if y >= len(self.options):
            return Text("")
        option = self.options[y]
        focused = y == self.index and self.has_focus
        current = y == self.index

        line = Text(style=self._row_style(y))
        line.append("> " if current else "  ", style=ACCENT if current else "")
        if self.multi_select:
            # Only a selectable row gets a checkbox. A disabled row — a section
            # heading, a status line, an error detail — is not a choice, and a
            # `[ ]` beside it reads as a broken checkbox that will not tick.
            if option.enabled:
                line.append("[x] " if option.selected else "[ ] ",
                            style=OK if option.selected else MUTED)
            else:
                line.append("    ")

        if not option.enabled:
            style = MUTED
        elif option.style:
            style = option.style
        elif focused or current:
            style = f"bold {ACCENT}"
        else:
            style = ""
        line.append(option.label, style=style)

        if option.hint:
            line.append("   " + option.hint, style=MUTED)
        return line


class Spinner(Static):
    """
    Braille spinner + label. Used for every in-flight request — there is no
    token streaming anywhere in v4, by design (04_TUI_SPEC.md §4).
    """

    frame = reactive(0)

    def __init__(self, label: str = "Working…", **kwargs) -> None:
        super().__init__(**kwargs)
        self._label = label
        self._timer = None

    def on_mount(self) -> None:
        self._timer = self.set_interval(0.08, self._tick)

    def _tick(self) -> None:
        self.frame = (self.frame + 1) % len(SPINNER_FRAMES)

    def set_label(self, label: str) -> None:
        self._label = label
        self.refresh()

    def watch_frame(self, _old: int, _new: int) -> None:
        self.refresh()

    def render(self) -> Text:
        text = Text()
        text.append(SPINNER_FRAMES[self.frame] + "  ", style=ACCENT)
        text.append(self._label)
        return text


def spinner_frame(tick: int) -> str:
    """Frame for widgets that animate many rows from one timer."""
    return SPINNER_FRAMES[tick % len(SPINNER_FRAMES)]


class LabeledInput(Vertical):
    """
    Label + input + optional hint. Enter on an empty field applies the
    default, mirroring v3.6's "Enter = pakai default" convention.
    """

    def __init__(
        self,
        label: str,
        value: str = "",
        placeholder: str = "",
        default: str = "",
        hint: str = "",
        password: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._label = label
        self._default = default
        self._hint = hint
        self._input = Input(
            value=value,
            placeholder=placeholder or (f"default: {default}" if default else ""),
            password=password,
        )

    def compose(self):
        yield Label(self._label, classes="muted")
        yield self._input
        if self._hint:
            yield Label(self._hint, classes="hint")

    @property
    def input(self) -> Input:
        return self._input

    @property
    def value(self) -> str:
        raw = self._input.value.strip()
        return raw or self._default

    @value.setter
    def value(self, new: str) -> None:
        self._input.value = new

    def focus_input(self, scroll_visible: bool = True) -> None:
        self._input.focus(scroll_visible=scroll_visible)


# Footer glyph -> the key Textual actually binds.
_KEY_NAMES = {
    "⏎": "enter",
    "↵": "enter",
    "⇧⏎": "shift+enter",
    "↑": "up",
    "↓": "down",
    "←": "left",
    "→": "right",
    "esc": "escape",
    "space": "space",
    "tab": "tab",
}

# Hints that read as one chip but are really two keys side by side.
_KEY_PAIRS = {"↑↓": ("↑", "↓"), "←→": ("←", "→")}

# Advertised but deliberately not clickable: a stray click must not be able
# to kill a running job.
_KEY_INERT = {"^C"}

# The colour roles, spelled the way a caller is likely to spell them.
_ROLE_STYLES = {"ok": OK, "warn": WARN, "error": ERROR,
                "muted": MUTED, "accent": ACCENT, "info": ACCENT}


def escape_markup(text: str) -> str:
    """
    Make arbitrary text safe to interpolate into a markup string.

    Neither `rich.markup.escape` nor `textual.markup.escape` is enough: both
    only escape what they themselves would read as a *valid* tag, and
    Textual's parser then swallows what they left behind. `[N]` survives Rich
    and dies in Textual — and so does `[BR]`, which is this app's own
    placeholder token, so a translation carrying a line break loses it on the
    way into any panel. Escaping every opening bracket is the one form that
    survives both parsers.

    Where the *whole* string is arbitrary, prefer handing `Static.update` a
    `rich.text.Text`: that skips markup parsing altogether and cannot be got
    wrong later.
    """
    return (text or "").replace("[", r"\[")


def _is_style(style: str) -> bool:
    try:
        RichStyle.parse(style)
    except Exception:
        return False
    return True


def _key_name(glyph: str) -> str | None:
    """The key a hint chip presses, or None if the chip is informational."""
    if glyph in _KEY_INERT:
        return None
    if glyph in _KEY_NAMES:
        return _KEY_NAMES[glyph]
    return glyph if len(glyph) == 1 else None


class KeyHints(Static):
    """
    A row of `key  action` hints, as used in the footer and at the foot of
    the modals.

    Each chip is a button: it lights up under the pointer and, when clicked,
    does what the key beside it does. Screens already declare exactly which
    keys are live right now, so this gives the mouse everything the keyboard
    has — including the Enter that submits a form — without any screen having
    to grow a parallel set of buttons.
    """

    ALLOW_SELECT = False        # chips are click targets, not selectable text

    def __init__(self, keys: Iterable[tuple[str, str]] | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._keys: list[tuple[str, str]] = list(keys or [])
        self._spans: list[tuple[int, int, str]] = []    # start, end, key name
        self._hover = -1

    def set_keys(self, keys: list[tuple[str, str]]) -> None:
        # Drop the old chip positions rather than let them survive into the
        # frame before the repaint: a click landing there would press the key
        # that used to be in that spot. A dead click is the safer failure.
        self._keys = keys
        self._spans = []
        self._hover = -1
        self.refresh()

    # ── Mouse ─────────────────────────────────────────────────────────────────

    def _span_at(self, x: int) -> int:
        for index, (start, end, _key) in enumerate(self._spans):
            if start <= x < end:
                return index
        return -1

    def _chip_at(self, event: events.MouseEvent) -> int:
        """
        Spans are measured in content cells, but mouse coordinates arrive
        relative to the widget's outer region — and the footer carries
        `padding: 0 1`, so the two are a cell apart. Without this every chip
        would light up and fire one cell right of where it is painted.
        """
        offset = event.get_content_offset(self)
        return -1 if offset is None else self._span_at(offset.x)

    def on_mouse_move(self, event: events.MouseMove) -> None:
        hover = self._chip_at(event)
        if hover != self._hover:
            self._hover = hover
            self.refresh()

    def on_leave(self, _event: events.Leave) -> None:
        if self._hover != -1:
            self._hover = -1
            self.refresh()

    async def on_click(self, event: events.Click) -> None:
        index = self._chip_at(event)
        if index == -1:
            return
        event.stop()
        # One press per double-click. Chips look like buttons and get
        # double-clicked like buttons, and some of them delete things.
        if event.chain == 1:
            await self._press(self._spans[index][2])

    async def _press(self, key: str) -> None:
        """
        Do what *this screen* says the key does, rather than replaying a
        keystroke — because on the form screens the focused Input would eat
        it. Input binds `left` to move its own cursor, and it types any plain
        letter, so simulating would leave `←  back` dead and turn `f  fetch`
        into an "f" in the text box.

        Keys the screen does not bind fall through to a real key press, and
        reach whatever has focus: that is how `⏎` still submits a form and
        `space` still ticks the row under the cursor. Nothing here is
        focusable, so the click never moved focus in the first place.
        """
        try:
            bindings = self.screen._bindings.get_bindings_for_key(key)
        except NoBinding:
            bindings = []
        if bindings:
            await self.app.run_action(bindings[0].action, self.screen)
        else:
            self.app.simulate_key(key)

    # ── Render ────────────────────────────────────────────────────────────────

    def render(self) -> Text:
        text = Text()
        spans: list[tuple[int, int, str]] = []
        for glyph, action in self._keys:
            chips = _KEY_PAIRS.get(glyph, (glyph,))
            single = len(chips) == 1
            for position, chip in enumerate(chips):
                key = _key_name(chip)
                hot = key is not None and self._hover == len(spans)
                start = text.cell_len
                if key is None:
                    text.append(f" {chip} ", style=MUTED)
                else:
                    text.append(
                        f" {chip} ",
                        style=f"bold {ACCENT} reverse" if hot else f"bold {ACCENT}",
                    )
                end = text.cell_len

                if single:
                    text.append(action, style=ACCENT if hot else MUTED)
                    end = text.cell_len              # the word is part of the button
                    text.append("  ")
                elif position == len(chips) - 1:
                    # A split chip labels both halves at once, so the action
                    # word belongs to neither and stays outside both targets.
                    text.append(f"{action}  ", style=MUTED)

                if key is not None:
                    spans.append((start, end, key))
        self._spans = spans
        return text


class PasteArea(TextArea):
    """
    A multi-line box where Enter means "done" and Shift+Enter means "new
    line" — the way a chat box behaves, rather than the way an editor does.

    Three keys make a new line, and the redundancy is not a nicety. A
    terminal can only tell Shift+Enter (or Ctrl+Enter) from a plain Enter if
    it speaks the kitty keyboard protocol, which Textual asks for but not
    every terminal grants; where it is not granted, both arrive as Enter and
    would submit. Ctrl+J is a genuinely different byte — LF against Enter's
    CR — so it survives every terminal, and it is what the hint offers when
    the other two do nothing.

    `min_lines`/`max_lines` make the box grow with its contents between those
    bounds, for the chat entry, where a fixed tall box would eat the
    transcript it exists to answer.
    """

    NEWLINE_KEYS = ("shift+enter", "ctrl+enter", "ctrl+j")

    class Submitted(Message):
        def __init__(self, area: "PasteArea") -> None:
            super().__init__()
            self.area = area

    def __init__(self, *args, min_lines: int = 0, max_lines: int = 0, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._min_lines = min_lines
        self._max_lines = max_lines

    def on_key(self, event: events.Key) -> None:
        # Runs before TextArea's own handler, which maps enter to a newline;
        # prevent_default stops the rest of the chain from seeing the key.
        if event.key in self.NEWLINE_KEYS:
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted(self))

    # ── Growing ───────────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self.fit_height()

    def on_text_area_changed(self, _event: TextArea.Changed) -> None:
        # Our own message, handled before it bubbles — the screen still sees
        # it. Not stopped: a caller may well want to know the text changed.
        self.fit_height()

    def fit_height(self) -> None:
        """
        Size to the wrapped content, within the bounds. Wrapped, not
        `line_count`: a single long line that soft-wraps to three occupies
        three, and a box that says otherwise hides what you are typing.
        """
        if not self._max_lines:
            return
        try:
            rows = self.wrapped_document.height
        except Exception:
            rows = self.document.line_count
        rows = max(self._min_lines, min(self._max_lines, rows or 1))
        self.styles.height = rows + 2       # the `tall` border, top and bottom


class LabeledTextArea(Vertical):
    """LabeledInput's shape, for a field that holds more than one line."""

    def __init__(
        self,
        label: str,
        value: str = "",
        placeholder: str = "",
        hint: str = "",
        min_lines: int = 0,
        max_lines: int = 0,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._label = label
        self._hint = hint
        self._area = PasteArea(value, soft_wrap=True, placeholder=placeholder,
                               min_lines=min_lines, max_lines=max_lines)

    def compose(self):
        # An empty label is no label — it would otherwise cost a whole row,
        # which is a row of transcript where this sits under a chat.
        if self._label:
            yield Label(self._label, classes="muted")
        yield self._area
        if self._hint:
            yield Label(self._hint, classes="hint")

    @property
    def area(self) -> PasteArea:
        return self._area

    @property
    def value(self) -> str:
        return self._area.text

    @value.setter
    def value(self, new: str) -> None:
        self._area.text = new
        self._area.fit_height()

    def focus_input(self, scroll_visible: bool = True) -> None:
        self._area.focus(scroll_visible=scroll_visible)


class StatusBar(KeyHints):
    """Footer showing only the keys valid on the current screen."""

    def __init__(self, **kwargs) -> None:
        super().__init__(id="footer", **kwargs)
        self._message = ""
        self._message_style = ""

    def notify_text(self, message: str, style: str = "") -> None:
        # Callers pass a colour from theme.py, but a role name like "warn"
        # reads just as naturally and Rich cannot parse one — it raises out of
        # the compositor, where nothing catches it, and the app dies on a
        # repaint. Resolve it here instead, while there is still a caller to
        # blame, and fall back to muted rather than taking the app down.
        self._message = message
        self._message_style = _ROLE_STYLES.get(style, style)
        if self._message_style and not _is_style(self._message_style):
            self._message_style = MUTED
        self.refresh()

    def render(self) -> Text:
        text = super().render()
        if self._message:
            text.append("  " + self._message, style=self._message_style or MUTED)
        return text


class ErrorBox(Static):
    """
    Renders a provider's raw error text verbatim. v4 talks to arbitrary
    endpoints, so the real message must always reach the user
    (03_PROVIDER_SYSTEM.md §7).

    CSS on the call site bounds its height (`max-height` + overflow scroll), so
    a long provider payload can be scrolled rather than pushing the layout
    around it — wherever the box sits, it never grows without limit.
    """

    def __init__(self, **kwargs) -> None:
        # The id is a fallback: a call site that passes its own id (the Judge
        # rail, say) needs the CSS to match that instead, and `id` cannot be
        # passed twice into the same constructor.
        kwargs.setdefault("id", "error-box")
        super().__init__(**kwargs)
        self.display = False
        self._title = ""
        self._body = ""

    def show(self, body: str, title: str = "provider response") -> None:
        self._title = title
        self._body = (body or "").strip()
        self.display = bool(self._body)
        self.refresh()

    def clear(self) -> None:
        self._body = ""
        self.display = False

    def render(self) -> Text:
        text = Text()
        text.append(f"{self._title}\n", style=f"bold {ERROR}")
        text.append(self._body, style=ERROR)
        return text


class ProviderNotice(Static):
    """
    A small provider-status card, bottom-right of a results screen.

    Identical messages are merged and counted rather than repeated: one
    "temporarily unavailable ×14" line instead of fourteen. It stays narrow and
    a few lines tall, so the Judge Results list keeps its space; the raw errors
    still go to the events rail / log for debugging. Clickable to open the
    Activity view; auto-dismisses when a recovery notice supersedes a
    non-fatal one.

    Kind drives colour and dismissal:
      warn  — temporary (rate limit / server / retry). Auto-clears on recovery.
      error — fatal (confirmed quota, auth, all connections gone). Sticks until
              dismissed.
      ok    — recovery. Shown briefly, then fades on its own.
    """

    class Clicked(Message):
        pass

    DEFAULT_CSS = """
    ProviderNotice {
        display: none;
        dock: bottom;
        layer: overlay;
        width: 40;
        max-width: 50%;
        height: auto;
        margin: 0 1 1 0;
        padding: 0 1;
        border: round #7c8494;
        background: #141821;
    }
    ProviderNotice.-warn { border: round #fbbf24; }
    ProviderNotice.-error { border: round #f87171; }
    ProviderNotice.-ok { border: round #4ade80; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(id="provider-notice", **kwargs)
        self._title = ""
        self._detail = ""
        self._count = 0
        self._kind = "warn"
        self._last_ref = ""       # e.g. "request 237"
        self._fade_timer = None

    def notify_issue(
        self,
        title: str,
        detail: str = "",
        kind: str = "warn",
        ref: str = "",
    ) -> None:
        """
        Show or update the card.

        A repeat of the same (title, detail) bumps the counter instead of
        stacking; a different message resets it. `ref` is the latest request /
        batch this concerns, shown so a user can see it is still happening.
        """
        same = title == self._title and detail == self._detail and kind == self._kind
        if same:
            self._count += 1
        else:
            self._title = title
            self._detail = detail
            self._kind = kind
            self._count = 1
        if ref:
            self._last_ref = ref
        self._apply_kind(kind)
        self.display = True
        self.refresh()
        # A recovery/notice card is transient; a fatal one stays put.
        self._arm_fade(kind)

    def _apply_kind(self, kind: str) -> None:
        for cls in ("-warn", "-error", "-ok"):
            self.remove_class(cls)
        self.add_class(f"-{kind}" if kind in ("warn", "error", "ok") else "-warn")

    def _arm_fade(self, kind: str) -> None:
        if self._fade_timer is not None:
            try:
                self._fade_timer.stop()
            except Exception:
                pass
            self._fade_timer = None
        # Auto-dismiss after 5 seconds
        try:
            self._fade_timer = self.set_timer(5.0, self.dismiss)
        except Exception:
            pass

    def dismiss(self) -> None:
        self.display = False
        self._title = ""
        self._detail = ""
        self._count = 0
        self._last_ref = ""

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.post_message(self.Clicked())

    def render(self) -> Text:
        colour = {"warn": WARN, "error": ERROR, "ok": OK}.get(self._kind, WARN)
        text = Text()
        text.append(f"{self._title}\n", style=f"bold {colour}")
        if self._detail:
            text.append(f"{escape_markup(self._detail)}\n", style=MUTED)
        tail_bits = []
        if self._count > 1:
            tail_bits.append(f"×{self._count}")
        if self._last_ref:
            tail_bits.append(self._last_ref)
        tail = "  ·  ".join(tail_bits)
        if tail:
            text.append(f"{tail}", style=MUTED)
        return text


@dataclass
class FeedRow:
    """One source -> translation pair in the run screen feed."""
    row_num: int
    source: str
    result: str = ""
    state: str = "pending"      # pending | ok | retry | failed | skipped
    error: str = ""
    batch_num: int = 0


class TranslationFeed(Widget):
    """
    The left column of the run screen: source line, then the translation
    indented beneath it. Rows appear as soon as their batch starts (showing
    a spinner) and fill in when the batch lands.

    The feed keeps only the last `max_rows` rows, so a 12,000-row job does not
    become 36,000 rendered lines. That trim is *suspended while someone has
    scrolled back*: dropping rows off the top slides the content up under a
    parked viewport, so the lines being read drift away on their own — the view
    would creep forward towards the newest output without anyone touching it.
    Holding the trim keeps every line index stable, and the backlog is trimmed in
    one go when following resumes, where it costs nothing because the view jumps
    to the end anyway.
    """

    tick = reactive(0)

    def __init__(self, max_rows: int = 400, **kwargs) -> None:
        super().__init__(**kwargs)
        self._rows: list[FeedRow] = []
        self._index: dict[int, FeedRow] = {}
        self._max_rows = max_rows
        self.follow = True
        # True while the reader has scrolled back: see the class docstring.
        self._hold_trim = False

    def on_mount(self) -> None:
        self.set_interval(0.1, self._tick)

    def _tick(self) -> None:
        if any(r.state == "pending" for r in self._rows[-40:]):
            self.tick += 1

    def watch_tick(self, _old: int, _new: int) -> None:
        self.refresh()

    # ── Data ──────────────────────────────────────────────────────────────────

    def add_pending(self, batch_num: int, rows: list[tuple[int, str]]) -> None:
        for row_num, source in rows:
            if row_num in self._index:
                continue
            entry = FeedRow(row_num=row_num, source=source, batch_num=batch_num)
            self._index[row_num] = entry
            self._rows.append(entry)
        self._trim()
        self.refresh(layout=True)

    def resolve(
        self,
        row_num: int,
        result: str,
        state: str = "ok",
        error: str = "",
    ) -> None:
        entry = self._index.get(row_num)
        if entry is None:
            entry = FeedRow(row_num=row_num, source="")
            self._index[row_num] = entry
            self._rows.append(entry)
            self._trim()
        entry.result = result
        entry.state = state
        entry.error = error
        self.refresh(layout=True)

    def clear(self) -> None:
        self._rows.clear()
        self._index.clear()
        self.refresh(layout=True)

    def hold_trim(self, hold: bool) -> None:
        """
        Stop or resume dropping rows off the top.

        Called by whoever owns the scroller when following is detached and
        re-attached. Resuming trims the backlog immediately, so the memory the
        hold cost is given back at the moment the view stops depending on it.
        """
        self._hold_trim = hold
        if not hold:
            self._trim()
            self.refresh(layout=True)

    def _trim(self) -> None:
        if self._hold_trim:
            return
        overflow = len(self._rows) - self._max_rows
        if overflow > 0:
            for entry in self._rows[:overflow]:
                self._index.pop(entry.row_num, None)
            del self._rows[:overflow]

    # ── Render ────────────────────────────────────────────────────────────────

    #: Lines each row occupies: source, translation, blank. Named because the
    #: scroll arithmetic in `take_trimmed_lines` has to agree with the renderer.
    LINES_PER_ROW = 3

    def get_content_height(self, container, viewport, width: int) -> int:
        return max(1, len(self._rows) * self.LINES_PER_ROW)

    def render_line(self, y: int) -> Strip:
        return _strip(self._line_text(y), self.size.width, self.rich_style)

    def _line_text(self, y: int) -> Text:
        block, part = divmod(y, self.LINES_PER_ROW)
        if block >= len(self._rows):
            return Text("")
        entry = self._rows[block]
        width = max(20, self.size.width)

        if part == 0:
            text = Text()
            text.append(f"{entry.row_num:>7}  ", style=MUTED)
            text.append(_truncate(entry.source, width - 9))
            return text

        if part == 1:
            text = Text(" " * 6)
            text.append("→  ", style=MUTED)
            if entry.state == "pending":
                text.append(spinner_frame(self.tick) + " generating…", style=ACCENT)
            elif entry.state == "retry":
                text.append(
                    spinner_frame(self.tick) + " " + (entry.result or "retrying…"),
                    style=WARN,
                )
            elif entry.state == "failed":
                body = entry.result or "[TRANSLATE_FAILED]"
                if entry.error:
                    body = f"{body}  {_truncate(entry.error, max(10, width - len(body) - 14))}"
                text.append(body, style=ERROR)
            elif entry.state == "skipped":
                text.append(entry.result or "(skipped)", style=MUTED)
            else:
                text.append(_truncate(entry.result, width - 9), style=OK)
            return text

        return Text("")


@dataclass
class ColumnRole:
    """One column the job has a use for, and how the preview should show it."""
    tag: str            # the short label printed beside the column letter
    label: str          # what it is called in prose, for the click prompt
    colour: str


class ColumnPreview(Widget):
    """
    A spreadsheet-shaped look at the source file — and the quickest way to
    fill the columns form: the column under the pointer lights up and a click
    posts `ColumnPicked` for it, so the screen can drop the letter straight
    into whichever field it is asking for.

    The grid is cached here rather than re-read on every repaint, because
    hovering repaints constantly and reading the sheet is disk work.
    """

    ALLOW_SELECT = False

    MIN_GUTTER = 6          # the row-number column down the left
    CAPTION_LINES = 1       # caption, then letters, then headers, then data

    hover_column = reactive(0)      # 1-based sheet column under the pointer, 0 = none

    class ColumnPicked(Message):
        def __init__(self, column: int) -> None:
            super().__init__()
            self.column = column        # 1-based, matches src_col/dst_col

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._letters: list[str] = []
        self._headers: list[str] = []
        self._rows: list[list[str]] = []
        self._start = 1
        self._total = 0
        self._roles: dict[int, ColumnRole] = {}
        self._prompt: ColumnRole | None = None
        self._error = ""

    # ── Data ──────────────────────────────────────────────────────────────────

    def set_grid(
        self,
        letters: list[str],
        headers: list[str],
        rows: list[list[str]],
        start: int,
        total: int,
        roles: dict[int, ColumnRole],
        prompt: ColumnRole | None = None,
    ) -> None:
        """`roles` tints already-chosen columns; `prompt` names what a click fills."""
        self._letters = letters
        self._headers = headers
        self._rows = rows
        self._start = start
        self._total = total
        self._roles = roles
        self._prompt = prompt
        self._error = ""
        self.refresh(layout=True)

    def set_prompt(self, prompt: ColumnRole | None) -> None:
        """Re-aim at a different role — only the caption changes, so this
        repaints without going back to the sheet."""
        self._prompt = prompt
        self.refresh()

    def set_error(self, message: str) -> None:
        self._error = message
        self._letters = []
        self._rows = []
        self.hover_column = 0
        self.refresh(layout=True)

    # ── Geometry ──────────────────────────────────────────────────────────────

    def _layout(self) -> tuple[int, int, int]:
        """
        (cell width, how many columns fit, gutter width) — one source of truth
        for both the painting and the hit-testing, so they can never disagree.

        The gutter grows with the row numbers it has to print: a sheet read
        from row 100000 would otherwise shift every column one cell right of
        where a click thinks it is.
        """
        last = self._start + max(0, len(self._rows) - 1)
        gutter = max(self.MIN_GUTTER, len(str(last)) + 1)
        available = max(40, self.size.width or 80)
        width = _fit(available - gutter, len(self._letters))
        shown = max(1, min(len(self._letters), (available - gutter) // (width + 1)))
        return width, shown, gutter

    def _column_at(self, x: int, y: int) -> int:
        """Content coordinates -> sheet column, or 0 for nothing clickable."""
        if self._error or not self._letters or y < self.CAPTION_LINES:
            return 0
        width, shown, gutter = self._layout()
        if x < gutter:
            return 0                            # the row-number column
        index = (x - gutter) // (width + 1)
        return index + 1 if index < shown else 0

    def _hit(self, event: events.MouseEvent) -> int:
        """
        Sheet column under the pointer, or 0.

        Mouse coordinates arrive relative to the widget's *outer* region;
        `get_content_offset` takes the border and padding off, and returns
        None for a pointer that is on them rather than inside the grid.
        """
        offset = event.get_content_offset(self)
        return 0 if offset is None else self._column_at(offset.x, offset.y)

    # ── Mouse ─────────────────────────────────────────────────────────────────

    def on_mouse_move(self, event: events.MouseMove) -> None:
        self.hover_column = self._hit(event)

    def on_leave(self, _event: events.Leave) -> None:
        self.hover_column = 0

    def on_click(self, event: events.Click) -> None:
        column = self._hit(event)
        if not column:
            return
        event.stop()
        # First click of a chain only. Double-clicking a cell is the most
        # natural gesture there is on a grid, and without this the second
        # click would drop the same column into the *next* role — quietly
        # making the source and the destination the same column.
        if event.chain == 1:
            self.post_message(self.ColumnPicked(column))

    def watch_hover_column(self, _old: int, _new: int) -> None:
        self.refresh()

    # ── Render ────────────────────────────────────────────────────────────────

    def get_content_height(self, container, viewport, width: int) -> int:
        if self._error or not self._letters:
            return 1
        return self.CAPTION_LINES + 2 + len(self._rows)

    def render_line(self, y: int) -> Strip:
        return _strip(self._line_text(y), self.size.width, self.rich_style)

    def _cell_style(self, column: int, base: str) -> str:
        if column == self.hover_column:
            return f"{base} on {HOVER_BG}".strip()
        return base

    def _line_text(self, y: int) -> Text:
        if self._error:
            return Text(self._error, style=ERROR) if y == 0 else Text("")
        if not self._letters:
            return Text("")

        width, shown, gutter = self._layout()
        if y == 0:
            return self._caption(shown)

        if y == 1:                              # column letters + role tags
            line = Text(" " * gutter)
            for index in range(shown):
                role = self._roles.get(index + 1)
                tag = self._letters[index] + (f" {role.tag}" if role else "")
                base = f"bold {role.colour}" if role else MUTED
                line.append(_pad(tag, width) + " ",
                            style=self._cell_style(index + 1, base))
            return line

        if y == 2:                              # the sheet's own header row
            line = Text(" " * gutter)
            for index in range(shown):
                role = self._roles.get(index + 1)
                header = self._headers[index] if index < len(self._headers) else ""
                line.append(_pad(header or "—", width) + " ",
                            style=self._cell_style(index + 1, role.colour if role else ""))
            return line

        offset = y - 3
        if offset >= len(self._rows):
            return Text("")
        row = self._rows[offset]
        line = Text(f"{self._start + offset:>{gutter - 1}} ", style=MUTED)
        for index in range(shown):
            role = self._roles.get(index + 1)
            value = row[index] if index < len(row) else ""
            base = role.colour if role else ("" if value else MUTED)
            line.append(_pad(value or "·", width) + " ",
                        style=self._cell_style(index + 1, base))
        return line

    def _caption(self, shown: int) -> Text:
        text = Text()
        text.append(f"preview · sheet has {self._total} rows", style=MUTED)
        hidden = len(self._letters) - shown
        if hidden > 0:
            text.append(f" · {hidden} more columns — type those", style=MUTED)
        if self._prompt is not None:
            text.append("  ·  click a column → ", style=MUTED)
            text.append(self._prompt.label, style=f"bold {self._prompt.colour}")
        return text


def _fit(available: int, columns: int) -> int:
    """Column width that fits `columns` side by side, within sane bounds."""
    if columns <= 0:
        return 14
    return max(8, min(24, available // columns - 1))


def _flatten(text: str) -> str:
    """
    Squash a cell's contents onto one line.

    Every grid in the app measures a cell with `cell_len` and pads it to an
    exact number of cells — but `cell_len` counts a control character as zero
    width, so a spreadsheet cell holding `"Night\\ntom"` measures 8 and is
    padded to 25 *bytes*. The terminal then honours the newline, and the tail
    plus its padding land on the next painted row, on top of the gutter where
    the next row number goes. That is what turns row 2613 into `Br2613`.

    So every character the terminal would act on becomes a space here: newline,
    carriage return, tab, vertical tab, form feed, and the rest of the C0/C1
    range a game script can easily carry. A visible marker would be more
    informative but would also change the cell width, and these are grids whose
    alignment is the whole point; `truncate()` is where `\\n` is shown as `\\n`,
    because there the text is not being padded to a fixed size.
    """
    if not text:
        return ""
    return "".join(
        " " if (ord(char) < 32 or 0x7F <= ord(char) <= 0x9F) else char
        for char in text
    )


def _pad(text: str, width: int) -> str:
    """Pad/trim to an exact cell width — CJK-safe, so columns stay aligned."""
    text = _flatten(text)
    if cell_len(text) > width:
        return set_cell_size(text, max(1, width - 1)) + "…"
    return set_cell_size(text, width)


def _strip(text: Text, width: int, base_style) -> Strip:
    """
    Rich Text -> a Strip padded to the widget width (what Textual expects).

    `base_style` carries the row's background band (hover / cursor / severity
    colour). It is *combined under* each segment's own style rather than used
    only as a fallback: a segment that sets a foreground colour would otherwise
    drop the band's background, which is why a coloured row could not be
    hovered. Combining keeps the band's `bgcolor` while the segment's foreground
    wins on top. A segment with no style of its own just gets the base.
    """
    segments = [
        Segment(
            segment.text,
            (base_style + segment.style) if segment.style else base_style,
            segment.control,
        )
        for segment in text.render(_console())
    ]
    return Strip(segments, text.cell_len).adjust_cell_length(max(0, width), base_style)


def _console():
    from rich.console import Console

    global _CONSOLE
    if _CONSOLE is None:
        _CONSOLE = Console()
    return _CONSOLE


_CONSOLE = None


def _truncate(text: str, width: int) -> str:
    """
    Shorten to `width` for a line that is *not* padded to a fixed size.

    Here a newline can be shown as `\\n` rather than blanked, because nothing is
    being aligned against it — and knowing the cell contained a line break is
    useful in a feed. Other control characters are still removed: they would
    move the cursor.
    """
    text = (text or "").replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")
    text = _flatten(text)
    if width <= 1:
        return ""
    return text if len(text) <= width else text[: width - 1] + "…"


# Painting helpers, named for the widgets in sibling modules that draw their
# own grids. Same functions the widgets above use — one implementation of
# "how wide is a cell", so two grids can never disagree about it.
fit_width = _fit
pad_cells = _pad
strip_line = _strip
truncate = _truncate
flatten_cell = _flatten        # for widgets that build a cell without padding
render_console = _console      # for widgets that need Rich's cell-aware wrap
