"""
data/app_prefs.py — Non-secret application preferences.

Everything here is a *default* rather than a job setting: the processing mode a
new job starts from, the ceiling a Judge request is split against, and how many
rows a Fix All request opens with. None of it is a secret, so it sits beside the
vault as plain JSON (config.APP_PREFS_FILE) for the same reason the style presets
do: losing it to a forgotten master password would be a poor trade.

Reading never raises. A missing, unreadable or hand-mangled file simply means
"no preferences yet", and a translation must never be blocked by one. Writing
is atomic (`.tmp` + `os.replace`), so a crash mid-write cannot truncate it.
"""

from __future__ import annotations

import json
import os
import tempfile

from .. import config

SCHEMA_VERSION = 1

# The domain of `processing_mode_default`. Kept here rather than imported from
# core.models so this module stays leaf-level (config only).
PROCESSING_MODES = ("sequential", "parallel", "wave")

# How Judge grades a flagged row. `severity` also asks for critical/moderate/
# minor; `none` is the original flag-only behaviour. Kept here rather than
# imported from judge.models so this module stays leaf-level (config only).
GRADING_MODES = ("severity", "none")

DEFAULTS: dict = {
    "processing_mode_default": config.DEFAULT_PROCESSING_MODE,
    "judge_grading_mode": config.DEFAULT_JUDGE_GRADING_MODE,
    "max_rows_per_request": config.DEFAULT_MAX_ROWS_PER_REQUEST,
    "max_tokens_per_request": config.MAX_TOKENS_PER_REQUEST,
    "fix_batch_rows": config.DEFAULT_FIX_BATCH_ROWS,
    "fallback_save_range": config.DEFAULT_FALLBACK_SAVE_RANGE,
    "preferred_rows_per_request": config.DEFAULT_PREFERRED_ROWS_PER_REQUEST,
    "max_rows_per_request_cap": config.DEFAULT_MAX_ROWS_PER_REQUEST_CAP,
    "request_timeout": config.DEFAULT_REQUEST_TIMEOUT,
    # Judge: treat the rows of a request as a chronological dialogue stream
    # (opt-in). OFF keeps the strict per-line isolation that protects against
    # cross-line false positives.
    "judge_chain_context": False,
    "judge_chain_window": 1,
    # Judge comment style — how the FIX part of a comment may be written:
    #   auto     — the model picks per finding: exact word replacement when the
    #               fix is unambiguous, direction-only critique for style
    #   explicit — always give the exact replacement words (most precise fixes,
    #               most prone to suggestion ping-pong)
    #   critical — critique only, never dictate words (the fix model rewrites
    #               freely; no word can be forced)
    "judge_comment_style": "auto",
    # Translate: ask the model to keep terminology/tone continuity across the
    # rows of one batch (they are consecutive lines of the file).
    "translate_batch_context": False,
    # Priority failover: automatically cascade failed batches to models ranked 1, 2, 3...
    "priority_failover_enabled": True,
    "failover_retry_threshold": 3,
    # Per-model remembered Judge operation settings (re-judge / fix). Keyed by
    # "<provider_id>\n<connection_id>\n<model_id>" so two models with the same
    # name on different connections stay separate. Values:
    # {"remember": bool, "max_rows": int, "workers": int}
    "judge_model_prefs": {},
    # Engine profile chosen per file (FileEngineScreen). Keyed by resolved
    # absolute path so the same dump always recalls its engine; LRU-capped so
    # an old project folder cannot grow the prefs file forever.
    "engine_per_file": {},
}


