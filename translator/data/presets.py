"""
data/presets.py — Translation style presets.

Two kinds, and the difference is the point:

  PRESETS      the built-in styles. They are code, so they are always there
               and cannot be deleted — there is no state in which the app has
               no styles to offer.
  user presets a prompt you wrote and named, saved as plain JSON at
               config.STYLE_PRESETS_FILE. Yours to add and delete.

The built-in list was ported as-is from v3.6's config.py; it lives here
rather than there so config.py holds true constants only
(01_ARCHITECTURE.md §4).
"""

from __future__ import annotations

import json
import os
import re
import tempfile

from .. import config

SCHEMA_VERSION = 1

PRESETS: list[dict] = [
    {
        "key": "formal",
        "label": "Formal / Resmi",
        "prompt": (
            "Translate the text into a formal and professional tone. "
            "Use correct grammar, structured sentences, and appropriate honorifics when relevant. "
            "Avoid informal expressions and contractions. "
            "Ensure the translation remains faithful to the original meaning."
        ),
    },
    {
        "key": "casual",
        "label": "Natural / Casual",
        "prompt": (
            "Translate the text into a natural, conversational tone used in everyday communication. "
            "Use clear and relaxed language that sounds natural to native speakers. "
            "Do not censor, soften, or sanitize strong language or explicit wording if it appears in the original text. "
            "Preserve the original tone and emotional intensity."
        ),
    },
    {
        "key": "game_formal",
        "label": "Game Translator — Formal",
        "prompt": (
            "Translate the text for a video game using a formal tone suitable for UI, menus, "
            "system messages, and official in-game descriptions. "
            "Keep wording concise and clear. "
            "Preserve game terminology, item names, and proper nouns."
        ),
    },
    {
        "key": "game_casual",
        "label": "Game Translator — Casual",
        "prompt": (
            "Translate the text for a video game using a natural and engaging tone suitable for "
            "character dialogue and interactive storytelling. "
            "Use language that feels natural when spoken. "
            "Do not censor, soften, or sanitize profanity, insults, or explicit expressions if they exist in the source text. "
            "Preserve character names, item names, and game terminology."
            "Preserve the original tone and emotional intensity."
        ),
    },
    {
        "key": "technical",
        "label": "Technical / IT",
        "prompt": (
            "Translate technical or IT-related content with high accuracy. "
            "Preserve technical terms, acronyms, code snippets, commands, file paths, and product names. "
            "Use industry-standard terminology and avoid altering technical meaning."
        ),
    },
    {
        "key": "subtitle",
        "label": "Subtitle / Dialogue",
        "prompt": (
            "Translate the text as subtitles or spoken dialogue. "
            "Keep sentences concise, natural, and easy to read when spoken. "
            "Maintain the emotional tone and intent of the original dialogue."
        ),
    },
    {
        "key": "ecommerce",
        "label": "E-Commerce / Product",
        "prompt": (
            "Translate product descriptions for an e-commerce context. "
            "Make the text clear, persuasive, and appealing to customers. "
            "Preserve product names, model numbers, specifications, and numerical details."
        ),
    },
]


# ── Saved presets ─────────────────────────────────────────────────────────────

def load_user_presets() -> list[dict]:
    """
    The user's saved styles, oldest first.

    A missing, unreadable or malformed file yields none rather than an error:
    this is a convenience store, and nothing about it is worth blocking a
    translation over. Entries missing any field are dropped individually, so
    one bad row cannot hide the rest.
    """
    try:
        with open(config.STYLE_PRESETS_FILE, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return []
    entries = payload.get("presets") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return []
    return [
        {"key": str(e["key"]), "label": str(e["label"]), "prompt": str(e["prompt"])}
        for e in entries
        if isinstance(e, dict) and e.get("key") and e.get("label") and e.get("prompt")
    ]


def save_user_preset(label: str, prompt: str) -> dict:
    """Store a new preset and return it. Raises OSError if it cannot be written."""
    presets = load_user_presets()
    preset = {
        "key": _unique_key(label, presets),
        "label": label.strip(),
        "prompt": prompt.strip(),
    }
    presets.append(preset)
    _write(presets)
    return preset


def update_user_preset(key: str, prompt: str) -> bool:
    """Update an existing saved preset's prompt. False if not found or is builtin."""
    if is_builtin(key):
        return False
    presets = load_user_presets()
    found = False
    for p in presets:
        if p["key"] == key:
            p["prompt"] = prompt.strip()
            found = True
            break
    if not found:
        return False
    _write(presets)
    return True


def delete_user_preset(key: str) -> bool:
    """False if there was no such saved preset — built-ins never match."""
    presets = load_user_presets()
    remaining = [p for p in presets if p["key"] != key]
    if len(remaining) == len(presets):
        return False
    _write(remaining)
    return True


def is_builtin(key: str) -> bool:
    return any(p["key"] == key for p in PRESETS)


def find_preset(key: str) -> dict | None:
    """Look up a style by key, built-in or saved."""
    return next((p for p in PRESETS + load_user_presets() if p["key"] == key), None)


def _unique_key(label: str, existing: list[dict]) -> str:
    """A readable key, so the JSON stays worth opening by hand."""
    base = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_") or "custom"
    taken = {p["key"] for p in existing} | {p["key"] for p in PRESETS}
    key, suffix = base, 2
    while key in taken:
        key, suffix = f"{base}_{suffix}", suffix + 1
    return key


def _write(presets: list[dict]) -> None:
    """Replace the file in one step, so a crash mid-write cannot truncate it."""
    directory = os.path.dirname(config.STYLE_PRESETS_FILE)
    os.makedirs(directory, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory, delete=False, suffix=".tmp"
    )
    try:
        with handle:
            json.dump({"version": SCHEMA_VERSION, "presets": presets},
                      handle, ensure_ascii=False, indent=2)
        os.replace(handle.name, config.STYLE_PRESETS_FILE)
    except Exception:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise
