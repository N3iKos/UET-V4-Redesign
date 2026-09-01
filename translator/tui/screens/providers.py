"""
tui/screens/providers.py — Provider management screens.

  ProviderListScreen      list + status indicator
  AddProviderScreen       the 4-field flow (03_PROVIDER_SYSTEM.md §2)
  ProviderDetailScreen    connections + strategy selector
  ConnectionDetailScreen  models, fetch, manual add, batch test, presets
  ReasoningModal          per-model reasoning metadata (FEATURE-004)
  PresetModal             rate-limit preset picker

All network work runs in a worker thread; the UI shows a spinner and never
streams tokens. Provider errors are rendered verbatim in an ErrorBox.
"""

from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, Static

from ...providers.ratelimit import PRESET_LABELS, RateLimitPreset
from ...providers.registry import ModelTestResult, RegistryError, ValidationResult
from ..app import BaseScreen, MenuScreen
from ..theme import ACCENT, ERROR, MUTED, OK, WARN
from ..widgets.common import (
    ErrorBox,
    KeyHints,
    LabeledInput,
    Option,
    Picker,
    Spinner,
    spinner_frame,
)


def _status_dot(provider) -> tuple[str, str]:
    if not provider.connections:
        return "●", "no connections"
    if any(not c.is_valid for c in provider.connections):
        return "●", "some invalid"
    tested = [m for c in provider.connections for m in c.models]
    if tested and all(m.last_test_status == "untested" for m in tested):
        return "●", "untested"
    return "●", "all valid"


def _status_style(label: str) -> str:
    return {"all valid": OK, "untested": MUTED}.get(label, ERROR)


def _reasoning_tag(model) -> str:
    """
    What the model table prints in its THINK column.

    The control matters more than the capability: `effort` and `toggle` behave
    differently at run time, and `always_on` explains why no toggle is offered.
    """
    if not model.supports_reasoning:
        return "—"
    return {
        "toggle": "toggle" + (" on" if model.reasoning_default else ""),
        "effort": "effort",
        "always_on": "always",
    }.get(model.reasoning_control, "—")


# ── Provider list ─────────────────────────────────────────────────────────────

class ProviderListScreen(MenuScreen):
    breadcrumb = "Providers"
    title_text = "Providers"

    BINDINGS = [
        Binding("a", "add", "add", show=False),
        Binding("d", "delete", "delete", show=False),
    ]

    def build_options(self) -> list[Option]:
        options: list[Option] = []
        for provider in self.app.registry.providers:
            dot, label = _status_dot(provider)
            strategy = provider.strategy.replace("_", "-") if provider.strategy_active else "—"
            models = sum(len(c.models) for c in provider.connections)
            options.append(
                Option(
                    f"{provider.name:<18} {len(provider.connections)} conn  "
                    f"{models} models  {strategy:<12}",
                    provider.id,
                    hint=f"{dot} {label}",
                    style=_status_style(label),
                )
            )
        options.append(Option("+ Add provider", "__add__", style=ACCENT))
        return options

    def screen_keys(self) -> list[tuple[str, str]]:
        return [
            ("↑↓", "move"), ("⏎", "open"), ("a", "add"),
            ("d", "delete"), ("←", "back"),
        ]

    def handle_choice(self, option: Option) -> None:
        if option.value == "__add__":
            self.action_add()
        else:
            self.app.push_screen(ProviderDetailScreen(option.value))

    def action_add(self) -> None:
        self.app.push_screen(AddProviderScreen())

    def action_delete(self) -> None:
        option = self.picker.current
        if option is None or option.value == "__add__":
            return
        try:
            provider = self.app.registry.get_provider(option.value)
            self.app.registry.delete_provider(provider.id)
            self.notify_status(f"Deleted provider {provider.name}.", WARN)
            self.picker.set_options(self.build_options(), keep_index=True)
            self.set_header()
        except RegistryError as exc:
            self.notify_status(str(exc), ERROR)

    def on_screen_resumed(self) -> None:
        self.picker.set_options(self.build_options(), keep_index=True)
        self.set_header()


# ── Add provider ──────────────────────────────────────────────────────────────

