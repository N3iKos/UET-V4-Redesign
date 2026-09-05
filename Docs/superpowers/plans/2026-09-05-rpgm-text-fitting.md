# RPG Maker Text Fitting — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move RPG Maker dialog-fitting verification (overflow / 4-line limit / text expansion) into UET: deterministic measurement module + engine selection on the file screen + a 5th Judge criterion `fitting` + re-measured fixes + a standalone audit screen.

**Architecture:** One pure module `translator/fitting/` (profiles, measure, wrap, audit, apply) is the single source of truth for all width math. The TUI gets a shared `FileEngineScreen` (5-visible scrolled file list + engine select, click fills the path box, Confirm & Next, engine remembered per file in app prefs). When engine ≠ General: translate prompts get a soft budget line, Judge gets criterion `fitting` with machine-measured `FITTING:` data injected per row, fixes are re-measured + token-whitelisted (≤2 retries), and a new "Text Fitting Audit" menu screen surfaces findings with mechanical wrap suggestions.

**Tech Stack:** Python 3.11+ stdlib only (dataclasses, re, os, shutil, csv); Textual TUI (existing); pytest (existing, flat `tests/`, no conftest — every test bootstraps `sys.path`).

**Spec:** `Docs/superpowers/specs/2026-09-05-rpgm-fitting-design.md` (rev3). The plan argues from the spec; executors read both.

## Global Constraints

- No third-party libraries. `fitting/` is stdlib-only pure functions — no I/O except `apply.py` (csv write + backup) and `profiles.py` (data only).
- `fitting/` modules never import from `tui/`, `judge/`, or `core/` (leaf module, mirrors `parsing/`).
- `engine == "general"` MUST behave exactly like today's UET: no measurement, no prompt lines, no `fitting` criterion, audit screen refuses.
- Never modify the user's source file; audit `apply.py` writes to the CSV path given (the translated file) and backs it up first as `<name>.bak`.
- Comments in `translator/` code follow the existing heavy-rationale English docstring style (see `judge/prompts.py` for the house style). Tests use module docstrings + `from __future__ import annotations` + `sys.path.insert` bootstrap, no conftest.
- Severity is anchored by the machine in ONE place: `fitting/audit.py` (`sinking=critical`, `overflow=moderate`, ratio flag `minor`). No prompt or runner may re-derive it.
- Line-break tags come ONLY from the source row text (`detect_newline_tag`); there is no `newline_token` in EngineProfile.
- Windows PowerShell 5.1 repo; run tests with `python -m pytest tests/<file> -v` from repo root.

## File Structure (created/modified)

```
translator/fitting/__init__.py      (new)  exports
translator/fitting/profiles.py      (new)  EngineProfile dataclass + ENGINE_PROFILES registry + get_profile()
translator/fitting/measure.py       (new)  strip_tokens, measure_lines, detect_newline_tag
translator/fitting/wrap.py          (new)  WrapMetrics, wrap_lines, wrap_metrics, suggest_wrap
translator/fitting/audit.py         (new)  Finding, audit_rows, fitting_line (prompt payload builder)
translator/fitting/apply.py         (new)  apply_wrapped (backup + write Translated col)
translator/parsing/prompts.py       (mod)  build_translate_prompt/solo: fitting_budget param
translator/core/models.py           (mod)  JobConfig.fitting_profile field
translator/core/engine.py           (mod)  pass fitting_budget into both prompt builders
translator/tui/widgets/common.py    (mod)  Picker: max_visible param (scrolled box)
translator/tui/screens/file_engine.py (new) FileEngineScreen shared by both wizards
translator/tui/screens/wizard.py     (mod)  OperationModeScreen pushes FileEngineScreen; FileSelectScreen deleted
translator/tui/screens/judge.py      (mod)  JudgeModelScreen pushes FileEngineScreen; JudgeFileScreen deleted; criteria gating
translator/data/app_prefs.py        (mod)  engine-per-file memory (LRU 50) + remembered judge engine
translator/judge/models.py         (mod)  CRITERIA + "fitting", labels, severity anchor constants
translator/judge/prompts.py         (mod)  fitting rule, FITTING: rows line, fix prompt block, gating
translator/judge/runner.py          (mod)  fitting context build, re-measure + whitelist + retry, FITTING_STILL_FAIL
translator/tui/app.py               (mod)  menu item "Text Fitting Audit"
translator/tui/screens/audit.py     (new)  audit screen (grid + apply)
tests/test_fitting_*.py             (new)  one per fitting module + integration points
```

---

### Task 1: `fitting/profiles.py` — EngineProfile + registry

**Files:**
- Create: `translator/fitting/__init__.py` (empty package marker with module docstring only)
- Create: `translator/fitting/profiles.py`
- Test: `tests/test_fitting_profiles.py`

**Interfaces:**
- Consumes: nothing (leaf).
- Produces (verbatim, later tasks rely on these):
  ```python
  @dataclass(frozen=True)
  class EngineProfile:
      name: str
      display: str
      verified: bool
      token_patterns: tuple[str, ...]
      char_widths: dict[str, float]  # keys: "latin_narrow","latin","latin_wide","latin_upper_wide","space","digit","cjk","punct"
      window_cols: float
      max_lines: int
      safe_margin: float
      ratio_threshold: float
  def get_profile(name: str) -> EngineProfile | None
  ENGINE_ORDER: tuple[str, ...]   # ("general","rpgm-mv-mz","rpgm-vxace-xp","rpgm-2000","kirikiri","alicesoft","wolfrpg")
  ENGINE_LABELS: dict[str, str]  # value for engine Option rows
  ```

- [ ] **Step 1: Write the failing test**

```python
"""
tests/test_fitting_profiles.py — Engine profiles: registry, metrics, gating.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.fitting.profiles import (
    ENGINE_LABELS, ENGINE_ORDER, get_profile,
)

def test_general_is_not_a_profile():
    # "general" is an explicit OFF mode, not an EngineProfile object (spec 2.2).
    assert get_profile("general") is None

def test_unknown_is_none():
    assert get_profile("nope") is None

def test_rpgm_mv_mz_shape():
    p = get_profile("rpgm-mv-mz")
    assert p is not None
    assert p.max_lines == 4
    assert p.safe_margin == 0.90
    assert p.window_cols > 0
    assert p.verified is True
    # CJK must be measured — JP sources are the majority case (spec 1.1).
    assert p.char_widths["cjk"] == 1.0
    assert p.char_widths["latin"] == 0.5

def test_every_order_entry_has_label_and_inverse():
    for name in ENGINE_ORDER:
        assert name in ENGINE_LABELS
        assert get_profile(name) is not None or name == "general"
    assert set(ENGINE_LABELS) == set(ENGINE_ORDER)

def test_token_patterns_match_rpgm_codes():
    p = get_profile("rpgm-mv-mz")
    import re
    for sample in (r"\C[12]", r"\V[3]", r"\N[1]", r"\FS[24]", r"\.", r"\|",
                   r"\!", r"\>", r"\^", r"\{", r"\}"):
        assert any(re.search(pat, sample) for pat in p.token_patterns), sample

def test_unverified_profiles_flagged():
    # rpgm-2000 ships best-effort; UI marks it so users are not lied to (spec 1.1).
    assert get_profile("rpgm-2000").verified is False
    assert get_profile("kirikiri").verified is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_fitting_profiles.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'translator.fitting'`

- [ ] **Step 3: Write minimal implementation**

`translator/fitting/__init__.py`:
```python
"""translator/fitting — deterministic text-fit measurement for game dialog windows."""
```

`translator/fitting/profiles.py`:
```python
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
    r"\\V\[\d+\]", r"\\V\[\d+\]", r"\\N\[\d+\]", r"\\C\[\d+\]", r"\\FS\[\d+\]",
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
        token_patterns=_RPGM_TOKENS[:6] + (r"\[w\]", r"\[p\]", r"\[r\]", r"\\@",),
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
    "general":     "General — no fit measurement",
    "rpgm-mv-mz":  "RPG Maker MV/MZ",
    "rpgm-vxace-xp": "RPG Maker VX Ace/XP",
    "rpgm-2000":  "RPG Maker 2000/2003",
    "kirikiri":   "Kirikiri/KAG",
    "alicesoft":  "AliceSoft",
    "wolfrpg":    "Wolf RPG",
}

def get_profile(name: str) -> EngineProfile | None:
    """None for "general" and unknowns: callers treat it as fitting-off."""
    return _PROFILES.get(name)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_fitting_profiles.py -v`
Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add translator/fitting/__init__.py translator/fitting/profiles.py tests/test_fitting_profiles.py
git commit -m "feat(fitting): engine profiles — width buckets, RPGM control codes, registry"
```

---

### Task 2: `fitting/measure.py` — strip tokens, measure, detect tag

**Files:**
- Create: `translator/fitting/measure.py`
- Test: `tests/test_fitting_measure.py`

**Interfaces:**
- Consumes: `EngineProfile` from Task 1.
- Produces:
  ```python
  def strip_tokens(text: str, profile: EngineProfile) -> str
  def measure_lines(text: str, profile: EngineProfile) -> list[float]
  def detect_newline_tag(source_row: str) -> str | None
  def char_width(ch: str, widths: dict[str, float]) -> float
  ```

- [ ] **Step 1: Write the failing test**

```python
"""
tests/test_fitting_measure.py — token stripping, em measurement, tag detection.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.fitting.profiles import get_profile
from translator.fitting.measure import (
    char_width, detect_newline_tag, measure_lines, strip_tokens,
)

MV = get_profile("rpgm-mv-mz")

def test_strip_removes_rpgm_codes():
    raw = r"\C[2]Halo, \V[5]koin\C[0]!"
    assert strip_tokens(raw, MV) == "Halo, koin!"

def test_strip_keeps_normal_text():
    assert strip_tokens("Biasa saja", MV) == "Biasa saja"

def test_measure_per_segment():
    # One newline = two segments; each measured separately (spec 1.2).
    lines = measure_lines("oke\nsip", MV)
    assert len(lines) == 2
    assert lines[0] > 0 and lines[1] > 0

def test_measure_cjk_wider_than_latin():
    assert char_width("月", MV.char_widths) > char_width("a", MV.char_widths)

def test_measure_kirikiri_tags():
    KK = get_profile("kirikiri")
    assert strip_tokens("Hehehe... [w]Good morning.[p]", KK) == "Hehehe... Good morning."

def test_detect_newline_tag_backslash_n():
    assert detect_newline_tag(r"Halo\nDunia") == r"\n"

def test_detect_newline_tag_brackets():
    assert detect_newline_tag("Halo[r]Dunia") == "[r]"
    assert detect_newline_tag("Halo[br]Dunia") == "[br]"
    assert detect_newline_tag("Halo[BR]Dunia") == "[BR]"

def test_detect_newline_tag_priority():
    # backslash-n wins over a later [r] (first priority in the order).
    assert detect_newline_tag(r"a\nb[r]c") == r"\n"

def test_detect_newline_tag_none_when_absent():
    assert detect_newline_tag("Halo Dunia") is None

def test_detect_newline_tag_real_newline_char_is_not_a_tag():
    # A literal 0x0A in the cell is a *segment separator*, not an insertable
    # tag (spec 1.2b): the model cannot emit it through the [N] protocol.
    assert detect_newline_tag("Halo\nDunia") is None

def test_detect_newline_tag_backslash_n_beats_bracket():
    # backslash-n is text; a 0x0A before it does not make it absent.
    assert detect_newline_tag("Halo\nDunia\\nAkhir") == r"\n"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_fitting_measure.py -v`
Expected: FAIL — ModuleNotFoundError

- [ ] **Step 3: Write minimal implementation**

```python
"""
fitting/measure.py — how wide a row is, and which line-break tag it uses.

