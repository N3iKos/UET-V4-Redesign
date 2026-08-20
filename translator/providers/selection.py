"""
providers/selection.py — "which model is this job going to ask?", answered once.

Three wizards choose a model: Translate (`ModelSelectScreen`), Judge
(`JudgeModelScreen`) and Fix All (`FixAllSetupScreen`). Since a combo can be
chosen wherever a model can (FEATURE-005), each of them would otherwise need the
same four judgements — is this a combo, what is its rate limit, what does the
JobConfig look like, what do I print — and three copies of that is where they
start to disagree.

So a choice is a value: `ModelChoice`. It resolves against the registry and the
combo store, answers the questions above, and knows how to fill in a JobConfig.
The screens keep their pickers and their layout; what they no longer keep is an
opinion about combos.

Two deliberate asymmetries, both because a combo is several models:

  * **prompt_format** is `plain` for a combo. It is a per-model opt-in for
    structured output, and one member accepting `response_format` says nothing
    about the next; sending it to a member that rejects it would fail the request
    outright. Any member that wants JSON can still be used on its own.
  * **reasoning** is left to the executor. `RetryExecutor` already resolves it
    from whichever ModelEntry the lease named, which is the only layer that knows
    which member is serving a given request.

Nothing here does I/O beyond reading the combo file, and nothing here mutates the
registry.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..data.combos import COMBO_PROVIDER_ID, Combo, find_combo
from .ratelimit import RateLimitPreset


def is_combo_provider(provider_id: str) -> bool:
    """True for the pseudo-provider combos are listed under."""
    return (provider_id or "") == COMBO_PROVIDER_ID


@dataclass
class ModelChoice:
    """
    One resolved model-or-combo choice.

    `model_id` is a real model id for a plain choice and a *combo id* for a
    combo. `label` is what a screen should print in either case — for a combo
    that is its name, which is what the user picked it by.
    """

    provider_id:   str
    connection_id: str
    model_id:      str
    combo:         Combo | None = None
    # The ModelEntry, when there is exactly one. None for a combo, and None for a
    # plain choice whose model has since been removed.
    entry:         object | None = None
    provider:      object | None = None
    connection:    object | None = None

    @property
    def is_combo(self) -> bool:
        return self.combo is not None

    @property
    def label(self) -> str:
        if self.combo is not None:
            return self.combo.label
        entry = self.entry
        return getattr(entry, "label", None) or self.model_id

    @property
    def preset(self) -> RateLimitPreset:
        """
        The rate limit that paces this choice.

        A combo's own, explicitly, rather than anything derived from its members
        (FEATURE-005 pacing rule): whether two members' limits add up depends on
        whether they are separate accounts, which the app cannot know. So the
        combo states its limit, and `rpm = 0` means none (CR-001).
        """
        if self.combo is not None:
            return self.combo.preset
        entry = self.entry
        preset = getattr(entry, "preset", None)
        return preset if preset is not None else RateLimitPreset.default()

    @property
    def prompt_format(self) -> str:
        """`plain` for a combo — see the module docstring."""
        if self.combo is not None:
            return "plain"
        return getattr(self.entry, "prompt_format", "plain") or "plain"

    @property
    def base_url(self) -> str:
        """Empty for a combo: each lease carries its own endpoint."""
        if self.combo is not None:
            return ""
        return getattr(self.provider, "base_url", "")

    def describe(self) -> str:
        """
        One line naming what was chosen, for a summary panel.

        A combo says how many models and how it moves between them, because those
        two facts are the whole difference between "premium-coding" and any single
        model.
        """
        if self.combo is not None:
            return (
                f"Combo {self.combo.label} · {self.combo.size} model(s) · "
                f"{self.combo.strategy.replace('_', '-')}"
            )
        provider_name = getattr(self.provider, "name", "?")
        connection_name = getattr(self.connection, "name", "")
        tail = f" · {connection_name}" if connection_name else ""
        return f"{provider_name}{tail} · {self.label}"

    def apply_to(self, job) -> None:
        """
        Write this choice into a JobConfig.

        The one place that decides what a combo job looks like: `provider_id` is
        the pseudo-provider, `model_id` carries the combo's *name* so every screen
        and log line that prints it says something meaningful, and `combo_id` is
        what the pool is actually built from.
        """
        if self.combo is not None:
            job.provider_id = COMBO_PROVIDER_ID
            job.connection_id = ""
            job.model_id = self.combo.name
            job.combo_id = self.combo.id
        else:
            job.provider_id = self.provider_id
            job.connection_id = self.connection_id
            job.model_id = self.model_id
            job.combo_id = ""
        job.prompt_format = self.prompt_format   # type: ignore[assignment]


def resolve_choice(registry, provider_id: str, connection_id: str, model_id: str) -> ModelChoice:
    """
    Build a `ModelChoice` from the three ids a picker produced.

    Tolerant by design: a model or combo that has since been deleted yields a
    choice whose `entry`/`combo` is None rather than raising, so a screen can say
    so instead of crashing. Callers that must have a runnable choice check
    `is_runnable`.
    """
    if is_combo_provider(provider_id):
        return ModelChoice(
            provider_id=COMBO_PROVIDER_ID,
            connection_id="",
            model_id=model_id,
            combo=find_combo(model_id),
        )

    provider = connection = entry = None
    try:
        provider = registry.get_provider(provider_id)
    except Exception:
        provider = None
    if provider is not None:
        if connection_id:
            try:
                connection = registry.get_connection(provider_id, connection_id)
            except Exception:
                connection = None
        if connection is not None:
            entry = connection.find_model(model_id)
        else:
            # An "auto" connection choice, or a stale id: the model's metadata is
            # the same wherever it is served from, so the first match is enough to
            # read a preset and a prompt format off.
            entry = next(
                (
                    model
                    for conn in getattr(provider, "connections", [])
                    for model in conn.models
                    if model.id == model_id
                ),
                None,
            )
    return ModelChoice(
        provider_id=provider_id,
        connection_id=connection_id,
        model_id=model_id,
        entry=entry,
        provider=provider,
        connection=connection,
    )


def choice_is_runnable(choice: ModelChoice) -> tuple[bool, str]:
    """
    Can this choice be run, and if not, why not — in words a user can act on.

    A combo with no members left is the interesting case: it exists, it is
    selectable, and it would fail on the first request. Saying so at selection
    time is cheaper than saying it after the first batch.
    """
    if choice.is_combo:
        if choice.combo is None:
            return False, "That combo no longer exists — pick another."
        if not choice.combo.members:
            return False, (
                f"Combo {choice.combo.label} has no models in it. "
                f"Add some under Combos."
            )
        return True, ""
    if choice.entry is None:
        return False, "That model is no longer in the registry — pick another."
    return True, ""
