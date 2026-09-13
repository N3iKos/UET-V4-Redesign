# Design Spec: Multi-File Parallel Execution & Tabbed UI (Up to 4 Files)

## Overview

In visual novel, RPG Maker, and game localization workflows, game scripts are commonly distributed as multiple split scene/chapter files (e.g., `Forestia_part01.csv` through `Forestia_part04.csv`). Currently, users must either merge them into an unwieldy giant file (risking long monolithic runs) or open multiple terminal windows concurrently to translate/judge each file with identical settings.

This specification introduces **Multi-File Parallel Execution with Tabbed TUI** (supporting 1 to 4 files):
1. **Single Setup in Wizard**: The user selects 1 to 4 files in the file selector. Column assignments, translation/judge model, prompts, and processing options are configured once and applied to all selected files.
2. **Unified Tabbed UI**: During Translate or Judge execution, a top Tab Bar displays all active files (e.g. `[1] Part01 (45%) | [2] Part02 (12%)`). Users switch between tabs using numeric shortcuts (`1`–`4` or `Ctrl+1`–`Ctrl+4`).
3. **Lazy Render Optimization**: Only the currently active tab renders its grid, live feed, and worker activity lines. Inactive tabs run their background worker threads without burdening the TUI rendering loop.
4. **Independent Post-Evaluation State (Judge Mode)**: Once evaluation finishes, each file maintains its own independent report, filter chips, flagged rows list, and Fix All / Rejudge lifecycle. Saving (`Confirm & Save`) only writes to the active file.

---

## 1. User Interaction & Workflow

```text
┌────────────────────────────────────────────────────────────────────────┐
│ Wizard: FileEngineScreen                                               │
│ Select up to 4 files: [X] part01.csv  [X] part02.csv  [X] part03.csv   │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │ (Configure 1x: Columns, Model, Prompt)
                                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│ TUI: Multi-Tab Runner                                                  │
│ Tab Bar: [ 1. part01.csv (65%) ]  [ 2. part02.csv (30%) ]  [ 3. Done ] │
│ Active Tab View: Grid, Progress, Live Worker Feed for File 1           │
│ Switch via '1', '2', '3' or 'Ctrl+1', 'Ctrl+2', 'Ctrl+3'               │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │
                    ┌──────────────┴──────────────┐
                    ▼                             ▼
       [Judge Review: File 1]        [Judge Review: File 2]
       • 350 issues flagged          • 80 issues flagged
       • Fix All with Model A        • Manual row edit ('e')
       • Save to part01.csv          • Save to part02.csv
```

---

## 2. Component Architecture

### 2.1 File Selector (`translator/tui/screens/file_engine.py`)
- Support multi-selection of up to 4 files via spacebar toggle.
- A badge/counter `Selected (N/4)` indicates how many files are checked.
- If only 1 file is selected, behaves identically to classic single-file mode.
- Returns `list[str]` of canonical file paths to the calling wizard.

### 2.2 Wizard Coordination (`translator/tui/screens/wizard.py` & `judge.py`)
- The wizard holds `paths: list[str]`.
- For `unique_file` deduplication, each file generates its own unique file and mapping under `.uet_cache/<stem>.map.json`.
- The preview screen displays a summary table of all selected files with row counts and token estimates.

### 2.3 Tabbed Running Screen (`MultiRunScreen` & `MultiJudgeRunScreen`)
- **Container**: Uses a top bar (`FileTabBar`) rendered above the existing feed and sidebar.
- **Session State (`JobSession`)**:
  - `target: JudgeTarget` / `JobConfig`
  - `runner: JudgeRunner` / `Engine`
  - `ledger: WorkerLedger`
  - `report: JudgeReport | None`
  - `status: str` (`"running" | "paused" | "completed" | "failed"`)
  - `progress: tuple[int, int]` (`done, total`)
- **Keybindings**:
  - `1`, `2`, `3`, `4`: Switch directly to tab 1, 2, 3, or 4.
  - `[` / `]`: Switch to previous / next file tab.
  - `c`: Stop / Save on the currently active file tab.
  - `F` / `f`: Fix All / Fix Row on the currently active file tab.

---

## 3. Concurrency & Rate Limit Safety

- When multiple files run simultaneously, each file borrows connections from the central `ConnectionPool` or uses dedicated connections.
- The default worker count configured in settings (e.g. 10 workers) can either be shared across files or allocated per file based on provider rate limits.
- If a provider reports HTTP 429 (Rate Limit), backoff/cooldown events in the pool safely pause workers across all active files without thrashing.

---

## 4. Error Handling & State Recovery

- **Crash Isolation**: A network failure or unparsed response in File 1 does NOT stop or corrupt File 2, 3, or 4.
- **Autosave Protection**: Each session independently writes staged fixes and checkpoints to `.uet_cache/<stem>_staged_fix.json`.
- **Atomic Commits**: `apply_fix` uses atomic file replacement per file.