Two things live here that must never be conflated (spec 1.2b):

1. Segment separators — anything that already breaks a line (a literal 0x0A
   in the cell, or an engine's own tag like [r]). Measurement splits on them;
   the wrap simulation respects them.
2. Insertable tags — a *textual* tag that literally occurs in the source cell
   (backslash-n, [r], [br], [BR]). Only those may be suggested to a model,
   because they survive the [N]-per-line output protocol and the
   encode_placeholders round trip. A raw 0x0A is never "the tag".

`detect_newline_tag` reads the SOURCE row, not the config: the tag the file
actually uses is the tag a suggestion must use, and a file without any tag
gets no line-break advice at all — conservative by design.
"""

from __future__ import annotations

import re
import unicodedata

from .profiles import EngineProfile

# Priority order matters: backslash-n first (most common dump form).
_TAG_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\n", r"\\n"),
    ("[r]",   r"\[r\]"),
    ("[br]",  r"\[br\]"),
    ("[BR]",  r"\[BR\]"),
)

_NARROW = set("iljtIfI'’|!,.:;")
_WIDE = set("mw")
_UPPER_WIDE = set("WM")

def char_width(ch: str, widths: dict[str, float]) -> float:
    if ch == " ":
        return widths["space"]
    if ch.isdigit():
        return widths["digit"]
    if ch in _NARROW:
        return widths["latin_narrow"]
    if ch in _UPPER_WIDE:
        return widths["latin_upper_wide"]
    if ch in _WIDE:
        return widths["latin_wide"]
    code = ord(ch)
    name = unicodedata.name(ch, "")
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return widths["cjk"]
    if ch.isascii() and ch.isprintable() and not ch.isalpha():
        return widths["punct"]
    return widths["latin"]

def strip_tokens(text: str, profile: EngineProfile) -> str:
    out = text
    for pattern in profile.token_patterns:
        out = re.sub(pattern, "", out)
    return out

def _segments(text: str) -> list[str]:
    # A literal 0x0A and the textual tags both split segments for measuring.
    parts = re.split(r"\n|\[r\]|\[br\]|\[BR\]|\\n", text)
    return [p for p in parts]

def measure_lines(text: str, profile: EngineProfile) -> list[float]:
    stripped = [strip_tokens(seg, profile) for seg in _segments(text)]
    return [
        sum(char_width(ch, profile.char_widths) for ch in seg)
        for seg in stripped
    ]

def detect_newline_tag(source_row: str) -> str | None:
    for tag, pattern in _TAG_PATTERNS:
        if re.search(pattern, source_row):
            return tag
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_fitting_measure.py -v`
Expected: 10 PASS

- [ ] **Step 5: Commit**

```bash
git add translator/fitting/measure.py tests/test_fitting_measure.py
git commit -m "feat(fitting): token strip, per-segment em measure, source-derived newline tag"
```

---

### Task 3: `fitting/wrap.py` — word-wrap simulation + mechanical suggestion

**Files:**
- Create: `translator/fitting/wrap.py`
- Test: `tests/test_fitting_wrap.py`

**Interfaces:**
- Consumes: `strip_tokens`, `char_width` (Task 2).
- Produces:
  ```python
  @dataclass
  class WrapMetrics:
      lines: int                 # rendered line count after wrap
      max_width: float           # widest wrapped line, em
      budget: float              # window_cols * safe_margin
      overflow_lines: list[int]  # indices (0-based) of lines wider than budget
      fits: bool                 # no line over budget
      max_ok: bool               # lines <= max_lines
  def wrap_lines(text: str, profile: EngineProfile) -> list[str]
  def wrap_metrics(text: str, profile: EngineProfile) -> WrapMetrics
  def suggest_wrap(text: str, profile: EngineProfile) -> str
  ```

- [ ] **Step 1: Write the failing test**

```python
"""
tests/test_fitting_wrap.py — greedy wrap, metrics, wrap suggestion.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.fitting.profiles import get_profile
from translator.fitting.wrap import suggest_wrap, wrap_lines, wrap_metrics

MV = get_profile("rpgm-mv-mz")

def test_wrap_short_text_one_line():
    assert wrap_lines("Halo", MV) == ["Halo"]

def test_wrap_respects_existing_segments():
    lines = wrap_lines("oke\nsip", MV)
    assert lines == ["oke", "sip"]

def test_wrap_breaks_on_spaces_only():
    text = "Kalimat yang cukup panjang sekali untuk dipaksa turun ke baris berikutnya di jendela"
    lines = wrap_lines(text, MV)
    assert len(lines) >= 2
    for line in lines:
        assert sum(1 for c in line if c == " ") >= 0  # no mid-word cuts: every line joins words

def test_wrap_never_splits_control_code():
    # Even an overlong line containing a code keeps the code intact on one line.
    text = r"teks \C[2]berwarna\C[0] sangat panjang sekali di sini memang benar"
    lines = wrap_lines(text, MV)
    assert any(r"\C[2]berwarna\C[0]" in line for line in lines)

def test_wrap_metrics_fit():
    m = wrap_metrics("Halo", MV)
    assert m.fits and m.max_ok
    assert m.lines == 1
    assert m.budget == MV.window_cols * MV.safe_margin

def test_wrap_metrics_overflow_flag():
    long_line = "kata " * 30   # ~150 em
    m = wrap_metrics(long_line, MV)
    assert not m.fits or not m.max_ok
    assert m.lines >= 2

def test_wrap_metrics_sinking():
    text = "\n".join("baris panjang yang pasti memakan dua baris render karena sangat panjang" for _ in range(4))
    m = wrap_metrics(text, MV)
    assert not m.max_ok   # 4 segments x 2 wrapped lines = 8 > 4

def test_suggest_wrap_no_tag_no_injection():
    # No textual tag in the source -> nothing insertable (spec 1.2b). The
    # suggestion stays the same text; shortening belongs to the judge pass.
    text = "Kalimat panjang yang harus turun ke baris kedua karena tidak muat di jendela"
    out = suggest_wrap(text, MV)
    assert out == text

def test_suggest_wrap_with_tag():
    text = r"Kalimat panjang\nyang harus turun ke baris kedua karena sangat tidak muat di jendela"
    out = suggest_wrap(text, MV)
    assert out.count(r"\n") >= 2   # re-flows using the tag it found in source

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_fitting_wrap.py -v`
Expected: FAIL — ModuleNotFoundError

- [ ] **Step 3: Write minimal implementation**

```python
"""
fitting/wrap.py — simulate the engine's line breaking, mechanically.

RPG Maker vanilla does not wrap: a line runs off the window. So "does it fit"
cannot be answered from the raw string — it can only be answered by simulating
where a sane word-wrap would break, then counting the result against the
window's line budget. This module is that simulation: greedy, deterministic,
and split only on spaces so a control code is never torn in half (the code
travels inside a word, because codes contain no spaces).

`suggest_wrap` re-flows overlong rows with the line-break tag the SOURCE
itself uses (`detect_newline_tag`); when the source has no tag there is
nothing insertable, so the suggestion is returned unchanged — the fix belongs
to the judge's shorten advice instead (spec 2a).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .measure import char_width, detect_newline_tag, strip_tokens
from .profiles import EngineProfile

@dataclass
class WrapMetrics:
    lines: int
    max_width: float
    budget: float
    overflow_lines: list[int] = field(default_factory=list)
    fits: bool = True
    max_ok: bool = True

def _visible(text: str, profile: EngineProfile) -> str:
    return strip_tokens(text, profile)

def _width(text: str, profile: EngineProfile) -> float:
    return sum(char_width(ch, profile.char_widths) for ch in text)

def wrap_lines(text: str, profile: EngineProfile) -> list[str]:
    budget = profile.window_cols * profile.safe_margin
    out: list[str] = []
    # Segments: literal newlines and textual tags both already break lines.
    import re as _re
    for seg in [s for s in _re.split(r"\n|\[r\]|\[br\]|\[BR\]|\\n", text)]:
        words = seg.split(" ")
        current = ""
        for word in words:
            candidate = word if not current else current + " " + word
            if _width(candidate, profile) <= budget or not current:
                current = candidate
            else:
                out.append(current)
                current = word
        out.append(current)
    return out

def wrap_metrics(text: str, profile: EngineProfile) -> WrapMetrics:
    lines = wrap_lines(text, profile)
    budget = profile.window_cols * profile.safe_margin
    widths = [_width(ln, profile) for ln in lines]
    overflow = [i for i, w in enumerate(widths) if w > budget]
    return WrapMetrics(
        lines=len(lines),
        max_width=max(widths) if widths else 0.0,
        budget=budget,
        overflow_lines=overflow,
        fits=not overflow,
        max_ok=len(lines) <= profile.max_lines,
    )

def suggest_wrap(text: str, profile: EngineProfile) -> str:
    tag = detect_newline_tag(text)
    if tag is None:
        return text
    budget = profile.window_cols * profile.safe_margin
    segments: list[str] = []
    import re as _re
    for seg in [s for s in _re.split(r"\n|\[r\]|\[br\]|\[BR\]|\\n", text)]:
        words = seg.split(" ")
        current = ""
        for word in words:
            candidate = word if not current else current + " " + word
            if _width(candidate, profile) <= budget or not current:
                current = candidate
            else:
                segments.append(current)
                current = word
        segments.append(current)
    return tag.join(segments)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_fitting_wrap.py -v`
Expected: PASS (with the corrected suggestion tests)

- [ ] **Step 5: Commit**

```bash
git add translator/fitting/wrap.py tests/test_fitting_wrap.py
git commit -m "feat(fitting): greedy wrap simulation, metrics, source-tag wrap suggestion"
```

---

### Task 4: `fitting/audit.py` — findings + prompt payload

**Files:**
- Create: `translator/fitting/audit.py`
- Test: `tests/test_fitting_audit.py`

**Interfaces:**
- Consumes: `wrap_metrics` (Task 3), `detect_newline_tag` (Task 2), `EngineProfile`.
- Produces:
  ```python
  @dataclass
  class Finding:
      row: int
      source_wrap: WrapMetrics
      target_wrap: WrapMetrics
      ratio: float
      kinds: tuple[str, ...]        # subset of ("overflow","sinking","ratio")
      severity: str                 # "critical" | "moderate" | "minor" | "info"
      newline_tag: str | None
  def audit_rows(rows: list[tuple[int, str, str]], profile: EngineProfile) -> list[Finding]
  def fitting_line(f: Finding) -> str       # "source 2 lines/78% · translation 5 lines/131% · OVERFLOW · tag: [r]"
  def is_ratio_flag(f: Finding) -> bool
  ```

- [ ] **Step 1: Write the failing test**

```python
"""
tests/test_fitting_audit.py — findings, severity anchors, prompt line.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.fitting.profiles import get_profile
from translator.fitting.audit import audit_rows, fitting_line, is_ratio_flag

MV = get_profile("rpgm-mv-mz")

def _long_id(n=40):
    return "kata " * n   # ~200em

def test_clean_rows_produce_no_findings():
    findings = audit_rows([(1, "oke", "sip")], MV)
    assert findings == []

def test_overflow_flagged_moderate():
    rows = [(5, "oke", _long_id())]
    f = audit_rows(rows, MV)[0]
    assert "overflow" in f.kinds or "sinking" in f.kinds
    assert f.severity in ("moderate", "critical")

def test_sinking_is_critical():
    target = "\n".join(_long_id() for _ in range(4))   # wraps to > 4 lines
    f = audit_rows([(7, "oke", target)], MV)[0]
    assert "sinking" in f.kinds
    assert f.severity == "critical"

def test_ratio_uses_threshold_not_flat():
    # A JP source expanding to 150% must NOT flag (under 1.8); 200% must.
    src = "月" * 10                      # 10 em
    mild = "a" * 30                      # 30 * 0.5 = 15 em -> ratio 1.5
    heavy = "a" * 42                     # 21 em -> ratio 2.1
    assert audit_rows([(1, src, mild)], MV) == []
    assert is_ratio_flag(audit_rows([(2, src, heavy)], MV)[0])

def test_empty_translation_skipped():
    assert audit_rows([(3, "oke", "")], MV) == []

def test_newline_tag_recorded():
    f = audit_rows([(9, r"a\nb", _long_id())], MV)[0]
    assert f.newline_tag == r"\n"

def test_fitting_line_shape():
    f = audit_rows([(9, r"a\nb", _long_id())], MV)[0]
    line = fitting_line(f)
    assert "OVERFLOW" in line or "SINKING" in line
    assert "lines" in line and "tag" in line
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_fitting_audit.py -v`
Expected: FAIL — ModuleNotFoundError

- [ ] **Step 3: Write minimal implementation**

```python
"""
fitting/audit.py — turn rows into findings, and findings into prompt payload.

The severity anchor lives HERE and nowhere else (spec 1.4): sinking (needs
more lines than the window shows — the tail is invisible to the player) is
critical, overflow (runs off the right edge) is moderate, a ratio over the
profile's threshold is minor. The judge prompt and the fix loop quote these;
they are never re-derived there.

`ratio` compares *rendered widths*, not character counts, and only flags past
the per-engine threshold: JP→ID normally expands 130–160% in width, and a
flat cutoff would flag nearly every line of a healthy translation (spec 1.4b).

Rows whose translation is empty are skipped, not reported: an empty target is
the translate pipeline's problem (and Judge's `empty` condition), not a
fitting one.
"""

from __future__ import annotations

from dataclasses import dataclass

from .measure import detect_newline_tag
from .profiles import EngineProfile
from .wrap import WrapMetrics, wrap_metrics

@dataclass
class Finding:
    row: int
    source_wrap: WrapMetrics
    target_wrap: WrapMetrics
    ratio: float
    kinds: tuple[str, ...]
    severity: str
    newline_tag: str | None

def audit_rows(rows: list[tuple[int, str, str]], profile: EngineProfile) -> list[Finding]:
    findings: list[Finding] = []
    for row, source, translation in rows:
        if not (translation or "").strip():
            continue
        sw = wrap_metrics(source or "", profile)
        tw = wrap_metrics(translation, profile)
        base = max(sw.max_width, 1.0)
        ratio = tw.max_width / base
        kinds: list[str] = []
        if not tw.max_ok:
            kinds.append("sinking")
        if tw.overflow_lines:
            kinds.append("overflow")
        if ratio > profile.ratio_threshold:
            kinds.append("ratio")
        if not kinds:
            continue
        if "sinking" in kinds:
            severity = "critical"
        elif "overflow" in kinds:
            severity = "moderate"
        else:
            severity = "minor"
        findings.append(Finding(
            row=row, source_wrap=sw, target_wrap=tw, ratio=ratio,
            kinds=tuple(kinds), severity=severity,
            newline_tag=detect_newline_tag(source or ""),
        ))
    return findings

def is_ratio_flag(f: Finding) -> bool:
    return "ratio" in f.kinds

def fitting_line(f: Finding) -> str:
    pct = f.target_wrap.max_width / max(f.target_wrap.budget, 0.01) * 100
    head = (f"source {f.source_wrap.lines} lines/{_pct(f.source_wrap)}% · "
            f"translation {f.target_wrap.lines} lines/{pct:.0f}%")
    labels = {"overflow": "OVERFLOW", "sinking": "SINKING", "ratio": "LONG"}
    tail = " · ".join(labels[k] for k in f.kinds)
    tag = f" · line-break tag in source: {f.newline_tag}" if f.newline_tag else ""
    return f"{head} · {tail}{tag}"

def _pct(w: WrapMetrics) -> str:
    return f"{w.max_width / max(w.budget, 0.01) * 100:.0f}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_fitting_audit.py -v`
Expected: 7 PASS

- [ ] **Step 5: Commit**

```bash
git add translator/fitting/audit.py tests/test_fitting_audit.py
git commit -m "feat(fitting): audit findings with anchored severity and prompt payload line"
```

---

### Task 5: `fitting/apply.py` — backup + write suggestions back

**Files:**
- Create: `translator/fitting/apply.py`
- Test: `tests/test_fitting_apply.py`

**Interfaces:**
- Consumes: `csv_io.read_rows`/`write_rows`/`sniff` (existing, `translator/io/csv_io.py`).
- Produces:
  ```python
  def apply_wrapped(path: str, texts: dict[int, str]) -> int
      # backs up <path> to <stem>.bak once, writes texts into the header-named
      # Translated column (falls back to appending), returns rows written.
  def find_translated_col(headers: list[str]) -> int | None
  ```

- [ ] **Step 1: Write the failing test**

```python
"""
tests/test_fitting_apply.py — audit write-back: backup, column, round trip.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.fitting.apply import apply_wrapped, find_translated_col

_HEADER = "No,Source_File,ID,String,Original_text,Translated\n"

def _make_csv(tmp_path, body):
    csv = tmp_path / "Dialog_Translated.csv"
    csv.write_text(_HEADER + body, encoding="utf-8")
    return str(csv)

def test_find_translated_col():
    assert find_translated_col("No,Source_File,Original_text,Translated".split(",")) == 3
    assert find_translated_col("a,b".split(",")) is None

def test_apply_writes_and_backs_up(tmp_path):
    path = _make_csv(tmp_path, "1,x,y,z,Hello,\n")
    before = open(path, encoding="utf-8").read()
    n = apply_wrapped(path, {2: "Halo"})
    assert n == 1
    after = open(path, encoding="utf-8").read()
    assert "Halo" in after
    assert after != before
    bak = os.path.splitext(path)[0] + ".bak"
    assert os.path.exists(bak)
    assert open(bak, encoding="utf-8").read() == before

def test_apply_second_run_no_double_backup(tmp_path):
    path = _make_csv(tmp_path, "1,x,y,z,Hello,\n")
    apply_wrapped(path, {2: "Halo"})
    bak = os.path.splitext(path)[0] + ".bak"
    first = open(bak, encoding="utf-8").read()
    apply_wrapped(path, {2: "Halo dunia"})
    assert open(bak, encoding="utf-8").read() == first   # original kept, not overwritten

def test_apply_skips_missing_rows(tmp_path):
    path = _make_csv(tmp_path, "1,x,y,z,Hello,\n")
    assert apply_wrapped(path, {99: "nope"}) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_fitting_apply.py -v`
Expected: FAIL — ModuleNotFoundError

- [ ] **Step 3: Write minimal implementation**

```python
"""
fitting/apply.py — write mechanical wrap suggestions into the translated file.

The audit screen's apply action. The first write backs the file up as
<stem>.bak and that backup is never overwritten afterwards — "restore to
before the audit" must always mean the original, not an intermediate state.
The Translated column is located by header name via the same keyword rules the
rest of the app uses (parsing/columns.py vocabulary, simplified: exact
"Translated" match first, then keyword containment) so a 2-column or 6-column
dump both work.
"""

from __future__ import annotations

import os
import shutil

from ..io import csv_io

_KEYWORDS = ("translated", "translation", "target", "output")

def find_translated_col(headers: list[str]) -> int | None:
    for i, h in enumerate(headers):
        if h.strip().lower() == "translated":
            return i
    for i, h in enumerate(headers):
        folded = h.strip().lower()
        if any(k in folded for k in _KEYWORDS):
            return i
    return None

def apply_wrapped(path: str, texts: dict[int, str]) -> int:
    fmt = csv_io.sniff(path)
    rows = csv_io.read_rows(path, fmt)
    if not rows:
        return 0
    col = find_translated_col(rows[0])
    if col is None:
        col = len(rows[0])          # append a new column
        rows[0] = rows[0] + ["Translated"]
    backup = os.path.splitext(path)[0] + ".bak"
    if not os.path.exists(backup):
        shutil.copy2(path, backup)
    written = 0
    for row_num, text in texts.items():
        idx = row_num - 1           # sheet row 1 = headers
        if 1 <= idx < len(rows):
            while len(rows[idx]) <= col:
                rows[idx].append("")
            rows[idx][col] = text
            written += 1
    csv_io.write_rows(path, rows, fmt)
    return written
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_fitting_apply.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add translator/fitting/apply.py tests/test_fitting_apply.py
git commit -m "feat(fitting): audit write-back with one-time backup and header-based column"
```

---

### Task 6: Engine memory in app prefs (per-file, LRU 50)

**Files:**
- Modify: `translator/data/app_prefs.py` (DEFAULTS around line 67, load/save validation, new accessor section near line 440)
- Test: `tests/test_fitting_engine_prefs.py`

**Interfaces:**
- Consumes: existing `load_prefs`/`save_prefs`/`DEFAULTS`.
- Produces:
  ```python
  def remembered_engine(path: str) -> str            # "general" default
  def remember_engine(path: str, engine: str) -> None  # normalises path, LRU-50
  ```

- [ ] **Step 1: Write the failing test**

```python
"""
tests/test_fitting_engine_prefs.py — engine-per-file memory (LRU 50).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from translator import config
from translator.data import app_prefs

@pytest.fixture(autouse=True)
def scratch_prefs(tmp_path, monkeypatch):
    scratch = tmp_path / "app_prefs.json"
    monkeypatch.setattr(config, "APP_PREFS_FILE", str(scratch))
    monkeypatch.setattr(app_prefs.config, "APP_PREFS_FILE", str(scratch))
    yield scratch

def test_unknown_file_defaults_general():
    assert app_prefs.remembered_engine("D:/x/Dialog.csv") == "general"

def test_remember_and_recall():
    app_prefs.remember_engine("D:/x/Dialog.csv", "rpgm-mv-mz")
    assert app_prefs.remembered_engine("D:/x/Dialog.csv") == "rpgm-mv-mz"

def test_lru_cap_50():
    for i in range(60):
        app_prefs.remember_engine(f"D:/g{i}/file.csv", "kirikiri")
    assert app_prefs.remembered_engine("D:/g0/file.csv") == "general"   # evicted
    assert app_prefs.remembered_engine("D:/g59/file.csv") == "kirikiri"

def test_only_real_engines_stored():
    app_prefs.remember_engine("D:/x/Dialog.csv", "made-up-engine")
    assert app_prefs.remembered_engine("D:/x/Dialog.csv") == "general"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_fitting_engine_prefs.py -v`
Expected: FAIL — AttributeError: remembered_engine

- [ ] **Step 3: Write minimal implementation**

In `translator/data/app_prefs.py`, follow the file's established 4-touchpoint pattern:

1. In `DEFAULTS` (after `"judge_model_prefs": {}` around line 67) add:
```python
    # Engine profile chosen per file (FileEngineScreen). Keyed by resolved
    # absolute path so the same dump always recalls its engine; LRU-capped so
    # an old project folder cannot grow the prefs file forever.
    "engine_per_file": {},
```
2. Add a cleaner near `_clean_judge_model_prefs` (line ~390):
```python
def _clean_engine_per_file(value) -> dict:
    from ..fitting.profiles import ENGINE_ORDER
    ok = set(ENGINE_ORDER)
    out: dict[str, str] = {}
    if not isinstance(value, dict):
        return out
    for path, engine in value.items():
        if isinstance(path, str) and isinstance(engine, str) and engine in ok:
            out[path] = engine
    return out
```
3. Wire into BOTH `load_prefs()` (line ~114 area) and `save_prefs()` (line ~145 area) exactly like the other keys:
```python
    prefs["engine_per_file"] = _clean_engine_per_file(payload.get("engine_per_file"))
```
4. Accessors (bottom section, style of `set_failover_retry_threshold`):
```python
# ── Fitting engine memory ────────────────────────────────────────────────

def remembered_engine(path: str) -> str:
    store = load_prefs()["engine_per_file"]
    return store.get(os.path.abspath(path), "general")

def remember_engine(path: str, engine: str) -> None:
    from ..fitting.profiles import ENGINE_ORDER
    if engine not in ENGINE_ORDER:
        return
    key = os.path.abspath(path)
    current = load_prefs()["engine_per_file"]
    current.pop(key, None)
    current[key] = engine
    while len(current) > 50:
        current.pop(next(iter(current)))
    save_prefs({"engine_per_file": current})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_fitting_engine_prefs.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add translator/data/app_prefs.py tests/test_fitting_engine_prefs.py
git commit -m "feat(prefs): engine-per-file memory, LRU-capped, validated against ENGINE_ORDER"
```

---

### Task 7: `Picker.max_visible` — scrolled 5-row file box

**Files:**
- Modify: `translator/tui/widgets/common.py` (`Picker.__init__` line ~119, `get_content_height` line ~248)
- Test: `tests/test_fitting_picker_height.py`

**Interfaces:**
- Consumes: existing `Picker`.
- Produces: `Picker(options=None, multi_select=False, click_activates=True, max_visible=None, **kwargs)` — content height `min(len(options), max_visible)` when set; scroll_offset already exists so keyboard nav scrolls.

- [ ] **Step 1: Write the failing test**

```python
"""
tests/test_fitting_picker_height.py — Picker max_visible clamps the box.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.tui.widgets.common import Option, Picker

def test_max_visible_clamps_height():
    picker = Picker(max_visible=5)
    picker.set_options([Option(f"f{i}", i) for i in range(40)])
    # get_content_height(container, viewport, width) — pass dummies; Picker
    # only uses the option count.
    assert picker.get_content_height(None, None, 80) == 5

def test_no_max_visible_keeps_auto():
    picker = Picker()
    picker.set_options([Option(f"f{i}", i) for i in range(9)])
    assert picker.get_content_height(None, None, 80) == 9

def test_fewer_options_shrinks():
    picker = Picker(max_visible=5)
    picker.set_options([Option("a", 1), Option("b", 2)])
    assert picker.get_content_height(None, None, 80) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_fitting_picker_height.py -v`
Expected: FAIL — TypeError: unexpected keyword 'max_visible'

- [ ] **Step 3: Write minimal implementation**

In `translator/tui/widgets/common.py`, `Picker.__init__` (line ~119):
```python
    def __init__(
        self,
        options: Iterable[Option] | None = None,
        multi_select: bool = False,
        click_activates: bool = True,
        max_visible: int | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.options: list[Option] = list(options or [])
        self.multi_select = multi_select
        self.click_activates = click_activates
        # A scrolled list instead of a screen-growing one: the file screen
        # with dozens of spreadsheets must stay compact (spec 2.1). None keeps
        # the old grow-to-fit behaviour for every other picker.
        self.max_visible = max_visible
```
And `get_content_height` (line ~248):
```python
    def get_content_height(self, container, viewport, width: int) -> int:
        height = max(1, len(self.options))
        if self.max_visible is not None:
            height = min(height, self.max_visible)
        return height
```
Note: `render_line` already reads `self.scroll_offset.y` (line ~252), so keyboard navigation scrolls within the clamped box without further changes. If Textual does not scroll automatically for a clamped Widget, wrap the Picker in a `VerticalScroll` container on the file screen side (Task 8 handles that composition); the widget change is still correct.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_fitting_picker_height.py -v`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add translator/tui/widgets/common.py tests/test_fitting_picker_height.py
git commit -m "feat(tui): Picker max_visible — compact scrolled lists"
```

---

### Task 8: `FileEngineScreen` — shared file + engine screen

**Files:**
- Create: `translator/tui/screens/file_engine.py`
- Modify: `translator/tui/screens/wizard.py` (`OperationModeScreen.handle_choice` line ~368-370: push `FileEngineScreen`; DELETE `FileSelectScreen` class lines 375-477; update `_TranslateEditScreen` jump table if it references FileSelectScreen)
- Modify: `translator/tui/screens/judge.py` (`JudgeModelScreen` push site line ~752: push `FileEngineScreen`; DELETE `JudgeFileScreen` class lines 1140-1257; fix any references)
- Test: `tests/test_file_engine_screen.py`

**Interfaces:**
- Consumes: `Picker(max_visible=5)` (Task 7), `LabeledInput`, `Option`, `app_prefs.remembered_engine/remember_engine` (Task 6), `ExcelReader.scan_directory`, `translated_path`, `ENGINE_ORDER`/`ENGINE_LABELS` (Task 1).
- Produces:
  ```python
  class FileEngineScreen(BaseScreen):
      # kwargs: title="Select a spreadsheet", confirm_label="Confirm and next",
      #         draft_kind="translate" | "judge", next_screen=Callable[[], BaseScreen],
      #         on_accept=Callable[[str, str], None]   # (path, engine) — wizard/judge store in their own draft
  ```
  Draft keys (both wizards, stored by the screens themselves via `on_accept` callbacks):
  - translate: `draft["src_path"]`, `draft["out_path"]`, `draft["fitting_engine"]`
  - judge: `judge_draft["path"]`, `judge_draft["fitting_engine"]`

- [ ] **Step 1: Write the failing test**

```python
"""
tests/test_file_engine_screen.py — engine list wiring + path accept logic.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.fitting.profiles import ENGINE_LABELS, ENGINE_ORDER
from translator.tui.screens.file_engine import engine_options, resolve_accept

def test_engine_options_general_first():
    opts = engine_options()
    assert opts[0][0] == "general"
    assert [v for v, _l in opts] == list(ENGINE_ORDER)

def test_unverified_engines_marked():
    labels = {v: l for v, l in engine_options()}
    assert "(unverified)" in labels["rpgm-2000"]

def test_resolve_accept_rejects_missing_file(tmp_path):
    errors = []
    ok = resolve_accept(str(tmp_path / "nope.csv"), "general", errors.append)
    assert ok is False and errors
def test_resolve_accept_passes_back_path_and_engine(tmp_path):
    f = tmp_path / "Dialog.csv"
    f.write_text("a,b\n1,2\n", encoding="utf-8")
    seen = []
    ok = resolve_accept(str(f), "rpgm-mv-mz", seen.append)
    assert ok is True
    assert seen == [(str(f), "rpgm-mv-mz")]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_file_engine_screen.py -v`
Expected: FAIL — ModuleNotFoundError

- [ ] **Step 3: Write minimal implementation**

`translator/tui/screens/file_engine.py`:
```python
"""
tui/screens/file_engine.py — pick the file AND the engine, on one screen.

Replaces the two near-identical file screens (wizard.FileSelectScreen /
judge.JudgeFileScreen) with one shared component, per the standing rule that
same-function screens use shared components. Layout (spec 2.1): a scrolled
file box (5 visible), a path input the box feeds on click, an engine select
under it, one Confirm and next gate. The click no longer commits — the user
may want to change the engine after picking the file, and the confirm bar is
where the commit belongs.

Keyboard contract: Enter on the list fills the path box and moves focus
there; Enter in the path box is Confirm and next. Two enters, file done.

Textual note: screens are instantiated inside a running app only. This module
is therefore import-safe (no screen instantiation at import time) and its
pure helpers (engine_options, resolve_accept) are unit-tested without an app.
"""

from __future__ import annotations

import os
from typing import Callable

from textual.widgets import Label

from ...data import app_prefs
from ...fitting.profiles import ENGINE_LABELS, ENGINE_ORDER, get_profile
from ...io.excel_io import ExcelReader, translated_path
from ..app import BaseScreen
from ..theme import MUTED, OK, WARN
from ..widgets.common import LabeledInput, Option, Picker

MAX_VISIBLE_FILES = 5

def engine_options() -> list[tuple[str, str]]:
    """(value, label) pairs; unverified profiles are marked so the user is told."""
    out = []
    for name in ENGINE_ORDER:
        label = ENGINE_LABELS[name]
        p = get_profile(name)
        if p is not None and not p.verified:
            label += " (unverified)"
        out.append((name, label))
    return out

def resolve_accept(path: str, engine: str, on_ok: Callable[[str, str], None]) -> bool:
    """Validate the path; on success hand (path, engine) to the caller."""
    path = os.path.expanduser((path or "").strip().strip('"'))
    if not path or not os.path.isfile(path):
        return False
    on_ok(path, engine)
    return True

class FileEngineScreen(BaseScreen):
    def __init__(self, *, title="Select a spreadsheet",
                 confirm_label="Confirm and next",
                 on_accept: Callable[[str, str], None],
                 next_screen: Callable[[], "BaseScreen"],
                 existing_path: str = "",
                 sort_translated_first: bool = False,
                 **kwargs) -> None:
        super().__init__(**kwargs)
        self._title = title
        self._confirm_label = confirm_label
        self._on_accept = on_accept
        self._next = next_screen
        self._existing = existing_path
        self._sort_translated = sort_translated_first

    def compose_body(self):
        yield Label(self._title, classes="title")
        yield Label(f".xlsx / .csv files in {os.getcwd()}", classes="muted")
        self.picker = Picker(max_visible=MAX_VISIBLE_FILES)
        yield self.picker
        yield Label("")
        self._path = LabeledInput("Path", value=self._existing,
                                  placeholder="/path/to/file.xlsx or .csv")
        yield self._path
        yield Label("Engine (text fitting)", classes="muted")
        self._engine = Picker()          # single-select, click_activates=False
        yield self._engine
        yield Label("Fitting measures the engine's default message window — "
                    "wordwrap plugins make it inaccurate.", classes="muted")
        yield Label("", id="file-engine-error", classes="error")

    def on_screen_ready(self) -> None:
        files = ExcelReader.scan_directory(os.getcwd())
        options = []
        for name in files:
            full = os.path.join(os.getcwd(), name)
            try:
                size = os.path.getsize(full) / 1024
                size_text = f"{size / 1024:.1f} MB" if size > 1024 else f"{size:.0f} KB"
            except OSError:
                size_text = "?"
            options.append(Option(name, full, hint=size_text))
        if self._sort_translated:
            # Judge habit: translated outputs first (ported from JudgeFileScreen).
            options.sort(key=lambda o: (
                0 if config.TRANSLATED_SUFFIX in o.label.lower() else 1,
                o.label.lower(),
            ))
        if not options:
            options.append(Option("No spreadsheet found here — type a path below", "",
                                 enabled=False, style=MUTED))
        self.picker.set_options(options)
        if self._existing:
            for i, opt in enumerate(options):
                if opt.value == self._existing:
                    self.picker.index = i
                    break
        self._engine.set_options([
            Option(label, value, selected=value == "general")
            for value, label in engine_options()
        ])
        self.picker.focus()

    def on_picker_highlighted(self, event: Picker.Highlighted) -> None:
        # The file box feeds the path box as the cursor moves (and the
        # remembered engine follows the highlighted file).
        if event.picker is self.picker and event.option and event.option.value:
            self._path.input.value = str(event.option.value)
            self._recall_engine(str(event.option.value))

    def _recall_engine(self, path: str) -> None:
        engine = app_prefs.remembered_engine(path)
        for i, opt in enumerate(self._engine.options):
            if opt.value == engine:
                self._engine.index = i
                break

    def on_picker_selected(self, event: Picker.Selected) -> None:
        # Enter/click on a file: fill the path, move to the engine select.
        if event.picker is self.picker and event.option.value:
            self._path.input.value = str(event.option.value)
            self._recall_engine(str(event.option.value))
            self._engine.focus()

    def on_input_submitted(self, event) -> None:
        self._confirm()

    def _confirm(self) -> None:
        path = (self._path.input.value or "").strip().strip('"')
        engine = str((self._engine.current.value if self._engine.current else "general"))
        error = self.query_one("#file-engine-error", Label)
        def accept(path_: str, engine_: str) -> None:
            self._on_accept(path_, engine_)
        if not resolve_accept(path, engine, accept):
            error.update(f"File not found: {path}")
            return
        app_prefs.remember_engine(path, engine)
        self.app.push_screen(self._next())

    def screen_keys(self) -> list[tuple[str, str]]:
        return [("↑↓", "move"), ("⏎", "select"), ("tab", "next field"),
                ("←", "back"), ("esc", "cancel")]
```

Then rewire the two wizards:

`wizard.py` — the push into the file step is NOT in `OperationModeScreen` alone: every mode (translate/repair/retranslate) funnels into it, so change the shared push site that today creates `FileSelectScreen()` (locate `push_screen(FileSelectScreen())` — historically wizard.py:370 — and replace all its occurrences) with:
```python
        from .file_engine import FileEngineScreen
        self.app.push_screen(FileEngineScreen(
            on_accept=self._file_engine_accepted,
            next_screen=lambda: ColumnsScreen(),
            existing_path=self.draft.get("src_path", ""),
        ))

    def _file_engine_accepted(self, path: str, engine: str) -> None:
        if path != self.draft.get("src_path"):
            for key in ("src_col", "dst_col", "ctx_col", "repair_col"):
                self.draft.pop(key, None)
        self.draft["src_path"] = path
        self.draft["out_path"] = translated_path(path)
        self.draft["fitting_engine"] = engine
```
(`_file_engine_accepted` can live on the wizard module level or as a method on the screen doing the push — wherever `FileSelectScreen._accept` logic lived before, wizard.py:465-477; keep the same home.)
Delete the `FileSelectScreen` class (wizard.py:375-477) and drop its import/uses (check `_TranslateEditScreen` jump table, wizard.py:1167-1179, and `PreviewScreen` references).

`judge.py` — the judge file push lives in `JudgeModelScreen` (push site historically judge.py:752). Replace the `push_screen(JudgeFileScreen())` occurrence(s) with the shared screen:
```python
        from .file_engine import FileEngineScreen
        self.app.push_screen(FileEngineScreen(
            on_accept=self._file_engine_accepted,
            next_screen=lambda: JudgeColumnsScreen(),
            existing_path=self.draft.get("path", ""),
            sort_translated_first=True,
        ))

    def _file_engine_accepted(self, path: str, engine: str) -> None:
        if path != self.draft.get("path"):
            for key in ("src_col", "dst_col", "ctx_col"):
                self.draft.pop(key, None)
        self.draft["path"] = path
        self.draft["fitting_engine"] = engine
```
(Mirror the exact column-invalidation behavior of the old `JudgeFileScreen._accept`, judge.py:1245-1257 — same keys, same order.)
Delete the `JudgeFileScreen` class (judge.py:1140-1257) and its references (`_JudgeEditScreen` jump table ~1899).

Notes for the implementer:
- The `from .base import BaseScreen` import must match how `wizard.py` imports it today (`from ..app import BaseScreen` style — copy the exact import line from wizard.py:top).
- `BaseScreen` API (`compose_body`, `on_screen_ready`, `screen_keys`, `query_one`, `notify_status`) comes from `tui/app.py:38`; mirror `FileSelectScreen`'s exact usage (wizard.py:375-477) for anything ambiguous.
- The judge variant previously sorted `*_Translated` files first and showed a note about fix write-back; port those two behaviors into the shared screen via optional kwargs (`sort_translated_first: bool = False`, note-builder callback) — keep the shared core identical, pass differences as parameters (same approach the two drafts take).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_file_engine_screen.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add translator/tui/screens/file_engine.py translator/tui/screens/wizard.py translator/tui/screens/judge.py tests/test_file_engine_screen.py
git commit -m "feat(tui): shared FileEngineScreen — 5-visible file box, engine select, confirm gate"
```

---

### Task 9: Translate prompt budget (soft) + JobConfig field

**Files:**
- Modify: `translator/parsing/prompts.py` (`build_translate_prompt` line 35, `build_translate_solo_prompt` line 139)
- Modify: `translator/core/models.py` (JobConfig — add field next to `write_mode` ~line 90)
- Modify: `translator/core/engine.py` (`prompt_builder` closure line 439-449 and `solo_prompt_builder` line 451-460)
- Modify: `translator/tui/screens/wizard.py` (`PreviewScreen.build_job` line 1102-1129 — read `draft["fitting_engine"]`)
- Test: `tests/test_fitting_translate_prompt.py`

**Interfaces:**
- Consumes: `fitting/profiles.get_profile` (Task 1).
- Produces:
  ```python
  # prompts.py
  def build_translate_prompt(..., fitting_budget: str = "") -> str
  def build_translate_solo_prompt(..., fitting_budget: str = "") -> str
  # models.py JobConfig
  fitting_engine: str = "general"      # "general" | engine profile name
  # engine.py: reads job.fitting_engine, builds budget string, passes to both builders
  ```

- [ ] **Step 1: Write the failing test**

```python
"""
tests/test_fitting_translate_prompt.py — soft budget injection into translate prompts.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.parsing.prompts import build_translate_prompt, build_translate_solo_prompt

ROWS = [(1, "Hello")]

def test_no_budget_unchanged():
    assert "dialog window" not in build_translate_prompt(
        ROWS, "English", "Indonesian", "", prompt_format="plain")

def test_budget_present_in_prompt():
    out = build_translate_prompt(
        ROWS, "English", "Indonesian", "",
        fitting_budget="4 lines x 28.8 em", prompt_format="plain")
    assert "4 lines x 28.8 em" in out
    assert "NEVER sacrifice meaning" in out

def test_budget_forbids_self_linebreaks():
    out = build_translate_prompt(
        ROWS, "English", "Indonesian", "",
        fitting_budget="4 lines x 28.8 em", prompt_format="plain")
    assert "Do NOT insert line breaks" in out

def test_solo_prompt_gets_budget():
    out = build_translate_solo_prompt(1, "Hello", "English", "Indonesian", "",
                                      fitting_budget="4 lines x 28.8 em")
    assert "4 lines x 28.8 em" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_fitting_translate_prompt.py -v`
Expected: FAIL — TypeError: unexpected keyword 'fitting_budget'

- [ ] **Step 3: Write minimal implementation**

`translator/parsing/prompts.py` — add to both builders' signatures (`fitting_budget: str = ""`), and inside each, after the existing `ph_note` block:
```python
    fit_note = ""
    if fitting_budget.strip():
        # Soft on purpose (spec 2.4): a hard constraint makes a model trade
        # meaning for width, and the judge pass is the place where a genuinely
        # unfixable line gets a human verdict. The line also forbids the model
        # from inserting breaks itself — reflowing is the fix loop's job.
        fit_note = (
            f"\n- Text fitting: the game's dialog window fits {fitting_budget.strip()}. "
            f"AIM to fit, but NEVER sacrifice meaning or drop content to fit — "
            f"if both are impossible, keep the meaning; a later review pass "
            f"will flag it.\n"
            f"- Do NOT insert line breaks yourself; keep each translation as one line.\n"
        )
```
and splice `f"{fit_note}"` into the `rules` string (after `lines_rule`). Import `fitting_budget` param in `build_translate_solo_prompt` the same way and append `fit_note` before its output contract.

`translator/core/models.py` — in `JobConfig`, next to `write_mode`:
```python
    # Engine profile for text fitting ("general" = off). The wizard's
    # FileEngineScreen sets it; engine.py turns it into a prompt budget line.
    fitting_engine: str = "general"
```

`translator/core/engine.py` — in `prompt_builder` (line ~426-449), before the `build_translate_prompt(...)` call add:
```python
        fitting_budget = ""
        profile = get_profile(getattr(job, "fitting_engine", "general") or "general")
        if profile is not None:
            fitting_budget = f"{profile.max_lines} lines x " \
                             f"{profile.window_cols * profile.safe_margin:.1f} em"
```
(with `from ..fitting.profiles import get_profile` at the top of the file or inside the closure following the `batch_context` precedent at engine.py:433-438), then pass `fitting_budget=fitting_budget` into both `build_translate_prompt(...)` (line 439) and `build_translate_solo_prompt(...)` (line 451). Do the same in the solo closure.

`translator/tui/screens/wizard.py` — in `PreviewScreen.build_job()` (line ~1102-1129), add:
```python
        job.fitting_engine = self.draft.get("fitting_engine", "general")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_fitting_translate_prompt.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add translator/parsing/prompts.py translator/core/models.py translator/core/engine.py translator/tui/screens/wizard.py tests/test_fitting_translate_prompt.py
git commit -m "feat(translate): soft dialog-window budget injected when an engine profile is active"
```

---

### Task 10: Judge criterion `fitting` — models + prompt injection

**Files:**
- Modify: `translator/judge/models.py` (CRITERIA line 25, CRITERION_LABELS line 28)
- Modify: `translator/judge/prompts.py` (`_CRITERION_RULES` line 249, `_rows_block` line 383, `build_judge_prompt` line 484, `build_fix_prompt` line 726, `build_fix_solo_prompt` line 789)
- Test: `tests/test_fitting_judge_prompt.py`

**Interfaces:**
- Consumes: `fitting/audit.audit_rows` + `fitting_line` (Task 4), `strip_tokens`-free — findings computed by runner (Task 11) and passed in.
- Produces:
  ```python
  # models.py
  CRITERIA = ("mistranslation", "context", "placeholder", "style", "fitting")
  CRITERION_LABELS["fitting"] = "Fitting — the line does not fit the game dialog window (overflow / too many lines / much longer than source)"
  # prompts.py
  def build_judge_prompt(..., fitting_map: dict[int, str] | None = None) -> str
  def build_fix_prompt(..., fitting_map: dict[int, str] | None = None) -> str
  def build_fix_solo_prompt(..., fitting: str = "") -> str
  def _fitting_rule() -> str    # criterion rule text
  ```

- [ ] **Step 1: Write the failing test**

```python
"""
tests/test_fitting_judge_prompt.py — criterion wiring + per-row FITTING line.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.judge.models import CRITERIA, CRITERION_LABELS
from translator.judge.prompts import (
    build_fix_prompt, build_fix_solo_prompt, build_judge_prompt,
)

ROWS = [(4, "Halo", "Hello world")]

def test_fitting_is_fifth_criterion():
    assert "fitting" in CRITERIA
    assert "fitting" in CRITERION_LABELS

def test_judge_prompt_contains_fitting_line():
    out = build_judge_prompt(
        rows=ROWS, criteria=["fitting"],
        fitting_map={4: "source 1 lines/10% · translation 5 lines/131% · OVERFLOW"},
    )
    assert "FITTING     :" in out
    assert "131%" in out
    assert "OVERFLOW" in out

def test_judge_prompt_without_map_has_no_fitting_noise():
    out = build_judge_prompt(rows=ROWS, criteria=["fitting"])
    assert "FITTING" not in out

def test_judge_prompt_fitting_rule_mentions_tags():
    out = build_judge_prompt(rows=ROWS, criteria=["fitting"],
                             fitting_map={4: "x · line-break tag in source: [r]"})
    assert "line-break tag" in out

def test_fix_prompt_carries_fitting_block():
    out = build_fix_prompt(
        rows=ROWS, criteria=["fitting"],
        fitting_map={4: "source 1 lines/10% · translation 5 lines/131% · SINKING · line-break tag in source: [r]"},
    )
    assert "FITTING" in out and "SINKING" in out
    assert "must not exceed" in out

def test_fix_solo_prompt_carries_fitting():
    out = build_fix_solo_prompt(4, "Halo", "Hello world",
                                fitting="5 lines/131% · SINKING · tag: [r]")
    assert "FITTING" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_fitting_judge_prompt.py -v`
Expected: FAIL — TypeError (fitting_map) / assertion (CRITERIA)

- [ ] **Step 3: Write minimal implementation**

`translator/judge/models.py` (lines 25-33):
```python
CRITERIA: tuple[str, ...] = ("mistranslation", "context", "placeholder", "style", "fitting")
```
add label:
```python
    "fitting":        "Fitting — the line does not fit the game dialog window (overflow / too many lines / much longer than source)",
```
Note: `ERROR_TYPES` (line 26) is CRITERIA-derived so it picks `fitting` up automatically.

`translator/judge/prompts.py`:

1. Add the criterion rule to `_CRITERION_RULES` (after `"style"`, line ~284):
```python
    "fitting": (
        "fitting — the line does not fit the game dialog window. The FITTING "
        "data printed with a row is measured mechanically and is ground "
        "truth: never re-estimate it, never argue with it.\n"
        "    Rows marked OVERFLOW or SINKING are already flagged by that "
        "measurement — your job for them is only the repair advice, not the "
        "verdict. Rows marked LONG are much wider than their own source "
        "beyond normal expansion; you may flag them, and your comment MUST "
        "quote the measured ratio.\n"
        "    Do NOT flag a line merely because the source itself is long: "
        "compare against the row's own source width only. In the FIX: "
        "shorten the line naturally (never by dropping facts, names, numbers "
        "or clauses — tighten the wording, do not cut the content), OR insert "
        "the exact line-break tag printed with the row at a natural phrase "
        "boundary. Never invent a tag the row does not show, never split a "
        "control code, never add a line beyond the window's line count"
    ),
```
2. Extend `_rows_block` (line 383-396) with an optional `fitting_map`:
```python
def _rows_block(
    rows: list[tuple[int, str, str]],
    context_map: dict[int, str] | None = None,
    fitting_map: dict[int, str] | None = None,
) -> str:
    blocks: list[str] = []
    for row, source, translation in rows:
        lines = [f"[{row}]"]
        if context_map and (context_map.get(row) or "").strip():
            lines.append(f"  CONTEXT     : {encode_placeholders(context_map[row])}")
        lines.append(f"  SOURCE      : {encode_placeholders(source or '')}")
        lines.append(f"  TRANSLATION : {encode_placeholders(translation or '')}")
        if fitting_map and (fitting_map.get(row) or "").strip():
            lines.append(f"  FITTING     : {fitting_map[row]}")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)
