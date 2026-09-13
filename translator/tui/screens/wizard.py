"""
tui/screens/wizard.py — Translate job configuration wizard.

The redesigned flow (UET_UX §5), no Quick/Full split:

  ModelSelectScreen    the model, via the shared Model Picker
  ReasoningScreen      reasoning, only when the model exposes a choice
  OperationModeScreen  translate / repair / retranslate (folded into step 1)
  FileColumnsScreen    file selection and column mapping on one screen, with
                       header auto-detection of source/destination
  LanguageStyleScreen  source/target languages and the style preset, one screen
  ProcessingScreen     the shared Processing form (rows/request + workers)
  PreviewScreen        review with an Edit that jumps to any part, then Start

Every step writes into `app.draft`, so `←` returns to the previous step with
everything still filled in (04_TUI_SPEC.md §2).
"""

from __future__ import annotations

import os

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Label, Static

from ... import config
from ...core.models import JobConfig
from ...data import app_prefs
from ...data.presets import (
    PRESETS,
    delete_user_preset,
    is_builtin,
    load_user_presets,
    save_user_preset,
    update_user_preset,
)
from ...io.excel_io import ExcelReader, translated_path
from ...parsing.columns import detect_columns
from ...providers.ratelimit import RateLimitPreset
from ...providers.selection import (
    choice_is_runnable,
    is_combo_provider,
    resolve_choice,
)
from ...scheduling.scheduler import Scheduler
from ..app import BaseScreen, MenuScreen
from ..theme import ACCENT, ERROR, MUTED, OK, WARN
from ..widgets.common import (
    ColumnPreview,
    ColumnRole,
    KeyHints,
    LabeledInput,
    LabeledTextArea,
    Option,
    PasteArea,
    Picker,
    escape_markup,
)
from ..widgets.model_picker import ModelPicker
from ..widgets.processing import ProcessingForm
from .combos import combo_column_options, provider_column_options

TOTAL_STEPS = 6

# The six setup steps the redesigned Translate flow presents, matching Judge's
# shape so the two read the same (UET_UX §5):
#   1 Model → 2 Operation → 3 File → 4 Columns → 5 Language & Style →
#   6 Processing, then Review. Reasoning is still asked when the model exposes a
#   choice, folded into the Model step so the count stays honest.

# The columns a job can read. `tag` is what the preview prints beside the
# column letter; `label` is what the caption calls it when it asks for one.
ROLES = {
    "src": ColumnRole("src", "source", ACCENT),
    "dst": ColumnRole("dst", "destination", OK),
    "ctx": ColumnRole("ctx", "context", WARN),
    "fix": ColumnRole("fix", "repair output", WARN),
}

# Which of those each operation mode actually reads, in the order clicking the
# grid fills them. Read off core/engine.py rather than guessed from the mode
# name: `run_repair` only loads src_col when "compare" is in the mode and
# ctx_col when "context" is, so a plain repair never looks at the source
# column — and must not ask you to pick one.
ROLE_ORDER = {
    "translate":              ("src", "dst"),
    "translate_context":      ("src", "dst", "ctx"),
    "repair":                 ("dst", "fix"),
    "repair_compare":         ("src", "dst", "fix"),
    "repair_compare_context": ("src", "dst", "ctx", "fix"),
    "retranslate_failed":     ("src", "dst"),
}

OP_MODES = [
    ("translate", "Translate", "plain source → target"),
    ("translate_context", "Translate with context", "extra context column per row"),
    ("repair", "Repair", "fix an existing translation"),
    ("repair_compare", "Repair + compare", "compare against the source"),
    ("repair_compare_context", "Repair + compare + context", "…plus the context column"),
    ("retranslate_failed", "Retranslate failed/empty/short", "rescan and redo problem rows"),
]

COMMON_LANGS = [
    "Indonesian", "English", "Japanese", "Chinese (Simplified)", "Korean",
    "Spanish", "French", "German", "Russian", "Portuguese (Brazil)",
]


def _as_int(text: str, default: int) -> int | None:
    """`default` for an empty field, None for one holding something that
    isn't a number — the caller decides whether that is worth saying."""
    text = (text or "").strip()
    if not text:
        return default
    try:
        return int(text)
    except ValueError:
        return None


def _default_mode() -> str:
    """The processing mode saved in Settings, or the shipped default."""
    try:
        return app_prefs.default_processing_mode()
    except Exception:
        return config.DEFAULT_PROCESSING_MODE


def _provider_options(registry) -> list[Option]:
    """
    The PROVIDER column, including the fixed `Combo` entry.

    Defined beside the combo screens (`screens/combos.py`) and re-exported here
    so all three pickers — Translate, Judge, Fix All — draw the same column from
    the same function; a second copy is how one of them ends up without the
    Combo entry.
    """
    return provider_column_options(registry)


def _combo_options() -> list[Option]:
    """The MODEL column when Combo is the chosen provider."""
    return combo_column_options()


class WizardScreen(BaseScreen):
    """Adds `Step N of M` to the header and keeps the draft handy."""

    step = 1
    step_title = ""

    @property
    def draft(self) -> dict:
        return self.app.draft

    def _header_text(self) -> str:
        left = "UET v4 › Translate"
        right = f"Step {self.step} of {TOTAL_STEPS}" if self.step else ""
        width = max(20, self.size.width or 80)
        pad = max(1, width - len(left) - len(right) - 2)
        return f"{left}{' ' * pad}{right}"


class WizardMenuScreen(WizardScreen, MenuScreen):
    """Wizard step whose body is a single picker."""


# ── Step 1: provider → connection → model ─────────────────────────────────────

class ModelSelectScreen(WizardScreen):
    """
    Step 1 — the model, via the shared Model Picker.

    Quick/Full is gone (UET_UX §5): the flow always starts here and processing
    is derived automatically later. A Model Group may be chosen under the fixed
    `Model Groups` entry, exactly as a plain model is.
    """

    step = 1

    BINDINGS = [
        Binding("right", "focus_next_col", "next column", show=False),
    ]

    def on_screen_ready(self) -> None:
        self.app.draft.setdefault("mode", "full")
        self._picker.load(self.app.registry)

    def on_screen_resumed(self) -> None:
        self._picker.load(self.app.registry)

    def compose_body(self) -> ComposeResult:
        yield Label("Select a model", classes="title")
        self._picker = ModelPicker(id="translate-model-picker")
        yield self._picker

    def screen_keys(self) -> list[tuple[str, str]]:
        return [("↑↓", "move"), ("←→", "column"), ("⏎", "confirm"),
                ("esc", "cancel")]

    def action_focus_next_col(self) -> None:
        self._picker.action_focus_next_col()

    def action_back(self) -> None:
        if not self._picker.focus_prev_col():
            super().action_back()

    def on_model_picker_chosen(self, event: ModelPicker.Chosen) -> None:
        event.stop()
        choice = event.choice
        self.draft["provider_id"] = choice.provider_id
        self.draft["connection_id"] = choice.connection_id
        self.draft["model_id"] = choice.model_id
        self.draft["combo_id"] = choice.combo.id if choice.is_combo else ""
        self.app.push_screen(ReasoningScreen())


# ── Step 3: reasoning (conditional) ───────────────────────────────────────────

class ReasoningScreen(WizardMenuScreen):
    """
    Reasoning for this run, shaped by what the model actually allows
    (FEATURE-004 FR-404…FR-408).

    Four shapes, and only two of them are a question:

      toggle    on / off — written to `reasoning_enabled`
      effort    a level  — written to `reasoning_effort`
      always_on skipped with a note; nothing can be chosen
      none      skipped silently; no parameter is ever sent

    Skipping matters as much as asking: a screen offering an off switch a model
    does not have would send a parameter the server rejects (FR-407).

    A combo is skipped entirely: its members may control reasoning in four
    different ways, and one answer for all of them would be wrong for at least
    one. Each member's own metadata is applied per request by `RetryExecutor`,
    which is the only layer that knows which member is serving it.
    """

    step = 0
    title_text = "Reasoning"

    def _model(self):
        if self.draft.get("combo_id"):
            return None
        try:
            provider = self.app.registry.get_provider(self.draft["provider_id"])
        except Exception:
            return None
        return next(
            (m for c in provider.connections for m in c.models
             if m.id == self.draft["model_id"]), None
        )

    def on_screen_ready(self) -> None:
        if self.draft.get("combo_id"):
            self.draft["reasoning_effort"] = ""
            self.draft["reasoning_enabled"] = None
            self._skip(
                "Combo: each model follows its own reasoning setting."
            )
            return
        model = self._model()
        control = getattr(model, "reasoning_control", "none") if model else "none"
        supported = bool(model and model.supports_reasoning)

        if not supported or control == "none":
            # Nothing to decide and nothing to send.
            self.draft["reasoning_effort"] = ""
            self.draft["reasoning_enabled"] = None
            self._skip()
            return

        if control == "always_on":
            # It is on and cannot be turned off; asking would be a lie, and no
            # parameter is sent either way (FR-406).
            self.draft["reasoning_effort"] = ""
            self.draft["reasoning_enabled"] = None
            self._skip(f"{model.label}: reasoning is always on for this model.")
            return

        # Quick mode takes the model's own default rather than asking (FR-408).
        if self.draft.get("mode") == "quick":
            self.draft["reasoning_enabled"] = (
                model.reasoning_default if control == "toggle" else None
            )
            self.draft["reasoning_effort"] = (
                self._quick_level(model) if control == "effort" else ""
            )
            self._skip()
            return

        self.title_text = (
            "Reasoning" if control == "toggle" else "Reasoning effort"
        )
        super().on_screen_ready()

    def _skip(self, note: str = "") -> None:
        """
        Skip reasoning when the model has none, jumping straight to file picking.
        """
        self.app.pop_screen()
        from .file_engine import FileEngineScreen
        target = FileEngineScreen(
            on_accept=self._file_engine_accepted,
            next_screen=lambda: ColumnsScreen(),
            existing_path=self.draft.get("src_path", ""),
        )
        if note:
            target.queue_note(note, MUTED)
        self.app.push_screen(target)

    def _file_engine_accepted(self, path: str) -> None:
        if path != self.draft.get("src_path"):
            for key in ("src_col", "dst_col", "ctx_col", "repair_col"):
                self.draft.pop(key, None)
        self.draft["src_path"] = path
        self.draft["out_path"] = translated_path(path)

    @staticmethod
    def _quick_level(model) -> str:
        """The level Quick mode uses: the model's default, or nothing."""
        if not model.reasoning_default:
            return ""
        levels = [str(v) for v in (model.reasoning_options or [])]
        if not levels:
            return ""
        return "medium" if "medium" in levels else levels[0]

    def build_options(self) -> list[Option]:
        model = self._model()
        control = getattr(model, "reasoning_control", "none") if model else "none"

        if control == "toggle":
            default_on = bool(model and model.reasoning_default)
            options = [
                Option("Reasoning off", "off",
                       hint="fastest and cheapest" + ("" if default_on else " — model default")),
                Option("Reasoning on", "on",
                       hint="slower, usually more accurate"
                            + (" — model default" if default_on else "")),
            ]
            return options

        # control == "effort": a level, and no invented off switch (FR-405).
        levels = [str(v) for v in (model.reasoning_options or [])] if model else []
        if not levels:
            levels = ["low", "medium", "high"]
        return [
            Option("Leave it to the model", "",
                   hint="no reasoning_effort is sent")
        ] + [Option(level, level) for level in levels]

    def on_screen_resumed(self) -> None:
        self.picker.set_options(self.build_options(), keep_index=True)

    def handle_choice(self, option: Option) -> None:
        model = self._model()
        control = getattr(model, "reasoning_control", "none") if model else "none"
        if control == "toggle":
            self.draft["reasoning_enabled"] = option.value == "on"
            self.draft["reasoning_effort"] = ""
        else:
            self.draft["reasoning_effort"] = option.value
            self.draft["reasoning_enabled"] = None
        from .file_engine import FileEngineScreen
        self.app.push_screen(FileEngineScreen(
            on_accept=self._file_engine_accepted,
            next_screen=lambda: ColumnsScreen(),
            existing_path=self.draft.get("src_path", ""),
        ))


# ── Step 4: columns ───────────────────────────────────────────────────────────

class ColumnsScreen(WizardScreen):
    """
    Column mapping for the file chosen on the previous step (UET_UX §5).

    Source/Destination are auto-detected from the header
    (`parsing/columns.detect_columns`); nothing is guessed when the header is
    unclear, so an ambiguous sheet asks for a manual pick. Clicking a column in
    the grid fills whichever field the form is pointing at.
    """

    step = 4

    _active = "src"

    def compose_body(self) -> ComposeResult:
        yield Label("Columns and range", classes="title")
        yield Label(os.path.basename(self.draft.get("src_path", "")), classes="hint")
        self._preview = ColumnPreview(id="col-preview", classes="panel")
        yield self._preview
        yield Label("")
        self._src = LabeledInput(
            "Source column", default="A",
            hint="Click a column above, or type C, 3, or D15217 (column + start row).",
        )
        self._dst = LabeledInput("Destination column", default="B")
        self._ctx = LabeledInput("Context column (optional)")
        self._repair = LabeledInput("Repair output column (optional)")
        self._start = LabeledInput("Start row", default="2")
        self._rows = LabeledInput("Rows to process", default="0",
                                  hint="Enter = all remaining rows.")
        yield self._src
        yield self._dst
        yield self._ctx
        yield self._repair
        yield self._start
        yield self._rows
        yield Label("", id="col-error", classes="error")

    def compose_above_footer(self) -> ComposeResult:
        self._confirm = Picker(
            [Option("Confirm and next", "accept", style=OK)], id="confirm-bar"
        )
        yield self._confirm

    def screen_keys(self) -> list[tuple[str, str]]:
        return [("↑↓", "move"), ("tab", "next field"), ("⏎", "confirm"),
                ("←", "back"), ("esc", "cancel")]

    def on_screen_ready(self) -> None:
        self._debounce = None
        self._grid_key: tuple[str, int] | None = None
        self._grid: tuple = ([], [], [], 0)
        self._restore_fields()
        self._auto_detect()
        self._active = self._order()[0]
        self._fields()[self._active].focus_input(scroll_visible=False)
        self._update_preview()

    def _restore_fields(self) -> None:
        for key, field in (("src_col", self._src), ("dst_col", self._dst),
                           ("ctx_col", self._ctx), ("repair_col", self._repair)):
            value = self.draft.get(key)
            if value:
                field.value = ExcelReader.col_letter(value)
        if self.draft.get("start_row"):
            self._start.value = str(self.draft["start_row"])
        if self.draft.get("num_rows"):
            self._rows.value = str(self.draft["num_rows"])

    def _auto_detect(self) -> None:
        """Fill Source/Destination from the header unless already chosen."""
        if self.draft.get("src_col") or self.draft.get("dst_col"):
            return
        path = self.draft.get("src_path", "")
        if not path:
            return
        try:
            _letters, headers, _rows = ExcelReader.grid(path, 1, 1, 26)
        except Exception:
            return
        src_col, dst_col = detect_columns(headers)
        if src_col and not (self._src.input.value or "").strip():
            self._src.value = ExcelReader.col_letter(src_col)
        if dst_col and not (self._dst.input.value or "").strip():
            self._dst.value = ExcelReader.col_letter(dst_col)
        if src_col or dst_col:
            self.queue_note(
                "Source/Destination detected from the header — change them if "
                "the guess is wrong.", MUTED
            )
        else:
            self.queue_note(
                "Could not detect the columns — click the grid or type a letter.",
                WARN
            )

    # ── Column mapping ────────────────────────────────────────────────────────

    def _fields(self) -> dict[str, LabeledInput]:
        return {"src": self._src, "dst": self._dst,
                "ctx": self._ctx, "fix": self._repair}

    def _order(self) -> tuple[str, ...]:
        return ROLE_ORDER.get(self.draft.get("op_mode", ""), ("src", "dst"))

    def _roles(self) -> list[tuple[ColumnRole, LabeledInput]]:
        fields = self._fields()
        return [(ROLES[tag], fields[tag]) for tag in self._order()]

    def _next_tag(self, tag: str) -> str:
        order = self._order()
        if tag in order:
            return order[(order.index(tag) + 1) % len(order)]
        return order[0]

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        for tag, field in self._fields().items():
            if event.widget is field.input and tag != self._active:
                self._active = tag
                self._preview.set_prompt(ROLES[tag])
                return

    def on_column_preview_column_picked(
        self, event: ColumnPreview.ColumnPicked
    ) -> None:
        fields = self._fields()
        field = fields[self._active]
        field.value = self._cell_ref(field, event.column)
        self._active = self._next_tag(self._active)
        fields[self._active].focus_input(scroll_visible=False)
        self._update_preview()

    @staticmethod
    def _cell_ref(field: LabeledInput, column: int) -> str:
        _column, row = ExcelReader.parse_cell_ref(field.input.value)
        letter = ExcelReader.col_letter(column)
        return f"{letter}{row}" if row else letter

    def on_input_changed(self, event: Input.Changed) -> None:
        if self._debounce is not None:
            self._debounce.stop()
        self._debounce = self.set_timer(0.12, self._update_preview)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        order = [self._src, self._dst, self._ctx, self._repair, self._start, self._rows]
        for index, field in enumerate(order):
            if event.input is field.input and index + 1 < len(order):
                order[index + 1].focus_input()
                return
        self._accept()

    def on_picker_selected(self, event: Picker.Selected) -> None:
        self._accept()

    def _parse(self) -> tuple[int, int, int, int, int, int] | None:
        src_col, src_row = ExcelReader.parse_cell_ref(self._src.value)
        dst_col, dst_row = ExcelReader.parse_cell_ref(self._dst.value)
        if src_col is None:
            src_col = int(self._src.value) if self._src.value.isdigit() else None
        if dst_col is None:
            dst_col = int(self._dst.value) if self._dst.value.isdigit() else None
        if not src_col or not dst_col:
            return None

        ctx_col = 0
        if self._ctx.value:
            parsed, _ = ExcelReader.parse_cell_ref(self._ctx.value)
            ctx_col = parsed or (int(self._ctx.value) if self._ctx.value.isdigit() else 0)
        repair_col = 0
        if self._repair.value:
            parsed, _ = ExcelReader.parse_cell_ref(self._repair.value)
            repair_col = parsed or (
                int(self._repair.value) if self._repair.value.isdigit() else 0
            )

        start = src_row or dst_row or _as_int(self._start.value, 2) or 2
        num_rows = _as_int(self._rows.value, 0) or 0
        return src_col, dst_col, ctx_col, repair_col, start, num_rows

    def _update_preview(self) -> None:
        path = self.draft.get("src_path", "")
        if not path:
            self._preview.set_error("No file chosen.")
            return
        parsed = self._parse()
        roles: dict[int, ColumnRole] = {}
        if parsed is not None:
            src_col, dst_col, ctx_col, repair_col, start, _rows = parsed
            columns = {"src": src_col, "dst": dst_col,
                       "ctx": ctx_col, "fix": repair_col}
            for role, _field in self._roles():
                if columns[role.tag]:
                    roles.setdefault(columns[role.tag], role)
        else:
            try:
                start = max(1, int(self._start.value or 2))
            except ValueError:
                start = 2

        key = (path, start)
        if key != self._grid_key:
            try:
                letters, headers, rows = ExcelReader.grid(path, start, 4, 12)
                total = ExcelReader.row_count(path)
            except Exception as exc:
                self._grid_key = None
                self._preview.set_error(f"Cannot read file: {exc}")
                return
            self._grid_key = key
            self._grid = (letters, headers, rows, total)

        letters, headers, rows, total = self._grid
        self._preview.set_grid(
            letters, headers, rows, start, total, roles,
            prompt=ROLES[self._active],
        )

    def _accept(self) -> None:
        error = self.query_one("#col-error", Label)
        for label, box in (("Start row", self._start),
                           ("Rows to process", self._rows)):
            if _as_int(box.input.value, 0) is None:
                error.update(f"{label} must be a number.")
                box.focus_input()
                return

        parsed = self._parse()
        if parsed is None:
            error.update("Source and destination columns are required.")
            return
        src_col, dst_col, ctx_col, repair_col, start, num_rows = parsed
        try:
            total = ExcelReader.row_count(self.draft["src_path"])
        except Exception as exc:
            error.update(f"Cannot read file: {exc}")
            return

        if num_rows <= 0:
            num_rows = max(0, total - start + 1)
        num_rows = min(num_rows, max(0, total - start + 1))
        if num_rows <= 0:
            error.update("That range contains no rows.")
            return

        self.draft.update(
            src_col=src_col, dst_col=dst_col, ctx_col=ctx_col,
            repair_col=repair_col, start_row=start, num_rows=num_rows,
            op_mode="translate_context" if ctx_col else "translate",
        )
        if app_prefs.dedup_mode() != "disabled":
            from ...core.dedup_file import prepare_unique_file
            full_src = self.draft["src_path"]
            full_out = self.draft["out_path"]
            try:
                unique, mapping, total_rows, unique_rows = prepare_unique_file(
                    full_src, src_col, dst_col, start, num_rows, full_out,
                )
            except Exception as exc:
                error.update(f"Cannot prepare unique working file: {exc}")
                return
            if mapping:
                self.draft.update(
                    full_src_path=full_src, full_out_path=full_out,
                    src_path=unique, out_path=translated_path(unique),
                    map_path=mapping, num_rows=unique_rows,
                )
                skipped = total_rows - unique_rows
                self.notify_status(
                    f"Deduplication: {unique_rows:,} unique rows "
                    f"({skipped:,} duplicates skipped — "
                    f"{skipped / max(1, total_rows) * 100:.1f}% savings).",
                    OK,
                )
        self.app.push_screen(LanguageStyleScreen())


# ── Step 3: languages and style (one screen) ──────────────────────────────────

class SaveNewPresetModal(ModalScreen[tuple[bool, str] | None]):
    """
    Modal popup asking to save a custom or modified prompt as a reusable preset.
    Returns:
      (True, name)  -> save to preset list with the given name
      (False, "")   -> use once without saving to presets
      None          -> cancelled
    """

    BINDINGS = [Binding("escape", "dismiss_none", "cancel", show=False)]

    def __init__(self, default_name: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self._default_name = default_name

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label("Save as new preset?", classes="title")
            yield Label("Give this preset a name to save it, or use once without saving:", classes="muted")
            self._name = LabeledInput("Preset name", value=self._default_name, placeholder="e.g. RPG Dialogue Casual")
            yield self._name
            yield Label("", id="save-preset-error", classes="error")
            self.picker = Picker([
                Option("Yes, save to presets list", "yes", style=OK),
                Option("No, use once without saving", "no"),
            ])
            yield self.picker
            yield KeyHints([("↑↓", "move"), ("⏎", "select"), ("esc", "cancel")])

    def on_mount(self) -> None:
        self._name.focus_input()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._save_yes()

    def on_picker_selected(self, event: Picker.Selected) -> None:
        if event.option.value == "yes":
            self._save_yes()
        elif event.option.value == "no":
            self.dismiss((False, ""))
        else:
            self.dismiss(None)

    def _save_yes(self) -> None:
        name = (self._name.value or "").strip()
        if not name:
            self.query_one("#save-preset-error", Label).update("Preset name cannot be empty.")
            self._name.focus_input()
            return
        self.dismiss((True, name))

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class ConfirmPresetChangeModal(ModalScreen[str | None]):
    """
    Modal popup when an existing saved preset's prompt was edited.
    Returns:
      "save" -> update preset file
      "once" -> use edited text once
      None   -> cancelled
    """

    BINDINGS = [Binding("escape", "dismiss_none", "cancel", show=False)]

    def __init__(self, preset_label: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._preset_label = preset_label

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label("Save changes to preset?", classes="title")
            yield Label(
                f"You modified the prompt for '{self._preset_label}'. "
                "Do you want to save these changes to the preset file?",
                classes="muted",
            )
            self.picker = Picker([
                Option("Yes, save changes to preset", "save", style=OK),
                Option("No, use once without saving", "once"),
            ])
            yield self.picker
            yield KeyHints([("↑↓", "move"), ("⏎", "select"), ("esc", "cancel")])

    def on_mount(self) -> None:
        self.picker.focus()

    def on_picker_selected(self, event: Picker.Selected) -> None:
        self.dismiss(str(event.option.value))

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class LanguageStyleScreen(WizardScreen):
    """
    Source/target languages and the translation style on one screen (UET_UX §5).

    The style list is the same one the built-ins and the user's saved presets
    provide, `+ New custom prompt` included, so a file translated with a named
    style stays one keystroke away from being reproduced.
    """

    step = 5

    NEW = "__new__"

    BINDINGS = [
        Binding("d", "delete", "delete", show=False),
        Binding("e", "edit_style", "edit", show=False),
    ]

    def compose_body(self) -> ComposeResult:
        yield Label("Language and style", classes="title")
        self._src = LabeledInput("Source language", default="auto",
                                 hint="auto = let model detect")
        self._tgt = LabeledInput("Target language", default="Indonesian")
        with Horizontal(id="lang-row"):
            with Vertical():
                yield self._src
            with Vertical():
                yield self._tgt
        yield Label("Common: " + " · ".join(COMMON_LANGS), classes="hint")
        yield Label("", id="lang-error", classes="error")
        yield Label("Style", classes="muted")
        with VerticalScroll(id="style-scroll", can_focus=False) as scroll:
            scroll.styles.border = ("round", MUTED)
            self.picker = Picker(click_activates=False)
            self.picker.styles.max_height = 10000
            yield self.picker
        self._prompt_edit = LabeledTextArea(
            "Prompt (press 'e' or click below to edit, Enter for newline)",
            placeholder="Style prompt...",
            min_lines=3,
            max_lines=5,
        )
        yield self._prompt_edit

    def compose_above_footer(self) -> ComposeResult:
        self._confirm_bar = Picker(
            [
                Option("Confirm and next", "confirm", style=OK),
                Option("Edit prompt in place (e)", "edit"),
            ],
            id="style-actions",
        )
        yield self._confirm_bar

    def on_screen_ready(self) -> None:
        self._src.value = self.draft.get("src_lang", "")
        self._tgt.value = self.draft.get("tgt_lang", "")
        self._reload()
        self._src.focus_input()

    def _reload(self, keep_index: bool = False) -> None:
        self._saved = load_user_presets()
        options = [Option(preset["label"], preset["key"]) for preset in PRESETS]
        options += [
            Option(preset["label"], preset["key"], hint="saved", style=OK)
            for preset in self._saved
        ]
        options.append(Option("+ New custom prompt", self.NEW, style=ACCENT))
        self.picker.set_options(options, keep_index=keep_index)
        try:
            scroll = self.query_one("#style-scroll", VerticalScroll)
            scroll.styles.height = min(len(options), 10) + 2
        except Exception:
            pass
        self.refresh_keys()
        self._show_prompt()

    def screen_keys(self) -> list[tuple[str, str]]:
        keys = [("tab", "next field"), ("↑↓", "style"), ("e", "edit prompt"), ("⏎", "confirm")]
        if getattr(self, "_saved", None):
            keys.append(("d", "delete saved"))
        return keys + [("←", "back"), ("esc", "cancel")]

    def _find(self, key) -> dict | None:
        return next((p for p in PRESETS + self._saved if p["key"] == key), None)

    def on_picker_highlighted(self, event: Picker.Highlighted) -> None:
        if event.picker is self.picker:
            self._show_prompt()
            self._keep_style_cursor_visible()

    def _keep_style_cursor_visible(self) -> None:
        try:
            scroll = self.query_one("#style-scroll", VerticalScroll)
            view = max(1, scroll.size.height - 1)
            y = scroll.scroll_offset.y
            index = self.picker.index
            if index < y:
                scroll.scroll_to(y=index, animate=False)
            elif index >= y + view:
                scroll.scroll_to(y=index - view + 1, animate=False)
        except Exception:
            pass

    def _show_prompt(self) -> None:
        option = self.picker.current
        if option is None or option.value == self.NEW:
            return
        preset = self._find(option.value)
        if preset:
            self._prompt_edit.value = preset["prompt"]

    def _langs_ok(self) -> bool:
        if not self._tgt.value:
            self.query_one("#lang-error", Label).update("Target language is required.")
            self._tgt.focus_input()
            return False
        self.query_one("#lang-error", Label).update("")
        self.draft["src_lang"] = self._src.value
        self.draft["tgt_lang"] = self._tgt.value
        return True

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input is self._src.input:
            self._tgt.focus_input()
            return
        if event.input is self._tgt.input:
            if self._langs_ok():
                self.picker.focus()
            return

    def on_paste_area_submitted(self, event: PasteArea.Submitted) -> None:
        # Pressing Enter inside multi-line prompt editor inserts a newline;
        # does NOT submit the wizard so user never accidentally advances!
        event.area.insert("\n")

    def on_picker_selected(self, event: Picker.Selected) -> None:
        if event.picker is getattr(self, "_confirm_bar", None):
            if event.option.value == "confirm":
                self._confirm()
            elif event.option.value == "edit":
                self.action_edit_style()
            return
        if event.picker is self.picker:
            if event.option.value == self.NEW:
                self._prompt_edit.value = ""
                self._prompt_edit.focus_input()
                return
            self._show_prompt()
            self._confirm_bar.focus()

    def action_edit_style(self) -> None:
        self._prompt_edit.focus_input()
        self.refresh_keys()

    def _confirm(self) -> None:
        if not self._langs_ok():
            return
        prompt = self._prompt_edit.value.strip()
        if not prompt:
            self.notify_status("Style prompt cannot be empty.", WARN)
            self._prompt_edit.focus_input()
            return
        self._pending_prompt = prompt

        option = self.picker.current
        is_new = (option is None or option.value == self.NEW)
        preset = self._find(option.value) if (option and option.value != self.NEW) else None

        if is_new:
            # Case 1: Custom prompt created from "+ New custom prompt" -> Ask to save
            self.app.push_screen(
                SaveNewPresetModal(),
                self._handle_new_preset_modal,
            )
            return

        if preset:
            original_prompt = (preset.get("prompt") or "").strip()
            if prompt != original_prompt:
                # Prompt was modified!
                if is_builtin(option.value):
                    # Builtin preset cannot be overwritten directly -> offer to save as new preset
                    self.app.push_screen(
                        SaveNewPresetModal(default_name=f"{preset['label']} (Custom)"),
                        self._handle_new_preset_modal,
                    )
                    return
                else:
                    # User's own saved preset (green) -> offer to save changes or use once
                    self.app.push_screen(
                        ConfirmPresetChangeModal(preset["label"]),
                        self._handle_change_preset_modal,
                    )
                    return

        # Case 3: Untouched preset -> continue directly
        self.draft["style_prompt"] = prompt
        self.app.push_screen(ProcessingScreen())

    def _handle_new_preset_modal(self, result: tuple[bool, str] | None) -> None:
        if result is None:
            # Cancelled -> stay on screen, focus prompt editor
            self._prompt_edit.focus_input()
            return

        save_it, name = result
        prompt = getattr(self, "_pending_prompt", "") or self._prompt_edit.value.strip()

        if save_it:
            try:
                new_preset = save_user_preset(name, prompt)
                self._reload()
                # Select the newly created preset in the list
                for idx, opt in enumerate(self.picker.options):
                    if opt.value == new_preset["key"]:
                        self.picker.index = idx
                        break
                self._keep_style_cursor_visible()
                self._show_prompt()
                self.notify_status(f"Saved preset '{name}'. Select and confirm to proceed.", OK)
                self.picker.focus()
            except Exception as exc:
                self.notify_status(f"Could not save preset: {exc}", ERROR)
        else:
            self.draft["style_prompt"] = prompt
            self.app.push_screen(ProcessingScreen())

    def _handle_change_preset_modal(self, choice: str | None) -> None:
        if choice is None:
            # Cancelled -> stay on screen, focus prompt editor
            self._prompt_edit.focus_input()
            return

        prompt = getattr(self, "_pending_prompt", "") or self._prompt_edit.value.strip()
        option = self.picker.current
        preset = self._find(option.value) if option else None

        if choice == "save" and preset:
            try:
                update_user_preset(preset["key"], prompt)
                self._reload(keep_index=True)
                self.notify_status(f"Updated preset '{preset['label']}'.", OK)
            except Exception as exc:
                self.notify_status(f"Could not update preset: {exc}", ERROR)
            self.draft["style_prompt"] = prompt
            self.app.push_screen(ProcessingScreen())
        elif choice == "once":
            self.draft["style_prompt"] = prompt
            self.app.push_screen(ProcessingScreen())

    def action_delete(self) -> None:
        option = self.picker.current
        if option is None or option.value == self.NEW:
            return
        if is_builtin(option.value):
            self.notify_status("Built-in styles cannot be deleted.", WARN)
            return
        preset = self._find(option.value)
        try:
            delete_user_preset(option.value)
        except OSError as exc:
            self.notify_status(f"Could not delete the preset: {exc}", ERROR)
            return
        self._reload(keep_index=True)
        self.notify_status(f"Deleted {preset['label'] if preset else 'preset'}.", WARN)

    def action_cancel(self) -> None:
        super().action_cancel()


# ── Step 4: processing (shared form) ──────────────────────────────────────────

class ProcessingScreen(WizardScreen):
    """
    The shared Processing form (UET_UX §7). Rows/request and workers are
    recommended from the model's (or group's) rate limit; Enter accepts, typing
    overrides with a warning but never a block. The grouped execution options —
    processing mode, write mode, anti-desync, logging/resume — sit below.
    """

    step = 6

    def _choice(self):
        draft = self.draft
        return resolve_choice(
            self.app.registry,
            draft.get("provider_id", ""),
            draft.get("connection_id", ""),
            draft.get("model_id", ""),
        )

    def compose_body(self) -> ComposeResult:
        choice = self._choice()
        self._preset = choice.preset
        yield Label(f"{choice.describe()} · preset {self._preset.label()}",
                    classes="hint")
        self._form = ProcessingForm(
            self._preset,
            rows_value=self.draft.get("batch_size"),
            workers_value=self.draft.get("n_workers"),
            advanced=True,
            draft=self.draft,
            id="translate-processing",
        )
        yield self._form

    def compose_above_footer(self) -> ComposeResult:
        self._confirm = Picker(
            [Option("Confirm and review", "accept", style=OK)], id="confirm-bar"
        )
        yield self._confirm

    def screen_keys(self) -> list[tuple[str, str]]:
        return [("↑↓", "move"), ("space", "toggle"), ("tab", "next field"),
                ("⏎", "confirm"), ("←", "back"), ("esc", "cancel")]

    def on_screen_ready(self) -> None:
        self._form.focus_first()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._accept()

    def on_picker_selected(self, event: Picker.Selected) -> None:
        # Only the confirm bar submits; the radio-group pickers inside the form
        # set their own value without leaving the screen.
        if event.picker is getattr(self, "_confirm", None):
            self._accept()

    def _accept(self) -> None:
        values = self._form.values()
        preset = self._preset
        self.draft.update(
            preset=preset,
            batch_size=values.rows_per_request,
            n_workers=values.workers,
            processing_mode=values.processing_mode,
            write_mode=values.write_mode,
            anti_desync=values.anti_desync,
            skip_filled=values.skip_filled,
            debug_log=values.debug_log,
            raw_log=values.raw_log,
        )
        self.app.push_screen(PreviewScreen())


# ── Step 5: review & start ────────────────────────────────────────────────────

class PreviewScreen(WizardMenuScreen):
    step = 5
    title_text = "Review and start"

    def compose_extra(self) -> ComposeResult:
        yield Label("")
        yield Static(self._summary(), id="job-summary", classes="panel")

    def _summary(self) -> str:
        draft = self.draft
        registry = self.app.registry
        choice = resolve_choice(
            registry,
            draft.get("provider_id", ""),
            draft.get("connection_id", ""),
            draft.get("model_id", ""),
        )
        preset: RateLimitPreset = draft["preset"]
        if choice.is_combo:
            combo = choice.combo
            provider_line = (
                f"Group · {combo.strategy.replace('_', '-')} over {combo.size} model(s)"
                if combo is not None else "Group · (missing)"
            )
        else:
            provider = choice.provider
            connection = choice.connection
            conn_label = (
                connection.name if connection
                else (
                    f"auto · {provider.strategy.replace('_', '-')} over "
                    f"{len(provider.connections)}"
                    if provider is not None else "?"
                )
            )
            provider_line = (
                f"{getattr(provider, 'name', '?')} · {conn_label}"
            )
        wave = draft["batch_size"] * draft["n_workers"]
        src_letter = ExcelReader.col_letter(draft["src_col"])
        dst_letter = ExcelReader.col_letter(draft["dst_col"])
        end_row = draft["start_row"] + draft["num_rows"] - 1

        already = ""
        try:
            filled = ExcelReader.filled_rows(
                draft["out_path"], draft["dst_col"], draft["start_row"], draft["num_rows"]
            )
            if filled and draft.get("skip_filled", True):
                already = f"{len(filled)} rows already filled → will be skipped"
        except Exception:
            pass

        workers = max(1, draft["n_workers"])
        bound = preset.limited_by(draft["batch_size"])

        # Check deduplication to provide accurate unique rows and realistic estimates
        from ...data.app_prefs import dedup_mode
        current_dedup = dedup_mode()
        unique_to_translate = draft["num_rows"]
        dup_saved = 0
        total_source_count = draft["num_rows"]
        if current_dedup != "disabled":
            try:
                src_entries = ExcelReader.load_column(
                    draft["src_path"], draft["src_col"], draft["start_row"], draft["num_rows"]
                )
                non_empty = [t for _, t in src_entries if (t or "").strip()]
                if non_empty:
                    total_source_count = len(non_empty)
                    unique_count = len(set(non_empty))
                    dup_saved = total_source_count - unique_count
                    if dup_saved > 0:
                        unique_to_translate = unique_count
            except Exception:
                pass

        batches_count = max(1, -(-unique_to_translate // draft["batch_size"]))
        if preset.unlimited:
            limits_line = (
                f"{preset.tokens_per_request(draft['batch_size']):,} tok/request · "
                f"no rate limit — paced by {workers} worker(s) only"
            )
            estimate_line = (
                f"~{batches_count} batches"
                + (f" (based on {unique_to_translate:,} unique rows)" if dup_saved else "")
                + " · no limit to estimate against"
            )
        else:
            rows_per_min = max(
                1,
                preset.sustainable_rows_per_minute(draft["batch_size"]) * workers,
            )
            minutes = max(1, round(unique_to_translate / rows_per_min))
            limits_line = (
                f"{preset.tokens_per_request(draft['batch_size']):,} tok/request · "
                f"{bound}-bound · ~{rows_per_min:,} rows/min sustained"
            )
            estimate_line = (
                f"~{minutes} min · ~{batches_count} batches"
                + (f" (based on {unique_to_translate:,} unique rows)" if dup_saved else "")
            )
        rate_line = (
            "no rate limit" if preset.unlimited
            else f"{preset.rpm} rpm / {preset.tpm:,} tpm"
        )

        display_src = os.path.basename(draft.get("full_src_path") or draft["src_path"])
        display_out = os.path.basename(draft.get("full_out_path") or draft["out_path"])

        lines = [
            f"[{MUTED}]Source[/]      {display_src}   "
            f"{src_letter} → {dst_letter}, rows {draft['start_row']}–{end_row} "
            f"({draft['num_rows']:,} total)",
        ]
        if dup_saved > 0:
            lines.append(
                f"[{MUTED}]Deduplication[/] [{OK}]{unique_to_translate:,} unique rows to translate[/] "
                f"({dup_saved:,} duplicates skipped — {dup_saved / max(1, total_source_count) * 100:.1f}% token savings)"
            )
        lines += [
            f"[{MUTED}]Saves to[/]    [{OK}]{display_out}[/]"
            f"   [{MUTED}]source file is never modified[/]",
            f"[{MUTED}]Provider[/]    {escape_markup(provider_line)}",
            f"[{MUTED}]Model[/]       {escape_markup(choice.label)} · "
            f"{preset.label()} ({rate_line})",
            f"[{MUTED}]Job[/]         {draft['op_mode']} · "
            f"{draft.get('src_lang', 'auto')} → {draft.get('tgt_lang', '')}",
            f"[{MUTED}]Processing[/]  {draft['batch_size']} rows/request · "
            f"{draft['n_workers']} worker(s) · {draft['processing_mode']} · "
            f"wave {wave} rows · {preset.wave_delay}s delay",
            f"[{MUTED}]Limits[/]      {limits_line}",
        ]
        if already:
            lines.append(f"[{WARN}]Resume[/]      {already}")
        lines.append(f"[{MUTED}]Estimate[/]    {estimate_line}")
        return "\n".join(lines)

    def build_options(self) -> list[Option]:
        return [
            Option("Start Translation", "start", style=OK),
            Option("Edit", "edit", hint="change a part without walking back"),
            Option("Cancel", "cancel"),
        ]

    def handle_choice(self, option: Option) -> None:
        from .run import RunScreen

        if option.value == "edit":
            self.app.push_screen(_TranslateEditScreen())
        elif option.value == "cancel":
            self.app.go_home()
        else:
            self.app.push_screen(RunScreen(self.build_job()))

    def build_job(self) -> JobConfig:
        draft = self.draft
        preset: RateLimitPreset = draft["preset"]
        job = JobConfig(
            provider_id=draft["provider_id"],
            model_id=draft["model_id"],
            connection_id=draft.get("connection_id", ""),
            src_path=os.path.abspath(draft["src_path"]),
            out_path=os.path.abspath(draft["out_path"]),
            full_src_path=os.path.abspath(draft.get("full_src_path", "")) if draft.get("full_src_path") else "",
            full_out_path=os.path.abspath(draft.get("full_out_path", "")) if draft.get("full_out_path") else "",
            map_path=os.path.abspath(draft.get("map_path", "")) if draft.get("map_path") else "",
            src_col=draft["src_col"],
            dst_col=draft["dst_col"],
            ctx_col=draft.get("ctx_col", 0),
            repair_col=draft.get("repair_col", 0),
            start_row=draft["start_row"],
            num_rows=draft["num_rows"],
            batch_size=draft["batch_size"],
            src_lang=draft.get("src_lang", "auto"),
            tgt_lang=draft.get("tgt_lang", ""),
            style_prompt=draft.get("style_prompt", ""),
            op_mode=draft["op_mode"],
            processing_mode=draft["processing_mode"],
            n_workers=draft["n_workers"],
            write_mode=draft.get("write_mode", "safe"),
            anti_desync=draft.get("anti_desync", "auto"),
            reasoning_effort=draft.get("reasoning_effort", ""),
            reasoning_enabled=draft.get("reasoning_enabled"),
            skip_filled=draft.get("skip_filled", True),
            debug_log=draft.get("debug_log", False),
            raw_log=draft.get("raw_log", False),
            mode=draft.get("mode", "full"),
        )
        job.wave_delay = preset.wave_delay
        job.batch_delay = preset.delay_per_request

        resolve_choice(
            self.app.registry,
            draft.get("provider_id", ""),
            draft.get("connection_id", ""),
            draft.get("model_id", ""),
        ).apply_to(job)

        if job.debug_log or job.raw_log:
            base_out = job.full_out_path or job.full_src_path or job.out_path
            job.log_path = os.path.splitext(base_out)[0] + "_uet.log"
        return job


class _TranslateEditScreen(WizardMenuScreen):
    """
    Review's Edit: jump straight to the part to change (UET_UX §5 Review).

    Each choice pushes the relevant setup screen; walking forward from there
    lands back on Review with the rest of the draft intact.
    """

    step = 6
    title_text = "Edit which part?"

    def build_options(self) -> list[Option]:
        return [
            Option("Model", "model"),
            Option("File", "file"),
            Option("Columns", "columns"),
            Option("Language & style", "lang"),
            Option("Processing", "processing"),
            Option("Back to review", "back"),
        ]

    def handle_choice(self, option: Option) -> None:
        target = {
            "model": ModelSelectScreen,
            "file": lambda: FileEngineScreen(
                on_accept=lambda p: self.draft.update(src_path=p, out_path=translated_path(p)),
                next_screen=lambda: ColumnsScreen(),
                existing_path=self.draft.get("src_path", ""),
            ),
            "columns": ColumnsScreen,
            "lang": LanguageStyleScreen,
            "processing": ProcessingScreen,
        }.get(option.value)
        if target is None:
            self.app.pop_screen()
            return
        self.app.push_screen(target())


