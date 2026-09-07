# Design Spec: Unique Working File Deduplication & Translation Memory Architecture

## Overview

When translating large game scripts (e.g. RPG Maker or visual novel event dumps), files often contain hundreds of thousands of rows with massive repetition (e.g. 124,759 rows containing only 7,749 unique texts — a 93.8% redundancy rate). 

Attempting to perform in-memory deduplication while keeping the entire 124k-row file in the active processing pipeline causes severe systemic issues:
1. **Misleading Progress Indicators**: The UI displays confusing progress ratios (e.g. `3,015 / 124,759 (2%)` when the job is actually 39% done), because the numerator only tracks unique model replies while the denominator reflects the full spreadsheet.
2. **Review Grid Clutter**: In the Judge screen, identical duplicate lines flood the grid (e.g. 50+ consecutive identical lines of `\c[7]Enki\c[0]`), drowning out unique dialog and making manual review unreadable.
3. **Retry & Unfinished Storms**: The Judge evaluation pipeline interprets un-sent duplicate rows as "unreviewed/abandoned" rows, triggering runaway retry attempts on tens of thousands of rows (`Retrying 113,252 unfinished rows...`).

This specification replaces in-memory sibling patching with the **Unique Working File Architecture** (the standard approach in professional CAT and game localization tools such as Poedit/gettext, MemoQ, and Translator++):
- A lightweight scanner extracts unique source rows into a dedicated working file in a local cache directory (`.uet_cache/`).
- The entire UET engine (Translate, Judge, Fix All, Re-judge) operates on the clean unique file (e.g. 7,749 rows).
- All progress bars, schedulers, retry loops, and review grids function naturally, transparently, and 100% accurately.
- A high-performance synchronization layer maps translations from the unique working file back to all sibling occurrences in the full target file (`*_Translated.csv`).

---

## 1. Deduplication Modes in Settings (`app_prefs`)

In `translator/data/app_prefs.py`, the setting `dedup_mode` provides three options:

| Mode | Behavior | Use Case |
|---|---|---|
| `unique_file` *(Default)* | Extracts unique rows into `.uet_cache/<stem>.unique.<ext>`, translates & judges on the unique file, and syncs translations back to the full file. | Recommended for all files with duplicate rows (saves 40%–95% tokens & time). |
| `persistent` | Same as `unique_file`, plus stores and retrieves translation pairs from the local SQLite Translation Memory (`~/.config/excel-translator-v4/tm.db`). | Ideal for multi-file game projects or recurring game patches. |
| `disabled` | Bypasses deduplication. Every row is processed independently as in classic UET. | Diagnostic mode or files where every line must be handled in isolation. |

---

## 2. Architecture & Data Flow

```text
               [Original File: 124,759 rows]
                              │
                              ▼ (File Select & Extraction)
            [.uet_cache/<stem>.map.json]
            [.uet_cache/<stem>.unique.csv] (7,749 rows)
                              │
          ┌───────────────────┴───────────────────┐
          ▼                                       ▼
   [Translate Wizard]                       [Judge Wizard]
  Processes 7,749 rows                    Reviews 7,749 rows
          │                                       │
          ▼                                       ▼
[.uet_cache/<stem>.unique_Translated.csv] ──► [Fix All / Re-Judge]
          │                                       │
          └───────────────────┬───────────────────┘
                              ▼ (Broadcast Sync: ~0.04s)
            [Final Output: 124,759 rows fully translated!]
```

---

## 3. Working File & Mapping Layer (`translator/core/dedup_file.py`)

A new module `translator/core/dedup_file.py` handles the creation, mapping, and synchronization of unique working files:

### 3.1 Mapping Data Structure (`.map.json`)
```json
{
  "version": 1,
  "original_file": "D:/Games/RPG/Dialog_Maps.csv",
  "unique_file": "D:/Games/RPG/.uet_cache/Dialog_Maps.unique.csv",
  "src_col": 5,
  "dst_col": 6,
  "start_row": 2,
  "total_rows": 124759,
  "unique_rows": 7744,
  "rep_to_full": {
    "2": [2, 14, 25, 102],
    "3": [3, 88, 120]
  }
}
```

