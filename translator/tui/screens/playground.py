"""
tui/screens/playground.py — Try models out, side by side, before trusting one
with a spreadsheet.

  PlaygroundMenuScreen  the section menu
  ModelSlotScreen       pick models — up to three to race, or one to judge
  CompareSetupScreen    what to send them: lines, languages, style
  CompareRunScreen      the grid, and the judge
  ChatScreen            an ordinary chat, streamed, with the numbers shown

The comparison sends the app's real translation prompt, so what a model does
here is what it would do on a real run. The chat is the one place in the app
that streams; everything about the translation pipeline stays batch-shaped.

It is also the only place a model's **reasoning is visible**. Elsewhere —
Activity Panel, Judge — reasoning is a phase and never content, on purpose. But
"is this model actually thinking, and did my on/off setting reach the server?"
is a question a spinner cannot answer, and getting it wrong is easy: a
`parameter_path` typo produces a request the server accepts and silently
ignores. So both screens show what was *asked* (the exact payload fragment)
next to what the model *did* (its thinking, and reasoning tokens when the
server itemises them), and both let reasoning be forced on or off for a run so
the two states can be compared directly.
"""

from __future__ import annotations

import threading
import time

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Label, Static

from ...data.presets import PRESETS, load_user_presets
from ...playground import judging, prefs
from ...playground.inputs import parse_input
from ...playground.runner import MAX_SLOTS, Slot, SlotResult, letter_for, run_compare
from ..app import BaseScreen, MenuScreen
from ..theme import ACCENT, ERROR, MUTED, OK, WARN
from ..widgets.chat import MODEL, NOTICE, YOU, ChatFeed
from ..widgets.common import (
    ErrorBox,
    KeyHints,
    LabeledInput,
    LabeledTextArea,
    Option,
    PasteArea,
    Picker,
    Spinner,
    escape_markup,
    truncate,
)
from ..widgets.compare import Column, CompareGrid


def _duration(ms: float) -> str:
    """Sub-second calls are the interesting ones here — `0.0s` says nothing."""
    return f"{ms:.0f}ms" if ms < 1000 else f"{ms / 1000:.1f}s"


# What a reasoning override means, in the words the screens use for it. `None`
# is not "off": it means the model's own configured default decides.
REASONING_CHOICES = [
    (None, "reasoning: model default",
     "whatever each model's own configuration says"),
    (True, "reasoning: force on",
     "send the on value to every model that can be told"),
    (False, "reasoning: force off",
     "send the off value — run it twice to prove the toggle works"),
]


def _reasoning_label(override: bool | None) -> str:
    for value, label, _hint in REASONING_CHOICES:
        if value is override or label == override:
            return label
    return "reasoning: model default"


# ── Section menu ──────────────────────────────────────────────────────────────

class PlaygroundMenuScreen(MenuScreen):
    breadcrumb = "Playground"
    title_text = "Playground"

    def build_options(self) -> list[Option]:
        return [
            Option("Compare models", "compare",
                   hint="run the same lines through up to 3 models, then have one grade them"),
            Option("Chat", "chat", hint="one model, streamed, with timings"),
        ]

    def compose_extra(self) -> ComposeResult:
        yield Label("")
        yield Label(
            "Nothing here writes a spreadsheet or changes a saved provider.",
            classes="hint",
        )

    def screen_keys(self) -> list[tuple[str, str]]:
        return [("↑↓", "move"), ("⏎", "select"), ("←", "back"), ("esc", "cancel")]

    def handle_choice(self, option: Option) -> None:
        if option.value == "compare":
            self.app.push_screen(ModelSlotScreen(MAX_SLOTS, self._compare_picked))
        else:
            self.app.push_screen(ModelSlotScreen(1, self._chat_picked))

    def _compare_picked(self, slots: list[Slot] | None) -> None:
        if slots:
            self.app.push_screen(CompareSetupScreen(slots))

    def _chat_picked(self, slots: list[Slot] | None) -> None:
        if slots:
            self.app.push_screen(ChatScreen(slots[0]))


# ── Model picker ──────────────────────────────────────────────────────────────

class ModelSlotScreen(BaseScreen):
    """
    Provider · Connection · Model, and a list of what you have taken.

    Picking one model is the common case and finishes immediately. Picking
    several needs a list, because the three may live on different providers
    and the columns can only show one provider at a time.
    """

    BINDINGS = [
        Binding("right", "focus_next_col", "next column", show=False),
        Binding("x", "drop", "remove", show=False),
    ]

    def __init__(self, max_slots: int, on_done, remembered: list[dict] | None = None,
                 **kwargs) -> None:
        super().__init__(**kwargs)
        self.max_slots = max_slots
        self._on_done = on_done
        self._remembered = remembered
        self.breadcrumb = (
            "Playground › Judge model" if max_slots == 1 else "Playground › Models"
        )
        self.chosen: list[Slot] = []

    def compose_body(self) -> ComposeResult:
        yield Label(
            "Judge model" if self.max_slots == 1 else f"Pick up to {self.max_slots} models",
            classes="title",
        )
        self.providers = Picker(click_activates=False)
        self.connections = Picker(click_activates=False)
        self.models = Picker()
        with Horizontal():
            with Vertical():
                yield Label("PROVIDER", classes="muted")
                yield self.providers
            with Vertical():
                yield Label("CONNECTION", classes="muted")
                yield self.connections
            with Vertical():
                yield Label("MODEL", classes="muted")
                yield self.models
        yield Label("")
        yield Static("", id="slot-summary", classes="hint")

    def compose_above_footer(self) -> ComposeResult:
        if self.max_slots > 1:
            self._confirm = Picker(
                [Option("Run these models", "go", style=OK)], id="confirm-bar"
            )
            yield self._confirm

    def screen_keys(self) -> list[tuple[str, str]]:
        if self.max_slots == 1:
            return [("↑↓", "move"), ("←→", "column"), ("⏎", "use this model"),
                    ("←", "back"), ("esc", "cancel")]
        return [("↑↓", "move"), ("←→", "column"), ("⏎", "add"), ("x", "remove"),
                ("←", "back"), ("esc", "cancel")]

    def on_screen_ready(self) -> None:
        self.providers.set_options([
            Option(p.name, p.id, hint=f"{len(p.connections)} conn")
            for p in self.app.registry.providers
        ])
        self._load_connections()
        self._restore()
        self._focus_first_choice()
        self._update_summary()

    def _restore(self) -> None:
        """Re-offer last time's models, so a repeat run is just Enter."""
        for slot in self._remembered or []:
            resolved = self._resolve(slot)
            if resolved is not None and len(self.chosen) < self.max_slots:
                self.chosen.append(resolved)

    def _resolve(self, pref: dict) -> Slot | None:
        try:
            provider = self.app.registry.get_provider(pref["provider_id"])
            connection = provider.find_connection(pref["connection_id"])
            if connection is None or connection.find_model(pref["model_id"]) is None:
                return None
        except Exception:
            return None
        return Slot(provider.id, connection.id, pref["model_id"],
                    provider.name, connection.name)

    # ── Cascade ───────────────────────────────────────────────────────────────

    def _provider(self):
        option = self.providers.current
        return self.app.registry.get_provider(option.value) if option else None

    def _load_connections(self) -> None:
        provider = self._provider()
        if provider is None:
            return
        self.connections.set_options([
            Option(c.name, c.id, hint=f"{len(c.models)} models")
            for c in provider.connections
        ])
        self._load_models()

    def _load_models(self) -> None:
        provider = self._provider()
        option = self.connections.current
        if provider is None or option is None:
            self.models.set_options([])
            return
        connection = provider.find_connection(option.value)
        models = connection.models if connection else []
        self.models.set_options([
            Option(m.label, m.id, hint=m.preset.label()) for m in models
        ])

    def _focus_first_choice(self) -> None:
        for picker in (self.providers, self.connections, self.models):
            if len(picker.options) > 1:
                picker.focus()
                return
        self.models.focus()

    def on_picker_highlighted(self, event: Picker.Highlighted) -> None:
        if event.picker is self.providers:
            self._load_connections()
        elif event.picker is self.connections:
            self._load_models()

    def action_focus_next_col(self) -> None:
        order = [self.providers, self.connections, self.models]
        for index, picker in enumerate(order):
            if picker.has_focus and index + 1 < len(order):
                order[index + 1].focus()
                return

    def action_back(self) -> None:
        order = [self.providers, self.connections, self.models]
        for index, picker in enumerate(order):
            if picker.has_focus and index > 0:
                order[index - 1].focus()
                return
        super().action_back()

    # ── Choosing ──────────────────────────────────────────────────────────────

    def on_picker_selected(self, event: Picker.Selected) -> None:
        if getattr(self, "_confirm", None) is not None and event.picker is self._confirm:
            self._finish()
            return
        if event.picker is not self.models:
            return

        provider = self._provider()
        option = self.connections.current
        if provider is None or option is None or self.models.current is None:
            return
        connection = provider.find_connection(option.value)
        if connection is None:
            return
        slot = Slot(provider.id, connection.id, self.models.current.value,
                    provider.name, connection.name)

        if self.max_slots == 1:
            self.chosen = [slot]
            self._finish()
            return

        if any(s.model_id == slot.model_id and s.connection_id == slot.connection_id
               for s in self.chosen):
            self.notify_status("That one is already in the list.", MUTED)
            return
        if len(self.chosen) >= self.max_slots:
            self.notify_status(
                f"That is {self.max_slots} already — press x to drop one first.", WARN
            )
            return
        self.chosen.append(slot)
        self._update_summary()

    def action_drop(self) -> None:
        if self.max_slots == 1 or not self.chosen:
            return
        dropped = self.chosen.pop()
        self._update_summary()
        self.notify_status(f"Removed {dropped.model_id}.", MUTED)

    def _update_summary(self) -> None:
        panel = self.query_one("#slot-summary", Static)
        if self.max_slots == 1:
            panel.update("Enter on a model uses it.")
            return
        if not self.chosen:
            panel.update("Nothing picked yet — Enter on a model adds it.")
            return
        lines = [
            f"{letter_for(index)}  {escape_markup(slot.model_id)}   "
            f"{escape_markup(slot.where)}"
            for index, slot in enumerate(self.chosen)
        ]
        panel.update("\n".join(lines))

    def _finish(self) -> None:
        if not self.chosen:
            self.notify_status("Pick at least one model.", WARN)
            return
        for index, slot in enumerate(self.chosen):
            slot.letter = letter_for(index)
        self.app.pop_screen()
        self._on_done(list(self.chosen))


# ── What to send ──────────────────────────────────────────────────────────────

class CompareSetupScreen(BaseScreen):
    breadcrumb = "Playground › Input"

    def __init__(self, slots: list[Slot], **kwargs) -> None:
        super().__init__(**kwargs)
        self.slots = slots

    def compose_body(self) -> ComposeResult:
        yield Label("What should they translate?", classes="title")
        yield Label(
            "  ·  ".join(f"{s.letter} {s.model_id}" for s in self.slots),
            classes="hint",
        )
        yield Label("")

        saved = prefs.load_prefs()
        self._text = LabeledTextArea(
            "Lines",
            placeholder="One line per row…",
            # Escaped: the hint renders through Rich markup, and the literal
            # prefixes it is describing would otherwise be read as tags and
            # vanish from the one sentence that explains them.
            hint=escape_markup(
                "Shift+Enter or Ctrl+Enter for a new line (Ctrl+J always works) · "
                "Enter moves on. With or without [1] [2] prefixes — "
                "whichever you use is detected."
            ),
        )
        self._src = LabeledInput("Source language", default="auto",
                                 value=saved.get("src_lang") or "")
        self._tgt = LabeledInput("Target language", default="Indonesian",
                                 value=saved.get("tgt_lang") or "")
        yield self._text
        yield Static("", id="input-note", classes="hint")
        yield Label("")
        yield self._src
        yield self._tgt
        yield Label("")
        yield Label("Style", classes="muted")
        self._style = Picker(click_activates=False)
        yield self._style
        yield Label("")
        yield Label("Reasoning", classes="muted")
        # Forcing it either way is what makes a reasoning setup testable: run
        # the same lines on then off, and the difference — or its absence — is
        # the answer. `Force off` on a model whose control is `always_on` or
        # `effort` changes nothing, and the results say so rather than pretending.
        self._reasoning = Picker([
            Option(label, value, hint=hint) for value, label, hint in REASONING_CHOICES
        ], click_activates=False)
        yield self._reasoning
        yield Label("", id="setup-error", classes="error")

    def compose_above_footer(self) -> ComposeResult:
        self._confirm = Picker(
            [Option("Run the comparison", "go", style=OK)], id="confirm-bar"
        )
        yield self._confirm

    def screen_keys(self) -> list[tuple[str, str]]:
        if getattr(self, "_text", None) is not None and self._text.area.has_focus:
            return [("⇧⏎", "new line"), ("⏎", "next field"),
                    ("tab", "next field"), ("esc", "cancel")]
        return [("tab", "next field"), ("⏎", "run"), ("←", "back"), ("esc", "cancel")]

    def on_descendant_focus(self, event) -> None:
        # The lines box and the plain fields disagree about what Enter does,
        # so the footer has to say which one is in front right now.
        self.refresh_keys()

    def on_screen_ready(self) -> None:
        self._styles = list(PRESETS) + load_user_presets()
        saved = prefs.load_prefs()
        self._style.set_options(
            [Option(p["label"], p["key"]) for p in self._styles]
        )
        for index, preset in enumerate(self._styles):
            if preset["key"] == saved.get("style_key"):
                self._style.index = index
                break
        self._text.focus_input()
        self._show_note()

    def on_text_area_changed(self, event) -> None:
        self._show_note()

    def _show_note(self) -> None:
        parsed = parse_input(self._text.value)
        note = self.query_one("#input-note", Static)
        # describe() names the `[N]` format literally — escape or Rich eats it.
        note.update(escape_markup(parsed.describe()) if parsed else "nothing to send yet")

    def on_paste_area_submitted(self, event: PasteArea.Submitted) -> None:
        """Enter in the lines box moves on; Shift+Enter is the new line."""
        self._src.focus_input()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input is self._src.input:
            self._tgt.focus_input()
            return
        self._run()

    def on_picker_selected(self, event: Picker.Selected) -> None:
        self._run()

    def _run(self) -> None:
        error = self.query_one("#setup-error", Label)
        parsed = parse_input(self._text.value)
        if not parsed:
            error.update("Paste at least one line to translate.")
            self._text.focus_input()
            return
        if not self._tgt.value.strip():
            error.update("Target language is required.")
            self._tgt.focus_input()
            return

        option = self._style.current
        preset = next((p for p in self._styles if p["key"] == (option.value if option else "")),
                      None)
        style_prompt = (preset or {}).get("prompt") or ""

        prefs.remember_job(self._src.value, self._tgt.value,
                           option.value if option else "")
        prefs.remember_compare([s.to_pref() for s in self.slots])

        reasoning_option = self._reasoning.current
        override = reasoning_option.value if reasoning_option else None

        self.app.push_screen(CompareRunScreen(
            self.slots, parsed.rows, self._src.value, self._tgt.value, style_prompt,
            reasoning_override=override,
        ))


# ── The race, and the judge ───────────────────────────────────────────────────

class CompareRunScreen(BaseScreen):
    breadcrumb = "Playground › Comparison"

    BINDINGS = [
        Binding("j", "judge", "judge", show=False),
        Binding("J", "judge_pick", "judge with…", show=False),
        Binding("r", "rerun", "run again", show=False),
        Binding("t", "toggle_reasoning", "reasoning", show=False),
        Binding("T", "flip_reasoning", "flip reasoning", show=False),
    ]

    def __init__(self, slots, rows, src_lang, tgt_lang, style_prompt,
                 reasoning_override: bool | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.slots = slots
        self.rows = rows
        self.src_lang = src_lang
        self.tgt_lang = tgt_lang
        self.style_prompt = style_prompt
        self.reasoning_override = reasoning_override
        self.results: list[SlotResult] = []
        self._stop = threading.Event()
        # Not `_running`: `MessagePump` owns that name and sets it True on mount,
        # so a screen keeping its own flag there reads as permanently busy.
        self._busy_batch = False
        self._judging = False
        self._show_reasoning = False

    def compose_body(self) -> ComposeResult:
        yield Label(
            f"{len(self.rows)} line(s) · {self.src_lang} → {self.tgt_lang}",
            classes="hint",
        )
        self._mode = Static("", id="compare-mode", classes="hint")
        yield self._mode
        self.grid = CompareGrid(id="compare-grid")
        yield self.grid
        yield Label("")
        self._spinner = Spinner("Judging…")
        self._spinner.display = False
        yield self._spinner
        # What each model was asked to do about thinking, and what it did.
        # Folded away by default: it matters when you are debugging a reasoning
        # setup and is noise the rest of the time.
        self._reasoning_panel = Static("", id="reasoning-panel", classes="panel")
        self._reasoning_panel.display = False
        yield self._reasoning_panel
        self._verdict = Static("", id="verdict", classes="panel")
        self._verdict.display = False
        yield self._verdict
        self._error = ErrorBox()
        yield self._error

    def screen_keys(self) -> list[tuple[str, str]]:
        if self._busy_batch:
            return [("↑↓", "scroll"), ("^C", "quit")]
        keys = [("↑↓", "scroll"), ("j", "judge"), ("J", "judge with…"),
                ("r", "run again")]
        if any(result.reasoning_control != "none" for result in self.results):
            keys.append(("t", "hide thinking" if self._show_reasoning
                         else "show thinking"))
            keys.append(("T", "flip reasoning & rerun"))
        return keys + [("←", "back"), ("esc", "cancel")]

    def on_screen_ready(self) -> None:
        self._app = self.app
        self.app.on_shutdown_hook = self._stop.set
        self.grid.set_sources(self.rows, [
            Column(letter=s.letter, label=s.model_id, where=s.where) for s in self.slots
        ])
        self.set_interval(0.1, self.grid.tick)
        self._paint_mode()
        self._start()

    def _paint_mode(self) -> None:
        self._mode.update(escape_markup(_reasoning_label(self.reasoning_override)))

    def _start(self) -> None:
        self._stop = threading.Event()
        self._busy_batch = True
        self.results = []
        self._verdict.display = False
        self._reasoning_panel.display = False
        self._error.clear()
        self.grid.set_sources(self.rows, [
            Column(letter=s.letter, label=s.model_id, where=s.where) for s in self.slots
        ])
        self._paint_mode()
        self.refresh_keys()
        self.notify_status("Running…", ACCENT)
        self._race()

    @work(thread=True)
    def _race(self) -> None:
        app = self._app
        try:
            run_compare(
                app.registry, self.slots, self.rows,
                self.src_lang, self.tgt_lang, self.style_prompt,
                on_result=lambda result: app.call_from_thread(self._landed, result),
                stop_event=self._stop,
                reasoning_override=self.reasoning_override,
            )
        except Exception as exc:      # never let a crash eat the screen
            app.call_from_thread(self._crashed, f"{type(exc).__name__}: {exc}")
            return
        app.call_from_thread(self._race_done)

    def _landed(self, result: SlotResult) -> None:
        self.results.append(result)
        self.grid.update_column(result.slot.letter, self._column_for(result))
        self._paint_reasoning()

    def _column_for(self, result: SlotResult) -> Column:
        slot = result.slot
        if not result.ok:
            return Column(letter=slot.letter, label=slot.model_id, where=slot.where,
                          state="done", error=result.error, stat="failed")
        bits = [_duration(result.latency_ms)]
        if result.completion_tokens:
            bits.append(f"{result.completion_tokens} tok")
            if result.tokens_per_second:
                bits.append(f"{result.tokens_per_second:.0f}/s")
        if result.reasoning_control != "none":
            # The verdict, not just the setting: "on, no trace" is the case
            # worth seeing, and it only shows up by comparing the two.
            verdict = result.reasoning_verdict
            if result.reasoning_tokens:
                verdict += f" {result.reasoning_tokens}t"
            bits.append(verdict)
        if result.missing:
            bits.append(f"{len(result.missing)} missing")
        if result.positional:
            bits.append("no [N]")
        return Column(letter=slot.letter, label=slot.model_id, where=slot.where,
                      state="done", rows=result.rows, stat=" · ".join(bits))

    def _race_done(self) -> None:
        self._busy_batch = False
        self.refresh_keys()
        failed = [r for r in self.results if not r.ok]
        if failed:
            self._error.show(
                "\n\n".join(f"{r.slot.letter}  {r.slot.model_id}\n{r.error}" for r in failed),
                "provider response",
            )
        ok = len(self.results) - len(failed)
        self._paint_reasoning()
        message = (f"{ok} of {len(self.results)} answered — j to judge, "
                   f"r to run again.")
        # Surface a reasoning mismatch here rather than leaving it to be noticed:
        # a request that was accepted and then ignored is the failure mode this
        # screen exists to catch.
        silent = [
            r for r in self.results
            if r.ok and r.reasoning_requested and not r.thought
        ]
        if silent:
            message = (
                f"{ok} of {len(self.results)} answered · reasoning was requested "
                f"but left no trace on {', '.join(r.slot.letter for r in silent)} "
                f"— t for detail."
            )
        self.notify_status(message, OK if ok and not silent else
                           WARN if ok else ERROR)

    def _paint_reasoning(self) -> None:
        """
        The panel that answers "did reasoning actually happen?".

        Per model: what the request carried, what came back, and the model's own
        thinking when it returned any. Withheld unless asked for — it is a
        debugging view, not part of the comparison.
        """
        if not self._show_reasoning:
            self._reasoning_panel.display = False
            return
        relevant = [r for r in self.results if r.reasoning_control != "none"]
        if not relevant:
            self._reasoning_panel.update(
                f"[{MUTED}]None of these models declares reasoning support, so "
                f"no reasoning parameter was sent. Configure it per model under "
                f"Providers & Models › r.[/]"
            )
            self._reasoning_panel.display = True
            return

        lines = [f"[bold {ACCENT}]Reasoning[/]  "
                 f"[{MUTED}]{escape_markup(_reasoning_label(self.reasoning_override))}[/]",
                 ""]
        for result in sorted(relevant, key=lambda r: r.slot.letter):
            colour = OK if result.thought else (
                ERROR if result.reasoning_requested else MUTED
            )
            lines.append(
                f"[{colour}]{result.slot.letter}[/]  "
                f"{escape_markup(result.slot.model_id)}   "
                f"[{colour}]{escape_markup(result.reasoning_verdict)}[/]"
            )
            sent = result.reasoning_detail or "(no parameter sent)"
            lines.append(f"     [{MUTED}]sent[/]     {escape_markup(sent)}")
            if result.reasoning_tokens:
                lines.append(
                    f"     [{MUTED}]tokens[/]   {result.reasoning_tokens:,} "
                    f"reasoning tokens reported"
                )
            if result.reasoning.strip():
                # Trimmed: a chain of thought can be longer than the answer, and
                # this is a panel rather than a transcript. The chat screen shows
                # the whole thing.
                excerpt = truncate(result.reasoning.strip().replace("\n", " ⏎ "), 300)
                lines.append(f"     [{MUTED}]thought[/]  {escape_markup(excerpt)}")
            elif result.reasoning_requested:
                lines.append(
                    f"     [{MUTED}]thought[/]  "
                    f"[{ERROR}]nothing came back — the parameter may be wrong for "
                    f"this model, or the server hides reasoning[/]"
                )
            lines.append("")
        lines.append(
            f"[{MUTED}]T runs the same lines with the opposite setting, which is "
            f"the quickest way to prove a toggle works.[/]"
        )
        self._reasoning_panel.update("\n".join(lines))
        self._reasoning_panel.display = True

    def action_toggle_reasoning(self) -> None:
        if self._busy_batch:
            return
        self._show_reasoning = not self._show_reasoning
        self._paint_reasoning()
        self.refresh_keys()

    def action_flip_reasoning(self) -> None:
        """
        Rerun with the opposite reasoning setting.

        `model default` flips to `force on`, and after that it alternates. Two
        runs is all it takes to see whether a toggle reaches the server.
        """
        if self._busy_batch or self._judging:
            return
        self.reasoning_override = True if self.reasoning_override is None \
            else not self.reasoning_override
        self._show_reasoning = True
        self.notify_status(
            f"Rerunning with {_reasoning_label(self.reasoning_override)}.", ACCENT
        )
        self._start()

    def _crashed(self, message: str) -> None:
        self._busy_batch = False
        self.refresh_keys()
        self._error.show(message, "comparison failed")

    def action_rerun(self) -> None:
        if not self._busy_batch and not self._judging:
            self._start()

    # ── Judging ───────────────────────────────────────────────────────────────

    def action_judge(self) -> None:
        """Reuse the judge model that was chosen last time, if there is one."""
        if not self._ready_to_judge():
            return
        remembered = prefs.load_prefs().get("judge")
        slot = self._resolve_judge(remembered) if remembered else None
        if slot is None:
            self.action_judge_pick()
            return
        self._begin_judging(slot)

    def action_judge_pick(self) -> None:
        if not self._ready_to_judge():
            return
        remembered = prefs.load_prefs().get("judge")
        self.app.push_screen(
            ModelSlotScreen(1, self._judge_picked,
                            remembered=[remembered] if remembered else None)
        )

    def _judge_picked(self, slots: list[Slot] | None) -> None:
        if not slots:
            return
        prefs.remember_judge(slots[0].to_pref())
        self._begin_judging(slots[0])

    def _ready_to_judge(self) -> bool:
        if self._busy_batch or self._judging:
            return False
        if not any(r.ok for r in self.results):
            self.notify_status("Nothing to judge — no model answered.", WARN)
            return False
        return True

    def _resolve_judge(self, pref: dict) -> Slot | None:
        try:
            provider = self.app.registry.get_provider(pref["provider_id"])
            connection = provider.find_connection(pref["connection_id"])
            if connection is None or connection.find_model(pref["model_id"]) is None:
                return None
        except Exception:
            return None
        return Slot(provider.id, connection.id, pref["model_id"],
                    provider.name, connection.name)

    def _begin_judging(self, slot: Slot) -> None:
        self._judging = True
        self._error.clear()
        self._spinner.set_label(f"Judging with {slot.model_id}…")
        self._spinner.display = True
        self.refresh_keys()
        ordered = sorted(self.results, key=lambda r: r.slot.letter)
        prompt = judging.build_judge_prompt(
            self.rows, ordered, self.src_lang, self.tgt_lang, self.style_prompt
        )
        self._judge_worker(slot, prompt)

    @work(thread=True)
    def _judge_worker(self, slot: Slot, prompt: str) -> None:
        app = self._app
        verdict = judging.run_judge(app.registry, slot, prompt)
        app.call_from_thread(self._judged, slot, verdict)

    def _judged(self, slot: Slot, verdict: judging.JudgeVerdict) -> None:
        self._judging = False
        self._spinner.display = False
        self.refresh_keys()

        if not verdict.ok:
            self._error.show(verdict.error, f"judge {slot.model_id} failed")
            self.notify_status("The judge could not be reached.", ERROR)
            return

        if verdict.unparsed:
            self._verdict.update(
                f"[{WARN}]The judge answered in prose rather than JSON.[/]\n\n"
                + escape_markup(verdict.raw[:1500])
            )
            self._verdict.display = True
            self.notify_status("Judged, but the reply could not be scored.", WARN)
            return

        for result in self.results:
            score = verdict.score_for(result.slot.letter)
            column = self._column_for(result)
            if score is not None:
                column.score = f"{score.score:g}/10"
                column.winner = result.slot.letter == verdict.winner
            self.grid.update_column(result.slot.letter, column)
        self.grid.set_notes({note.row: note.note for note in verdict.notes})

        lines = [f"[bold {ACCENT}]Judged by {escape_markup(slot.model_id)}[/]", ""]
        for score in verdict.scores:
            result = next((r for r in self.results if r.slot.letter == score.letter), None)
            name = escape_markup(result.slot.model_id) if result else score.letter
            mark = " ★" if score.letter == verdict.winner else ""
            colour = OK if score.letter == verdict.winner else MUTED
            lines.append(
                f"[{colour}]{score.letter}{mark}[/]  {name}   "
                f"[bold]{score.score:g}/10[/]   {escape_markup(score.verdict)}"
            )
        lines.append("")
        lines.append(f"[{MUTED}]Candidates were shown to the judge as A/B/C only — "
                     f"it never saw the model names.[/]")
        self._verdict.update("\n".join(lines))
        self._verdict.display = True
        self.notify_status(f"Winner: {verdict.winner}", OK)

    def on_compare_grid_row_clicked(self, event: CompareGrid.RowClicked) -> None:
        self.app.push_screen(RowDetailModal(event.row_num, event.source, event.translations))
    def action_cancel(self) -> None:
        self._stop.set()
        self.app.on_shutdown_hook = None
        super().action_cancel()

    def action_back(self) -> None:
        self._stop.set()
        self.app.on_shutdown_hook = None
        super().action_back()


class RowDetailModal(ModalScreen[None]):
    """
    Detailed modal view for a single row's comparison:
    Shows full original source line, then each model's full translation.
    """
    BINDINGS = [Binding("escape,enter,q", "dismiss_none", "close", show=False)]

    DEFAULT_CSS = """
    RowDetailModal .modal {
        width: 84;
        max-height: 80%;
    }
    #detail-scroll {
        max-height: 22;
        margin-bottom: 1;
    }
    #detail-scroll Label {
        margin-top: 1;
    }
    #detail-scroll .panel {
        padding: 1 2;
        border: round #2b323f;
    }
    """

    def __init__(self, row_num: int, source: str, translations: dict[str, str], **kwargs) -> None:
        super().__init__(**kwargs)
        self.row_num = row_num
        self.source = source
        self.translations = translations

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label(f"Row {self.row_num} Detail", classes="title")
            with VerticalScroll(id="detail-scroll"):
                yield Label("SOURCE LINE", classes="muted")
                yield Static(escape_markup(self.source), classes="panel")
                yield Label("")
                for model_label, translation in self.translations.items():
                    yield Label(f"MODEL: {escape_markup(model_label)}", classes="muted")
                    yield Static(escape_markup(translation or "(no translation)"), classes="panel")
                    yield Label("")
            yield KeyHints([("esc/⏎", "close")])

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


# ── Chat ──────────────────────────────────────────────────────────────────────

class ChatScreen(BaseScreen):
    """
    One model, streamed, with the numbers shown — and its thinking shown too.

    The chat is where a reasoning setup is easiest to verify, because everything
    arrives live: the thinking block fills before the answer does, the header
    says how long it took and how many tokens it cost, and `w` flips reasoning
    on or off for the next message so the same question can be asked both ways.

    The rail under the transcript always states what the *next* message will
    carry — not what the last one did — because that is the thing you are about
    to spend tokens on.
    """

    breadcrumb = "Playground › Chat"

    BINDINGS = [
        Binding("ctrl+r", "reset", "reset", show=False),
        Binding("ctrl+t", "toggle_thinking", "fold thinking", show=False),
        Binding("ctrl+w", "cycle_reasoning", "reasoning", show=False),
        Binding("escape", "stop_or_leave", "stop", show=False),
    ]

    FLUSH_SECONDS = 0.05      # how often a streaming worker is allowed to repaint

    def __init__(self, slot: Slot, **kwargs) -> None:
        super().__init__(**kwargs)
        self.slot = slot
        self.messages: list[dict] = []
        self._stop = threading.Event()
        self._streaming = False
        self._started = 0.0
        self._reasoning_override: bool | None = None
        self._thinking_open = True
        self._think_started = 0.0

    def compose_body(self) -> ComposeResult:
        yield Label(f"{self.slot.model_id}", classes="title")
        yield Label(self.slot.where, classes="hint")
        self._reasoning_line = Static("", id="chat-reasoning", classes="hint")
        yield self._reasoning_line
        self.feed = ChatFeed(id="chat-feed")
        with VerticalScroll(id="chat-pane"):
            yield self.feed

    def compose_above_footer(self) -> ComposeResult:
        self._stats = Static("", id="chat-stats")
        yield self._stats
        # Grows with what you type and stops at six lines, so a long message
        # is writable without the transcript disappearing behind the box.
        self._entry = LabeledTextArea(
            "",
            placeholder="Say something…   (Shift+Enter for a new line)",
            min_lines=1,
            max_lines=6,
            id="chat-entry",
        )
        yield self._entry

    DEFAULT_CSS = f"""
    #chat-pane {{ height: 1fr; }}
    #chat-feed {{ height: auto; width: 1fr; }}
    #chat-reasoning {{ height: auto; }}
    #chat-stats {{ height: 1; color: {MUTED}; padding: 0 1; }}
    #chat-entry PasteArea {{ height: 3; }}
    """

    def screen_keys(self) -> list[tuple[str, str]]:
        if self._streaming:
            return [("esc", "stop"), ("^C", "quit")]
        keys = [("⏎", "send"), ("⇧⏎", "new line")]
        if self._model_reasons():
            keys.append(("^W", "reasoning on/off"))
        if any(turn.reasoning for turn in self.feed.turns):
            keys.append(("^T", "fold thinking" if self._thinking_open
                         else "unfold thinking"))
        return keys + [("^R", "reset"), ("←", "back"), ("esc", "cancel")]

    def on_screen_ready(self) -> None:
        self._app = self.app
        self.app.on_shutdown_hook = self._stop.set
        self.set_interval(0.1, self._animate)
        self._entry.focus_input()
        self._paint_reasoning_line()
        self._show_stats("ready")

    # ── Reasoning state ───────────────────────────────────────────────────────

    def _model(self):
        try:
            connection = self.app.registry.get_connection(
                self.slot.provider_id, self.slot.connection_id
            )
        except Exception:
            return None
        return connection.find_model(self.slot.model_id)

    def _model_reasons(self) -> bool:
        model = self._model()
        return bool(model is not None and model.supports_reasoning)

    def _resolved(self):
        """What the next message will ask for. Never raises."""
        from ...providers.reasoning import resolve_for

        return resolve_for(self._model(), override=self._reasoning_override)

    def _paint_reasoning_line(self) -> None:
        """
        What the *next* message will carry, stated before it is sent.

        For a model with no reasoning metadata this says so plainly and points
        at where to configure it — the alternative is someone toggling `^W` and
        wondering why nothing changes.
        """
        from ...providers.reasoning import describe

        model = self._model()
        if model is None or not model.supports_reasoning:
            self._reasoning_line.update(
                "reasoning: not configured for this model — no parameter is sent "
                "(set it under Providers & Models › r)"
            )
            return

        resolved = self._resolved()
        payload = resolved.payload()
        control = resolved.config.control
        state = "on" if resolved.enabled else "off"
        source = "your choice" if self._reasoning_override is not None \
            else "model default"

        if control == "always_on":
            detail = "always on — cannot be switched off, and no parameter is sent"
        elif control == "effort":
            level = resolved.level or "(none)"
            detail = f"effort {level} · {_payload_summary(payload)}"
        else:
            detail = f"{state} ({source}) · {_payload_summary(payload)}"
        self._reasoning_line.update(escape_markup(f"reasoning: {detail}"))

    def action_cycle_reasoning(self) -> None:
        """
        Cycle the next message's reasoning: model default → on → off → …

        Only meaningful for a `toggle` model, and the rail says as much for the
        others rather than silently doing nothing.
        """
        if self._streaming:
            return
        if not self._model_reasons():
            self.notify_status(
                "This model has no reasoning configured — nothing to switch.", WARN
            )
            return
        order = [None, True, False]
        self._reasoning_override = order[
            (order.index(self._reasoning_override) + 1) % len(order)
        ]
        self._paint_reasoning_line()
        self.refresh_keys()
        label = {None: "the model's own default", True: "on", False: "off"}[
            self._reasoning_override
        ]
        self.notify_status(f"Next message: reasoning {label}.", ACCENT)

    def action_toggle_thinking(self) -> None:
        if not any(turn.reasoning for turn in self.feed.turns):
            return
        self._thinking_open = not self._thinking_open
        self.feed.set_reasoning_open(self._thinking_open)
        self._scroll()
        self.refresh_keys()

    # ── Streaming ─────────────────────────────────────────────────────────────

    def _animate(self) -> None:
        self.feed.tick()
        if self._streaming:
            elapsed = time.time() - self._started
            if self._think_started and not self.feed.turns[-1].text:
                self._show_stats(f"{elapsed:.1f}s · thinking…")
            else:
                self._show_stats(f"{elapsed:.1f}s · streaming…")
        self._scroll()

    def _scroll(self) -> None:
        try:
            self.query_one("#chat-pane", VerticalScroll).scroll_end(animate=False)
        except Exception:
            pass

    def _show_stats(self, text: str) -> None:
        self._stats.update(text)

    def on_paste_area_submitted(self, event: PasteArea.Submitted) -> None:
        """Enter sends; Shift+Enter, Ctrl+Enter and Ctrl+J make a new line."""
        if self._streaming:
            return
        said = self._entry.value.strip()
        if not said:
            return
        self._entry.value = ""
        self.messages.append({"role": "user", "content": said})
        self.feed.add(YOU, said)
        turn = self.feed.add(MODEL, "", streaming=True)
        turn.reasoning_open = self._thinking_open
        self._streaming = True
        self._started = time.time()
        self._think_started = 0.0
        self._stop = threading.Event()
        self.refresh_keys()
        self._stream(list(self.messages))

    @work(thread=True)
    def _stream(self, messages: list[dict]) -> None:
        from ...core.models import ProviderError
        from ...providers.client import ProviderClient

        app = self._app
        try:
            provider = app.registry.get_provider(self.slot.provider_id)
            connection = app.registry.get_connection(
                self.slot.provider_id, self.slot.connection_id
            )
        except Exception as exc:
            app.call_from_thread(self._stream_failed, f"{type(exc).__name__}: {exc}")
            return

        client = ProviderClient(provider.base_url, connection.api_key)
        buffer: list[str] = []
        think_buffer: list[str] = []
        last = time.time()
        last_think = time.time()

        def on_delta(piece: str) -> None:
            nonlocal last
            buffer.append(piece)
            now = time.time()
            # Coalesce: one call_from_thread per token floods the message pump
            # and the screen spends its time laying out instead of painting.
            if now - last >= self.FLUSH_SECONDS:
                chunk, last = "".join(buffer), now
                buffer.clear()
                app.call_from_thread(self._chunk, chunk)

        def on_reasoning(piece: str) -> None:
            nonlocal last_think
            think_buffer.append(piece)
            now = time.time()
            if now - last_think >= self.FLUSH_SECONDS:
                chunk, last_think = "".join(think_buffer), now
                think_buffer.clear()
                app.call_from_thread(self._think_chunk, chunk)

        try:
            stats = client.stream_chat(
                model=self.slot.model_id,
                messages=messages,
                on_delta=on_delta,
                reasoning=self._resolved(),
                stop_event=self._stop,
                on_reasoning=on_reasoning,
            )
        except ProviderError as exc:
            app.call_from_thread(self._stream_failed, exc.response_text.strip() or str(exc))
            return
        except Exception as exc:
            app.call_from_thread(self._stream_failed, f"{type(exc).__name__}: {exc}")
            return

        if think_buffer:
            app.call_from_thread(self._think_chunk, "".join(think_buffer))
        if buffer:
            app.call_from_thread(self._chunk, "".join(buffer))
        app.call_from_thread(self._stream_done, stats)

    def _chunk(self, text: str) -> None:
        self.feed.append_to_last(text)
        self.feed.refresh_lines()
        self._scroll()

    def _think_chunk(self, text: str) -> None:
        if not self._think_started:
            self._think_started = time.time()
        self.feed.append_reasoning_to_last(text)
        self.feed.refresh_lines()
        self._scroll()

    def _stream_done(self, stats) -> None:
        self._streaming = False
        self.refresh_keys()
        turn = self.feed.turns[-1] if self.feed.turns else None
        if turn is not None:
            turn.streaming = False
            turn.text = stats.content
            turn.stat = self._stat_line(stats)
            if stats.reasoning:
                turn.reasoning = stats.reasoning
            turn.reasoning_note = self._think_note(stats)
            turn.cache_key = ()
        self.messages.append({"role": "assistant", "content": stats.content})
        self.feed.refresh_lines()
        self._scroll()
        self._show_stats(self._stat_line(stats))
        self._paint_reasoning_line()
        self._verify_reasoning(stats)
        self._entry.focus_input()

    def _think_note(self, stats) -> str:
        """The line beside the thinking header: how long, and how much."""
        if not stats.thought:
            return ""
        bits = []
        if stats.think_ms:
            bits.append(f"first thought at {_duration(stats.think_ms)}")
        if stats.ttft_ms and stats.think_ms:
            bits.append(f"answer at {_duration(stats.ttft_ms)}")
        if stats.reasoning_tokens:
            bits.append(f"{stats.reasoning_tokens} reasoning tokens")
        elif stats.reasoning:
            bits.append(f"{len(stats.reasoning)} chars")
        return " · ".join(bits)

    def _verify_reasoning(self, stats) -> None:
        """
        Say whether the setting reached the model — the point of the exercise.

        A request that asked for reasoning and came back with none is reported as
        a mismatch rather than left to be inferred: the usual cause is a
        `parameter_path` or value that is wrong for this model.
        """
        resolved = self._resolved()
        if not resolved.config.is_supported:
            return
        if resolved.enabled and not stats.thought:
            self.notify_status(
                "Reasoning was requested but the reply carried none — check the "
                "parameter path and values for this model.",
                WARN,
            )
        elif not resolved.enabled and stats.thought:
            self.notify_status(
                "Reasoning was switched off and the model thought anyway — the "
                "off value may not be what this server expects.",
                WARN,
            )
        elif stats.thought:
            self.notify_status("Reasoning confirmed: the model thought.", OK)

    @staticmethod
    def _stat_line(stats) -> str:
        about = "≈" if stats.tokens_estimated else ""
        bits = [
            f"{_duration(stats.ttft_ms)} to first token",
            f"{_duration(stats.latency_ms)} total",
            f"{about}{stats.tokens_per_second:.0f} tok/s",
            f"{about}{stats.completion_tokens} out",
        ]
        if stats.prompt_tokens:
            bits.append(f"{stats.prompt_tokens} in")
        if stats.reasoning_tokens:
            bits.append(f"{stats.reasoning_tokens} think")
        if stats.chunks:
            bits.append(f"{stats.chunks} chunks")
        if stats.stopped:
            bits.append("stopped")
        elif stats.finish_reason:
            bits.append(stats.finish_reason)
        return " · ".join(bits)

    def _stream_failed(self, message: str) -> None:
        self._streaming = False
        self.refresh_keys()
        if self.feed.turns:
            self.feed.turns.pop()          # drop the empty streaming turn
        self.feed.add(NOTICE, message)
        # The failed exchange must not poison the next one.
        if self.messages and self.messages[-1]["role"] == "user":
            self.messages.pop()
        self.feed.refresh_lines()
        self._scroll()
        self._show_stats("failed")
        self._entry.focus_input()

    def action_reset(self) -> None:
        if self._streaming:
            return
        self.messages.clear()
        self.feed.clear()
        self._show_stats("ready")
        self.refresh_keys()
        self._entry.focus_input()

    def action_stop_or_leave(self) -> None:
        if self._streaming:
            self._stop.set()
            self.notify_status("Stopping…", WARN)
            return
        self.app.on_shutdown_hook = None
        super().action_cancel()

    def action_back(self) -> None:
        if self._streaming:
            return
        self.app.on_shutdown_hook = None
        super().action_back()


def _payload_summary(payload: dict) -> str:
    """
    The reasoning fragment as it will appear in the request body.

    Shown verbatim rather than paraphrased: `chat_template_kwargs.thinking=true`
    is checkable against a provider's docs, "reasoning enabled" is not.
    """
    if not payload:
        return "no parameter sent"

    def walk(node, prefix=""):
        for key, value in node.items():
            path = f"{prefix}{key}"
            if isinstance(value, dict):
                yield from walk(value, f"{path}.")
            else:
                shown = "true" if value is True else "false" if value is False else value
                yield f"{path}={shown}"

    return " ".join(walk(payload))