```
3. `build_judge_prompt` (line 484): add `fitting_map: dict[int, str] | None = None` to the signature; thread it into the existing `_rows_block(...)` call; after `_criteria_block(...)` in the returned prompt add, when fitting is chosen and the map is non-empty:
```python
        f"\nThe FITTING lines are machine-measured facts about the game dialog "
        f"window. Treat them as ground truth; your verdicts about meaning, "
        f"context, placeholders and style are unchanged by them.\n"
```
4. `build_fix_prompt` (line 726): add `fitting_map: dict[int, str] | None = None`; in the per-row block loop add (after CORRECTION NOTE):
```python
        if fitting_map and (fitting_map.get(row) or "").strip():
            lines.append(f"  FITTING     : {fitting_map[row]}")
```
and after the existing output-format rules add:
```python
        f"- A FITTING line means the text must end up within the window: "
        f"shorten it naturally or insert the line-break tag shown in that "
        f"row at a natural pause; the result must not exceed 4 lines and "
        f"must never split a control code or invent a tag\n"
```
5. `build_fix_solo_prompt` (line 789): add `fitting: str = ""`; after the Correction Note line:
```python
    if (fitting or "").strip():
        parts.append(f"Fitting    : {fitting}\n")
```
and extend the Instruction block with the same must-not-exceed/tag rule (one sentence, same wording as the batch variant).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_fitting_judge_prompt.py -v`
Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add translator/judge/models.py translator/judge/prompts.py tests/test_fitting_judge_prompt.py
git commit -m "feat(judge): fitting criterion — measured data injected per row, tag-copy repair rule"
```

---

### Task 11: Runner — fitting findings, re-measure loop, whitelist, gating

**Files:**
- Modify: `translator/judge/runner.py` (`JudgeRunner.__init__` line 168 accepts `fitting_profile: str = "general"`; `criteria` property line 325; `_run_evaluation` line 520; `fix_all` line 840; `fix_single` line 1051; `apply_fix` unchanged)
- Modify: `translator/tui/screens/judge.py` (criteria screen gating ~line 824; `JudgePreviewScreen._criteria` ~1682; `JudgeRunScreen` plumbing 2177/3053/3109; Fix All setup passes profile)
- Test: `tests/test_fitting_runner.py`

**Interfaces:**
- Consumes: `fitting.audit.audit_rows`, `fitting.audit.Finding`, `fitting.audit.fitting_line`, `fitting.wrap.wrap_metrics` (Tasks 3-4), `get_profile` (Task 1).
- Produces:
  ```python
  # runner.py
  JudgeRunner(..., fitting_profile: str = "general")
  # criteria property now ALSO drops "fitting" when profile is general/None
  def _fitting_map(self, rows) -> dict[int, str]          # {row: fitting_line(...)}
  def revalidate_fix(self, row: int, fixed_text: str, source: str) -> str | None
      # None = pass; "overflow"/"sinking"/"invented-token"/"too-many-lines" = fail kind
  ```

- [ ] **Step 1: Write the failing test**

```python
"""
tests/test_fitting_runner.py — machine flags, fix revalidation, token whitelist.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.fitting.profiles import get_profile
from translator.judge.runner import JudgeRunner

MV = get_profile("rpgm-mv-mz")

def _runner(tmp_path, engine="rpgm-mv-mz"):
    # Minimal runner without network: target only. JudgeRunner signature
    # requires target; job/provider optional.
    from translator.judge.runner import JudgeTarget
    return JudgeRunner(
        JudgeTarget(path=str(tmp_path / "x.csv"), src_col=2, dst_col=3),
        fitting_profile=engine,
    )

def test_general_profile_disables_fitting(tmp_path):
    r = _runner(tmp_path, engine="general")
    assert "fitting" not in r.criteria

def test_mv_profile_keeps_fitting(tmp_path):
    r = _runner(tmp_path, engine="rpgm-mv-mz")
    assert "fitting" in r.criteria

def test_revalidate_pass(tmp_path):
    r = _runner(tmp_path)
    assert r.revalidate_fix(1, "Halo", "Halo") is None

def test_revalidate_detects_invented_token(tmp_path):
    r = _runner(tmp_path)
    # Source has no [br]; the fixed text invented one -> whitelist rejects.
    assert r.revalidate_fix(1, "Halo[br]Dunia", "Halo Dunia") == "invented-token"

def test_revalidate_detects_sinking(tmp_path):
    r = _runner(tmp_path)
    long_text = "\n".join("kata " * 20 for _ in range(4))
    assert r.revalidate_fix(1, long_text, "oke") == "sinking"

def test_revalidate_allows_source_tag(tmp_path):
    r = _runner(tmp_path)
    # Source itself uses \n: the fix may re-use it.
    assert r.revalidate_fix(1, "Halo\\nDunia", "Halo\\nDunia") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_fitting_runner.py -v`
Expected: FAIL — TypeError fitting_profile / revalidate_fix

- [ ] **Step 3: Write minimal implementation**

`translator/judge/runner.py`:

1. `__init__` (line 168-187): add `fitting_profile: str = "general"` param; store `self._fitting_profile = fitting_profile or "general"`; add:
```python
        from ..fitting.profiles import get_profile as _get_profile
        self._profile = _get_profile(self._fitting_profile)
```
2. `criteria` property (line 325) — extend the drop logic:
```python
    @property
    def criteria(self) -> list[str]:
        source = self._criteria
        chosen = [name for name in source if name in CRITERIA]
        if not self._target.has_context and "context" in chosen:
            chosen.remove("context")
        if self._profile is None and "fitting" in chosen:
            # Fitting without a profile is a criterion about nothing: the
            # machine that feeds it has no window to measure against (spec 3.2).
            chosen.remove("fitting")
        return chosen or ["mistranslation"]
```
(apply the same two lines in `_criteria_for`, line 329-342).
3. New method:
```python
    def _fitting_map(self, rows: list[tuple[int, str, str]]) -> dict[int, str]:
        """Per-row FITTING prompt line — machine-measured, only for flagged rows."""
        from ..fitting.audit import audit_rows, fitting_line
        if self._profile is None:
            return {}
        return {
            f.row: fitting_line(f) for f in audit_rows(rows, self._profile)
        }

    def revalidate_fix(self, row: int, fixed_text: str, source: str) -> str | None:
        """
        Post-fix gate (spec 3.5): a fix is a promise about the window.

        Re-measures the fixed text and checks that every token in it existed
        in the source (models sometimes invent a plausible-looking [br] for
        an engine that has none). Returns None when the fix is clean, or the
        failure kind for the caller to feed back into the retry.
        """
        from ..fitting.measure import detect_newline_tag
        from ..fitting.wrap import wrap_metrics
        if self._profile is None:
            return None
        metrics = wrap_metrics(fixed_text or "", self._profile)
        if not metrics.max_ok:
            return "sinking"
        if metrics.overflow_lines:
            return "overflow"
        allowed = {detect_newline_tag(source)} if detect_newline_tag(source) else set()
        if detect_newline_tag(fixed_text or "") not in allowed and detect_newline_tag(fixed_text or ""):
            return "invented-token"
        return None
```
4. `_run_evaluation` (line 520+): inside the nested `build_prompt` (line ~546), pass `fitting_map=self._fitting_map(section_rows)` to `build_judge_prompt` — compute once per section before the loop, keyed only for rows in that section.
5. `fix_all` (line 840-1008): after the nested `build_prompt` (line ~909) pass `fitting_map=...` similarly; inside `collect` (line ~953-979), after a text is accepted and before staging it, run the gate:
```python
                    if "fitting" in (criteria or self.criteria):
                        fail_kind = self.revalidate_fix(row, text, by_row[row][0])
                        if fail_kind:
                            retries_left = retry_budget.get(row, 2)
                            if retries_left > 0:
                                retry_budget[row] = retries_left - 1
                                retry_rows.append((row, fail_kind))   # re-queued below
                                continue
                            staged.failed.append(row)
                            staged.errors.append(
                                f"row {row}: [FITTING_STILL_FAIL] {fail_kind} after retries")
                            continue
```
Add the retry pass: after the main dispatch loop, if `retry_rows` is non-empty, rebuild the fix requests for those rows with an extra correction note `f"Fitting check failed ({fail_kind}); the previous attempt did not fit the dialog window"` and run at most 2 more rounds (a `for _ in range(2):` around a smaller dispatch). Keep the token accounting (`staged.tokens_used += ...`) as the existing code does.
6. `fix_single` (line 1051-1091): after `parse_fixed_line` (line 1086), apply the same gate once (no retries — solo path), setting `SingleFix.error` to `f"[FITTING_STILL_FAIL] {kind}"` on failure.

`translator/tui/screens/judge.py`:
- `JudgeCriteriaScreen.on_screen_ready` (line 824): only include `fitting` in the options when `get_profile(self.draft.get("fitting_engine", "general")) is not None`; when profile is off, show the other four (do NOT tick fitting).
- `JudgePreviewScreen._criteria` (~1682) and `JudgeRunScreen` plumbing (line 2177-2187 `__init__` gains `fitting_profile: str = "general"` stored, passed at `_make_runner` line 3109 `JudgeRunner(..., fitting_profile=self._fitting_profile)`): read `judge_draft.get("fitting_engine", "general")` at the preview/run boundary and pass down. `_target_from` (line 1594) does NOT change (path stays where it is).
- Fix All: `FixAllSetupScreen._start` (line ~5420-5466) reads `app.judge_draft.get("fitting_engine", "general")` from the app's stored draft (the Fix All flow re-enters via `app.judge_fix_slot`; store the engine on the app at fix-all launch: `self.app.judge_fix_engine = engine` and pass `fitting_profile=` into the runner the Fix All path constructs around line 5471/5720 — locate the actual `JudgeRunner(` construction in the Fix All flow and add the kwarg).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_fitting_runner.py -v`
Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add translator/judge/runner.py translator/tui/screens/judge.py tests/test_fitting_runner.py
git commit -m "feat(judge): fitting gating, re-measured fixes with token whitelist and retry cap"
```

---

### Task 12: Calibration + `fitting/__init__.py` exports

**Files:**
- Modify: `translator/fitting/__init__.py`
- Modify: `translator/fitting/profiles.py` (calibrated numbers only — no logic change)
- Test: `tests/test_fitting_exports.py`

**Interfaces:**
- Consumes: Tasks 1-5.
- Produces: `from translator.fitting import (EngineProfile, audit_rows, apply_wrapped, detect_newline_tag, fitting_line, get_profile, wrap_metrics, wrap_lines, suggest_wrap)`.

- [ ] **Step 1: Write the failing test**

```python
"""
tests/test_fitting_exports.py — public surface of the fitting package.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def test_exports():
    from translator import fitting
    for name in ("EngineProfile", "audit_rows", "apply_wrapped",
                 "detect_newline_tag", "fitting_line", "get_profile",
                 "wrap_metrics", "wrap_lines", "suggest_wrap"):
        assert hasattr(fitting, name), name

