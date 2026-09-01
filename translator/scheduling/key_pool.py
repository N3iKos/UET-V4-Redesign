"""
scheduling/key_pool.py — Per-connection key pool.

Adapted from v3.6's UniversalKeyPool with two changes:
  * scoped to ONE Connection instead of a whole job (01_ARCHITECTURE.md §5),
  * no print() anywhere — v3.6 printed pool events straight to the terminal;
    v4 reports them through an optional callback so only tui/ renders.

A Connection normally carries a single api_key, but the pool stays list-based
so a connection holding several keys load-balances exactly like v3.6 did.
Keyless (local) connections get one sentinel entry with an empty key.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterator, Literal

from .. import config

KeyStatus = Literal["active", "cooldown", "exhausted"]

# No fictional per-model token cap. v4 talks to arbitrary endpoints and has no
# way to know a provider's real daily token budget, so it must NEVER invent one:
# a made-up cap turned a long-but-healthy job into a false "daily limit reached"
# once cumulative usage crossed it. `0` means "no token cap" — a connection is
# only ever exhausted on a *real* signal from the provider (a confirmed daily
# 429, or a remaining-requests-day header that reads 0).
DEFAULT_TPD = 0


@dataclass
class KeyState:
    key:             str
    tpd_limit:       int
    status:          KeyStatus  = "active"
    cooldown_until:  float      = 0.0
    tpd_used:        int        = 0
    rpd_remaining:   int        = 999_999
    requests_ok:     int        = 0
    requests_fail:   int        = 0
    total_latency:   float      = 0.0
    last_used:       float      = 0.0
    last_error_type: str | None = None
    tpm_reset_in:    int        = 0

    @property
    def avg_latency(self) -> float:
        calls = self.requests_ok + self.requests_fail
        return self.total_latency / calls if calls > 0 else 0.0

    @property
    def tpd_exhausted(self) -> bool:
        return self.tpd_limit > 0 and self.tpd_used >= self.tpd_limit * 0.95

    def masked(self) -> str:
        k = (self.key or "").strip()
        if len(k) > 8:
            return f"{k[:4]}{'*' * (len(k) - 8)}{k[-4:]}"
        return "****" if k else "(no key)"


class AllKeysExhausted(RuntimeError):
    """Every key on this connection hit a daily limit — connection is done."""


class KeyPool:
    """
    Thread-safe pool of the keys belonging to one Connection.

    Worker flow (unchanged from v3.6):
      1. acquire()  → blocks until a key is usable
      2. use state.key for the request
      3. record_success / record_rate_limit / record_503 / record_error
    """

    def __init__(
        self,
        keys: list[str] | None = None,
        tpd_limit: int = DEFAULT_TPD,
        on_event: Callable[[str, KeyState], None] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._on_event = on_event
        # Set by the executor (via the connection pool): a force-cancel wakes
        # a worker parked in acquire() instead of letting it wait out a key
        # cooldown the run no longer cares about.
        self.stop_event: threading.Event | None = None
        entries = [k for k in (keys or []) if k is not None]
        if not entries:
            entries = [""]      # keyless/local connection
        self._states: dict[str, KeyState] = {
            k: KeyState(key=k, tpd_limit=tpd_limit) for k in entries
        }

    # ── Acquisition ───────────────────────────────────────────────────────────

    def acquire(self, timeout: float = 600.0) -> KeyState:
        """
        Block until a key is usable and return it.
        Raises AllKeysExhausted if every key hit a daily limit,
        TimeoutError if nothing frees up within `timeout`.

        When `stop_event` is set by the executor, a stop during the wait raises
        TimeoutError immediately with a "cancelled" message — the retry loop's
        top-of-loop stop check then exits without another attempt.
        """
        deadline = time.time() + timeout
        while True:
            with self._lock:
                self._refresh_cooldowns()
                if self._all_exhausted():
                    raise AllKeysExhausted(
                        "All keys on this connection are exhausted (daily limit reached)."
                    )
                state = self._best_active_key()
                if state is not None:
                    state.last_used = time.time()
                    self._event.clear()
                    return state
                wait = self._soonest_cooldown_wait()

            if self.stop_event is not None and self.stop_event.is_set():
                raise TimeoutError(
                    "Cancelled — keys were still cooling down when the run stopped."
                )
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError("No key on this connection became available in time.")
            if self.stop_event is not None:
                waited = self.stop_event.wait(timeout=min(wait, remaining, 2.0))
                if waited:
                    continue          # loop back to the stop check above
            else:
                self._event.wait(timeout=min(wait, remaining, 2.0))

    def try_acquire(self) -> KeyState | None:
        """Non-blocking variant — None if nothing is usable right now."""
        with self._lock:
            self._refresh_cooldowns()
            state = self._best_active_key()
            if state is not None:
                state.last_used = time.time()
            return state

    # ── Recording ─────────────────────────────────────────────────────────────

    def record_success(self, key: str, tokens: int, headers: dict | None = None) -> None:
        from ..providers.http_utils import parse_header_int

        with self._lock:
            state = self._states.get(key)
            if state is None:
                return
            state.tpd_used += max(0, tokens)
            state.requests_ok += 1
            state.last_error_type = None

            if headers:
                # Day-scoped only: the window counter hitting 0 is a per-minute
                # limit, and treating it as a spent day would retire a healthy
                # key mid-job.
                rpd = parse_header_int(
                    headers,
                    ("x-ratelimit-remaining-requests-day",),
                )
                if rpd >= 0:
                    state.rpd_remaining = rpd
                reset = parse_header_int(headers, ("x-ratelimit-reset-tokens-minute",))
                if reset > 0:
                    state.tpm_reset_in = reset

            if state.tpd_exhausted:
                self._exhaust(state, "token daily limit")
            elif state.rpd_remaining == 0:
                self._exhaust(state, "request daily limit")

    def record_rate_limit(
        self,
        key: str,
        delay: int,
        is_daily: bool = False,
        headers: dict | None = None,
    ) -> None:
        from ..providers.http_utils import parse_header_int

        with self._lock:
            state = self._states.get(key)
            if state is None:
                return
            state.requests_fail += 1

            if headers:
                # Only the day-scoped header proves daily exhaustion. The plain
                # window counter (often per-minute) reading 0 is an ordinary
                # short rate limit, not a spent quota.
                rpd = parse_header_int(
                    headers,
                    ("x-ratelimit-remaining-requests-day",),
                )
                if rpd == 0:
                    is_daily = True
                reset = parse_header_int(headers, ("x-ratelimit-reset-tokens-minute",))
                if reset > 0 and not is_daily:
                    delay = max(delay, reset + 1)

            if is_daily:
                state.last_error_type = "daily_limit"
                self._exhaust(state, "daily limit (429)")
            else:
                state.last_error_type = "rate_limit"
                state.status = "cooldown"
                state.cooldown_until = time.time() + max(1, delay)
                self._emit("cooldown", state)
        self._event.set()

    def record_503(self, key: str) -> None:
        """Short penalty cooldown — not treated as exhaustion."""
        with self._lock:
            state = self._states.get(key)
            if state is None:
                return
            state.requests_fail += 1
            state.last_error_type = "503"
            state.status = "cooldown"
            state.cooldown_until = time.time() + config.RETRY_DELAY_503_PENALTY
            self._emit("cooldown", state)
        self._event.set()

    def record_error(self, key: str, latency: float = 0.0) -> None:
        """Non-rate-limit error — key stays active, only the failure is tracked."""
        with self._lock:
            state = self._states.get(key)
            if state is None:
                return
            state.requests_fail += 1
            state.total_latency += latency
            state.last_error_type = "error"

    def record_latency(self, key: str, latency: float) -> None:
        with self._lock:
            state = self._states.get(key)
            if state:
                state.total_latency += latency

    def refresh_key(self, key: str) -> None:
        """
        Promote one key out of an expired cooldown.

        v3.6 needed this because _refresh_cooldowns only ran inside
        wait_for_key(), which pinned parallel workers never called.
        """
        with self._lock:
            state = self._states.get(key)
            if state and state.status == "cooldown" and time.time() >= state.cooldown_until:
                state.status = "active"
                self._event.set()

    # ── Introspection ─────────────────────────────────────────────────────────

    def state_for(self, key: str) -> KeyState | None:
        return self._states.get(key)

    def all_keys(self) -> list[str]:
        return list(self._states.keys())

    def active_count(self) -> int:
        with self._lock:
            self._refresh_cooldowns()
            return self._count_active()

    def all_exhausted(self) -> bool:
        with self._lock:
            return self._all_exhausted()

    def cooldown_remaining(self) -> float:
        """Seconds until the soonest cooldown expires (0 if a key is free now)."""
        with self._lock:
            self._refresh_cooldowns()
            if self._best_active_key() is not None:
                return 0.0
            return self._soonest_cooldown_wait()

    def total_tokens_consumed(self) -> int:
        with self._lock:
            return sum(s.tpd_used for s in self._states.values())

    def requests_ok(self) -> int:
        with self._lock:
            return sum(s.requests_ok for s in self._states.values())

    def requests_fail(self) -> int:
        with self._lock:
            return sum(s.requests_fail for s in self._states.values())

    def iter_states(self) -> Iterator[KeyState]:
        with self._lock:
            yield from list(self._states.values())

    # ── Internals (call with lock held) ───────────────────────────────────────

    def _emit(self, kind: str, state: KeyState) -> None:
        if self._on_event is not None:
            self._on_event(kind, state)

    def _exhaust(self, state: KeyState, reason: str) -> None:
        state.status = "exhausted"
        state.last_error_type = state.last_error_type or reason
        self._emit("exhausted", state)

    def _refresh_cooldowns(self) -> None:
        now = time.time()
        freed = False
        for state in self._states.values():
            if state.status == "cooldown" and now >= state.cooldown_until:
                state.status = "active"
                freed = True
        if freed:
            self._event.set()

    def _best_active_key(self) -> KeyState | None:
        candidates = [
            s for s in self._states.values()
            if s.status == "active" and not s.tpd_exhausted and s.rpd_remaining > 0
        ]
        if not candidates:
            return None
        # Spread load by token usage, break ties by latency — same as v3.6.
        return min(candidates, key=lambda s: (s.tpd_used, s.avg_latency))

    def _count_active(self) -> int:
        return sum(
            1 for s in self._states.values()
            if s.status == "active" and not s.tpd_exhausted
        )

    def _all_exhausted(self) -> bool:
        return all(s.status == "exhausted" for s in self._states.values())

    def _soonest_cooldown_wait(self) -> float:
        cooling = [s for s in self._states.values() if s.status == "cooldown"]
        if not cooling:
            return 2.0
        return max(0.1, min(s.cooldown_until for s in cooling) - time.time())
