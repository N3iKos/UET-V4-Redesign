"""
tui/widgets/model_picker.py — The one Model Picker every flow shares.

Translate, Judge and Fix All all choose a model the same way: a Provider column,
a Connection column, a Model column, plus the fixed **Model Groups** entry that
stands beside the real providers (a group's members can span providers, so it
cannot live inside one). Before this widget each flow built that cascade itself
and they drifted — Fix All even grew a second, flat picker.

`ModelPicker` owns the three columns, the narrowing between them and the
resolution to a `ModelChoice`. A screen drops it into its body, calls
`load(registry)`, and listens for `ModelPicker.Chosen`. The keyboard walks the
columns with `←/→` and commits with Enter from any of them; the mouse narrows by
clicking a provider/connection and commits by clicking (or double-clicking) a
model — the same contract the standalone pickers had.
"""

from __future__ import annotations

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Label, Static

from ...providers.selection import (
    ModelChoice,
    choice_is_runnable,
    is_combo_provider,
    resolve_choice,
)
from ..theme import ACCENT, MUTED
from .common import Option, Picker, escape_markup


def provider_column_options(registry):
    """PROVIDER column, incl. the fixed Model Groups entry. Defined in combos."""
    from ..screens.combos import provider_column_options as _impl

    return _impl(registry)


def group_column_options():
    """MODEL column when Model Groups is the chosen provider."""
    from ..screens.combos import combo_column_options as _impl

    return _impl()


