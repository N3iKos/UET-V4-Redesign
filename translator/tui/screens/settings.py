"""
tui/screens/settings.py — Settings: defaults, vault password, paths, about.

Two kinds of thing live here, and the split matters. The vault password is a
security operation. The *defaults* — processing mode, Judge grading mode, the
Judge request sizes, and how many rows a Fix All request opens with — are
preferences: they change what a new job or a new evaluation starts from, never
what a job already running does, and they are stored outside the vault as plain
JSON (`data/app_prefs.py`) because none of it is secret.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, Static

from ... import config
from ...data import app_prefs
from ..app import BaseScreen, MenuScreen
from ..theme import ACCENT, ERROR, MUTED, OK, WARN
from ..widgets.common import KeyHints, LabeledInput, Option, Picker

# What each mode means, in one line, because picking one is a rate-limit
# decision rather than a cosmetic one.
MODE_HINTS = {
    "sequential": "one batch at a time — slowest, safest",
    "parallel":   "workers take batches as they free up — fastest (default)",
    "wave":       "workers move in lockstep, one cooldown per wave",
}

# How Judge grades a flagged row. The trade is triage against cost: severity
# asks the model for one more judgement per row and gives back the ability to
# repair only what would actually confuse a reader.
GRADING_HINTS = {
    "severity": "also rank each problem critical / moderate / minor (default)",
    "none":     "flag problems without ranking them — cheaper, no triage",
}


def _fallback_hint(value: int) -> str:
    """One line describing what the current fallback range does."""
    if value <= 0:
        return "off — wait forever for the locked file (old behaviour)"
    return (
        f"{value} — if the output is open elsewhere, save to "
        f"name_1 … name_{value} instead"
    )


def _chain_hint(prefs: dict) -> str:
    """One line describing the dialogue-chain setting's current state."""
    if not prefs.get("judge_chain_context"):
        return "off — each row judged alone (safest against false positives)"
    window = prefs.get("judge_chain_window", 1)
    return (
        f"on — rows read as consecutive dialogue; re-judge shows {window} "
        f"neighbour(s) per side as context"
    )


def _translate_chain_hint(prefs: dict) -> str:
    """One line describing the translate batch-continuity setting."""
    if not prefs.get("translate_batch_context"):
        return "off — each line translated independently (current default)"
    return "on — one batch's lines keep terminology and tone together"


COMMENT_STYLE_HINTS = {
    "auto": "AI picks per finding: exact words when clear, critique for style",
    "explicit": "always suggest the exact replacement words — most precise fixes",
    "critical": "critique only, never dictate words — the fix model rewrites freely",
}


def _comment_style_hint(prefs: dict) -> str:
    """One line describing the comment-style setting's current mode."""
    style = prefs.get("judge_comment_style", "auto")
    return f"{style} — {COMMENT_STYLE_HINTS.get(style, '')}"


def _failover_hint(prefs: dict) -> str:
    """One line describing the priority failover setting's current mode."""
    if not prefs.get("priority_failover_enabled", True):
        return "off — failed batches do not switch models"
    retries = prefs.get("failover_retry_threshold", 3)
    return f"on — switch to Priority #1, #2... after {retries} failed retries"


def _dedup_hint(prefs: dict) -> str:
    mode = prefs.get("dedup_mode", "unique_file")
    return {
        "unique_file": "Unique File — extract unique rows to working file & sync back (default)",
        "persistent": "Persistent TM — cross-file SQLite cache (~/.config/.../tm.db)",
        "disabled": "Disabled — translate every row independently",
    }.get(mode, mode)


