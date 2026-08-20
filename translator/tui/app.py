"""
tui/app.py — Top-level TUI app and navigation shell.

TUI LIBRARY CHOICE: **Textual** (04_TUI_SPEC.md §1 asks for this to be
documented here).

Why Textual over prompt_toolkit:
  * The run screen is the deciding factor — N worker status lines plus a
    live translation feed, all updating in place while worker threads run in
    the background. Textual's reactive widgets + `call_from_thread` handle
    exactly that; with prompt_toolkit it would be a hand-rolled redraw loop.
  * Declarative CSS layout survives terminal resize, which the two-column
    run screen needs.
  * Ready-made Input/DataTable/ModalScreen widgets for the wizard.
Cost: a heavier dependency (textual + rich). Accepted — this is a local
personal tool, not something being shipped as a wheel.

The keyboard drives everything; the mouse mirrors it and is never required
(see `widgets/common.py` for how). Screens are pushed/popped so `←`/`esc`
return to the parent without losing state, and Ctrl+C always exits cleanly.
"""

from __future__ import annotations

from typing import Callable

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Label, Static

from ..providers.registry import ProviderRegistry
from .theme import ACCENT, APP_CSS, WARN
from .widgets.common import Picker, Option, StatusBar


class BaseScreen(Screen):
    """
    Header / body / footer skeleton every screen shares.

    Subclasses implement `compose_body()` and `screen_keys()`; navigation
    bindings and the footer come for free.
    """

    BINDINGS = [
        Binding("escape", "cancel", "cancel", show=False),
        Binding("left", "back", "back", show=False),
    ]

    title_text = ""
    breadcrumb = ""
    # A message to show once this screen is on screen. A caller that pushes a
    # screen cannot call `notify_status` on it: `push_screen` returns before the
    # screen mounts, so its footer does not exist yet and the call raises. This is
    # the supported way to hand the next screen something to say.
    _pending_note: tuple[str, str] | None = None

    def compose(self) -> ComposeResult:
        yield Static(self._header_text(), id="header")
        # Scrollable so a short terminal scrolls the form instead of
        # squashing every field down to one line.
        with VerticalScroll(id="body"):
            yield from self.compose_body()
        self._status = StatusBar()
        # One docked strip holds everything at the bottom. Two widgets both
        # docked to the same edge do not stack in Textual — they are each
        # placed against it and overlap — so anything that has to sit above
        # the footer goes inside this with it and is laid out normally.
        with Vertical(id="bottom"):
            yield from self.compose_above_footer()
            yield self._status

    def on_mount(self) -> None:
        self._status.set_keys(self.screen_keys())
        if self._pending_note is not None:
            message, style = self._pending_note
            self._pending_note = None
            self.notify_status(message, style)
        self.on_screen_ready()

    def queue_note(self, message: str, style: str = "") -> None:
        """
        Say this as soon as the screen is up.

        Shown immediately if the screen is already mounted, so the same call
        works whether it comes from the screen itself or from whoever pushed it.
        """
        if self.is_mounted and getattr(self, "_status", None) is not None:
            self.notify_status(message, style)
            return
        self._pending_note = (message, style)

    # ── Hooks for subclasses ──────────────────────────────────────────────────

    def compose_body(self) -> ComposeResult:
        yield Label("")

    def compose_above_footer(self) -> ComposeResult:
        """
        Bottom-docked widgets, for a screen that needs one thing to stay put
        while the body scrolls — a form's confirm bar, say. Docked rather than
        yielded from `compose_body` so it cannot end up below the fold, which
        is the whole reason it exists.
        """
        return iter(())

    def screen_keys(self) -> list[tuple[str, str]]:
        return [("↑↓", "move"), ("⏎", "select"), ("←", "back"), ("^C", "quit")]

    def on_screen_ready(self) -> None:
        """Called once after mount — focus a widget, kick off a load, etc."""

    # ── Shared helpers ────────────────────────────────────────────────────────

    def refresh_keys(self) -> None:
        self._status.set_keys(self.screen_keys())

    def notify_status(self, message: str, style: str = "") -> None:
        self._status.notify_text(message, style)

    def set_header(self, text: str | None = None) -> None:
        self.query_one("#header", Static).update(text or self._header_text())

    def _header_text(self) -> str:
        crumb = self.breadcrumb or self.title_text
        left = f"UET v4 › {crumb}" if crumb else "UET v4"
        right = self.app.status_summary() if isinstance(self.app, UETApp) else ""
        width = max(20, self.size.width or 80)
        pad = max(1, width - len(left) - len(right) - 2)
        return f"{left}{' ' * pad}{right}"

    # ── Navigation ────────────────────────────────────────────────────────────

    def action_back(self) -> None:
        if len(self.app.screen_stack) > 1:
            self.app.pop_screen()

    def action_cancel(self) -> None:
        """Esc leaves the current flow, not the whole app (04_TUI_SPEC §2)."""
        if len(self.app.screen_stack) > 1:
            self.app.pop_screen()


