"""
tui/screens/judge.py — The Judge screens.

The setup is a wizard, and it walks the same road the translate wizard does —
model, file & columns, criteria, language & style, processing, review — because
someone who has just configured a translation already knows this shape and
should not have to learn a second one:

  JudgeModelScreen      the model (via the shared Model Picker, fresh each session)
  JudgeFileScreen       pick a spreadsheet from the folder, or type a path
  JudgeColumnsScreen    click the grid: source, then translation, then context
  JudgeCriteriaScreen   what to judge on (multi-select)
  JudgeStyleScreen      languages, and the style preset to judge against
  JudgePacingScreen     the shared Processing form
  JudgePreviewScreen    the estimate, the review, and the confirmation if large
  JudgeRunScreen        the grid, the Activity Panel, and every row action
  FixModelPopup         the model for a single-row Fix
  FixAllSetupScreen     scope, model, categories, how many at a time
  JudgeFixReviewScreen  staged fixes, approved one at a time or all at once
  JudgeSummaryScreen    score, problem count, error-type distribution, Judge Again
  ConfirmSaveModal      Problems / Fixed / Remaining, before leaving to the summary
  RowDetailModal        one row, in full
  RowEditModal          one row, edited by hand
  ConfirmRevertModal    both versions of a repaired row, before undoing it

Quick/Full is gone (UET_UX §6): every evaluation walks the same steps, and the
processing figures are recommended from the rate limit rather than asked cold.

State lives in `app.judge_draft`, so `←` returns to the previous step with
everything still filled in — the same contract `app.draft` gives the translate
wizard.

Three constraints run through all of it.

**The model is chosen every session.** It is deliberately step one rather than a
field buried in a form, and no default is remembered (FR-102 / AC-107). Judging
is the one operation where the identity of the model *is* the result, so
inheriting last session's choice would quietly change what a score means.

**Nothing is saved on its own.** The grid is a report and the staged fixes are a
proposal. The only write happens when someone approves a row, and it goes
through `JudgeRunner.apply_fix` — the only writer in the feature (FR-111). A
hand edit and a revert are the two writes that skip the staging screen, because a
human typed the text in one and chose between two visible versions in the other;
there is nothing left to review and nobody else to review it.

**A batch shows the panel; a single row shows a spinner.** Evaluation, Fix All
and the batch re-judge run workers, so they get the same Activity Panel the run
screen uses (FR-207). Fix It and Judge Again are one request about one row, so
they get a small indicator next to the action and nothing more (FR-208 /
AC-206).

**A re-check has to be as hard as the first check.** `g` re-judges one row on
its own, which is cheap but weaker: alone, a row cannot be compared with
anything, so every consistency question passes by default. `G` re-judges every
applied fix through the evaluation path — `plan()` then `evaluate()`, the same
prompt and the same request shape — so a fix that is still wrong is flagged
again instead of quietly clearing. It reads the file, so it covers applied fixes
only; a staged fix is not yet what the file says.

The results screen holds the entire loop — review, fix one, fix a filtered
batch, re-judge, edit, revert — because that loop is iterative by nature. Making
any step of it a trip to another screen and back is what turns a review into a
chore, and a chore does not get finished.

Every cell in the grid is model output or spreadsheet content, so it is painted
as `rich.text.Text` and never as markup: a `[bracket]` in a translation must not
be able to kill a repaint.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field

from rich.text import Text
from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Input, Label, Static

from ... import config
from ...core.models import JobConfig
from ...data import app_prefs
from ...data.combos import COMBO_PROVIDER_ID, find_combo, load_combos
from ...data.presets import (
    PRESETS,
    delete_user_preset,
    is_builtin,
    load_user_presets,
    save_user_preset,
)
from ...io.excel_io import ExcelReader
from ...judge import estimator
from ...judge.models import (
    CRITERIA,
    CRITERION_LABELS,
    SEVERITIES,
    SEVERITY_LABELS,
    JudgeReport,
    StagedFix,
)
from ...judge.runner import (
    DEFAULT_FIX_BATCH_ROWS,
    EvaluationPlan,
    JudgeRunner,
    JudgeTarget,
)
from ...providers.ratelimit import RateLimitPreset
from ...providers.selection import (
    choice_is_runnable,
    is_combo_provider,
    resolve_choice,
)
from ..app import BaseScreen, MenuScreen
from ..theme import ACCENT, ERROR, MUTED, OK, WARN
from ..widgets.activity import (
    ActivityHeader,
    ActivityPanel,
    EventsHeader,
    EventsPanel,
    SmallIndicator,
    WorkerLedger,
)
from ..widgets.common import (
    ColumnPreview,
    ColumnRole,
    ErrorBox,
    KeyHints,
    LabeledInput,
    LabeledTextArea,
    Option,
    PasteArea,
    Picker,
    ProviderNotice,
    escape_markup,
    flatten_cell,
    strip_line,
    truncate,
)
from ..widgets.compare import Column, CompareGrid
from ..widgets.model_picker import ModelPicker
from ..widgets.processing import ProcessingForm
from ...parsing.columns import detect_columns

# The setup is one length now — Quick/Full is gone (UET_UX §6). Every evaluation
# walks the same steps and the header counts them:
#   model → file & columns → criteria → language & style → processing → review
FULL_STEPS = 6
QUICK_STEPS = 6

# The columns Judge reads, tinted in the preview the same way the wizard tints
# the ones a translate job reads. Order is the order clicking fills them.
ROLES = {
    "src": ColumnRole("src", "source", ACCENT),
    "dst": ColumnRole("dst", "translation", OK),
    "ctx": ColumnRole("ctx", "context", WARN),
}
ROLE_ORDER = ("src", "dst", "ctx")

# Grid column letters. Judge has one "model" column — the comment — so the
# letter is cosmetic, but CompareGrid keys its columns by it.
COMMENT_LETTER = "J"

# Severity carries colour everywhere it appears, and always the same colour:
# critical reads as an error because it is one, minor as a note because it is.
_SEVERITY_STYLES = {
    "critical": ERROR,
    "moderate": WARN,
    "minor":    MUTED,
}


def _as_column(value: str) -> int:
    """`"C"`, `"3"`, `"D15"` → 3/4. 0 when it is not a column at all."""
    text = (value or "").strip()
    if not text:
        return 0
    column, _row = ExcelReader.parse_cell_ref(text)
    if column:
        return column
    return int(text) if text.isdigit() else 0


def _fix_batch_default() -> int:
    """
    Rows per Fix All request the wizard opens with.

    Read from Settings on every open rather than captured at import, so changing
    the default takes effect without restarting the app. Falls back to the
    shipped constant, because prefs are a convenience and a repair run must not
    depend on them being readable (CR-002 FR-613).
    """
    try:
        return app_prefs.fix_batch_rows()
    except Exception:
        return DEFAULT_FIX_BATCH_ROWS


class JudgeWizardScreen(BaseScreen):
    """
    Adds `Step N of M` to the header and hands every step the same draft.

    Mirrors `WizardScreen` rather than sharing it: the translate wizard's draft
    is a translation job and gets reset when that wizard starts, and an
    evaluation must not be able to trample it.
    """

    #: Position in the sequence. `step_quick` is kept for the base-class shape but
    #: mirrors `step_full` now that Quick/Full is gone.
    step_full: int = 1
    step_quick: int | None = 1

    @property
    def draft(self) -> dict:
        if not hasattr(self.app, "judge_draft") or self.app.judge_draft is None:
            self.app.judge_draft = {}
        return self.app.judge_draft

    @property
    def step(self) -> int:
        return self.step_full

    def total_steps(self) -> int:
        return FULL_STEPS

    def _header_text(self) -> str:
        left = "UET v4 › Judge"
        right = f"Step {self.step} of {self.total_steps()}" if self.step else ""
        width = max(20, self.size.width or 80)
        pad = max(1, width - len(left) - len(right) - 2)
        return f"{left}{' ' * pad}{right}"


class JudgeSetupScreen(JudgeWizardScreen):
    """
    The entry point, from either door (FR-101).

    It holds no fields of its own: it seeds the draft — empty from the main
    menu, pre-filled from a job that has just finished — and hands over to step
    one. Keeping the class name means both call sites stay as they were.
    """

    breadcrumb = "Judge"

    def __init__(self, job: JobConfig | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._job = job

    def on_screen_ready(self) -> None:
        job = self._job
        self.app.judge_draft = {}
        # A fresh evaluation resets the per-session Fix and Rejudge model
        # choices (UET_UX §6 / bug #10): a new file/job must ask again.
        self.app.judge_fix_slot = None
        self.app.judge_rejudge_slot = None
        draft = self.app.judge_draft
        if job is not None:
            # Everything about the file is already known, so the wizard's
            # later steps arrive filled in and can be walked through with Enter.
            draft.update(
                path=job.out_path,
                src_col=job.src_col,
                dst_col=job.dst_col,
                ctx_col=job.ctx_col,
                start_row=job.start_row,
                num_rows=job.num_rows,
                src_lang=job.src_lang,
                tgt_lang=job.tgt_lang,
                style_prompt=job.style_prompt,
                custom_placeholders=job.custom_placeholders,
                # The engine the translate run used — carrying it keeps the
                # re-judge measured against the same window (and stops the
                # file screen from overwriting the remembered engine with
                # "general" just because the picker defaulted there).
                fitting_engine=getattr(job, "fitting_engine", "general"),
                from_job=True,
            )
        self.app.pop_screen()
        self.app.push_screen(JudgeModelScreen())


class FixModelPopup(ModalScreen[tuple]):
    """
    Pick the model for a single-row Fix or a Rejudge (UET_UX §6 / bug #10).

    A flat model+group list plus one checkbox: `Don't ask again this session`.
    Ticking it stores the choice on the app so the rest of this Judge session's
    operations of the same kind skip the popup; a new evaluation resets it. Fix
    and Rejudge keep *separate* remembered slots, so a model chosen to repair
    with is never silently reused to grade with. Returns `(slot, remember)` or
    None.
    """

    BINDINGS = [Binding("escape", "dismiss_none", "cancel", show=False)]

    DEFAULT_CSS = """
    FixModelPopup .modal { width: 72; height: auto; max-height: 95%; }
    FixModelPopup #fix-model-body { height: auto; max-height: 1fr; }
    FixModelPopup #fix-model-foot { dock: bottom; height: auto; }
    """

    def __init__(
        self,
        model_slot: tuple[str, str, str],
        title: str = "Fix this row with",
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._judge_slot = model_slot
        self._title = title

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label(self._title, classes="title")
            with VerticalScroll(id="fix-model-body"):
                self._model = Picker()
                yield self._model
                yield Label("")
                self._remember = Picker([
                    Option("Don't ask again this session", "remember"),
                ], multi_select=True)
                yield self._remember
            with Vertical(id="fix-model-foot"):
                yield KeyHints([("↑↓", "move"), ("space", "toggle"),
                                ("⏎", "select"), ("esc", "cancel")])

    def on_mount(self) -> None:
        self._model.set_options(_fix_model_options(self.app.registry, self._judge_slot))
        self._model.focus()

    def on_picker_selected(self, event: Picker.Selected) -> None:
        if event.picker is self._remember:
            return
        option = self._model.current
        if option is None or not option.value:
            return
        remember = "remember" in self._remember.selected_values()
        self.dismiss((option.value, remember))

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


def _fix_model_options(registry, current) -> list[Option]:
    """Flat model + group list for a Fix popup, judge model first."""
    options: list[Option] = []
    for provider in registry.providers:
        for connection in provider.connections:
            for model in connection.models:
                slot = (provider.id, connection.id, model.id)
                is_judge = slot == current
                options.append(Option(
                    model.label + ("   (judge model)" if is_judge else ""),
                    slot,
                    hint=f"{provider.name} · {connection.name} · {model.preset.label()}",
                    style=OK if is_judge else "",
                ))
    for combo in load_combos():
        if not combo.members:
            continue
        slot = (COMBO_PROVIDER_ID, "", combo.id)
        is_judge = slot == current
        options.append(Option(
            f"Group · {combo.label}" + ("   (judge model)" if is_judge else ""),
            slot,
            hint=f"{combo.size} model(s) · {combo.strategy.replace('_', '-')}",
            style=OK if is_judge else ACCENT,
        ))
    options.sort(key=lambda option: option.value != current)
    return options or [
        Option("No models available — add one under Providers & Models", None,
               enabled=False, style=MUTED)
    ]


class RejudgeSetupScreen(ModalScreen["tuple | None"]):
    """
    The setup step for a re-judge: which model, at what pacing (UET_UX bug #10).

    A re-judge is a grading job in its own right, so it asks which model to
    grade with rather than silently reusing the Judge or Fix model — and
    unlike a single-row Fix, it runs as a batch, so the rows/request and
    worker count are its pacing and worth their own fields.

    The `Don't ask again for this model` checkbox is per-model and persisted:
    ticking it for Model A stores A's settings (model, rows, workers) in the
    app preferences, and the next re-judge that lands on A runs immediately
    with them — while Model B still asks, because B has no stored settings.
    Unticking removes the entry, so "stop remembering" is one checkbox, not
    a hunt through Settings.

    Returns `(slot, max_rows, workers)` or None.
    """

    DEFAULT_CSS = """
    RejudgeSetupScreen .modal { width: 76; height: auto; max-height: 95%; }
    RejudgeSetupScreen #rejudge-body { height: auto; max-height: 1fr; }
    RejudgeSetupScreen #rejudge-foot { dock: bottom; height: auto; }
    """

    BINDINGS = [Binding("escape", "dismiss_none", "cancel", show=False)]

    def __init__(
        self,
        model_slot: tuple[str, str, str],
        scope_label: str = "",
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._judge_slot = model_slot
        self._scope = scope_label

    # ── Layout ────────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label("Re-judge with", classes="title")
            if self._scope:
                yield Label(self._scope, classes="hint")
            with VerticalScroll(id="rejudge-body"):
                self._model = Picker(click_activates=False)
                yield self._model
                self._batch = LabeledInput(
                    "Rows / request",
                    value=str(_rejudge_rows_default()),
                    default=str(_rejudge_rows_default()),
                )
                self._workers = LabeledInput("Workers")
                yield self._batch
                yield self._workers
                self._remember = Picker([
                    Option("Don't ask again for this model", "remember"),
                ], multi_select=True)
                yield self._remember
                yield Static("", id="rejudge-scope", classes="hint")
            with Vertical(id="rejudge-foot"):
                self._error = ErrorBox()
                yield self._error
                self._confirm = Picker([
                    Option("Start re-judging", "go", style=OK),
                    Option("Cancel", "cancel"),
                ])
                yield self._confirm
                yield KeyHints([
                    ("↑↓", "move"), ("tab", "field"), ("⏎", "start"),
                    ("esc", "cancel"),
                ])

    def on_mount(self) -> None:
        self._model.set_options(_fix_model_options(self.app.registry, self._judge_slot))
        self._workers.value = str(self._derived_workers())
        self._apply_remembered()
        self._paint_scope()

    # ── Options ───────────────────────────────────────────────────────────────

    def _chosen_slot(self) -> tuple[str, str, str] | None:
        option = self._model.current
        return option.value if option and option.value else None

    def _typed_batch(self) -> int:
        try:
            return max(1, int(self._batch.value))
        except (ValueError, AttributeError):
            return _rejudge_rows_default()

    def _derived_workers(self) -> int:
        """What this model's rate limit affords at the chosen batch size."""
        slot = self._chosen_slot()
        if slot is None:
            return 1
        choice = resolve_choice(self.app.registry, *slot)
        return choice.preset.workers_for(
            self._typed_batch(), tok_per_row=config.TOKEN_EST_MIN_PER_ROW * 2
        )

    def _apply_remembered(self) -> None:
        """
        Prefill from this model's stored settings, when there are any.

        A remembered model keeps its rows and workers figures rather than the
        rate-limit derivation — the derivation is a guess, and the stored
        numbers are what the user last chose to run with.
        """
        slot = self._chosen_slot()
        if slot is None:
            return
        from ...data.app_prefs import remembered_judge_settings

        remembered = remembered_judge_settings(slot)
        if remembered is None:
            return
        self._batch.value = str(remembered["max_rows"])
        self._workers.value = str(remembered["workers"])

    def _paint_scope(self) -> None:
        panel = self.query_one("#rejudge-scope", Static)
        slot = self._chosen_slot()
        batch = self._typed_batch()
        lines: list[str] = []
        if slot is not None:
            choice = resolve_choice(self.app.registry, *slot)
            lines.append(
                f"[{MUTED}]Limits[/]    "
                f"{escape_markup(choice.preset.describe_limits(batch, tok_per_row=config.TOKEN_EST_MIN_PER_ROW * 2))}"
            )
        panel.update("\n".join(lines))

    # ── Events ────────────────────────────────────────────────────────────────

    def on_picker_highlighted(self, event: Picker.Highlighted) -> None:
        if event.picker is self._model:
            self._apply_remembered()
            self._workers.value = str(self._derived_workers())
            self._paint_scope()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input is self._batch.input:
            self._paint_scope()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input is self._batch.input:
            self._workers.focus_input()
            return
        self._start()

    def on_picker_selected(self, event: Picker.Selected) -> None:
        if event.picker is self._confirm:
            if event.option.value == "go":
                self._start()
            else:
                self.dismiss(None)
            return
        if event.picker is self._model:
            self._batch.focus_input()

    # ── Confirming ────────────────────────────────────────────────────────────

    def _start(self) -> None:
        self._error.clear()
        slot = self._chosen_slot()
        if slot is None:
            self._error.show("Pick a model to re-judge with.", "cannot start")
            return
        try:
            workers = max(1, int(self._workers.value))
        except ValueError:
            self._error.show("Workers must be a number.", "cannot start")
            return
        try:
            rows = self._typed_batch()
        except (ValueError, AttributeError):
            self._error.show("Rows per request must be a number.", "cannot start")
            return

        from ...data.app_prefs import remember_judge_settings

        remember_judge_settings(
            slot, max_rows=rows, workers=workers,
            remember="remember" in self._remember.selected_values(),
        )
        self.dismiss((slot, rows, workers))

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


def _rejudge_rows_default() -> int:
    """
    The rows/request a re-judge form opens with: 20, not the Judge ceiling.

    A re-judge re-asks rows the file already reviewed once, with neighbours
    attached in chain mode — a smaller request keeps the second verdict
    comparable to the first evaluation's sections and leaves room for the
    injected context, while 100 (the Judge batching ceiling) made the form's
    opening number a trap for exactly the runs that most need the smaller
    size.
    """
    return 20


class RowFixModal(ModalScreen["tuple | None"]):
    """
    One flagged row, in a single popup (UET_UX §6 Fix one row).

    Clicking a row opens this instead of a read-only detail: the translation is
    editable in place, `Apply` writes it by hand (→ Fixed automatically), and
    `Fix it` hands off to the model popup. Both close this and act on the same
    row — no screen change, one panel.

    Returns `("edit", text)`, `("fix", None)`, or `None`.
    """

    BINDINGS = [
        Binding("escape", "dismiss_none", "cancel", show=False),
        Binding("ctrl+f", "fix_it", "fix it", show=False),
    ]

    DEFAULT_CSS = """
    RowFixModal .modal { width: 88; height: auto; max-height: 95%; }
    RowFixModal #row-fix-body { height: auto; max-height: 1fr; }
    RowFixModal #row-fix-body Label { margin-top: 1; }
    RowFixModal #row-fix-body .panel { padding: 0 1; border: round #2b323f; }
    RowFixModal #row-foot { dock: bottom; height: auto; }
    RowFixModal #row-actions { height: auto; border-top: solid #2b323f; }
    """

    def __init__(
        self,
        row: int,
        source: str,
        translation: str,
        comment: str = "",
        issue=None,
        before: str = "",
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._row = row
        self._source = source
        self._translation = translation
        self._comment = comment
        self._issue = issue
        self._before = before

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            title = f"Row {self._row}"
            if self._issue is not None:
                title += f"  ·  {self._issue.label}"
            yield Label(title, classes="title")
            # The body fills the space between the title and the docked action
            # bar, and scrolls only when the window is too short. The actions and
            # hints are docked to the bottom, so they are always visible.
            with VerticalScroll(id="row-fix-body"):
                yield Label("Source", classes="muted")
                yield Static(escape_markup(self._source or "(empty)"), classes="panel")
                if self._before:
                    yield Label("Before fix", classes="error")
                    yield Static(escape_markup(self._before), classes="panel")
                if self._comment:
                    yield Label("Why it was flagged", classes="warn")
                    yield Static(escape_markup(self._comment), classes="panel")
                self._editor = LabeledTextArea(
                    "Translation",
                    value=self._translation,
                    hint="edit and apply to fix by hand, or use Fix it for a model",
                    min_lines=2,
                    max_lines=8,
                )
                yield self._editor
            with Vertical(id="row-foot"):
                self._actions = Picker([
                    Option("Apply edit (mark Fixed)", "apply", style=OK),
                    Option("Fix it with a model…", "fix", style=ACCENT),
                    Option("Cancel", "cancel"),
                ], id="row-actions")
                yield self._actions
                yield KeyHints([
                    ("↑↓", "move"), ("⏎", "choose"), ("ctrl+j", "new line"),
                    ("esc", "cancel"),
                ])

    def on_mount(self) -> None:
        self._editor.focus_input()
        try:
            area = self._editor.area
            area.move_cursor(area.document.end)
        except Exception:
            pass

    def on_paste_area_submitted(self, _event: PasteArea.Submitted) -> None:
        # Enter in the text box applies the hand edit — the common case.
        self._apply()

    def on_picker_selected(self, event: Picker.Selected) -> None:
        if event.option.value == "apply":
            self._apply()
        elif event.option.value == "fix":
            self.dismiss(("fix", None))
        else:
            self.dismiss(None)

    def _apply(self) -> None:
        self.dismiss(("edit", self._editor.value))

    def action_fix_it(self) -> None:
        self.dismiss(("fix", None))

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


# ── Step 1: model (shared Model Picker) ───────────────────────────────────────

class JudgeModelScreen(JudgeWizardScreen):
    """
    Step 1 — the judge model, via the shared Model Picker.

    Quick/Full is gone (UET_UX §6). The model is still deliberately first and
    never pre-filled: the score a Judge produces only means something alongside
    the name of the model that produced it, so choosing one is the act that
    starts an evaluation (FR-102 / AC-107). A Model Group may be chosen here too.
    """

    step_full = 1
    step_quick = 1
    breadcrumb = "Judge › Model"

    BINDINGS = [Binding("right", "focus_next_col", "next column", show=False)]

    def compose_body(self) -> ComposeResult:
        yield Label("Judge model", classes="title")
        yield Label(
            "Picked fresh every session — a remembered judge would quietly "
            "change what a score means.",
            classes="hint",
        )
        yield Label("")
        self._picker = ModelPicker(id="judge-model-picker")
        yield self._picker

    def screen_keys(self) -> list[tuple[str, str]]:
        return [("↑↓", "move"), ("←→", "column"), ("⏎", "confirm"), ("esc", "cancel")]

    def on_screen_ready(self) -> None:
        # Judging on everything is the default; the Criteria step narrows it.
        self.draft.setdefault("criteria", list(CRITERIA))
        self.draft.setdefault("style_prompt", self.draft.get("style_prompt", ""))
        self._picker.load(self.app.registry)

    def action_focus_next_col(self) -> None:
        self._picker.action_focus_next_col()

    def action_back(self) -> None:
        if not self._picker.focus_prev_col():
            self.app.go_home()

    def on_model_picker_chosen(self, event: ModelPicker.Chosen) -> None:
        event.stop()
        choice = event.choice
        self.draft.update(
            provider_id=choice.provider_id,
            connection_id=choice.connection_id,
            model_id=choice.model_id,
            combo_id=choice.combo.id if choice.is_combo else "",
        )
        from .file_engine import FileEngineScreen
        self.app.push_screen(FileEngineScreen(
            title="Select a spreadsheet to review",
            on_accept=self._file_engine_accepted,
            next_screen=lambda: JudgeColumnsScreen(),
            existing_path=self.draft.get("path", ""),
            sort_translated_first=True,
        ))

    def _file_engine_accepted(self, path: str, engine: str) -> None:
        if path != self.draft.get("path"):
            # A different file invalidates the columns picked for the old one.
            for key in ("src_col", "dst_col", "ctx_col"):
                self.draft.pop(key, None)
        self.draft["path"] = path
        self.draft["fitting_engine"] = engine


def _reasoning_note(model) -> str:
    """
    What this model will do about reasoning, stated once here.

    A Judge session does not offer an override — it follows the model's own
    default — so the only honest place to say what that means is where the model
    is chosen.
    """
    if model is None or not model.supports_reasoning:
        return ""
    control = model.reasoning_control
    if control == "always_on":
        return " · reasoning always on"
    if control == "toggle":
        return f" · reasoning {'on' if model.reasoning_default else 'off'} by default"
    if control == "effort":
        return " · reasoning effort from the model's own default"
    return ""


# ── Step 3: criteria ──────────────────────────────────────────────────────────

class JudgeCriteriaScreen(JudgeWizardScreen):
    """
    What to judge on — an evaluation's equivalent of the operation mode.

    All four are ticked to begin with, because "review everything" is the
    default question. Untick to narrow: a prompt that asks about style will
    report style problems, so a run that only cares about placeholders is both
    cheaper and quieter for asking about less (FR-103).

    `context` is decided later, when a context column is or is not picked, so
    it is offered here and dropped automatically at run time if the file has no
    such column (AC-102). Saying that plainly beats hiding the row and leaving
    someone to wonder where the criterion went.
    """

    step_full = 3
    step_quick = 3            # Criteria comes after File & Columns now
    breadcrumb = "Judge › Criteria"
    title_text = "What should the model judge?"

    def compose_body(self) -> ComposeResult:
        yield Label(self.title_text, classes="title")
        yield Label(
            "Only the ticked criteria are mentioned in the prompt at all.",
            classes="hint",
        )
        yield Label("")
        self.picker = Picker(multi_select=True)
        yield self.picker
        yield Label("")
        yield Static("", id="judge-criteria-note", classes="hint")
        self._error = ErrorBox()
        yield self._error

    def compose_above_footer(self) -> ComposeResult:
        # A one-row button bar, so a click commits it. `click_activates=False`
        # here meant the first click only moved the cursor and a second was
        # needed to do anything — right for a radio group, wrong for a button.
        self._confirm = Picker(
            [Option("Confirm and next", "next", style=OK)], id="confirm-bar"
        )
        yield self._confirm

    def screen_keys(self) -> list[tuple[str, str]]:
        return [("↑↓", "move"), ("space", "toggle"), ("⏎", "confirm"),
                ("←", "back"), ("esc", "cancel")]

    def on_screen_ready(self) -> None:
        from ...fitting.profiles import get_profile
        has_profile = get_profile(self.draft.get("fitting_engine", "general") or "general") is not None
        visible_criteria = [c for c in CRITERIA if c != "fitting" or has_profile]
        chosen = set(self.draft.get("criteria") or visible_criteria)
        self.picker.set_options([
            Option(CRITERION_LABELS[name], name, selected=name in chosen)
            for name in visible_criteria
        ])
        self.picker.focus()
        self._note()

    def _note(self) -> None:
        # Both caveats stated up front, because both change what the score means.
        self.query_one("#judge-criteria-note", Static).update(
            "The context criterion needs a context column; without one it is "
            "dropped from the prompt automatically.\n"
            "Style is judged objectively: with no style prompt for this file, the "
            "model infers the register from the text and reports only "
            "inconsistency, wrong register for the kind of line, or wording that "
            "reads as translated — never its own preference."
        )

    def on_picker_toggled(self, event: Picker.Toggled) -> None:
        self._error.clear()

    def on_picker_selected(self, event: Picker.Selected) -> None:
        chosen = self.picker.selected_values()
        if not chosen:
            self._error.show(
                "Tick at least one criterion — there is nothing to judge otherwise.",
                "cannot continue",
            )
            return
        self.draft["criteria"] = list(chosen)
        self.app.push_screen(JudgeStyleScreen())


# ── Step 4: languages and style ───────────────────────────────────────────────

class JudgeStyleScreen(JudgeWizardScreen):
    """
    The languages and the style the translation was *supposed* to have.

    Judge needs both for the same reason a translator does: "is this too formal"
    has no answer until something says what formality was asked for, and
    "mistranslated from what into what" is easier to judge when both languages
    are named rather than inferred from the characters.

    Same preset list as the translate wizard — the built-ins, whatever the user
    has saved, `+ New custom prompt` — because a file translated with
    `game-H-ID` should be reviewed against `game-H-ID`, and making that a
    retyped paragraph rather than one keystroke is how the two drift apart.

    `No style prompt` leads the list and is a legitimate answer, not a fallback:
    the register is then inferred from the text and judged for consistency,
    fitness and naturalness, without imposing taste (`judge/prompts.py`).
    """

    step_full = 4
    step_quick = 4            # Language & style step
    breadcrumb = "Judge › Style"

    NEW = "__new__"
    NONE = "__none__"

    BINDINGS = [Binding("d", "delete", "delete", show=False)]

    def compose_body(self) -> ComposeResult:
        yield Label("Languages and style", classes="title")
        yield Label(
            "What the translation was asked to be — the yardstick the review "
            "measures against.",
            classes="hint",
        )
        yield Label("")
        self._src = LabeledInput(
            "Source language",
            value=self.draft.get("src_lang", ""),
            default="auto",
            hint="auto = let the model work it out from the text",
        )
        self._tgt = LabeledInput(
            "Target language",
            value=self.draft.get("tgt_lang", ""),
            default="Indonesian",
        )
        yield self._src
        yield self._tgt
        yield Label("")
        yield Label("Style the translation should match", classes="muted")
        self.picker = Picker()
        yield self.picker
        yield Label("")
        yield Static("", id="judge-style-preview", classes="panel")
        self._custom = LabeledInput(
            "Custom prompt",
            hint="The register the translation should be in: tone, what to keep "
                 "verbatim, what to leave alone.",
        )
        self._custom.display = False
        yield self._custom
        self._name = LabeledInput(
            "Save as preset",
            hint="Name it to keep it for next time, or leave empty to use it just this once.",
        )
        self._name.display = False
        yield self._name

    def compose_above_footer(self) -> ComposeResult:
        # A button, not a radio group: one click commits (see JudgeCriteriaScreen).
        self._confirm = Picker(
            [Option("Confirm and next", "next", style=OK)], id="confirm-bar"
        )
        yield self._confirm

    def screen_keys(self) -> list[tuple[str, str]]:
        if getattr(self, "_custom", None) is not None and self._custom.display:
            return [("⏎", "next / save"), ("esc", "back to list")]
        keys = [("↑↓", "move"), ("tab", "next field"), ("⏎", "confirm")]
        if getattr(self, "_saved", None):
            keys.append(("d", "delete saved"))
        return keys + [("←", "back"), ("esc", "cancel")]

    def on_screen_ready(self) -> None:
        self._reload()
        self._src.focus_input()

    def _commit(self) -> None:
        """Languages are required before the style choice means anything."""
        if not self._tgt.value.strip():
            self.notify_status("Target language is required.", ERROR)
            self._tgt.focus_input()
            return
        self.draft["src_lang"] = self._src.value
        self.draft["tgt_lang"] = self._tgt.value
        self.app.push_screen(JudgePacingScreen())

    def _reload(self, keep_index: bool = False) -> None:
        self._saved = load_user_presets()
        options = [
            Option(
                "No style prompt (infer register objectively)",
                self.NONE,
                hint="recommended when no style prompt was specified",
                style=OK,
            )
        ]
        options += [Option(preset["label"], preset["key"]) for preset in PRESETS]
        options += [
            Option(preset["label"], preset["key"], hint="saved", style=OK)
            for preset in self._saved
        ]
        options.append(Option("+ New custom prompt", self.NEW, style=ACCENT))
        self.picker.set_options(options, keep_index=keep_index)

        if not keep_index:
            self._preselect(options)

        self.refresh_keys()
        self._show_prompt()

    def _preselect(self, options: list[Option]) -> None:
        """
        Land on the style this file was translated with, when that is known.

        The post-run door carries the finished job's own style prompt, so a review
        of a `game-H-ID` file starts on `game-H-ID` rather than on whatever
        happens to be first. Matched by key when there is one and by prompt text
        otherwise, because a job only remembers the text.
        """
        wanted_key = (self.draft.get("style_key") or "").strip()
        wanted_prompt = (self.draft.get("style_prompt") or "").strip()
        for index, option in enumerate(options):
            if wanted_key and option.value == wanted_key:
                self.picker.index = index
                return
        if wanted_prompt:
            for index, option in enumerate(options):
                preset = self._find(option.value)
                if preset and preset["prompt"].strip() == wanted_prompt:
                    self.picker.index = index
                    return
        elif self.draft.get("from_job"):
            # The job it came from had no style either; say so rather than
            # silently proposing the first preset as though it did.
            for index, option in enumerate(options):
                if option.value == self.NONE:
                    self.picker.index = index
                    return

    def _find(self, key) -> dict | None:
        return next((p for p in PRESETS + self._saved if p["key"] == key), None)

    def on_picker_highlighted(self, event: Picker.Highlighted) -> None:
        self._show_prompt()

    def _show_prompt(self) -> None:
        option = self.picker.current
        if not option or option.value == self.NONE:
            self.query_one("#judge-style-preview", Static).update(
                "[dim]No style prompt — model infers register from text and judges "
                "consistency, fitness and naturalness objectively.[/]"
            )
            return
        preset = self._find(option.value)
        self.query_one("#judge-style-preview", Static).update(
            escape_markup(preset["prompt"]) if preset
            else "[dim]You will type your own prompt.[/]"
        )

    def on_picker_selected(self, event: Picker.Selected) -> None:
        if event.picker is getattr(self, "_confirm", None):
            # The footer's confirm commits whatever the list is on, so the step
            # can be finished from the language fields without walking back.
            self._accept_current()
            return
        if event.option.value == self.NONE:
            self.draft["style_key"] = ""
            self.draft["style_prompt"] = ""
            self._commit()
            return
        if event.option.value == self.NEW:
            self._custom.display = True
            self._custom.focus_input()
            self.refresh_keys()
            return
        preset = self._find(event.option.value)
        if preset is None:
            return
        self.draft["style_key"] = event.option.value
        self.draft["style_prompt"] = preset["prompt"]
        self._commit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input is self._src.input:
            self._tgt.focus_input()
            return
        if event.input is self._tgt.input:
            self.picker.focus()
            return
        if event.input is self._custom.input:
            if not self._custom.value.strip():
                self.notify_status("Type a prompt, or press Esc to go back.", WARN)
                return
            self._name.display = True
            self._name.focus_input()
            return
        self._accept_custom()

    def _accept_current(self) -> None:
        """Commit whichever style the list is currently on."""
        if self._custom.display:
            self._accept_custom()
            return
        option = self.picker.current
        if option is None or option.value == self.NONE:
            self.draft["style_key"] = ""
            self.draft["style_prompt"] = ""
            self._commit()
            return
        if option.value == self.NEW:
            self._custom.display = True
            self._custom.focus_input()
            self.refresh_keys()
            return
        preset = self._find(option.value)
        if preset is None:
            return
        self.draft["style_key"] = option.value
        self.draft["style_prompt"] = preset["prompt"]
        self._commit()

    def _accept_custom(self) -> None:
        prompt = self._custom.value.strip()
        name = self._name.value.strip()
        if name:
            try:
                saved = save_user_preset(name, prompt)
            except OSError as exc:
                # Stay put rather than move on quietly: they asked for it to be
                # kept, and the next screen would take the message with it.
                self.notify_status(f"Could not save the preset: {exc}", ERROR)
                return
            self.draft["style_key"] = saved["key"]
        self.draft["style_prompt"] = prompt
        self._commit()

    def action_delete(self) -> None:
        option = self.picker.current
        if option is None or option.value in (self.NEW, self.NONE):
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
        if self._custom.display:
            self._custom.display = False
            self._name.display = False
            self._custom.input.value = ""
            self._name.input.value = ""
            self.picker.focus()
            self.notify_status("")
            self.refresh_keys()
            return
        super().action_cancel()


# ── Step 2: columns ───────────────────────────────────────────────────────────

class JudgeColumnsScreen(JudgeWizardScreen):
    """
    Click the grid and the columns fill themselves in: source, then
    translation, then the optional context column.

    Same widget and same gesture as the translate wizard's column step, because
    reading the sheet is how anyone decides which column is which. The fields
    below stay authoritative and still accept `C`, `3` or `D15` typed by hand.
    """

    step_full = 2
    step_quick = 2
    breadcrumb = "Judge › Columns"

    _active = "src"

    def compose_body(self) -> ComposeResult:
        yield Label("Columns and range", classes="title")
        yield Label(os.path.basename(self.draft.get("path", "")), classes="hint")
        self._preview = ColumnPreview(id="judge-preview", classes="panel")
        yield self._preview
        yield Label("")

        self._src = LabeledInput(
            "Source column",
            value=_letter(self.draft.get("src_col")),
            default="A",
            hint="Click a column above, or type C, 3, or D15 (column + start row).",
        )
        self._dst = LabeledInput(
            "Translation column", value=_letter(self.draft.get("dst_col")), default="B"
        )
        self._ctx = LabeledInput(
            "Context column (optional)",
            value=_letter(self.draft.get("ctx_col")),
            hint="Without one, the context criterion is dropped.",
        )
        self._start = LabeledInput(
            "Start row", value=str(self.draft.get("start_row") or 2), default="2"
        )
        self._rows = LabeledInput(
            "Rows to evaluate",
            value=str(self.draft.get("num_rows") or 0),
            default="0",
            hint="Enter = every remaining row.",
        )
        yield self._src
        yield self._dst
        yield self._ctx
        yield self._start
        yield self._rows
        yield Label("", id="judge-col-error", classes="error")

    def compose_above_footer(self) -> ComposeResult:
        # A button, not a radio group: one click commits (see JudgeCriteriaScreen).
        self._confirm = Picker(
            [Option("Confirm and next", "next", style=OK)], id="confirm-bar"
        )
        yield self._confirm

    def screen_keys(self) -> list[tuple[str, str]]:
        return [("tab", "next field"), ("⏎", "confirm"), ("←", "back"), ("esc", "cancel")]

    def on_screen_ready(self) -> None:
        self._debounce = None
        self._grid_key: tuple | None = None
        self._grid: tuple = ([], [], [], 0)
        self._active = "src"
        self._auto_detect()
        self._fields()[self._active].focus_input(scroll_visible=False)
        self._update_preview()

    def _auto_detect(self) -> None:
        """Fill Source/Translation from the header unless already chosen."""
        if self.draft.get("src_col") or self.draft.get("dst_col"):
            return
        path = self.draft.get("path", "")
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
                "Source/Translation detected from the header — change them if "
                "the guess is wrong.", MUTED
            )
        else:
            self.queue_note(
                "Could not detect the columns — click the grid or type a letter.",
                WARN
            )

    # ── Which field a click fills ─────────────────────────────────────────────

    def _fields(self) -> dict[str, LabeledInput]:
        return {"src": self._src, "dst": self._dst, "ctx": self._ctx}

    def _next_tag(self, tag: str) -> str:
        if tag in ROLE_ORDER:
            return ROLE_ORDER[(ROLE_ORDER.index(tag) + 1) % len(ROLE_ORDER)]
        return ROLE_ORDER[0]

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
        # Advance here rather than leaving it to the focus event, so the caption
        # never lags a frame behind the field it is naming. Focus follows, but
        # without scrolling: on a short terminal, revealing the next field would
        # push the grid off the top and leave nothing to click.
        self._active = self._next_tag(self._active)
        fields[self._active].focus_input(scroll_visible=False)
        self._update_preview()

    @staticmethod
    def _cell_ref(field: LabeledInput, column: int) -> str:
        """Swap the column, but keep a start row that was already typed."""
        _column, row = ExcelReader.parse_cell_ref(field.input.value)
        letter = ExcelReader.col_letter(column)
        return f"{letter}{row}" if row else letter

    # ── Form ──────────────────────────────────────────────────────────────────

    def on_input_changed(self, event: Input.Changed) -> None:
        if self._debounce is not None:
            self._debounce.stop()
        self._debounce = self.set_timer(0.12, self._update_preview)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        order = [self._src, self._dst, self._ctx, self._start, self._rows]
        for index, field in enumerate(order):
            if event.input is field.input and index + 1 < len(order):
                order[index + 1].focus_input()
                return
        self._accept()

    def on_picker_selected(self, event: Picker.Selected) -> None:
        self._accept()

    def _parse(self) -> tuple[int, int, int, int, int] | None:
        src_col, src_row = ExcelReader.parse_cell_ref(self._src.value)
        dst_col, dst_row = ExcelReader.parse_cell_ref(self._dst.value)
        if src_col is None:
            src_col = _as_column(self._src.value) or None
        if dst_col is None:
            dst_col = _as_column(self._dst.value) or None
        if not src_col or not dst_col:
            return None
        ctx_col = _as_column(self._ctx.value)
        # Never raise from here: this runs on a debounce timer, and an exception
        # in a timer callback has nowhere to go but through the app.
        start = src_row or dst_row or _int(self._start.value, 2)
        num_rows = _int(self._rows.value, 0)
        return src_col, dst_col, ctx_col, max(1, start), max(0, num_rows)

    def _update_preview(self) -> None:
        parsed = self._parse()
        roles: dict[int, ColumnRole] = {}
        if parsed is not None:
            src_col, dst_col, ctx_col, start, _rows = parsed
            for tag, column in (("src", src_col), ("dst", dst_col), ("ctx", ctx_col)):
                if column:
                    roles.setdefault(column, ROLES[tag])
        else:
            start = max(1, _int(self._start.value, 2))

        path = self.draft.get("path", "")
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
        error = self.query_one("#judge-col-error", Label)
        for label, box in (("Start row", self._start), ("Rows to evaluate", self._rows)):
            if _int_or_none(box.input.value) is None:
                error.update(f"{label} must be a number.")
                box.focus_input()
                return

        parsed = self._parse()
        if parsed is None:
            error.update("A source and a translation column are required.")
            return
        src_col, dst_col, ctx_col, start, num_rows = parsed
        if src_col == dst_col:
            error.update(
                "Source and translation cannot be the same column — there would "
                "be nothing to compare."
            )
            return
        try:
            total = ExcelReader.row_count(self.draft["path"])
        except Exception as exc:
            error.update(f"Cannot read file: {exc}")
            return

        available = max(0, total - start + 1)
        if num_rows <= 0:
            num_rows = available
        num_rows = min(num_rows, available)
        if num_rows <= 0:
            error.update("That range contains no rows.")
            return

        self.draft.update(
            src_col=src_col, dst_col=dst_col, ctx_col=ctx_col,
            start_row=start, num_rows=num_rows,
        )
        # New order (UET_UX §6): file & columns → criteria → language & style →
        # processing → review.
        self.app.push_screen(JudgeCriteriaScreen())


# ── Step 5: processing (shared form) ──────────────────────────────────────────

class JudgePacingScreen(JudgeWizardScreen):
    """
    The shared Processing form (UET_UX §6/§7), same as Translate.

    Rows/request and workers are recommended from the model's (or group's) rate
    limit; Enter accepts, typing overrides with a warning but never a block. A
    review carries the source *and* the translation for every row, so the token
    cap can still split a request smaller than the rows/request figure at run
    time — the form's recommendation is a target, and the estimator has the
    final say on the actual section size.
    """

    step_full = 5
    step_quick = 5
    breadcrumb = "Judge › Processing"

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
            rows_value=self.draft.get("max_rows_per_request"),
            workers_value=self.draft.get("n_workers"),
            advanced=True,
            draft=self.draft,
            id="judge-processing",
        )
        yield self._form
        yield Label("", id="judge-pacing-error", classes="error")

    def compose_above_footer(self) -> ComposeResult:
        self._confirm = Picker(
            [Option("Confirm and review", "next", style=OK)], id="confirm-bar"
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
        if event.picker is getattr(self, "_confirm", None):
            self._accept()

    def _accept(self) -> None:
        values = self._form.values()
        rows = max(config.MIN_MAX_ROWS, min(config.MAX_MAX_ROWS, values.rows_per_request))
        self.draft.update(
            preset=self._preset,
            max_rows_per_request=rows,
            n_workers=values.workers,
            workers_override=values.manual_override,
            processing_mode=values.processing_mode,
            write_mode=values.write_mode,
            anti_desync=values.anti_desync,
            skip_filled=values.skip_filled,
            debug_log=values.debug_log,
            raw_log=values.raw_log,
        )
        self.app.push_screen(JudgePreviewScreen())


def _judge_rows_default() -> int:
    """Rows per Judge request the wizard opens with: the Settings value."""
    try:
        return app_prefs.max_rows_per_request()
    except Exception:
        return config.DEFAULT_MAX_ROWS_PER_REQUEST


def _target_from(draft: dict) -> JudgeTarget:
    """
    A `JudgeTarget` from the draft, for the two screens that need to price a plan.

    Shared rather than duplicated: the pacing step and the review step must split
    the file the same way, or the worker count would be derived from a different
    request shape than the one actually sent.
    """
    return JudgeTarget(
        path=draft["path"],
        src_col=draft["src_col"],
        dst_col=draft["dst_col"],
        ctx_col=draft.get("ctx_col", 0),
        start_row=draft.get("start_row", 2),
        num_rows=draft.get("num_rows", 0),
        src_lang=draft.get("src_lang", ""),
        tgt_lang=draft.get("tgt_lang", ""),
        style_prompt=draft.get("style_prompt", ""),
        custom_placeholders=draft.get("custom_placeholders", ""),
    )


def _letter(column) -> str:
    try:
        return ExcelReader.col_letter(int(column)) if column else ""
    except Exception:
        return ""


def _int(text: str, default: int) -> int:
    try:
        return int((text or "").strip() or default)
    except ValueError:
        return default


def _int_or_none(text: str) -> int | None:
    text = (text or "").strip()
    if not text:
        return 0
    try:
        return int(text)
    except ValueError:
        return None


# ── Final step: review and start ──────────────────────────────────────────────

class JudgePreviewScreen(JudgeWizardScreen, MenuScreen):
    """
    What is about to be sent, what it will cost, and one decision.

    The estimate is computed without sending anything, which is the whole point
    of it: a large evaluation should be a choice made before the tokens are
    spent, not a surprise afterwards (FR-106 / AC-104).
    """

    step_full = FULL_STEPS
    step_quick = QUICK_STEPS
    breadcrumb = "Judge › Review"
    title_text = "Review and start"

    def compose_extra(self) -> ComposeResult:
        yield Label("")
        yield Static("", id="judge-plan", classes="panel")
        self._error = ErrorBox()
        yield self._error

    def screen_keys(self) -> list[tuple[str, str]]:
        return [("↑↓", "move"), ("⏎", "select"), ("←", "back"), ("esc", "cancel")]

    def on_screen_ready(self) -> None:
        super().on_screen_ready()
        self._plan: EvaluationPlan | None = None
        self._compute()

    def build_options(self) -> list[Option]:
        return [
            Option("Start Evaluation", "start", style=OK),
            Option("Edit", "edit", hint="change a part without walking back"),
            Option("Cancel", "cancel"),
        ]

    # ── The plan ──────────────────────────────────────────────────────────────

    def _target(self) -> JudgeTarget:
        return _target_from(self.draft)

    def _criteria(self) -> list[str]:
        return list(self.draft.get("criteria") or CRITERIA)

    def _rows_cap(self) -> int | None:
        """
        Rows per request, when the pacing step named one.

        `None` in Quick mode, which never asks — the runner then reads the saved
        Settings value, exactly as before.
        """
        return self.draft.get("max_rows_per_request") or None

    def _compute(self) -> None:
        panel = self.query_one("#judge-plan", Static)
        target = self._target()
        try:
            runner = JudgeRunner(
                target=target,
                criteria=self._criteria(),
                max_rows=self._rows_cap(),
                fitting_profile=self.draft.get("fitting_engine", "general"),
            )
            plan = runner.plan()
        except Exception as exc:
            self._plan = None
            panel.update(escape_markup(f"Cannot read that range: {exc}"))
            return
        self._plan = plan

        draft = self.draft
        criteria = runner.criteria          # context already dropped if unavailable
        dropped = set(self._criteria()) - set(criteria)
        pacing = self._pacing(plan)
        # The label, not the third id: for a combo that id is a uuid, and
        # "Model 2171a408d40b…" tells nobody which combo is about to run.
        choice = resolve_choice(
            self.app.registry,
            draft.get("provider_id", ""),
            draft.get("connection_id", ""),
            draft.get("model_id", ""),
        )
        model_name = (
            f"Group · {choice.label}" if choice.is_combo else choice.label
        )
        lines = [
            f"[{MUTED}]File[/]        {escape_markup(os.path.basename(draft['path']))}",
            f"[{MUTED}]Columns[/]     "
            f"{ExcelReader.col_letter(draft['src_col'])} → "
            f"{ExcelReader.col_letter(draft['dst_col'])}"
            + (f" · context {ExcelReader.col_letter(draft['ctx_col'])}"
               if draft.get("ctx_col") else "")
            + f", rows {draft['start_row']}–"
              f"{draft['start_row'] + draft['num_rows'] - 1} ({draft['num_rows']:,})",
            f"[{MUTED}]Model[/]       {escape_markup(model_name)}",
            f"[{MUTED}]Languages[/]   "
            f"{escape_markup(draft.get('src_lang') or 'auto')} → "
            f"{escape_markup(draft.get('tgt_lang') or '(not stated)')}",
            f"[{MUTED}]Style[/]       {escape_markup(self._style_label())}",
            f"[{MUTED}]Criteria[/]    {escape_markup(', '.join(criteria))}",
            f"[{MUTED}]Requests[/]    {plan.request_count} · "
            f"~{plan.estimated_tokens:,} tokens estimated · "
            f"each under {plan.max_tokens:,}"
            + (f" · up to {self._rows_cap()} row(s)" if self._rows_cap() else ""),
        ]
        if pacing:
            lines.append(f"[{MUTED}]Pacing[/]      {escape_markup(pacing)}")
        if dropped:
            lines.append(
                f"[{WARN}]Dropped[/]     {escape_markup(', '.join(sorted(dropped)))} "
                f"— no context column was picked"
            )
        if plan.prejudged:
            lines.append(
                f"[{MUTED}]Free[/]        {len(plan.prejudged)} row(s) are already "
                f"known bad (empty or {config.FAIL_MARKER}) and cost no request"
            )
        if plan.warned:
            lines.append(
                f"[{WARN}]Large[/]       above the "
                f"{config.TOKEN_WARN_THRESHOLD:,} token threshold — you will be "
                f"asked to confirm"
            )
        lines.append(
            f"[{MUTED}]Nothing is written to the file unless you approve a fix.[/]"
        )
        panel.update("\n".join(lines))

    def _style_label(self) -> str:
        """
        The style the review will judge against, named rather than pasted.

        A preset is named; a one-off prompt is quoted short; no style at all says
        what happens instead, because "none" alone would read as a gap rather
        than a deliberate choice.
        """
        draft = self.draft
        prompt = (draft.get("style_prompt") or "").strip()
        if not prompt:
            return "none — register inferred from the text, judged objectively"
        key = (draft.get("style_key") or "").strip()
        if key:
            from ...data.presets import find_preset

            preset = find_preset(key)
            if preset:
                return preset["label"]
        return f"custom · {truncate(prompt, 60)}"

    def _pacing(self, plan: EvaluationPlan) -> str:
        """
        The worker count these requests earn against the model's rate limit.

        Derived from the biggest section the estimator produced, because that is
        the request that has to fit — and shown here rather than only in the
        Activity Panel, so a token-bound limit is visible before the tokens are
        spent rather than after. For a combo it is the combo's own limit, which is
        the only rate limit a combo run is paced by.
        """
        draft = self.draft
        choice = resolve_choice(
            self.app.registry,
            draft.get("provider_id", ""),
            draft.get("connection_id", ""),
            draft.get("model_id", ""),
        )
        if choice.entry is None and not choice.is_combo:
            return ""
        if not plan.sections:
            return ""
        # Full mode's pacing step may have replaced the model's own limit; that is
        # the limit the run will use, so it is the one to describe.
        preset = draft.get("preset") or choice.preset
        rows = max(len(section) for section in plan.sections)
        tokens = max(estimator.estimate_total(section) for section in plan.sections)
        line = preset.describe_limits(rows, tokens_per_request=tokens)
        if choice.is_combo and choice.combo is not None:
            line = (
                f"combo {choice.combo.label} · "
                f"{choice.combo.strategy.replace('_', '-')} · {line}"
            )
        workers = draft.get("n_workers")
        if workers:
            # Full mode chose this, so the review must show what will run rather
            # than what the arithmetic suggests.
            line += f" · running {int(workers)} worker(s)"
        return line

    # ── Starting ──────────────────────────────────────────────────────────────

    def handle_choice(self, option: Option) -> None:
        if option.value == "edit":
            self.app.push_screen(_JudgeEditScreen())
        elif option.value == "cancel":
            self.app.go_home()
        else:
            self._start()

    def _start(self) -> None:
        plan = self._plan
        if plan is None or not plan.total_rows:
            self._error.show(
                "That range contains no translated rows.", "nothing to evaluate"
            )
            return
        if plan.warned:
            self.app.push_screen(ConfirmEstimateModal(plan), self._confirmed)
            return
        self._confirmed(True)

    def _confirmed(self, go: bool | None) -> None:
        if not go:
            return
        draft = self.draft
        self.app.push_screen(JudgeRunScreen(
            target=self._target(),
            model_slot=(draft["provider_id"], draft["connection_id"], draft["model_id"]),
            criteria=self._criteria(),
            # Full mode's pacing step, or None in Quick where nothing was asked.
            max_rows=self._rows_cap(),
            workers=draft.get("n_workers") or None,
            preset=draft.get("preset"),
            # The execution mode, write mode and logging flags the Processing
            # step wrote into the draft. Carried explicitly because the run
            # screen has no wizard draft (UET_UX bug #2/#3).
            processing={
                "processing_mode": draft.get("processing_mode"),
                "write_mode": draft.get("write_mode", "safe"),
                "anti_desync": draft.get("anti_desync", "auto"),
                "skip_filled": draft.get("skip_filled", False),
                "debug_log": draft.get("debug_log", False),
                "raw_log": draft.get("raw_log", False),
            },
            fitting_profile=draft.get("fitting_engine", "general"),
        ))


class _JudgeEditScreen(JudgeWizardScreen, MenuScreen):
    """
    Review's Edit for Judge: jump to the part to change (UET_UX §6 Review).

    Walking forward from the chosen step lands back on Review with the rest of
    the draft intact, the same contract the Translate review's Edit gives.
    """

    step_full = FULL_STEPS
    step_quick = QUICK_STEPS
    breadcrumb = "Judge › Edit"
    title_text = "Edit which part?"

    def build_options(self) -> list[Option]:
        return [
            Option("Model", "model"),
            Option("File & columns", "file"),
            Option("Criteria", "criteria"),
            Option("Language & style", "style"),
            Option("Processing", "processing"),
            Option("Back to review", "back"),
        ]

    def handle_choice(self, option: Option) -> None:
        target = {
            "model": JudgeModelScreen,
            "file": JudgeColumnsScreen,
            "criteria": JudgeCriteriaScreen,
            "style": JudgeStyleScreen,
            "processing": JudgePacingScreen,
        }.get(option.value)
        if target is None:
            self.app.pop_screen()
            return
        self.app.push_screen(target())


class ConfirmEstimateModal(ModalScreen[bool]):
    """
    The confirmation a large evaluation gets (FR-106 / AC-104).

    It states the cost in the units being spent — requests and tokens — because
    "this is a big job" is not a number anyone can decide on.
    """

    BINDINGS = [Binding("escape", "dismiss_none", "cancel", show=False)]

    def __init__(self, plan: EvaluationPlan, **kwargs) -> None:
        super().__init__(**kwargs)
        self._plan = plan

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label("That is a large evaluation", classes="title")
            yield Static(escape_markup(
                f"{self._plan.total_rows:,} row(s) in "
                f"{self._plan.request_count} request(s)\n"
                f"~{self._plan.estimated_tokens:,} tokens estimated\n\n"
                f"The estimate is a heuristic (about four characters to a "
                f"token, plus 25% headroom), so the real figure will differ. "
                f"Each request stays under {self._plan.max_tokens:,} tokens."
            ), classes="panel")
            self.picker = Picker([
                Option("Start anyway", "go", style=OK),
                Option("Go back and narrow the range", "back"),
            ])
            yield self.picker
            yield KeyHints([("↑↓", "move"), ("⏎", "select"), ("esc", "cancel")])

    def on_mount(self) -> None:
        self.picker.focus()

    def on_picker_selected(self, event: Picker.Selected) -> None:
        self.dismiss(event.option.value == "go")

    def action_dismiss_none(self) -> None:
        self.dismiss(False)


# ── The evaluation itself ─────────────────────────────────────────────────────

class FilterBar(Widget):
    """
    A row of clickable chips: `all`, `problems`, then one per error type.

    A bar rather than a dropdown because the counts are the useful part — seeing
    `placeholder 31` next to `style 2` tells you what is wrong with the file
    before you have read a single row, and a dropdown would hide exactly that
    behind a click.

    Chips are click targets and `tab` walks them, so it works the same whether
    you are reaching for the mouse or not. Painted as `rich.text.Text`: an error
    type is a fixed vocabulary, but the grid it filters is not, and one markup
    accident in this app is enough.
    """

    ALLOW_SELECT = False

    hover_chip = reactive(-1)

    class ChipClicked(Message):
        def __init__(self, key: str) -> None:
            super().__init__()
            self.key = key

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._chips: list[tuple[str, str, int]] = []
        self._active = ""
        # (start, end, key) in content cells, so a click knows what it hit.
        self._spans: list[tuple[int, int, str]] = []

    def set_chips(self, chips: list[tuple[str, str, int]], active: str) -> None:
        self._chips = list(chips)
        self._active = active
        # Drop the old hit boxes rather than let them survive into the frame
        # before the repaint: a click landing there would select the chip that
        # used to be in that spot.
        self._spans = []
        self.hover_chip = -1
        self.refresh(layout=True)

    def watch_hover_chip(self, _old: int, _new: int) -> None:
        self.refresh()

    # ── Mouse ─────────────────────────────────────────────────────────────────

    def _chip_at(self, event: events.MouseEvent) -> int:
        offset = event.get_content_offset(self)
        if offset is None or offset.y != 0:
            return -1
        for index, (start, end, _key) in enumerate(self._spans):
            if start <= offset.x < end:
                return index
        return -1

    def on_mouse_move(self, event: events.MouseMove) -> None:
        self.hover_chip = self._chip_at(event)

    def on_leave(self, _event: events.Leave) -> None:
        self.hover_chip = -1

    def on_click(self, event: events.Click) -> None:
        index = self._chip_at(event)
        if index == -1:
            return
        event.stop()
        # One selection per double-click: chips look like buttons and get
        # double-clicked like buttons.
        if event.chain == 1:
            self.post_message(self.ChipClicked(self._spans[index][2]))

    # ── Render ────────────────────────────────────────────────────────────────

    def get_content_height(self, container, viewport, width: int) -> int:
        return 1 if self._chips else 0

    def render_line(self, y: int) -> Strip:
        if y != 0:
            return Strip.blank(self.size.width)
        return strip_line(self._text(), self.size.width, self.rich_style)

    def _text(self) -> Text:
        text = Text()
        spans: list[tuple[int, int, str]] = []
        if not self._chips:
            self._spans = []
            return text

        text.append("show ", style=MUTED)
        for key, label, count in self._chips:
            active = key == self._active
            hot = len(spans) == self.hover_chip
            style = self._chip_style(key, active, hot)
            start = text.cell_len
            text.append(f" {label} {count} ", style=style)
            spans.append((start, text.cell_len, key))
            text.append(" ")
        self._spans = spans
        return text

    @staticmethod
    def _chip_style(key: str, active: bool, hot: bool) -> str:
        # Colour carries meaning here: the selected chip is reversed so it reads
        # as pressed, `fixed` is green because it is the one chip that reports
        # success rather than a problem, and a severity keeps the same colour it
        # has everywhere else in the screen.
        base = {
            "all": MUTED,
            "problems": WARN,
            "fixed": OK,
        }.get(key)
        if base is None and key.startswith("sev:"):
            base = _SEVERITY_STYLES.get(key[4:], WARN)
        base = base or WARN
        if active:
            return f"bold {base} reverse"
        if hot:
            return f"bold {base}"
        return base


class GridPane(VerticalScroll):
    """
    The scroller around the report grid.

    While the evaluation is live the grid grows underneath it, so wheeling has to
    mean what it means on the run screen: scrolling back stops the view being
    dragged along, and reaching the bottom starts following again. Without this,
    reading a row that has just been flagged is impossible — the next section
    inserts rows above it and the line walks off the screen.
    """

    def on_mouse_scroll_up(self, _event: events.MouseScrollUp) -> None:
        detach = getattr(self.screen, "_detach_live_follow", None)
        if detach is not None:
            detach()

    def on_mouse_scroll_down(self, _event: events.MouseScrollDown) -> None:
        attach = getattr(self.screen, "_attach_live_follow", None)
        if attach is not None and self.scroll_target_y >= self.max_scroll_y - 3:
            attach()


class JudgeRunScreen(BaseScreen):
    """
    The grid, the filter bar, the panel, and the actions.

    Layout mirrors the run screen on purpose: a wide grid on the left, a narrow
    rail on the right. What the rail holds differs — a score and a distribution
    rather than a progress bar — but the Activity Panel in it is the same widget
    the translation run uses, because Judge's evaluation and Fix All go through
    the same workers (FR-207 / AC-205).

    The grid is `CompareGrid`. Before a fix it shows one column, the AI's
    comment; after a fix is applied it grows a second, so the original and the
    repaired text sit side by side and the change is readable at a glance
    instead of having to be remembered.

    Above it sits a filter bar: `all`, `problems`, one chip per severity, then one
    per error type that actually occurred. Clicking a chip narrows the grid to
    those rows, and filtering is a *view* — `CompareGrid.set_row_filter` hides
    rows without discarding them, so nothing is re-read and no request is
    repeated. Fix All then offers what is on screen, which is what makes "fix
    just the critical placeholder problems" a single gesture.

    Everything a flagged row needs happens here, without leaving: fix one row,
    fix everything the filter is showing, re-judge, edit by hand, or revert a
    repair that made things worse. Sending someone to another screen and back for
    the middle of that loop is what makes a review tedious, and a tedious review
    does not get finished.

    Both long operations report as they go, in two places. The rail carries the
    counters and the last few results; the grid *fills in* — a row appears with
    its verdict under it the moment its section is parsed, so a file of a few
    thousand rows is readable while it is being reviewed rather than only after.
    The rows are merged in sheet order rather than appended as they arrive,
    because sections finish out of order and a grid that reads 41, 12, 27 cannot
    be scanned.

    Merging in sheet order is also why the view is pinned. New rows can appear
    *above* what is on screen, so without a pin a reader would be pushed down the
    report by rows they are not looking at. Following (the default) keeps the
    newest line in view; scrolling back with the wheel, the arrows or PageUp
    detaches it and keeps the row that was at the top exactly where it was, and
    coming back to the bottom follows again — the same gesture the translate feed
    has.
    """

    breadcrumb = "Judge · results"

    BINDINGS = [
        Binding("a", "toggle_workers", "workers", show=False),
        Binding("e", "toggle_events", "events", show=False),
        Binding("enter", "open_row", "open row", show=False),
        # `f` and `c` are context-sensitive: while a batch runs they mean
        # "follow the latest row" and "stop", which are the only two things worth
        # doing mid-run; once the report exists they mean "fix all" and
        # "confirm & save" (bug #2/#3). One key each, dispatched on state, so the
        # live screen and the review screen never fight over a binding.
        Binding("f", "f_key", "follow / fix all", show=False),
        Binding("c", "c_key", "stop / confirm", show=False),
        Binding("C", "c_key", "stop / confirm", show=False),
        Binding("g", "judge_again", "judge again", show=False),
        Binding("G", "judge_again_fixed", "re-judge fixed", show=False),
        Binding("R", "retry_unfinished", "retry unfinished", show=False),
        Binding("r", "revert_row", "revert", show=False),
        Binding("s", "summary", "summary", show=False),
        Binding("tab", "next_filter", "next filter", show=False),
        Binding("shift+tab", "prev_filter", "previous filter", show=False),
        Binding("escape", "leave", "back", show=False),
        # The cursor moves; the pane follows it. Paging is the way to travel a
        # long report, which is why it keeps the raw scroll.
        Binding("up,k", "move_cursor(-1)", "previous row", show=False),
        Binding("down,j", "move_cursor(1)", "next row", show=False),
        Binding("pageup", "scroll_grid(-12)", "page up", show=False),
        Binding("pagedown", "scroll_grid(12)", "page down", show=False),
        Binding("home", "scroll_grid_home", "top", show=False),
        Binding("end", "scroll_grid_end", "bottom", show=False),
    ]

    def __init__(
        self,
        target: JudgeTarget,
        model_slot: tuple[str, str, str],
        criteria: list[str],
        max_rows: int | None = None,
        workers: int | None = None,
        preset=None,
        processing: dict | None = None,
        fitting_profile: str = "general",
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._target = target
        self._model_slot = model_slot
        self._criteria = list(criteria)
        self._fitting_profile = fitting_profile or "general"
        # What Full mode's pacing step chose. All three are None in Quick, where
        # the model's own preset and the saved row cap are used instead.
        self._max_rows = int(max_rows) if max_rows else None
        self._chosen_workers = int(workers) if workers else None
        self._chosen_preset = preset
        # A snapshot of the processing choices made on the setup wizard's
        # Processing step. `JudgeRunScreen` is a plain screen with no wizard
        # draft of its own, so the execution mode, write mode and logging flags
        # are handed to it explicitly at construction (UET_UX bug #2/#3). Read
        # once here so a later Settings change cannot make the running job
        # disagree with what Review showed.
        self._processing: dict = dict(processing or {})
        self._stop = threading.Event()
        # ── Force-cancel (double `c`) ─────────────────────────────────────────
        # The first `c` sets the stop flag and lets the request in flight
        # finish — its result is real work and is kept. A second `c` while the
        # first is still pending SEVERS the request: every client the executors
        # hold gets its socket closed, the blocked read raises in the worker
        # thread, and the run ends now instead of after the current output.
        self._cancel_requested_at = 0.0    # when the first `c` landed
        self._aborted_run = False
        self._report: JudgeReport | None = None
        self._runner: JudgeRunner | None = None
        self._workers: dict[int, object] = {}
        # UI-only hold so a worker's Success / Failed / Retrying state stays
        # readable ~1s instead of being coalesced away between repaints
        # (bug #5/#6/#7). Never holds the runner.
        self._ledger = WorkerLedger()
        # Last events-rail line and its repeat count, so an identical provider
        # error collapses to one counted line instead of a wall (bug #8).
        self._last_log_body = ""
        self._last_log_count = 0
        self._events: list[str] = []
        # A worker-backed operation is in flight: the evaluation, or a Fix All.
        # Named `_busy_batch` rather than `_running` because `MessagePump` owns
        # `self._running` and sets it True the moment a screen mounts — a screen
        # that keeps its own flag under that name reads as permanently busy and
        # every action guarded by it silently does nothing.
        self._busy_batch = False
        self._busy_row = 0
        # The row every single-row action works on, as a sheet row number rather
        # than an index into a list that filtering reshuffles. -1/0 = none.
        self._cursor_row = 0
        # "all" | "problems" | one error type | one severity | "fixed".
        # Starts on problems: after an evaluation those are the rows anyone
        # opened this screen to read.
        self._filter = "problems"
        # row -> text before the applied fix, so the comparison column has
        # something to compare against.
        self._before: dict[int, str] = {}
        # row -> what it was flagged as before the fix was applied. Kept so a
        # revert can put the flag back: the problem is in the file again, and a
        # report that pretends otherwise describes a file that no longer exists.
        self._fixed_issue: dict[int, object] = {}
        # The grading this run asked for. Read once at construction so a Settings
        # change mid-evaluation cannot make the report disagree with the prompts
        # that produced it.
        self._grading_mode = app_prefs.judge_grading_mode()
        # Live feeds, bounded. Painted from the rail's timer rather than per
        # event, so a fast section costs one repaint (NFR-201).
        # Rows, as a set: a row reported twice — which a retry can do — must not
        # push the figure past its own total.
        self._reviewed_rows: set[int] = set()
        self._flagged_live: list[tuple[int, object]] = []
        self._fixed_live: list[tuple[int, str, bool]] = []
        # Rows a Fix All has settled, whether they were rewritten, left alone or
        # failed. The denominator of the repair bar.
        self._fix_seen: set[int] = set()
        self._fix_plan: "FixAllPlan | None" = None
        # A batch re-judge (G) in flight, and its live figures. Kept apart from
        # the Fix All ones because both can be the operation `_busy_batch`
        # describes, and a rail that showed "Repairing" during a re-check would
        # be lying about what is happening.
        self._rejudging = False
        self._rejudge_rows: list[int] = []
        self._rejudge_seen: set[int] = set()
        self._rejudge_flagged: list[tuple[int, object]] = []
        # Every row a re-judge has covered, single (g) or batch (G), so the
        # filter bar can offer exactly the second verdicts as their own view:
        # "which of my repairs did the judge look at again, and how did they
        # come out" is a question the severity chips cannot answer.
        self._rejudged_rows: set[int] = set()
        # Rows that PASSED a re-judge (layer 2 — verdict persistence): the
        # judge's verdicts fluctuate run to run on identical text (~9% flip
        # rate measured), so a row that already survived one re-judge is only
        # re-flaggable by a CRITICAL finding; a minor/style re-flag is treated
        # as sampling noise and ignored. Cleared only when the row is written
        # again (its text changed, so the old verdict no longer describes it).
        self._verdict_stable: set[int] = set()
        # Fix cycle counter per row (layer 3 — convergence guard): a row that
        # has been fixed this many times and is STILL flagged leaves the
        # automatic loop and is marked for human review instead, so the
        # fix→re-judge cycle provably terminates.
        self._fix_cycles: dict[int, int] = {}
        # Rows whose fix budget is exhausted — flagged "needs manual review"
        # and excluded from Fix All / G by default.
        self._settled_rows: set[int] = set()
        # Cached (rows, tokens) of the largest section — see _section_shape.
        # None until measured once; invalidated whenever the file is written.
        self._section_shape_cache: tuple[int, int] | None = None
        # Requests finished and requests planned, for the second progress line.
        # A row count alone cannot show a run sitting on one slow request.
        self._sections_done = 0
        self._sections_total = 0
        # The live grid: rows whose verdict has landed but which the timer has
        # not painted yet, and everything it has painted so far.
        self._pending_live: list[tuple[int, object]] = []
        self._live_rows: dict[int, str] = {}          # row -> translation
        self._live_notes: dict[int, str] = {}         # row -> verdict line
        self._live_styles: dict[int, str] = {}        # row -> that line's colour
        self._by_row: dict[int, tuple[str, str]] = {}  # row -> (source, translation)
        # Follow the newest reviewed row, like the translate feed. Scrolling back
        # detaches it; reaching the bottom again re-attaches. Only meaningful
        # while the evaluation is live — once the report exists the grid stops
        # growing and the cursor owns the view.
        self._follow_live = True
        # True while a temporary provider warning card is on screen, so the next
        # successful request can replace it with a recovery note.
        self._issue_active = False
        # ── Lifecycle & timing (UET_UX bug #4/#5/#7) ──────────────────────────
        # An explicit state rather than the single `_busy_batch` flag, so the UI,
        # the animation and the ETA can each say exactly what is happening:
        #   idle → running → (retrying) → completed | paused | failed | cancelled
        self._run_state = "idle"
        # A monotonic animation tick that advances on every repaint while work is
        # in flight, INDEPENDENTLY of whether a new row landed. `grid._tick` only
        # moves while a column is PENDING, and Judge's columns are always `done`,
        # so the worker panel's spinner was being handed tick=0 forever and sat
        # frozen on one frame — which is what read as a hang (bug #5).
        self._anim_tick = 0
        # Wall-clock accounting for the live progress feed (bug #7). `_elapsed`
        # accumulates only while running, so a pause does not keep inflating it.
        self._started_at = 0.0
        self._elapsed = 0.0
        # The active operation's own context, so the notice card and status show
        # the model actually working rather than the Judge model by default
        # (bug #1/#9). (label, model_slot).
        self._op_label = "Judge"
        self._op_slot = self._model_slot
        # Rows a paused-run Retry is re-sending, for reference/diagnostics.
        self._retry_rows: list[int] = []

    # ── Layout ────────────────────────────────────────────────────────────────

    def compose_body(self) -> ComposeResult:
        self.grid = CompareGrid(id="judge-grid")
        self.activity = ActivityPanel(title="Workers", id="judge-activity")
        self.activity_header = ActivityHeader(self.activity, id="judge-activity-header")
        self.activity.header_widget = self.activity_header
        self.events = EventsPanel(title="Events", id="judge-events")
        self.events_header = EventsHeader(self.events, id="judge-events-header")
        self.events.header_widget = self.events_header
        self.rail = Static("", id="judge-rail")
        self.indicator = SmallIndicator(id="judge-indicator")
        # Chips rather than a dropdown: every filter and its row count is
        # visible at once, which is the fastest way to see *what kind* of
        # problem this file has before deciding what to fix.
        self.filters = FilterBar(id="judge-filters")

        yield self.filters
        with Horizontal(id="judge-columns"):
            with GridPane(id="judge-grid-pane"):
                yield self.grid
            with Vertical(id="judge-rail-pane"):
                # Right rail: the fixed state block on top (score, rows,
                # severity), then Worker above Events, each with a fixed
                # header above its own bounded scroll area — a long run never
                # grows the rail without bound.
                yield self.rail
                yield self.activity_header
                with VerticalScroll(id="judge-workers-scroll"):
                    yield self.activity
                yield self.events_header
                with VerticalScroll(id="judge-events-scroll"):
                    yield self.events
                # Fatal errors render here — the bottom of the rail, not below
                # the grid. An ErrorBox that grows under the grid shoves the
                # whole report out of frame, which is worse than the error: the
                # user loses the work they were reading to read a message they
                # can do nothing about until the operation ends. In the rail it
                # is bounded (max-height) and scrollable, and the grid never
                # moves a line.
                self._error = ErrorBox(id="judge-error")
                yield self._error
        yield self.indicator
        # Provider trouble shows here — a small card, bottom-right, not a
        # full-width wall of duplicated errors. The raw errors still go to the
        # events rail and the log.
        self._notice = ProviderNotice()
        yield self._notice

    DEFAULT_CSS = f"""
    #judge-filters {{ height: auto; }}
    #judge-columns {{ height: 1fr; }}
    #judge-grid-pane {{ width: 1fr; padding-right: 1; }}
    #judge-rail-pane {{
        width: 34;
        border-left: solid {MUTED};
        padding-left: 1;
        height: 1fr;
    }}
    #judge-rail {{ height: auto; }}
    #judge-activity-header {{ height: 1; }}
    #judge-workers-scroll {{ height: 1fr; width: 1fr; scrollbar-size: 1 1; }}
    #judge-events-header {{ height: 1; }}
    #judge-events-scroll {{ height: 1fr; width: 1fr; scrollbar-size: 1 1; }}
    #judge-activity {{ height: auto; width: 1fr; }}
    #judge-events {{ height: auto; width: 1fr; }}
    #judge-error {{
        height: auto;
        max-height: 8;
        width: 1fr;
        overflow-y: auto;
        scrollbar-size: 1 1;
        border-top: solid {MUTED};
        margin-top: 0;
    }}
    #judge-indicator {{ height: auto; padding: 0 1; }}
    """

    def screen_keys(self) -> list[tuple[str, str]]:
        if self._busy_batch:
            # `end` is offered because scrolling back is now possible mid-run and
            # a key that resumes following has to be findable.
            keys = [("↑↓", "scroll"), ("a", "workers"), ("e", "events")]
            if not self._follow_live:
                keys.append(("f", "follow latest"))
            if self._stop.is_set():
                # A graceful cancel is pending: the second press severs the
                # request in flight, and that is worth saying in the footer.
                return keys + [("c", "force stop")]
            return keys + [("c", "stop")]
        if self._report is None:
            return [("↑↓", "move"), ("←", "back")]
        keys = [
            ("↑↓", "row"), ("⏎", "open"), ("tab", "filter"),
            ("a", "workers"), ("e", "events"),
        ]
        row = self._cursor_row
        flagged = row in self._report.issues
        repaired = row in self._before

        if self._run_state == "paused" and self._unfinished_rows():
            # The provider gave out mid-run: the first offer is to pick up where
            # it stopped, not to review a report that is not finished (bug #4).
            keys.append(("R", f"retry {len(self._unfinished_rows())} unfinished"))
        # `f` fixes everything the filter is showing. A single row is fixed by
        # opening it (Enter / click) and choosing there.
        if self._report.problem_rows:
            keys.append(("f", self._fix_all_label()))
        if flagged or repaired:
            keys.append(("g", "judge again"))
        if self._before:
            # Every applied fix, re-checked through the batch path. Offered
            # whenever anything has been written, not only when the cursor
            # happens to sit on a repaired row.
            keys.append(("G", f"re-judge {len(self._before)} fixed"))
        if repaired:
            keys.append(("r", "revert"))
        return keys + [("c", "confirm & save"), ("s", "summary"), ("←", "back")]

    def _fix_all_label(self) -> str:
        """What `F` will offer, named after the filter it will draw from."""
        if self._filter in ("all", "problems"):
            return "fix all…"
        if self._filter == "fixed":
            # The repaired rows that are still flagged — a second pass over the
            # ones the first repair did not settle.
            return "fix flagged…"
        scope = self._filter[4:] if self._filter.startswith("sev:") else self._filter
        return f"fix {scope}…"

    def on_screen_ready(self) -> None:
        self._app = self.app
        self.app.on_shutdown_hook = self._stop.set
        self._expected_rows = self._count_rows()
        # (source, translation) per row, read once with the count. The live grid
        # needs the source text for a row the moment its verdict lands, and
        # reading the sheet per row — or per repaint — would be absurd.
        self._by_row = {
            row: (source, translation)
            for row, source, translation in self._expected_rows
        }
        self.set_interval(0.12, self._repaint)
        # The same columns the live flush will use, so the header does not change
        # shape the moment the first row lands.
        self.grid.set_sources([], self._columns())
        self._run_state = "running"
        self._started_at = time.monotonic()
        self._paint_rail()
        self._evaluate()

    def _count_rows(self) -> list:
        """
        The rows this evaluation will review, read once.

        Only the length is used — it is the denominator of the live progress
        figure — but reading it here keeps that figure off the repaint path,
        where it would cost a sheet read eight times a second.
        """
        try:
            runner = JudgeRunner(target=self._target, criteria=self._criteria)
            rows, _contexts = runner.load_rows()
            return rows
        except Exception:
            return []

    def _columns(self) -> list[Column]:
        """
        Two columns only: Source and Translation (UET_UX §6 grid).

        Before/after is not shown as extra columns any more — it lives in the
        row popup. The grid stays two columns wide and readable, and status
        (severity, fixed) is carried by the row's colour instead.

        While the evaluation is still running the translation column is filled
        from the live map instead of the report, which does not exist yet — that
        is what lets the grid grow row by row rather than appearing all at once.
        """
        report = self._report
        if report is None:
            live = Column(
                letter=COMMENT_LETTER,
                label="TRANSLATION",
                where=self._model_id(),
                state="done",
                rows=dict(self._live_rows),
                # The grid only paints flagged rows mid-run; this stat is what
                # proves the run itself is moving when the grid is quiet.
                stat=f"{len(self._reviewed_rows):,} reviewed · "
                     f"{len(self._flagged_live):,} flagged",
            )
            return [live]

        translation = Column(
            letter=COMMENT_LETTER,
            label="TRANSLATION",
            where=self._model_id(),
            state="done",
            rows={row: dst for row, _src, dst in report.rows},
        )
        translation.stat = self._grid_stat(report)
        return [translation]

    def _row_styles(self) -> dict[int, str]:
        """
        Row colours for the grid: severity for a flagged row, green for a
        repaired one, default for a clean row (UET_UX §6).

        A repaired row keeps its place in whatever filter it was flagged under —
        it is just green now instead of red — *unless a re-judge flagged it
        again*: the row is back in `report.issues`, and the colour must say so.
        Green on a row the judge just re-flagged would read as "fixed and
        holding" when it is exactly the opposite, so `report.issues` wins over
        `_before` when both apply.
        """
        report = self._report
        styles: dict[int, str] = {}
        if report is None:
            return styles
        for row in self._before:
            if row not in report.issues:
                # Repaired and not re-flagged — green. A row that IS in
                # report.issues is painted below with its new severity.
                styles[row] = OK
        for row, issue in report.issues.items():
            styles[row] = _SEVERITY_STYLES.get(issue.severity, WARN)
        return styles

    def _grid_stat(self, report: JudgeReport) -> str:
        shown = len(self.grid.visible_rows) if hasattr(self, "grid") else len(report.rows)
        if self._filter == "all":
            return f"{len(report.rows)} row(s)"
        if self._filter == "problems":
            return f"{len(report.problem_rows)} flagged of {len(report.rows)}"
        if self._filter == "fixed":
            return f"{len(self._before)} row(s) repaired"
        if self._filter == "rejudged":
            return f"{len(self._rejudged_rows)} row(s) re-judged"
        return f"{shown} row(s) with {self._filter}"

    def _model_id(self) -> str:
        """
        What to *call* the judge on screen.

        The label rather than the raw third id, because for a combo that id is a
        uuid: "Combo · premium-coding" is what the user picked it by, and a hex
        string in the rail would be unreadable.
        """
        choice = self._choice()
        if choice.is_combo:
            return f"Group · {choice.label}"
        return choice.label

    # ── Repaint ───────────────────────────────────────────────────────────────

    def _repaint(self) -> None:
        """
        One timer for the whole screen, as on the run screen: the grid's
        spinner, the panel's durations, the rail and the live grid all advance
        from it, so a burst of worker updates costs one paint (NFR-201).

        The rail is repainted here rather than only from `_log`. It was the
        latter, which is why the progress bar sat at 0%: `_reviewed` was counting
        correctly all along, but nothing asked the rail to draw it again unless a
        retry or a failure happened to log a line.
        """
        try:
            self._flush_live()
            if self._busy_batch:
                # Animation and the running clock advance every tick while work
                # is in flight, whether or not a row landed (bug #5/#7). The
                # activity panel's spinner is driven from `_anim_tick`, not the
                # grid's tick, because Judge's grid columns are always `done` and
                # `grid._tick` therefore never moves.
                self._anim_tick += 1
                if self._run_state == "running" and self._started_at:
                    self._elapsed = time.monotonic() - self._started_at
                self._paint_rail()
            self.grid.tick()
            self.activity.set_statuses(self._ledger.statuses(), self._anim_tick)
        except Exception:
            pass

    def _flush_live(self) -> None:
        """
        Move whatever the runner has reported since the last tick into the grid.

        Rows are added rather than the grid rebuilt, and the note map is handed
        over whole: `CompareGrid.add_rows` keeps them in sheet order, so a report
        stays scannable even though the sections that produced it finished out of
        order.

        That ordering is also why the view has to be pinned. Merging by row number
        means a section that finishes late inserts lines *above* what is on screen,
        so a reader who has scrolled back would be pushed down the report by rows
        they are not looking at. While following, the view sticks to the newest
        line; while detached, it sticks to the row it was showing.
        """
        if not self._pending_live:
            return
        landed = self._pending_live
        self._pending_live = []

        pane = self._grid_pane()
        anchor = self._anchor_row(pane)

        fresh: list[tuple[int, str]] = []
        for row, issue in landed:
            source, translation = self._by_row.get(row, ("", ""))
            self._live_rows[row] = translation
            # While the evaluation runs, only the rows the judge actually
            # FLAGGED enter the grid. The clean ones are real progress — the
            # bar and the counters move — but painting thousands of passing
            # rows buries the handful a reviewer came to read, and a clean
            # file looks exactly like a stalled one when both show a wall of
            # green-less lines. Everything reviewed is still kept in memory
            # (`_live_rows`), and the finished report shows the full file
            # through the filter bar as always.
            if issue is None:
                continue
            fresh.append((row, source))
            self._live_notes[row] = f"{issue.label} · {issue.comment}"
            self._live_styles[row] = _SEVERITY_STYLES.get(
                getattr(issue, "severity", ""), WARN
            )

        # One rebuild for all three changes rather than three.
        with self.grid.batched():
            self.grid.set_columns(self._columns())
            self.grid.add_rows(fresh)
            self.grid.set_notes(dict(self._live_notes), dict(self._live_styles))

        self._restore_anchor(pane, anchor)

    # ── Keeping the view still while rows land ────────────────────────────────

    def _grid_pane(self) -> VerticalScroll | None:
        try:
            return self.query_one("#judge-grid-pane", VerticalScroll)
        except Exception:
            return None

    def _anchor_row(self, pane: "VerticalScroll | None") -> tuple[int, int] | None:
        """
        `(row, offset within the view)` for the row at the top of the screen.

        `None` while following, where the answer is simply "the end". Recorded
        *before* a rebuild so `_restore_anchor` can put the same row back in the
        same place afterwards.
        """
        if pane is None or self._follow_live:
            return None
        top = int(pane.scroll_target_y)
        for probe in range(top, top + max(1, pane.size.height)):
            row = self.grid.row_at_line(probe)
            if row:
                return row, probe - top
        return None

    def _restore_anchor(
        self, pane: "VerticalScroll | None", anchor: tuple[int, int] | None
    ) -> None:
        """Put the anchored row back where it was, or stay at the end."""
        if pane is None:
            return
        if self._follow_live:
            # Scroll *after* the rebuild's layout has settled. Doing it inline
            # scrolls to the pre-rebuild bottom, then the new rows lay out and
            # the view lurches again — the "jump/flicker" when a result lands
            # (bug #4). `call_after_refresh` runs once layout is current, so the
            # view moves exactly once, to the real end.
            self.call_after_refresh(self._scroll_pane_end, pane)
            return
        if anchor is None:
            return
        row, offset = anchor
        y = self.grid.line_of_row(row)
        if y < 0:
            return
        pane.scroll_to(y=max(0, y - offset), animate=False)

    @staticmethod
    def _scroll_pane_end(pane) -> None:
        """Scroll a pane to the bottom, ignoring a pane torn down mid-run."""
        try:
            pane.scroll_end(animate=False)
        except Exception:
            pass

    def _paint_rail(self) -> None:
        lines = [f"[bold {ACCENT}]Judge[/]", escape_markup(self._model_id()[:30])]
        where = ", ".join(self._criteria)
        lines.append(f"[{MUTED}]{escape_markup(where)}[/]")
        if self._grading_mode == "severity":
            lines.append(f"[{MUTED}]graded by severity[/]")
        # While a Fix All / Rejudge is running on a different model, name it so
        # the rail is never lying about which model is doing the current work
        # (bug #1/#9). Only shown when the operation actually differs.
        if self._busy_batch and self._op_label != "Judge":
            active = self._op_model_id()
            if active and active != self._model_id():
                lines.append(
                    f"[{ACCENT}]{escape_markup(self._op_label)}[/] "
                    f"[{MUTED}]{escape_markup(active[:24])}[/]"
                )
        lines.append("")

        report = self._report
        if report is None:
            # Mid-evaluation: the live feed *is* the content, because a score
            # cannot exist yet and an empty rail reads as a hang.
            lines += self._live_review_lines()
        elif self._busy_batch:
            # Mid-Fix All, or mid batch re-judge: the report is on screen behind
            # this, so the rail carries whichever operation is running.
            lines += (
                self._live_rejudge_lines() if self._rejudging
                else self._live_fix_lines()
            )
        else:
            summary = report.summary
            lines += [
                f"[bold {ACCENT}]Score[/]",
                f"{summary.percent:.1f}%  ({summary.evaluated - summary.problem_rows}"
                f"/{summary.evaluated} ok)",
                "",
                f"[bold {ACCENT}]Rows[/]",
                f"[{ERROR}]problems[/]  {summary.problem_rows:>6,}",
                f"[{MUTED}]evaluated[/] {summary.evaluated:>6,}",
            ]
            if summary.unevaluated:
                lines.append(f"[{WARN}]unjudged[/]  {summary.unevaluated:>6,}")
            if summary.severity_counts:
                lines += ["", f"[bold {ACCENT}]Severity[/]"]
                for name, count in summary.ranked_severities():
                    lines.append(
                        f"[{_SEVERITY_STYLES.get(name, MUTED)}]{name:<10}[/]"
                        f"{count:>6,}"
                    )
            if summary.distribution:
                lines += ["", f"[bold {ACCENT}]Error types[/]"]
                for name, count in summary.ranked_distribution():
                    lines.append(f"[{MUTED}]{name:<14}[/]{count:>4,}")
            if self._before:
                lines += [
                    "",
                    f"[{OK}]repaired[/]  {len(self._before):>6,}",
                ]
            lines += [
                "",
                f"[{MUTED}]tokens ~{summary.estimated_tokens:,} est · "
                f"{summary.tokens_used:,} used[/]",
            ]

        self._paint_events()
        try:
            self.rail.update("\n".join(lines))
        except Exception:
            pass

    def _paint_events(self) -> None:
        """The Events panel, fed from the same list the rail used to print."""
        try:
            self.events.set_events(self._events[-40:] or [f"[{MUTED}]—[/]"])
        except Exception:
            pass

    @staticmethod
    def _bar(done: int, total: int, width: int = 16) -> str:
        """A `████░░░░ 42%` bar. One place, so both feeds read identically."""
        pct = 0 if total <= 0 else min(100, int(done / total * 100))
        filled = int(width * pct / 100)
        return f"[{ACCENT}]{'█' * filled}{'░' * (width - filled)}[/] {pct}%"

    def _live_review_lines(self) -> list[str]:
        """The evaluation as it happens: how far along, and what it has flagged."""
        total = len(self._plan_rows())
        done = len(self._reviewed_rows)
        lines = [f"[bold {ACCENT}]{self._state_label()}[/]"]
        if total:
            lines.append(self._bar(done, total))
            lines.append(f"{done:,} / {total:,} rows")
        else:
            lines.append(f"{done:,} rows")
        if self._sections_total:
            # The request figure alongside the row one: they answer different
            # questions — how much of the file is done, and how many calls are
            # left — and a row count alone hides a run stuck on one slow request.
            lines.append(
                f"[{MUTED}]request {min(self._sections_done + 1, self._sections_total)}"
                f" of {self._sections_total}[/]"
            )
        lines.append(self._timing_line(done, total))
        lines.append("")
        lines.append(f"[bold {ACCENT}]Flagged so far[/]  {len(self._flagged_live):,}")
        if not self._flagged_live:
            lines.append(f"[{MUTED}]nothing yet[/]")
        for row, issue in self._flagged_live[-8:]:
            severity = getattr(issue, "severity", "")
            style = _SEVERITY_STYLES.get(severity, WARN)
            label = severity or ",".join(getattr(issue, "error_types", []) or [])
            lines.append(f"[{style}]{row:>6}[/] {escape_markup(label)[:18]}")
        return lines

    def _state_label(self, running_word: str = "Reviewing") -> str:
        """
        The word the live rail leads with, driven by the lifecycle state.

        `running_word` lets each operation name its own normal-progress verb —
        "Reviewing", "Repairing", "Re-judging fixes" — while the retry / paused /
        cancelled words stay shared, so every operation shows the same explicit
        lifecycle (bug #4).
        """
        if self._run_state in ("running", "idle", "completed"):
            return running_word
        return {
            "retrying": "Retrying",
            "paused": "Paused",
            "failed": "Failed",
            "cancelled": "Cancelled",
        }.get(self._run_state, running_word)

    def _timing_line(self, done: int, total: int) -> str:
        """
        Elapsed, and an ETA once there is enough throughput to mean anything
        (bug #7). While paused the clock stops rather than counting a wait that
        is not work; before the first row it says so instead of guessing.
        """
        elapsed = self._elapsed
        if self._run_state == "paused":
            return f"[{WARN}]{self._fmt_short(elapsed)} elapsed · paused[/]"
        if done <= 0 or total <= 0:
            return f"[{MUTED}]{self._fmt_short(elapsed)} elapsed · estimating…[/]"
        remaining = (elapsed / done) * max(0, total - done)
        return (f"[{MUTED}]{self._fmt_short(elapsed)} elapsed · "
                f"~{self._fmt_short(remaining)} left[/]")

    @staticmethod
    def _fmt_short(seconds: float) -> str:
        seconds = int(max(0, seconds))
        if seconds < 60:
            return f"{seconds}s"
        if seconds < 3600:
            return f"{seconds // 60}m {seconds % 60:02d}s"
        return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"

    def _live_fix_lines(self) -> list[str]:
        """Fix All as it happens, one repaired row at a time."""
        plan = self._fix_plan
        total = len(plan.rows) if plan else 0
        lines = [f"[bold {ACCENT}]{self._state_label('Repairing')}[/]"]
        done = len(self._fix_seen)
        if total:
            # Rows settled, not rows changed: a batch the model left entirely
            # alone is finished work, and counting only rewrites would leave the
            # bar at 0% on a run that did exactly what it should.
            lines.append(self._bar(done, total))
            lines.append(f"{done:,} / {total:,} rows")
        if plan is not None:
            lines.append(f"[{MUTED}]{escape_markup(plan.model_label[:26])}[/]")
        lines.append(self._timing_line(done, total))
        lines.append("")
        if not self._fixed_live:
            lines.append(f"[{MUTED}]waiting for the first reply…[/]")
        for row, text, changed in self._fixed_live[-8:]:
            if changed:
                lines.append(f"[{OK}]{row:>6}[/] {escape_markup(truncate(text, 20))}")
            else:
                # A row the model declined to change is not a failure, and it
                # must not read like one.
                lines.append(f"[{MUTED}]{row:>6}  left as-is[/]")
        return lines

    def _live_rejudge_lines(self) -> list[str]:
        """
        A batch re-judge as it happens.

        Same shape as the evaluation's feed rather than the repair's, because it
        is the same operation: rows reviewed, and what came back flagged.
        """
        total = len(self._rejudge_rows)
        done = len(self._rejudge_seen)
        lines = [f"[bold {ACCENT}]{self._state_label('Re-judging fixes')}[/]"]
        if total:
            lines.append(self._bar(done, total))
            lines.append(f"{done:,} / {total:,} rows")
        lines.append(self._timing_line(done, total))
        lines.append("")
        lines.append(
            f"[bold {ACCENT}]Still wrong[/]  {len(self._rejudge_flagged):,}"
        )
        if not self._rejudge_flagged:
            lines.append(f"[{MUTED}]nothing yet[/]")
        for row, issue in self._rejudge_flagged[-8:]:
            severity = getattr(issue, "severity", "")
            style = _SEVERITY_STYLES.get(severity, WARN)
            label = severity or ",".join(getattr(issue, "error_types", []) or [])
            lines.append(f"[{style}]{row:>6}[/] {escape_markup(label)[:18]}")
        return lines

    def _plan_rows(self) -> list:
        """Rows this evaluation set out to review, for the progress figure."""
        return getattr(self, "_expected_rows", []) or []

    def _log(self, message: str, style: str = "") -> None:
        body = escape_markup(message)
        # Collapse an identical message repeated back-to-back into one counted
        # line rather than a wall of the same provider error (bug #8). The raw
        # text still reaches the log; this is only the on-screen events rail.
        if body == self._last_log_body and self._events:
            self._last_log_count += 1
            self._events[-1] = f"[{style or MUTED}]{body}  ×{self._last_log_count}[/]"
        else:
            self._last_log_body = body
            self._last_log_count = 1
            self._events.append(f"[{style or MUTED}]{body}[/]")
            del self._events[:-30]
        # While a batch is running the repaint timer already owns the rail and
        # redraws it every tick; painting again here would mean two updaters
        # writing the same widget from different call sites in the same frame,
        # which is a source of the flicker (bug #8). Only paint directly when the
        # timer is not (i.e. between operations).
        if not self._busy_batch:
            self._paint_rail()

    # ── Evaluation ────────────────────────────────────────────────────────────

    def _job(
        self,
        model_slot: tuple[str, str, str] | None = None,
        workers: int | None = None,
    ) -> JobConfig:
        """
        A JobConfig for the pooled path.

        Pacing is derived from the model's own rate limit, at the size of the
        requests Judge actually sends, BUT a manual worker override typed by the
        user is strictly honoured (UET_UX bug #3 fix) — never recalculated or
        quietly capped back down.
        """
        provider_id, connection_id, model_id = model_slot or self._model_slot
        proc = self._processing
        job = JobConfig(
            provider_id=provider_id,
            model_id=model_id,
            connection_id=connection_id,
            src_path=self._target.path,
            out_path=self._target.path,
            src_col=self._target.src_col,
            dst_col=self._target.dst_col,
            ctx_col=self._target.ctx_col,
            start_row=self._target.start_row,
            num_rows=self._target.num_rows,
            src_lang=self._target.src_lang,
            tgt_lang=self._target.tgt_lang,
            style_prompt=self._target.style_prompt,
            custom_placeholders=self._target.custom_placeholders,
            processing_mode=proc.get("processing_mode") or app_prefs.default_processing_mode(),
            write_mode=proc.get("write_mode", "safe"),
            anti_desync=proc.get("anti_desync", "auto"),
            debug_log=proc.get("debug_log", False),
            raw_log=proc.get("raw_log", False),
            log_path=(
                os.path.splitext(self._target.path)[0] + "_judge.log"
                if proc.get("debug_log") or proc.get("raw_log")
                else ""
            ),
        )
        choice = self._choice(model_slot)
        choice.apply_to(job)
        if choice.entry is not None or choice.is_combo:
            preset = choice.preset
            if self._chosen_preset is not None and model_slot is None:
                preset = self._chosen_preset
            rows, tokens = self._section_shape()
            job.batch_size = rows
            job.tok_per_row = max(
                config.TOKEN_EST_MIN_PER_ROW, int(tokens / max(1, rows))
            )
            job.n_workers = preset.workers_for(rows, tokens_per_request=tokens)
            job.wave_delay = preset.wave_delay
            job.batch_delay = round(preset.delay_per_request, 3)
            job.reasoning_enabled = None

        # Manual worker override logic: if the user typed an override during
        # setup or Fix All, it MUST override the derived figure (bug #3 fix).
        target_workers = workers if workers is not None else self._chosen_workers
        if target_workers is not None and target_workers > 0:
            job.n_workers = int(target_workers)

        if job.n_workers <= 1 and job.processing_mode == "parallel":
            job.processing_mode = "sequential"
        job.max_completion_tokens = 4096
        return job

    def _section_shape(self) -> tuple[int, int]:
        """
        (rows, tokens) of the most expensive request Judge will send.

        Measured from the plan the estimator produced rather than assumed, so the
        pacing arithmetic and the auto-split agree on what a request costs. The
        *largest* section is the one that has to fit inside the rate limit; pacing
        for the average would under-count exactly the requests most likely to
        trip it.

        Cached per session: the measurement reads and plans the WHOLE file
        (~0.5-1s), and every operation builds a JobConfig — a single-row
        re-judge paying that per keystroke was pure hidden latency. The shape
        only changes when the file is written (a Fix All / hand edit), which
        invalidates the cache explicitly.
        """
        if self._section_shape_cache is not None:
            return self._section_shape_cache
        fallback = (
            config.DEFAULT_BATCH_SIZE,
            config.DEFAULT_BATCH_SIZE * config.DEFAULT_TOK_PER_ROW * 2,
        )
        try:
            runner = JudgeRunner(
                target=self._target,
                criteria=self._criteria,
                max_rows=self._max_rows,
            )
            plan = runner.plan()
        except Exception:
            return fallback
        if not plan.sections:
            return fallback

        biggest = max(plan.sections, key=estimator.estimate_total)
        rows = max(len(section) for section in plan.sections)
        shape = (max(1, rows), max(1, estimator.estimate_total(biggest)))
        self._section_shape_cache = shape
        return shape

    def _choice(self, model_slot: tuple[str, str, str] | None = None):
        """
        The chosen model-or-combo, resolved once.

        One helper for the pacing, the summary and the pool, so a combo cannot be
        recognised by one of them and missed by another.
        """
        provider_id, connection_id, model_id = model_slot or self._model_slot
        return resolve_choice(self.app.registry, provider_id, connection_id, model_id)

    def _model_entry(self, model_slot: tuple[str, str, str] | None = None):
        return self._choice(model_slot).entry

    def _provider(self, model_slot: tuple[str, str, str] | None = None):
        """The provider serving this choice, or None for a combo (it spans several)."""
        return self._choice(model_slot).provider

    def _make_runner(
        self,
        model_slot: tuple[str, str, str] | None = None,
        workers: int | None = None,
        on_row=None,
        on_fix=None,
        on_section=None,
    ) -> JudgeRunner:
        """
        A runner wired to this screen's callbacks.

        `model_slot`/`workers` are how Fix All borrows the machinery with a
        different model and pacing; `on_row`/`on_fix`/`on_section` are the live
        feeds, passed only by the caller that has somewhere to show them.

        `registry` is passed as well as `provider` because a combo has no single
        provider — the runner asks `build_pool`, which needs the registry to
        resolve the combo's members.
        """
        return JudgeRunner(
            target=self._target,
            job=self._job(model_slot, workers),
            provider=self._provider(model_slot),
            registry=self.app.registry,
            criteria=self._criteria,
            # Full mode's row cap. `None` in Quick, where the runner reads the
            # saved Settings value as it always has.
            max_rows=self._max_rows,
            grading_mode=self._grading_mode,
            on_status=lambda status: self._app.call_from_thread(
                self._on_status, status
            ),
            on_event=lambda event: self._app.call_from_thread(self._on_event, event),
            on_notice=lambda kind, msg: self._app.call_from_thread(
                self._on_notice, kind, msg
            ),
            on_row=on_row,
            on_fix=on_fix,
            on_section=on_section,
            stop_event=self._stop,
            fitting_profile=self._fitting_profile,
        )

    @work(thread=True)
    def _evaluate(self) -> None:
        app = self._app
        self._busy_batch = True
        app.call_from_thread(self._begin_op, "Judge", self._model_slot)
        app.call_from_thread(self.refresh_keys)
        try:
            runner = self._make_runner(
                on_row=lambda row, issue: app.call_from_thread(
                    self._row_reviewed, row, issue
                ),
                on_section=lambda index, total, rows, ok: app.call_from_thread(
                    self._section_done, index, total, rows, ok
                ),
            )
            self._runner = runner
            report = runner.evaluate()
        except Exception as exc:            # a crash must not eat the screen
            app.call_from_thread(self._failed, f"{type(exc).__name__}: {exc}")
            return
        app.call_from_thread(self._evaluated, report)

    def _row_reviewed(self, row: int, issue) -> None:
        """
        One reviewed row, live — into the grid as well as the rail.

        Queued rather than painted: a section of fifty rows arrives as fifty
        calls within a few milliseconds, and the repaint timer turns the burst
        into one grid rebuild (NFR-201). The count is a set of row numbers, so a
        row that arrives twice cannot take the figure past its own total.
        """
        self._reviewed_rows.add(row)
        self._pending_live.append((row, issue))
        if issue is not None:
            self._flagged_live.append((row, issue))
            del self._flagged_live[:-200]

    def _section_done(self, index: int, total: int, rows: list[int], ok: bool) -> None:
        """
        One finished request.

        The progress figure needs this as well as the per-row one: a request that
        fails or answers in prose reports no rows at all, so a bar fed only by
        rows would stop moving exactly when something has gone wrong. Its rows are
        counted as done — they will not be reviewed again — while staying out of
        the score, which is `_summarise`'s job.
        """
        self._sections_done += 1
        self._sections_total = max(self._sections_total, total, index)
        if not ok:
            self._reviewed_rows.update(rows)

    def _evaluated(self, report: JudgeReport) -> None:
        self._busy_batch = False
        # A run the pool aborted (every connection/model gone) comes back here
        # with a partial report. It is paused, not completed: the finished rows
        # are kept and shown, but the state and the offered action say the work
        # did not finish and can be retried when the provider is back (bug #4).
        self._run_state = "paused" if self._aborted_run else "completed"
        self._report = report
        # Anything still queued belongs to this report; dropping it would leave
        # the last section's rows out of the grid until the next rebuild.
        self._pending_live.clear()
        self._fill_grid()
        self._paint_rail()
        self.refresh_keys()

        if report.failed_sections:
            # A summary card, not a wall: the raw per-section errors are already
            # in the events rail / log. Merge them into one line with a count.
            n = len(report.failed_sections)
            self._notice.notify_issue(
                "Some requests failed",
                f"{n} request(s) — see Workers for details",
                kind="warn",
            )
        if self._aborted_run:
            # The provider gave out mid-run. Say what happened, keep every
            # finished row, and offer to pick up the unfinished ones once the
            # connection is back (bug #4).
            self._notice.notify_issue(
                "Judge paused",
                f"{self._op_model_id()} — provider unavailable. R retries the "
                f"unfinished rows.",
                kind="error",
            )
            self.notify_status(
                "Paused — the provider became unavailable. Finished rows are "
                "kept; press R to retry the rest when it is back.",
                WARN,
            )
            return
        problems = len(report.problem_rows)
        if not problems:
            self.notify_status(
                "No problems found — every row passed. s for the summary.", OK
            )
        else:
            self.notify_status(
                f"{problems} row(s) flagged · f fixes the row under the cursor, "
                f"F fixes what this filter shows, e edits it by hand.",
                WARN,
            )

    def _failed(self, message: str) -> None:
        self._busy_batch = False
        self._run_state = "failed"
        self.refresh_keys()
        self._error.show(message, "evaluation failed")
        self.notify_status("The evaluation could not be completed.", ERROR)

    def _unfinished_rows(self) -> list[int]:
        """
        Rows the run never got a verdict for, when it was paused mid-way (bug #4).

        The rows the plan set out to review, minus the ones an answer landed for.
        Empty once a run completes cleanly, which is what keeps the Retry action
        hidden except when there is genuinely something left to do.
        """
        if self._report is None:
            return []
        planned = {row for row, _s, _t in self._plan_rows()}
        if not planned:
            planned = {row for row, _s, _t in self._report.rows}
        reviewed = set(self._reviewed_rows)
        return sorted(planned - reviewed)

    def action_retry_unfinished(self) -> None:
        """
        Pick up a paused run from the rows it never reached (bug #4).

        Only meaningful after the provider gave out mid-evaluation: the finished
        rows stay exactly as they are, and this re-runs the evaluation over just
        the unfinished ones, folding the fresh verdicts into the report already
        on screen. Nothing already reviewed is re-sent.
        """
        if self._busy_batch or self._busy_row:
            return
        rows = self._unfinished_rows()
        if not rows:
            self.notify_status("Nothing is unfinished — the run completed.", MUTED)
            return
        self._busy_batch = True
        self._run_state = "running"
        self._aborted_run = False
        self._retry_rows = list(rows)
        self._begin_op("Judge", self._model_slot)
        self.refresh_keys()
        self.notify_status(
            f"Retrying {len(rows)} unfinished row(s)…", ACCENT
        )
        self._retry_unfinished(list(rows))

    @work(thread=True)
    def _retry_unfinished(self, rows: list[int]) -> None:
        app = self._app
        wanted = set(rows)
        try:
            runner = self._make_runner(
                on_row=lambda row, issue: app.call_from_thread(
                    self._row_reviewed, row, issue
                ),
                on_section=lambda index, total, section_rows, ok:
                    app.call_from_thread(
                        self._section_done, index, total, section_rows, ok
                    ),
            )
            all_rows, _contexts = runner.load_rows()
            subset = [entry for entry in all_rows if entry[0] in wanted]
            if not subset:
                app.call_from_thread(self._retry_done, None)
                return
            plan = runner.plan(subset)
            fresh = runner.evaluate(plan=plan)
        except Exception as exc:
            app.call_from_thread(self._failed, f"{type(exc).__name__}: {exc}")
            return
        app.call_from_thread(self._retry_done, fresh)

    def _retry_done(self, fresh: "JudgeReport | None") -> None:
        self._busy_batch = False
        self._run_state = "completed"
        self.refresh_keys()
        report = self._report
        if report is None:
            return
        if fresh is None:
            self.notify_status("Those rows are no longer in the range.", WARN)
            return
        # Fold the fresh verdicts in without disturbing what already landed: a
        # row that came back flagged joins the issues, a clean one stays clean.
        for row, issue in fresh.issues.items():
            if issue is not None and row not in report.issues:
                report.issues[row] = issue
        self._pending_live.clear()
        self._fill_grid()
        self._paint_rail()
        remaining = self._unfinished_rows()
        if remaining:
            self._run_state = "paused"
            self.notify_status(
                f"{len(remaining)} row(s) still unfinished — the provider may "
                f"still be unavailable. R retries again.",
                WARN,
            )
        else:
            self.notify_status("Retry complete — every row has a verdict now.", OK)
        self.refresh_keys()

    def _fill_grid(self) -> None:
        """
        Source and translation, two columns, coloured by status (UET_UX §6).

        The row's colour carries severity (or green once repaired); a short note
        under a still-flagged row names its category. The full comment and the
        before/after comparison live in the row popup, so the grid itself stays
        clean. The current filter is reapplied afterwards, so a re-judge or an
        applied fix does not silently drop the view back to everything.
        """
        report = self._report
        if report is None:
            return
        rows = [(row, source) for row, source, _dst in report.rows]
        # A short note per still-flagged row — category only, since the colour
        # already says the severity. Fixed rows carry no note: the green row is
        # the whole message, and the detail is one click away.
        notes = {
            row: issue.label
            for row, issue in report.issues.items()
        }
        note_styles = {
            row: _SEVERITY_STYLES.get(issue.severity, WARN)
            for row, issue in report.issues.items()
        }
        with self.grid.batched():
            self.grid.set_sources(rows, self._columns())
            self.grid.set_notes(notes, note_styles)
            self.grid.set_row_styles(self._row_styles())
        self._apply_filter()

    # ── Filtering ─────────────────────────────────────────────────────────────

    def _filter_counts(self) -> list[tuple[str, str, int]]:
        """
        (key, label, count) per chip: `all`, `problems`, the severities, then the
        error types that actually occurred.

        Counts include repaired rows under their original flag, so the numbers
        do not shrink as someone fixes — a critical row that has been fixed is
        still counted under `critical` (and shown green), which keeps the chip a
        stable tally of what the file had rather than what is left. The `fixed`
        chip says how many of those are now green.

        Only what is present gets a chip. A row of zeroes would be four more
        things to read past on the way to the one that matters.
        """
        report = self._report
        if report is None:
            return []

        def _count(key: str) -> int:
            rows = self._rows_for_filter(key)
            return len(report.rows) if rows is None else len(rows)

        chips = [
            ("all", "all", len(report.rows)),
            ("problems", "problems", _count("problems")),
        ]
        # Severities and categories present either now or before a fix.
        severities = dict(report.summary.severity_counts)
        distribution = dict(report.summary.distribution)
        for row, issue in self._fixed_issue.items():
            if row not in self._before:
                continue
            sev = getattr(issue, "severity", "")
            if sev:
                severities[sev] = severities.get(sev, 0)
            for cat in getattr(issue, "error_types", []):
                distribution[cat] = distribution.get(cat, 0)
        for name in SEVERITIES:
            if name in severities:
                chips.append((f"sev:{name}", name, _count(f"sev:{name}")))
        for name in sorted(distribution, key=lambda n: (-distribution[n], n)):
            chips.append((name, name, _count(name)))
        if self._before:
            # Only after something has been applied, and last, because it is a
            # record of what happened rather than a kind of problem.
            chips.append(("fixed", "fixed", len(self._before)))
        if self._settled_rows:
            # The convergence guard's output: rows three fix cycles deep and
            # still flagged. They leave the automatic loop; this chip is where
            # a human finds them.
            chips.append(("settled", "settled", len(self._settled_rows)))
        if self._rejudged_rows:
            # The second verdicts: rows the judge looked at again after a fix,
            # with the outcome carried by the row's colour (severity = still
            # wrong, green = holds up). A record, like `fixed`, so it sits last.
            chips.append(("rejudged", "re-judged", len(self._rejudged_rows)))
        return chips

    def _rows_for_filter(self, key: str) -> list[int] | None:
        """
        Row numbers a filter shows. `None` means every row.

        A repaired row keeps its place in the filter it was flagged under
        (UET_UX §6): `_fixed_issue` remembers the flag `report.drop` removed, so
        a critical row that has been fixed still appears under `critical` — now
        green — instead of vanishing. The `fixed` chip lists exactly those.

        `problems` counts ONLY the rows still flagged now. It used to include
        every row ever fixed (flagged ∪ _before), which made the number
        mathematically incapable of going down no matter how much was repaired
        — two contradictory numbers on one screen, and the one a user reads as
        "how much is left" lied. The `fixed` chip is the tally of everything
        already repaired; `problems` is what remains.
        """
        report = self._report
        if report is None or key == "all":
            return None
        if key == "problems":
            return sorted(set(report.problem_rows))
        if key == "fixed":
            return sorted(self._before)
        if key == "settled":
            # Rows whose three fix cycles are spent and are still flagged —
            # the automatic loop is done with them; a human decides.
            return sorted(self._settled_rows)
        if key == "rejudged":
            # Every row the judge has looked at twice — passed or still wrong,
            # the colour tells them apart. Kept separately from `fixed` because
            # a row can be repaired and not yet re-checked, and vice versa a
            # re-check can cover a row repaired by hand before any Fix All.
            return sorted(self._rejudged_rows)
        if key.startswith("sev:"):
            severity = key[4:]
            live = {row for row, issue in report.issues.items()
                    if issue.severity == severity}
            fixed = {row for row, issue in self._fixed_issue.items()
                     if getattr(issue, "severity", "") == severity
                     and row in self._before}
            return sorted(live | fixed)
        live = {row for row, issue in report.issues.items()
                if key in issue.error_types}
        fixed = {row for row, issue in self._fixed_issue.items()
                 if key in getattr(issue, "error_types", []) and row in self._before}
        return sorted(live | fixed)

    def _apply_filter(self) -> None:
        rows = self._rows_for_filter(self._filter)
        self.grid.set_row_filter(None if rows is None else set(rows))
        self.filters.set_chips(self._filter_counts(), self._filter)
        # The cursor is a sheet row, so narrowing the view only has to move it
        # when the row it is on has been hidden — which keeps someone's place
        # when they switch to `critical` and back.
        visible = self.grid.visible_rows
        if self._cursor_row not in visible:
            self._cursor_row = self._preferred_row(visible)
        self._sync_cursor()
        self.refresh_keys()

    def _preferred_row(self, visible: list[int]) -> int:
        """
        Where the cursor lands in a fresh view: the first flagged row, else the
        first row at all.

        A flagged row is what every action on this screen is about, so starting
        on one means `f` and `g` work without having to travel first.
        """
        if not visible:
            return 0
        report = self._report
        if report is not None:
            flagged = set(report.problem_rows)
            for row in visible:
                if row in flagged:
                    return row
        return visible[0]

    def _sync_cursor(self) -> None:
        """Tell the grid which row is selected, and scroll it into view."""
        self.grid.cursor_row = self._cursor_row or -1
        self._scroll_to_cursor()

    def _scroll_to_cursor(self) -> None:
        """
        Keep the cursor row on screen as it moves.

        The grid paints two header lines and hangs a note under any row that has
        one, so a row's y is not its index — it has to be counted, which is what
        `line_of_row` does.

        `scroll_target_y` rather than `scroll_y`: with smooth scrolling in flight
        the two differ, and comparing against the *current* position re-issues a
        scroll the animation was already performing, which fights the anchor.
        """
        if not self._cursor_row:
            return
        pane = self._grid_pane()
        if pane is None:
            return
        try:
            y = self.grid.line_of_row(self._cursor_row)
            if y < 0:
                return
            height = max(1, pane.size.height)
            top = pane.scroll_target_y
            if y < top + 1:
                pane.scroll_to(y=max(0, y - 1), animate=False)
            elif y > top + height - 3:
                pane.scroll_to(y=max(0, y - height + 3), animate=False)
        except Exception:
            pass

    def action_move_cursor(self, step: int) -> None:
        """
        Move the selection by one visible row.

        Rows, not lines: the arrow keys used to scroll the pane, which meant the
        thing `f` and `g` acted on was invisible and moved by a hidden index. A
        screen that offers "fix this row" has to let someone choose which row.

        While the evaluation is live, moving up also detaches the follow and
        moving to the last row re-attaches it — the same gesture the translate
        feed has, so a row can be read while the report is still growing.
        """
        visible = self.grid.visible_rows
        if not visible:
            return
        if self._cursor_row in visible:
            index = visible.index(self._cursor_row) + step
        else:
            index = 0 if step > 0 else len(visible) - 1
        index = max(0, min(index, len(visible) - 1))
        if step < 0:
            self._detach_live_follow()
        elif index == len(visible) - 1:
            self._attach_live_follow()
        self._cursor_row = visible[index]
        self._sync_cursor()
        self.refresh_keys()

    def _visible_problems(self) -> list[int]:
        """Flagged rows the filter is currently showing, in sheet order."""
        report = self._report
        if report is None:
            return []
        flagged = set(report.problem_rows)
        return [row for row in self.grid.visible_rows if row in flagged]

    def set_filter(self, key: str) -> None:
        if self._report is None or key == self._filter:
            return
        self._filter = key
        self._apply_filter()
        shown = len(self._visible_problems())
        if key == "all":
            self.notify_status("showing every row", MUTED)
        elif key == "problems":
            self.notify_status(f"showing {shown} flagged row(s)", WARN)
        elif key == "fixed":
            self.notify_status(
                f"showing {len(self._before)} repaired row(s) — before and after, "
                f"r reverts the one under the cursor",
                OK,
            )
        elif key == "rejudged":
            self.notify_status(
                f"showing {len(self._rejudged_rows)} re-judged row(s) — "
                f"severity colour = still wrong, green = the fix holds up",
                ACCENT,
            )
        elif key.startswith("sev:"):
            severity = key[4:]
            self.notify_status(
                f"showing {shown} {severity} row(s) — f offers just these",
                _SEVERITY_STYLES.get(severity, WARN),
            )
        else:
            self.notify_status(
                f"showing {shown} row(s) with {key} — f offers just these", WARN
            )

    def on_filter_bar_chip_clicked(self, event: "FilterBar.ChipClicked") -> None:
        self.set_filter(event.key)

    def _cycle_filter(self, step: int) -> None:
        chips = [key for key, _label, _count in self._filter_counts()]
        if not chips:
            return
        index = chips.index(self._filter) if self._filter in chips else 0
        self.set_filter(chips[(index + step) % len(chips)])

    def action_next_filter(self) -> None:
        self._cycle_filter(1)

    def action_prev_filter(self) -> None:
        self._cycle_filter(-1)

    # ── Callbacks from the worker thread ──────────────────────────────────────

    def _on_status(self, status) -> None:
        self._workers[getattr(status, "worker_id", 0)] = status
        # Observe every transition so the panel can hold Success/Failed/Retrying.
        self._ledger.note(status)

    # ── Operation context (UET_UX bug #1/#9) ─────────────────────────────────
    #
    # Judge, Fix Row, Fix All and Rejudge are separate operations that may run
    # on different models. The status card used to read the model from the Judge
    # setup regardless of which one was working, so a Fix on `gemini` showed the
    # Judge's `ollama/gemma`. Each operation now names itself and its slot, and
    # every status the UI shows reads from *that* rather than from `_model_slot`.

    def _begin_op(self, label: str, slot: tuple[str, str, str] | None) -> None:
        self._op_label = label
        self._op_slot = slot or self._model_slot
        # A new operation starts with a clear stop flag: `c` sets it to stop
        # whatever is running, and without a clear here the next operation —
        # started after the previous one finished — would be instantly
        # cancelled by the flag an earlier cancel left behind.
        self._stop.clear()
        # And a fresh cancel window: the double-press force-cancel counts
        # presses against the *current* operation only, so a `c` pressed long
        # after the last one is a first press again.
        self._cancel_requested_at = 0.0
        # A new operation starts clean: any card left from a previous one is no
        # longer relevant, and a stale "recovered"/"failed" line must not linger.
        self._issue_active = False
        try:
            self._notice.dismiss()
        except Exception:
            pass
        # Same for the rail's error box: without this, a fatal error from an
        # earlier operation stayed on screen through everything that followed,
        # reading as live output of whatever is running now.
        try:
            self._error.clear()
        except Exception:
            pass

    def _op_model_id(self) -> str:
        """The label of the model the *active operation* is using, not the Judge's."""
        try:
            choice = self._choice(self._op_slot)
        except Exception:
            return self._model_id()
        if choice.is_combo:
            return f"Group · {choice.label}"
        return choice.label

    def _on_event(self, event) -> None:
        # Every event still goes to the log verbatim (diagnostics keep the raw
        # provider text). The small notice card only ever shows a merged summary.
        if event.kind == "aborted":
            # Every connection/model is gone after the retry budget was spent.
            # The operation is not "running" any more; say so explicitly rather
            # than leaving a frozen feed that reads as a hang (bug #4).
            self._log(event.message, ERROR)
            self._aborted_run = True
            self._run_state = "paused"
            self._show_provider_issue(event)
            self.refresh_keys()
        elif event.kind == "batch_failed":
            self._log(event.message, ERROR)
            self._show_provider_issue(event)
        elif event.kind == "retry":
            # A request is being retried automatically (bug #4 lifecycle). This
            # is a distinct state from plain running, so the rail and footer can
            # say "Retrying" instead of pretending nothing is wrong.
            self._log(event.message, WARN)
            if self._run_state == "running":
                self._run_state = "retrying"
            self._show_retry(event)
        elif event.kind in ("batch_done", "wave_done"):
            # A request went through — the connection is working. If a warning
            # card was up, replace it with a brief recovery note, and the state
            # returns to plain running from a transient retry.
            if self._run_state == "retrying":
                self._run_state = "running"
            self._maybe_recover()

    def _show_provider_issue(self, event) -> None:
        kind = getattr(event, "error_kind", "") or ""
        ref = f"request {event.batch_num}" if event.batch_num else ""
        title, detail, level = self._classify_notice(kind, event.message)
        self._notice.notify_issue(title, detail, kind=level, ref=ref)
        if level != "error":
            self._issue_active = True

    def _show_retry(self, event) -> None:
        # A retry is temporary by definition — a small warning card, merged.
        self._notice.notify_issue(
            f"{self._op_label} retrying",
            self._op_model_id(),
            kind="warn",
            ref=f"request {event.batch_num}" if event.batch_num else "",
        )
        self._issue_active = True

    def _classify_notice(self, kind: str, message: str) -> tuple[str, str, str]:
        """(title, detail, level) for the notice card, from UET's classification."""
        model = self._op_model_id()
        if kind == "quota":
            return ("Provider quota reached",
                    f"{model} — confirmed daily/quota limit", "error")
        if kind == "auth":
            return ("Authentication failed", f"{model} — check the API key", "error")
        if kind == "invalid":
            return ("Request rejected",
                    f"{model} — invalid request or model", "error")
        if kind == "server":
            return ("Provider unavailable",
                    f"{model} — server error, retrying", "warn")
        if kind == "rate_limit":
            return ("Rate limited",
                    f"{model} — temporary, retrying", "warn")
        # Unknown / all-connections-unavailable: neutral, temporary wording. Only
        # a confirmed quota is ever called exhaustion.
        low = (message or "").lower()
        if "unavailable" in low or "cooldown" in low or "rate" in low:
            return ("Provider temporarily unavailable",
                    f"{model} — paused, retry when it is back", "warn")
        return ("Provider issue", f"{model}", "warn")

    def _maybe_recover(self) -> None:
        if getattr(self, "_issue_active", False):
            self._issue_active = False
            self._notice.notify_issue(
                "Provider recovered",
                f"{self._op_model_id()} — resumed automatically",
                kind="ok",
            )

    def on_provider_notice_clicked(self, _event: ProviderNotice.Clicked) -> None:
        pass

    def _on_notice(self, kind: str, message: str) -> None:
        self._log(message, {"error": ERROR, "warn": WARN}.get(kind, MUTED))
        if kind == "error":
            # A fatal notice from the runner (e.g. every connection gone). Show a
            # sticky card rather than the old full-width error wall; keep the raw
            # text in the log. Whether it is truly quota or just temporary is
            # already in the message the pool produced.
            low = (message or "").lower()
            if "quota" in low or "daily" in low or "billing" in low:
                title, detail, level = (
                    "Provider quota reached", self._op_model_id(), "error")
            else:
                title, detail, level = (
                    "Provider unavailable",
                    f"{self._op_model_id()} — may be temporary, try again shortly",
                    "error",
                )
            self._notice.notify_issue(title, detail, kind=level)

    # ── Row selection ─────────────────────────────────────────────────────────

    def _current_row(self) -> int:
        """
        The row every single-row action works on.

        The cursor row if it is still on screen, else the best row in the current
        view. Kept as a sheet number rather than an index into a filtered list,
        which is what used to make `f` fix a different row than the one being
        looked at after a filter change.
        """
        visible = self.grid.visible_rows
        if self._cursor_row in visible:
            return self._cursor_row
        self._cursor_row = self._preferred_row(visible)
        self._sync_cursor()
        return self._cursor_row

    def _flagged_row(self) -> int:
        """The row under the cursor, but only if it is actually flagged."""
        row = self._current_row()
        report = self._report
        if report is None or row not in report.issues:
            return 0
        return row

    def on_compare_grid_row_clicked(self, event: CompareGrid.RowClicked) -> None:
        # A click selects, exactly as the arrow keys do, then opens the row.
        self._cursor_row = event.row_num
        self._sync_cursor()
        self.refresh_keys()
        self._open_row(event.row_num)

    def action_open_row(self) -> None:
        """Enter opens the row under the cursor — the one gateway to fixing it."""
        if self._busy_batch or self._busy_row:
            return
        row = self._current_row()
        if row:
            self._open_row(row)

    def _open_row(self, row: int) -> None:
        """
        Open one row in the fix popup: read source and comment, edit the
        translation by hand, or hand off to a model. The single entry point for
        fixing one row — clicking a row and pressing Enter both land here.
        """
        report = self._report
        if report is None or self._busy_batch or self._busy_row:
            return
        self.app.push_screen(
            RowFixModal(
                row=row,
                source=report.source_for(row),
                translation=report.translation_for(row),
                comment=report.comment_for(row),
                issue=report.issues.get(row),
                before=self._before.get(row, ""),
            ),
            lambda result, row=row: self._row_action(row, result),
        )

    def _row_action(self, row: int, result) -> None:
        """
        The row popup came back with an action:
          ("edit", text) — a hand edit, applied immediately as a manual Fixed.
          ("fix", None)  — repair with a model, via the usual model popup.
          None           — closed with no change.
        """
        if not result:
            return
        kind, payload = result
        if kind == "edit":
            self._edited(row, payload)
        elif kind == "fix":
            self._fix_row_for(row)

    def action_scroll_grid(self, lines: int) -> None:
        """
        Page through the report.

        Paging back detaches the live follow and paging to the bottom re-attaches
        it, matching what the arrow keys and the wheel do — otherwise PageUp would
        move the view and the next section would immediately drag it away again.
        """
        pane = self._grid_pane()
        if pane is None:
            return
        if lines < 0:
            self._detach_live_follow()
        elif pane.scroll_target_y + lines >= pane.max_scroll_y - 1:
            self._attach_live_follow()
        pane.scroll_to(y=pane.scroll_target_y + lines, animate=False)

    def _detach_live_follow(self) -> None:
        """Stop pinning the view to the newest reviewed row."""
        if self._follow_live and self._report is None:
            self._follow_live = False
            self.notify_status(
                "scroll freely — rows keep landing; F follows the latest again",
                WARN,
            )
            self.refresh_keys()

    def _attach_live_follow(self) -> None:
        """Follow the newest reviewed row again."""
        if not self._follow_live:
            self._follow_live = True
            if self._report is None:
                self.notify_status("following the newest reviewed row", ACCENT)
                self.refresh_keys()

    def action_scroll_grid_home(self) -> None:
        pane = self._grid_pane()
        if pane is None:
            return
        self._detach_live_follow()
        pane.scroll_home(animate=False)

    def action_scroll_grid_end(self) -> None:
        pane = self._grid_pane()
        if pane is None:
            return
        self._attach_live_follow()
        pane.scroll_end(animate=False)

    def action_f_key(self) -> None:
        """
        `f` while a batch runs follows the newest row; otherwise it is Fix All.

        The live screen and the report screen want different things from the same
        key, and dispatching here keeps one binding honest rather than two that
        would clash (bug #2/#3).
        """
        if self._busy_batch:
            self.action_follow_latest()
        else:
            self.action_fix_all()

    def action_c_key(self) -> None:
        """
        `c` cancels the running operation; on the finished report it confirms
        and saves.

        Cancelling only stops the operation in flight — Fix Row, Fix All,
        Rejudge, or a paused retry — and stays on the results screen. The
        report already on screen and every result that landed before the stop
        are kept; nothing is re-sent, and nothing takes the user back to the
        setup. It is a cancel, not a leave.
        """
        if self._busy_batch or self._busy_row:
            self.action_cancel_operation()
        else:
            self.action_confirm_save()

    def action_cancel_operation(self) -> None:
        """
        `c` — stop whatever is running (batch or single-row) and stay here.

        The FIRST press is a graceful stop: the stop flag is set, and the
        request in flight is allowed to finish — its reply is real work the
        report keeps. Nothing new is sent after it.

        The SECOND press, while the first is still pending, is a force stop:
        every request actually in flight is severed at the socket, so the run
        ends immediately instead of after the current output. Two presses
        because a graceful stop can still wait out a slow generation, and
        "wait" is sometimes exactly what the user no longer wants.
        """
        now = time.monotonic()
        pending = (self._busy_batch or self._busy_row) and self._stop.is_set()
        if pending and (now - self._cancel_requested_at) <= 5.0:
            # Second press inside the window: force. Sever the sockets under
            # every in-flight request; the blocked reads raise in their worker
            # threads, which see the stop flag and give up without retrying.
            self._force_abort()
            self._run_state = "cancelled"
            self.notify_status(
                "Force-cancelling — aborting the request in flight…", ERROR,
            )
            self.refresh_keys()
            return

        self._cancel_requested_at = now
        self._stop.set()
        if self._busy_batch:
            self._run_state = "cancelled"
        self.notify_status(
            "Cancelling after the current output — press c again to abort "
            "immediately.",
            WARN,
        )
        self.refresh_keys()

    def _force_abort(self) -> None:
        """
        Sever every HTTP request this screen's operation has in flight.

        Every client the executors built carries this screen's stop event, and
        the client module keeps a registry of (event → clients); `abort_all_for`
        closes each registered client's live socket. The blocked reads raise
        in their worker threads as cancelled ProviderErrors, which the retry
        loop sees (stop flag set) and gives up on without rotating or
        re-issuing.
        """
        from ...providers.client import abort_all_for

        self._stop.set()
        try:
            abort_all_for(self._stop)
        except Exception:
            # The registry is best-effort: with or without it, the stop flag
            # alone still ends the run at the next checkpoint — every result
            # already read is kept, and nothing new is sent.
            pass

    def action_follow_latest(self) -> None:
        """
        Jump back to the newest reviewed row and resume following it (bug #6).

        `f` while running is the counterpart to scrolling back: a reader who
        paged up to study an earlier flag presses it to catch up with the live
        feed, exactly as the translate run screen's follow key does. Row
        insertion never forces the view down on its own — scrolling up detaches
        follow and it stays put — so this is the one gesture that re-attaches it.
        """
        pane = self._grid_pane()
        if pane is None:
            return
        self._attach_live_follow()
        pane.scroll_end(animate=False)
        if self._report is None:
            self.notify_status("following the newest reviewed row", ACCENT)
        self.refresh_keys()

    def action_toggle_workers(self) -> None:
        open_now = self.activity.toggle()
        self.notify_status(
            "worker panel shown" if open_now else "worker panel hidden", MUTED
        )

    def action_toggle_activity(self) -> None:
        self.action_toggle_workers()

    def action_toggle_events(self) -> None:
        """Show or hide the events rail — `e`, or click its header."""
        open_now = self.events.toggle()
        self.refresh_keys()
        self.notify_status(
            "events panel shown" if open_now else "events panel hidden — e to show",
            MUTED,
        )

    def on_events_panel_header_clicked(
        self, _event: EventsPanel.HeaderClicked
    ) -> None:
        """Clicking the events header is the same gesture as pressing `e`."""
        self.refresh_keys()

    # ── Fix It (one row, small indicator) ─────────────────────────────────────

    def _fix_row_for(self, row: int) -> None:
        """Repair one row: reuse the session model, or ask for one via popup."""
        if self._busy_batch or self._busy_row:
            return
        session_slot = getattr(self.app, "judge_fix_slot", None)
        if session_slot is not None:
            self._begin_fix_row(row, session_slot)
            return
        self.app.push_screen(
            FixModelPopup(model_slot=self._model_slot),
            lambda result: self._fix_model_chosen(row, result),
        )

    def _fix_model_chosen(self, row: int, result) -> None:
        """The Fix popup came back: (slot, remember) or None."""
        if not result:
            return
        slot, remember = result
        if remember:
            # Reused for every single fix this Judge session; reset when a new
            # evaluation is set up (JudgeSetupScreen).
            self.app.judge_fix_slot = slot
        self._begin_fix_row(row, slot)

    def _begin_fix_row(self, row: int, slot: tuple[str, str, str]) -> None:
        self._busy_row = row
        self._fix_row_slot = slot
        self._begin_op("Fix Row", slot)
        self.indicator.start(f"fixing row {row}…")
        self.refresh_keys()
        self._fix_one(row)

    @work(thread=True)
    def _fix_one(self, row: int) -> None:
        app = self._app
        slot = getattr(self, "_fix_row_slot", None) or self._model_slot
        runner = self._make_runner(model_slot=slot)
        report = self._report
        result = runner.fix_single(
            row,
            comment=report.comment_for(row) if report else "",
            source=report.source_for(row) if report else None,
            translation=report.translation_for(row) if report else None,
        )
        app.call_from_thread(self._fixed_one, result)

    def _fixed_one(self, result) -> None:
        self._busy_row = 0
        self.indicator.stop()
        self._begin_op("Judge", self._model_slot)
        self.refresh_keys()
        if not result.ok:
            self._error.show(result.error, f"fix row {result.row} failed")
            self.notify_status("That row could not be fixed.", ERROR)
            return
        # Staged, not written: one row is reviewed the same way a hundred are.
        self.app.push_screen(
            JudgeFixReviewScreen(
                runner=self._runner or self._make_runner(),
                report=self._report,
                staged=StagedFix(texts={result.row: result.text}),
            ),
            self._fixes_applied,
        )

    # ── Fix All (batch, worker panel) ─────────────────────────────────────────

    def action_fix_all(self) -> None:
        """
        Open the Fix All wizard for the rows the filter is showing.

        Nothing is sent yet. Fix All spends real tokens and rewrites lines a
        human may not have read, so it gets a setup step: which model, which
        categories and severities, how many rows per request, how many workers.
        The filter narrows what is *offered*; the wizard decides what is sent.

        The filter being the scope is the whole point: with `critical` on screen,
        `F` repairs the critical rows and leaves everything else alone, which is
        one gesture for what would otherwise be three passes. It works from the
        `fixed` view too, where the scope is the repaired rows a re-judge has
        flagged again — a second attempt at the ones the first repair did not
        settle.

        An empty cell is excluded here rather than in the wizard — there is
        nothing to repair in a row that was never translated, and it needs a
        translate run instead.
        """
        report = self._report
        if report is None or self._busy_batch or self._busy_row:
            return
        visible = self._visible_problems()
        if not visible:
            self.notify_status(
                "Nothing flagged in this filter — tab to another one, or press e "
                "to edit the row under the cursor."
                if self._filter != "problems"
                else "Nothing is flagged.",
                MUTED,
            )
            return
        fixable = [
            row for row in visible
            if "empty" not in report.issues[row].error_types
        ]
        if not fixable:
            self.notify_status(
                "The flagged rows are empty cells — translate them instead.", WARN
            )
            return
        self.app.push_screen(
            FixAllSetupScreen(
                report=report,
                rows=fixable,
                model_slot=self._model_slot,
                target=self._target,
                scope=self._scope_label(),
            ),
            self._fix_all_confirmed,
        )

    def _scope_label(self) -> str:
        """How the wizard describes where its rows came from."""
        if self._filter == "all":
            return "every flagged row"
        if self._filter == "problems":
            return "every flagged row"
        if self._filter == "fixed":
            return "repaired rows that are still flagged"
        if self._filter.startswith("sev:"):
            return f"rows graded {self._filter[4:]}"
        return f"rows flagged as {self._filter}"

    def _fix_all_confirmed(self, plan: "FixAllPlan | None") -> None:
        """The wizard came back with a plan, or the user backed out."""
        if plan is None or self._report is None or self._busy_batch:
            return
        if not plan.rows:
            self.notify_status("No rows match those filters.", WARN)
            return
        self._busy_batch = True
        self._fix_plan = plan
        self._fixed_live.clear()
        self._fix_seen.clear()
        self._begin_op("Fix All", plan.model_slot)
        self.refresh_keys()
        self.notify_status(
            f"Fixing {len(plan.rows)} row(s) with {plan.model_label} · "
            f"{plan.rows_per_request} per request · {plan.workers} worker(s)…",
            ACCENT,
        )
        self._fix_many(plan)

    @work(thread=True)
    def _fix_many(self, plan: "FixAllPlan") -> None:
        app = self._app
        try:
            # A wizard that chose a different model needs its own runner: the
            # cached one is pinned to the Judge model and its pacing.
            runner = self._make_runner(
                model_slot=plan.model_slot,
                workers=plan.workers,
                on_fix=lambda row, text, changed: app.call_from_thread(
                    self._fix_landed, row, text, changed
                ),
                on_section=lambda index, total, rows, ok: app.call_from_thread(
                    self._fix_section_done, rows
                ),
            )
            staged = runner.fix_all(
                plan.rows,
                report=self._report,
                criteria=plan.criteria,
                severities=plan.severities,
                batch_size=plan.rows_per_request,
            )
        except Exception as exc:
            app.call_from_thread(self._failed, f"{type(exc).__name__}: {exc}")
            return
        app.call_from_thread(self._fixed_many, staged)

    def _fix_landed(self, row: int, text: str, changed: bool) -> None:
        """
        One repaired row, live.

        Shown as it arrives for the same reason the translate feed is: a long
        Fix All is otherwise a blank screen, and a row the model chose to leave
        alone is worth seeing immediately rather than discovering at the end.
        """
        self._fix_seen.add(row)
        self._fixed_live.append((row, text, changed))
        del self._fixed_live[:-200]

    def _fix_section_done(self, rows: list[int]) -> None:
        """
        A repair request finished, so all of its rows are settled.

        Counted per request as well as per row because a row can be settled
        without producing a fix — left alone, or failed — and a bar fed only by
        rewrites would understate a run that is going fine.
        """
        self._fix_seen.update(rows)

    def _fixed_many(self, staged: StagedFix) -> None:
        self._busy_batch = False
        self._fix_plan = None
        self._begin_op("Judge", self._model_slot)
        self.refresh_keys()
        if staged.errors:
            # Per-request provider failures already reach the Events rail and
            # the notice card; a second panel repeating the same raw strings
            # was the duplication the rail lost space to.
            self._log(
                f"{len(staged.errors)} fix request(s) failed — see Events for "
                f"the provider text", WARN,
            )
            self._notice.notify_issue(
                "Some fix requests failed",
                f"{len(staged.errors)} request(s) — see Events for details",
                kind="warn",
            )
        if not staged.texts:
            # `unchanged` is a result, not a failure: the model declined to guess
            # rather than returning something wrong, and saying so is the whole
            # point of tracking it separately.
            if staged.unchanged:
                self.notify_status(
                    f"{len(staged.unchanged)} row(s) were left as they were — "
                    f"fixing them would have changed what the source means.",
                    WARN,
                )
            else:
                self.notify_status("No fixes came back.", ERROR)
            return
        self.app.push_screen(
            JudgeFixReviewScreen(
                runner=self._runner or self._make_runner(),
                report=self._report,
                staged=staged,
            ),
            self._fixes_applied,
        )

    def _fixes_applied(self, applied: dict[int, str] | None) -> None:
        """
        Reflect approved rows in the report — they are no longer problems.

        The pre-fix text is kept so the grid can show before and after side by
        side (that is what grows the comparison column), and the view switches to
        those rows: having just approved a change, the next thing anyone wants is
        to see it.

        The flag each row carried is kept too, because a revert has to put it
        back — the problem is in the file again the moment the old text is.

        A "fix" whose text is identical to what the file already held is not a
        repair: writing it changed nothing, so the row keeps its flag and its
        severity colour rather than turning green. A green row that still carries
        the exact text the judge flagged would read as "fixed" while the problem
        the judge named is still in it — which is precisely what Fix All exists
        to remove.
        """
        report = self._report
        if not applied or report is None:
            return
        changed: dict[int, str] = {}
        unchanged: list[int] = []
        for row, text in applied.items():
            if (text or "").strip() == (report.translation_for(row) or "").strip():
                unchanged.append(row)
                continue
            changed[row] = text
        for row, text in changed.items():
            # Recorded before the report is updated, or "before" would already
            # be the fix. First write wins, so fixing a row twice still compares
            # against what the file originally held.
            self._before.setdefault(row, report.translation_for(row))
            issue = report.issues.get(row)
            if issue is not None:
                self._fixed_issue.setdefault(row, issue)
            report.replace_translation(row, text)
            report.drop(row)
            # The text changed, so any verdict this row earned — including a
            # pass the noise-suppressor was holding onto — no longer describes
            # what is in the file: it must be re-earned on the next re-judge.
            # A settled row that the human chose to rewrite by hand re-enters
            # the loop too: new text, new chance.
            self._verdict_stable.discard(row)
            self._settled_rows.discard(row)
            self._fix_cycles.pop(row, None)
            # The file changed: the cached section shape no longer describes
            # it, so the next operation re-measures it.
            self._section_shape_cache = None
        # Stay on the current filter: a fixed row keeps its place there, now
        # green, so there is nothing to switch away to.
        if changed:
            self._cursor_row = min(changed)
        elif unchanged:
            self._cursor_row = min(unchanged)
        self._fill_grid()
        self._paint_rail()
        if unchanged and not changed:
            self.notify_status(
                f"{len(unchanged)} row(s) were written back IDENTICAL to the "
                f"file — the problem the judge named is still in them, so they "
                f"stay flagged. Open a row to edit it by hand, or try a "
                f"stronger repair model.",
                WARN,
            )
        elif unchanged:
            self.notify_status(
                f"{len(changed)} row(s) written to column "
                f"{ExcelReader.col_letter(self._target.dst_col)} — now green. "
                f"{len(unchanged)} row(s) came back identical and stay flagged.",
                OK,
            )
        else:
            self.notify_status(
                f"{len(changed)} row(s) written to column "
                f"{ExcelReader.col_letter(self._target.dst_col)} — now green. "
                f"Click a row to see before/after, g re-judges it, r reverts it.",
                OK,
            )

    # ── Judge Again (one row, small indicator) ────────────────────────────────

    def action_judge_again(self) -> None:
        """
        Re-check the row under the cursor (FR-112).

        Works on a flagged row and on a repaired one, which is the point: after a
        fix is applied the row leaves the problem list, and that is exactly the
        row someone doubts. The loop this screen exists to serve is see → fix →
        re-check, and all three steps are keys on this one screen.

        A re-judge is a grading job in its own right, so it asks which model to
        grade with — the Rejudge popup — rather than silently reusing the Judge
        or Fix model (UET_UX bug #10).
        """
        row = self._current_row()
        if not row or self._busy_batch or self._busy_row:
            return
        if self._report is not None and row not in self._report.issues \
                and row not in self._before:
            self.notify_status(
                "Row {} was not flagged and has not been repaired — there is "
                "nothing to re-check.".format(row), MUTED
            )
            return
        self._with_rejudge_model(
            lambda slot, rows, workers: self._begin_rejudge_row(
                row, slot, rows_per_request=rows, workers=workers
            )
        )

    def _with_rejudge_model(self, then) -> None:
        """
        Resolve the model a Rejudge should use, asking if necessary.

        The remembered slot comes from the per-model preferences store: a model
        whose settings were ticked "Don't ask again" runs immediately with its
        stored rows/workers, and any other model opens the full setup form.
        Kept separate from the Fix slot so the two never bleed into each other.
        """
        from ...data.app_prefs import remembered_judge_settings

        session_slot = getattr(self.app, "judge_rejudge_slot", None)
        if session_slot is not None:
            remembered = remembered_judge_settings(session_slot)
            if remembered is not None:
                then(session_slot, remembered["max_rows"], remembered["workers"])
                return
        self.app.push_screen(
            RejudgeSetupScreen(
                model_slot=self._model_slot,
                scope_label=self._rejudge_scope_label(),
            ),
            lambda result: self._rejudge_model_chosen(result, then),
        )

    def _rejudge_scope_label(self) -> str:
        """What the setup popup says is being re-judged."""
        if self._rejudging or getattr(self, "_rejudge_rows", None):
            n = len(getattr(self, "_rejudge_rows", []) or [])
            return f"{n} applied fix(es)" if n else ""
        row = self._current_row()
        return f"row {row}" if row else ""

    def _rejudge_model_chosen(self, result, then) -> None:
        if not result:
            return
        slot, rows, workers = result
        then(slot, rows, workers)

    def _begin_rejudge_row(
        self, row: int, slot: tuple[str, str, str],
        rows_per_request: int | None = None, workers: int | None = None,
    ) -> None:
        self._busy_row = row
        self._rejudge_row_slot = slot
        self._begin_op("Rejudge", slot)
        self.indicator.start(f"re-judging row {row}…")
        self.refresh_keys()
        self._rejudge(row, slot)

    @work(thread=True)
    def _rejudge(self, row: int, slot: tuple[str, str, str] | None = None) -> None:
        app = self._app
        runner = self._make_runner(model_slot=slot) if slot else \
            (self._runner or self._make_runner())
        report = self._report
        verdict = runner.judge_again(
            row,
            text=report.translation_for(row) if report else None,
            source=report.source_for(row) if report else None,
        )
        app.call_from_thread(self._rejudged, verdict)

    def _rejudged(self, verdict) -> None:
        self._busy_row = 0
        self.indicator.stop()
        self._begin_op("Judge", self._model_slot)
        self.refresh_keys()
        if not verdict.ok:
            self._error.show(verdict.error, f"judge row {verdict.row} failed")
            return
        report = self._report
        if report is None:
            return
        # A re-check happened, so the row joins the re-judged view whatever the
        # verdict was: the chip answers "what did the judge say the second
        # time", and that includes the rows that passed.
        self._rejudged_rows.add(verdict.row)
        repaired = verdict.row in self._before
        if verdict.passed:
            report.drop(verdict.row)
            self.notify_status(
                f"Row {verdict.row} passed — the fix holds up."
                if repaired else f"Row {verdict.row} passed — removed.",
                OK,
            )
        elif verdict.unparsed:
            self.notify_status(
                f"Row {verdict.row}: the model answered in prose, so it stays "
                f"flagged.", WARN
            )
        else:
            if verdict.issue is not None:
                if verdict.row in report.issues:
                    # Replace in place: the old issue's counts are already in
                    # the summary, so a swap must not double-count.
                    old = report.issues[verdict.row]
                    report.issues[verdict.row] = verdict.issue
                    if old is not verdict.issue:
                        for error_type in old.error_types:
                            count = report.summary.distribution.get(error_type, 0)
                            if count > 1:
                                report.summary.distribution[error_type] = count - 1
                            else:
                                report.summary.distribution.pop(error_type, None)
                    for error_type in verdict.issue.error_types:
                        report.summary.distribution[error_type] = (
                            report.summary.distribution.get(error_type, 0) + 1
                        )
                else:
                    # The row was dropped when its fix was applied: restore
                    # properly, counts included. A direct assignment here used
                    # to leave the summary without the fitting key, which hid
                    # the category from Fix All and silently skipped the
                    # re-measure gate for the row.
                    report.restore(verdict.issue)
            # A repaired row that comes back flagged is the case this button
            # exists for: the fix was applied and it is still wrong. f offers a
            # second pass over exactly these; open the row to edit it by hand
            # when a second pass is not going to help either.
            self.notify_status(
                f"Row {verdict.row} is still wrong after the fix — "
                f"{escape_markup(verdict.issue.label) if verdict.issue else 'flagged'}"
                f". f retries the repaired rows, open it to edit, r reverts it."
                if repaired else f"Row {verdict.row} is still flagged.",
                ERROR if repaired else WARN,
            )
        self._fill_grid()
        self._paint_rail()

    # ── Judge Again, every applied fix (batch, worker panel) ──────────────────

    def action_judge_again_fixed(self) -> None:
        """
        Re-check every row a fix has been *written* to (shift+G).

        `g` sends one row on its own, and that is where the false negatives come
        from: a lone row has no neighbours, so every question that needs
        comparison — is this name spelled the way the rest of the file spells it,
        does this register match the lines around it — cannot be answered, and a
        whole class of problem passes by default.

        So this goes down the batch path instead: `plan()` + `evaluate()`, the
        same prompt, the same request size and the same neighbouring rows the
        first evaluation used. That is what makes the second verdict comparable
        to the first rather than a softer test.

        It reads the translations from the file, so it covers applied fixes only.
        A staged fix has not been written yet and is not what the file says.

        Settled rows (three fix cycles spent and still flagged) are excluded:
        their verdict is not going to change on a fourth roll of the dice, and
        re-rolling them is exactly the loop the cycle budget exists to end.
        They stay visible under their severity filter, flagged for manual
        review.
        """
        report = self._report
        if report is None or self._busy_batch or self._busy_row:
            return
        rows = [
            row for row in sorted(self._before)
            if row not in self._settled_rows
        ]
        if not rows:
            settled = len(self._settled_rows)
            self.notify_status(
                "Nothing left to re-judge automatically."
                + (f" {settled} row(s) are settled after 3 fix cycles — "
                   f"open them to review by hand." if settled else "")
                if self._settled_rows else
                "Nothing has been written yet — G re-checks the rows an applied "
                "fix changed. A staged fix is not in the file, so there is "
                "nothing to read back.",
                MUTED,
            )
            return
        self._with_rejudge_model(
            lambda slot, batch, workers: self._begin_rejudge_fixed(
                rows, slot, rows_per_request=batch, workers=workers
            )
        )

    def _begin_rejudge_fixed(
        self, rows: list[int], slot: tuple[str, str, str],
        rows_per_request: int | None = None, workers: int | None = None,
    ) -> None:
        self._busy_batch = True
        self._rejudging = True
        self._rejudge_slot = slot
        self._rejudge_rows = list(rows)
        self._rejudge_seen = set()
        self._rejudge_flagged = []
        self._begin_op("Rejudge", slot)
        self.refresh_keys()
        self.notify_status(
            f"Re-judging {len(rows)} applied fix(es) the same way the first "
            f"evaluation did — batched, with neighbours…",
            ACCENT,
        )
        self._rejudge_many(list(rows), slot, workers=workers)

    @work(thread=True)
    def _rejudge_many(
        self, rows: list[int], slot: tuple[str, str, str] | None = None,
        workers: int | None = None,
    ) -> None:
        app = self._app
        wanted = set(rows)
        try:
            runner = self._make_runner(
                model_slot=slot,
                workers=workers,
                on_row=lambda row, issue: app.call_from_thread(
                    self._rejudge_row_landed, row, issue
                ),
                on_section=lambda index, total, section_rows, ok:
                    app.call_from_thread(
                        self._rejudge_section_done, section_rows, ok
                    ),
            )
            # Read from the file: this is a check on what was actually written.
            all_rows, _contexts = runner.load_rows()
            subset = [entry for entry in all_rows if entry[0] in wanted]
            if not subset:
                app.call_from_thread(self._rejudged_many, None, [])
                return
            # The same planner the first evaluation used, over these rows only,
            # so the sections are built and priced identically. In chain mode
            # the plan also carries the flagged rows' file neighbours as
            # CONTEXT ONLY rows the prompt shows around them.
            from ...data.app_prefs import judge_chain_context

            if judge_chain_context():
                plan = runner.rechain_plan(subset)
            else:
                plan = runner.plan(subset)
            fresh = runner.evaluate(plan=plan)
        except Exception as exc:
            app.call_from_thread(
                self._rejudge_failed, f"{type(exc).__name__}: {exc}"
            )
            return
        app.call_from_thread(
            self._rejudged_many, fresh, [row for row, _s, _t in subset]
        )

    def _rejudge_row_landed(self, row: int, issue) -> None:
        """One re-checked row, live — the same feed the evaluation has."""
        self._rejudge_seen.add(row)
        if issue is not None:
            self._rejudge_flagged.append((row, issue))
            del self._rejudge_flagged[:-200]

    def _rejudge_section_done(self, rows: list[int], ok: bool) -> None:
        self._rejudge_seen.update(rows)

    def _rejudge_failed(self, message: str) -> None:
        self._busy_batch = False
        self._rejudging = False
        self._begin_op("Judge", self._model_slot)
        self.refresh_keys()
        self._error.show(message, "re-judge failed")
        self.notify_status("The applied fixes could not be re-judged.", ERROR)

    def _rejudged_many(self, fresh: "JudgeReport | None", rows: list[int]) -> None:
        """
        Fold the second verdict into the report already on screen.

        The old flag goes whatever happens — it described text that is no longer
        in the file — and a row that is still wrong is flagged again with what
        the re-judge said. A row in a request that failed or answered in prose is
        left exactly as it was: it was not judged, and treating that as a pass is
        the false negative this whole path exists to remove.
        """
        self._busy_batch = False
        self._rejudging = False
        self._begin_op("Judge", self._model_slot)
        self.refresh_keys()
        report = self._report
        if report is None:
            return
        if fresh is None or not rows:
            self.notify_status(
                "Those rows are no longer in the evaluated range — nothing was "
                "re-judged.", WARN
            )
            return

        unevaluated: set[int] = set()
        for section in fresh.sections:
            if not section.ok or section.unparsed:
                unevaluated.update(section.rows)

        still: list[int] = []
        cleared: list[int] = []
        held_stable: list[int] = []      # layer 2: noise-suppressed re-flags
        for row in rows:
            if row in unevaluated:
                continue
            # Judged again, whatever the outcome: the re-judged chip is a
            # record of the second verdicts, not only the bad ones.
            self._rejudged_rows.add(row)
            if row in report.issues:
                report.drop(row)
            issue = fresh.issues.get(row)
            if issue is None:
                cleared.append(row)
                # Layer 2: a pass is remembered. Later re-judges may only
                # overturn it with a critical finding; minor/style flips are
                # sampling noise, and honouring them is what made the problem
                # count oscillate instead of converge.
                self._verdict_stable.add(row)
                continue
            if row in self._verdict_stable and \
                    getattr(issue, "severity", "") != "critical":
                # Layer 2 suppression: this row already passed a re-judge and
                # the new finding is not critical — keep the pass, drop the
                # flag. The verdict stays stable in the report and the count
                # stays converged.
                held_stable.append(row)
                continue
            report.restore(issue)
            still.append(row)

        # Layer 3: count fix cycles for rows that came back flagged. A row at
        # the budget is settled: it leaves the automatic loop (Fix All / G
        # skip it) and is marked for human review, so the cycle ends even if
        # the model keeps disagreeing with itself.
        newly_settled: list[int] = []
        for row in still:
            self._fix_cycles[row] = self._fix_cycles.get(row, 0) + 1
            if self._fix_cycles[row] >= 3 and row not in self._settled_rows:
                self._settled_rows.add(row)
                newly_settled.append(row)

        if fresh.failed_sections:
            # Per-request provider failures already reach the Events rail and
            # the notice card; the ErrorBox below is for terminal failures only.
            self._log(
                f"{len(fresh.failed_sections)} re-judge request(s) failed — "
                f"see Events for the provider text", WARN,
            )

        self._filter = "rejudged" if self._rejudged_rows else "fixed"
        if still:
            self._cursor_row = still[0]
        self._fill_grid()
        self._paint_rail()

        parts = [f"{len(cleared)} fix(es) hold up"]
        if held_stable:
            # Layer 2 in one number: flips the run suppressed because the row
            # had already passed and the new finding was not critical.
            parts.append(f"{len(held_stable)} noise suppressed")
        if still:
            parts.append(f"{len(still)} still wrong")
        if newly_settled:
            parts.append(
                f"{len(newly_settled)} settled after 3 cycles — needs manual "
                f"review"
            )
        skipped = len(unevaluated & set(rows))
        if skipped:
            parts.append(f"{skipped} unjudged (request failed)")
        self.notify_status(
            " · ".join(parts)
            + (". f retries the ones still flagged, open a row to edit by hand."
               if still else "."),
            ERROR if still else OK,
        )

    # ── Editing by hand ───────────────────────────────────────────────────────

    def _edited(self, row: int, text: str | None) -> None:
        """
        Write a hand-edited row, or do nothing if the edit was cancelled.

        Written immediately rather than staged: a human wrote this text, so there
        is nothing to review and nobody to review it. `None` is cancel; an empty
        string is refused rather than written, because blanking a cell is what
        Judge reports as a problem in the first place.
        """
        report = self._report
        if text is None or report is None:
            return
        text = text.strip()
        if not text:
            self.notify_status(
                "An empty cell is what Judge flags as a problem — nothing written.",
                WARN,
            )
            return
        if text == (report.translation_for(row) or "").strip():
            self.notify_status("Unchanged — nothing written.", MUTED)
            return

        runner = self._runner or self._make_runner()
        try:
            runner.apply_fix({row: text}, approved=[row])
        except Exception as exc:
            self._error.show(f"{type(exc).__name__}: {exc}", "could not write")
            self.notify_status("That edit could not be written.", ERROR)
            return

        # Recorded exactly as a model's fix is, so the before/after column and
        # the revert both work on a hand edit too.
        self._before.setdefault(row, report.translation_for(row))
        issue = report.issues.get(row)
        if issue is not None:
            self._fixed_issue.setdefault(row, issue)
        report.replace_translation(row, text)
        report.drop(row)
        # New text: any prior verdict is void, and a settled row re-enters
        # the loop — a human rewrite deserves a fresh check.
        self._verdict_stable.discard(row)
        self._settled_rows.discard(row)
        self._fix_cycles.pop(row, None)
        # The file changed: the cached section shape must be re-measured.
        self._section_shape_cache = None
        self._cursor_row = row
        self._fill_grid()
        # The row stays put in its filter, now green. Keep the cursor on it so
        # the follow-up — check it, undo it — needs no travel.
        self._follow_row(row)
        self._paint_rail()
        self.notify_status(
            f"Row {row} written by hand to column "
            f"{ExcelReader.col_letter(self._target.dst_col)} — now green. "
            f"g re-judges it, r reverts it.",
            OK,
        )

    def _follow_row(self, row: int) -> None:
        """
        Keep the cursor on a row after an action, staying in the current filter.

        A repaired row keeps its place in whatever filter it was in (now green),
        so there is no need to switch views — only to make sure the cursor lands
        on it and it is scrolled into sight.
        """
        report = self._report
        if report is None:
            return
        if row not in self.grid.visible_rows:
            # Only when the row genuinely left the current view (a revert out of
            # `fixed`, say) do we fall back to a filter that still holds it.
            if row in self._before:
                target = self._filter if self._filter != "all" else "fixed"
            elif row in report.issues:
                target = "problems"
            else:
                target = "all"
            if target != self._filter:
                self._filter = target
                self._apply_filter()
        self._cursor_row = row
        self._sync_cursor()
        self.refresh_keys()

    # ── Reverting ─────────────────────────────────────────────────────────────

    def action_revert_row(self) -> None:
        """
        Put back what the row said before it was repaired.

        A fix can be worse than what it replaced, and until now the only way back
        was to remember the old text and retype it. The pre-fix text is already
        kept for the comparison column, so the undo costs nothing to offer — and
        the flag comes back with it, because the problem is in the file again.
        """
        row = self._current_row()
        if not row or self._busy_batch or self._busy_row:
            return
        if row not in self._before:
            self.notify_status("That row has not been repaired.", MUTED)
            return
        original = self._before[row]
        self.app.push_screen(
            ConfirmRevertModal(
                row=row,
                current=(self._report.translation_for(row) if self._report else ""),
                original=original,
            ),
            lambda go, row=row: self._revert_confirmed(row, bool(go)),
        )

    def _revert_confirmed(self, row: int, go: bool) -> None:
        report = self._report
        if not go or report is None or row not in self._before:
            return
        original = self._before[row]
        runner = self._runner or self._make_runner()
        try:
            # An empty original is written as an empty cell deliberately: that is
            # what the file held, and a revert that silently kept the repair
            # would be a worse surprise than a blank.
            runner.apply_fix({row: original or " "}, approved=[row])
        except Exception as exc:
            self._error.show(f"{type(exc).__name__}: {exc}", "could not write")
            self.notify_status("That row could not be reverted.", ERROR)
            return

        report.replace_translation(row, original)
        # The row is no longer repaired, so it leaves the comparison column and
        # the `fixed` filter along with it.
        del self._before[row]
        issue = self._fixed_issue.pop(row, None)
        if issue is not None:
            report.restore(issue)
        self._cursor_row = row
        if self._filter == "fixed" and not self._before:
            # The chip this view is built on has just gone; staying would leave
            # an empty grid with no way to tell why.
            self._filter = "problems"
        self._fill_grid()
        self._follow_row(row)
        self._paint_rail()
        self.notify_status(
            f"Row {row} reverted to what it said before the fix"
            + (" — flagged again." if issue is not None else "."),
            WARN,
        )

    # ── Leaving ───────────────────────────────────────────────────────────────

    def action_confirm_save(self) -> None:
        """
        Confirm & Save (UET_UX §6): show Problems / Fixed / Remaining, then go
        to the summary.

        Applied fixes are already written to the file — `apply_fix` is the only
        writer and runs when a fix is approved — so this is a confirmation and a
        way out, not a second write. It states what still stands so leaving is a
        decision rather than a drift.
        """
        report = self._report
        if report is None or self._busy_batch or self._busy_row:
            return
        fixed = len(self._before)
        remaining = len(report.problem_rows)
        self.app.push_screen(
            ConfirmSaveModal(problems=remaining + fixed, fixed=fixed,
                             remaining=remaining),
            self._confirm_save_done,
        )

    def _confirm_save_done(self, go: bool | None) -> None:
        if not go or self._report is None:
            return
        self.app.push_screen(JudgeSummaryScreen(self._report, self._target))

    def action_summary(self) -> None:
        if self._report is not None:
            self.app.push_screen(JudgeSummaryScreen(self._report, self._target))

    def action_leave(self) -> None:
        self._stop.set()
        if self._busy_batch:
            self._run_state = "cancelled"
        self.app.on_shutdown_hook = None
        if len(self.app.screen_stack) > 1:
            self.app.pop_screen()


# ── Fix All setup ─────────────────────────────────────────────────────────────

@dataclass
class FixAllPlan:
    """
    What a confirmed Fix All is going to do.

    Produced by the wizard, consumed by `JudgeRunScreen._fix_all_confirmed`. A
    plain record rather than a pile of arguments, so the screen can show what is
    about to happen and the runner can be handed exactly that.

    `batch_size` defaults to 0, meaning "ask Settings": the wizard always fills
    it in, and a default captured at class-definition time would freeze whatever
    the pref was when the module was imported.
    """

    model_slot: tuple[str, str, str]
    rows: list[int] = field(default_factory=list)
    criteria: list[str] = field(default_factory=list)
    severities: list[str] = field(default_factory=list)
    batch_size: int = 0
    workers: int = 1

    @property
    def model_id(self) -> str:
        return self.model_slot[2]

    @property
    def model_label(self) -> str:
        """
        The name to print. For a combo the slot carries a uuid, so it is resolved
        to the combo's name — a hex string in a status line tells nobody anything.
        """
        provider_id, _connection_id, model_id = self.model_slot
        if provider_id == COMBO_PROVIDER_ID:
            combo = find_combo(model_id)
            return f"Group · {combo.label}" if combo else "Group · (missing)"
        return model_id

    @property
    def rows_per_request(self) -> int:
        """The size to send, resolving 0 to the saved default."""
        return self.batch_size or _fix_batch_default()


class FixAllSetupScreen(ModalScreen["FixAllPlan | None"]):
    """
    The popup between pressing Fix All and anything being sent (UET_UX §6).

    A popup rather than a screen: the Judge Results panel stays visible behind
    it, so scope and model are chosen without losing sight of the report they
    act on — the same overlay treatment single-row Fix already gets.

    Fix All spends real tokens and rewrites lines a human may not have read, so
    it does not start on a keystroke. Four decisions live here:

      **model** — repairing is a different job from reviewing, and a model that
        grades well is not always the one that writes well. Defaults to the
        Judge model, because that is the least surprising answer and often right.
      **categories and severities** — two filters, ANDed. Narrowing is the point:
        "fix the critical placeholder problems and leave the register alone" is a
        sentence a user can mean, and without both filters it takes three passes.
      **rows per request** — opens at the Settings default (20). A repair reply
        is a whole rewritten line per row, so a model asked for fifty at once
        starts merging and dropping them; lower it for a model that does, raise
        it for one that copes. What is typed here applies to this run only —
        changing the default is a Settings act (CR-002 FR-613).
      **workers** — pre-filled from the rate limit arithmetic, editable because
        the derivation assumes a request duration it cannot measure.

    The severity block is hidden entirely when the evaluation was not graded.
    Offering a filter that cannot match anything is worse than not offering it:
    it looks broken.
    """

    DEFAULT_CSS = """
    FixAllSetupScreen .modal { width: 76; height: auto; max-height: 95%; }
    FixAllSetupScreen #fix-all-body { height: auto; max-height: 1fr; }
    FixAllSetupScreen #fix-step2 { height: auto; }
    FixAllSetupScreen #fix-foot { dock: bottom; height: auto; }
    FixAllSetupScreen #fix-actions { height: auto; border-top: solid #2b323f; }
    """

    BINDINGS = [
        Binding("a", "select_all", "all", show=False),
        Binding("n", "select_none", "none", show=False),
        Binding("escape", "cancel", "cancel", show=False),
    ]

    def __init__(
        self,
        report: JudgeReport,
        rows: list[int],
        model_slot: tuple[str, str, str],
        target: JudgeTarget,
        scope: str = "",
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._report = report
        self._offered = list(rows)
        self._model_slot = model_slot
        self._target = target
        self._scope = scope
        self._graded = report.graded and bool(report.summary.severity_counts)
        # Two steps: 1 = what to fix (categories + severity), 2 = how (model +
        # processing). Kept as a popup with two panes rather than two screens, so
        # the Judge Results panel stays behind it the whole time.
        self._step = 1

    # ── Layout ────────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label("Fix All", classes="title")
            scope = f" · {self._scope}" if self._scope else ""
            yield Label(
                f"{len(self._offered)} flagged row(s){scope}",
                classes="hint",
            )

            with VerticalScroll(id="fix-all-body"):
                # Step 1: one combined checkbox list — categories and severity in
                # a single Picker, so ↑↓ walks the whole thing without Tab.
                self._filters = Picker(multi_select=True)
                yield self._filters

                # Step 2: how to fix.
                with Vertical(id="fix-step2"):
                    self._model = Picker(click_activates=False)
                    yield self._model
                    self._batch = LabeledInput(
                        "Rows / request",
                        value=str(_fix_batch_default()),
                        default=str(_fix_batch_default()),
                    )
                    self._workers = LabeledInput("Workers")
                    yield self._batch
                    yield self._workers
                    self._remember = Picker([
                        Option("Don't ask again for this model", "remember"),
                    ], multi_select=True)
                    yield self._remember
                    yield Static("", id="fix-scope", classes="hint")

            with Vertical(id="fix-foot"):
                self._error = ErrorBox()
                yield self._error
                self._confirm = Picker([], id="fix-actions")
                yield self._confirm
                self._hints = KeyHints([])
                yield self._hints

    def on_mount(self) -> None:
        self._filters.set_options(self._filter_options())
        self._model.set_options(self._model_options())
        self._workers.value = str(self._derived_workers())
        self._apply_remembered()
        self._show_step(1)

    def _filter_options(self) -> list[Option]:
        """Categories then severity, in one list, with disabled section rows."""
        options: list[Option] = [
            Option("Problem categories", "_cat_head", enabled=False, style=MUTED),
        ]
        options += [
            Option(f"  {opt.label}", f"cat:{opt.value}", hint=opt.hint,
                   selected=opt.selected)
            for opt in self._criteria_options() if opt.value
        ]
        if self._graded:
            options.append(
                Option("Severity", "_sev_head", enabled=False, style=MUTED)
            )
            options += [
                Option(f"  {opt.label}", f"sev:{opt.value}", hint=opt.hint,
                       selected=opt.selected, style=opt.style)
                for opt in self._severity_options() if opt.value
            ]
        return options

    def _show_step(self, step: int) -> None:
        self._step = step
        self._filters.display = step == 1
        self.query_one("#fix-step2").display = step == 2
        self._error.clear()
        if step == 1:
            self._confirm.set_options([
                Option("Next →", "next", style=OK),
                Option("Cancel", "cancel"),
            ])
            self._hints.set_keys([
                ("↑↓", "move"), ("space", "tick"), ("a", "all"), ("n", "none"),
                ("⏎", "next"), ("esc", "cancel"),
            ])
            self._filters.focus()
        else:
            self._confirm.set_options([
                Option("Start fixing", "go", style=OK),
                Option("← Back", "back"),
                Option("Cancel", "cancel"),
            ])
            self._hints.set_keys([
                ("↑↓", "move"), ("tab", "field"), ("⏎", "start"), ("esc", "cancel"),
            ])
            self._paint_scope()
            self._model.focus()

    def _next_step(self) -> None:
        """Validate the categories/severity choice, then move to the model step."""
        self._error.clear()
        if not self._chosen_criteria():
            self._error.show("Tick at least one category.", "nothing to fix")
            return
        if self._graded and not self._chosen_severities():
            self._error.show("Tick at least one severity.", "nothing to fix")
            return
        if not self._matching_rows():
            self._error.show("No rows match those filters.", "nothing to fix")
            return
        self._show_step(2)

    # ── Options ───────────────────────────────────────────────────────────────

    def _model_options(self) -> list[Option]:
        """
        Every validated model, plus every combo, with the Judge model first.

        Listed flat rather than as three columns: this is a single choice on a
        screen that already has four, and the provider/connection cascade would
        dominate it. Combos are listed at the end of the models for the same
        reason they are a separate column entry elsewhere — they are a different
        kind of thing, and mixing them in would hide that.
        """
        options: list[Option] = []
        current = self._model_slot
        for provider in self.app.registry.providers:
            for connection in provider.connections:
                for model in connection.models:
                    slot = (provider.id, connection.id, model.id)
                    is_judge = slot == current
                    options.append(Option(
                        model.label + ("   (judge model)" if is_judge else ""),
                        slot,
                        hint=f"{provider.name} · {connection.name} · "
                             f"{model.preset.label()}",
                        style=OK if is_judge else "",
                    ))
        for combo in load_combos():
            if not combo.members:
                # Offering an empty group here would be offering a repair that
                # cannot run; the Model Groups screen is where it gets fixed.
                continue
            slot = (COMBO_PROVIDER_ID, "", combo.id)
            is_judge = slot == current
            options.append(Option(
                f"Group · {combo.label}"
                + ("   (judge model)" if is_judge else ""),
                slot,
                hint=f"{combo.size} model(s) · "
                     f"{combo.strategy.replace('_', '-')}",
                style=OK if is_judge else ACCENT,
            ))
        options.sort(key=lambda option: option.value != current)
        return options or [
            Option("No models available — add one under Providers & Models", None,
                   enabled=False, style=MUTED)
        ]

    def _criteria_options(self) -> list[Option]:
        """
        One row per category *present in this report*, with its count.

        A category nothing was flagged for would be a checkbox that cannot change
        the outcome, so it is left out rather than shown ticked and inert.
        """
        distribution = self._report.summary.distribution
        options: list[Option] = []
        for name in CRITERIA:
            count = distribution.get(name, 0)
            if not count:
                continue
            options.append(Option(
                f"{CRITERION_LABELS[name]}",
                name,
                hint=f"{count} row(s)",
                selected=True,
            ))
        return options or [
            Option("No repairable categories in this report", None,
                   enabled=False, style=MUTED)
        ]

    def _severity_options(self) -> list[Option]:
        """
        One row per severity present, worst first.

        `critical` and `moderate` start ticked and `minor` does not: a stylistic
        preference is the one class of "problem" where a repair is most likely to
        churn a line for no gain, so opting into it should be deliberate.
        """
        counts = self._report.summary.severity_counts
        options: list[Option] = []
        for name in SEVERITIES:
            count = counts.get(name, 0)
            if not count:
                continue
            options.append(Option(
                SEVERITY_LABELS[name],
                name,
                hint=f"{count} row(s)",
                selected=name != "minor",
                style=_SEVERITY_STYLES.get(name, ""),
            ))
        return options

    # ── Live scope ────────────────────────────────────────────────────────────

    def _chosen_criteria(self) -> list[str]:
        return [
            option.value[4:] for option in self._filters.options
            if option.selected and option.enabled
            and isinstance(option.value, str) and option.value.startswith("cat:")
        ]

    def _chosen_severities(self) -> list[str]:
        if not self._graded:
            return []
        return [
            option.value[4:] for option in self._filters.options
            if option.selected and option.enabled
            and isinstance(option.value, str) and option.value.startswith("sev:")
        ]

    def _chosen_slot(self) -> tuple[str, str, str] | None:
        option = self._model.current
        return option.value if option and option.value else None

    def _matching_rows(self) -> list[int]:
        """Rows that pass both filters, within what the grid filter offered."""
        offered = set(self._offered)
        return [
            row for row in self._report.rows_matching(
                self._chosen_criteria(), self._chosen_severities()
            )
            if row in offered
        ]

    def _typed_batch(self) -> int:
        try:
            return max(1, int(self._batch.value))
        except (ValueError, AttributeError):
            return _fix_batch_default()

    def _derived_workers(self) -> int:
        """
        What this model's rate limit affords at the chosen batch size.

        Recomputed as the batch changes, because the two are not independent: a
        bigger repair request costs more tokens, so a TPM allowance affords fewer
        of them. For a combo it is the combo's own limit — the same rule as
        everywhere else, including "0 means no limit, so the parallel cap decides".
        """
        slot = self._chosen_slot()
        if slot is None:
            return 1
        choice = resolve_choice(self.app.registry, *slot)
        rows = self._typed_batch()
        # A repair carries source, translation and the rewritten line, so it is
        # charged more per row than a translate batch of the same size.
        per_row = config.TOKEN_EST_MIN_PER_ROW * 2
        return choice.preset.workers_for(rows, tok_per_row=per_row)

    def _paint_scope(self) -> None:
        panel = self.query_one("#fix-scope", Static)
        rows = self._matching_rows()
        slot = self._chosen_slot()
        batch = self._typed_batch()
        requests = -(-len(rows) // max(1, batch)) if rows else 0

        lines = [
            f"[{MUTED}]Rows[/]      {len(rows):,} of {len(self._offered):,} offered",
            f"[{MUTED}]Requests[/]  {requests} at {batch} row(s) each",
        ]
        if slot is not None:
            choice = resolve_choice(self.app.registry, *slot)
            lines.append(
                f"[{MUTED}]Limits[/]    "
                f"{escape_markup(choice.preset.describe_limits(batch, tok_per_row=config.TOKEN_EST_MIN_PER_ROW * 2))}"
            )
            if choice.is_combo and choice.combo is not None:
                lines.append(
                    f"[{MUTED}]Group[/]     {escape_markup(choice.combo.label)} · "
                    f"{choice.combo.size} model(s) · "
                    f"{choice.combo.strategy.replace('_', '-')}"
                )
        if not rows:
            lines.append(
                f"[{WARN}]Nothing matches both filters — widen one of them.[/]"
            )
        panel.update("\n".join(lines))

    # ── Events ────────────────────────────────────────────────────────────────

    def on_picker_highlighted(self, event: Picker.Highlighted) -> None:
        if event.picker is self._model:
            self._apply_remembered()
            self._workers.value = str(self._derived_workers())
            self._paint_scope()

    def _apply_remembered(self) -> None:
        """
        Prefill rows/workers from this model's stored settings, if any.

        Stored numbers win over the rate-limit derivation: they are what the
        user last chose to run with on this exact model.
        """
        slot = self._chosen_slot()
        if slot is None:
            return
        from ...data.app_prefs import remembered_judge_settings

        remembered = remembered_judge_settings(slot)
        if remembered is None:
            return
        self._batch.value = str(remembered["max_rows"])
        self._workers.value = str(remembered["workers"])

    def on_picker_toggled(self, event: Picker.Toggled) -> None:
        self._error.clear()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input is self._batch.input:
            # The worker count follows the batch, so it has to be re-derived
            # rather than left describing a request size that is no longer used.
            self._workers.value = str(self._derived_workers())
            self._paint_scope()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input is self._batch.input:
            self._workers.focus_input()
            return
        if self._step == 2:
            self._start()

    def on_picker_selected(self, event: Picker.Selected) -> None:
        if event.picker is self._confirm:
            value = event.option.value
            if value == "next":
                self._next_step()
            elif value == "back":
                self._show_step(1)
            elif value == "go":
                self._start()
            else:
                self.action_cancel()
            return
        if event.picker is self._filters:
            # Enter on the combined checkbox list means "done choosing" → Next.
            self._next_step()
            return
        if event.picker is self._model:
            self._batch.focus_input()

    def action_select_all(self) -> None:
        if self._step != 1:
            return
        for option in self._filters.options:
            if option.enabled:
                option.selected = True
        self._filters.refresh()

    def action_select_none(self) -> None:
        if self._step != 1:
            return
        for option in self._filters.options:
            option.selected = False
        self._filters.refresh()

    # ── Confirming ────────────────────────────────────────────────────────────

    def _start(self) -> None:
        self._error.clear()
        slot = self._chosen_slot()
        if slot is None:
            self._error.show("Pick a model to repair with.", "cannot start")
            return
        if not self._chosen_criteria():
            self._error.show(
                "Tick at least one category — there is nothing to fix otherwise.",
                "cannot start",
            )
            return
        if self._graded and not self._chosen_severities():
            self._error.show(
                "Tick at least one severity level, or the filter excludes every "
                "row.",
                "cannot start",
            )
            return
        rows = self._matching_rows()
        if not rows:
            self._error.show(
                "No rows match both filters. Widen the categories or the "
                "severities.",
                "nothing to fix",
            )
            return
        try:
            workers = max(1, int(self._workers.value))
        except ValueError:
            self._error.show("Workers must be a number.", "cannot start")
            return

        # Per-model and persisted: ticking stores this model's rows/workers in
        # the app preferences, so the next Fix All that opens on this model is
        # prefilled with them; unticking removes the entry. The scope filters
        # are never remembered (UET_UX §6).
        from ...data.app_prefs import remember_judge_settings

        remember_judge_settings(
            slot, max_rows=self._typed_batch(), workers=workers,
            remember="remember" in self._remember.selected_values(),
        )
        if "remember" in self._remember.selected_values():
            # Session convenience alongside the persisted entry: single-row Fix
            # It reuses the slot without a popup for the rest of the session.
            self.app.judge_fix_slot = slot

        self.dismiss(FixAllPlan(
            model_slot=slot,
            rows=rows,
            criteria=self._chosen_criteria(),
            severities=self._chosen_severities(),
            batch_size=self._typed_batch(),
            workers=workers,
        ))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_back(self) -> None:
        self.dismiss(None)


# ── Review and approve ────────────────────────────────────────────────────────

class JudgeFixReviewScreen(ModalScreen["dict | None"]):
    """
    Staged fixes, before and after, with a checkbox each (AC-103).

    A popup over the Judge Results panel, not a screen change: the model's
    repair is a proposal, and reviewing it should not take you off the report it
    came from. Rows start selected because approving most of them is the common
    case, but nothing is written until Apply, and Apply only writes what is
    still ticked.

    A proposal can also be *amended* here (`e`). A repair that is nearly right is
    the common case where neither ticking nor unticking is the answer.
    """

    DEFAULT_CSS = """
    JudgeFixReviewScreen .modal { width: 96; height: auto; max-height: 95%; }
    JudgeFixReviewScreen #fix-review-body { height: auto; max-height: 1fr; }
    JudgeFixReviewScreen #fix-review-list { height: auto; }
    JudgeFixReviewScreen #fix-detail { margin-top: 1; padding: 0 1; border: round #2b323f; }
    JudgeFixReviewScreen #fix-review-foot { dock: bottom; height: auto; }
    JudgeFixReviewScreen #fix-review-actions { height: auto; border-top: solid #2b323f; }
    """

    BINDINGS = [
        Binding("a", "select_all", "all", show=False),
        Binding("n", "select_none", "none", show=False),
        Binding("e", "edit", "edit", show=False),
        Binding("escape", "discard", "discard", show=False),
    ]

    def __init__(
        self,
        runner: JudgeRunner,
        report: JudgeReport | None,
        staged: StagedFix,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._runner = runner
        self._report = report
        self._staged = staged
        # Rows whose staged text a human changed, so the list can say so: an
        # amended line is no longer the model's proposal and should not be read
        # as one.
        self._amended: set[int] = set()

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            count = len(self._staged.texts)
            yield Label(f"Review {count} proposed fix(es)", classes="title")
            yield Label(
                "Nothing is written yet. Untick what you don't want, e edits a "
                "row, then apply.",
                classes="hint",
            )
            # Scrolls only when the window is too short; the action bar stays out
            # of the scroll so it is always reachable.
            with VerticalScroll(id="fix-review-body"):
                self.picker = Picker(multi_select=True, id="fix-review-list")
                yield self.picker
                yield Static("", id="fix-detail")
                extra = []
                if self._staged.unchanged:
                    extra.append(
                        f"{len(self._staged.unchanged)} row(s) left unchanged on "
                        f"purpose (fixing would change the meaning)."
                    )
                if self._staged.failed:
                    gate_failures = [e for e in self._staged.errors
                                     if "FITTING_STILL_FAIL" in e]
                    if gate_failures:
                        # A gate rejection DID produce text — "produced nothing"
                        # would be a lie. Name the rows and the reason so the
                        # user can fix them by hand (spec 3.5).
                        extra.append(
                            f"{len(gate_failures)} row(s) failed the dialog-window "
                            f"re-measure after retries and are left as they were: "
                            + "; ".join(gate_failures)
                        )
                    else:
                        extra.append(
                            f"{len(self._staged.failed)} row(s) produced nothing and are "
                            f"left as they were."
                        )
                if extra:
                    yield Label(" · ".join(extra), classes="hint")
            with Vertical(id="fix-review-foot"):
                self._error = ErrorBox()
                yield self._error
                self._confirm = Picker([
                    Option("Apply the ticked rows", "apply", style=OK),
                    Option("Discard every fix", "discard"),
                ], id="fix-review-actions")
                yield self._confirm
                yield KeyHints([
                    ("↑↓", "move"), ("space", "toggle"), ("a", "all"), ("n", "none"),
                    ("e", "edit"), ("⏎", "apply"), ("esc", "discard"),
                ])

    def on_mount(self) -> None:
        self._app = self.app
        self.picker.set_options([
            Option(self._row_label(row), row, selected=True)
            for row in self._staged.rows
        ])
        self.picker.focus()
        self._show_detail()

    def _row_label(self, row: int) -> str:
        # Flattened and truncated: a proposed fix can carry a newline, and a
        # picker row that breaks in two would overlap the row under it.
        text = truncate(self._staged.texts.get(row, ""), 60)
        mark = " (edited)" if row in self._amended else ""
        return f"{row:>6}  {text}{mark}"

    def on_picker_highlighted(self, event: Picker.Highlighted) -> None:
        if event.picker is self.picker:
            self._show_detail()

    def _show_detail(self) -> None:
        option = self.picker.current
        panel = self.query_one("#fix-detail", Static)
        if option is None or self._report is None:
            panel.update("")
            return
        row = option.value
        before = self._report.translation_for(row)
        after = self._staged.texts.get(row, "")
        comment = self._report.comment_for(row)
        # `flatten_cell` on each value: this panel is one line per field, and a
        # cell holding a newline would push the next field's label out of line.
        lines = [
            f"[{MUTED}]row {row}[/]",
            f"[{MUTED}]source[/] {escape_markup(flatten_cell(self._report.source_for(row)))}",
            f"[{ERROR}]before[/] {escape_markup(flatten_cell(before))}",
            f"[{OK}]after [/] {escape_markup(flatten_cell(after))}",
        ]
        if comment:
            lines.append(f"[{WARN}]why   [/] {escape_markup(flatten_cell(comment))}")
        if row in self._amended:
            lines.append(f"[{ACCENT}]edited by hand[/]")
        panel.update("\n".join(lines))

    def action_edit(self) -> None:
        """
        Amend the staged text for the row under the cursor.

        Still staged afterwards — this changes the proposal, not the file, so the
        row keeps its checkbox and the write still waits for Apply.
        """
        option = self.picker.current
        if option is None or self._report is None:
            return
        row = option.value
        self.app.push_screen(
            RowEditModal(
                row=row,
                source=self._report.source_for(row),
                text=self._staged.texts.get(row, ""),
                comment=self._report.comment_for(row),
                before=self._report.translation_for(row),
            ),
            lambda text, row=row: self._amend(row, text),
        )

    def _amend(self, row: int, text: str | None) -> None:
        if text is None:
            return
        text = text.strip()
        if not text:
            # Blanking a staged fix means "do not apply this one", which is what
            # the checkbox is for — and writing an empty cell is what Judge flags.
            self.notify_status(
                "An empty fix is not applied — untick the row instead.", WARN
            )
            return
        if text == (self._staged.texts.get(row) or "").strip():
            return
        self._staged.texts[row] = text
        self._amended.add(row)
        # Rebuilt in place, keeping which rows are ticked: rebuilding from
        # scratch would silently re-tick anything the user had turned off.
        selected = {
            option.value for option in self.picker.options if option.selected
        }
        self.picker.set_options([
            Option(self._row_label(number), number, selected=number in selected)
            for number in self._staged.rows
        ], keep_index=True)
        self._show_detail()
        self.notify_status(f"Row {row} edited — still staged, apply to write.", OK)

    def notify_status(self, message: str, style: str = "") -> None:
        # A modal has no footer StatusBar of its own; surface transient notes
        # through the app's toast instead of raising.
        try:
            self.app.notify(message)
        except Exception:
            pass

    def action_select_all(self) -> None:
        for option in self.picker.options:
            option.selected = True
        self.picker.refresh()

    def action_select_none(self) -> None:
        for option in self.picker.options:
            option.selected = False
        self.picker.refresh()

    def on_picker_selected(self, event: Picker.Selected) -> None:
        if event.picker is self._confirm:
            if event.option.value == "apply":
                self._apply()
            else:
                self.action_discard()

    def _apply(self) -> None:
        approved = [
            option.value for option in self.picker.options if option.selected
        ]
        if not approved:
            self._error.show("Nothing is ticked — tick a row or discard.", "cannot apply")
            return
        try:
            written = self._runner.apply_fix(self._staged.texts, approved=approved)
        except Exception as exc:
            # A locked file surfaces here rather than silently losing the fix.
            self._error.show(f"{type(exc).__name__}: {exc}", "could not write")
            return
        applied = {row: self._staged.texts[row] for row in approved}
        self.dismiss(applied)

    def action_discard(self) -> None:
        # Dismissed with nothing, so the caller knows no row was applied and
        # the report keeps every flag it had.
        self.dismiss({})

    def action_back(self) -> None:
        self.action_discard()

    def action_cancel(self) -> None:
        self.action_discard()


# ── Summary ───────────────────────────────────────────────────────────────────

class JudgeSummaryScreen(MenuScreen):
    """
    Score, problem count, error-type distribution — on screen only (FR-114).

    No export, no report file. The decision was deliberate: a saved report goes
    stale the moment a row is fixed, and a stale quality report is worse than
    none (ADR-001).
    """

    breadcrumb = "Judge · summary"
    title_text = "Evaluation summary"

    def __init__(self, report: JudgeReport, target: JudgeTarget, **kwargs) -> None:
        super().__init__(**kwargs)
        self._report = report
        self._target = target

    def compose_extra(self) -> ComposeResult:
        yield Label("")
        yield Static(self._summary(), classes="panel")

    def _summary(self) -> str:
        report = self._report
        summary = report.summary
        correct = summary.evaluated - summary.problem_rows
        lines = [
            f"[bold {ACCENT}]Score[/]  {summary.percent:.1f}%   "
            f"({correct:,} of {summary.evaluated:,} rows had no issue)",
            "",
            f"[{ERROR}]flagged[/]     {summary.problem_rows:>8,}",
            f"[{MUTED}]evaluated[/]   {summary.evaluated:>8,}",
        ]
        if summary.unevaluated:
            lines.append(
                f"[{WARN}]unjudged[/]    {summary.unevaluated:>8,}   "
                f"a request failed — these were never reviewed"
            )
        lines += [
            f"[{MUTED}]───────────────────[/]",
            f"total       {summary.total:>8,}",
            "",
        ]

        if summary.severity_counts:
            # Severity first: it answers "how bad is this file", which is the
            # question anyone opens a summary with.
            lines.append(f"[bold {ACCENT}]Severity[/]")
            width = max(1, summary.problem_rows)
            for name, count in summary.ranked_severities():
                bar = "█" * max(1, round(20 * count / width))
                style = _SEVERITY_STYLES.get(name, WARN)
                lines.append(f"  {name:<15}{count:>5,}  [{style}]{bar}[/]")
            lines.append("")

        if summary.distribution:
            lines.append(f"[bold {ACCENT}]Error types[/]")
            width = max(1, summary.problem_rows)
            for name, count in summary.ranked_distribution():
                bar = "█" * max(1, round(20 * count / width))
                lines.append(f"  {name:<15}{count:>5,}  [{WARN}]{bar}[/]")
            lines.append(
                f"  [{MUTED}]a row can carry more than one type, so these "
                f"may exceed the flagged count[/]"
            )
        else:
            lines.append(f"[{OK}]No issues were reported.[/]")

        lines += [
            "",
            f"[{MUTED}]file        {escape_markup(os.path.basename(self._target.path))}[/]",
            f"[{MUTED}]column      {ExcelReader.col_letter(self._target.dst_col)}[/]",
            f"[{MUTED}]criteria    {escape_markup(', '.join(report.criteria))}[/]",
            f"[{MUTED}]grading     {report.grading_mode}[/]",
            f"[{MUTED}]tokens      ~{summary.estimated_tokens:,} estimated · "
            f"{summary.tokens_used:,} used[/]",
            "",
            f"[{MUTED}]This report is on screen only — nothing was written to "
            f"disk, and no fix was applied without approval.[/]",
        ]
        return "\n".join(lines)

    def build_options(self) -> list[Option]:
        return [
            Option("Back to the results", "back"),
            Option("Judge Again", "again",
                   hint="a fresh full evaluation of this file"),
            Option("Back to main menu", "home"),
        ]

    def screen_keys(self) -> list[tuple[str, str]]:
        return [("↑↓", "move"), ("⏎", "select"), ("←", "back")]

    def handle_choice(self, option: Option) -> None:
        if option.value == "home":
            self.app.go_home()
        elif option.value == "again":
            # A full re-evaluation from scratch, carrying the file and columns so
            # the setup only asks for a model again (UET_UX §6 Judge Again).
            job = JobConfig(
                provider_id="",
                model_id="",
                src_path=self._target.path,
                out_path=self._target.path,
                src_col=self._target.src_col,
                dst_col=self._target.dst_col,
                ctx_col=self._target.ctx_col,
                start_row=self._target.start_row,
                num_rows=self._target.num_rows,
                src_lang=self._target.src_lang,
                tgt_lang=self._target.tgt_lang,
                style_prompt=self._target.style_prompt,
                custom_placeholders=self._target.custom_placeholders,
            )
            self.app.go_home()
            self.app.push_screen(JudgeSetupScreen(job=job))
        else:
            self.app.pop_screen()


class RowEditModal(ModalScreen[str | None]):
    """
    One row, editable by hand.

    The escape hatch the fix loop needs. A model that has already failed twice on
    a line will fail a third time, and the person reading the file usually knows
    what it should say — so at some point the useful thing is a text box, not
    another request. The source and the AI's complaint stay on screen while the
    text is edited, because they are what the edit is answering.

    Returns the new text, or `None` for cancelled. Enter saves and Ctrl+J makes a
    newline, which is `PasteArea`'s contract everywhere else in the app: a cell
    can legitimately hold more than one line, and the terminal cannot always tell
    Shift+Enter from Enter.
    """

    BINDINGS = [
        Binding("escape", "dismiss_none", "cancel", show=False),
        Binding("ctrl+s", "save", "save", show=False),
    ]

    DEFAULT_CSS = """
    RowEditModal .modal { width: 90; height: auto; max-height: 95%; }
    RowEditModal #edit-scroll { height: auto; max-height: 1fr; }
    RowEditModal #edit-scroll Label { margin-top: 1; }
    RowEditModal #edit-scroll .panel { padding: 0 1; border: round #2b323f; }
    RowEditModal #edit-foot { dock: bottom; height: auto; }
    """

    def __init__(
        self,
        row: int,
        source: str,
        text: str,
        comment: str = "",
        before: str = "",
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._row = row
        self._source = source
        self._text = text
        self._comment = comment
        self._before = before

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label(f"Edit row {self._row}", classes="title")
            with VerticalScroll(id="edit-scroll"):
                yield Label("SOURCE", classes="muted")
                yield Static(escape_markup(self._source or "(empty)"), classes="panel")
                if self._before:
                    # What the row said before it was repaired, so a hand edit can
                    # borrow from the original instead of being typed from nothing.
                    yield Label("BEFORE FIX", classes="error")
                    yield Static(escape_markup(self._before), classes="panel")
                if self._comment:
                    yield Label("WHY IT WAS FLAGGED", classes="warn")
                    yield Static(escape_markup(self._comment), classes="panel")
            with Vertical(id="edit-foot"):
                self._editor = LabeledTextArea(
                    "TRANSLATION",
                    value=self._text,
                    hint="⏎ saves · ctrl+j new line · esc cancels",
                    min_lines=3,
                    max_lines=10,
                )
                yield self._editor
                yield KeyHints([("⏎", "save"), ("ctrl+j", "new line"), ("esc", "cancel")])

    def on_mount(self) -> None:
        self._editor.focus_input()
        # Cursor at the end, not the start: this box opens pre-filled, and typing
        # into it should extend the line rather than push characters in front of
        # it. Someone replacing the whole thing selects or deletes; someone
        # tweaking the tail — the common case — just types.
        try:
            area = self._editor.area
            area.move_cursor(area.document.end)
        except Exception:
            pass

    def on_paste_area_submitted(self, _event: PasteArea.Submitted) -> None:
        self.action_save()

    def action_save(self) -> None:
        self.dismiss(self._editor.value)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class ConfirmRevertModal(ModalScreen[bool]):
    """
    The confirmation a revert gets, showing both texts.

    A revert overwrites a cell, so it is the same class of action as applying a
    fix and gets the same treatment: both versions on screen, and a deliberate
    choice between them. Reading the two side by side is also the only way to
    answer the question that prompted it — whether the repair actually made
    things worse.
    """

    BINDINGS = [Binding("escape", "dismiss_none", "cancel", show=False)]

    DEFAULT_CSS = """
    ConfirmRevertModal .modal { width: 86; height: auto; max-height: 95%; }
    ConfirmRevertModal #revert-scroll { height: auto; max-height: 1fr; }
    ConfirmRevertModal #revert-scroll Label { margin-top: 1; }
    ConfirmRevertModal #revert-scroll .panel { padding: 0 1; border: round #2b323f; }
    ConfirmRevertModal #revert-foot { dock: bottom; height: auto; }
    """

    def __init__(self, row: int, current: str, original: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._row = row
        self._current = current
        self._original = original

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label(f"Revert row {self._row}?", classes="title")
            with VerticalScroll(id="revert-scroll"):
                yield Label("CURRENT (the repair)", classes="ok")
                yield Static(
                    escape_markup(self._current or "(empty)"), classes="panel"
                )
                yield Label("WILL BE RESTORED TO", classes="error")
                yield Static(
                    escape_markup(self._original or "(empty)"), classes="panel"
                )
            with Vertical(id="revert-foot"):
                yield Label(
                    "The row goes back to being flagged, because the problem is "
                    "back in the file.",
                    classes="hint",
                )
                self.picker = Picker([
                    Option("Revert it", "go", style=WARN),
                    Option("Keep the repair", "keep"),
                ])
                yield self.picker
                yield KeyHints([("↑↓", "move"), ("⏎", "select"), ("esc", "cancel")])

    def on_mount(self) -> None:
        self.picker.focus()

    def on_picker_selected(self, event: Picker.Selected) -> None:
        self.dismiss(event.option.value == "go")

    def action_dismiss_none(self) -> None:
        self.dismiss(False)


class ConfirmSaveModal(ModalScreen[bool]):
    """
    Confirm and Save the evaluation (UET_UX §6).

    States what still stands — Problems, Fixed, Remaining — so leaving the
    results screen is a decision rather than a drift. Applied fixes are already
    on disk (`apply_fix` is the only writer), so this confirms and exits to the
    summary; it never writes a second time.
    """

    BINDINGS = [Binding("escape", "dismiss_none", "cancel", show=False)]

    def __init__(self, problems: int, fixed: int, remaining: int, **kwargs) -> None:
        super().__init__(**kwargs)
        self._problems = problems
        self._fixed = fixed
        self._remaining = remaining

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label("Confirm and save", classes="title")
            yield Static(escape_markup(
                f"Problems   {self._problems:>6,}\n"
                f"Fixed      {self._fixed:>6,}\n"
                f"Remaining  {self._remaining:>6,}\n\n"
                "Approved fixes are already written to the file. Remaining "
                "rows are still flagged — you can keep fixing, or save and "
                "review the summary."
            ), classes="panel")
            self.picker = Picker([
                Option("Save and view summary", "go", style=OK),
                Option("Keep working", "stay"),
            ])
            yield self.picker
            yield KeyHints([("↑↓", "move"), ("⏎", "select"), ("esc", "cancel")])

    def on_mount(self) -> None:
        self.picker.focus()

    def on_picker_selected(self, event: Picker.Selected) -> None:
        self.dismiss(event.option.value == "go")

    def action_dismiss_none(self) -> None:
        self.dismiss(False)
