"""
config.py — TRUE constants only.

Unlike v3.6's config.py, this file contains NO provider data, NO model
tables, NO rate-limit tables and NO reasoning-support maps. All of that is
user-defined data managed by providers/registry.py and persisted in the
vault. Translation style presets live in translator/data/presets.py.
"""

from __future__ import annotations

import os

# ── Paths ─────────────────────────────────────────────────────────────────────
# Deliberately distinct from v3.6's ~/.config/excel-translator so v3.6's
# keys.enc / cooldowns.json are never read, touched or overwritten.
CONFIG_DIR  = os.path.join(os.path.expanduser("~"), ".config", "excel-translator-v4")
VAULT_FILE  = os.path.join(CONFIG_DIR, "vault.enc")
# Saved translation styles live beside the vault, not inside it: a style
# prompt is not a secret, and losing your prompts to a forgotten master
# password — or to the vault reset that follows — would be a poor trade.
STYLE_PRESETS_FILE = os.path.join(CONFIG_DIR, "style_presets.json")
# Same reasoning for what the playground remembers between visits.
PLAYGROUND_PREFS_FILE = os.path.join(CONFIG_DIR, "playground.json")
# Non-secret application preferences (default processing mode, token cap).
# Beside the vault for the same reason as the two above: none of it is secret,
# and none of it is worth losing to a forgotten master password.
APP_PREFS_FILE = os.path.join(CONFIG_DIR, "app_prefs.json")
# Combos: named groups of models, run fallback or round-robin (FEATURE-005).
# Beside the vault rather than inside it because a combo stores only *references*
# to provider/connection/model ids that already live in the vault, plus its own
# rate limit — no credential of any kind, so nothing here is worth a password.
COMBOS_FILE = os.path.join(CONFIG_DIR, "combos.json")
VAULT_MAGIC = b"UETV4\x00\x00\x00"
VAULT_SCHEMA_VERSION = 1
VAULT_MAX_ATTEMPTS = 5        # wrong unlocks before the reset option appears

# ── Output ────────────────────────────────────────────────────────────────────
# Results go to <name>_Translated.<ext>; the source file is never written to.
TRANSLATED_SUFFIX = "_Translated"

# ── Processing defaults ───────────────────────────────────────────────────────
FAIL_MARKER         = "[TRANSLATE_FAILED]"
DEFAULT_BATCH_SIZE  = 25      # v4 default (v3.6 was 10) per 02_DATA_MODELS §3
DEFAULT_TEMPERATURE = 0.1
DEFAULT_MAX_RETRIES = 3
DEFAULT_TOK_PER_ROW = 150

# Default execution strategy. Kept as "parallel" so an existing user's runs
# behave exactly as before; Settings persists an override in APP_PREFS_FILE
# (FEATURE-003 FR-304 / ADR-002).
DEFAULT_PROCESSING_MODE = "parallel"

# ── Judge Request Sizing ───────────────────────────────────────────────────────
# Default rows per request for Judge auto-split (Settings: max_rows_per_request).
DEFAULT_MAX_ROWS_PER_REQUEST = 25
MIN_MAX_ROWS = 5
MAX_MAX_ROWS = 100

# ── Shared Processing sizing (Translate & Judge) ──────────────────────────────
# The rows/request the shared Processing form *aims* for, and the ceiling it will
# never auto-suggest above. Preferred is a target, not a floor: when the rate
# limit only affords fewer rows safely, the form recommends the lower figure and
# the user just presses Enter to accept it (UET_UX Processing rules).
DEFAULT_PREFERRED_ROWS_PER_REQUEST = 15
DEFAULT_MAX_ROWS_PER_REQUEST_CAP = 100
MIN_PREFERRED_ROWS = 1
MAX_PREFERRED_ROWS = 100

# How Judge grades a flagged row (Settings: judge_grading_mode).
#   "severity" — also asks for critical/moderate/minor per row (default)
#   "none"     — flag only, the original behaviour
# Severity is the default because an unranked list of flagged rows leaves the
# user with the same triage problem the evaluation was meant to solve.
DEFAULT_JUDGE_GRADING_MODE = "severity"

