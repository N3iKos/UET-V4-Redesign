"""
scheduling/retry.py — RetryExecutor: one LLM request, with backoff.

v3.6 had two executors: RetryExecutor (shared pool, sequence/adaptive) and
DedicatedKeyExecutor (key-pinned, parallel). In v4 they collapse into ONE
class, because the thing a worker is pinned to is now the *connection*, and
connection selection lives in ConnectionPool — so both former variants
reduce to "ask the pool who serves me, then run the request against that
connection's key pool" (03_PROVIDER_SYSTEM.md §8).

It is also where reasoning is resolved for the translation path: the executor
knows which connection (and therefore which ModelEntry) is serving this
request, so it can combine the model's metadata with the job's override and
hand the client a finished specification (ADR-003). Whether reasoning ended up
active is reported through `on_phase` so the Activity Panel can show a `think`
phase — the reasoning *content* is never passed on, let alone rendered.

The same reasoning is why a combo needs no new executor (FEATURE-005). A lease
may name its own endpoint and model, so "which model am I about to ask, and
where" is answered per attempt from the lease rather than once from the job —
and a combo's fallback becomes the retry loop it already had, with rotation
handled by the pool underneath.

Nothing here prints; v3.6's log_warn/countdown calls become RetryEvent
callbacks the TUI can render.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Literal

from .. import config
from ..core.models import JobConfig, ProviderError
from ..providers.client import ProviderClient
from ..providers.reasoning import ResolvedReasoning, resolve_for
from .connection_pool import AllConnectionsUnavailable, ConnectionPool
from .key_pool import AllKeysExhausted

EventKind = Literal[
    "attempt", "rate_limit", "cooldown", "server_error", "error",
    "exhausted", "switched", "success", "gave_up",
]


@dataclass
class RetryEvent:
    kind:        EventKind
    label:       str = ""
    attempt:     int = 0
    max_attempts: int = 0
    delay:       float = 0.0
    connection:  str = ""
    message:     str = ""       # raw provider text, never sanitised away


@dataclass
class ExecutionResult:
    content:     str | None
    tokens:      int = 0
    latency:     float = 0.0
    attempts:    int = 0
    connection:  str = ""
    error:       str = ""       # raw provider error of the final failure
    error_kind:  str = ""       # UET classification of that failure


class RetryExecutor:
    """
    Executes a single prompt against whichever connection the pool hands out,
    retrying per JobConfig (max_attempts / retry_mode / delays).
    """

    def __init__(
        self,
        connection_pool: ConnectionPool,
        job: JobConfig,
        base_url: str,
        on_event: Callable[[RetryEvent], None] | None = None,
        stop_event: threading.Event | None = None,
        on_phase: Callable[[str, bool], None] | None = None,
    ) -> None:
        self._pool = connection_pool
        self._job = job
        self._base_url = base_url
        self._on_event = on_event
        self._stop = stop_event or threading.Event()
        # (phase, reasoning_active) — the Activity Panel's only reasoning
        # signal. Content never travels this channel (FR-205 / NFR-104).
        self._on_phase = on_phase
        self._clients: dict[str, ProviderClient] = {}
        self._clients_lock = threading.Lock()

    # ── Public ────────────────────────────────────────────────────────────────

    def execute(
        self,
        prompt: str,
        label: str = "",
        rows: int = 0,
        use_repair_model: bool = False,
        response_format: dict | None = None,
    ) -> ExecutionResult:
        job = self._job
        # What the job asked for. A combo lease overrides it per attempt, because
        # only the pool knows which member is serving this one.
        requested_model = job.effective_repair_model() if use_repair_model else job.model_id
        model = requested_model
        infinite = job.retry_mode == "infinite"
        attempt = 0
        last_error = ""
        last_error_kind = ProviderError.KIND_UNKNOWN
        connection_name = ""
        # Wave accounting happens once per unit of work, not once per attempt,
        # or a retry-heavy batch would rotate the round-robin wave early.
        rows_to_charge = rows

        while not self._stop.is_set():
            attempt += 1
            if not infinite and attempt > job.max_attempts:
                break

            try:
                lease = self._pool.acquire(rows=rows_to_charge)
            except AllConnectionsUnavailable as exc:
                # Every connection is genuinely gone (confirmed quota, or nothing
                # freed within the acquire timeout). Propagate rather than
                # swallowing into a per-batch error: the scheduler stops the run
                # once and says why, instead of every remaining batch re-acquiring
                # and reproducing the same message (the 224/225/226 cascade).
                self._emit("exhausted", label, attempt, message=str(exc))
                raise
            rows_to_charge = 0
            connection_name = lease.name

            try:
                key_state = lease.key_pool.acquire(timeout=self._acquire_timeout())
            except AllKeysExhausted as exc:
                last_error = str(exc)
                self._pool.report_exhausted(lease.connection_id, str(exc))
                self._emit("exhausted", label, attempt,
                           connection=connection_name, message=last_error)
                continue
            except TimeoutError as exc:
                last_error = str(exc)
                self._pool.report_cooldown(
                    lease.connection_id, lease.key_pool.cooldown_remaining(), str(exc)
                )
                self._emit("cooldown", label, attempt,
                           connection=connection_name, message=last_error)
                continue

            key = key_state.key
            # The lease decides where this goes and, for a combo, which model to
            # ask for: a combo's members live on different providers, so the
            # endpoint is a property of the slot rather than of the job. An empty
            # (or absent) `base_url`/`model_id` means "as the job says", which is
            # what every provider lease sends — read with getattr so any
            # lease-shaped object still works.
            base_url = getattr(lease, "base_url", "") or self._base_url
            model = getattr(lease, "model_id", "") or requested_model
            client = self._client_for(key, base_url)
            # The lease names the connection, so the ModelEntry (and its
            # reasoning metadata) is only knowable here — not when the job was
            # built, and not inside the client.
            reasoning = self._reasoning_for(lease.connection, model)
            started = time.time()

            try:
                self._emit("attempt", label, attempt, connection=connection_name)
                self._phase("think" if reasoning.active else "generating",
                            reasoning.active)
                result = client.generate(
                    model=model,
                    prompt=prompt,
                    temperature=job.temperature,
                    reasoning=reasoning,
                    max_completion_tokens=self._max_completion_tokens(),
                    response_format=response_format,
                )
                latency = time.time() - started
                lease.key_pool.record_success(key, result.tokens, result.headers)
                lease.key_pool.record_latency(key, latency)
                self._pool.report_success(lease.connection_id)
                self._emit("success", label, attempt, connection=connection_name)
                return ExecutionResult(
                    content=result.content,
                    tokens=result.tokens,
                    latency=latency,
                    attempts=attempt,
                    connection=connection_name,
                )

            except ProviderError as exc:
                latency = time.time() - started
                last_error = exc.response_text or str(exc)
                last_error_kind = getattr(exc, "kind", ProviderError.KIND_UNKNOWN)
                self._phase("retry", False)
                self._handle_provider_error(
                    exc, lease, key, label, attempt, latency, connection_name
                )

            except Exception as exc:      # unexpected, keep the job alive
                latency = time.time() - started
                last_error = f"{type(exc).__name__}: {exc}"
                last_error_kind = ProviderError.KIND_UNKNOWN
                self._phase("retry", False)
                lease.key_pool.record_error(key, latency)
                self._pool.report_error(lease.connection_id, last_error)
                self._emit("error", label, attempt,
                           connection=connection_name, message=last_error)
                self._sleep(job.retry_delay_err)

        self._emit("gave_up", label, attempt, connection=connection_name, message=last_error)
        return ExecutionResult(
            content=None,
            attempts=max(0, attempt - 1),
            connection=connection_name,
            error=last_error,
            error_kind=last_error_kind,
        )

    # ── Error handling ────────────────────────────────────────────────────────

    def _handle_provider_error(
        self,
        exc: ProviderError,
        lease,
        key: str,
        label: str,
        attempt: int,
        latency: float,
        connection_name: str,
    ) -> None:
        job = self._job
        preview = exc.preview()
        kind = getattr(exc, "kind", ProviderError.KIND_UNKNOWN)

        # Confirmed quota / daily / billing wall — the *only* case that retires
        # a connection. Everything else is temporary and must leave it usable.
        if kind == ProviderError.KIND_QUOTA:
            lease.key_pool.record_rate_limit(
                key, exc.retry_delay, is_daily=True, headers=exc.headers
            )
            self._pool.report_exhausted(lease.connection_id, preview)
            self._emit("exhausted", label, attempt,
                       connection=connection_name, message=preview)
            return

        if exc.is_rate_limit:
            # Temporary rate limit: cool the connection down and let it recover.
            # Never treated as exhaustion regardless of how the 429 was phrased.
            lease.key_pool.record_rate_limit(
                key, exc.retry_delay, is_daily=False, headers=exc.headers
            )
            if exc.retry_delay > config.MAX_INLINE_COOLDOWN:
                # Too long to absorb here — park the connection and let the
                # pool hand out another one on the next attempt.
                self._pool.report_cooldown(lease.connection_id, exc.retry_delay, preview)
                self._emit("cooldown", label, attempt, delay=exc.retry_delay,
                           connection=connection_name, message=preview)
                return
            self._pool.report_cooldown(lease.connection_id, exc.retry_delay, preview)
            self._emit("rate_limit", label, attempt, delay=exc.retry_delay,
                       connection=connection_name, message=preview)
            self._sleep(exc.retry_delay)
            return

        if kind == ProviderError.KIND_SERVER or exc.status_code in (500, 502, 503, 504):
            if getattr(exc, "is_timeout", False):
                # The request ran past the user's request_timeout — the endpoint
                # stopped answering, which is not a blip the same slot should be
                # trusted to clear on the next attempt. Rotate away from the
                # slot *now* (report_error parks it briefly and moves the pool
                # on) so the retry lands on a different connection or combo
                # member, and no retry-delay is slept: the request already cost
                # the user N seconds of waiting.
                lease.key_pool.record_error(key, latency)
                self._pool.report_error(lease.connection_id, preview)
                self._emit("error", label, attempt,
                           connection=connection_name, message=preview)
                return
            # Temporary server/gateway failure: short penalty cooldown, retry or
            # let the pool fall back — not exhaustion.
            lease.key_pool.record_503(key)
            self._pool.report_cooldown(
                lease.connection_id, config.RETRY_DELAY_503_PENALTY, preview
            )
            self._emit("server_error", label, attempt, delay=job.retry_delay_503,
                       connection=connection_name, message=preview)
            self._sleep(job.retry_delay_503)
            return

        # auth / invalid / unknown — a real error to surface, but no assumption
        # about quota. Record the failure and, under fallback, let the pool move
        # to another connection; the connection is not permanently retired.
        lease.key_pool.record_error(key, latency)
        self._pool.report_error(lease.connection_id, preview)
        self._emit("error", label, attempt, delay=job.retry_delay_err,
                   connection=connection_name, message=preview)
        self._sleep(job.retry_delay_err)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _reasoning_for(self, connection, model_id: str) -> ResolvedReasoning:
        """
        Combine the model's metadata with the job's override. A model that is
        not on this connection (or carries no metadata) yields the safe
        fallback: no reasoning parameter at all.
        """
        entry = None
        try:
            entry = connection.find_model(model_id)
        except Exception:
            entry = None
        return resolve_for(
            entry,
            override=self._job.reasoning_enabled,
            level=self._job.reasoning_effort,
        )

    def _phase(self, phase: str, reasoning_active: bool) -> None:
        if self._on_phase is None:
            return
        try:
            self._on_phase(phase, reasoning_active)
        except Exception:
            pass          # a status callback must never break a request

    def _client_for(self, key: str, base_url: str = "") -> ProviderClient:
        """
        One client per (endpoint, key).

        Keyed by both because a combo can put two members with different base
        URLs behind the same API key — a single-key cache would then send one
        member's request to the other's endpoint.

        The per-request timeout comes from Settings (`request_timeout`), read
        once per client rather than per attempt: a mid-run Settings change
        applies from the *next* client built, which is the same freshness rule
        every other preference here follows. A request that exceeds it raises
        a read timeout inside the client, which this loop counts as a failed
        attempt — so a hung model loses its slot instead of holding it.
        """
        url = base_url or self._base_url
        cache_key = f"{url}\n{key}"
        with self._clients_lock:
            client = self._clients.get(cache_key)
            if client is None:
                client = ProviderClient(url, key or None,
                                        timeout=self._request_timeout())
                self._clients[cache_key] = client
            return client

    @staticmethod
    def _request_timeout() -> float:
        """
        The user's per-request timeout, in seconds. Never raises: prefs are a
        convenience, and a request must never fail because the prefs file
        could not be read — the shipped default is the answer then.
        """
        try:
            from ..data.app_prefs import request_timeout

            return float(request_timeout())
        except Exception:
            return float(config.DEFAULT_REQUEST_TIMEOUT)

    def _max_completion_tokens(self) -> int:
        """
        Cap generation length. Without this some providers assume the full
        context window, which triggers premature TPM limits and lets a
        runaway generation burn thousands of tokens (v3.6 Bug 7).
        """
        if self._job.max_completion_tokens > 0:
            return self._job.max_completion_tokens
        per_row = max(200, int(self._job.tok_per_row * 3.0))
        return int(max(1, self._job.batch_size) * per_row)

    def _acquire_timeout(self) -> float:
        return float(max(60, config.MAX_INLINE_COOLDOWN * 2))

    def _sleep(self, seconds: float) -> None:
        """Interruptible sleep so cancel/Ctrl+C doesn't wait out a backoff."""
        if seconds > 0:
            self._stop.wait(timeout=seconds)

    def _emit(
        self,
        kind: EventKind,
        label: str,
        attempt: int,
        delay: float = 0.0,
        connection: str = "",
        message: str = "",
    ) -> None:
        if self._on_event is None:
            return
        self._on_event(
            RetryEvent(
                kind=kind,
                label=label,
                attempt=attempt,
                max_attempts=0 if self._job.retry_mode == "infinite" else self._job.max_attempts,
                delay=delay,
                connection=connection,
                message=message,
            )
        )
