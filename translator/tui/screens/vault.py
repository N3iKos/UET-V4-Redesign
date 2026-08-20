"""
tui/screens/vault.py — Vault unlock / first-run setup.

Same UX as v3.6's password prompt, restyled. A wrong password never says
more than it should, but a corrupt-vault case is reported plainly.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.widgets import Input, Label

from ... import config
from ..app import BaseScreen, MainMenuScreen
from ..widgets.common import LabeledInput


class VaultScreen(BaseScreen):
    breadcrumb = "Vault"

    def compose_body(self) -> ComposeResult:
        first_run = not self.app.registry.vault_exists
        self._first_run = first_run

        yield Label(
            "Set a master password" if first_run else "Unlock vault",
            classes="title",
        )
        yield Label(config.VAULT_FILE, classes="hint")
        yield Label("")

        self._password = LabeledInput(
            "Master password",
            password=True,
            hint=(
                "This encrypts your API keys at rest. There is no recovery — "
                "if you forget it, the vault is gone."
                if first_run else ""
            ),
        )
        yield self._password

        self._confirm = LabeledInput("Confirm password", password=True)
        self._confirm.display = first_run
        yield self._confirm

        yield Label("", id="vault-error", classes="error")

        self._reset = LabeledInput(
            "Type DELETE to erase the vault and start over",
            hint="Every saved provider and API key is destroyed. This cannot be undone.",
        )
        self._reset.display = False
        yield self._reset

    def screen_keys(self) -> list[tuple[str, str]]:
        if getattr(self, "_reset_offered", False):
            return [("⏎", "confirm reset"), ("esc", "keep trying"), ("^C", "quit")]
        return [("⏎", "unlock"), ("^C", "quit")]

    def on_screen_ready(self) -> None:
        self._attempts = 0
        self._reset_offered = False
        self._password.focus_input()

    def action_back(self) -> None:
        if self._reset_offered:
            self._cancel_reset()

    def action_cancel(self) -> None:
        if self._reset_offered:
            self._cancel_reset()
            return
        self.app.exit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input is self._reset.input:
            self._confirm_reset()
            return
        if self._first_run and event.input is self._password.input:
            self._confirm.focus_input()
            return
        self._attempt()

    def _attempt(self) -> None:
        error = self.query_one("#vault-error", Label)
        password = self._password.input.value

        if not password:
            error.update("Password cannot be empty.")
            return

        if self._first_run:
            if password != self._confirm.input.value:
                error.update("Passwords do not match.")
                self._confirm.input.value = ""
                self._confirm.focus_input()
                return

        if not self.app.registry.unlock(password):
            self._attempts += 1
            self._password.input.value = ""
            remaining = config.VAULT_MAX_ATTEMPTS - self._attempts
            if remaining > 0:
                error.update(
                    f"Wrong password — vault not opened. "
                    f"{remaining} attempt{'s' if remaining != 1 else ''} left "
                    f"before a reset is offered."
                )
                self._password.focus_input()
            else:
                self._offer_reset(error)
            return

        if self._first_run:
            self.app.registry.save()      # write an empty vault immediately

        self.app.pop_screen()
        self.app.push_screen(MainMenuScreen())

    # ── Forgotten password ────────────────────────────────────────────────────

    def _offer_reset(self, error: Label) -> None:
        """
        After VAULT_MAX_ATTEMPTS the only way forward is deleting the vault.
        Guarded by a typed confirmation so a run of typos can't wipe real keys.
        """
        self._reset_offered = True
        error.update(
            f"Wrong password {config.VAULT_MAX_ATTEMPTS} times. "
            f"There is no recovery — the vault can only be erased and rebuilt."
        )
        self._password.display = False
        self._reset.display = True
        self._reset.input.value = ""
        self._reset.focus_input()
        self.refresh_keys()

    def _cancel_reset(self) -> None:
        self._reset_offered = False
        self._attempts = 0
        self._reset.display = False
        self._reset.input.value = ""
        self._password.display = True
        self._password.input.value = ""
        self.query_one("#vault-error", Label).update("")
        self._password.focus_input()
        self.refresh_keys()

    def _confirm_reset(self) -> None:
        error = self.query_one("#vault-error", Label)
        if self._reset.input.value.strip() != "DELETE":
            error.update("Type DELETE exactly, or press Esc to keep trying.")
            self._reset.input.value = ""
            self._reset.focus_input()
            return

        self.app.registry.destroy_vault()
        self.app.pop_screen()
        self.app.push_screen(VaultScreen())