# ── Fix All request sizing ────────────────────────────────────────────────────
# Rows per repair request (Settings: fix_batch_rows, wizard may override per run).
# Was 5, chosen for reliability rather than from measurement (ADR-008); 20 is the
# default now because 5 rows a request means twenty times the requests — each
# re-sending the same instruction block — for the same work (CR-002 FR-611).
# The upper bound matches the estimator's MAX_ROWS_PER_SECTION; the lower bound is
# 1 because one row a request is a legitimate choice for a model that merges them.
DEFAULT_FIX_BATCH_ROWS = 20
MIN_FIX_BATCH_ROWS = 1
MAX_FIX_BATCH_ROWS = 50

# Safety token ceiling per request to prevent HTTP 400 Context Exceeded.
MAX_TOKENS_PER_REQUEST  = 10_000
TOKENS_PER_CHAR_DIVISOR = 4        # ≈ characters per token
TOKEN_EST_MIN_PER_ROW   = 150      # floor per row, mirrors DEFAULT_TOK_PER_ROW
TOKEN_EST_SAFETY_MARGIN = 0.25     # 25% headroom on every estimate
TOKEN_WARN_THRESHOLD    = 50_000   # "this is a lot of tokens" confirmation
MIN_MAX_TOKENS          = 1_000    # Minimum safety limit
MAX_MAX_TOKENS          = 100_000

# ── Retry delays (seconds) ────────────────────────────────────────────────────
RETRY_DELAY_503         = 45
RETRY_DELAY_ERR         = 5
RETRY_DELAY_503_PENALTY = 15
RETRY_DELAY_FALLBACK_429 = 30   # used when a 429 carries no usable Retry-After
MAX_INLINE_COOLDOWN      = 90   # cooldown longer than this = give up on the connection
CONNECTION_ERROR_COOLDOWN = 60  # how long a connection is parked after a plain error

# After this many *consecutive* whole-batch failures (each already retried
# max_attempts times) with no success in between, the run is paused rather than
# left to plough through the rest of the queue re-emitting the same error. A
# systemic failure — provider down, content blocked, bad key — trips this fast;
# an isolated flaky batch among successes never does, because any success resets
# the counter. Progress already written is kept; no new request is sent until
# the user retries or stops.
MAX_CONSECUTIVE_FAILURES = 3

# ── Parse recovery thresholds ─────────────────────────────────────────────────
PARSE_GOOD_RATIO = 0.8

# ── Checkpointing ─────────────────────────────────────────────────────────────
CHECKPOINT_ROWS = 200          # rows between forced Excel flushes

# ── Locked-output fallback ──────────────────────────────────────────────────
# When the output file is held open by another program, os.replace onto it
# fails. Rather than retry forever (and risk the user losing the whole run to a
# spreadsheet they forgot to close), the writer waits a bounded time on the real
# target and then saves to a numbered sibling — abcd_Translated_1.xlsx, _2, … —
# switching all later checkpoints to that file so nothing fragments.
#
# How long to keep trying the real target before the first fallback. A genuinely
# open workbook survives far longer than any antivirus/sync-client lock, so this
# is deliberately generous: it gives the user a fair chance to close the file and
# keep the tidy original name before a sibling is created.
LOCK_MAX_RETRY_BEFORE_FALLBACK = 30.0

# How many numbered siblings to try (abcd_Translated_1 … _N) before giving up.
# 0 disables the feature entirely and restores the old behaviour: retry the real
# target forever. Persisted per-user in APP_PREFS_FILE as `fallback_save_range`.
DEFAULT_FALLBACK_SAVE_RANGE = 10
MIN_FALLBACK_SAVE_RANGE     = 0
MAX_FALLBACK_SAVE_RANGE     = 99

# ── Network ───────────────────────────────────────────────────────────────────
DEFAULT_HTTP_TIMEOUT = 120.0
PING_TIMEOUT         = 15.0

# How long a single generate request may run before it is treated as hung and
# retried on another attempt/connection (Settings: request_timeout). A request
# that exceeds this raises a read timeout inside the client, which the retry
# loop counts as a failed attempt — so a model that stops streaming never holds
# its worker hostage. User-configurable because "too long" depends entirely on
# the model: a reasoning model on a big batch can legitimately need several
# minutes, while a fast chat model that goes past two minutes is stuck.
DEFAULT_REQUEST_TIMEOUT  = 120     # seconds
MIN_REQUEST_TIMEOUT      = 30
MAX_REQUEST_TIMEOUT      = 600
