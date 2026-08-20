"""
tui/widgets/processing.py — The one Processing form Translate and Judge share.

Both flows pace a job against the same rate-limit-derived defaults, and both let
a power user override them. Before this widget each flow had its own pacing
screen and they drifted; this is the single form.

The recommendation rules (UET_UX Processing):
  * the form *aims* for the preferred rows/request (Settings, default 15);
  * it never suggests above the maximum (Settings, default 100);
  * when the rate limit only affords fewer rows safely, the lower figure is
    what shows — preferred is a target, not a floor;
  * the recommended value sits dim in the field; Enter accepts it as-is;
  * typing a different number marks a manual override, and an override that
    risks the limit shows a warning but is never blocked.

`advanced=True` (the Translate flow) adds the grouped execution options that the
old Full mode had — processing mode, write mode, anti-desync and the logging /
resume flags — arranged by their group so the hierarchy is readable. Judge does
not use them, so it leaves `advanced` off and the extras never render.
"""

from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, Label, Static

from ... import config
from ...data import app_prefs
from ...providers.ratelimit import RateLimitPreset
from ..theme import ACCENT, MUTED, WARN
from .common import LabeledInput, Option, Picker


@dataclass
class ProcessingValues:
    """What the form resolved to, ready to drop into a draft/JobConfig."""

    rows_per_request: int
    workers: int
    request_delay: float
    manual_override: bool
    # Execution options — only meaningful when the form was built `advanced`.
    # They carry the shipped defaults otherwise, so a caller can read them
    # unconditionally.
    processing_mode: str = config.DEFAULT_PROCESSING_MODE
    write_mode: str = "safe"
    anti_desync: str = "auto"
    debug_log: bool = False
    raw_log: bool = False
    skip_filled: bool = True


def _default_mode() -> str:
    try:
        return app_prefs.default_processing_mode()
    except Exception:
        return config.DEFAULT_PROCESSING_MODE


def recommend(preset: RateLimitPreset, tok_per_row: int | None = None) -> tuple[int, int]:
    """
    (rows/request, workers) recommended for this preset — preferred, but capped
    by what the limit safely affords.
    """
    preferred = app_prefs.preferred_rows_per_request()
    ceiling = app_prefs.max_rows_per_request_cap()
    affordable = max(1, int(preset.suggested_batch_size))
    rows = min(preferred, ceiling)
    if not preset.unlimited:
        rows = min(rows, affordable)
    rows = max(1, rows)
    workers = max(1, preset.workers_for(rows, tok_per_row))
    return rows, workers