class SettingsScreen(MenuScreen):
    breadcrumb = "Settings"
    title_text = "Settings"

    def build_options(self) -> list[Option]:
        prefs = app_prefs.load_prefs()
        mode = prefs["processing_mode_default"]
        grading = prefs["judge_grading_mode"]

        return [
            # ── 1. Translation ────────────────────────────────────────────────
            Option("TRANSLATION", "_grp_translate", enabled=False, style=ACCENT),
            Option("  Default processing mode", "mode",
                   hint=f"{mode} — {MODE_HINTS.get(mode, '')}"),
            Option("  Translate batch continuity", "translate_chain",
                   hint=_translate_chain_hint(prefs)),
            Option("  Deduplication & TM", "dedup_mode",
                   hint=_dedup_hint(prefs)),
            Option("  Preferred rows/request", "preferred_rows",
                   hint=f"{prefs['preferred_rows_per_request']} rows — what Processing aims for"),
            Option("  Maximum rows/request", "max_rows_cap",
                   hint=f"{prefs['max_rows_per_request_cap']} rows — ceiling never auto-suggested above"),
            Option("", "_gap1", enabled=False),

            # ── 2. Judge & Repair ─────────────────────────────────────────────
            Option("JUDGE & REPAIR", "_grp_judge", enabled=False, style=ACCENT),
            Option("  Judge grading mode", "grading",
                   hint=f"{grading} — {GRADING_HINTS.get(grading, '')}"),
            Option("  Judge comment style", "comment_style",
                   hint=_comment_style_hint(prefs)),
            Option("  Judge dialogue-chain context", "judge_chain",
                   hint=_chain_hint(prefs)),
            Option("  Max rows per request", "rows",
                   hint=f"{prefs['max_rows_per_request']} rows — Judge batching limit"),
            Option("  Default rows per Fix All", "fix_rows",
                   hint=f"{prefs['fix_batch_rows']} rows — what Fix All opens with"),
            Option("", "_gap2", enabled=False),

            # ── 3. Network & System ───────────────────────────────────────────
            Option("NETWORK & SYSTEM", "_grp_system", enabled=False, style=ACCENT),
            Option("  Priority failover", "priority_failover",
                   hint=_failover_hint(prefs)),
            Option("  Request timeout before retry", "request_timeout",
                   hint=f"{prefs['request_timeout']}s — cancelled and retried after this"),
            Option("  Max tokens per request", "tokens",
                   hint=f"{prefs['max_tokens_per_request']:,} — Context Exceeded safety net"),
            Option("  Locked-output fallback range", "fallback_range",
                   hint=_fallback_hint(prefs["fallback_save_range"])),
            Option("", "_gap3", enabled=False),

            # ── 4. Security & App ─────────────────────────────────────────────
            Option("SECURITY & APP", "_grp_app", enabled=False, style=ACCENT),
            Option("  Change vault password", "password"),
            Option("  Lock vault and quit", "lock"),
            Option("  About", "about"),
        ]

    def compose_extra(self) -> ComposeResult:
        yield Label("")
        yield Label(f"Vault file   {config.VAULT_FILE}", classes="hint")
        yield Label(f"Styles       {config.STYLE_PRESETS_FILE}", classes="hint")
        yield Label(f"Playground   {config.PLAYGROUND_PREFS_FILE}", classes="hint")
        yield Label(f"App prefs    {config.APP_PREFS_FILE}", classes="hint")
        yield Label(f"Fail marker  {config.FAIL_MARKER}", classes="hint")
        yield Label(
            f"Defaults     batch {config.DEFAULT_BATCH_SIZE} · "
            f"temp {config.DEFAULT_TEMPERATURE} · "
            f"retries {config.DEFAULT_MAX_RETRIES} · "
            f"fix rows {config.DEFAULT_FIX_BATCH_ROWS} · "
            f"fallback {config.DEFAULT_FALLBACK_SAVE_RANGE}",
            classes="hint",
        )

    def handle_choice(self, option: Option) -> None:
        if option.value == "mode":
            self.app.push_screen(ProcessingModeModal(), self._mode_done)
        elif option.value == "grading":
            self.app.push_screen(GradingModeModal(), self._grading_done)
        elif option.value == "comment_style":
            self.app.push_screen(CommentStyleModal(), self._comment_style_done)
        elif option.value == "judge_chain":
            self.app.push_screen(JudgeChainModal(), self._judge_chain_done)
        elif option.value == "translate_chain":
            self.app.push_screen(TranslateChainModal(), self._translate_chain_done)
        elif option.value == "dedup_mode":
            self.app.push_screen(
                DedupModeModal(app_prefs.dedup_mode()),
                self._dedup_mode_done,
            )
        elif option.value == "rows":
            self.app.push_screen(MaxRowsModal(), self._rows_done)
        elif option.value == "preferred_rows":
            self.app.push_screen(PreferredRowsModal(), self._preferred_rows_done)
        elif option.value == "max_rows_cap":
            self.app.push_screen(MaxRowsCapModal(), self._max_rows_cap_done)
        elif option.value == "tokens":
            self.app.push_screen(MaxTokensModal(), self._tokens_done)
        elif option.value == "fix_rows":
            self.app.push_screen(FixBatchRowsModal(), self._fix_rows_done)
        elif option.value == "priority_failover":
            self.app.push_screen(PriorityFailoverModal(), self._failover_done)
        elif option.value == "request_timeout":
            self.app.push_screen(RequestTimeoutModal(), self._request_timeout_done)
        elif option.value == "fallback_range":
            self.app.push_screen(FallbackRangeModal(), self._fallback_range_done)
        elif option.value == "password":
            self.app.push_screen(ChangePasswordModal(), self._password_done)
        elif option.value == "lock":
            self.app.registry.lock()
            self.app.exit()
        else:
            self.app.push_screen(AboutScreen())

    def _mode_done(self, mode: str | None) -> None:
        if not mode:
            return
        app_prefs.set_default_processing_mode(mode)
        self.picker.set_options(self.build_options(), keep_index=True)
        self.notify_status(
            f"New jobs default to {mode}. The wizard can still override it.", OK
        )

    def _dedup_mode_done(self, mode: str | None) -> None:
        if not mode:
            return
        stored = app_prefs.set_dedup_mode(mode)
        self.picker.set_options(self.build_options(), keep_index=True)
        self.notify_status(f"Deduplication mode set to {stored}.", OK)

    def _grading_done(self, mode: str | None) -> None:
        if not mode:
            return
        app_prefs.set_judge_grading_mode(mode)
        self.picker.set_options(self.build_options(), keep_index=True)
        note = (
            "New evaluations rank each problem, and Fix All can filter by severity."
            if mode == "severity"
            else "New evaluations flag problems without ranking them."
        )
        self.notify_status(note, OK)

    def _comment_style_done(self, style: str | None) -> None:
        if not style:
            return
        stored = app_prefs.set_judge_comment_style(style)
        self.picker.set_options(self.build_options(), keep_index=True)
        notes = {
            "auto": "The judge picks per finding: exact words when the fix is "
                    "clear, critique only for style.",
            "explicit": "Judge comments always name the exact replacement "
                        "words — repairs are precise, but watch for "
                        "suggestion ping-pong on style rows.",
            "critical": "Judge comments critique only and never dictate "
                        "words — the fix model rewrites freely.",
        }
        self.notify_status(notes.get(stored, stored), OK)

    def _rows_done(self, value: int | None) -> None:
        if not value:
            return
        stored = app_prefs.set_max_rows_per_request(value)
        self.picker.set_options(self.build_options(), keep_index=True)
        note = f"Judge requests are split at max {stored} rows."
        if stored != value:
            note += f" ({value} was outside the allowed range {config.MIN_MAX_ROWS}-{config.MAX_MAX_ROWS}.)"
        self.notify_status(note, OK if stored == value else WARN)

    def _preferred_rows_done(self, value: int | None) -> None:
        if value is None:
            return
        stored = app_prefs.set_preferred_rows_per_request(value)
        self.picker.set_options(self.build_options(), keep_index=True)
        note = f"Processing aims for {stored} rows/request."
        if stored != value:
            note += (
                f" ({value} was outside the allowed range "
                f"{config.MIN_PREFERRED_ROWS}-{config.MAX_PREFERRED_ROWS}.)"
            )
        self.notify_status(note, OK if stored == value else WARN)

    def _max_rows_cap_done(self, value: int | None) -> None:
        if value is None:
            return
        stored = app_prefs.set_max_rows_per_request_cap(value)
        self.picker.set_options(self.build_options(), keep_index=True)
        note = f"Processing never auto-suggests above {stored} rows/request."
        if stored != value:
            note += (
                f" ({value} was outside the allowed range "
                f"{config.MIN_PREFERRED_ROWS}-{config.MAX_PREFERRED_ROWS}.)"
            )
        self.notify_status(note, OK if stored == value else WARN)

    def _tokens_done(self, value: int | None) -> None:
        if not value:
            return
        stored = app_prefs.set_max_tokens_per_request(value)
        self.picker.set_options(self.build_options(), keep_index=True)
        note = f"Judge requests are split at {stored:,} tokens."
        if stored != value:
            note += f" ({value:,} was outside the allowed range.)"
        self.notify_status(note, OK if stored == value else WARN)

    def _fix_rows_done(self, value: int | None) -> None:
        # 0 is a real answer here and means "clamp me to 1" — so the guard is
        # `is None`, not falsiness, or typing 0 would silently change nothing.
        if value is None:
            return
        stored = app_prefs.set_fix_batch_rows(value)
        self.picker.set_options(self.build_options(), keep_index=True)
        note = f"Fix All opens with {stored} row(s) per request."
        if stored != value:
            note += (
                f" ({value} was outside the allowed range "
                f"{config.MIN_FIX_BATCH_ROWS}-{config.MAX_FIX_BATCH_ROWS}.)"
            )
        self.notify_status(note, OK if stored == value else WARN)

    def _failover_done(self, result: "tuple[str, int | None] | None") -> None:
        if not result:
            return
        mode, retries = result
        enabled = app_prefs.set_priority_failover_enabled(mode == "on")
        if retries is not None and enabled:
            app_prefs.set_failover_retry_threshold(retries)
        self.picker.set_options(self.build_options(), keep_index=True)
        if enabled:
            r = app_prefs.failover_retry_threshold()
            self.notify_status(
                f"Priority failover enabled — batches cascade to Priority #1, #2... "
                f"after {r} failed retries.", OK,
            )
        else:
            self.notify_status(
                "Priority failover disabled — workers stick to primary model.", MUTED,
            )

    def _fallback_range_done(self, value: int | None) -> None:
        # 0 is a real answer — it turns the fallback off — so guard on `is None`,
        # not falsiness, or typing 0 would silently change nothing.
        if value is None:
            return
        stored = app_prefs.set_fallback_save_range(value)
        self.picker.set_options(self.build_options(), keep_index=True)
        if stored <= 0:
            note = "Locked-output fallback off — the writer waits for the file."
        else:
            note = f"A locked output now falls back to name_1 … name_{stored}."
        if stored != value:
            note += (
                f" ({value} was outside the allowed range "
                f"{config.MIN_FALLBACK_SAVE_RANGE}-{config.MAX_FALLBACK_SAVE_RANGE}.)"
            )
        self.notify_status(note, OK if stored == value else WARN)

    def _request_timeout_done(self, value: int | None) -> None:
        # Guard is `is None` rather than falsiness — a timeout is never 0 in
        # practice (the clamp floor is 30), but the convention keeps the
        # callback honest about "cancelled" vs "typed nothing".
        if value is None:
            return
        stored = app_prefs.set_request_timeout(value)
        self.picker.set_options(self.build_options(), keep_index=True)
        note = (
            f"A request that runs past {stored}s is cancelled and retried on "
            f"another attempt/connection."
        )
        if stored != value:
            note += (
                f" ({value} was outside the allowed range "
                f"{config.MIN_REQUEST_TIMEOUT}-{config.MAX_REQUEST_TIMEOUT}.)"
            )
        self.notify_status(note, OK if stored == value else WARN)

    def _password_done(self, changed: bool | None) -> None:
        if changed:
            self.notify_status("Vault password changed.", OK)
        elif changed is False:
            self.notify_status("Old password was wrong — nothing changed.", ERROR)


    def _judge_chain_done(self, result: "tuple[str, int | None] | None") -> None:
        """The chain modal came back: (mode, window) or None."""
        if not result:
            return
        mode, window = result
        enabled = app_prefs.set_judge_chain_context(mode == "on")
        if window is not None and enabled:
            app_prefs.set_judge_chain_window(window)
        self.picker.set_options(self.build_options(), keep_index=True)
        if enabled:
            w = app_prefs.judge_chain_window()
            self.notify_status(
                f"Judge reads rows as consecutive dialogue. Re-judge shows {w} "
                f"neighbour(s) per side as context.", OK,
            )
        else:
            self.notify_status(
                "Judge judges each row alone again — the safest mode against "
                "cross-line false positives.", MUTED,
            )

    def _translate_chain_done(self, mode: str | None) -> None:
        if not mode:
            return
        enabled = app_prefs.set_translate_batch_context(mode == "on")
        self.picker.set_options(self.build_options(), keep_index=True)
        if enabled:
            self.notify_status(
                "Translate batches keep terminology and tone together.", OK,
            )
        else:
            self.notify_status(
                "Translate lines are independent again.", MUTED,
            )