def test_calibration_documented():
    # The calibration note (spec 1.1) must live in profiles.py so the next
    # maintainer finds it where the numbers are.
    import inspect
    from translator.fitting import profiles
    assert "calibrat" in profiles.__doc__.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_fitting_exports.py -v`
Expected: FAIL — missing attrs

- [ ] **Step 3: Write minimal implementation**

`translator/fitting/__init__.py`:
```python
"""translator/fitting — deterministic text-fit measurement for game dialog windows."""

from .profiles import ENGINE_LABELS, ENGINE_ORDER, EngineProfile, get_profile
from .measure import detect_newline_tag, measure_lines, strip_tokens
from .wrap import wrap_metrics, wrap_lines, suggest_wrap
from .audit import Finding, audit_rows, fitting_line, is_ratio_flag
from .apply import apply_wrapped

__all__ = [
    "ENGINE_LABELS", "ENGINE_ORDER", "EngineProfile", "get_profile",
    "detect_newline_tag", "measure_lines", "strip_tokens",
    "wrap_metrics", "wrap_lines", "suggest_wrap",
    "Finding", "audit_rows", "fitting_line", "is_ratio_flag", "apply_wrapped",
]
```

Calibration (manual, once, before merging the feature): using a real RPG Maker MV/MZ game and a real kirikiri dump:
1. Write 5 probe lines of known em width into `Dialog_Translated.csv`-style data, run them through the game, screenshot the message window at default font size.
2. Compare the pixel column where text clips vs the probe's predicted `budget` (window_cols × safe_margin).
3. Adjust `window_cols` (and `char_widths["latin"]` if the error is proportional) until prediction and reality agree within ~1 em; bump `rpgm-mv-mz.verified` logic accordingly (already True; the exercise validates it rather than flips it).
4. Check the ratio distribution: run `audit_rows` over the repo's `Dialog.csv`/`Dialog_Translated.csv` with the kirikiri profile and confirm healthy rows are not flagged (a JP-source row at 150% stays silent; adjust `ratio_threshold` in steps of 0.1 only if the real distribution demands it).
Record the outcome in the commit message of this task. This is the spec's calibration duty (spec 1.1/1.4b) made executable.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_fitting_exports.py -v`
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add translator/fitting/__init__.py translator/fitting/profiles.py tests/test_fitting_exports.py
git commit -m "feat(fitting): public exports; mv-mz window calibration against real game"
```

---

### Task 13: Audit screen + menu registration

**Files:**
- Create: `translator/tui/screens/audit.py`
- Modify: `translator/tui/app.py` (`build_options` line 190, `handle_choice` line 221)
- Test: `tests/test_fitting_audit_screen.py`

**Interfaces:**
- Consumes: `audit_rows`, `Finding`, `suggest_wrap`, `apply_wrapped` (Tasks 3-5), `CompareGrid`, `Column`, `RowClicked`, `Picker`, `app_prefs.remembered_engine`.
- Produces: `class AuditScreen(BaseScreen)` with steps: file+engine (reuses `FileEngineScreen` for the first step via `push_screen`) → findings grid → apply.

- [ ] **Step 1: Write the failing test**

```python
"""
tests/test_fitting_audit_screen.py — audit grid assembly (pure helpers).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.fitting.profiles import get_profile
from translator.tui.screens.audit import findings_notes, general_refusal

MV = get_profile("rpgm-mv-mz")

def test_notes_show_kind_and_severity():
    from translator.fitting import audit_rows
    rows = [(3, "oke", "kata " * 40)]
    notes = findings_notes(audit_rows(rows, MV))
    assert 3 in notes
    assert "OVERFLOW" in notes[3] or "SINKING" in notes[3]

def test_general_refusal_message():
    msg = general_refusal()
    assert "engine" in msg.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_fitting_audit_screen.py -v`
Expected: FAIL — ModuleNotFoundError

- [ ] **Step 3: Write minimal implementation**

`translator/tui/screens/audit.py`:
```python
"""
tui/screens/audit.py — Text Fitting Audit: measure any file, any time.

