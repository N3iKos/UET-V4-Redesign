"""
core/models.py — Job/processing dataclasses. No logic beyond trivial helpers,
no imports from sibling packages (providers/, scheduling/, tui/).

Provider-topology dataclasses live in providers/models.py and are kept
separate on purpose (01_ARCHITECTURE.md §3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .. import config

# ── Enums / literals ──────────────────────────────────────────────────────────

OpMode = Literal[
    "translate",
    "translate_context",
    "repair",
    "repair_compare",
    "repair_compare_context",
    "retranslate_failed",
]

# "adaptive" from v3.6 is deliberately dropped (03_PROVIDER_SYSTEM.md §6).
# "wave" is the third mode (FEATURE-003): every worker starts a batch at the
# same time and the next wave waits for all of them, so cooldowns apply once
# per wave rather than per worker.
ProcessingMode = Literal["sequential", "parallel", "wave"]
WriteMode      = Literal["safe", "buffered"]
RetryMode      = Literal["cooldown", "infinite"]
AntiDesync     = Literal["off", "auto", "strict"]
PromptFormat   = Literal["plain", "json"]
ConfigMode     = Literal["quick", "full"]


# ── Job configuration ─────────────────────────────────────────────────────────

@dataclass
class JobConfig:
    # Provider / model — registry IDs, never hardcoded provider names
    provider_id:     str = ""
    model_id:        str = ""
    connection_id:   str = ""     # "" = let ConnectionPool pick per strategy
    repair_model_id: str = ""     # "" = same as model_id
    # A combo instead of a single model (FEATURE-005). Set together with
    # `provider_id = data.combos.COMBO_PROVIDER_ID`; `model_id` then carries the
    # combo's *name*, for the screens, and the models actually asked for come
    # from the pool one lease at a time. Kept as an id rather than an inlined
    # copy of the combo so an edit to the combo is not silently half-applied to a
    # job someone is about to start.
    combo_id:        str = ""

    # File paths
    src_path: str = ""
    out_path: str = ""
    full_src_path: str = ""
    full_out_path: str = ""
    map_path: str = ""

    # Column indices (1-based)
    src_col:    int = 0
    dst_col:    int = 0
    repair_col: int = 0
    ctx_col:    int = 0

    # Range
    start_row:  int = 2
    num_rows:   int = 0
    user_row_limit: int = 0
    batch_size: int = config.DEFAULT_BATCH_SIZE

    # Language / style
    src_lang:            str = "auto"
    tgt_lang:            str = ""
    style_prompt:        str = ""
    global_context:      str = ""
    custom_placeholders: str = ""

    # Processing mode
    op_mode:         OpMode         = "translate"
    processing_mode: ProcessingMode = config.DEFAULT_PROCESSING_MODE   # type: ignore[assignment]
    n_workers:       int            = 1

    # Retry
    max_attempts:    int       = config.DEFAULT_MAX_RETRIES
    retry_mode:      RetryMode = "cooldown"
    retry_delay_503: int       = config.RETRY_DELAY_503
    retry_delay_err: int       = config.RETRY_DELAY_ERR

    # Write
    write_mode:  WriteMode = "safe"
    buffer_size: int       = 5

    # Pacing (derived from the model's RateLimitPreset by default)
    batch_delay: float = 0.5
    wave_delay:  float = 1.0

    # Misc
    temperature:           float        = config.DEFAULT_TEMPERATURE
    # Effort level for a model whose reasoning is control="effort".
    reasoning_effort:      str          = ""
    # The user's per-run reasoning choice for a control="toggle" model.
    # None = no opinion, follow the model's own reasoning_default (FR-403).
    reasoning_enabled:     bool | None  = None
    prompt_format:         PromptFormat = "plain"   # "json" = explicit per-model opt-in
    max_completion_tokens: int          = 0         # 0 = let provider decide
    tok_per_row:           int          = config.DEFAULT_TOK_PER_ROW
    skip_filled:           bool         = True
    with_repair:           bool         = False
    anti_desync:           AntiDesync   = "auto"
    debug_log:             bool         = False
    raw_log:               bool         = False
    log_path:              str          = ""

    # New in v4
    mode: ConfigMode = "full"

    def effective_repair_model(self) -> str:
        return self.repair_model_id or self.model_id

    @property
    def uses_combo(self) -> bool:
        """
        True when this job runs a combo rather than one model.

        Asked by whoever builds the pool, so the two places that construct one
        (`ProcessingEngine`, `JudgeRunner`) make the same decision from the same
        field rather than each sniffing `provider_id`.
        """
        return bool(self.combo_id)


# ── Per-batch result ──────────────────────────────────────────────────────────

@dataclass
class BatchResult:
    results:     dict[int, str] = field(default_factory=dict)
    failed_rows: list[int]      = field(default_factory=list)


# ── Aggregate stats ───────────────────────────────────────────────────────────

@dataclass
class ProcessStats:
    total:           int   = 0
    ok:              int   = 0
    repaired:        int   = 0
    skipped:         int   = 0
    failed:          int   = 0
    elapsed:         float = 0.0
    tokens_consumed: int   = 0

    def update(self, result: BatchResult, is_repair: bool = False) -> None:
        for text in result.results.values():
            if text and text != config.FAIL_MARKER:
                self.ok += 1
                if is_repair:
                    self.repaired += 1
            else:
                self.failed += 1
        self.failed += len(result.failed_rows)


# ── Provider error ────────────────────────────────────────────────────────────

class ProviderError(Exception):
    """
    Carries the provider's *real* error text. v4 talks to arbitrary
    user-defined endpoints, so response_text must never be discarded in
    favour of a generic message (03_PROVIDER_SYSTEM.md §7).

    `kind` is UET's classification of the failure, decided from the real
    response rather than assumed. It drives how the scheduler treats the
    connection (cooldown vs park vs retire), and never guesses quota
    exhaustion from an anonymous 429:

      rate_limit  temporary 429 / window limit → cooldown, connection recovers
      quota       confirmed daily/quota/billing wall → connection retired
      server      5xx / gateway → short penalty, retry or fallback
      auth        401 / 403 without a quota signal → not a rate limit
      invalid     400 / 404 / 422 — bad request or model → not provider quota
      unknown     anything else → neutral, kept for diagnostics, no assumption
    """

    KIND_RATE_LIMIT = "rate_limit"
    KIND_QUOTA = "quota"
    KIND_SERVER = "server"
    KIND_AUTH = "auth"
    KIND_INVALID = "invalid"
    KIND_UNKNOWN = "unknown"

    def __init__(
        self,
        msg:           str,
        status_code:   int  = 0,
        is_rate_limit: bool = False,
        is_tpd:        bool = False,
        retry_delay:   int  = config.RETRY_DELAY_ERR,
        response_text: str  = "",
        headers:       dict | None = None,
        kind:          str  = "unknown",
        is_timeout:    bool = False,
    ):
        super().__init__(msg)
        self.status_code   = status_code
        self.is_rate_limit = is_rate_limit
        self.is_tpd        = is_tpd
        self.retry_delay   = retry_delay
        self.response_text = response_text
        self.headers: dict = headers or {}
        self.kind          = kind
        # True only when the request exceeded the user's request_timeout: the
        # endpoint never answered at all, which is a different signal from a
        # 504 the server actually sent. The pool uses it to rotate away from
        # the slot even under round-robin, where an ordinary short cooldown is
        # absorbed inline — a slot that hangs must not hold its wave.
        self.is_timeout    = is_timeout

    def preview(self, limit: int = 200) -> str:
        """Short form for inline display; full text stays in response_text."""
        raw = (self.response_text or str(self)).strip().replace("\n", " ")
        return raw if len(raw) <= limit else raw[:limit] + "..."