class ProcessingModeModal(ModalScreen[str]):
    """
    The mode a new job starts from (FEATURE-003 FR-304).

    It is a default, not a switch: the wizard still asks per run, and Quick mode
    is what actually consumes this. `parallel` stays the shipped default so an
    existing user who never opens this screen sees no change (AC-304).
    """

    BINDINGS = [Binding("escape", "dismiss_none", "cancel", show=False)]

    def compose(self) -> ComposeResult:
        current = app_prefs.default_processing_mode()
        with Vertical(classes="modal"):
            yield Label("Default processing mode", classes="title")
            yield Label(
                "Used by Quick mode and offered first in the wizard. "
                "A running job is not affected.",
                classes="hint",
            )
            self.picker = Picker([
                Option(
                    name + ("   (current)" if name == current else ""),
                    name,
                    hint=MODE_HINTS[name],
                )
                for name in app_prefs.PROCESSING_MODES
            ])
            yield self.picker
            yield KeyHints([("↑↓", "move"), ("⏎", "save"), ("esc", "cancel")])

    def on_mount(self) -> None:
        current = app_prefs.default_processing_mode()
        for index, name in enumerate(app_prefs.PROCESSING_MODES):
            if name == current:
                self.picker.index = index
                break
        self.picker.focus()

    def on_picker_selected(self, event: Picker.Selected) -> None:
        self.dismiss(event.option.value)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


