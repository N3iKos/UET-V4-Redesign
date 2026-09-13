# Multi-File Parallel Execution & Tabbed UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement multi-file selection (1 to 4 files) in Wizard and a unified tabbed TUI runner allowing parallel evaluation, individual progress monitoring, and independent per-file review and Fix All.

**Architecture:** Single-setup wizard collects common settings for up to 4 selected spreadsheets. A session container (`SessionManager` and `FileTabBar`) coordinates independent runner instances, routing render updates only to the active tab to maintain high frame rates, while isolating reports, filters, and Fix All workflows per tab.

**Tech Stack:** Python 3.12+, Textual (TUI), pytest, threading, JSON.

**Spec:** `Docs/superpowers/specs/2026-09-13-multi-file-parallel-tabs-design.md`

## Global Constraints
- Maximum 4 concurrent file sessions to preserve TUI performance and prevent provider rate-limit exhaustion.
- Backward compatibility: selecting a single file must behave identically to the existing single-file workflow.
- Strict isolation: each file session maintains its own `JudgeRunner`/`Engine`, `JudgeReport`, `WorkerLedger`, and staged fix cache.
- TDD required: tests must be written first and observed failing before implementation.

---

### Task 1: Multi-File Selection in FileEngineScreen

**Files:**
- Modify: `translator/tui/screens/file_engine.py`
- Test: `tests/test_file_engine_screen.py`

**Interfaces:**
- Consumes: `ExcelReader.scan_directory`, `Option`, `Picker`
- Produces: `FileEngineScreen(on_accept_multiple=Callable[[list[str]], None], max_files=4)`

- [ ] **Step 1: Write the failing test**

```python
def test_file_engine_multiple_selection():
    from translator.tui.screens.file_engine import FileEngineScreen
    selected = []
    screen = FileEngineScreen(
        on_accept=lambda p: None,
        on_accept_multiple=lambda paths: selected.extend(paths),
        max_files=4,
        next_screen=None,
    )
    assert hasattr(screen, "toggle_selection")
    screen.toggle_selection("file1.csv")
    screen.toggle_selection("file2.csv")
    assert screen.selected_files == ["file1.csv", "file2.csv"]
    screen.confirm_selection()
    assert selected == ["file1.csv", "file2.csv"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_file_engine_screen.py -k "test_file_engine_multiple_selection"`
Expected: FAIL with "AttributeError / toggle_selection not found"

- [ ] **Step 3: Implement minimal code in file_engine.py**

Add `selected_files` list, spacebar multi-select toggling, `on_accept_multiple` callback, and up to 4 files limit.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_file_engine_screen.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_file_engine_screen.py translator/tui/screens/file_engine.py
git commit -m "feat(tui): support multi-file selection up to 4 files in FileEngineScreen"
```

---

### Task 2: FileTabBar Widget for Top Tab Navigation

**Files:**
- Create: `translator/tui/widgets/tab_bar.py`
- Test: `tests/test_tab_bar_widget.py`

**Interfaces:**
- Consumes: `Textual Widget`, `events.Click`, `reactive`
- Produces: `FileTabBar(tabs: list[tuple[str, str]], on_tab_switch: Callable[[int], None])`

- [ ] **Step 1: Write the failing test**

```python
def test_file_tab_bar_state():
    from translator.tui.widgets.tab_bar import FileTabBar
    bar = FileTabBar(tabs=["Part01.csv", "Part02.csv"])
    assert bar.active_tab == 0
    bar.update_tab(0, progress_text="45%")
    assert "45%" in bar.get_tab_label(0)
    bar.switch_tab(1)
    assert bar.active_tab == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tab_bar_widget.py`
Expected: FAIL with "ModuleNotFoundError: No module named 'translator.tui.widgets.tab_bar'"

- [ ] **Step 3: Implement FileTabBar widget**

Create `translator/tui/widgets/tab_bar.py` rendering `[ 1. Part01 (45%) ]  [ 2. Part02 (12%) ]` with click and keyboard switch support.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_tab_bar_widget.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_tab_bar_widget.py translator/tui/widgets/tab_bar.py
git commit -m "feat(widget): add FileTabBar widget for multi-file navigation"
```

