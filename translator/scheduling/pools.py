"""
scheduling/pools.py — One place that decides which pool a job runs on.

Two callers build a pool from a JobConfig — `core/engine.py` and
`judge/runner.py` — and a third (`tui/screens/run.py`) builds one early so it can
attach its own `on_switch` feed. Without a shared factory, "is this a combo?"
would be answered in three places and could drift; with one, adding a combo to
the Judge path took no new decision at all.

The rule is one line: a job carrying `combo_id` gets a `ComboPool`, anything else
gets a `ConnectionPool`. Everything above — Scheduler, RetryExecutor,
ProviderClient — is unchanged either way, which is the point of FEATURE-005
being a selection feature rather than an execution one.
"""

from __future__ import annotations

from typing import Callable

from ..core.models import JobConfig
from ..data.combos import find_combo
from .combo_pool import ComboPool
from .connection_pool import AllConnectionsUnavailable, ConnectionPool, SwitchEvent


def build_pool(
    job: JobConfig,
    registry,
    provider=None,
    on_switch: Callable[[SwitchEvent], None] | None = None,
    on_notice: Callable[[str, str], None] | None = None,
):
    """
    The pool this job should run on.

    `provider` is optional and only used on the non-combo path, where the caller
    has usually already fetched it. A combo needs the registry instead: its
    members name several providers and it resolves them itself.

    Raises `AllConnectionsUnavailable` when the combo no longer resolves to a
    single usable model — the same exception the provider path raises for an
    empty provider, so callers need no second failure mode.
    """
    if job.uses_combo:
        combo = find_combo(job.combo_id)
        if combo is None:
            raise AllConnectionsUnavailable(
                f"Combo {job.model_id or job.combo_id!r} no longer exists — "
                f"pick a model or combo again."
            )
        return ComboPool(
            combo,
            registry,
            on_switch=on_switch,
            on_notice=on_notice,
        )

    if provider is None:
        provider = registry.get_provider(job.provider_id)
    return ConnectionPool(
        provider,
        connection_ids=[job.connection_id] if job.connection_id else None,
        on_switch=on_switch,
    )


def base_url_for(job: JobConfig, registry, provider=None) -> str:
    """
    The base URL a Scheduler should be given.

    Empty for a combo: every lease carries its own, because the members can sit
    on different providers, and a single URL for the job would be wrong for all
    but one of them.
    """
    if job.uses_combo:
        return ""
    if provider is None:
        try:
            provider = registry.get_provider(job.provider_id)
        except Exception:
            return ""
    return getattr(provider, "base_url", "")