### 3.2 Functions in `dedup_file.py`
1. `prepare_unique_file(src_path: str, src_col: int, dst_col: int, start_row: int = 2) -> tuple[str, str, int, int]`:
   - Scans the source column of `src_path`.
   - Identifies unique non-empty source strings and groups all matching sheet row indices.
   - If `unique_count < total_count`:
     - Creates directory `.uet_cache/` beside `src_path` (with automatic fallback to `~/.config/excel-translator-v4/cache/` if the directory is read-only).
     - Writes `<stem>.unique.<ext>` containing only header + representative unique rows.
     - Writes `<stem>.map.json`.
     - Returns `(unique_file_path, map_file_path, total_rows, unique_rows)`.
   - If `unique_count == total_count` (no duplicates):
     - Returns `(src_path, "", total_rows, total_rows)`.

2. `sync_unique_to_full(map_file_path: str, unique_out_path: str, full_out_path: str, dst_col: int) -> int`:
   - Reads the unique translations from `unique_out_path`.
   - Loads the map from `map_file_path`.
   - Copies each unique translation to all its sibling row indices in `full_out_path`.
   - Performs atomic file write via `excel_io`.
   - Returns the total number of cells updated in the full file.

---

## 4. Wizard & Screen Integration

### 4.1 File Selection (`translator/tui/screens/file_engine.py`)
- When a file is chosen, the file engine screen detects whether deduplication is enabled in `app_prefs`.
- If enabled, it runs `prepare_unique_file`.
- When duplicates exist:
  - Sets `draft["src_path"] = unique_file_path`.
  - Sets `draft["out_path"] = unique_out_path`.
  - Sets `draft["full_src_path"] = original_file_path`.
  - Sets `draft["full_out_path"] = original_out_path`.
  - Sets `draft["map_path"] = map_file_path`.
  - Sets `draft["num_rows"] = unique_rows`.
  - Displays a clean status chip: `Deduplication: 7,749 unique rows (117,010 duplicates skipped — 93.8% savings)`.

### 4.2 Translation Pipeline (`translator/core/engine.py`)
- `ProcessingEngine` translates the unique file (7,749 rows).
- The scheduler, pacing, and retry logic operate on 7,749 rows.
- Progress bar in `RunScreen` displays `X / 7,749 rows (Y%)` with zero discrepancies.
- On batch completion and on job finish, `sync_unique_to_full` automatically syncs the newly translated lines to `draft["full_out_path"]`.
- The final output file `Dialog_Maps_Translated.csv` remains 100% complete with all 124,759 rows populated.

### 4.3 Judge & Review Pipeline (`translator/judge/runner.py` & `translator/tui/screens/judge.py`)
- When starting Judge on a translated file:
  - If `.uet_cache/<stem>.unique_Translated.<ext>` exists, Judge automatically selects the unique file!
  - The review grid displays **only 7,749 rows**.
  - No duplicate spam in the grid (e.g. 50 identical lines are collapsed into 1 single row to inspect).
  - When the user runs Fix All or manual row edit, approving the fix saves it to the unique file AND immediately calls `sync_unique_to_full` to update all duplicate rows in `full_out_path`.
  - Re-judging and retrying work cleanly on the unique rows, preventing the `113,252 unfinished rows` retry storm.

---

## 5. Removal of Legacy In-Memory Hacks

All previous in-memory duplicate mapping hacks are cleanly removed:
1. Remove `_sibling_map` and duplicate row inflation from `translator/core/engine.py`.
2. Remove `_judge_siblings` and duplicate issue expansion from `translator/judge/runner.py`.
3. Remove dual-progress calculation logic (`_done_unique_rows` vs `_done_rows`) from `translator/tui/screens/run.py` and `judge.py`.
4. Progress bars return to the standard, bug-free formula: `done / total * 100` where `total = len(plan.rows)` of the active file.

---

## 6. Testing & Verification Plan

1. **Unit Tests (`tests/test_dedup_file.py`)**:
   - Test `prepare_unique_file` on files with duplicates: correct row extraction, map JSON formatting, correct unique row count.
   - Test `prepare_unique_file` on files with no duplicates: returns original file directly.
   - Test `sync_unique_to_full`: updates all sibling rows in target CSV/XLSX without altering other columns.
2. **Integration Tests**:
   - Verify `ProcessingEngine` translation run with unique file syncs all 124k rows to the final output file.
   - Verify `JudgeRunner` evaluates unique rows and that fixes broadcast to the full file.
3. **Full Regression Suite**:
   - Run `python -m pytest tests/` across all test files to ensure 100% pass rate.