DEDUP_HINTS = {
    "unique_file": "extract unique rows to working file & sync back (fast, clean review, default)",
    "persistent": "unique file + persistent SQLite Translation Memory cache (~/.config/.../tm.db)",
    "disabled": "process every row independently without deduplication",
}


class DedupModeModal(ModalScreen[str]):
    BINDINGS = [Binding("escape", "dismiss_none", "cancel", show=False)]

    def __init__(self, current: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label("Deduplication & Translation Memory", classes="title")
            yield Label(
                "When files contain duplicate text lines, process unique rows once "
                "and copy the result to all identical rows:",
                classes="hint",
            )
            self.picker = Picker([
                Option(
                    name + ("   (current)" if name == self._current else ""),
                    name,
                    hint=DEDUP_HINTS.get(name, ""),
                )
                for name in app_prefs.DEDUP_MODES
            ])
            yield self.picker
            yield KeyHints([("↑↓", "move"), ("⏎", "save"), ("esc", "cancel")])

    def on_mount(self) -> None:
        for index, name in enumerate(app_prefs.DEDUP_MODES):
            if name == self._current:
                self.picker.index = index
                break
        self.picker.focus()

    def on_picker_selected(self, event: Picker.Selected) -> None:
        self.dismiss(str(event.option.value))

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class GradingModeModal(ModalScreen[str]):
    """
    How Judge grades a flagged row.

    `severity` is the default because a list of 300 flagged rows with no ranking
    leaves the user with the triage problem the evaluation was supposed to
    solve. `none` exists for the runs where that ranking is not worth the extra
    judgement call — and it is one fewer thing for a small model to get wrong.

    Changing this affects the *next* evaluation. A report already on screen keeps
    the mode it was produced with, because its severities (or absence of them)
    came from prompts that have already been sent.
    """

    BINDINGS = [Binding("escape", "dismiss_none", "cancel", show=False)]

    def compose(self) -> ComposeResult:
        current = app_prefs.judge_grading_mode()
        with Vertical(classes="modal"):
            yield Label("Judge grading mode", classes="title")
            yield Label(
                "Applies to the next evaluation. A report already on screen "
                "keeps the grading it was produced with.",
                classes="hint",
            )
            self.picker = Picker([
                Option(
                    name.capitalize() + ("   (current)" if name == current else ""),
                    name,
                    hint=GRADING_HINTS[name],
                )
                for name in app_prefs.GRADING_MODES
            ])
            yield self.picker
            yield Label("")
            yield Static(
                f"[{MUTED}]With severity, Fix All can be pointed at just the "
                f"critical rows — the ones where a player would actually "
                f"misunderstand something — instead of every flagged line.[/]",
                classes="panel",
            )
            yield KeyHints([("↑↓", "move"), ("⏎", "save"), ("esc", "cancel")])

    def on_mount(self) -> None:
        current = app_prefs.judge_grading_mode()
        for index, name in enumerate(app_prefs.GRADING_MODES):
            if name == current:
                self.picker.index = index
                break
        self.picker.focus()

    def on_picker_selected(self, event: Picker.Selected) -> None:
        self.dismiss(event.option.value)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class CommentStyleModal(ModalScreen[str]):
    """
    How the FIX part of a Judge comment may be written.

    Three modes, for comparing repair strategies per project:

      **auto** (default) — the model picks per finding: an exact word
      replacement when the fix is unambiguous, a direction-only critique for
      matters of style.
      **explicit** — the exact replacement is always demanded when specific
      words are at fault. Most precise repairs; most prone to suggestion
      ping-pong when the judge's taste differs from the fix model's.
      **critical** — dictating words is forbidden: critique only, and the fix
      model rewrites freely. No forced word can be wrong, but no repair is
      guaranteed either.

    Applies to the next evaluation. A report already on screen keeps the
    comments it was produced with.
    """

    BINDINGS = [Binding("escape", "dismiss_none", "cancel", show=False)]

    def compose(self) -> ComposeResult:
        current = app_prefs.judge_comment_style()
        with Vertical(classes="modal"):
            yield Label("Judge comment style", classes="title")
            yield Label(
                "Applies to the next evaluation. Change it between runs to "
                "compare which repair strategy suits a project best.",
                classes="hint",
            )
            self.picker = Picker([
                Option("Auto" + ("   (current)" if current == "auto" else ""),
                       "auto",
                       hint=COMMENT_STYLE_HINTS["auto"]),
                Option("Explicit — suggest exact words"
                       + ("   (current)" if current == "explicit" else ""),
                       "explicit",
                       hint=COMMENT_STYLE_HINTS["explicit"]),
                Option("Critical — critique only"
                       + ("   (current)" if current == "critical" else ""),
                       "critical",
                       hint=COMMENT_STYLE_HINTS["critical"]),
            ])
            yield self.picker
            yield KeyHints([("↑↓", "move"), ("⏎", "save"), ("esc", "cancel")])

    def on_mount(self) -> None:
        current = app_prefs.judge_comment_style()
        for index, name in enumerate(("auto", "explicit", "critical")):
            if name == current:
                self.picker.index = index
                break
        self.picker.focus()

    def on_picker_selected(self, event: Picker.Selected) -> None:
        self.dismiss(event.option.value)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class PriorityFailoverModal(ModalScreen["tuple[str, int | None] | None"]):
    """
    Priority Failover settings: On/Off and retry threshold.
    """

    BINDINGS = [Binding("escape", "dismiss_none", "cancel", show=False)]

    def compose(self) -> ComposeResult:
        current = "on" if app_prefs.priority_failover_enabled() else "off"
        with Vertical(classes="modal"):
            yield Label("Priority failover", classes="title")
            yield Label(
                "When a batch fails after reaching the retry threshold, automatically "
                "cascade that worker to Priority #1 model (then #2, etc.)\n"
                "Models with Priority Rank > 0 are configured under Providers & Models.",
                classes="hint",
            )
            self.picker = Picker([
                Option("On" + ("   (current)" if current == "on" else ""),
                       "on",
                       hint="cascade failed batches to priority fallback models (default)"),
                Option("Off" + ("   (current)" if current == "off" else ""),
                       "off",
                       hint="workers stay pinned to the selected primary model"),
            ])
            yield self.picker
            yield Label("")
            self._retries = LabeledInput(
                "Failover after retries (1–5)",
                value=str(app_prefs.failover_retry_threshold()),
                default=str(app_prefs.failover_retry_threshold()),
                hint="How many failed attempts on a model before switching to the next priority model.",
            )
            yield self._retries
            yield KeyHints([("↑↓", "move"), ("tab", "field"),
                            ("⏎", "save"), ("esc", "cancel")])

    def on_mount(self) -> None:
        current = "on" if app_prefs.priority_failover_enabled() else "off"
        self.picker.index = 0 if current == "on" else 1
        self.picker.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._save()

    def on_picker_selected(self, event: Picker.Selected) -> None:
        self._save()

    def _save(self) -> None:
        mode = self.picker.current.value if self.picker.current else "on"
        retries = None
        if mode == "on":
            try:
                retries = int(self._retries.value)
            except ValueError:
                retries = 3
        self.dismiss((mode, retries))

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class JudgeChainModal(ModalScreen["tuple[str, int | None] | None"]):
    """
    The Judge dialogue-chain context switch, plus its window size.

    OFF (the default) keeps the strict per-line isolation: a batch is not a
    passage, and one line's verdict must not lean on another's — that rule
    exists because cross-line reading produced documented false positives.
    ON swaps the prompt for a chain-aware one with a scene-break guard, and a
    re-judge injects the flagged rows' physical neighbours as CONTEXT ONLY
    rows the model may read but never report.

    Returns ("on"|"off", window-or-None), or None when cancelled. The window
    is asked for only when the mode is on.
    """

    BINDINGS = [Binding("escape", "dismiss_none", "cancel", show=False)]

    def compose(self) -> ComposeResult:
        current = "on" if app_prefs.judge_chain_context() else "off"
        with Vertical(classes="modal"):
            yield Label("Judge dialogue-chain context", classes="title")
            yield Label(
                "Applies to the next evaluation and re-judge. Off is the "
                "safest mode against cross-line false positives; on reads the "
                "rows of a request as consecutive dialogue.",
                classes="hint",
            )
            self.picker = Picker([
                Option("Off" + ("   (current)" if current == "off" else ""),
                       "off",
                       hint="each row judged against its own source alone"),
                Option("On" + ("   (current)" if current == "on" else ""),
                       "on",
                       hint="neighbouring lines may be used as context, with a "
                            "scene-break guard"),
            ])
            yield self.picker
            yield Label("")
            self._window = LabeledInput(
                "Neighbour rows per side (when on)",
                value=str(app_prefs.judge_chain_window()),
                default=str(app_prefs.judge_chain_window()),
            )
            yield self._window
            yield KeyHints([("↑↓", "move"), ("tab", "field"),
                            ("⏎", "save"), ("esc", "cancel")])

    def on_mount(self) -> None:
        current = "on" if app_prefs.judge_chain_context() else "off"
        self.picker.index = 0 if current == "off" else 1
        self.picker.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._save()

    def on_picker_selected(self, event: Picker.Selected) -> None:
        self._save()

    def _save(self) -> None:
        mode = self.picker.current.value if self.picker.current else "off"
        window = None
        if mode == "on":
            try:
                window = int(self._window.value)
            except ValueError:
                window = None          # clamped to the stored value by prefs
        self.dismiss((mode, window))

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class TranslateChainModal(ModalScreen[str]):
    """
    Whether a translate batch is told to keep continuity across its rows.

    The rows of one batch are consecutive lines of the file either way; this
    only decides whether the prompt says so. Off keeps the per-line
    independence rule that stops a model from merging or completing lines.
    """

    BINDINGS = [Binding("escape", "dismiss_none", "cancel", show=False)]

    def compose(self) -> ComposeResult:
        current = "on" if app_prefs.translate_batch_context() else "off"
        with Vertical(classes="modal"):
            yield Label("Translate batch continuity", classes="title")
            yield Label(
                "Applies to the next translate run. On asks the model to keep "
                "terminology, names and tone consistent across the lines of "
                "one batch; the anti-merge rules stay on either way.",
                classes="hint",
            )
            self.picker = Picker([
                Option("Off" + ("   (current)" if current == "off" else ""),
                       "off",
                       hint="each line translated independently (default)"),
                Option("On" + ("   (current)" if current == "on" else ""),
                       "on",
                       hint="one batch's lines keep terminology and tone together"),
            ])
            yield self.picker
            yield KeyHints([("↑↓", "move"), ("⏎", "save"), ("esc", "cancel")])

    def on_mount(self) -> None:
        current = "on" if app_prefs.translate_batch_context() else "off"
        self.picker.index = 0 if current == "off" else 1
        self.picker.focus()

    def on_picker_selected(self, event: Picker.Selected) -> None:
        self.dismiss(event.option.value)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class MaxRowsModal(ModalScreen[int]):
    """
    The Judge batching limit in rows (Settings: max_rows_per_request).
    """

    BINDINGS = [Binding("escape", "dismiss_none", "cancel", show=False)]

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label("Max rows per request (Judge)", classes="title")
            yield Label(
                "Judge splits evaluation into requests with at most this many rows. "
                "Token safety cap still applies to prevent Context Exceeded.",
                classes="hint",
            )
            self._value = LabeledInput(
                "Rows",
                value=str(app_prefs.max_rows_per_request()),
                default=str(config.DEFAULT_MAX_ROWS_PER_REQUEST),
                hint=f"{config.MIN_MAX_ROWS}–{config.MAX_MAX_ROWS} rows",
            )
            yield self._value
            yield Label("", id="rows-error", classes="error")
            yield KeyHints([("⏎", "save"), ("esc", "cancel")])

    def on_mount(self) -> None:
        self._value.focus_input()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        try:
            number = int(self._value.value)
        except ValueError:
            self.query_one("#rows-error", Label).update("That must be a number.")
            return
        self.dismiss(number)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class PreferredRowsModal(ModalScreen[int]):
    """Preferred rows/request the shared Processing form aims for (Settings)."""

    BINDINGS = [Binding("escape", "dismiss_none", "cancel", show=False)]

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label("Preferred rows per request (Processing)", classes="title")
            yield Label(
                "The rows/request the shared Processing form aims for. It is a "
                "target, not a floor: when the rate limit only affords fewer "
                "rows safely, the lower figure is recommended instead.",
                classes="hint",
            )
            self._value = LabeledInput(
                "Rows",
                value=str(app_prefs.preferred_rows_per_request()),
                default=str(config.DEFAULT_PREFERRED_ROWS_PER_REQUEST),
                hint=f"{config.MIN_PREFERRED_ROWS}–{config.MAX_PREFERRED_ROWS} rows",
            )
            yield self._value
            yield Label("", id="preferred-error", classes="error")
            yield KeyHints([("⏎", "save"), ("esc", "cancel")])

    def on_mount(self) -> None:
        self._value.focus_input()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        try:
            number = int(self._value.value)
        except ValueError:
            self.query_one("#preferred-error", Label).update("That must be a number.")
            return
        self.dismiss(number)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class MaxRowsCapModal(ModalScreen[int]):
    """Ceiling the shared Processing form never auto-suggests above (Settings)."""

    BINDINGS = [Binding("escape", "dismiss_none", "cancel", show=False)]

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label("Maximum rows per request (Processing)", classes="title")
            yield Label(
                "The Processing form never recommends above this. Typing a "
                "larger figure by hand is still allowed — with a warning.",
                classes="hint",
            )
            self._value = LabeledInput(
                "Rows",
                value=str(app_prefs.max_rows_per_request_cap()),
                default=str(config.DEFAULT_MAX_ROWS_PER_REQUEST_CAP),
                hint=f"{config.MIN_PREFERRED_ROWS}–{config.MAX_PREFERRED_ROWS} rows",
            )
            yield self._value
            yield Label("", id="cap-error", classes="error")
            yield KeyHints([("⏎", "save"), ("esc", "cancel")])

    def on_mount(self) -> None:
        self._value.focus_input()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        try:
            number = int(self._value.value)
        except ValueError:
            self.query_one("#cap-error", Label).update("That must be a number.")
            return
        self.dismiss(number)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class MaxTokensModal(ModalScreen[int]):
    """
    The Judge auto-split ceiling (FEATURE-001 FR-106 / NFR-101).

    Lower it when a provider's context window is small; raise it to send fewer,
    bigger requests. Out-of-range values are clamped rather than rejected, so a
    typo cannot leave the setting empty.
    """

    BINDINGS = [Binding("escape", "dismiss_none", "cancel", show=False)]

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label("Max tokens per request", classes="title")
            yield Label(
                "Judge splits rows into sections so each request's *estimate* "
                "stays under this. A section that overruns is re-split on the "
                "next pass.",
                classes="hint",
            )
            self._value = LabeledInput(
                "Tokens",
                value=str(app_prefs.max_tokens_per_request()),
                default=str(config.MAX_TOKENS_PER_REQUEST),
                hint=f"{config.MIN_MAX_TOKENS:,}–{config.MAX_MAX_TOKENS:,}",
            )
            yield self._value
            yield Label("", id="tokens-error", classes="error")
            yield KeyHints([("⏎", "save"), ("esc", "cancel")])

    def on_mount(self) -> None:
        self._value.focus_input()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        try:
            number = int(self._value.value)
        except ValueError:
            self.query_one("#tokens-error", Label).update("That must be a number.")
            return
        self.dismiss(number)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class FixBatchRowsModal(ModalScreen[int]):
    """
    How many rows a Fix All request opens with (CR-002 FR-612).

    A default, not a limit: the Fix All wizard still asks per run, and a figure
    typed there is sent as typed. Out-of-range values are clamped rather than
    refused — and the clamp is reported — so a typo cannot leave the setting
    empty or send a request of 999 rows.
    """

    BINDINGS = [Binding("escape", "dismiss_none", "cancel", show=False)]

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label("Default rows per Fix All request", classes="title")
            yield Label(
                "A repair reply is a whole rewritten line per row, so a model "
                "asked for too many at once starts merging and dropping them. "
                "Applies to the next Fix All; the wizard can still override it.",
                classes="hint",
            )
            self._value = LabeledInput(
                "Rows",
                value=str(app_prefs.fix_batch_rows()),
                default=str(config.DEFAULT_FIX_BATCH_ROWS),
                hint=f"{config.MIN_FIX_BATCH_ROWS}–{config.MAX_FIX_BATCH_ROWS} rows",
            )
            yield self._value
            yield Label("", id="fix-rows-error", classes="error")
            yield KeyHints([("⏎", "save"), ("esc", "cancel")])

    def on_mount(self) -> None:
        self._value.focus_input()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        try:
            number = int(self._value.value)
        except ValueError:
            self.query_one("#fix-rows-error", Label).update("That must be a number.")
            return
        self.dismiss(number)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class FallbackRangeModal(ModalScreen[int]):
    """
    How many numbered siblings the writer may create when the output is locked.

    When a spreadsheet is open in another program the atomic replace onto it
    fails. Rather than block the run forever, the writer waits a short while on
    the real name and then saves to name_1, name_2, … up to this many — moving
    every later checkpoint to that file so one run never fragments. 0 turns the
    fallback off and restores the wait-forever behaviour. Out-of-range values
    are clamped rather than refused, so a typo cannot empty the setting.
    """

    BINDINGS = [Binding("escape", "dismiss_none", "cancel", show=False)]

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label("Locked-output fallback range", classes="title")
            yield Label(
                "If the output file is open in another program, save to a "
                "numbered sibling (name_1 … name_N) instead of waiting on it. "
                "0 turns this off and waits for the file to be closed.",
                classes="hint",
            )
            self._value = LabeledInput(
                "Range",
                value=str(app_prefs.fallback_save_range()),
                default=str(config.DEFAULT_FALLBACK_SAVE_RANGE),
                hint=f"{config.MIN_FALLBACK_SAVE_RANGE}–{config.MAX_FALLBACK_SAVE_RANGE} "
                     f"(0 = off)",
            )
            yield self._value
            yield Label("", id="fallback-error", classes="error")
            yield KeyHints([("⏎", "save"), ("esc", "cancel")])

    def on_mount(self) -> None:
        self._value.focus_input()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        try:
            number = int(self._value.value)
        except ValueError:
            self.query_one("#fallback-error", Label).update("That must be a number.")
            return
        self.dismiss(number)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class RequestTimeoutModal(ModalScreen[int]):
    """
    How long a single generate request may run before it counts as hung.

    Past this the client raises a read timeout, the retry loop counts a failed
    attempt, and the next attempt may land on a different connection or combo
    member — a model that stopped answering must not hold its worker hostage
    until the user notices a frozen run.

    120s is the default because it is generous for an ordinary chat model on a
    full batch, while a reasoning model on a slow endpoint may legitimately
    need the room the ceiling (600s) allows. Out-of-range values are clamped
    rather than refused, so a typo cannot empty the setting.
    """

    BINDINGS = [Binding("escape", "dismiss_none", "cancel", show=False)]

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label("Request timeout before retry", classes="title")
            yield Label(
                "A request that runs longer than this is cancelled and retried "
                "(up to 3 attempts), then another connection or combo member "
                "takes over. Raise it for slow reasoning models; lower it to "
                "recover faster from a provider that stops mid-reply.",
                classes="hint",
            )
            self._value = LabeledInput(
                "Seconds",
                value=str(app_prefs.request_timeout()),
                default=str(config.DEFAULT_REQUEST_TIMEOUT),
                hint=f"{config.MIN_REQUEST_TIMEOUT}–{config.MAX_REQUEST_TIMEOUT}s "
                     f"(default {config.DEFAULT_REQUEST_TIMEOUT}s)",
            )
            yield self._value
            yield Label("", id="timeout-error", classes="error")
            yield KeyHints([("⏎", "save"), ("esc", "cancel")])

    def on_mount(self) -> None:
        self._value.focus_input()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        try:
            number = int(self._value.value)
        except ValueError:
            self.query_one("#timeout-error", Label).update("That must be a number.")
            return
        self.dismiss(number)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class ChangePasswordModal(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "dismiss_none", "cancel", show=False)]

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label("Change vault password", classes="title")
            self._old = LabeledInput("Current password", password=True)
            self._new = LabeledInput("New password", password=True)
            self._confirm = LabeledInput("Confirm new password", password=True)
            yield self._old
            yield self._new
            yield self._confirm
            yield Label("", id="pw-error", classes="error")
            yield KeyHints([("⏎", "next / apply"), ("esc", "cancel")])

    def on_mount(self) -> None:
        self._old.focus_input()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        order = [self._old, self._new, self._confirm]
        for index, field in enumerate(order):
            if event.input is field.input and index + 1 < len(order):
                order[index + 1].focus_input()
                return

        error = self.query_one("#pw-error", Label)
        if not self._new.input.value:
            error.update("New password cannot be empty.")
            return
        if self._new.input.value != self._confirm.input.value:
            error.update("New passwords do not match.")
            return
        ok = self.app.registry.change_password(
            self._old.input.value, self._new.input.value
        )
        self.dismiss(bool(ok))

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class AboutScreen(BaseScreen):
    breadcrumb = "Settings › About"

    def compose_body(self) -> ComposeResult:
        yield Label("Universal Excel Translator v4", classes="title")
        yield Label(
            "Local, single-user batch translator for spreadsheets.\n"
            "Providers, connections and models are user-defined data — "
            "no provider is hardcoded anywhere in the source, and neither is "
            "any model's reasoning support.\n\n"
            "TUI: Textual · Excel: openpyxl · HTTP: requests\n"
            "Vault: PBKDF2-HMAC-SHA256 + SHA-256 keystream + HMAC tag",
            classes="hint",
        )

    def screen_keys(self) -> list[tuple[str, str]]:
        return [("←", "back")]