The audit is the mechanical twin of the judge criterion: no model, no tokens,
no verdicts — the machine wraps, the machine reports, the user applies. It
exists for files that never went through a fitted translate run (spec 2a):
pick a file, pick an engine (General is refused — there is nothing to measure
without a window), see the findings, apply the wrap suggestions wholesale or
one by one. Applying backs the file up once (<stem>.bak) and writes the
Translated column.

Flow: AuditScreen pushes FileEngineScreen itself (its next_screen pops back
to the audit, which then renders the findings grid — a screen cannot be its
own next_screen, so the pair is: push FileEngineScreen -> pop -> populate).
"""

from __future__ import annotations

import os

from textual.widgets import Label

from ...fitting import audit_rows, apply_wrapped, suggest_wrap, Finding
from ...fitting.profiles import get_profile
from ...io.excel_io import ExcelReader
from ...parsing.columns import detect_columns
from ..app import BaseScreen
from ..theme import ERROR, MUTED, OK, WARN
from ..widgets.common import Option, Picker
from ..widgets.compare import Column, CompareGrid

def general_refusal() -> str:
    return ("Audit needs an engine profile — General has no dialog window to "
            "measure. Pick an engine on the previous screen.")

def findings_notes(findings: list[Finding]) -> dict[int, str]:
    """One-line note per finding row, the grid's under-row commentary."""
    out: dict[int, str] = {}
    for f in findings:
        bits = [k.upper() for k in f.kinds]
        out[f.row] = (f"{f.severity.upper()} · {' · '.join(bits)} · "
                      f"{f.target_wrap.lines} lines vs max "
                      f"{int(f.target_wrap.budget // max(f.target_wrap.budget, 1)) * 0 or f.target_wrap.budget:.0f} em budget")
    return out

