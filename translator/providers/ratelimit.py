"""
providers/ratelimit.py — RateLimitPreset, the preset table, and the pacing
derivations: how many rows per request, and how many workers.

No network calls, no knowledge of any specific provider. The rpm/tpm figures in
the table are starting estimates for a tier (02_DATA_MODELS.md §2 / §7), meant
to be replaced with a provider's real numbers.

**Worker count is derived, never tabulated.** Two limits bind a run and they
bind differently: RPM caps how many requests a minute, TPM caps how many tokens
a minute, and which one runs out first depends on how big a request is. A batch
of 50 rows at 40k TPM sustains a quarter of the requests a batch of 10 does. So
`workers_for()` computes the number from both limits *and* the size of the
request being sent, and every caller — Quick mode, the wizard's Full mode
default, and Judge — asks it rather than reading a fixed 2 from a table.

The derivation reproduces the hand-calibrated table closely (2/2/3/4 against the
old 1/2/4/5), which is the main evidence that it is sound; where it differs it
differs upward, because the table was written before TPM was part of the sum.

**`rpm = 0` means no rate limit at all** (CR-001 / ADR-013). It is a first-class
state rather than a small number: `unlimited` is asked once and every pacing
derivation branches on it, so a local server with no quota is paced by
`PARALLEL_MAX` and the machine, not by an arithmetic accident that used to turn
0 into one request a minute. When `rpm == 0` the tpm figure is ignored for the
worker count too — "not clamped by rpm/tpm at all" is one rule to remember, and
two rules that differ by which field is zero would be two.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from .. import config

PresetName = Literal["safe", "average", "speed", "ultra_fast", "custom"]

# Tuning knobs for the derivation formula (02_DATA_MODELS.md §7 items 1-2).
WAVE_SAFETY_MARGIN        = 1.2    # doc suggested starting range 1.1-1.3
BATCH_MIN, BATCH_MAX      = 5, 50
PARALLEL_MAX              = 8

# How long one request takes, send to parsed reply. The single figure here that
# cannot be derived from a rate limit — it depends on the model, the batch and
# the network. 20s is a middling batch of 20-25 rows against a hosted model;
# local servers are slower, small batches faster.
#
# Being wrong about it costs throughput, never a 429: it only decides how many
# workers are needed to keep the pipe full, and the per-request pacing delay
# still enforces both limits underneath.
ASSUMED_REQUEST_SECONDS = 20.0

# A row is sent once and comes back once, so it is charged twice. This is the
# same factor the batch derivation has always used (its 300 was 2 x 150).
TOKENS_PER_ROW_ROUND_TRIP = 2

# Fixed cost of the instruction block every request carries, whatever its size.
REQUEST_OVERHEAD_TOKENS = 400

# name -> (rpm, tpm, batch). Batch is a tier's *published shape*, not a
# derivation: a bigger batch is not a rate-limit violation, it is a desync and
# latency trade, so it stays a stated choice. Parallelism is derived from it.
_PRESET_TABLE: dict[str, tuple[int, int, int]] = {
    "safe":       (5,   15_000,  10),
    "average":    (15,  40_000,  20),
    "speed":      (35,  80_000,  25),
    "ultra_fast": (45, 120_000,  30),
}

PRESET_LABELS: dict[str, str] = {
    "safe":       "Safe",
    "average":    "Average",
    "speed":      "Speed",
    "ultra_fast": "Ultra Fast",
    "custom":     "Custom",
}

DEFAULT_PRESET_NAME: PresetName = "average"


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


@dataclass
class RateLimitPreset:
    name: PresetName = DEFAULT_PRESET_NAME
    rpm:  int = 15
    tpm:  int = 40_000
    # Cached derivations — recomputed by derive() whenever rpm/tpm change.
    suggested_batch_size: int = 20
    suggested_parallel:   int = 2

    # ── Derived pacing ────────────────────────────────────────────────────────
    @property
    def unlimited(self) -> bool:
        """
        No rate limit at all (CR-001 FR-601).

        `rpm <= 0` is the single source of that judgement — an empty field, a
        typed 0 and a negative all arrive here as "unclamped". Everything below
        branches on this property rather than re-comparing numbers, so the
        answer cannot differ between two call sites.
        """
        return self.rpm <= 0

    @property
    def delay_per_request(self) -> float:
        """
        Inter-request spacing implied by RPM.

        This is the floor under everything else: N workers each waiting this long
        between requests send N times as many, which is why the worker count has
        to come from the same arithmetic rather than a separate table.

        Unlimited means 0.0 — there is no spacing to enforce. The old fallback
        of 60.0 was the slowest run the app could produce, which is the opposite
        of what someone typing 0 asked for.
        """
        if self.unlimited:
            return 0.0
        return 60.0 / self.rpm

    @property
    def wave_delay(self) -> float:
        return round(self.delay_per_request * WAVE_SAFETY_MARGIN, 3)

    def max_rows_per_minute(self) -> int:
        """
        `rpm * batch`, or 0 when unlimited — where 0 means "no ceiling", not
        "no throughput". A UI showing this figure has to check `unlimited`
        first; the number on its own cannot carry that distinction.
        """
        if self.unlimited:
            return 0
        return self.rpm * self.suggested_batch_size

    # ── Rate-limit arithmetic ─────────────────────────────────────────────────

    def tokens_per_request(self, rows: int, tok_per_row: int | None = None) -> int:
        """
        What one request of `rows` rows is expected to cost, both directions.

        Overhead is counted once per request and rows twice each (prompt and
        reply), which is the same shape `judge/estimator.py` uses — the two must
        agree or a Judge run would be paced against a different number than it
        is split by.
        """
        per_row = int(tok_per_row or config.DEFAULT_TOK_PER_ROW)
        return REQUEST_OVERHEAD_TOKENS + max(1, rows) * per_row * TOKENS_PER_ROW_ROUND_TRIP

    def cost_of(
        self,
        rows: int,
        tok_per_row: int | None = None,
        tokens_per_request: int | None = None,
    ) -> int:
        """
        The token cost of one request — measured if the caller knows it, derived
        if not.

        Judge knows it exactly: `judge/estimator.py` has already priced every
        section in order to split them. Passing that figure in beats re-deriving
        it from a per-row average, because a review's cost is dominated by how
        long the rows are rather than how many there are.
        """
        if tokens_per_request:
            return max(1, int(tokens_per_request))
        return self.tokens_per_request(rows, tok_per_row)

    def requests_per_minute_budget(
        self,
        rows: int,
        tok_per_row: int | None = None,
        tokens_per_request: int | None = None,
    ) -> float:
        """
        How many requests a minute *both* limits allow, at this request size.

        The binding constraint is whichever is smaller. This is the number that
        makes the difference visible: at 40k TPM a 20-row request affords 6.2
        requests a minute, so 15 RPM is unreachable and the run is
        token-bound — while a 5-row request affords 15, and RPM binds instead.

        Unlimited returns `inf`: an unbounded allowance, not the zero budget the
        old `rpm <= 0` branch returned. tpm is deliberately not consulted there —
        with no request limit there is nothing to divide the token allowance
        into, and FR-602 asks for one rule, not one per field.
        """
        if self.unlimited:
            return math.inf
        by_requests = float(self.rpm)
        if self.tpm <= 0:
            return by_requests
        by_tokens = self.tpm / self.cost_of(rows, tok_per_row, tokens_per_request)
        return min(by_requests, by_tokens)

    def workers_for(
        self,
        rows: int | None = None,
        tok_per_row: int | None = None,
        request_seconds: float = ASSUMED_REQUEST_SECONDS,
        tokens_per_request: int | None = None,
    ) -> int:
        """
        Workers needed to use the allowance without exceeding it.

        A worker spends `request_seconds` in flight per request, so one worker
        delivers `60 / request_seconds` requests a minute. Dividing the budget by
        that gives the count that keeps the pipe full: more would queue behind
        the pacing delay and buy nothing, fewer would leave the allowance unused.

        Clamped to 1…`PARALLEL_MAX` — a rate limit generous enough to want 30
        workers still meets a machine that cannot usefully run them, and eight is
        where the run screen stops being readable.

        Rounded down, so the estimate errs toward under-using an allowance rather
        than toward a 429.

        With no rate limit the clamp *is* the answer: `PARALLEL_MAX`, the same
        ceiling that binds a very generous limit (FR-602).
        """
        if self.unlimited:
            return PARALLEL_MAX
        rows = int(rows or self.suggested_batch_size)
        budget = self.requests_per_minute_budget(rows, tok_per_row, tokens_per_request)
        if budget <= 0:
            return 1
        per_worker = 60.0 / max(1.0, float(request_seconds))
        return _clamp(int(math.floor(budget / per_worker)) or 1, 1, PARALLEL_MAX)

    def sustainable_rows_per_minute(
        self,
        rows: int | None = None,
        tok_per_row: int | None = None,
        tokens_per_request: int | None = None,
    ) -> int:
        """
        Rows a minute this preset really sustains, both limits considered.

        0 when unlimited, meaning "not bounded by a limit" — the caller has to
        check `unlimited` before printing it, for the same reason as
        `max_rows_per_minute()`.
        """
        if self.unlimited:
            return 0
        rows = int(rows or self.suggested_batch_size)
        budget = self.requests_per_minute_budget(rows, tok_per_row, tokens_per_request)
        return int(budget * rows)

    def limited_by(
        self,
        rows: int | None = None,
        tok_per_row: int | None = None,
        tokens_per_request: int | None = None,
    ) -> str:
        """
        Which limit binds at this request size: `"requests"`, `"tokens"`,
        `"both"`, or `"unlimited"`. Shown in the UI, because "you are
        token-bound, use smaller batches" is actionable and a worker count on
        its own is not.

        `"unlimited"` is a fifth value rather than a forced `"requests"`: saying
        a run is request-bound when no request limit exists would be false, and
        the UI needs the distinction to phrase its summary.
        """
        if self.unlimited:
            return "unlimited"
        rows = int(rows or self.suggested_batch_size)
        if self.tpm <= 0:
            return "requests"
        by_tokens = self.tpm / self.cost_of(rows, tok_per_row, tokens_per_request)
        if abs(by_tokens - self.rpm) < 0.5:
            return "both"
        return "tokens" if by_tokens < self.rpm else "requests"

    # ── Construction ──────────────────────────────────────────────────────────
    @classmethod
    def from_name(cls, name: str) -> "RateLimitPreset":
        key = (name or "").strip().lower()
        if key not in _PRESET_TABLE:
            raise ValueError(f"unknown preset '{name}'; use custom() for user values")
        rpm, tpm, batch = _PRESET_TABLE[key]
        preset = cls(
            name=key,  # type: ignore[arg-type]
            rpm=rpm,
            tpm=tpm,
            suggested_batch_size=batch,
        )
        # The tier states its batch; the worker count follows from both limits at
        # that batch, so a named preset and a custom one are derived identically.
        preset.suggested_parallel = preset.workers_for(batch)
        return preset

    @classmethod
    def custom(cls, rpm: int, tpm: int) -> "RateLimitPreset":
        """
        A user-entered limit. `0` is kept as 0 — it is the "no limit" value
        (FR-603), and the old `max(1, ...)` turned it into the slowest possible
        run instead. Negatives normalise to 0 rather than being refused: the
        screens show what was stored, so both spellings of "none" have to land
        on the same number.
        """
        preset = cls(name="custom", rpm=max(0, int(rpm)), tpm=max(0, int(tpm)))
        preset.derive()
        return preset

    @classmethod
    def default(cls) -> "RateLimitPreset":
        return cls.from_name(DEFAULT_PRESET_NAME)

    def derive(self) -> "RateLimitPreset":
        """
        Recompute the suggested batch and worker count from rpm/tpm.

        Batch first, from token headroom per request: a limit that only affords
        3k tokens a request cannot carry 50 rows, whatever the RPM says. Then the
        worker count *at that batch*, because the two are not independent —
        doubling the batch halves the requests a TPM allowance affords, so a
        worker count derived before the batch was known would be wrong.

        With no limit there is no headroom to divide, so the batch falls back to
        `config.DEFAULT_BATCH_SIZE` clamped to the usual range and the worker
        count to `PARALLEL_MAX` (FR-604).

        v3.6's `floor(rpm / 10)` is gone. It ignored TPM entirely, collapsed to 0
        below 10 RPM, and gave the same answer for a 5-row request as for a
        50-row one.
        """
        if self.unlimited:
            self.suggested_batch_size = _clamp(
                int(config.DEFAULT_BATCH_SIZE), BATCH_MIN, BATCH_MAX
            )
            self.suggested_parallel = PARALLEL_MAX
            return self
        tokens_per_request = self.tpm / self.rpm if self.rpm > 0 else self.tpm
        rows_by_tokens = int(
            (tokens_per_request - REQUEST_OVERHEAD_TOKENS)
            / (config.DEFAULT_TOK_PER_ROW * TOKENS_PER_ROW_ROUND_TRIP)
        ) if tokens_per_request > REQUEST_OVERHEAD_TOKENS else BATCH_MIN
        self.suggested_batch_size = _clamp(rows_by_tokens, BATCH_MIN, BATCH_MAX)
        self.suggested_parallel = self.workers_for(self.suggested_batch_size)
        return self

    # ── Serialization ─────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {"name": self.name, "rpm": self.rpm, "tpm": self.tpm}

    @classmethod
    def from_dict(cls, data: dict | None) -> "RateLimitPreset":
        if not data:
            return cls.default()
        name = str(data.get("name", DEFAULT_PRESET_NAME)).lower()
        if name in _PRESET_TABLE:
            preset = cls.from_name(name)
            # Honour stored overrides if the user edited a named preset's numbers
            # — and re-derive, or the worker count would still describe the tier's
            # published limits rather than the ones actually in force. An override
            # of 0 survives as 0 (FR-603/AC-614): the tier keeps its name, and the
            # limit it is actually run under is none.
            rpm = _as_limit(data.get("rpm"), preset.rpm)
            tpm = _as_limit(data.get("tpm"), preset.tpm)
            if (rpm, tpm) != (preset.rpm, preset.tpm):
                preset.rpm, preset.tpm = rpm, tpm
                if preset.unlimited:
                    preset.suggested_parallel = PARALLEL_MAX
                else:
                    preset.suggested_parallel = preset.workers_for(
                        preset.suggested_batch_size
                    )
            return preset
        return cls.custom(
            _as_limit(data.get("rpm"), 15), _as_limit(data.get("tpm"), 40_000)
        )

    def label(self) -> str:
        return PRESET_LABELS.get(self.name, self.name)

    def describe_limits(
        self,
        rows: int | None = None,
        tok_per_row: int | None = None,
        tokens_per_request: int | None = None,
    ) -> str:
        """
        One line explaining the derivation, for the screens that show it.

        States the binding limit rather than only the result: someone who can see
        they are token-bound knows to shrink the batch, whereas "3 workers" alone
        invites the assumption that more would be faster.

        Unlimited gets its own sentence. Running the normal arithmetic would
        print `0 rpm · 0 tpm → 0.0 req/min`, which reads as a stalled run rather
        than an unrestricted one (FR-605 item 5 / AC-617).
        """
        rows = int(rows or self.suggested_batch_size)
        workers = self.workers_for(rows, tok_per_row, tokens_per_request=tokens_per_request)
        cost = self.cost_of(rows, tok_per_row, tokens_per_request)
        if self.unlimited:
            return (
                f"no rate limit → {workers} worker"
                + ("s" if workers != 1 else "")
                + f" at {rows} rows ({cost:,} tok each), "
                f"capped by the parallel limit rather than rpm/tpm"
            )
        budget = self.requests_per_minute_budget(rows, tok_per_row, tokens_per_request)
        bound = self.limited_by(rows, tok_per_row, tokens_per_request)
        return (
            f"{self.rpm} rpm · {self.tpm:,} tpm → "
            f"~{budget:.1f} req/min at {rows} rows "
            f"({cost:,} tok each), "
            f"{bound}-bound → {workers} worker" + ("s" if workers != 1 else "")
        )


def all_presets() -> list[RateLimitPreset]:
    """Named presets in ascending aggressiveness (custom excluded)."""
    return [RateLimitPreset.from_name(name) for name in _PRESET_TABLE]


def _as_limit(value, fallback: int) -> int:
    """
    A stored rpm/tpm figure, normalised.

    `0` is a legitimate value and passes straight through; a negative becomes 0
    for the same reason `custom()` normalises one; anything unreadable falls back
    rather than raising, because a hand-edited vault must still load.
    """
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return int(fallback)