class ModelPicker(Vertical):
    """Provider · Connection · Model cascade, resolving to a `ModelChoice`."""

    BINDINGS = [Binding("right", "focus_next_col", "next column", show=False)]

    class Chosen(Message):
        """Enter/click committed a runnable choice."""

        def __init__(self, choice: ModelChoice) -> None:
            super().__init__()
            self.choice = choice

    class Highlighted(Message):
        """The highlighted model changed — for a live summary elsewhere."""

        def __init__(self, choice: ModelChoice | None) -> None:
            super().__init__()
            self.choice = choice

    def __init__(self, show_summary: bool = True, **kwargs) -> None:
        super().__init__(**kwargs)
        self._registry = None
        self._show_summary = show_summary

    def compose(self) -> ComposeResult:
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
        if self._show_summary:
            yield Label("")
            yield Static("", id="model-picker-summary", classes="hint")

    # ── Data ──────────────────────────────────────────────────────────────────

    def load(self, registry) -> None:
        self._registry = registry
        self.providers.set_options(provider_column_options(registry))
        self._load_connections()
        self._focus_first_choice()

    def _focus_first_choice(self) -> None:
        for picker in (self.providers, self.connections, self.models):
            if len(picker.options) > 1:
                picker.focus()
                return
        self.models.focus()

    def focus(self, *args, **kwargs):
        self._focus_first_choice()
        return self

    def _combo_selected(self) -> bool:
        option = self.providers.current
        return bool(option and is_combo_provider(option.value))

    def _provider(self):
        option = self.providers.current
        if option is None or is_combo_provider(option.value):
            return None
        try:
            return self._registry.get_provider(option.value)
        except Exception:
            return None

    def _load_connections(self) -> None:
        if self._combo_selected():
            self.connections.set_options([
                Option("(not used by groups)", "", enabled=False, style=MUTED)
            ])
            self._load_models()
            return
        provider = self._provider()
        if provider is None:
            return
        options: list[Option] = []
        if provider.strategy_active:
            options.append(
                Option(f"(auto — {provider.strategy.replace('_', '-')})", "",
                       hint="let the pool choose", style=ACCENT)
            )
        options += [
            Option(c.name, c.id, hint=f"{len(c.models)} models")
            for c in provider.connections
        ]
        self.connections.set_options(options)
        self._load_models()

    def _load_models(self) -> None:
        if self._combo_selected():
            self.models.set_options(group_column_options())
            self._emit_highlight()
            return
        provider = self._provider()
        if provider is None:
            return
        option = self.connections.current
        conn_id = option.value if option else ""
        if conn_id:
            models = provider.find_connection(conn_id).models
        else:
            seen, models = set(), []
            for connection in provider.connections:
                for model in connection.models:
                    if model.id not in seen:
                        seen.add(model.id)
                        models.append(model)
        self.models.set_options([
            Option(m.label, m.id, hint=m.preset.label()) for m in models
        ])
        self._emit_highlight()

    # ── Resolution ──────────────────────────────────────────────────────────────

    def choice(self) -> ModelChoice:
        provider_option = self.providers.current
        connection_option = self.connections.current
        model_option = self.models.current
        return resolve_choice(
            self._registry,
            provider_option.value if provider_option else "",
            connection_option.value if connection_option else "",
            model_option.value if model_option else "",
        )

    def _emit_highlight(self) -> None:
        model_option = self.models.current
        choice = self.choice() if (model_option and model_option.enabled) else None
        if self._show_summary:
            self._update_summary(choice)
        self.post_message(self.Highlighted(choice))

    def _update_summary(self, choice: ModelChoice | None) -> None:
        try:
            panel = self.query_one("#model-picker-summary", Static)
        except Exception:
            return
        if choice is None:
            panel.update(
                "[dim]Nothing to pick here — add models under Providers & Models, "
                "or a group under Model Groups.[/]"
            )
            return
        if choice.is_combo:
            combo = choice.combo
            if combo is None:
                panel.update("[dim]That group no longer exists.[/]")
                return
            preset = choice.preset
            limit = ("no rate limit — parallel cap only" if preset.unlimited
                     else f"{preset.rpm} rpm · {preset.tpm:,} tpm")
            panel.update(escape_markup(
                f"Selected: Group · {combo.label}\n"
                f"{combo.size} model(s) · {combo.strategy.replace('_', '-')} · {limit}"
            ))
            return
        provider = choice.provider
        if provider is None:
            panel.update("[dim]That provider no longer exists.[/]")
            return
        connection_option = self.connections.current
        conn_label = connection_option.label if connection_option else "?"
        model = choice.entry
        preset = choice.preset
        status = ""
        if model and getattr(model, "last_test_status", "") == "ok":
            status = f" · last test ✓ {model.last_test_latency_ms:.0f}ms"
        elif model and getattr(model, "last_test_status", "") == "error":
            status = " · last test ✗"
        limit = ("no rate limit" if preset.unlimited
                 else f"{preset.rpm} rpm · {preset.tpm:,} tpm")
        panel.update(escape_markup(
            f"Selected: {provider.name} · {conn_label} · {choice.label}\n"
            f"Preset {preset.label()} · {limit}{status}"
        ))

    # ── Events ──────────────────────────────────────────────────────────────────

    def on_picker_highlighted(self, event: Picker.Highlighted) -> None:
        event.stop()
        if event.picker is self.providers:
            self._load_connections()
        elif event.picker is self.connections:
            self._load_models()
        else:
            self._emit_highlight()

    def on_picker_selected(self, event: Picker.Selected) -> None:
        event.stop()
        model_option = self.models.current
        if model_option is None or not model_option.enabled:
            return
        choice = self.choice()
        ok, why = choice_is_runnable(choice)
        if not ok:
            self.notify_status(why)
            return
        self.post_message(self.Chosen(choice))

    def notify_status(self, message: str) -> None:
        """Bubble a refusal up to the hosting screen if it can show it."""
        screen = self.screen
        if hasattr(screen, "notify_status"):
            from ..theme import WARN

            screen.notify_status(message, WARN)

    # ── Column navigation ────────────────────────────────────────────────────────

    def _column_order(self) -> list[Picker]:
        if self._combo_selected():
            return [self.providers, self.models]
        return [self.providers, self.connections, self.models]

    def action_focus_next_col(self) -> None:
        order = self._column_order()
        for index, picker in enumerate(order):
            if picker.has_focus and index + 1 < len(order):
                order[index + 1].focus()
                return

    def focus_prev_col(self) -> bool:
        """Move focus one column left. Returns False when already leftmost."""
        order = self._column_order()
        for index, picker in enumerate(order):
            if picker.has_focus and index > 0:
                order[index - 1].focus()
                return True
        return False
