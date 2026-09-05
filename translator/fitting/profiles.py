"""
fitting/profiles.py — Engine profiles: how wide a dialog window is per engine.

All width math is in *em* units (1 em = the engine's default font size), so a
profile is font-file-free by design (spec 1.1): RPG Maker MV/MZ renders message
text in a window about 32 em wide with 4 visible lines, and the numbers for the
other engines follow the same convention. `char_widths` buckets characters by
class instead of per-codepoint tables: CJK/fullwidth glyphs are 1.0 em because
that is how monospaced-in-practice JP glyphs render, narrow latin letters
(i l j t f) ≈ 0.25, default latin ≈ 0.5, wide latin (m w) ≈ 0.85 — these are
deliberate starting estimates; Task 12 calibrates them against a real MV game
before any of this is called "verified" beyond kirikiri/mv-mz.

`ratio_threshold` differs per engine because Japanese→Indonesian normally
expands 130–160% in *width* (a 4-kanji word becomes a many-letter word at half
the per-glyph width); a flat 130% flag would drown the report in false
positives (spec 1.4b). English→Indonesian expansion is milder, so the kirikiri
profile (EN-source projects in practice) starts at 1.4.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Shared width buckets. Units: em fraction of the default font size.
_WIDTHS = {
    "latin_narrow":    0.25,   # i l j t f I
    "latin":           0.50,   # most letters
    "latin_wide":      0.85,   # m w
    "latin_upper_wide": 1.00,  # W M
    "space":           0.30,
    "digit":           0.50,
    "cjk":             1.00,   # kanji/kana/fullwidth — also most JP punctuation
    "punct":           0.35,   # . , ! ? ; : ' " ( )
}

# RPG Maker control codes (MV/MZ + VX Ace + 2000 share most of the set).
_RPGM_TOKENS = (
    r"\\V\[\d+\]", r"\\N\[\d+\]", r"\\C\[\d+\]", r"\\FS\[\d+\]",
    r"\\FI\[\d+\]", r"\\FM\[\d+\]", r"\\FB", r"\\FR", r"\\I\[\d+\]",
    r"\\\.", r"\\\|", r"\\!", r"\\>", r"\\<", r"\\\^", r"\\\{", r"\\\}",
)

@dataclass(frozen=True)
class EngineProfile:
    name: str
    display: str
    verified: bool
    token_patterns: tuple[str, ...]
    char_widths: dict[str, float] = field(default_factory=lambda: dict(_WIDTHS))
    window_cols: float = 32.0
    max_lines: int = 4
    safe_margin: float = 0.90
    ratio_threshold: float = 1.8

_PROFILES: dict[str, EngineProfile] = {
    "rpgm-mv-mz": EngineProfile(
        name="rpgm-mv-mz", display="RPG Maker MV/MZ", verified=True,
        token_patterns=_RPGM_TOKENS, window_cols=32.0, ratio_threshold=1.8,
    ),
    "rpgm-vxace-xp": EngineProfile(
        name="rpgm-vxace-xp", display="RPG Maker VX Ace/XP", verified=False,
        token_patterns=_RPGM_TOKENS, window_cols=30.0, ratio_threshold=1.8,
    ),
    "rpgm-2000": EngineProfile(
        name="rpgm-2000", display="RPG Maker 2000/2003", verified=False,
        token_patterns=_RPGM_TOKENS, window_cols=26.0, max_lines=4,
        ratio_threshold=1.8,
    ),
    "kirikiri": EngineProfile(
        name="kirikiri", display="Kirikiri/KAG", verified=True,
        # Kirikiri tags seen in real dumps: [w] [p] [r] and \-class escapes.
        token_patterns=_RPGM_TOKENS[:3] + (r"\[w\]", r"\[p\]", r"\[r\]", r"\\@",),
        window_cols=30.0, ratio_threshold=1.4,
    ),
    "alicesoft": EngineProfile(
        name="alicesoft", display="AliceSoft", verified=False,
        token_patterns=(r"\\@", r"\\!", r"\\n", r"\\d", r"\\w"),
        window_cols=28.0, ratio_threshold=1.4,
    ),
    "wolfrpg": EngineProfile(
        name="wolfrpg", display="Wolf RPG", verified=False,
        token_patterns=(r"\\n", r"\[color\]", r"\[key\]"),
        window_cols=28.0, ratio_threshold=1.4,
    ),
}

ENGINE_ORDER: tuple[str, ...] = (
    "general", "rpgm-mv-mz", "rpgm-vxace-xp", "rpgm-2000",
    "kirikiri", "alicesoft", "wolfrpg",
)

ENGINE_LABELS: dict[str, str] = {
    "general":       "General — no fit measurement",
    "rpgm-mv-mz":    "RPG Maker MV/MZ",
    "rpgm-vxace-xp": "RPG Maker VX Ace/XP",
    "rpgm-2000":     "RPG Maker 2000/2003",
    "kirikiri":      "Kirikiri/KAG",
    "alicesoft":     "AliceSoft",
    "wolfrpg":       "Wolf RPG",
}

def get_profile(name: str) -> EngineProfile | None:
    """None for "general" and unknowns: callers treat it as fitting-off."""
    return _PROFILES.get(name)