class AddProviderScreen(BaseScreen):
    """
    All four fields on one screen, then one Enter to test and save. Nothing
    is persisted unless validation passes (03_PROVIDER_SYSTEM.md §2 step 5).
    """

    breadcrumb = "Providers › Add"

    def __init__(self, provider_id: str | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._provider_id = provider_id          # set = adding a connection
        self._validation: ValidationResult | None = None

    def compose_body(self) -> ComposeResult:
        adding_connection = self._provider_id is not None
        if adding_connection:
            provider = self.app.registry.get_provider(self._provider_id)
            yield Label(f"Add connection to {provider.name}", classes="title")
            yield Label(provider.base_url, classes="hint")
        else:
            yield Label("Add provider", classes="title")

        self._name = LabeledInput("Provider name", placeholder="MyGroq")
        self._url = LabeledInput(
            "Base URL",
            placeholder="https://api.groq.com/openai/v1",
            hint="Must be OpenAI-compatible. /models and /chat/completions "
                 "are derived from this.",
        )
        self._conn = LabeledInput("Connection name", default="Main Key",
                                  placeholder="Main Key")
        self._key = LabeledInput(
            "API key", password=True,
            hint="Leave blank for local servers that need no auth.",
        )

        if not adding_connection:
            yield self._name
            yield self._url
        yield self._conn
        yield self._key

        self._spinner = Spinner("Testing connection…")
        self._spinner.display = False
        yield self._spinner

        self._error = ErrorBox()
        yield self._error

        self._fallback = Picker()
        self._fallback.display = False
        yield self._fallback

        self._manual = LabeledInput(
            "Model ID", placeholder="llama-3.3-70b-versatile",
            hint="This endpoint has no usable /models — validate with one "
                 "model ID instead.",
        )
        self._manual.display = False
        yield self._manual

    def screen_keys(self) -> list[tuple[str, str]]:
        return [("tab", "next field"), ("⏎", "test & save"),
                ("←", "back"), ("esc", "cancel")]

    def on_screen_ready(self) -> None:
        self._app = self.app          # worker threads must not touch self.app
        if self._provider_id is None:
            self._name.focus_input()
        else:
            self._conn.focus_input()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if self._manual.display and event.input is self._manual.input:
            self._validate(manual_model=self._manual.value)
            return
        self._validate()

    def _current_url(self) -> str:
        if self._provider_id is not None:
            return self.app.registry.get_provider(self._provider_id).base_url
        return self._url.value

    def _validate(self, manual_model: str = "") -> None:
        if self._provider_id is None and not self._name.value:
            self.notify_status("Provider name is required.", ERROR)
            self._name.focus_input()
            return
        if not self._current_url():
            self.notify_status("Base URL is required.", ERROR)
            self._url.focus_input()
            return

        self._error.clear()
        self._fallback.display = False
        self._spinner.display = True
        self._spinner.set_label(
            f"Testing {manual_model}…" if manual_model else "Testing connection…"
        )
        self.notify_status("")
        self._run_validation(self._current_url(), self._key.value or None, manual_model)

    @work(thread=True)
    def _run_validation(self, base_url: str, api_key: str | None, manual_model: str) -> None:
        app = self._app
        registry = app.registry
        if manual_model:
            result = registry.validate_with_model(base_url, api_key, manual_model)
        else:
            result = registry.validate_connection(base_url, api_key)
        app.call_from_thread(self._validation_done, result, manual_model)

    def _validation_done(self, result: ValidationResult, manual_model: str) -> None:
        self._spinner.display = False
        self._validation = result

        if result.is_valid:
            self._save(result, manual_model)
            return

        self._error.show(
            result.error,
            f"provider response — connection invalid, nothing saved",
        )
        options = [Option("Edit fields and retry", "retry")]
        if result.needs_manual_model:
            options.append(Option("Enter a model ID manually instead", "manual"))
        options.append(Option("Cancel", "cancel"))
        self._fallback.set_options(options)
        self._fallback.display = True
        self._fallback.focus()

    def on_picker_selected(self, event: Picker.Selected) -> None:
        choice = event.option.value
        self._fallback.display = False
        self._error.clear()
        if choice == "retry":
            (self._conn if self._provider_id else self._name).focus_input()
        elif choice == "manual":
            self._manual.display = True
            self._manual.focus_input()
        else:
            self.app.pop_screen()

    def _save(self, result: ValidationResult, manual_model: str) -> None:
        registry = self.app.registry
        try:
            if self._provider_id is None:
                provider = registry.add_provider(
                    name=self._name.value,
                    base_url=self._url.value,
                    connection_name=self._conn.value,
                    api_key=self._key.value or None,
                    validation=result,
                )
                connection = provider.connections[0]
            else:
                provider = registry.get_provider(self._provider_id)
                connection = registry.add_connection(
                    provider_id=provider.id,
                    name=self._conn.value,
                    api_key=self._key.value or None,
                    validation=result,
                )
        except RegistryError as exc:
            self._error.show(str(exc), "cannot save")
            return

        if manual_model:
            registry.add_models(provider.id, connection.id, [manual_model])

        self.app.pop_screen()
        self.app.push_screen(ConnectionDetailScreen(provider.id, connection.id))


# ── Provider detail ───────────────────────────────────────────────────────────

class ProviderDetailScreen(MenuScreen):
    BINDINGS = [
        Binding("s", "strategy", "strategy", show=False),
        Binding("l", "scope", "limit scope", show=False),
        Binding("d", "delete", "delete", show=False),
    ]

    def __init__(self, provider_id: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.provider_id = provider_id

    @property
    def provider(self):
        return self.app.registry.get_provider(self.provider_id)

    def compose_body(self) -> ComposeResult:
        provider = self.provider
        self.breadcrumb = f"Providers › {provider.name}"
        yield Label(provider.name, classes="title")
        yield Label(provider.base_url, classes="hint")
        yield Label("")
        yield Static(self._strategy_block(), id="strategy-block")
        yield Label("")
        yield Static(self._scope_block(), id="scope-block")
        yield Label("")
        yield Label("Connections", classes="muted")
        yield self.picker

    def _scope_block(self) -> str:
        provider = self.provider
        active = provider.strategy_active
        scope = getattr(provider, "rate_limit_scope", "independent")
        mark = lambda name: "(•)" if scope == name else "( )"  # noqa: E731
        head = ("Rate-limit scope" if active
                else "Rate-limit scope  [dim](needs 2+ connections)[/dim]")
        open_tag, close_tag = ("", "") if active else ("[dim]", "[/dim]")
        return (
            f"[{MUTED}]{head}[/]\n"
            f"{open_tag}  {mark('independent')} independent  "
            f"each connection's limit is its own — capacity is the sum\n"
            f"  {mark('shared')} shared       "
            f"one allowance covers every connection{close_tag}"
        )

    def _strategy_block(self) -> str:
        provider = self.provider
        active = provider.strategy_active
        mark = lambda name: "(•)" if provider.strategy == name else "( )"  # noqa: E731
        head = "Multi-connection strategy" if active else \
               "Multi-connection strategy  [dim](needs 2+ connections)[/dim]"
        # The dim wrapper has to disappear entirely when the selector is live:
        # an empty `[]` tag leaves the closing `[/]` with nothing to close, and
        # Textual raises rather than ignoring it.
        open_tag, close_tag = ("", "") if active else ("[dim]", "[/dim]")
        return (
            f"[{MUTED}]{head}[/]\n"
            f"{open_tag}  {mark('fallback')} fallback      "
            f"stay on one connection until it fails\n"
            f"  {mark('round_robin')} round-robin   "
            f"one full wave per connection, then switch{close_tag}"
        )

    def build_options(self) -> list[Option]:
        options = []
        for connection in self.provider.connections:
            status = "valid" if connection.is_valid else "invalid"
            options.append(
                Option(
                    f"{connection.name:<16} {connection.masked_key():<18} "
                    f"{len(connection.models)} models",
                    connection.id,
                    hint=f"● {status}",
                    style=OK if connection.is_valid else ERROR,
                )
            )
        options.append(Option("+ Add connection", "__add__", style=ACCENT))
        return options

    def screen_keys(self) -> list[tuple[str, str]]:
        keys = [("↑↓", "move"), ("⏎", "open")]
        if self.provider.strategy_active:
            keys.append(("s", "strategy"))
            keys.append(("l", "scope"))
        keys += [("d", "delete conn"), ("←", "back")]
        return keys

    def handle_choice(self, option: Option) -> None:
        if option.value == "__add__":
            self.app.push_screen(AddProviderScreen(self.provider_id))
        else:
            self.app.push_screen(ConnectionDetailScreen(self.provider_id, option.value))

    def action_strategy(self) -> None:
        provider = self.provider
        if not provider.strategy_active:
            self.notify_status(
                "Strategy needs 2+ connections — it is inert right now.", MUTED
            )
            return
        new = "round_robin" if provider.strategy == "fallback" else "fallback"
        self.app.registry.set_strategy(provider.id, new)
        self.query_one("#strategy-block", Static).update(self._strategy_block())
        self.notify_status(f"Strategy set to {new.replace('_', '-')}.", OK)

    def action_scope(self) -> None:
        provider = self.provider
        if not provider.strategy_active:
            self.notify_status(
                "Rate-limit scope needs 2+ connections — it is inert right now.",
                MUTED,
            )
            return
        current = getattr(provider, "rate_limit_scope", "independent")
        new = "shared" if current == "independent" else "independent"
        self.app.registry.set_rate_limit_scope(provider.id, new)
        self.query_one("#scope-block", Static).update(self._scope_block())
        self.notify_status(f"Rate-limit scope set to {new}.", OK)

    def action_delete(self) -> None:
        option = self.picker.current
        if option is None or option.value == "__add__":
            return
        try:
            self.app.registry.delete_connection(self.provider_id, option.value)
            self.picker.set_options(self.build_options(), keep_index=True)
            self.query_one("#strategy-block", Static).update(self._strategy_block())
            self.refresh_keys()
            self.notify_status("Connection deleted.", WARN)
        except RegistryError as exc:
            self.notify_status(str(exc), ERROR)

    def on_screen_resumed(self) -> None:
        self.picker.set_options(self.build_options(), keep_index=True)
        self.query_one("#strategy-block", Static).update(self._strategy_block())
        self.query_one("#scope-block", Static).update(self._scope_block())
        self.refresh_keys()


# ── Connection detail ─────────────────────────────────────────────────────────

class ConnectionDetailScreen(BaseScreen):
    BINDINGS = [
        Binding("f", "fetch", "fetch", show=False),
        Binding("m", "manual", "manual", show=False),
        Binding("t", "test", "test", show=False),
        Binding("p", "preset", "preset", show=False),
        Binding("P", "preset_all", "preset all", show=False),
        Binding("c", "conn_limit", "connection limit", show=False),
        Binding("x", "remove", "remove", show=False),
        Binding("r", "reasoning", "reasoning", show=False),
        Binding("o", "priority", "priority", show=False),
        Binding("j", "json_toggle", "json", show=False),
    ]

    def __init__(self, provider_id: str, connection_id: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.provider_id = provider_id
        self.connection_id = connection_id
        self._testing: set[str] = set()
        self._tick = 0

    @property
    def provider(self):
        return self.app.registry.get_provider(self.provider_id)

    @property
    def connection(self):
        return self.app.registry.get_connection(self.provider_id, self.connection_id)

    def compose_body(self) -> ComposeResult:
        self.breadcrumb = f"Providers › {self.provider.name} › {self.connection.name}"
        yield Label(f"{self.provider.name} › {self.connection.name}", classes="title")
        yield Label(
            f"{self.provider.base_url}   key {self.connection.masked_key()}",
            classes="hint",
        )
        yield Label(self._conn_limit_text(), id="conn-limit", classes="hint")
        yield Label("")
        yield Static(self._table_header(), classes="muted")
        self.picker = Picker(multi_select=True)
        yield self.picker
        yield Label("")
        self._spinner = Spinner("Working…")
        self._spinner.display = False
        yield self._spinner
        self._error = ErrorBox()
        yield self._error
        self._manual_input = LabeledInput(
            "Model ID", placeholder="llama-3.3-70b-versatile"
        )
        self._manual_input.display = False
        yield self._manual_input

    @staticmethod
    def _table_header() -> str:
        return (
            f"{'MODEL':<27}{'PRESET':<11}{'RPM/TPM':<11}{'PRI':<5}{'FMT':<6}{'THINK':<10}LAST TEST"
        )

    def _conn_limit_text(self) -> str:
        limit = getattr(self.connection, "rate_limit", None)
        if limit is None:
            return "connection limit: none — each model's own preset paces it"
        if limit.unlimited:
            return "connection limit: no rate limit"
        return f"connection limit: {limit.rpm} rpm · {limit.tpm:,} tpm"

    def screen_keys(self) -> list[tuple[str, str]]:
        return [
            ("space", "select"), ("f", "fetch"), ("m", "add"), ("t", "test"),
            ("p", "preset"), ("P", "all"), ("c", "conn limit"), ("r", "think"),
            ("o", "priority"), ("j", "json"), ("x", "remove"), ("←", "back"),
        ]

    def on_screen_ready(self) -> None:
        self._app = self.app          # worker threads must not touch self.app
        self._refresh_models()
        self.picker.focus()
        self.set_interval(0.1, self._animate)

    def _animate(self) -> None:
        if self._testing:
            self._tick += 1
            self._refresh_models(keep=True)

    def _refresh_models(self, keep: bool = True) -> None:
        options: list[Option] = []
        for model in self.connection.models:
            preset = model.preset
            if model.id in self._testing:
                status = f"{spinner_frame(self._tick)} testing"
                style = ACCENT
            elif model.last_test_status == "ok":
                latency = model.last_test_latency_ms or 0
                status = f"✓ {latency:.0f}ms"
                style = OK
            elif model.last_test_status == "error":
                status = "✗ error"
                style = ERROR
            else:
                status = "· never"
                style = MUTED

            # Built as one f-string: implicit concatenation followed by
            # `.ljust()` pads the *joined* string, which is already longer than
            # the width and so silently does nothing — that is how the rate
            # column ended up flush against the format column.
            #
            # An unlimited model says so in words: `0/0k` reads as a broken row
            # rather than as "no limit" (CR-001 FR-605 / AC-615).
            limits = "no limit" if preset.unlimited \
                else f"{preset.rpm}/{preset.tpm // 1000}k"
            p_rank = f"P{model.priority_rank}" if getattr(model, "priority_rank", 0) > 0 else "—"
            label = (
                f"{model.label[:25]:<27}{preset.label():<11}{limits:<11}"
                f"{p_rank:<5}"
                f"{'json' if model.prompt_format == 'json' else 'plain':<6}"
                f"{_reasoning_tag(model):<10}{status}"
            )
            options.append(Option(label, model.id, style=style))
            if model.last_test_status == "error" and model.last_test_error:
                options.append(
                    Option(f"     └ {model.last_test_error[:90]}", f"__err__{model.id}",
                           enabled=False, style=ERROR)
                )

        if not options:
            options.append(
                Option("No models yet — press f to fetch, or m to add one manually",
                       "__none__", enabled=False, style=MUTED)
            )
        self.picker.set_options(options, keep_index=keep)

    def _selected_model_ids(self) -> list[str]:
        chosen = [
            value for value in self.picker.selected_values()
            if isinstance(value, str) and not value.startswith("__")
        ]
        if chosen:
            return chosen
        current = self.picker.current
        if current and isinstance(current.value, str) and not current.value.startswith("__"):
            return [current.value]
        return []

    # ── Actions ───────────────────────────────────────────────────────────────

    def action_fetch(self) -> None:
        self._error.clear()
        self._spinner.set_label("Fetching models…")
        self._spinner.display = True
        self._fetch_worker()

    @work(thread=True)
    def _fetch_worker(self) -> None:
        app = self._app
        result = app.registry.fetch_models(self.provider_id, self.connection_id)
        app.call_from_thread(self._fetch_done, result)

    def _fetch_done(self, result) -> None:
        self._spinner.display = False
        if not result.is_ok or not result.models:
            self._error.show(
                result.error or "The endpoint returned no models.",
                "provider response — /models unavailable",
            )
            self.notify_status("Press m to add a model ID manually.", WARN)
            return
        existing = {m.id for m in self.connection.models}
        fresh = [m for m in result.models if m not in existing]
        if not fresh:
            self.notify_status("All fetched models are already added.", MUTED)
            return
        self.app.push_screen(
            ModelPickModal(fresh), self._models_picked
        )

    def _models_picked(self, chosen: list[str] | None) -> None:
        if not chosen:
            return
        added = self.app.registry.add_models(self.provider_id, self.connection_id, chosen)
        self._refresh_models(keep=False)
        self.notify_status(f"Added {len(added)} model(s).", OK)

    def action_manual(self) -> None:
        self._manual_input.display = True
        self._manual_input.focus_input()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input is self._manual_input.input:
            model_id = self._manual_input.value
            self._manual_input.input.value = ""
            self._manual_input.display = False
            if model_id:
                self.app.registry.add_models(
                    self.provider_id, self.connection_id, [model_id]
                )
                self._refresh_models(keep=False)
                self.notify_status(f"Added {model_id}.", OK)
            self.picker.focus()

    def action_test(self) -> None:
        model_ids = self._selected_model_ids()
        if not model_ids:
            self.notify_status("Nothing selected.", WARN)
            return
        self._error.clear()
        self._testing = set(model_ids)
        self._refresh_models()
        self.notify_status(f"Testing {len(model_ids)} model(s)…", ACCENT)
        self._test_worker(model_ids)

    @work(thread=True)
    def _test_worker(self, model_ids: list[str]) -> None:
        app = self._app

        def progress(result: ModelTestResult) -> None:
            app.call_from_thread(self._one_test_done, result)

        app.registry.test_models(
            self.provider_id, self.connection_id, model_ids, on_result=progress
        )
        app.call_from_thread(self._tests_done)

    def _one_test_done(self, result: ModelTestResult) -> None:
        self._testing.discard(result.model_id)
        self._refresh_models()

    def _tests_done(self) -> None:
        self._testing.clear()
        self._refresh_models()
        ok = sum(1 for m in self.connection.models if m.last_test_status == "ok")
        failed = sum(1 for m in self.connection.models if m.last_test_status == "error")
        self.notify_status(f"Test finished — {ok} ok, {failed} failed.",
                           OK if not failed else WARN)

    def action_preset(self) -> None:
        model_ids = self._selected_model_ids()
        if not model_ids:
            return
        self.app.push_screen(
            PresetModal(model_ids[0]),
            lambda preset: self._apply_preset(preset, model_ids),
        )

    def action_preset_all(self) -> None:
        if not self.connection.models:
            return
        self.app.push_screen(
            PresetModal("all models on this connection"),
            lambda preset: self._apply_preset(
                preset, [m.id for m in self.connection.models]
            ),
        )

    def _apply_preset(self, preset: RateLimitPreset | None, model_ids: list[str]) -> None:
        if preset is None:
            return
        for model_id in model_ids:
            self.app.registry.set_model_preset(
                self.provider_id, self.connection_id, model_id,
                RateLimitPreset.from_dict(preset.to_dict()),
            )
        self._refresh_models()
        limit = "no rate limit" if preset.unlimited \
            else f"{preset.rpm} rpm · {preset.tpm:,} tpm"
        self.notify_status(
            f"Preset {preset.label()} ({limit}) applied to {len(model_ids)} model(s).",
            OK,
        )

    def action_conn_limit(self) -> None:
        """Set (or clear) this connection's own rate limit (UET_UX §3)."""
        self.app.push_screen(
            PresetModal(f"connection · {self.connection.name}"),
            self._apply_conn_limit,
        )

    def _apply_conn_limit(self, preset: RateLimitPreset | None) -> None:
        if preset is None:
            return
        self.app.registry.set_connection_rate_limit(
            self.provider_id, self.connection_id,
            RateLimitPreset.from_dict(preset.to_dict()),
        )
        try:
            self.query_one("#conn-limit", Label).update(self._conn_limit_text())
        except Exception:
            pass
        limit = "no rate limit" if preset.unlimited \
            else f"{preset.rpm} rpm · {preset.tpm:,} tpm"
        self.notify_status(
            f"Connection rate limit set to {preset.label()} ({limit}). "
            f"Used for capacity when the provider has 2+ connections.",
            OK,
        )

    def action_remove(self) -> None:
        for model_id in self._selected_model_ids():
            try:
                self.app.registry.remove_model(
                    self.provider_id, self.connection_id, model_id
                )
            except RegistryError:
                pass
        self._refresh_models(keep=True)
        self.notify_status("Model(s) removed.", WARN)

    def action_reasoning(self) -> None:
        """
        Open the reasoning metadata editor for the highlighted model.

        Not a toggle any more: "can it reason" and "can it be told not to" are
        different questions, and the parameter that carries the answer differs
        per model (FEATURE-004). Guessing it would send a field the server
        rejects, so it is asked for instead.
        """
        model_ids = self._selected_model_ids()
        if not model_ids:
            return
        model = self.app.registry.get_model(
            self.provider_id, self.connection_id, model_ids[0]
        )
        self.app.push_screen(
            ReasoningModal(model), lambda saved: self._reasoning_saved(model.id, saved)
        )

    def _reasoning_saved(self, model_id: str, saved: dict | None) -> None:
        if not saved:
            return
        try:
            self.app.registry.set_model_reasoning_config(
                self.provider_id, self.connection_id, model_id, **saved
            )
        except RegistryError as exc:
            self.notify_status(str(exc), ERROR)
            return
        self._refresh_models()
        control = saved.get("control", "none")
        self.notify_status(f"{model_id}: reasoning {control}.", OK)

    def action_priority(self) -> None:
        """Edit the priority failover rank for the highlighted model."""
        model_ids = self._selected_model_ids()
        if not model_ids:
            return
        model = self.app.registry.get_model(
            self.provider_id, self.connection_id, model_ids[0]
        )
        self.app.push_screen(
            PriorityModal(model, self.app.registry),
            lambda rank: self._priority_saved(model.id, rank),
        )

    def _priority_saved(self, model_id: str, rank: int | None) -> None:
        if rank is None:
            return
        try:
            self.app.registry.set_model_priority_rank(
                self.provider_id, self.connection_id, model_id, rank
            )
        except RegistryError as exc:
            self.notify_status(str(exc), ERROR)
            return
        self._refresh_models()
        label = f"Priority #{rank}" if rank > 0 else "Priority cleared (normal model)"
        self.notify_status(f"{model_id}: {label}.", OK)

    def action_json_toggle(self) -> None:
        """
        Structured-output opt-in. Off by default because plenty of
        OpenAI-compatible servers reject response_format entirely.
        """
        for model_id in self._selected_model_ids():
            model = self.app.registry.get_model(
                self.provider_id, self.connection_id, model_id
            )
            new = "plain" if model.prompt_format == "json" else "json"
            self.app.registry.set_model_prompt_format(
                self.provider_id, self.connection_id, model_id, new
            )
        self._refresh_models()
        self.notify_status(
            "Structured JSON output toggled — your provider may not support it.", WARN
        )


def validate_priority_rank(
    registry, candidate_rank: int, current_model_id: str = ""
) -> str | None:
    """
    Ensure priority rank is either 0 (no priority/normal model) or unique
    across all configured models across all providers/connections.
    Returns error message if duplicated, otherwise None.
    """
    try:
        rank = int(candidate_rank)
    except (ValueError, TypeError):
        return "Priority rank must be a positive number or 0."
    if rank <= 0:
        return None
    for p in getattr(registry, "providers", []):
        for c in p.connections:
            for m in c.models:
                if m.id != current_model_id and m.priority_rank == rank:
                    return f"Priority Rank #{rank} is already assigned to {m.label}"
    return None


class PriorityModal(ModalScreen[int | None]):
    """Edit priority failover rank for a model (0 = None, 1, 2, 3...)."""

    BINDINGS = [Binding("escape", "dismiss_none", "cancel", show=False)]

    def __init__(self, model, registry, **kwargs) -> None:
        super().__init__(**kwargs)
        self._model = model
        self._registry = registry

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label(f"Priority Failover · {self._model.label}", classes="title")
            yield Label(
                "Set failover rank (0 = None/normal, 1 = First fallback, 2 = Second fallback...)\n"
                "Numbers greater than 0 must be unique across all models.",
                classes="muted",
            )
            self._input = LabeledInput(
                "Priority Rank",
                value=str(getattr(self._model, "priority_rank", 0) or 0),
                default="0",
                placeholder="0",
                hint="0 = normal model; 1, 2, 3... = failover order",
            )
            yield self._input
            self._error = Label("", id="priority-error", classes="error")
            yield self._error
            yield KeyHints([("⏎", "save"), ("esc", "cancel")])

    def on_mount(self) -> None:
        self._input.focus_input()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._save()

    def _save(self) -> None:
        val = self._input.value.strip()
        try:
            rank = int(val)
        except ValueError:
            self._error.update("Priority rank must be a whole number (0, 1, 2...).")
            return
        if rank < 0:
            self._error.update("Priority rank cannot be negative.")
            return
        err = validate_priority_rank(self._registry, rank, current_model_id=self._model.id)
        if err:
            self._error.update(err)
            return
        self.dismiss(rank)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


# ── Modals ────────────────────────────────────────────────────────────────────

class ModelPickModal(ModalScreen[list]):
    """Pick which fetched models to actually add — never force-add all."""

    BINDINGS = [
        Binding("escape", "dismiss_none", "cancel", show=False),
        Binding("a", "select_all", "all", show=False),
    ]

    def __init__(self, models: list[str], **kwargs) -> None:
        super().__init__(**kwargs)
        self._models = models

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label(f"Fetched {len(self._models)} model(s) — pick what to add",
                        classes="title")
            self.picker = Picker(
                [Option(model, model) for model in self._models], multi_select=True
            )
            yield self.picker
            yield KeyHints([
                ("space", "select"), ("a", "select all"),
                ("⏎", "add"), ("esc", "cancel"),
            ])

    def on_mount(self) -> None:
        self.picker.focus()

    def action_select_all(self) -> None:
        for option in self.picker.options:
            option.selected = True
        self.picker.refresh()

    def on_picker_selected(self, event: Picker.Selected) -> None:
        chosen = self.picker.selected_values() or [event.option.value]
        self.dismiss(chosen)

    def action_dismiss_none(self) -> None:
        self.dismiss([])


class ReasoningModal(ModalScreen[dict]):
    """
    Reasoning metadata for one model (FEATURE-004 FR-404…FR-407).

    The control comes first because it decides what else is worth asking: only
    a `toggle` model needs a parameter path and on/off values, and only
    `toggle`/`effort` have a meaningful default. Fields for the others stay
    hidden rather than collecting values the adapter would ignore.

    Nothing here guesses. Leaving a toggle's path empty is refused by the
    registry, because a toggle that sends no parameter is a switch that does
    nothing while claiming otherwise.
    """

    BINDINGS = [Binding("escape", "dismiss_none", "cancel", show=False)]

    CONTROLS = [
        ("none", "Off — no reasoning",
         "no parameter is ever sent (safe default)"),
        ("toggle", "Toggle — can be switched on and off",
         "needs a parameter path, e.g. chat_template_kwargs.enable_thinking"),
        ("effort", "Effort — level only (low/medium/high)",
         "sends reasoning_effort; a level is not an off switch"),
        ("always_on", "Always on — cannot be disabled",
         "no toggle is shown and no parameter is sent"),
    ]

    def __init__(self, model, **kwargs) -> None:
        super().__init__(**kwargs)
        self._model = model

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label(f"Reasoning · {self._model.label}", classes="title")
            yield Label("How does this model control reasoning?", classes="muted")
            self._control = Picker(
                [Option(label, value, hint=hint) for value, label, hint in self.CONTROLS],
                click_activates=False,
            )
            yield self._control

            self._path = LabeledInput(
                "Parameter path",
                value=self._model.reasoning_parameter_path,
                placeholder="chat_template_kwargs.enable_thinking",
                hint="Dotted path into the request body. Your provider's docs "
                     "name it; it is never guessed.",
            )
            self._on_value = LabeledInput(
                "Value when on", value=_value_text(self._model.reasoning_on_value),
                default="true", hint="true / false / a number / a word.",
            )
            self._off_value = LabeledInput(
                "Value when off", value=_value_text(self._model.reasoning_off_value),
                default="false",
            )
            self._levels = LabeledInput(
                "Effort levels",
                value=", ".join(self._model.reasoning_options) or "",
                default="low, medium, high",
                hint="Comma-separated, as the provider spells them.",
            )
            self._default = Picker([
                Option("default: reasoning off", "off"),
                Option("default: reasoning on", "on"),
            ], click_activates=False)
            yield self._path
            yield self._on_value
            yield self._off_value
            yield self._levels
            yield Label("Quick mode uses this default", classes="muted")
            yield self._default
            yield Label("", id="reasoning-error", classes="error")
            yield KeyHints([("↑↓", "move"), ("tab", "next field"),
                            ("⏎", "save"), ("esc", "cancel")])

    def on_mount(self) -> None:
        for index, (value, _label, _hint) in enumerate(self.CONTROLS):
            if value == self._model.reasoning_control:
                self._control.index = index
                break
        self._default.index = 1 if self._model.reasoning_default else 0
        self._apply_visibility()
        self._control.focus()

    # ── Which fields matter ───────────────────────────────────────────────────

    def _chosen(self) -> str:
        option = self._control.current
        return option.value if option else "none"

    def on_picker_highlighted(self, event: Picker.Highlighted) -> None:
        if event.picker is self._control:
            self._apply_visibility()

    def _apply_visibility(self) -> None:
        control = self._chosen()
        toggle = control == "toggle"
        self._path.display = toggle
        self._on_value.display = toggle
        self._off_value.display = toggle
        self._levels.display = control == "effort"
        self._default.display = control in ("toggle", "effort")

    # ── Saving ────────────────────────────────────────────────────────────────

    def on_picker_selected(self, event: Picker.Selected) -> None:
        self._save()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        order = [self._path, self._on_value, self._off_value, self._levels]
        visible = [field for field in order if field.display]
        for index, field in enumerate(visible):
            if event.input is field.input and index + 1 < len(visible):
                visible[index + 1].focus_input()
                return
        self._save()

    def _save(self) -> None:
        control = self._chosen()
        error = self.query_one("#reasoning-error", Label)
        if control == "toggle" and not self._path.value.strip():
            error.update("A toggle model needs a parameter path.")
            self._path.focus_input()
            return

        levels = [
            part.strip() for part in (self._levels.value or "").split(",")
            if part.strip()
        ]
        default_on = (self._default.current or Option("off")).value == "on"
        self.dismiss({
            "supported": control != "none",
            "control": control,
            "parameter_path": self._path.value.strip() if control == "toggle" else "",
            "on_value": _parse_value(self._on_value.value),
            "off_value": _parse_value(self._off_value.value),
            "default_enabled": default_on and control in ("toggle", "effort"),
            "options": levels if control == "effort" else [],
        })

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


def _value_text(value) -> str:
    """A stored on/off value, as it should appear in the field."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def _parse_value(text: str):
    """
    `"true"` → True, `"3"` → 3, anything else stays the string the user typed.

    Types matter to the server: a model expecting `true` will reject `"true"`,
    so the obvious literals are converted and everything else is passed through
    untouched rather than coerced into a guess.
    """
    raw = (text or "").strip()
    lowered = raw.lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


class PresetModal(ModalScreen[RateLimitPreset]):
    """
    Rate-limit preset picker, with a Custom rpm/tpm path.

    `0` in either field means *no limit* (CR-001 FR-605), and so does an empty
    field — hence `default="0"` on both, so clearing a box and typing 0 land in
    the same place. Text that is not a number is refused with a visible message
    rather than a silent `return`, which is what the old code did and which left
    the modal open with no explanation.
    """

    BINDINGS = [Binding("escape", "dismiss_none", "cancel", show=False)]

    def __init__(self, target: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._target = target

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label(f"Rate limit preset · {self._target}", classes="title")
            options = []
            for name in ("safe", "average", "speed", "ultra_fast"):
                preset = RateLimitPreset.from_name(name)
                options.append(
                    Option(
                        f"{PRESET_LABELS[name]:<12}"
                        f"{preset.rpm:>3} rpm · {preset.tpm // 1000:>3}k tpm",
                        name,
                        # The worker count is derived from both limits at this
                        # batch, so the hint states what the tier actually
                        # sustains rather than a tabulated guess.
                        hint=f"batch {preset.suggested_batch_size} "
                             f"×{preset.workers_for(preset.suggested_batch_size)} · "
                             f"{preset.limited_by()}-bound · "
                             f"~{preset.sustainable_rows_per_minute():,} rows/min",
                    )
                )
            options.append(
                Option("Custom", "custom",
                       hint="enter your own rpm / tpm — 0 = no limit")
            )
            self.picker = Picker(options)
            yield self.picker

            self._rpm = LabeledInput(
                "RPM", default="0",
                hint="0 or empty = no rate limit (workers follow the parallel cap).",
            )
            self._tpm = LabeledInput("TPM", default="0")
            self._rpm.display = False
            self._tpm.display = False
            yield self._rpm
            yield self._tpm
            yield Label("", id="preset-error", classes="error")
            yield KeyHints([("⏎", "apply"), ("esc", "cancel")])

    def on_mount(self) -> None:
        self.picker.focus()

    def on_picker_selected(self, event: Picker.Selected) -> None:
        if event.option.value == "custom":
            self._rpm.display = True
            self._tpm.display = True
            self._rpm.focus_input()
            return
        self.dismiss(RateLimitPreset.from_name(event.option.value))

    def on_input_changed(self, event: Input.Changed) -> None:
        self.query_one("#preset-error", Label).update("")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input is self._rpm.input:
            self._tpm.focus_input()
            return
        error = self.query_one("#preset-error", Label)
        try:
            rpm, tpm = int(self._rpm.value), int(self._tpm.value)
        except ValueError:
            error.update("RPM and TPM must be numbers. 0 means no limit.")
            self._rpm.focus_input()
            return
        # `custom()` keeps 0 as 0 and derives the batch and worker count from
        # whatever is left, so a hand-entered limit — including none — is paced
        # the same way a named tier is.
        self.dismiss(RateLimitPreset.custom(rpm, tpm))

    def action_dismiss_none(self) -> None:
        self.dismiss(None)
