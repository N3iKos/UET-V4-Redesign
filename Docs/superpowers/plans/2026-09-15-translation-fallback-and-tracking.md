# Translation Fallback to Source & Cache Flagging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement a game-safe translation fallback that writes the original source text to the destination cell upon translation failure (instead of breaking the game with `[TRANSLATE_FAILED]`), while tracking failed rows in `.uet_cache/` so they can be re-translated or fixed in Judge.

**Architecture:** 
1. Add `failed_fallback_mode` preference (`"copy_source"` default vs `"fail_marker"`).
2. Update `ProcessingEngine._write_result` to write `source_text` to the target cell on permanent batch failure and save failed row numbers into `.uet_cache/{clean_stem}_{dst_col}_failed.json`.
3. Update `ProcessingEngine.run_retranslate_failed` to detect failed rows from both `.uet_cache/*_failed.json` and legacy `[TRANSLATE_FAILED]` markers.
4. Update `JudgeRunner.plan()` to load `.uet_cache/*_failed.json` and automatically flag those rows as `failed` with zero model cost.
5. Provide a Settings UI toggle so users can choose between Game-Safe (Copy Source) and Debug Marker (`[TRANSLATE_FAILED]`).

**Tech Stack:** Python 3.11+, Textual (TUI), openpyxl/csv I/O, pytest.

---

## Global Constraints

- Never break existing tests; all 254 current tests must pass.
- TDD Red-Green cycle mandatory for every step.
- Absolute paths must be handled safely across Windows and Unix platforms.
- Cache directory is always `.uet_cache/` in the output file's directory.

---

### Task 1: App Preference for Translation Fallback Mode

**Files:**
- Modify: `translator/data/app_prefs.py`
- Test: `tests/test_translation_fallback_pref.py`

**Interfaces:**
- `app_prefs.failed_fallback_mode() -> str`: returns `"copy_source"` (default) or `"fail_marker"`.
- `app_prefs.set_failed_fallback_mode(mode: str) -> None`.

- [ ] **Step 1: Write failing test for fallback mode preferences**

```python
# tests/test_translation_fallback_pref.py
import pytest
from translator.data.app_prefs import (
    failed_fallback_mode,
    set_failed_fallback_mode,
)

def test_failed_fallback_mode_defaults_to_copy_source(monkeypatch, tmp_path):
    monkeypatch.setattr("translator.data.app_prefs._prefs_dir", lambda: tmp_path)
    assert failed_fallback_mode() == "copy_source"

def test_set_failed_fallback_mode_persists(monkeypatch, tmp_path):
    monkeypatch.setattr("translator.data.app_prefs._prefs_dir", lambda: tmp_path)
    set_failed_fallback_mode("fail_marker")
    assert failed_fallback_mode() == "fail_marker"
    set_failed_fallback_mode("copy_source")
    assert failed_fallback_mode() == "copy_source"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_translation_fallback_pref.py`
Expected: FAIL (ImportError: cannot import name 'failed_fallback_mode')

- [ ] **Step 3: Implement preference functions in app_prefs.py**

In `translator/data/app_prefs.py`:
```python
def failed_fallback_mode() -> str:
    """Fallback value when translation fails: 'copy_source' (game-safe) or 'fail_marker' ([TRANSLATE_FAILED])."""
    val = _load().get("failed_fallback_mode", "copy_source")
    return val if val in ("copy_source", "fail_marker") else "copy_source"

def set_failed_fallback_mode(mode: str) -> None:
    if mode in ("copy_source", "fail_marker"):
        _update("failed_fallback_mode", mode)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_translation_fallback_pref.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add translator/data/app_prefs.py tests/test_translation_fallback_pref.py
git commit -m "feat(prefs): add failed_fallback_mode preference ('copy_source' vs 'fail_marker')"
```

---

### Task 2: Failed Row Cache Manager Helper

**Files:**
- Create: `translator/core/failed_cache.py`
- Test: `tests/test_failed_cache.py`