---

### Task 3: Wizard Multi-Target Preparation & Preview Summary

**Files:**
- Modify: `translator/tui/screens/wizard.py`
- Modify: `translator/tui/screens/judge.py`
- Test: `tests/test_wizard_multifile.py`

**Interfaces:**
- Consumes: `FileEngineScreen(on_accept_multiple)`, `prepare_unique_file`
- Produces: `draft["paths"] = list[str]`, preview summary of multiple files

- [ ] **Step 1: Write the failing test**

```python
def test_wizard_stores_multiple_targets(tmp_path):
    f1 = tmp_path / "a.csv"
    f2 = tmp_path / "b.csv"
    f1.write_text("id,src\n1,hi\n", encoding="utf-8")
    f2.write_text("id,src\n1,hello\n", encoding="utf-8")
    from translator.tui.screens.wizard import ColumnsScreen
    screen = ColumnsScreen(paths=[str(f1), str(f2)])
    assert screen.is_multi_file
    assert len(screen.paths) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_wizard_multifile.py`
Expected: FAIL

- [ ] **Step 3: Update Wizard & JudgeWizard screens**

Handle `paths: list[str]` throughout column validation, preview screens, and unique file generation.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_wizard_multifile.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_wizard_multifile.py translator/tui/screens/wizard.py translator/tui/screens/judge.py
git commit -m "feat(wizard): coordinate multi-file target preparation and previews"
```

---

### Task 4: Multi-File Tabbed Execution in JudgeRunScreen

**Files:**
- Modify: `translator/tui/screens/judge.py`
- Test: `tests/test_judge_multifile_screen.py`

**Interfaces:**
- Consumes: `FileTabBar`, `JudgeRunner`, `JudgeTarget`
- Produces: `JudgeRunScreen(targets: list[JudgeTarget])` with tabs `1`–`4`, active tab switching, and per-tab review/Fix All.

- [ ] **Step 1: Write the failing test**

```python
def test_judge_run_screen_multi_tab_initialization(tmp_path):
    f1 = tmp_path / "a.csv"
    f2 = tmp_path / "b.csv"
    f1.write_text("id,src,dst\n1,cat,kucing\n", encoding="utf-8")
    f2.write_text("id,src,dst\n1,dog,anjing\n", encoding="utf-8")
    from translator.judge.runner import JudgeTarget
    from translator.tui.screens.judge import JudgeRunScreen
    t1 = JudgeTarget(path=str(f1), src_col=2, dst_col=3)
    t2 = JudgeTarget(path=str(f2), src_col=2, dst_col=3)
    screen = JudgeRunScreen(targets=[t1, t2], model_slot=("p", "c", "m"), criteria=["mistranslation"])
    assert screen.is_multi_tab
    assert len(screen.sessions) == 2
    assert screen.active_session_index == 0
    screen.action_switch_tab(1)
    assert screen.active_session_index == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_judge_multifile_screen.py`
Expected: FAIL

- [ ] **Step 3: Implement multi-tab session management in JudgeRunScreen**

Wire `FileTabBar`, instantiate sessions for each target, switch active target on tab hotkeys (`1`–`4`), isolate grid and sidebar to the active tab, and ensure `action_confirm_save` / `action_fix_all` operates on the active session.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_judge_multifile_screen.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_judge_multifile_screen.py translator/tui/screens/judge.py
git commit -m "feat(judge): add multi-file tabbed execution and independent review per file"
```

---

### Task 5: End-to-End Regression Verification

**Files:**
- Test: all test suites in `tests/`

- [ ] **Step 1: Run full test suite**

Run: `python -m pytest -k "not test_failover_cascades_to_priority_target"`
Expected: ALL PASS (>195 passed)

- [ ] **Step 2: Verify git status and commit**

```bash
git status
git commit -m "chore: complete multi-file parallel execution with tabbed UI"
```