class ProcessingForm(Vertical):
    """Rows/request + Workers + (optionally) the grouped execution options."""

    # Grow to the content's height rather than the default `1fr`. Inside the
    # screen's scrollable body a 1fr form would shrink to the viewport and the
    # advanced options below would be clipped with no scrollbar; auto lets the
    # body scroll them (BUG: minimized window had no scrollbar).
    DEFAULT_CSS = """
    ProcessingForm { height: auto; }
    """

    def __init__(
        self,
        preset: RateLimitPreset,
        tok_per_row: int | None = None,
        rows_value: int | None = None,
        workers_value: int | None = None,
        advanced: bool = False,
        draft: dict | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._preset = preset
        self._tok_per_row = tok_per_row
        self._advanced = advanced
        self._draft = draft or {}
        rec_rows, rec_workers = recommend(preset, tok_per_row)
        self._rec_rows = rec_rows
        self._rec_workers = rec_workers
        self._init_rows = rows_value
        self._init_workers = workers_value

    def compose(self) -> ComposeResult:
        yield Label("Processing", classes="title")
        yield Label(
            "Recommended from the rate limit — press Enter to accept, or type "
            "your own. A number past the safe capacity warns but is allowed.",
            classes="hint",
        )
        yield Label("")
        self._rows = LabeledInput(
            "Rows / request",
            value=str(self._init_rows) if self._init_rows else "",
            default=str(self._rec_rows),
            hint=f"recommended {self._rec_rows}",
        )
        self._workers = LabeledInput(
            "Workers",
            value=str(self._init_workers) if self._init_workers else "",
            default=str(self._rec_workers),
            hint=f"recommended {self._rec_workers}",
        )
        yield self._rows
        yield self._workers
        yield Label("")
        yield Static("", id="processing-derived", classes="hint")
        yield Label("", id="processing-warn", classes="warn")

        if self._advanced:
            yield from self._compose_advanced()

    def _compose_advanced(self) -> ComposeResult:
        # Radio groups use click_activates=False so a click only sets the value;
        # the screen's confirm bar (or Enter) is what submits.
        yield Label("")
        yield Label("Processing mode", classes="muted")
        self._mode_picker = Picker([
            Option("parallel", "parallel",
                   hint="workers take batches as they free up — fastest"),
            Option("wave", "wave",
                   hint="lockstep: all workers together, one cooldown per wave"),
            Option("sequential", "sequential", hint="one batch at a time"),
        ], click_activates=False)
        yield self._mode_picker

        yield Label("")
        yield Label("Write mode", classes="muted")
        self._write_picker = Picker([
            Option("safe write (flush every batch)", "safe"),
            Option("buffered write (fewer disk writes)", "buffered"),
        ], click_activates=False)
        yield self._write_picker

        yield Label("")
        yield Label("Anti-desync", classes="muted")
        self._desync_picker = Picker([
            Option("anti-desync: auto", "auto"),
            Option("anti-desync: strict", "strict"),
            Option("anti-desync: off", "off"),
        ], click_activates=False)
        yield self._desync_picker

        yield Label("")
        yield Label("Logging & execution options", classes="muted")
        self._flags = Picker([
            Option("debug log", "debug_log"),
            Option("raw API log", "raw_log"),
            Option("resume — skip rows already filled", "skip_filled", selected=True),
        ], multi_select=True)
        yield self._flags

    def on_mount(self) -> None:
        self._rows.input.placeholder = f"recommended: {self._rec_rows}"
        self._workers.input.placeholder = f"recommended: {self._rec_workers}"
        if self._advanced:
            self._preselect(self._mode_picker, self._draft.get("processing_mode") or _default_mode())
            self._preselect(self._write_picker, self._draft.get("write_mode", "safe"))
            self._preselect(self._desync_picker, self._draft.get("anti_desync", "auto"))
            # Restore flag ticks from a previous visit.
            flags = {
                "debug_log": self._draft.get("debug_log", False),
                "raw_log": self._draft.get("raw_log", False),
                "skip_filled": self._draft.get("skip_filled", True),
            }
            for option in self._flags.options:
                option.selected = bool(flags.get(option.value, option.selected))
            self._flags.refresh()
        self._update_derived()

    @staticmethod
    def _preselect(picker: Picker, value: str) -> None:
        for index, option in enumerate(picker.options):
            if option.value == value:
                picker.index = index
                return

    # ── Live derivation ──────────────────────────────────────────────────────

    def on_input_changed(self, event: Input.Changed) -> None:
        self._update_derived()

    def _typed_rows(self) -> int:
        try:
            return max(1, int(self._rows.value))
        except (ValueError, AttributeError):
            return self._rec_rows

    def _typed_workers(self) -> int:
        try:
            return max(1, int(self._workers.value))
        except (ValueError, AttributeError):
            return self._rec_workers

    def _update_derived(self) -> None:
        preset = self._preset
        rows = self._typed_rows()
        workers = self._typed_workers()
        wave = rows * workers
        derived = (
            f"Derived → {rows} rows/request · {workers} worker(s) · "
            f"wave {wave} rows · delay {preset.wave_delay}s"
        )
        if not preset.unlimited:
            derived += (
                f" · ~{preset.sustainable_rows_per_minute(rows):,} rows/min sustained"
            )
        try:
            self.query_one("#processing-derived", Static).update(derived)
        except Exception:
            return
        warn = self.query_one("#processing-warn", Label)
        safe_workers = max(1, preset.workers_for(rows, self._tok_per_row))
        if not preset.unlimited and (rows > self._rec_rows or workers > safe_workers):
            warn.update(
                f"⚠ Above the safe capacity for this limit "
                f"(recommended {self._rec_rows} rows · {safe_workers} workers). "
                f"Allowed, but expect rate-limit backoff."
            )
        else:
            warn.update("")

    # ── Result ──────────────────────────────────────────────────────────────

    def values(self) -> ProcessingValues:
        rows = self._typed_rows()
        workers = self._typed_workers()
        override = rows != self._rec_rows or workers != self._rec_workers
        result = ProcessingValues(
            rows_per_request=rows,
            workers=workers,
            request_delay=self._preset.delay_per_request,
            manual_override=override,
        )
        if self._advanced:
            mode = (self._mode_picker.current or Option("parallel")).value
            # A single worker is a sequential run whatever the picker says.
            result.processing_mode = mode if workers > 1 else "sequential"
            result.write_mode = (self._write_picker.current or Option("safe")).value
            result.anti_desync = (self._desync_picker.current or Option("auto")).value
            flags = set(self._flags.selected_values())
            result.debug_log = "debug_log" in flags
            result.raw_log = "raw_log" in flags
            result.skip_filled = "skip_filled" in flags
        else:
            result.processing_mode = _default_mode() if workers > 1 else "sequential"
        return result

    def focus_first(self) -> None:
        self._rows.focus_input()
