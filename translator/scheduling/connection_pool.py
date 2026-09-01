"""
scheduling/connection_pool.py — Connection selection across one Provider.

New in v4: v3.6 had no concept of several interchangeable connections for a
single provider. This tier sits above KeyPool and implements the two
strategies from 03_PROVIDER_SYSTEM.md §5:

  round_robin : exhaust one full wave (batch_size x parallel rows) on a
                connection, then move to the next and cycle. Switching is
                driven by ROWS SERVED, not requests completed, because
                parallel workers finish out of order.
  fallback    : stay on one connection until it errors or hits a cooldown
                too long to absorb inline, then advance to the next.

A provider with one connection degenerates to "always return that one",
without the UI having to enforce it.

**What the pool hands out is a slot, not a connection.** A slot is "somewhere a
request can go": a connection, the base URL to send it to, and optionally the
model to ask for. For a plain provider there is one slot per connection and the
model is whatever the job says, which is exactly the old behaviour. Combos
(FEATURE-005) need the other two fields — their members span providers, so the
endpoint differs per slot, and two members may share one connection while naming
different models, so identity has to be the slot rather than the connection.
That is why `ConnectionState` and the reporting methods are keyed by slot id and
why `ComboPool` can subclass this rather than reimplementing rotation, cooldown
tracking and exhaustion.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Literal

from .. import config
from ..providers.models import Connection, Provider
from .key_pool import KeyPool

ConnectionStatus = Literal["active", "cooldown", "exhausted"]


@dataclass
class PoolSlot:
    """
    Somewhere a request can be sent.

    For a provider there is one slot per connection and `model_id` is empty,
    meaning "whatever the job asked for" — identical to the old behaviour. A
    combo fills all three fields, because its members span providers (so the
    endpoint differs per slot) and two members may share one connection while
    naming different models (so the connection alone cannot identify a slot).
    """

    slot_id:    str
    connection: Connection
    base_url:   str = ""
    model_id:   str = ""
    label:      str = ""

    @property
    def name(self) -> str:
        return self.label or self.connection.name


@dataclass
class ConnectionState:
    connection_id: str            # the slot id; equal to the connection id for a provider
    key_pool:      KeyPool
    name:          str = ""
    status:        ConnectionStatus = "active"
    cooldown_until: float = 0.0
    requests_ok:   int = 0
    requests_fail: int = 0
    rows_served_this_wave: int = 0
    rows_served_total:     int = 0

    @property
    def is_available(self) -> bool:
        return self.status == "active"


@dataclass
class ConnectionLease:
    """What a worker gets back: which connection to use, and its key pool."""
    connection:  Connection
    state:       ConnectionState
    key_pool:    KeyPool
    wave_index:  int = 0
    switched:    bool = False   # True when this lease changed the active connection
    # Where to send it and what to ask for. Empty means "as configured on the
    # job", which is what every provider lease says; a combo lease names both,
    # because only the pool knows which member is serving this request.
    base_url:    str = ""
    model_id:    str = ""
    slot_label:  str = ""

    @property
    def connection_id(self) -> str:
        """
        The id every `report_*` call is keyed by.

        The *slot*, not the connection: for a provider the two are the same
        string, and for a combo two slots may share a connection and must be
        cooled down, rotated and counted separately.
        """
        return self.state.connection_id

    @property
    def name(self) -> str:
        return self.slot_label or self.connection.name


@dataclass
class SwitchEvent:
    """Reported to the caller (TUI) whenever the active connection changes."""
    from_name: str
    to_name:   str
    reason:    Literal["wave_complete", "error", "cooldown", "exhausted"]
    wave_index: int = 0
    detail:    str = ""


class AllConnectionsUnavailable(RuntimeError):
    """Every connection is exhausted or cooling down past the deadline."""


class ConnectionPool:
    """
    Owns one ConnectionState (and thus one KeyPool) per slot, and decides which
    slot serves the next request.

    For a Provider a slot is a Connection, which is the whole of the original
    behaviour. The indirection exists so `ComboPool` can supply slots that span
    providers and models without touching rotation, cooldowns or exhaustion —
    those are the parts worth having exactly once.

    Thread-safe: parallel workers call acquire()/report_* concurrently.
    Never prints — switches are surfaced through the on_switch callback.
    """

    def __init__(
        self,
        provider: Provider,
        wave_size: int = 0,
        tpd_limit: int | None = None,
        on_switch: Callable[[SwitchEvent], None] | None = None,
        connection_ids: list[str] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._provider = provider
        self._on_switch = on_switch
        self._wave_size = max(0, wave_size)
        self._wave_index = 0
        self._index = 0
        # Set by whoever runs requests over this pool (RetryExecutor): a
        # force-cancel must wake a worker parked in acquire() immediately
        # rather than after the cooldown it is waiting out.
        self.stop_event: threading.Event | None = None

        candidates = provider.connections
        if connection_ids:
            # JobConfig pinned a specific connection — honour it and ignore
            # the strategy entirely.
            candidates = [c for c in provider.connections if c.id in connection_ids]
        if not candidates:
            raise AllConnectionsUnavailable("Provider has no usable connections.")

        self._init_slots(
            [
                PoolSlot(
                    slot_id=conn.id,
                    connection=conn,
                    base_url=provider.base_url,
                    label=conn.name,
                )
                for conn in candidates
            ],
            tpd_limit=tpd_limit,
        )

    def _init_slots(
        self, slots: list[PoolSlot], tpd_limit: int | None = None
    ) -> None:
        """
        Install the slots and give each one its own KeyPool.

        Separate from `__init__` because a subclass builds its slots from
        something other than a Provider; everything after this point is shared.
        """
        if not slots:
            raise AllConnectionsUnavailable("No usable connections.")
        self._slots: list[PoolSlot] = list(slots)
        self._states: dict[str, ConnectionState] = {}
        for slot in self._slots:
            keys = [slot.connection.api_key] if slot.connection.api_key else []
            pool_kwargs = {"keys": keys}
            if tpd_limit is not None:
                pool_kwargs["tpd_limit"] = tpd_limit
            self._states[slot.slot_id] = ConnectionState(
                connection_id=slot.slot_id,
                name=slot.name,
                key_pool=KeyPool(**pool_kwargs),
            )

    # ── Introspection ─────────────────────────────────────────────────────────

    @property
    def strategy(self) -> str:
        # Strategy is inert with a single slot — no special-casing needed
        # further down the stack.
        return self._provider.strategy if len(self._slots) >= 2 else "fallback"

    @property
    def wave_index(self) -> int:
        return self._wave_index

    @property
    def wave_size(self) -> int:
        return self._wave_size

    def set_wave_size(self, wave_size: int) -> None:
        with self._lock:
            self._wave_size = max(0, wave_size)

    def states(self) -> list[ConnectionState]:
        with self._lock:
            return [self._states[slot.slot_id] for slot in self._slots]

    def state_for(self, connection_id: str) -> ConnectionState:
        return self._states[connection_id]

    def key_pool_for(self, connection_id: str) -> KeyPool:
        return self._states[connection_id].key_pool

    def total_tokens_consumed(self) -> int:
        return sum(s.key_pool.total_tokens_consumed() for s in self._states.values())

    def active_connection(self) -> Connection:
        with self._lock:
            return self._current_slot().connection

    def slots(self) -> list[PoolSlot]:
        with self._lock:
            return list(self._slots)

    # ── Acquisition ───────────────────────────────────────────────────────────

    def acquire(self, rows: int = 0, timeout: float = 600.0) -> ConnectionLease:
        """
        Pick the slot that should serve the next `rows` rows.

        Round-robin counts the rows against the current wave and rotates once
        the wave is full. Fallback keeps the current slot until
        report_* marks it unusable.

        `stop_event` (an attribute set by the executor) is checked between
        waits: a force-cancel must reach a worker parked in this loop
        immediately, not after the cooldown it is waiting out. The wait is an
        interruptible event-wait when the flag is wired, so the check above
        runs the moment the stop fires.
        """
        deadline = time.time() + timeout
        while True:
            with self._lock:
                self._refresh_cooldowns()

                if self.strategy == "round_robin":
                    lease = self._acquire_round_robin(rows)
                else:
                    lease = self._acquire_fallback()

                if lease is not None:
                    state = lease.state
                    state.rows_served_this_wave += rows
                    state.rows_served_total += rows
                    return lease

                if self._all_exhausted():
                    raise AllConnectionsUnavailable(self._report_total_failure())
                wait = self._soonest_cooldown_wait()

            if self.stop_event is not None and self.stop_event.is_set():
                raise AllConnectionsUnavailable(
                    "Cancelled — every connection was still waiting out a "
                    "cooldown when the run stopped."
                )
            remaining = deadline - time.time()
            if remaining <= 0:
                raise AllConnectionsUnavailable(
                    "No connection became available before the timeout."
                )
            if self.stop_event is not None:
                # An interruptible wait: wakes the moment the stop fires, so
                # the check above runs immediately instead of after the tick.
                self.stop_event.wait(timeout=min(wait, remaining, 2.0))
            else:
                time.sleep(min(wait, remaining, 2.0))

    def _report_total_failure(self) -> str:
        """
        Called once at the moment nothing is left, and returns what to say.

        A hook rather than a plain message so a subclass can *announce* total
        failure as well as describe it: `AllConnectionsUnavailable` is caught by
        `RetryExecutor` and turned into a per-batch error, which reaches the
        events rail but not the notice channel — and "every model in your combo
        is gone" is not something to leave someone to infer from a run that
        simply produced nothing.
        """
        return self._exhausted_message()

    def _exhausted_message(self) -> str:
        """Overridden by ComboPool, which has a different thing to say."""
        return (
            "All connections for this provider are unavailable. "
            "This may be a temporary rate limit — try again shortly."
        )

    def _current_slot(self) -> PoolSlot:
        return self._slots[self._index % len(self._slots)]

    def _lease(self, slot: PoolSlot, state: ConnectionState, switched: bool = False):
        return ConnectionLease(
            slot.connection, state, state.key_pool, self._wave_index,
            switched=switched,
            base_url=slot.base_url,
            model_id=slot.model_id,
            slot_label=slot.label,
        )

    def _acquire_round_robin(self, rows: int) -> ConnectionLease | None:
        current = self._current_slot()
        state = self._states[current.slot_id]

        # Wave complete on this slot → rotate before serving.
        if (
            self._wave_size > 0
            and state.rows_served_this_wave >= self._wave_size
            and len(self._slots) > 1
        ):
            return self._rotate("wave_complete")

        if state.is_available:
            return self._lease(current, state)

        # Current one is down; look for any other usable slot.
        return self._rotate("cooldown" if state.status == "cooldown" else "exhausted")

    def _acquire_fallback(self) -> ConnectionLease | None:
        current = self._current_slot()
        state = self._states[current.slot_id]
        if state.is_available:
            return self._lease(current, state)
        return self._rotate("cooldown" if state.status == "cooldown" else "exhausted")

    def _rotate(self, reason: str, detail: str = "") -> ConnectionLease | None:
        """Advance to the next usable slot. None if none is usable now."""
        total = len(self._slots)
        previous = self._current_slot()

        for step in range(1, total + 1):
            candidate = self._slots[(self._index + step) % total]
            state = self._states[candidate.slot_id]
            if not state.is_available:
                continue

            self._index = (self._index + step) % total
            state.rows_served_this_wave = 0
            if reason == "wave_complete":
                self._wave_index += 1
            if candidate.slot_id != previous.slot_id:
                self._states[previous.slot_id].rows_served_this_wave = 0
                self._emit_switch(
                    SwitchEvent(
                        from_name=previous.name,
                        to_name=candidate.name,
                        reason=reason,          # type: ignore[arg-type]
                        wave_index=self._wave_index,
                        detail=detail,
                    )
                )
            return self._lease(
                candidate, state, switched=candidate.slot_id != previous.slot_id
            )
        return None

    # ── Reporting ─────────────────────────────────────────────────────────────

    def report_success(self, connection_id: str) -> None:
        with self._lock:
            self._states[connection_id].requests_ok += 1

    def report_error(self, connection_id: str, detail: str = "") -> None:
        """
        A plain (non-rate-limit) error — a timeout, a broken reply, an
        unexpected exception.

        Rotates away from the failing slot under BOTH strategies. The old rule
        was fallback-only ("stick to one connection until it errors"), but a
        round-robin pool that keeps handing requests to a slot that just timed
        out burns the rest of its wave on the same dead endpoint — the rotation
        was per-wave, so a member that dies mid-wave holds its slot until the
        wave completes. A short park (CONNECTION_ERROR_COOLDOWN) stops the very
        next `_rotate()` from picking the failing slot straight back up, and
        `_is_current` stops parallel workers failing on the same slot from
        rotating more than once.
        """
        with self._lock:
            state = self._states[connection_id]
            state.requests_fail += 1
            if self._is_current(connection_id):
                # Park it briefly, otherwise the very next _rotate() would pick
                # the failing connection straight back up and flap A->B->A.
                if len(self._slots) > 1:
                    state.status = "cooldown"
                    state.cooldown_until = time.time() + config.CONNECTION_ERROR_COOLDOWN
                self._rotate("error", detail)

    def report_cooldown(self, connection_id: str, delay: float, detail: str = "") -> None:
        """
        Rate limit on this connection. A cooldown short enough to absorb
        inline leaves the connection in place; anything longer than
        MAX_INLINE_COOLDOWN parks it and moves on (mirrors v3.6's threshold).
        """
        with self._lock:
            state = self._states[connection_id]
            state.requests_fail += 1
            if delay <= config.MAX_INLINE_COOLDOWN and self.strategy == "round_robin":
                return
            state.status = "cooldown"
            state.cooldown_until = time.time() + max(1.0, delay)
            if self._is_current(connection_id):
                self._rotate("cooldown", detail or f"cooldown {delay:.0f}s")

    def report_exhausted(self, connection_id: str, detail: str = "") -> None:
        with self._lock:
            state = self._states[connection_id]
            state.status = "exhausted"
            if self._is_current(connection_id):
                self._rotate("exhausted", detail)

    # ── Internals ─────────────────────────────────────────────────────────────

    def _is_current(self, connection_id: str) -> bool:
        """
        Only the slot currently in front may trigger a rotation. Without this,
        several parallel workers failing on the same slot each rotate once and
        the pool flaps between slots.
        """
        return self._current_slot().slot_id == connection_id

    def _emit_switch(self, event: SwitchEvent) -> None:
        if self._on_switch is not None:
            self._on_switch(event)

    def _refresh_cooldowns(self) -> None:
        now = time.time()
        for state in self._states.values():
            if state.status == "cooldown" and now >= state.cooldown_until:
                state.status = "active"
            if state.status == "active" and state.key_pool.all_exhausted():
                state.status = "exhausted"

    def _all_exhausted(self) -> bool:
        return all(s.status == "exhausted" for s in self._states.values())

    def _soonest_cooldown_wait(self) -> float:
        cooling = [s for s in self._states.values() if s.status == "cooldown"]
        if not cooling:
            return 1.0
        return max(0.1, min(s.cooldown_until for s in cooling) - time.time())
