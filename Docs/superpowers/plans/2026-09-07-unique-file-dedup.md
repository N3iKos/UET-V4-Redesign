# Unique Working File Deduplication & Translation Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace fragile in-memory sibling mapping with the standard Unique Working File Architecture: extract unique rows to `.uet_cache/<stem>.unique.<ext>`, run Translate and Judge cleanly on unique rows, and sync translations back to the full file.

**Architecture:** 
- A dedicated leaf module `translator/core/dedup_file.py` handles scanning unique rows, writing `.uet_cache/<stem>.unique.<ext>` + `.map.json`, and high-speed broadcast syncing back to the full file.
- `FileEngineScreen` scans and assigns the unique file as the active job file whenever duplicates exist (saving 40%–95% tokens and time).
- `ProcessingEngine` and `JudgeRunner` operate naturally on the clean unique file; progress bars, grids, and retry loops calculate strictly against actual active rows (no misleading 2% bars, no 113k retry storms).
- Translations and fixes are synchronized into the full target file (`*_Translated.<ext>`) incrementally and on completion.

**Tech Stack:** Python 3.11+ stdlib (`json`, `os`, `shutil`, `re`), existing `translator.io.csv_io` & `excel_io`, Textual TUI, pytest.

**Spec:** `docs/superpowers/specs/2026-09-07-unique-file-dedup-design.md`

## Global Constraints

- No third-party dependencies beyond existing ones (`textual`, `rich`, `openpyxl`, `requests`).
- `dedup_mode` defaults to `"unique_file"`. Other options: `"persistent"` (with SQLite TM), `"disabled"`.
- Never modify the user's original source file.
- All tests bootstrap via `sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))`.

---

### Task 1: `dedup_file.py` — File Scanner & Working File Generator (`prepare_unique_file`)

**Files:**
- Create: `translator/core/dedup_file.py`
- Create: `tests/test_dedup_file.py`

**Interfaces:**
- Produces:
  ```python
  def prepare_unique_file(
      src_path: str, src_col: int, dst_col: int, start_row: int = 2
  ) -> tuple[str, str, int, int]:
      """
      Scans src_path. If duplicates exist, creates .uet_cache/<stem>.unique.<ext>
      and <stem>.map.json. Returns (active_file_path, map_file_path, total_rows, unique_rows).
      If no duplicates exist, returns (src_path, "", total_rows, total_rows).
      """
  ```

- [ ] **Step 1: Write failing tests in `tests/test_dedup_file.py`**
- [ ] **Step 2: Run test to verify it fails (`python -m pytest tests/test_dedup_file.py -v`)**
- [ ] **Step 3: Implement `prepare_unique_file` in `translator/core/dedup_file.py`**
- [ ] **Step 4: Run test to verify it passes (`python -m pytest tests/test_dedup_file.py -v`)**
- [ ] **Step 5: Commit `feat(core): prepare_unique_file scanner and map generator`**

---

### Task 2: `dedup_file.py` — High-Speed Synchronization (`sync_unique_to_full`)

**Files:**
- Modify: `translator/core/dedup_file.py`
- Modify: `tests/test_dedup_file.py`

**Interfaces:**
- Produces:
  ```python
  def sync_unique_to_full(
      map_file_path: str, unique_out_path: str, full_out_path: str, dst_col: int
  ) -> int:
      """
      Copies translations from unique_out_path to all matching rows in full_out_path.
      Returns the number of cells updated.
      """
  ```

- [ ] **Step 1: Write failing tests for `sync_unique_to_full` in `tests/test_dedup_file.py`**
- [ ] **Step 2: Run test to verify failure**
- [ ] **Step 3: Implement `sync_unique_to_full` in `translator/core/dedup_file.py`**
- [ ] **Step 4: Run test to verify it passes**
- [ ] **Step 5: Commit `feat(core): sync_unique_to_full translation broadcaster`**

---

### Task 3: Settings & Preferences — Deduplication Modes (`unique_file`, `persistent`, `disabled`)

**Files:**
- Modify: `translator/data/app_prefs.py`
- Modify: `translator/tui/screens/settings.py`
- Modify: `tests/test_dedup.py`

**Interfaces:**
- `DEDUP_MODES = ("unique_file", "persistent", "disabled")`
- Default is `"unique_file"`.

- [ ] **Step 1: Update tests in `tests/test_dedup.py` for new mode names**
- [ ] **Step 2: Update `app_prefs.py` and `settings.py` (DEDUP_MODES, defaults, hints, modal options)**
- [ ] **Step 3: Verify with pytest (`python -m pytest tests/test_dedup.py tests/test_tm.py -v`)**
- [ ] **Step 4: Commit `feat(prefs): update dedup modes to unique_file (default), persistent, disabled`**

---

### Task 4: File Selection Integration (`FileEngineScreen`)

**Files:**
- Modify: `translator/tui/screens/file_engine.py`
- Modify: `translator/tui/screens/wizard.py`

**Interfaces:**
- When selecting a file, if `dedup_mode != "disabled"`, automatically inspect the file.
- If duplicates exist:
  - Generate unique file & map file.
  - Populate draft with unique file paths while keeping `full_src_path` and `full_out_path`.
  - Display informative notification.

- [ ] **Step 1: Update `FileEngineScreen` and wizard file selection callback**
- [ ] **Step 2: Verify with tests (`python -m pytest tests/test_file_engine_screen.py -v`)**
- [ ] **Step 3: Commit `feat(tui): auto-prepare unique working file on file selection`**

---

### Task 5: Translation Engine Cleanup & Sync Hook

**Files:**
- Modify: `translator/core/models.py` (add `map_path`, `full_src_path`, `full_out_path` to `JobConfig`)
- Modify: `translator/core/engine.py` (remove `_sibling_map`, add `sync_unique_to_full` on finish & flush)
- Modify: `translator/tui/screens/run.py` (restore pure `done / total * 100` progress bar)

- [ ] **Step 1: Clean `core/engine.py` (remove legacy sibling map code, call sync on write/finish)**
- [ ] **Step 2: Clean `tui/screens/run.py` (restore standard progress calculation)**
- [ ] **Step 3: Run existing tests to ensure no regressions**
- [ ] **Step 4: Commit `refactor(engine): clean progress bar and sync unique file to full target`**

---

### Task 6: Judge Pipeline Integration & Grid Cleanup

**Files:**
- Modify: `translator/judge/runner.py` (remove `_judge_siblings`, call `sync_unique_to_full` in `apply_fix`)
- Modify: `translator/tui/screens/judge.py` (clean progress bar, auto-detect unique file for review)

- [ ] **Step 1: Clean `judge/runner.py` (remove `_judge_siblings`, sync on `apply_fix`)**
- [ ] **Step 2: Clean `judge.py` (restore standard progress calculation, no 113k retry storms)**
- [ ] **Step 3: Verify with tests (`python -m pytest tests/test_judge_*.py -v`)**
- [ ] **Step 4: Commit `refactor(judge): review unique file, clean grid, sync fixes to full target`**

---

### Task 7: Full Test Suite Verification

- [ ] **Step 1: Run all test files across the repository**
- [ ] **Step 2: Verify `Dialog_Maps.csv` deduplication runs cleanly in headless check**
- [ ] **Step 3: Final commit & summary**
