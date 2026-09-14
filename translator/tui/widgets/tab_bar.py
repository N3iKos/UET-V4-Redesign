"""
tui/widgets/tab_bar.py — Multi-file top tab navigation bar.

Renders tabs for up to 4 files:
  [ 1. Part01.csv 45% ]  [ 2. Part02.csv 12% ]  [ 3. Part03.csv ✓ ]
Highlights the currently active tab and supports keyboard/mouse switching.
"""

from __future__ import annotations

import os
import re
from typing import Callable
from rich.text import Text
from textual import events
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget

from ..theme import ACCENT, MUTED, OK, WARN, ERROR, BORDER


class FileTabBar(Widget):
    DEFAULT_CSS = """
    FileTabBar {
        height: 1;
        width: 100%;
        background: $background;
        color: $text;
        padding: 0 1;
    }
    """

    active_tab = reactive(0)

    class TabSwitched(Message):
        def __init__(self, index: int) -> None:
            super().__init__()
            self.index = index

    def __init__(
        self,
        tabs: list[str] | None = None,
        on_tab_switch: Callable[[int], None] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._on_tab_switch = on_tab_switch
        self._tabs_data: list[dict[str, str]] = []
        for name in (tabs or []):
            base = os.path.basename(name)
            self._tabs_data.append({
                "name": base,
                "progress": "",
                "status": "running",
            })
        self._click_regions: list[tuple[int, int, int]] = []  # (start_col, end_col, tab_idx)

    @property
    def tabs(self) -> list[str]:
        return [t["name"] for t in self._tabs_data]

    def set_tabs(self, tabs: list[str]) -> None:
        self._tabs_data = [
            {"name": os.path.basename(t), "progress": "", "status": "running"}
            for t in tabs
        ]
        if self.active_tab >= len(self._tabs_data):
            self.active_tab = max(0, len(self._tabs_data) - 1)
        self.refresh()

    def update_tab(
        self,
        index: int,
        label: str | None = None,
        progress_text: str | None = None,
        status: str | None = None,
    ) -> None:
        if 0 <= index < len(self._tabs_data):
            tab = self._tabs_data[index]
            if label is not None:
                tab["name"] = os.path.basename(label)
            if progress_text is not None:
                tab["progress"] = progress_text
            if status is not None:
                tab["status"] = status
            self.refresh()

    def get_tab_label(self, index: int) -> str:
        if 0 <= index < len(self._tabs_data):
            t = self._tabs_data[index]
            parts = [f"{index + 1}. {t['name']}"]
            if t["progress"]:
                parts.append(t["progress"])
            if t["status"] == "done":
                parts.append("✓")
            elif t["status"] == "failed":
                parts.append("✗")
            elif t["status"] == "paused":
                parts.append("⏸")
            return " ".join(parts)
        return ""

    def switch_tab(self, index: int) -> None:
        if 0 <= index < len(self._tabs_data) and index != self.active_tab:
            self.active_tab = index
            if self._on_tab_switch is not None:
                self._on_tab_switch(index)
            self.post_message(self.TabSwitched(index))
            self.refresh()

    def next_tab(self) -> None:
        if self._tabs_data:
            self.switch_tab((self.active_tab + 1) % len(self._tabs_data))

    def prev_tab(self) -> None:
        if self._tabs_data:
            self.switch_tab((self.active_tab - 1) % len(self._tabs_data))

    @staticmethod
    def _ellipsize(text: str, width: int) -> str:
        if Text(text).cell_len <= width:
            return text
        if width <= 1:
            return "…"[:width]
        result = ""
        for character in text:
            if Text(result + character + "…").cell_len > width:
                break
            result += character
        return result + "…"

    @staticmethod
    def _middle_ellipsize(text: str, width: int) -> str:
        if Text(text).cell_len <= width:
            return text
        if width <= 1:
            return "…"[:width]
        ellipsis = "..." if width >= 10 else "…"
        el_len = Text(ellipsis).cell_len
        avail = width - el_len
        if avail <= 0:
            return ellipsis[:width]
        head_len = avail // 2 + (avail % 2)
        tail_len = avail // 2
        head = ""
        for ch in text:
            if Text(head + ch).cell_len > head_len:
                break
            head += ch
        tail = ""
        for ch in reversed(text):
            if Text(ch + tail).cell_len > tail_len:
                break
            tail = ch + tail
        return head + ellipsis + tail

    def render_for_width(self, width: int) -> Text:
        out = Text()
        self._click_regions.clear()
        count = len(self._tabs_data)
        if not count:
            return out
        available = max(count * 2, width)
        base_width, remainder = divmod(available, count)
        col = 0
        for i in range(count):
            tab_width = base_width + (1 if i < remainder else 0)
            is_active = i == self.active_tab
            inner = max(2, tab_width - 2)
            prefix = f"{i + 1}."

            t = self._tabs_data[i]
            status_parts = []
            if t["progress"]:
                status_parts.append(t["progress"])
            if t["status"] == "done":
                status_parts.append("✓")
            elif t["status"] == "failed":
                status_parts.append("✗")
            elif t["status"] == "paused":
                status_parts.append("⏸")
            status_str = (" " + " ".join(status_parts)) if status_parts else ""

            prefix_len = Text(prefix).cell_len
            status_len = Text(status_str).cell_len

            clean_name = re.sub(r"\.[0-9a-fA-F]+\.unique", "", t["name"])
            available_for_name = inner - prefix_len - status_len
            if available_for_name >= 4:
                short_name = self._middle_ellipsize(clean_name, available_for_name)
                label = f"{prefix}{short_name}{status_str}"
            elif available_for_name > 0:
                short_name = self._ellipsize(clean_name, available_for_name)
                label = f"{prefix}{short_name}{status_str}"
            else:
                avail_status = max(0, inner - prefix_len)
                label = prefix + self._ellipsize(status_str.strip(), avail_status)

            decorated = tab_width >= 4
            tab_text = f"[{label}]" if is_active and decorated else label
            if not is_active and decorated:
                tab_text = f" {label} "
            padding = max(0, tab_width - Text(tab_text).cell_len)
            tab_text += " " * padding
            start_col = col
            out.append(tab_text, style=f"bold {ACCENT} reverse" if is_active else MUTED)
            col += Text(tab_text).cell_len
            self._click_regions.append((start_col, col, i))
        return out

    def render(self) -> Text:
        return self.render_for_width(max(1, self.size.width or 80))

    def on_click(self, event: events.Click) -> None:
        click_x = event.x
        for start, end, idx in self._click_regions:
            if start <= click_x < end:
                self.switch_tab(idx)
                event.stop()
                break
