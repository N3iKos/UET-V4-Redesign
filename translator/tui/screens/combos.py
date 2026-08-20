"""
tui/screens/combos.py — Combo management screens.

  ComboListScreen    every combo, with its size and strategy
  ComboDetailScreen  members, order, strategy, rate limit, delete
  ComboMemberModal   Provider → Connection → Model, to add one member

Deliberately shaped like `providers.py`'s two-screen flow (list → detail), for
the same reason Judge's wizard was shaped like the translate wizard: someone who
has already learned how to manage a provider's connections should not have to
learn a second idiom to manage a combo's models (FEATURE-005).

Two things differ from a provider, and both follow from what a combo is:

  * **order is functional.** Under Fallback the list *is* the try-order, so
    moving a member is an edit rather than a cosmetic preference — hence
    `[`/`]` on the detail screen and the position shown in every row.
  * **members can be dangling.** A combo stores ids into the vault, and the
    vault is edited independently, so a model can be deleted after a combo names
    it. Such a row is shown as broken rather than hidden: a combo that has
    quietly shrunk behaves very differently, and only the user can fix it.

Every write goes through `data/combos.py` (atomic JSON beside the vault), and
nothing here prints — feedback is `notify_status` and `ErrorBox`, as everywhere.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, Static

from ...data.combos import (
    COMBO_PROVIDER_ID,
    COMBO_PROVIDER_LABEL,
    STRATEGY_LABELS,
    Combo,
    ComboError,
    ComboMember,
    delete_combo,
    find_combo,
    load_combos,
    save_combo,
    unique_name,
)
from ...providers.ratelimit import RateLimitPreset
from ..app import BaseScreen, MenuScreen
from ..theme import ACCENT, ERROR, MUTED, OK, WARN
from ..widgets.common import ErrorBox, KeyHints, LabeledInput, Option, Picker
from .providers import PresetModal


def _limit_text(preset: RateLimitPreset) -> str:
    """
    A combo's rate limit in words.

    "no rate limit" rather than `0 rpm · 0 tpm`, because the second reads as a
    misconfiguration and the first is the thing that was asked for (CR-001).
    """
    if preset.unlimited:
        return "no rate limit"
    return f"{preset.rpm} rpm · {preset.tpm:,} tpm"


# ── The two columns every model picker shares ─────────────────────────────────

def provider_column_options(registry) -> list[Option]:
    """
    The PROVIDER column: the user's providers, plus one fixed `Model Groups` entry.

    Model Groups is appended rather than mixed in, and styled, because it is not
    one of the user's providers — it is a different kind of thing that happens to
    be selectable in the same column (FEATURE-005). Offered even with no groups
    saved: an entry that is invisible until you already know it exists is worse
    than an empty MODEL column that says where to make one.

    Lives here, with the group screens, and is used by all three pickers
    (Translate, Judge, Fix All) so the entry cannot appear in one and not another.
    """
    options = [
        Option(p.name, p.id, hint=f"{len(p.connections)} conn")
        for p in registry.providers
    ]
    combos = load_combos()
    options.append(
        Option(
            COMBO_PROVIDER_LABEL,
            COMBO_PROVIDER_ID,
            hint=f"{len(combos)} group(s)" if combos else "none yet",
            style=ACCENT,
        )
    )
    return options


def combo_column_options() -> list[Option]:
    """The MODEL column when Model Groups is the chosen provider."""
    combos = load_combos()
    if not combos:
        return [
            Option("No groups yet — create one under Model Groups", "",
                   enabled=False, style=MUTED)
        ]
    return [
        Option(
            combo.label,
            combo.id,
            hint=f"{combo.size} model(s) · {combo.strategy.replace('_', '-')}",
        )
        for combo in combos
    ]


def _member_label(registry, member: ComboMember) -> tuple[str, str, bool]:
    """
    `(label, hint, ok)` for one member row.

    `ok=False` when the member no longer resolves. It keeps its place in the list
    so the order the user arranged is still recognisable, and says what is
    missing instead of vanishing.
    """
    try:
        provider = registry.get_provider(member.provider_id)
        connection = registry.get_connection(member.provider_id, member.connection_id)
        model = registry.get_model(
            member.provider_id, member.connection_id, member.model_id
        )
    except Exception:
        return (
            f"{member.model_id or '(unknown model)'}",
            "✗ no longer in the registry",
            False,
        )
    return (
        f"{model.label[:34]:<36}{provider.name[:14]:<16}{connection.name[:14]}",
        _limit_text(model.preset),
        True,
    )


# ── Combo list ────────────────────────────────────────────────────────────────

class ComboListScreen(MenuScreen):
    """
    Every combo. Mirrors `ProviderListScreen`, including its keys.

    A combo is offered in the model pickers whether or not it has members, so an
    empty one is shown here as a problem rather than left looking finished — it
    is the only screen that can fix it.
    """

    breadcrumb = "Model Groups"
    title_text = "Model Groups"

    BINDINGS = [
        Binding("a", "add", "add", show=False),
        Binding("d", "delete", "delete", show=False),
        Binding("c", "duplicate", "duplicate", show=False),
    ]

    def compose_extra(self) -> ComposeResult:
        yield Label("")
        yield Static(
            f"[{MUTED}]A group runs several models as one choice: Fallback tries "
            f"them in order, Round robin shares the work. Groups appear in the "
            f"Translate, Judge and Fix All model pickers under the "
            f"[/][{ACCENT}]Model Groups[/][{MUTED}] provider.[/]",
            classes="hint",
        )

    def build_options(self) -> list[Option]:
        options: list[Option] = []
        for combo in load_combos():
            broken = sum(
                0 if _member_label(self.app.registry, member)[2] else 1
                for member in combo.members
            )
            if not combo.members:
                hint, style = "● empty — add models", ERROR
            elif broken:
                hint, style = f"● {broken} missing model(s)", WARN
            else:
                hint, style = "● ready", OK
            options.append(
                Option(
                    f"{combo.label[:22]:<24}{combo.size} model(s)  "
                    f"{combo.strategy.replace('_', '-'):<13}"
                    f"{_limit_text(combo.preset)}",
                    combo.id,
                    hint=hint,
                    style=style,
                )
            )
        options.append(Option("+ New group", "__add__", style=ACCENT))
        return options

    def screen_keys(self) -> list[tuple[str, str]]:
        return [
            ("↑↓", "move"), ("⏎", "open"), ("a", "new"),
            ("c", "duplicate"), ("d", "delete"), ("←", "back"),
        ]

    def handle_choice(self, option: Option) -> None:
        if option.value == "__add__":
            self.action_add()
        else:
            self.app.push_screen(ComboDetailScreen(option.value))

    def action_add(self) -> None:
        self.app.push_screen(NewComboScreen())

    def action_duplicate(self) -> None:
        """
        Copy a combo under a new name.

        Worth a key of its own: "the same four models but round-robin" is the
        second combo most people want, and rebuilding it member by member is the
        one part of this flow that is genuinely tedious.
        """
        combo = self._current()
        if combo is None:
            return
        copy = Combo(
            name=unique_name(f"{combo.name} copy"),
            strategy=combo.strategy,
            members=[
                ComboMember(m.provider_id, m.connection_id, m.model_id)
                for m in combo.members
            ],
            preset=RateLimitPreset.from_dict(combo.preset.to_dict()),
        )
        try:
            save_combo(copy)
        except (ComboError, OSError) as exc:
            self.notify_status(str(exc), ERROR)
            return
        self.picker.set_options(self.build_options(), keep_index=True)
        self.notify_status(f"Created {copy.name}.", OK)

    def action_delete(self) -> None:
        combo = self._current()
        if combo is None:
            return
        try:
            delete_combo(combo.id)
        except OSError as exc:
            self.notify_status(str(exc), ERROR)
            return
        self.picker.set_options(self.build_options(), keep_index=True)
        self.notify_status(
            f"Deleted {combo.label}. Jobs already running are unaffected.", WARN
        )

    def _current(self) -> Combo | None:
        option = self.picker.current
        if option is None or option.value == "__add__":
            return None
        return find_combo(option.value)

    def on_screen_resumed(self) -> None:
        self.picker.set_options(self.build_options(), keep_index=True)


# ── New combo ─────────────────────────────────────────────────────────────────

class NewComboScreen(BaseScreen):
    """
    Just the name, then straight into the detail screen.

    Deliberately one field. A combo needs a name, a member list, an order, a
    strategy and a rate limit; asking for all five up front makes a five-step
    wizard out of something whose members are easier to judge once they are on
    screen together. So the name is asked here — it is the only thing that cannot
    be edited *in place* on an empty combo — and everything else is the detail
    screen, which is the same screen used to edit it later.
    """

    breadcrumb = "Model Groups › New"

    def compose_body(self) -> ComposeResult:
        yield Label("New group", classes="title")
        yield Label(
            "Name it something you will recognise in the model picker — "
            "\"premium-coding\", \"fallback-gemma\".",
            classes="hint",
        )
        yield Label("")
        self._name = LabeledInput("Group name", placeholder="Translator")
        yield self._name
        self._error = ErrorBox()
        yield self._error

    def screen_keys(self) -> list[tuple[str, str]]:
        return [("⏎", "create"), ("←", "back"), ("esc", "cancel")]

    def on_screen_ready(self) -> None:
        self._name.focus_input()

    def on_input_submitted(self, event) -> None:
        self._create()

    def _create(self) -> None:
        name = (self._name.value or "").strip()
        if not name:
            self._error.show("A group needs a name.", "cannot create")
            return
        # Created empty and saved on the way out instead of now: `save_combo`
        # refuses a memberless combo, which is the right rule for the file and the
        # wrong one for a form you have not finished filling in.
        combo = Combo(name=name, strategy="fallback",
                      preset=RateLimitPreset.custom(0, 0))
        self.app.pop_screen()
        self.app.push_screen(ComboDetailScreen(combo.id, draft=combo))


# ── Combo detail ──────────────────────────────────────────────────────────────

class ComboDetailScreen(BaseScreen):
    """
    Everything about one combo: members, their order, strategy, rate limit.

    `draft` carries an unsaved combo in from `NewComboScreen`. It is held in
    memory until it has a member, because a combo with none cannot be run and
    would be a trap in the model picker; every later edit saves immediately, the
    way `ConnectionDetailScreen` does.
    """

    BINDINGS = [
        Binding("a", "add_member", "add model", show=False),
        Binding("x", "remove_member", "remove", show=False),
        Binding("s", "strategy", "strategy", show=False),
        Binding("p", "rate_limit", "rate limit", show=False),
        Binding("r", "rename", "rename", show=False),
        Binding("D", "delete", "delete combo", show=False),
        Binding("[", "move_up", "move up", show=False),
        Binding("]", "move_down", "move down", show=False),
    ]

    def __init__(self, combo_id: str, draft: Combo | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._combo_id = combo_id
        # Set only until the first member makes the combo savable.
        self._draft = draft

    # ── State ─────────────────────────────────────────────────────────────────

    @property
    def combo(self) -> Combo | None:
        if self._draft is not None:
            return self._draft
        return find_combo(self._combo_id)

    def _persist(self, combo: Combo) -> bool:
        """
        Save, or keep it as a draft while it still has no members.

        Returns False when the write failed, having already said so — the caller
        then leaves the screen as it is rather than reporting success.
        """
        if not combo.members:
            self._draft = combo
            return True
        try:
            save_combo(combo)
        except (ComboError, OSError) as exc:
            self._error.show(str(exc), "not saved")
            return False
        self._draft = None
        self._error.clear()
        return True

    # ── Layout ────────────────────────────────────────────────────────────────

    def compose_body(self) -> ComposeResult:
        combo = self.combo
        name = combo.label if combo else "(missing)"
        self.breadcrumb = f"Model Groups › {name}"
        yield Label(name, classes="title")
        yield Static("", id="combo-head", classes="hint")
        yield Label("")
        yield Static(self._table_header(), classes="muted")
        self.picker = Picker()
        yield self.picker
        yield Label("")
        yield Static("", id="combo-note", classes="panel")
        self._error = ErrorBox()
        yield self._error

    @staticmethod
    def _table_header() -> str:
        return f"{'#':<4}{'MODEL':<36}{'PROVIDER':<16}{'CONNECTION':<16}MODEL RATE LIMIT"

    def screen_keys(self) -> list[tuple[str, str]]:
        return [
            ("↑↓", "move"), ("a", "add model"), ("x", "remove"),
            ("[]", "reorder"), ("s", "strategy"), ("p", "rate limit"),
            ("r", "rename"), ("D", "delete"), ("←", "back"),
        ]

    def on_screen_ready(self) -> None:
        self._refresh()
        self.picker.focus()

    def on_screen_resumed(self) -> None:
        self._refresh(keep=True)

    def _refresh(self, keep: bool = False) -> None:
        combo = self.combo
        if combo is None:
            self.picker.set_options([
                Option("This group no longer exists.", "", enabled=False, style=ERROR)
            ])
            return

        options: list[Option] = []
        for index, member in enumerate(combo.members, start=1):
            label, hint, ok = _member_label(self.app.registry, member)
            options.append(
                Option(
                    f"{index:<4}{label}",
                    member.key,
                    hint=hint,
                    style="" if ok else ERROR,
                )
            )
        if not options:
            options.append(
                Option("No models yet — press a to add one", "__none__",
                       enabled=False, style=MUTED)
            )
        self.picker.set_options(options, keep_index=keep)
        self.query_one("#combo-head", Static).update(
            f"{combo.strategy.replace('_', '-')} · {_limit_text(combo.preset)}"
            + ("   [unsaved — add a model to save it]" if self._draft is not None else "")
        )
        self.query_one("#combo-note", Static).update(self._note(combo))

    def _note(self, combo: Combo) -> str:
        """
        What this combo will actually do, in the terms that matter at run time.

        The worker count is stated because it is the one number a combo's own rate
        limit decides, and "no limit" is spelled out for the same reason it is in
        the table: 0 rpm looks like a mistake and reads as one.
        """
        preset = combo.preset
        workers = preset.workers_for()
        lines = [
            f"[{MUTED}]Strategy[/]    {STRATEGY_LABELS.get(combo.strategy, combo.strategy)}",
            f"[{MUTED}]Rate limit[/]  {preset.describe_limits()}",
            f"[{MUTED}]Workers[/]     {workers} — this combo's own limit decides it, "
            f"not the members'",
        ]
        if combo.strategy == "fallback" and combo.size > 1:
            lines.append(
                f"[{MUTED}]Order[/]       tried top to bottom; use [ and ] to change it[/]"
            )
        broken = [
            member for member in combo.members
            if not _member_label(self.app.registry, member)[2]
        ]
        if broken:
            lines.append(
                f"[{ERROR}]Missing[/]     {len(broken)} member(s) no longer exist and "
                f"will be skipped at run time"
            )
        if not combo.members:
            lines.append(
                f"[{WARN}]Empty[/]       a group with no models cannot run — add one"
            )
        return "\n".join(lines)

    def _current_member(self) -> str:
        option = self.picker.current
        if option is None or not option.enabled or not isinstance(option.value, str):
            return ""
        return "" if option.value.startswith("__") else option.value

    # ── Members ───────────────────────────────────────────────────────────────

    def action_add_member(self) -> None:
        self.app.push_screen(ComboMemberModal(), self._member_picked)

    def _member_picked(self, slot: tuple[str, str, str] | None) -> None:
        if not slot:
            return
        combo = self.combo
        if combo is None:
            return
        member = ComboMember(*slot)
        if not combo.add_member(member):
            self.notify_status("That model is already in this group.", WARN)
            return
        if not self._persist(combo):
            # Roll the in-memory change back so the screen matches the file.
            combo.remove_member(member.key)
            self._refresh(keep=True)
            return
        self._refresh(keep=True)
        self.refresh_keys()
        self.notify_status(f"Added {member.model_id}.", OK)

    def action_remove_member(self) -> None:
        key = self._current_member()
        combo = self.combo
        if not key or combo is None:
            return
        if combo.size == 1:
            # Removing the last member would leave a combo the model picker still
            # offers and no request can use. Deleting it is the honest action, and
            # `D` already does that.
            self.notify_status(
                "That is the only model — press D to delete the whole group.", WARN
            )
            return
        combo.remove_member(key)
        if not self._persist(combo):
            self._refresh(keep=True)
            return
        self._refresh(keep=True)
        self.notify_status("Model removed.", WARN)

    def action_move_up(self) -> None:
        self._move(-1)

    def action_move_down(self) -> None:
        self._move(1)

    def _move(self, offset: int) -> None:
        key = self._current_member()
        combo = self.combo
        if not key or combo is None:
            return
        if not combo.move_member(key, offset):
            return
        if not self._persist(combo):
            self._refresh(keep=True)
            return
        # Follow the row that moved, so holding `]` walks a member down the list
        # instead of dragging whatever lands under a fixed cursor.
        self.picker.index = max(0, min(self.picker.index + offset,
                                       len(self.picker.options) - 1))
        self._refresh(keep=True)

    # ── Settings ──────────────────────────────────────────────────────────────

    def action_strategy(self) -> None:
        combo = self.combo
        if combo is None:
            return
        if combo.size < 2:
            self.notify_status(
                "Strategy needs 2+ models — it is inert right now.", MUTED
            )
            return
        combo.strategy = (
            "round_robin" if combo.strategy == "fallback" else "fallback"
        )
        if not self._persist(combo):
            return
        self._refresh(keep=True)
        self.notify_status(
            f"Strategy set to {combo.strategy.replace('_', '-')}.", OK
        )

    def action_rate_limit(self) -> None:
        combo = self.combo
        if combo is None:
            return
        # The same modal the per-model limit uses, so "0 = no limit" behaves
        # identically wherever a rate limit is typed (CR-001 FR-605).
        self.app.push_screen(
            PresetModal(f"group {combo.label}"), self._rate_limit_picked
        )

    def _rate_limit_picked(self, preset: RateLimitPreset | None) -> None:
        if preset is None:
            return
        combo = self.combo
        if combo is None:
            return
        combo.preset = RateLimitPreset.from_dict(preset.to_dict())
        if not self._persist(combo):
            return
        self._refresh(keep=True)
        self.notify_status(
            f"Group rate limit: {_limit_text(combo.preset)} → "
            f"{combo.preset.workers_for()} worker(s).",
            OK,
        )

    def action_rename(self) -> None:
        combo = self.combo
        if combo is None:
            return
        self.app.push_screen(RenameComboModal(combo.name), self._renamed)

    def _renamed(self, name: str | None) -> None:
        combo = self.combo
        if not name or combo is None:
            return
        previous = combo.name
        combo.name = name
        if not self._persist(combo):
            combo.name = previous
            return
        self.set_header()
        self._refresh(keep=True)
        self.notify_status(f"Renamed to {name}.", OK)

    def action_delete(self) -> None:
        combo = self.combo
        if combo is None:
            self.app.pop_screen()
            return
        try:
            delete_combo(combo.id)
        except OSError as exc:
            self._error.show(str(exc), "not deleted")
            return
        self.app.pop_screen()


class RenameComboModal(ModalScreen[str]):
    BINDINGS = [Binding("escape", "dismiss_none", "cancel", show=False)]

    def __init__(self, current: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label("Rename group", classes="title")
            self._name = LabeledInput("Group name", value=self._current)
            yield self._name
            yield Label("", id="rename-error", classes="error")
            yield KeyHints([("⏎", "save"), ("esc", "cancel")])

    def on_mount(self) -> None:
        self._name.focus_input()

    def on_input_submitted(self, event) -> None:
        name = (self._name.value or "").strip()
        if not name:
            self.query_one("#rename-error", Label).update("A group needs a name.")
            return
        self.dismiss(name)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


# ── Member picker ─────────────────────────────────────────────────────────────

class ComboMemberModal(ModalScreen[tuple]):
    """
    Provider → Connection → Model, the same three columns as the wizard's step 2.

    Deliberately the same shape and the same keys: this is the picker the user
    already knows, and a combo member is exactly the thing it already picks. The
    difference is what it returns — a `(provider, connection, model)` triple for
    the combo to store — and that a connection must be chosen explicitly here,
    with no "auto" row: a member names one connection, because which key serves
    it is part of what makes two members different.
    """

    BINDINGS = [
        Binding("escape", "dismiss_none", "cancel", show=False),
        Binding("right", "focus_next_col", "next column", show=False),
        Binding("left", "focus_prev_col", "previous column", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label("Add a model to this group", classes="title")
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
            yield Static("", id="combo-pick-summary", classes="hint")
            yield KeyHints([("↑↓", "move"), ("←→", "column"),
                            ("⏎", "add"), ("esc", "cancel")])

    def on_mount(self) -> None:
        self.providers.set_options([
            Option(p.name, p.id, hint=f"{len(p.connections)} conn")
            for p in self.app.registry.providers
        ] or [Option("No providers yet", "", enabled=False, style=MUTED)])
        self._load_connections()
        for picker in (self.providers, self.connections, self.models):
            if len(picker.options) > 1:
                picker.focus()
                return
        self.models.focus()

    def _provider(self):
        option = self.providers.current
        if option is None or not option.value:
            return None
        try:
            return self.app.registry.get_provider(option.value)
        except Exception:
            return None

    def _load_connections(self) -> None:
        provider = self._provider()
        if provider is None:
            self.connections.set_options([])
            self.models.set_options([])
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
            self._summary()
            return
        connection = provider.find_connection(option.value)
        models = connection.models if connection else []
        self.models.set_options([
            Option(m.label, m.id, hint=_limit_text(m.preset)) for m in models
        ] or [Option("No models on this connection", "", enabled=False, style=MUTED)])
        self._summary()

    def _summary(self) -> None:
        panel = self.query_one("#combo-pick-summary", Static)
        provider = self._provider()
        connection_option = self.connections.current
        model_option = self.models.current
        if provider is None or connection_option is None \
                or model_option is None or not model_option.value:
            panel.update("Nothing to add here.")
            return
        panel.update(
            f"{provider.name} · {connection_option.label} · {model_option.label}"
        )

    def on_picker_highlighted(self, event: Picker.Highlighted) -> None:
        if event.picker is self.providers:
            self._load_connections()
        elif event.picker is self.connections:
            self._load_models()
        else:
            self._summary()

    def action_focus_next_col(self) -> None:
        order = [self.providers, self.connections, self.models]
        for index, picker in enumerate(order):
            if picker.has_focus and index + 1 < len(order):
                order[index + 1].focus()
                return

    def action_focus_prev_col(self) -> None:
        order = [self.providers, self.connections, self.models]
        for index, picker in enumerate(order):
            if picker.has_focus and index > 0:
                order[index - 1].focus()
                return

    def on_picker_selected(self, event: Picker.Selected) -> None:
        provider = self._provider()
        connection_option = self.connections.current
        model_option = self.models.current
        if provider is None or connection_option is None or model_option is None:
            return
        if not model_option.value:
            return
        self.dismiss((provider.id, connection_option.value, model_option.value))

    def action_dismiss_none(self) -> None:
        self.dismiss(None)
