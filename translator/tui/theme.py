"""
tui/theme.py — Colour roles and shared CSS.

v3.6 sprinkled raw ANSI escapes through cli.py/ui.py. v4 keeps the same
colour *roles* (info/ok/warn/error/proc) but defines them once, here, as
theme constants the TUI layer uses (04_TUI_SPEC.md §3).
"""

from __future__ import annotations

# Colour roles — referenced from CSS below and from Rich markup in widgets.
ACCENT = "#38bdf8"      # current selection, focus, step number
OK = "#4ade80"          # success, valid, finished
WARN = "#fbbf24"        # cooldown, retry, skipped
ERROR = "#f87171"       # invalid, failed, raw provider errors
MUTED = "#7c8494"       # hints, secondary data
TEXT = "#e6e6e6"
PANEL = "#141821"
BORDER = "#2b323f"

# Pointer feedback. The keyboard has `>` and bold text to mark the cursor; a
# mouse needs a full-width band it can aim at, and a second, quieter band that
# follows the pointer so you can see what a click would hit before clicking.
SELECT_BG = "#22304a"       # cursor row, picker focused
SELECT_DIM_BG = "#181d28"   # cursor row, picker not focused
HOVER_BG = "#151b26"        # row under the pointer
BORDER_HOVER = "#3f4a5c"    # input border under the pointer

# Markdown and maths, in the chat transcript. A reply is prose first, so
# these stay quiet: enough to tell code from a quote from a formula, not
# enough to turn a paragraph into a colour chart.
MD_CODE = "#e0af68"         # inline code and fenced blocks
MD_CODE_BG = "#1a2029"      # the tint behind inline code only
MD_MATH = "#c8a2e0"         # anything that came out of $…$
MD_LINK = "#7dcfff"
MD_RULE = "#2b323f"         # horizontal rules, table lines, the quote bar

# Spinner frames — braille, same family v3.6 used.
SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

APP_CSS = f"""
Screen {{
    background: #0b0e14;
    color: {TEXT};
}}

#header {{
    dock: top;
    height: 1;
    background: {PANEL};
    color: {MUTED};
    padding: 0 1;
}}

/* The docked strip along the bottom. The footer and anything a screen pins
   above it live in here, because same-edge docked siblings overlap. */
#bottom {{
    dock: bottom;
    height: auto;
}}

#footer {{
    height: 1;
    background: {PANEL};
    color: {MUTED};
    padding: 0 1;
}}

#body {{
    padding: 1 2;
    scrollbar-size-vertical: 1;
}}

/* Form rows keep their natural height; the body scrolls instead of
   compressing them when the window is short. */
LabeledInput {{
    height: auto;
    margin-bottom: 1;
}}

Input {{
    height: 3;
    min-height: 3;
}}

.title {{
    color: {ACCENT};
    text-style: bold;
    margin-bottom: 1;
}}

.hint {{
    color: {MUTED};
}}

.ok {{ color: {OK}; }}
.warn {{ color: {WARN}; }}
.error {{ color: {ERROR}; }}
.muted {{ color: {MUTED}; }}
.accent {{ color: {ACCENT}; }}

Picker {{
    height: auto;
    max-height: 100%;
}}

/* Must grow to its full content height, otherwise the scroll container
   has nothing to scroll and the feed silently clips at one screenful. */
TranslationFeed {{
    height: auto;
    width: 1fr;
}}

ActivityPanel {{
    height: auto;
    width: 1fr;
}}

ActivityHeader {{
    height: 1;
    width: 1fr;
}}

EventsPanel {{
    height: auto;
    width: 1fr;
}}

EventsHeader {{
    height: 1;
    width: 1fr;
}}

ColumnPreview {{
    height: auto;
    width: 1fr;
}}

CompareGrid {{
    height: auto;
    width: 1fr;
}}

#verdict {{
    margin-top: 1;
}}

/* Footer / modal key hints — every chip is a click target. */
KeyHints {{
    height: 1;
    color: {MUTED};
}}

/* A form's confirm, pinned above the footer so scrolling cannot hide it. */
#confirm-bar {{
    height: 2;
    border-top: solid {BORDER};
}}

#lang-row {{
    height: auto;
    width: 1fr;
}}

#lang-row > Vertical {{
    width: 1fr;
    height: auto;
    margin-right: 1;
}}

#lang-row > Vertical:last-child {{
    margin-right: 0;
}}

Input {{
    background: {PANEL};
    border: tall {BORDER};
    padding: 0 1;
}}

/* Listed before :focus so a focused-and-hovered input keeps the accent. */
Input:hover {{
    border: tall {BORDER_HOVER};
}}

Input:focus {{
    border: tall {ACCENT};
}}

/* The multi-line box has to read as the same kind of field as an Input, so
   it borrows the same chrome rather than Textual's default editor look. */
PasteArea {{
    height: 8;
    background: {PANEL};
    border: tall {BORDER};
    padding: 0 1;
    scrollbar-size-vertical: 1;
}}

PasteArea:hover {{
    border: tall {BORDER_HOVER};
}}

PasteArea:focus {{
    border: tall {ACCENT};
}}

LabeledTextArea {{
    height: auto;
    margin-bottom: 1;
}}

DataTable {{
    background: transparent;
}}

DataTable > .datatable--cursor {{
    background: {BORDER};
    color: {ACCENT};
    text-style: bold;
}}

.panel {{
    border: round {BORDER};
    padding: 0 1;
}}

.panel-title {{
    color: {MUTED};
}}

#error-box {{
    border: round {ERROR};
    color: {ERROR};
    padding: 0 1;
    margin: 1 0;
}}

ModalScreen {{
    align: center middle;
}}

.modal {{
    border: round {ACCENT};
    background: {PANEL};
    padding: 1 2;
    width: 70;
    height: auto;
    max-height: 95%;
}}
"""