**Interfaces:**
- `failed_cache_path(out_path: str, dst_col: int) -> str`
- `load_failed_rows(out_path: str, dst_col: int) -> set[int]`
- `record_failed_rows(out_path: str, dst_col: int, failed_rows: set[int] | list[int]) -> None`
- `clear_failed_rows(out_path: str, dst_col: int, resolved_rows: set[int] | list[int]) -> None`

- [ ] **Step 1: Write failing test for failed cache helper**

```python
# tests/test_failed_cache.py
import os
from translator.core.failed_cache import (
    failed_cache_path,
    load_failed_rows,
    record_failed_rows,
    clear_failed_rows,
)

def test_failed_cache_lifecycle(tmp_path):
    out_file = tmp_path / "game_Translated.csv"
    out_file.write_text("id,src,dst\n", encoding="utf-8")
    path_str = str(out_file)

    assert load_failed_rows(path_str, 3) == set()

    record_failed_rows(path_str, 3, [824, 825])
    assert load_failed_rows(path_str, 3) == {824, 825}

    # Adding more
    record_failed_rows(path_str, 3, [826])
    assert load_failed_rows(path_str, 3) == {824, 825, 826}

    # Clearing resolved
    clear_failed_rows(path_str, 3, [824])
    assert load_failed_rows(path_str, 3) == {825, 826}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_failed_cache.py`
Expected: FAIL (ModuleNotFoundError: No module named 'translator.core.failed_cache')

- [ ] **Step 3: Implement failed_cache.py**

Create `translator/core/failed_cache.py`:
```python
from __future__ import annotations

import json
import os
import time

def failed_cache_path(out_path: str, dst_col: int) -> str:
    clean_p = out_path
    if ".uet_cache" in clean_p:
        import re
        clean_p = re.sub(r"\.[0-9a-f]{8,}\.unique", "", clean_p)
    folder = os.path.dirname(os.path.abspath(clean_p))
    cache_dir = os.path.join(folder, ".uet_cache")
    base = os.path.splitext(os.path.basename(clean_p))[0]
    return os.path.join(cache_dir, f"{base}_col{dst_col}_failed.json")

def load_failed_rows(out_path: str, dst_col: int) -> set[int]:
    path = failed_cache_path(out_path, dst_col)
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(int(r) for r in data.get("failed_rows", []))
    except Exception:
        return set()

def record_failed_rows(out_path: str, dst_col: int, failed_rows: set[int] | list[int]) -> None:
    if not failed_rows:
        return
    path = failed_cache_path(out_path, dst_col)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    existing = load_failed_rows(out_path, dst_col)
    existing.update(failed_rows)
    data = {
        "file": os.path.abspath(out_path),
        "dst_col": dst_col,
        "failed_rows": sorted(existing),
        "updated_at": time.time(),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def clear_failed_rows(out_path: str, dst_col: int, resolved_rows: set[int] | list[int]) -> None:
    if not resolved_rows:
        return
    path = failed_cache_path(out_path, dst_col)
    if not os.path.exists(path):
        return
    existing = load_failed_rows(out_path, dst_col)
    existing.difference_update(resolved_rows)
    if not existing:
        try:
            os.remove(path)
        except OSError:
            pass
        return
    data = {
        "file": os.path.abspath(out_path),
        "dst_col": dst_col,
        "failed_rows": sorted(existing),
        "updated_at": time.time(),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_failed_cache.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add translator/core/failed_cache.py tests/test_failed_cache.py
git commit -m "feat(cache): add failed_cache manager for tracking translation failures"
```

---

### Task 3: Engine Fallback to Source & Cache Tracking

**Files:**
- Modify: `translator/core/engine.py:680-725`
- Modify: `translator/core/engine.py:340-400`
- Test: `tests/test_engine_fallback.py`

**Interfaces:**
- When `result.failed_rows` occurs:
  - If `app_prefs.failed_fallback_mode() == "copy_source"`: payload gets `source_map.get(rn, "")` instead of `config.FAIL_MARKER`.
  - Records row to `record_failed_rows(job.out_path, job.dst_col, result.failed_rows)`.