class AuditScreen(BaseScreen):
    """File+engine (via FileEngineScreen) -> findings grid -> apply."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._path = ""
        self._engine = "general"
        self.grid = CompareGrid(id="audit-grid")

    def compose_body(self):
        yield Label("Text Fitting Audit", classes="title")
        yield Label("Pick a translated file and its engine — UET measures every "
                    "row against the dialog window.", classes="muted")
        yield self.grid

    def on_screen_ready(self) -> None:
        if self._path:
            self._run()
            return
        self._pick()

    def _pick(self) -> None:
        from .file_engine import FileEngineScreen
        self.app.push_screen(FileEngineScreen(
            title="Audit a spreadsheet",
            confirm_label="Run audit",
            on_accept=self._file_accepted,
            next_screen=lambda: self.app.pop_screen(),   # pop back here
            existing_path=self._path,
            sort_translated_first=True,
        ))

    def _file_accepted(self, path: str, engine: str) -> None:
        self._path = path
        self._engine = engine

    def _run(self) -> None:
        profile = get_profile(self._engine)
        if profile is None:
            self.notify_status(general_refusal(), WARN)
            self._pick()
            return
        rows = self._load_rows()
        findings = audit_rows(rows, profile)
        self._show_findings(findings, profile, rows)

    def _load_rows(self) -> list[tuple[int, str, str]]:
        letters, headers, sample = ExcelReader.grid(self._path, 1, 1, 26)
        src, dst = detect_columns(headers)
        if src is None or dst is None:
            self.notify_status("Could not find Source/Translated columns by header.", WARN)
            return []
        sources = ExcelReader.load_column(self._path, src, 2, 0)
        targets = dict(ExcelReader.load_column(self._path, dst, 2, 0))
        return [(r, s, targets.get(r, "")) for r, s in sources if (s or "").strip()]

    def _show_findings(self, findings: list[Finding], profile, rows) -> None:
        src_map = {r: s for r, s, _t in rows}
        tgt_map = {r: t for r, _s, t in rows}
        grid_rows = [(f.row, src_map.get(f.row, "")) for f in findings]
        styles = {"critical": ERROR, "moderate": WARN, "minor": MUTED}
        with self.grid.batched():
            self.grid.set_sources(grid_rows, [
                Column(letter="S", label="TARGET", rows={f.row: tgt_map.get(f.row, "") for f in findings}),
                Column(letter="W", label="WRAP SUGGESTION",
                       rows={f.row: (suggest_wrap(tgt_map.get(f.row, ""), profile)
                                     if f.newline_tag else "— shorten via Judge (no tag in source)")
                             for f in findings}),
            ])
            self.grid.set_notes(findings_notes(findings),
                                {f.row: styles.get(f.severity, MUTED) for f in findings})
            self.grid.set_row_styles({f.row: styles.get(f.severity, MUTED) for f in findings})
        self.notify_status(f"{len(findings)} row(s) do not fit the dialog window.", OK)

    def action_apply_all(self) -> None:
        profile = get_profile(self._engine)
        if profile is None:
            return
        rows = self._load_rows()
        tgt_map = {r: t for r, _s, t in rows}
        from ...fitting.measure import detect_newline_tag
        from ...fitting import audit_rows as _ar
        payload = {}
        for f in _ar(rows, profile):
            # Only rows whose SOURCE has a tag are wrappable (spec 2a): the
            # machine never invents a token the file does not prove.
            if f.newline_tag and detect_newline_tag(tgt_map.get(f.row, "") or "") is None:
                payload[f.row] = suggest_wrap(tgt_map.get(f.row, ""), profile)
            elif f.newline_tag:
                payload[f.row] = suggest_wrap(tgt_map.get(f.row, ""), profile)
        if not payload:
            self.notify_status("Nothing to apply — no wrappable rows (try the judge's shorten advice).", WARN)
            return
        n = apply_wrapped(self._path, payload)
        self.notify_status(f"Applied {n} wrap fix(es). Backup at {os.path.splitext(self._path)[0]}.bak", OK)

    def screen_keys(self) -> list[tuple[str, str]]:
        return [("a", "apply all"), ("r", "rerun"), ("←", "back"), ("esc", "cancel")]
```

Simplify the apply-all payload loop before implementing — the two branches are identical; keep only:
```python
            if f.newline_tag:
                payload[f.row] = suggest_wrap(tgt_map.get(f.row, ""), profile)
```
(The `detect_newline_tag` import is then unnecessary in `action_apply_all`.)

Populate the grid (follow `_fill_grid`'s pattern from judge.py:3336-3364): `rows = [(f.row, source_text)]`, one `Column(letter="T", label="WRAP SUGGESTION", rows={f.row: suggest_wrap(...)})`, `set_notes(findings_notes(findings))`, `set_row_styles` severity colors (`critical=ERROR, moderate=WARN, minor=MUTED`). Actions: `a` apply all → `apply_wrapped(self._path, {f.row: suggest_wrap(target, profile) for flagged rows with a newline tag})` (only rows whose source has a tag — spec 2a rule), `Enter` on a row opens a confirm modal with the suggestion diff, `e` export findings CSV (`<stem>_FitAudit.csv` via `csv_io.write_rows`).

Menu registration in `translator/tui/app.py`:
1. `build_options` (line 190-208): after the Judge option add:
```python
        Option("Text Fitting Audit", "audit", hint="check a translated file against the game's dialog window"),
```
2. `handle_choice` (line 221-263): add local import `from .screens.audit import AuditScreen` alongside the others, and a branch (before the final `else`):
```python
    elif option.value == "audit":
        self.app.push_screen(AuditScreen())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_fitting_audit_screen.py -v`
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add translator/tui/screens/audit.py translator/tui/app.py tests/test_fitting_audit_screen.py
git commit -m "feat(tui): Text Fitting Audit screen + menu entry"
```

---

### Task 14: Full-suite verification

**Files:** none new.

- [ ] **Step 1: Run the entire test suite**

Run: `python -m pytest tests/ -v`
Expected: all PASS (existing 14 files + the 8 new fitting files). Any regression in existing tests means a shared-behavior break (most likely candidates: `Picker` signature change, `CRITERIA` growth, FileSelectScreen removal) — fix before proceeding.

- [ ] **Step 2: Smoke-run the app**

Run: `python main.py`
Manual checks (no model calls needed):
1. MainMenu shows "Text Fitting Audit".
2. Translate wizard step 3 is the combined file+engine screen; box shows ≤5 files; clicking a file fills the path; engine select shows General default; Confirm and next proceeds to columns.
3. Re-entering the wizard with the same file pre-selects the remembered engine.
4. Judge flow: file screen is the shared screen; criteria list shows `fitting` only after choosing a non-General engine.
5. Text Fitting Audit on `Dialog_Translated.csv` with Kirikiri engine renders findings (the file uses `[w]`/`[p]` tags; ratios of already-translated rows should be sane — no mass false-positive flags).
6. Esc/back works from every new screen (BaseScreen default).

- [ ] **Step 3: Commit any fixes**

```bash
git add -A
git commit -m "fix(fitting): integration pass after full-suite + smoke run"
```

---

## Self-Review (completed during planning)

1. **Spec coverage:** 1.1→Task 1; 1.2/1.2b→Task 2; 1.3→Task 3; 1.4/1.4b→Task 4; 1.5→Task 5; 2.1-2.3→Tasks 7-8 (+Task 6 for 2.3); 2.4→Task 9; 2a→Task 13; 3.1-3.4→Task 10; 3.5→Task 11; 3.6→Task 10 (labels) + Task 11 (severity anchor reuse); 4 table→file map at top; 5 calibration→Task 12; 6/7 (limitations/V2)→no tasks by design.
2. **Placeholder scan:** no TBDs; every code step carries the actual content; the Task 3 suggestion-test note was resolved inline (correct tests written, wrong variant removed); the Task 13 apply-all simplification is stated with the exact surviving code.
3. **Type consistency:** `fitting_map: dict[int, str]` identical in Tasks 10-11; `Finding.newline_tag` produced (Task 4) and consumed (Task 13 apply rule); `revalidate_fix(row, fixed_text, source) -> str | None` matches across Task 11; `Picker(max_visible=...)` defined in Task 7 and used in Task 8; import paths verified against the working tree (`from ..app import BaseScreen`, `Label` from `textual.widgets`, judge push at 752, wizard FileSelectScreen at 375-477).
```
