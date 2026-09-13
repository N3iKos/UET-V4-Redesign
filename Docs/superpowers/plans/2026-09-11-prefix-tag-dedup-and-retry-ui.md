# Prefix Tag Deduplication & Worker Retry UI Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow deduplication to group rows with identical text even when prefixed by line ID tags (e.g. `\TRN[154]`), safely reattaching each row's unique tag during sync, and ensure the worker status UI transitions properly to active processing during retries instead of being stuck on `! retry`.

**Architecture:** 
1. `dedup_file.py`: Parse prefix tags (`\TRN[\d+]`, etc.) into `(tag, clean_text)`. Deduplicate based on `clean_text`, record individual row tag mapping in `map.json`, and reattach original tags in `sync_unique_to_full`.
2. `scheduler.py` & `activity.py`: Prevent `in_recovery` from overriding active `generating` / `think` phases so that in-flight LLM calls display live progress and reset the timer properly.

**Tech Stack:** Python 3.13, openpyxl, textual / rich, pytest.

---

### Task 1: Prefix Tag Deduplication & Reattachment in `dedup_file.py`

**Files:**
- Modify: `translator/core/dedup_file.py`
- Test: `tests/test_dedup_prefix_tags.py`

**Interfaces:**
- `_extract_prefix_tag(text: str) -> tuple[str, str]`: Returns `(prefix_tag, clean_text)`. E.g. `'\\TRN[154]壊れていて'` $\rightarrow$ `('\\TRN[154]', '壊れていて')`.
- `prepare_unique_file(...)`: Groups rows by `clean_text`, records `row_tags: dict[str, str]` in `map.json`.
- `sync_unique_to_full(...)`: Reattaches `row_tags[str(row)]` to the translated clean text when writing back.

- [ ] **Step 1: Write the failing tests for tag-aware deduplication**

Create `tests/test_dedup_prefix_tags.py`:
```python
import json
from pathlib import Path
import pytest
from openpyxl import Workbook
from translator.core import dedup_file
from translator.io.excel_io import load_workbook

def make_wb(path, rows):
    wb = Workbook()
    for row in rows:
        wb.active.append(row)
    wb.save(path)
    wb.close()
    return str(path)

def read_values(path):
    wb = load_workbook(str(path), data_only=False)
    try:
        return list(wb.active.iter_rows(values_only=True))
    finally:
        wb.close()

def test_trn_prefix_dedup_and_sync_reattaches_id(tmp_path):
    source = make_wb(tmp_path / "game.xlsx", [
        ["source", "target"],
        [r"\TRN[154]壊れていて入れない", ""],
        [r"\TRN[155]壊れていて入れない", ""],
        [r"\TRN[156]もうっ、遅いよ！", ""],
    ])
    unique, mapping, total, count = dedup_file.prepare_unique_file(source, 1, 2, start_row=2)
    # Total rows = 3, Unique rows = 2 (rows 154 and 155 grouped together)
    assert total == 3
    assert count == 2

    # Simulate translation on unique file
    wb = load_workbook(unique, data_only=False)
    # Representative 1: translated
    wb.active.cell(2, 2, r"\TRN[154]Sudah rusak, tidak bisa masuk")
    # Representative 2: translated
    wb.active.cell(3, 2, r"\TRN[156]Ih, lama banget!")
    wb.save(unique)
    wb.close()

    full_out = str(tmp_path / "game_Translated.xlsx")
    dedup_file.sync_unique_to_full(mapping, unique, full_out, 2)

    rows = read_values(full_out)
    assert rows[1][1] == r"\TRN[154]Sudah rusak, tidak bisa masuk"
    assert rows[2][1] == r"\TRN[155]Sudah rusak, tidak bisa masuk"  # Tag 155 preserved!
    assert rows[3][1] == r"\TRN[156]Ih, lama banget!"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_dedup_prefix_tags.py`
Expected: FAIL (total != 3 or count != 2, tag 155 not preserved)

- [ ] **Step 3: Implement prefix tag extraction and reattachment in `dedup_file.py`**

In `translator/core/dedup_file.py`:
- Add `_extract_prefix_tag(text: str) -> tuple[str, str]`:
  ```python
  import re
  _PREFIX_TAG_RE = re.compile(r"^(\\TRN\[\d+\])(.*)$", re.DOTALL)

  def _extract_prefix_tag(text: str) -> tuple[str, str]:
      match = _PREFIX_TAG_RE.match(text)
      if match:
          return match.group(1), match.group(2)
      return "", text
  ```
- In `prepare_unique_file`:
  Extract clean text for grouping, save `row_tags` in mapping:
  ```python
  row_tags = {}
  for row in range(start_row, last + 1):
      val = str(sheet.cell(row=row, column=src_col).value or "").strip()
      tag, clean = _extract_prefix_tag(val)
      row_tags[str(row)] = tag
      # group by clean text if clean text not empty, else by val
  ```
- In `sync_unique_to_full`:
  Reattach the row's specific prefix tag if the translated text has a stripped tag or matching prefix tag.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_dedup_prefix_tags.py tests/test_dedup_file.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add translator/core/dedup_file.py tests/test_dedup_prefix_tags.py
git commit -m "feat: support prefix tag preservation during deduplication and sync"
```

---

### Task 2: Worker Status UI & Retry State Transition

**Files:**
- Modify: `translator/scheduling/scheduler.py:925-945`
- Test: `tests/test_format_retry_phase.py`

**Interfaces:**
- In `scheduler.py`: when `RetryExecutor` or recovery executes an in-flight LLM call (`on_phase`), set `phase=phase` (e.g. `generating` or `think`) with `started_at=time.time()` so the worker is shown actively sending/thinking rather than stuck at static `retry`.

- [ ] **Step 1: Write test verifying phase transition during retry execution**

Update or check `tests/test_format_retry_phase.py` to assert that when a retry request is in flight, `status.phase` updates to `"generating"` / `"think"`.

- [ ] **Step 2: Implement smooth phase transition in `scheduler.py`**

In `translator/scheduling/scheduler.py`:
Update `on_phase`:
```python
        def on_phase(phase: str, reasoning_active: bool) -> None:
            self._set_status(
                worker_id,
                phase=phase,
                reasoning_active=reasoning_active,
                started_at=time.time(),
            )
```
Ensure that `phase="retry"` is shown on error / cooldown / retry events, but immediately transitions to `generating` (`sending`) as soon as the retry HTTP request begins.

- [ ] **Step 3: Run test to verify it passes**

Run: `python -m pytest tests/test_format_retry_phase.py tests/test_judge_retry.py`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add translator/scheduling/scheduler.py
git commit -m "fix: restore active sending status during worker retry request"
```