class MenuScreen(BaseScreen):
    """A screen whose body is a single Picker — the common case."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.picker = Picker()

    def compose_body(self) -> ComposeResult:
        if self.title_text:
            yield Label(self.title_text, classes="title")
        yield self.picker
        yield from self.compose_extra()

    def compose_extra(self) -> ComposeResult:
        return iter(())

    def on_screen_ready(self) -> None:
        self.picker.set_options(self.build_options())
        self.picker.focus()

    def build_options(self) -> list[Option]:
        return []

    def on_picker_selected(self, event: Picker.Selected) -> None:
        self.handle_choice(event.option)

    def handle_choice(self, option: Option) -> None:
        """Subclasses act on the chosen option."""


class MainMenuScreen(MenuScreen):
    """
    The top level, split into Work and Configure (UET_UX §2).

    Work is what produces or reviews a file — Translate, Judge, Playground.
    Configure is the setup those depend on — Providers & Models, Model Groups,
    Settings. The two are separated by a heading row rather than a submenu, so
    everything stays one keystroke away while the grouping tells you which is
    which.
    """

    breadcrumb = ""

    BINDINGS = [Binding("escape", "quit_app", "quit", show=False)]

    def build_options(self) -> list[Option]:
        return [
            Option("WORK", "_work", enabled=False, style=ACCENT),
            Option("Translate", "translate",
                   hint="run a translate / repair / retranslate job"),
            Option("Judge", "judge",
                   hint="review a translated file, comment on bad rows, fix them"),
            Option("Playground", "playground",
                   hint="compare models on your own lines, judge them, or chat"),
            Option("", "_gap", enabled=False),
            Option("CONFIGURE", "_configure", enabled=False, style=ACCENT),
            Option("Providers & Models", "providers",
                   hint="providers, connections, models, presets"),
            Option("Model Groups", "combos",
                   hint="models run as one choice: fallback or round-robin"),
            Option("Settings", "settings", hint="defaults, vault password"),
            Option("", "_gap2", enabled=False),
            Option("Quit", "quit"),
        ]

    def compose_extra(self) -> ComposeResult:
        yield Label("", id="menu-gap")
        yield Label(self.app.registry_summary(), classes="hint", id="menu-summary")

    def screen_keys(self) -> list[tuple[str, str]]:
        return [("↑↓", "move"), ("⏎", "select"), ("^C", "quit")]

    def on_screen_ready(self) -> None:
        super().on_screen_ready()
        self.query_one("#menu-summary", Label).update(self.app.registry_summary())

    def handle_choice(self, option: Option) -> None:
        # Imported here, not at module scope: every screen module imports
        # BaseScreen from this one, so a top-level import would be circular.
        from .screens.combos import ComboListScreen
        from .screens.judge import JudgeSetupScreen
        from .screens.playground import PlaygroundMenuScreen
        from .screens.providers import ProviderListScreen
        from .screens.settings import SettingsScreen
        from .screens.wizard import ModelSelectScreen

        if option.value in ("translate", "playground", "judge"):
            if not self.app.registry.providers:
                self.notify_status(
                    "No providers yet — add one under Providers & Models first.", WARN
                )
                return
            if option.value == "translate":
                # No Quick/Full split any more — the flow starts at the model and
                # derives processing automatically (UET_UX §5).
                self.app.draft = {"mode": "full"}
                self.app.push_screen(ModelSelectScreen())
                return
            screen = {
                "playground": PlaygroundMenuScreen,
                "judge": JudgeSetupScreen,
            }[option.value]
            self.app.push_screen(screen())
        elif option.value == "providers":
            self.app.push_screen(ProviderListScreen())
        elif option.value == "combos":
            if not self.app.registry.providers:
                # A group is built from registered models, so there is nothing to
                # put in one yet. Said here rather than on an empty picker.
                self.notify_status(
                    "Add a provider first — a model group is built from models "
                    "you have already registered.", WARN
                )
                return
            self.app.push_screen(ComboListScreen())
        elif option.value == "settings":
            self.app.push_screen(SettingsScreen())
        else:
            self.app.exit()

    def action_quit_app(self) -> None:
        self.app.exit()

    def action_back(self) -> None:
        pass      # top level — nothing to go back to


class UETApp(App):
    """
    Application shell. Owns the unlocked ProviderRegistry and the in-flight
    job draft; screens read and mutate those, never global state.
    """

    CSS = APP_CSS
    TITLE = "Universal Excel Translator v4"
    BINDINGS = [Binding("ctrl+c", "hard_quit", "quit", priority=True, show=False)]

    def __init__(self, registry: ProviderRegistry | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.registry = registry or ProviderRegistry()
        self.draft: dict = {}                 # job wizard state, survives Back
        # The Judge wizard's own state. Separate from `draft` on purpose: an
        # evaluation is not a translation job, and starting one must not be able
        # to trample a translate wizard someone left half-filled.
        self.judge_draft: dict = {}
        # The single-row Fix model chosen "for this session" in Judge Results.
        # None until the user ticks the popup's remember box; reset when a new
        # evaluation is set up.
        self.judge_fix_slot: tuple[str, str, str] | None = None
        # The Rejudge model chosen "for this session", kept apart from the Fix
        # one: fixing and re-judging are different jobs and a model that repairs
        # well is not always the one that grades well (UET_UX bug #10). Reset
        # with the Fix slot when a new evaluation is set up.
        self.judge_rejudge_slot: tuple[str, str, str] | None = None
        self.on_shutdown_hook: Callable[[], None] | None = None

    def on_mount(self) -> None:
        from .screens.vault import VaultScreen

        if self.registry.is_unlocked:
            self.push_screen(MainMenuScreen())
        else:
            self.push_screen(VaultScreen())

    # ── Shared state helpers ──────────────────────────────────────────────────

    def status_summary(self) -> str:
        if not self.registry.is_unlocked:
            return "vault: locked"
        providers = len(self.registry.providers)
        connections = sum(len(p.connections) for p in self.registry.providers)
        return f"{providers} providers · {connections} connections"

    def registry_summary(self) -> str:
        if not self.registry.is_unlocked:
            return ""
        providers = self.registry.providers
        connections = sum(len(p.connections) for p in providers)
        models = sum(len(c.models) for p in providers for c in p.connections)
        if not providers:
            return "No providers configured yet — start with Providers & Models."
        return f"{len(providers)} providers · {connections} connections · {models} models"

    def go_home(self) -> None:
        """Pop back to the main menu from anywhere."""
        while len(self.screen_stack) > 2:
            self.pop_screen()
        if len(self.screen_stack) > 1 and not isinstance(self.screen, MainMenuScreen):
            self.pop_screen()

    def action_hard_quit(self) -> None:
        if self.on_shutdown_hook is not None:
            try:
                self.on_shutdown_hook()
            except Exception:
                pass
        self.exit()