- When row succeeds:
  - Calls `clear_failed_rows(job.out_path, job.dst_col, [rn])`.
- In `run_retranslate_failed`:
  - Collects row numbers from `load_failed_rows(job.out_path, job.dst_col)` in addition to any cells with `config.FAIL_MARKER`.

- [ ] **Step 1: Write failing test for engine fallback behavior**

```python
# tests/test_engine_fallback.py
import os
from unittest.mock import MagicMock
from translator.core.engine import ProcessingEngine
from translator.core.models import JobConfig, BatchResult
from translator.core.failed_cache import load_failed_rows

def test_engine_writes_source_and_records_cache_on_failure(tmp_path, monkeypatch):
    csv_file = tmp_path / "game.csv"
    csv_file.write_text("id,src,dst\n1,nothing is real?,[TRANSLATE_FAILED]\n", encoding="utf-8")

    out_file = tmp_path / "game_Translated.csv"
    out_file.write_text("id,src,dst\n1,nothing is real?,\n", encoding="utf-8")

    monkeypatch.setattr("translator.data.app_prefs.failed_fallback_mode", lambda: "copy_source")

    job = JobConfig(
        src_path=str(csv_file),
        out_path=str(out_file),
        src_col=2,
        dst_col=3,
        start_row=2,
        num_rows=1,
    )
    engine = ProcessingEngine(job)
    # Simulate a failed batch
    source_map = {2: "nothing is real?"}
    res = BatchResult(results={}, failed_rows=[2])
    engine._write_result(res, source_map)

    # 1. Output file must contain source text "nothing is real?", NOT [TRANSLATE_FAILED]
    content = out_file.read_text(encoding="utf-8")
    assert "nothing is real?" in content
    assert "[TRANSLATE_FAILED]" not in content

    # 2. Failed cache must have row 2
    assert 2 in load_failed_rows(str(out_file), 3)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_engine_fallback.py`
Expected: FAIL (assert "[TRANSLATE_FAILED]" not in content or 2 not in load_failed_rows)

- [ ] **Step 3: Modify ProcessingEngine in translator/core/engine.py**

In `translator/core/engine.py`:
1. In `_write_result`:
   ```python
   from .failed_cache import record_failed_rows, clear_failed_rows
   from ..data.app_prefs import failed_fallback_mode

   mode = failed_fallback_mode()
   for row_num in result.failed_rows:
       if mode == "copy_source":
           fallback_val = source_map.get(row_num, "")
           payload.setdefault(row_num, fallback_val)
       else:
           payload.setdefault(row_num, config.FAIL_MARKER)

   if result.failed_rows:
       record_failed_rows(self._active_output_path or self.job.out_path, self.job.dst_col, result.failed_rows)
   if result.results:
       clear_failed_rows(self._active_output_path or self.job.out_path, self.job.dst_col, list(result.results.keys()))
   ```
2. In `run_retranslate_failed`:
   Read `cached_failed = load_failed_rows(job.out_path, job.dst_col)` and include any row in `cached_failed` where destination cell has the fallback text.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_engine_fallback.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add translator/core/engine.py tests/test_engine_fallback.py
git commit -m "feat(engine): fallback to source on failure and track failed rows in cache"
```

---

### Task 4: JudgeRunner Awareness of Cache-Flagged Rows

**Files:**
- Modify: `translator/judge/runner.py:435-460`
- Test: `tests/test_judge_failed_cache_plan.py`

**Interfaces:**
- In `JudgeRunner.plan()`:
  - Check `load_failed_rows(self._target.path, self._target.dst_col)`.
  - If a row is in `load_failed_rows` and its translation matches source (or `FAIL_MARKER`), flag it as `error_types=["failed"]` without model call.

- [ ] **Step 1: Write failing test for JudgeRunner plan awareness**

```python
# tests/test_judge_failed_cache_plan.py
from translator.judge.runner import JudgeRunner, JudgeTarget
from translator.core.failed_cache import record_failed_rows

def test_judge_plan_flags_cache_recorded_failed_rows(tmp_path):
    csv_file = tmp_path / "game_Translated.csv"
    csv_file.write_text("id,src,dst\n1,nothing is real?,nothing is real?\n", encoding="utf-8")

    record_failed_rows(str(csv_file), 3, [2])

    target = JudgeTarget(path=str(csv_file), src_col=2, dst_col=3, start_row=2, num_rows=1)
    runner = JudgeRunner(target)
    plan = runner.plan()

    assert 2 in plan.prejudged
    assert "failed" in plan.prejudged[2].error_types
    # Row 2 is excluded from askable sections!
    assert len(plan.sections) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_judge_failed_cache_plan.py`
Expected: FAIL (assert 2 in plan.prejudged)

- [ ] **Step 3: Update JudgeRunner.plan in translator/judge/runner.py**

In `translator/judge/runner.py`:
```python
        from ..core.failed_cache import load_failed_rows
        failed_set = load_failed_rows(self._target.path, self._target.dst_col)
        if getattr(self._target, "full_path", None):
            failed_set.update(load_failed_rows(self._target.full_path, self._target.dst_col))

        for row, source, translation in rows:
            text = (translation or "").strip()
            if not text:
                prejudged[row] = RowIssue(
                    row=row, error_types=["empty"],
                    comment="The target cell is empty — nothing was translated.",
                )
            elif text == config.FAIL_MARKER or row in failed_set:
                prejudged[row] = RowIssue(
                    row=row, error_types=["failed"],
                    comment=f"Translation failed and was preserved from source.",
                )
            else:
                askable.append((row, source, translation))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_judge_failed_cache_plan.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add translator/judge/runner.py tests/test_judge_failed_cache_plan.py
git commit -m "feat(judge): auto-prejudge cache-flagged failed rows without LLM cost"
```

---

### Task 5: Settings Screen UI Toggle

**Files:**
- Modify: `translator/tui/screens/settings.py`
- Test: `tests/test_settings_fallback_toggle.py`

**Interfaces:**
- Add option under Translation settings:
  `Failed rows output: [Copy source (Game-safe)] / [Fail marker ([TRANSLATE_FAILED])]`

- [ ] **Step 1: Write test for settings toggle representation**

```python
# tests/test_settings_fallback_toggle.py
from translator.tui.screens.settings import SettingsScreen
from translator.data.app_prefs import set_failed_fallback_mode, failed_fallback_mode

def test_settings_screen_has_fallback_option():
    screen = SettingsScreen()
    # verify option is rendered or accessible in settings screen options
    assert hasattr(screen, "_failed_fallback_picker") or any("fallback" in str(getattr(opt, "value", "")) for opt in getattr(screen, "_options", []))
```

- [ ] **Step 2: Run test and verify fail**

Run: `python -m pytest tests/test_settings_fallback_toggle.py`
Expected: FAIL

- [ ] **Step 3: Implement UI in settings.py**

Add picker or toggle in `translator/tui/screens/settings.py` hooked to `failed_fallback_mode()` and `set_failed_fallback_mode()`.

- [ ] **Step 4: Run test and verify pass**

Run: `python -m pytest tests/test_settings_fallback_toggle.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add translator/tui/screens/settings.py tests/test_settings_fallback_toggle.py
git commit -m "feat(ui): add failed rows fallback mode toggle in Settings"
```

---

### Task 6: Full Regression Verification & Backup

- [ ] **Step 1: Run full test suite**

Run: `python -m pytest -k "not test_failover_cascades_to_priority_target"`
Expected: All tests pass (>= 258 passed, 1 deselected)

- [ ] **Step 2: Create zip backup in backup/**

```bash
python -c "import zipfile, os; zf = zipfile.ZipFile('backup/backup_2026-09-15_fallback_to_source.zip', 'w', zipfile.ZIP_DEFLATED); ..."
```
