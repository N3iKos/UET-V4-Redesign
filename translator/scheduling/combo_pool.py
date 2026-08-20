"""
scheduling/combo_pool.py — ComboPool: one combo's members as a pool of slots.

A Combo is a new way of *choosing and rotating* a ModelEntry, not a new way of
executing one (FEATURE-005). So this is a `ConnectionPool` subclass rather than
a parallel execution path: rotation, cooldown tracking, exhaustion, wave
accounting and the `SwitchEvent` feed all come from the base class unchanged,
and the Scheduler, RetryExecutor and ProviderClient above it cannot tell the
difference.

What a combo adds is that a slot names its own endpoint and model:

  * members may sit on different providers, so `base_url` travels on the lease
    rather than being fixed once for the whole job;
  * two members may share a connection while naming different models, so slot
    identity is `provider/connection/model`, not the connection id.

Both are already accommodated by `PoolSlot`/`ConnectionLease`; this module only
has to build the slots and answer `strategy` from the combo instead of from a
Provider.

Resolution against the registry happens here, once, at construction — combos
store ids and a model can be deleted after a combo names it. A member that no
longer resolves is reported and dropped rather than left to fail per request; if
none resolve, construction raises `AllConnectionsUnavailable`, the same failure
the translate path already knows how to show.
"""

from __future__ import annotations

import threading
from typing import Callable

from ..data.combos import Combo
from ..providers.models import Connection, Provider
from .connection_pool import (
    AllConnectionsUnavailable,
    ConnectionPool,
    PoolSlot,
    SwitchEvent,
)


class ComboPool(ConnectionPool):
    """
    The pool for one Combo. Same contract as ConnectionPool, wider reach.

    `registry` is anything with `get_provider`/`get_connection`/`get_model` —
    the real `ProviderRegistry` in the app, a stub in a test — so this module
    never imports the registry and cannot be tempted to mutate it.
    """

    def __init__(
        self,
        combo: Combo,
        registry,
        wave_size: int = 0,
        tpd_limit: int | None = None,
        on_switch: Callable[[SwitchEvent], None] | None = None,
        on_notice: Callable[[str, str], None] | None = None,
    ) -> None:
        # The base class's __init__ wants a Provider; a combo has none, so the
        # shared state is set up here and `_init_slots` is called directly.
        self._lock = threading.RLock()
        self._combo = combo
        self._on_switch = on_switch
        self._on_notice = on_notice
        self._wave_size = max(0, wave_size)
        self._wave_index = 0
        self._index = 0
        # Total failure is announced at most once, however many workers reach it.
        self._announced_failure = False
        # A Provider is still needed for `strategy`'s base implementation to have
        # something to read; the property below overrides it, and this keeps any
        # other base-class attribute access honest.
        self._provider = Provider(name=combo.label, strategy=combo.strategy)

        slots, missing = _resolve(combo, registry)
        if missing and on_notice is not None:
            # Said out loud rather than silently skipped: a combo that has
            # quietly shrunk from four models to one behaves very differently,
            # and the user is the only one who can fix it.
            on_notice(
                "warn",
                f"{combo.label}: {len(missing)} model(s) in this combo no longer "
                f"exist and were skipped — " + ", ".join(missing[:3])
                + ("…" if len(missing) > 3 else ""),
            )
        if not slots:
            raise AllConnectionsUnavailable(
                f"Combo {combo.label!r} has no usable models — every model it "
                f"names has been removed or renamed. Edit it under Combos."
            )
        self._init_slots(slots, tpd_limit=tpd_limit)

    # ── Combo-specific behaviour ──────────────────────────────────────────────

    @property
    def combo(self) -> Combo:
        return self._combo

    @property
    def strategy(self) -> str:
        """
        The combo's own strategy, inert with a single member.

        Same shape as the Provider rule it replaces: with one slot there is
        nothing to rotate between, and saying so here keeps the special case out
        of the acquisition code.
        """
        return self._combo.strategy if len(self._slots) >= 2 else "fallback"

    def _report_total_failure(self) -> str:
        """
        Every model in this combo has failed — say so, out loud, once.

        `AllConnectionsUnavailable` alone is not enough. `RetryExecutor` catches
        it and turns it into a per-batch failure, so a run whose every member is
        exhausted ends with rows marked `[TRANSLATE_FAILED]` and nothing that
        names the cause — and with a combo the cause is exactly the thing the user
        needs to know, because it is a configuration they can fix. So the notice
        goes out here, at the moment it becomes true, and the exception still
        propagates as before (FEATURE-005 total-failure rule).

        Announced once per pool: several parallel workers hit this together, and
        the same sentence four times reads as four different problems.
        """
        message = self._exhausted_message()
        if self._on_notice is not None and not self._announced_failure:
            self._announced_failure = True
            try:
                self._on_notice("error", message)
            except Exception:
                pass        # a notice must never break the failure path
        return message

    def _exhausted_message(self) -> str:
        """
        What total failure means for a combo, in the combo's own terms.

        Names the combo and its strategy, because "every connection is exhausted"
        would send someone to the wrong screen: the fix is in Combos, not in the
        provider they think they are running.
        """
        return (
            f"Every model in combo {self._combo.label!r} has failed or hit its "
            f"limit ({self._combo.strategy.replace('_', '-')}). Nothing is left "
            f"to fall back to."
        )

    def model_ids(self) -> list[str]:
        """The models this pool can serve, in combo order — for a UI summary."""
        with self._lock:
            return [slot.model_id for slot in self._slots]


def _resolve(combo: Combo, registry) -> tuple[list[PoolSlot], list[str]]:
    """
    Turn a combo's stored ids into slots, reporting what no longer exists.

    Returns `(slots, missing_labels)`. A member is dropped when its provider,
    connection or model is gone — the ids are references into the vault and the
    vault is edited independently, so this is an expected state rather than
    corruption.
    """
    slots: list[PoolSlot] = []
    missing: list[str] = []
    for member in combo.members:
        try:
            provider = registry.get_provider(member.provider_id)
            connection = registry.get_connection(member.provider_id, member.connection_id)
            model = registry.get_model(
                member.provider_id, member.connection_id, member.model_id
            )
        except Exception:
            missing.append(member.model_id or member.key)
            continue
        if not isinstance(connection, Connection):
            missing.append(member.model_id or member.key)
            continue
        slots.append(
            PoolSlot(
                slot_id=member.key,
                connection=connection,
                base_url=provider.base_url,
                model_id=model.id,
                # Named for the panel: "provider · connection · model" is what
                # tells two members of a combo apart, and a bare connection name
                # would not when both live on the same provider.
                label=f"{provider.name} · {connection.name} · {model.label}",
            )
        )
    return slots, missing