def load_prefs() -> dict:
    """
    Every preference, defaults filled in. Never raises.

    Unknown keys are dropped and out-of-domain values are replaced by their
    default, so a hand-edited file cannot put the app into a state no screen
    knows how to render.
    """
    try:
        with open(config.APP_PREFS_FILE, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return dict(DEFAULTS)
    if not isinstance(payload, dict):
        return dict(DEFAULTS)

    prefs = dict(DEFAULTS)
    mode = payload.get("processing_mode_default")
    if isinstance(mode, str) and mode in PROCESSING_MODES:
        prefs["processing_mode_default"] = mode
    grading = payload.get("judge_grading_mode")
    if isinstance(grading, str) and grading in GRADING_MODES:
        prefs["judge_grading_mode"] = grading
    prefs["max_rows_per_request"] = _clamp_rows(
        payload.get("max_rows_per_request")
    )
    prefs["max_tokens_per_request"] = _clamp_tokens(
        payload.get("max_tokens_per_request")
    )
    prefs["fix_batch_rows"] = _clamp_fix_rows(payload.get("fix_batch_rows"))
    prefs["fallback_save_range"] = _clamp_fallback_range(
        payload.get("fallback_save_range")
    )
    prefs["preferred_rows_per_request"] = _clamp_preferred_rows(
        payload.get("preferred_rows_per_request")
    )
    prefs["max_rows_per_request_cap"] = _clamp_preferred_rows(
        payload.get("max_rows_per_request_cap"),
        default=config.DEFAULT_MAX_ROWS_PER_REQUEST_CAP,
    )
    prefs["request_timeout"] = _clamp_request_timeout(
        payload.get("request_timeout")
    )
    prefs["judge_model_prefs"] = _clean_judge_model_prefs(
        payload.get("judge_model_prefs")
    )
    prefs["engine_per_file"] = _clean_engine_per_file(
        payload.get("engine_per_file")
    )
    prefs["judge_chain_context"] = bool(payload.get("judge_chain_context", False))
    prefs["judge_chain_window"] = _clamp_chain_window(
        payload.get("judge_chain_window")
    )
    prefs["translate_batch_context"] = bool(
        payload.get("translate_batch_context", False)
    )
    prefs["judge_comment_style"] = _clean_comment_style(
        payload.get("judge_comment_style")
    )
    prefs["priority_failover_enabled"] = bool(
        payload.get("priority_failover_enabled", True)
    )
    prefs["failover_retry_threshold"] = _clamp_failover_retries(
        payload.get("failover_retry_threshold")
    )
    return prefs


def save_prefs(prefs: dict) -> None:
    """
    Merge and persist. Best effort: a preference that cannot be written is not
    worth an error in front of someone who just wanted to start a job.

    A partial dict is fine — anything absent keeps its stored value.
    """
    current = load_prefs()
    current.update({k: v for k, v in (prefs or {}).items() if k in DEFAULTS})
    mode = current.get("processing_mode_default")
    if mode not in PROCESSING_MODES:
        current["processing_mode_default"] = DEFAULTS["processing_mode_default"]
    if current.get("judge_grading_mode") not in GRADING_MODES:
        current["judge_grading_mode"] = DEFAULTS["judge_grading_mode"]
    current["max_rows_per_request"] = _clamp_rows(
        current.get("max_rows_per_request")
    )
    current["max_tokens_per_request"] = _clamp_tokens(
        current.get("max_tokens_per_request")
    )
    current["fix_batch_rows"] = _clamp_fix_rows(current.get("fix_batch_rows"))
    current["fallback_save_range"] = _clamp_fallback_range(
        current.get("fallback_save_range")
    )
    current["preferred_rows_per_request"] = _clamp_preferred_rows(
        current.get("preferred_rows_per_request")
    )
    current["max_rows_per_request_cap"] = _clamp_preferred_rows(
        current.get("max_rows_per_request_cap"),
        default=config.DEFAULT_MAX_ROWS_PER_REQUEST_CAP,
    )
    current["request_timeout"] = _clamp_request_timeout(
        current.get("request_timeout")
    )
    current["judge_model_prefs"] = _clean_judge_model_prefs(
        current.get("judge_model_prefs")
    )
    current["engine_per_file"] = _clean_engine_per_file(
        current.get("engine_per_file")
    )
    current["judge_chain_context"] = bool(current.get("judge_chain_context", False))
    current["judge_chain_window"] = _clamp_chain_window(
        current.get("judge_chain_window")
    )
    current["translate_batch_context"] = bool(
        current.get("translate_batch_context", False)
    )
    current["judge_comment_style"] = _clean_comment_style(
        current.get("judge_comment_style")
    )
    current["priority_failover_enabled"] = bool(
        current.get("priority_failover_enabled", True)
    )
    current["failover_retry_threshold"] = _clamp_failover_retries(
        current.get("failover_retry_threshold")
    )
    try:
        _write(current)
    except OSError:
        pass


# ── Accessors ─────────────────────────────────────────────────────────────────

def default_processing_mode() -> str:
    """The mode a new job starts from (wizard may still override per run)."""
    return load_prefs()["processing_mode_default"]


def judge_grading_mode() -> str:
    """How Judge grades a flagged row: `severity` (default) or `none`."""
    return load_prefs()["judge_grading_mode"]


def max_rows_per_request() -> int:
    """The max rows per Judge request, set by user in Settings."""
    return load_prefs()["max_rows_per_request"]


def max_tokens_per_request() -> int:
    """The Judge auto-split ceiling, clamped to the Settings range."""
    return load_prefs()["max_tokens_per_request"]


def fix_batch_rows() -> int:
    """
    Rows per Fix All request, as the *default* the wizard opens with.

    Not a limit: the wizard still asks per run, and a figure typed there is sent
    as typed (CR-002 FR-613). Changing the default is an explicit Settings act.
    """
    return load_prefs()["fix_batch_rows"]


def fallback_save_range() -> int:
    """
    How many numbered siblings the writer may create when the output file is
    locked (abcd_Translated_1 … _N). 0 disables it — the writer then retries the
    real target forever, the pre-feature behaviour.
    """
    return load_prefs()["fallback_save_range"]


def preferred_rows_per_request() -> int:
    """The rows/request the shared Processing form aims for (Settings)."""
    return load_prefs()["preferred_rows_per_request"]


def max_rows_per_request_cap() -> int:
    """The ceiling the Processing form never auto-suggests above (Settings)."""
    return load_prefs()["max_rows_per_request_cap"]

def request_timeout() -> int:
    """
    Seconds a single generate request may run before it counts as hung.

    Beyond this the client raises a read timeout, the retry loop counts a
    failed attempt, and the next attempt may land on a different connection or
    (for a combo) a different member model — which is the whole point: a model
    that stopped answering must not hold its worker hostage.
    """
    return load_prefs()["request_timeout"]


def set_default_processing_mode(mode: str) -> str:
    """Store the default mode; an unknown value leaves the stored one alone."""
    if mode not in PROCESSING_MODES:
        return default_processing_mode()
    save_prefs({"processing_mode_default": mode})
    return mode


def set_judge_grading_mode(mode: str) -> str:
    """Store the grading mode; an unknown value leaves the stored one alone."""
    if mode not in GRADING_MODES:
        return judge_grading_mode()
    save_prefs({"judge_grading_mode": mode})
    return mode


def set_max_rows_per_request(value: int) -> int:
    """Store max rows per request, clamped. Returns stored value."""
    clamped = _clamp_rows(value)
    save_prefs({"max_rows_per_request": clamped})
    return clamped


def set_max_tokens_per_request(value: int) -> int:
    """Store the token ceiling, clamped. Returns what was actually stored."""
    clamped = _clamp_tokens(value)
    save_prefs({"max_tokens_per_request": clamped})
    return clamped


def set_fix_batch_rows(value: int) -> int:
    """Store the Fix All default, clamped to 1–50. Returns the stored value."""
    clamped = _clamp_fix_rows(value)
    save_prefs({"fix_batch_rows": clamped})
    return clamped


def set_fallback_save_range(value: int) -> int:
    """Store the locked-output fallback range, clamped. Returns stored value."""
    clamped = _clamp_fallback_range(value)
    save_prefs({"fallback_save_range": clamped})
    return clamped


def set_preferred_rows_per_request(value: int) -> int:
    """Store the preferred rows/request, clamped. Returns the stored value."""
    clamped = _clamp_preferred_rows(value)
    save_prefs({"preferred_rows_per_request": clamped})
    return clamped


def set_max_rows_per_request_cap(value: int) -> int:
    """Store the Processing rows/request ceiling, clamped. Returns stored value."""
    clamped = _clamp_preferred_rows(
        value, default=config.DEFAULT_MAX_ROWS_PER_REQUEST_CAP
    )
    save_prefs({"max_rows_per_request_cap": clamped})
    return clamped

def set_request_timeout(value: int) -> int:
    """Store the per-request timeout (seconds), clamped. Returns stored value."""
    clamped = _clamp_request_timeout(value)
    save_prefs({"request_timeout": clamped})
    return clamped


# ── Per-model Judge operation settings ───────────────────────────────────────

def judge_model_prefs_key(slot: tuple[str, str, str] | list) -> str:
    """
    The store key for one model slot.

    The full triple rather than the model id alone: two connections can carry
    a model with the same id, and remembering settings for one of them must
    not silently apply them to the other.
    """
    try:
        provider, connection, model = (list(slot) + ["", "", ""])[:3]
    except TypeError:
        return ""
    return "\n".join(str(part) for part in (provider, connection, model))


def remembered_judge_settings(
    slot: tuple[str, str, str] | list,
) -> dict | None:
    """
    The remembered re-judge/fix settings for one model, or None.

    None (rather than a dict) means "this model has no remembered settings" —
    the caller must ask. A stored entry with `remember: false` counts as no
    entry: it is a draft the user chose not to persist.
    """
    key = judge_model_prefs_key(slot)
    if not key:
        return None
    entry = (load_prefs()["judge_model_prefs"] or {}).get(key)
    if not isinstance(entry, dict) or not entry.get("remember"):
        return None
    return {
        "max_rows": _clamp_rows(entry.get("max_rows")),
        "workers": _clamp_workers(entry.get("workers")),
    }


def remember_judge_settings(
    slot: tuple[str, str, str] | list,
    max_rows: int,
    workers: int,
    remember: bool,
) -> None:
    """
    Store (or clear) the remembered settings for one model.

    `remember=False` removes the entry entirely — the next use of this model
    asks again — rather than leaving a dormant draft behind.
    """
    key = judge_model_prefs_key(slot)
    if not key:
        return
    prefs = load_prefs()
    store = dict(prefs["judge_model_prefs"] or {})
    if remember:
        store[key] = {
            "remember": True,
            "max_rows": _clamp_rows(max_rows),
            "workers": _clamp_workers(workers),
        }
    else:
        store.pop(key, None)
    save_prefs({"judge_model_prefs": store})


def _clean_judge_model_prefs(value) -> dict:
    """Keep only well-formed entries; a hand-mangled file starts empty."""
    if not isinstance(value, dict):
        return {}
    clean: dict = {}
    for key, entry in value.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            continue
        if not entry.get("remember"):
            continue
        clean[key] = {
            "remember": True,
            "max_rows": _clamp_rows(entry.get("max_rows")),
            "workers": _clamp_workers(entry.get("workers")),
        }
    return clean


def _clean_engine_per_file(value) -> dict:
    """
    Engine-per-file memory: keep only real engine names, drop everything else.

    Validated against fitting.profiles.ENGINE_ORDER rather than a local copy
    because the engine list is the fitting module's data — a duplicate here
    would silently accept an engine the FileEngineScreen can never show. The
    import is local (function body) so this module stays importable as a leaf
    even if fitting ever grows heavier imports.
    """
    from ..fitting.profiles import ENGINE_ORDER
    ok = set(ENGINE_ORDER)
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for path, engine in value.items():
        if isinstance(path, str) and isinstance(engine, str) and engine in ok:
            out[path] = engine
    return out


def _clamp_workers(value) -> int:
    """Worker count for a remembered Judge operation, clamped to 1–50."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 1
    return max(1, min(50, number))


def _clamp_chain_window(value) -> int:
    """
    Neighbour rows per side for the Judge dialogue-chain context, 1–5.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 1
    return max(1, min(5, number))


def _clamp_failover_retries(value) -> int:
    """Retries before failover, clamped to 1–5."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 3
    return max(1, min(5, number))


# ── Priority Failover settings ────────────────────────────────────────────────

def priority_failover_enabled() -> bool:
    """Whether failed batches cascade to models with priority ranks (1, 2, 3...)."""
    return bool(load_prefs()["priority_failover_enabled"])


def failover_retry_threshold() -> int:
    """Number of failed retries on a model before cascading to the next priority model."""
    return int(load_prefs()["failover_retry_threshold"])


def set_priority_failover_enabled(enabled: bool) -> bool:
    save_prefs({"priority_failover_enabled": bool(enabled)})
    return bool(enabled)


def set_failover_retry_threshold(value: int) -> int:
    clamped = _clamp_failover_retries(value)
    save_prefs({"failover_retry_threshold": clamped})
    return clamped


# ── Fitting engine memory ────────────────────────────────────────────────

def remembered_engine(path: str) -> str:
    """The engine chosen for this file before, "general" when never chosen."""
    return load_prefs()["engine_per_file"].get(os.path.abspath(path), "general")

def remember_engine(path: str, engine: str) -> None:
    """
    Store the file's engine, most-recently-used last, capped at 50 entries.

    Re-choosing an engine moves the file to the back (pop-then-set), so the
    eviction order is "least recently chosen", and the cap keeps a year of
    project folders from growing the prefs file without bound.
    """
    from ..fitting.profiles import ENGINE_ORDER
    if engine not in ENGINE_ORDER:
        return
    key = os.path.abspath(path)
    store = load_prefs()["engine_per_file"]
    store.pop(key, None)
    store[key] = engine
    while len(store) > 50:
        store.pop(next(iter(store)))
    save_prefs({"engine_per_file": store})


def _clamp_chain_window(value) -> int:
    """
    Neighbour rows per side for the Judge dialogue-chain context, 1–5.

    The floor is 1 because 0 neighbours would be "the feature is on but shows
    no neighbours", which is just the feature off with extra steps; the ceiling
    keeps the injected context from costing more tokens than the rows it
    explains.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 1
    return max(1, min(5, number))


# ── Dialogue-chain / batch context settings ──────────────────────────────────

def judge_chain_context() -> bool:
    """
    Whether Judge treats a request's rows as a chronological dialogue stream.

    OFF (the default) keeps the strict per-line isolation: a batch is not a
    passage, and one line's verdict must not lean on another's. ON swaps the
    prompt for a chain-aware one — surrounding lines may be used as context,
    with a scene-break guard — and re-judge prompts gain injected neighbour
    rows as CONTEXT ONLY.
    """
    return bool(load_prefs()["judge_chain_context"])


def judge_chain_window() -> int:
    """Neighbour rows per side injected as context in a re-judge, 1–5."""
    return int(load_prefs()["judge_chain_window"])


def set_judge_chain_context(enabled: bool) -> bool:
    save_prefs({"judge_chain_context": bool(enabled)})
    return bool(enabled)


def set_judge_chain_window(value: int) -> int:
    clamped = _clamp_chain_window(value)
    save_prefs({"judge_chain_window": clamped})
    return clamped


def translate_batch_context() -> bool:
    """
    Whether a translate batch is told to keep continuity across its rows.

    The rows of one batch are consecutive lines of the file either way; this
    only decides whether the prompt says so.
    """
    return bool(load_prefs()["translate_batch_context"])


def set_translate_batch_context(enabled: bool) -> bool:
    save_prefs({"translate_batch_context": bool(enabled)})
    return bool(enabled)


# ── Judge comment style ───────────────────────────────────────────────────────

COMMENT_STYLES = ("auto", "explicit", "critical")


def _clean_comment_style(value) -> str:
    """`auto` unless a known style is stored — a mangled file never breaks runs."""
    return value if value in COMMENT_STYLES else "auto"


def judge_comment_style() -> str:
    """
    How the FIX part of a Judge comment may be written.

    `auto` (default) lets the model choose per finding: an exact word
    replacement when the fix is unambiguous, a direction-only critique for
    matters of style. `explicit` always demands the exact replacement words —
    the most precise repairs, and the most prone to suggestion ping-pong.
    `critical` forbids dictating words entirely: critique only, and the fix
    model rewrites freely.
    """
    return _clean_comment_style(load_prefs()["judge_comment_style"])


def set_judge_comment_style(style: str) -> str:
    stored = _clean_comment_style(style)
    save_prefs({"judge_comment_style": stored})
    return stored


# ── Internals ─────────────────────────────────────────────────────────────────

def _clamp_preferred_rows(value, default: int | None = None) -> int:
    """Rows/request for the shared Processing form, clamped to the config range."""
    fallback = int(
        default if default is not None
        else DEFAULTS["preferred_rows_per_request"]
    )
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(config.MIN_PREFERRED_ROWS, min(config.MAX_PREFERRED_ROWS, number))


def _clamp_rows(value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return int(DEFAULTS["max_rows_per_request"])
    return max(config.MIN_MAX_ROWS, min(config.MAX_MAX_ROWS, number))


def _clamp_tokens(value) -> int:
    """Anything unusable falls back to the default rather than raising."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return int(DEFAULTS["max_tokens_per_request"])
    return max(config.MIN_MAX_TOKENS, min(config.MAX_MAX_TOKENS, number))


def _clamp_fix_rows(value) -> int:
    """
    Rows per Fix All request, clamped to 1–50 (CR-002 FR-612).

    The ceiling is the estimator's `MAX_ROWS_PER_SECTION`; the floor is 1 rather
    than 5, because repairing one row per request is a reasonable answer for a
    model that keeps merging them.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return int(DEFAULTS["fix_batch_rows"])
    return max(config.MIN_FIX_BATCH_ROWS, min(config.MAX_FIX_BATCH_ROWS, number))


def _clamp_fallback_range(value) -> int:
    """
    Numbered-sibling count for a locked output, clamped to 0–99.

    0 is a legitimate answer — it turns the feature off and restores the
    retry-forever behaviour — so an unusable value falls back to the *default*
    rather than to 0, which would silently disable a safety net the user never
    meant to disable.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return int(DEFAULTS["fallback_save_range"])
    return max(config.MIN_FALLBACK_SAVE_RANGE,
               min(config.MAX_FALLBACK_SAVE_RANGE, number))


def _clamp_request_timeout(value) -> int:
    """
    Per-request timeout in seconds, clamped to the Settings range.

    The floor is 30 rather than 1 because a legitimate slow reply — a reasoning
    model on a big batch — takes tens of seconds, and a timeout below that
    would kill healthy requests and turn the retry loop into a churn machine.
    The ceiling (600s) exists so a typo cannot make a hung request wait forever.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return int(DEFAULTS["request_timeout"])
    return max(config.MIN_REQUEST_TIMEOUT,
               min(config.MAX_REQUEST_TIMEOUT, number))


def _write(prefs: dict) -> None:
    """Replace the file in one step, so a crash mid-write cannot truncate it."""
    directory = os.path.dirname(config.APP_PREFS_FILE)
    if directory:
        os.makedirs(directory, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory or None, delete=False, suffix=".tmp"
    )
    try:
        with handle:
            json.dump({"version": SCHEMA_VERSION, **prefs},
                      handle, ensure_ascii=False, indent=2)
        os.replace(handle.name, config.APP_PREFS_FILE)
    except Exception:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise
