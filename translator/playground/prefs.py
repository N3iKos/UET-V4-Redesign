"""
playground/prefs.py — What the playground should offer by default next time.

Only the judge model really needs this: having picked one, you should be able
to judge the next comparison with a single Enter instead of walking the
provider/connection/model columns again. Pick a different one and that
becomes the new default.

Stored as plain JSON beside the vault, for the same reason the style presets
are (config.py): none of it is secret, and none of it is worth losing to a
forgotten master password.
"""

from __future__ import annotations

import json
import os
import tempfile

from .. import config

SCHEMA_VERSION = 1

# A model is addressed by three ids, because a model id alone is not unique —
# the same id can sit on several connections and several providers.
_SLOT_KEYS = ("provider_id", "connection_id", "model_id")

DEFAULTS: dict = {
    "judge": None,          # {"provider_id": ..., "connection_id": ..., "model_id": ...}
    "compare": [],          # the slots used last, same shape, in order
    "src_lang": "auto",
    "tgt_lang": "",
    "style_key": "",
}


def load_prefs() -> dict:
    """
    Never raises. A missing, unreadable or malformed file just means "no
    preferences yet" — this is a convenience, and nothing about it is worth
    blocking on. Unknown keys are dropped and missing ones defaulted, so an
    older or hand-edited file still works.
    """
    try:
        with open(config.PLAYGROUND_PREFS_FILE, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return dict(DEFAULTS)
    if not isinstance(payload, dict):
        return dict(DEFAULTS)

    prefs = dict(DEFAULTS)
    prefs["judge"] = _clean_slot(payload.get("judge"))
    compare = payload.get("compare")
    if isinstance(compare, list):
        prefs["compare"] = [s for s in (_clean_slot(e) for e in compare) if s]
    for key in ("src_lang", "tgt_lang", "style_key"):
        value = payload.get(key)
        if isinstance(value, str):
            prefs[key] = value
    return prefs


def save_prefs(prefs: dict) -> None:
    """Best effort: a preference that cannot be written is not worth an error
    in front of someone who just wanted to run a comparison."""
    try:
        _write({**DEFAULTS, **{k: v for k, v in prefs.items() if k in DEFAULTS}})
    except OSError:
        pass


def remember_judge(slot: dict | None) -> None:
    prefs = load_prefs()
    prefs["judge"] = _clean_slot(slot)
    save_prefs(prefs)


def remember_compare(slots: list[dict]) -> None:
    prefs = load_prefs()
    prefs["compare"] = [s for s in (_clean_slot(e) for e in slots) if s]
    save_prefs(prefs)


def remember_job(src_lang: str, tgt_lang: str, style_key: str) -> None:
    prefs = load_prefs()
    prefs.update(src_lang=src_lang, tgt_lang=tgt_lang, style_key=style_key)
    save_prefs(prefs)


def _clean_slot(slot) -> dict | None:
    """A slot is only usable if it names all three ids."""
    if not isinstance(slot, dict):
        return None
    cleaned = {key: str(slot.get(key) or "") for key in _SLOT_KEYS}
    if not cleaned["provider_id"] or not cleaned["model_id"]:
        return None
    return cleaned


def _write(prefs: dict) -> None:
    """Replace the file in one step, so a crash mid-write cannot truncate it."""
    directory = os.path.dirname(config.PLAYGROUND_PREFS_FILE)
    os.makedirs(directory, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory, delete=False, suffix=".tmp"
    )
    try:
        with handle:
            json.dump({"version": SCHEMA_VERSION, **prefs},
                      handle, ensure_ascii=False, indent=2)
        os.replace(handle.name, config.PLAYGROUND_PREFS_FILE)
    except Exception:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise
