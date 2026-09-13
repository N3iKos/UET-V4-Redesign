# Priority Failover & Model Transparency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement automatic per-worker cascading model failover (Priority Failover) when a batch fails after a configurable retry threshold, and update the Workers panel UI to display model names rather than generic connection names.

**Architecture:** 
1. Store priority ranking on `ModelEntry` data models with uniqueness validation in the Providers & Models UI.
2. Add global settings in `app_prefs` and the Settings screen for enabling/disabling failover and setting the retry threshold before failover.
3. Enhance `RetryExecutor` and `Scheduler` to cascade to the highest-priority model on batch failure while keeping other workers isolated.
4. Update `worker_line()` in `activity.py` to render concise model names and show `[P1]`, `[P2]` indicators on failover.

**Tech Stack:** Python 3.10+, Textual TUI, Pytest

**Spec:** `Docs/2026-09-02-priority-failover-design.md`

## Global Constraints
- Python 3.10+ compatibility.
- Zero breaking changes to existing translation or judge flows when Priority Failover is OFF or no models are ranked.
- All existing tests in `pytest` suite must continue to pass.

---

### Task 1: Model Priority Rank Data Model & Validation

**Files:**
- Modify: `translator/providers/models.py:40-100`
- Modify: `translator/tui/screens/providers.py:860-950`
- Test: `tests/test_model_priority_rank.py`

**Interfaces:**
- `ModelEntry.priority_rank: int` (default 0)
- `validate_priority_rank(registry, candidate_rank, current_model_id) -> str | None`

- [ ] **Step 1: Write failing tests for ModelEntry priority rank and uniqueness validation**

Create `tests/test_model_priority_rank.py`:
```python
from translator.providers.models import ModelEntry
from translator.providers.registry import Registry

def test_model_entry_default_priority():
    model = ModelEntry(id="m1", name="Model 1")
    assert getattr(model, "priority_rank", 0) == 0

def test_priority_rank_uniqueness():
    model1 = ModelEntry(id="m1", name="M1", priority_rank=1)
    model2 = ModelEntry(id="m2", name="M2", priority_rank=1)
    # Validation helper must detect duplicate rank > 0
```

- [ ] **Step 2: Run test to verify failure**
Run: `pytest tests/test_model_priority_rank.py -v`
Expected: FAIL (priority_rank field or validation helper missing)

- [ ] **Step 3: Implement `priority_rank` in `ModelEntry` and validation in `providers.py`**
Add `priority_rank: int = 0` to `ModelEntry` dataclass.
In `providers.py`, add `Priority Rank` input to `ModelFormModal`, with duplicate checking across all providers/connections in `registry`.

- [ ] **Step 4: Run test to verify it passes**
Run: `pytest tests/test_model_priority_rank.py -v`
Expected: PASS

- [ ] **Step 5: Commit**
```bash
git add translator/providers/models.py translator/tui/screens/providers.py tests/test_model_priority_rank.py
git commit -m "feat(providers): add priority_rank to ModelEntry with unique validation"
```

---

### Task 2: Priority Failover Settings in App Prefs & Settings UI

**Files:**
- Modify: `translator/data/app_prefs.py`
- Modify: `translator/tui/screens/settings.py`
- Test: `tests/test_failover_prefs.py`

**Interfaces:**
- `app_prefs.priority_failover_enabled() -> bool`
- `app_prefs.set_priority_failover_enabled(bool) -> bool`
- `app_prefs.failover_retry_threshold() -> int`
- `app_prefs.set_failover_retry_threshold(int) -> int`

- [ ] **Step 1: Write failing tests for priority failover settings**

Create `tests/test_failover_prefs.py`:
```python
from translator.data import app_prefs

def test_failover_defaults():
    assert app_prefs.priority_failover_enabled() is True
    assert app_prefs.failover_retry_threshold() == 3

def test_failover_setters_and_clamping():
    app_prefs.set_priority_failover_enabled(False)
    assert app_prefs.priority_failover_enabled() is False
    app_prefs.set_failover_retry_threshold(10)
    assert app_prefs.failover_retry_threshold() == 5  # clamped 1..5
```

- [ ] **Step 2: Run test to verify failure**
Run: `pytest tests/test_failover_prefs.py -v`
Expected: FAIL

- [ ] **Step 3: Implement settings in `app_prefs.py` and UI in `settings.py`**
Add defaults, accessors, setters, and clamp functions in `app_prefs.py`.
Add `Priority failover` and `Failover after retries` options and modals to `SettingsScreen`.

- [ ] **Step 4: Run test to verify it passes**
Run: `pytest tests/test_failover_prefs.py -v`
Expected: PASS

- [ ] **Step 5: Commit**
```bash
git add translator/data/app_prefs.py translator/tui/screens/settings.py tests/test_failover_prefs.py
git commit -m "feat(settings): add priority failover toggles and threshold configuration"
```

---

### Task 3: Priority Failover Logic in Retry & Scheduler

**Files:**
- Modify: `translator/scheduling/retry.py`
- Modify: `translator/scheduling/scheduler.py`
- Test: `tests/test_priority_failover_execution.py`

**Interfaces:**
- `RetryExecutor.execute(..., failover_ladder=[...])`
- `Scheduler` tracks active model per worker in `WorkerStatus`

- [ ] **Step 1: Write failing tests for failover execution**

Create `tests/test_priority_failover_execution.py`:
```python
def test_retry_failover_cascades_to_priority_model():
    # Primary model fails 3 times -> cascades to Priority 1 model -> succeeds
```

- [ ] **Step 2: Run test to verify failure**
Run: `pytest tests/test_priority_failover_execution.py -v`
Expected: FAIL

- [ ] **Step 3: Implement failover cascading in `RetryExecutor` and `Scheduler`**
Collect all available priority models sorted by `priority_rank` ascending.
When primary model exhausts `failover_retry_threshold`, switch lease to Priority #1, emit failover event, and retry.
Ensure worker isolation: only the failing task switches target model.

- [ ] **Step 4: Run test to verify it passes**
Run: `pytest tests/test_priority_failover_execution.py -v`
Expected: PASS

- [ ] **Step 5: Commit**
```bash
git add translator/scheduling/retry.py translator/scheduling/scheduler.py tests/test_priority_failover_execution.py
git commit -m "feat(scheduler): implement per-worker priority failover ladder"
```

---

### Task 4: UI Transparency — Model Name in Workers Panel

**Files:**
- Modify: `translator/tui/widgets/activity.py`
- Modify: `translator/tui/screens/run.py`
- Modify: `translator/tui/screens/judge.py`
- Test: `tests/test_worker_line_model_display.py`

**Interfaces:**
- `WorkerStatus.model_name: str`
- `WorkerStatus.failover_rank: int` (0 = normal, 1, 2 = failover)
- `worker_line()` renders model name with `[P1]` tag if failover active

- [ ] **Step 1: Write failing tests for worker_line rendering model name**

Create `tests/test_worker_line_model_display.py`:
```python
from translator.tui.widgets.activity import worker_line
from translator.scheduling.scheduler import WorkerStatus

def test_worker_line_shows_model_name():
    status = WorkerStatus(worker_id=1, model_name="gemma4:31b", batch_num=5)
    line = worker_line(status)
    assert "gemma4:31b" in line.plain

def test_worker_line_shows_failover_indicator():
    status = WorkerStatus(worker_id=2, model_name="deepseek-v4", failover_rank=1, batch_num=5)
    line = worker_line(status)
    assert "[P1]" in line.plain or "P1" in line.plain
```

- [ ] **Step 2: Run test to verify failure**
Run: `pytest tests/test_worker_line_model_display.py -v`
Expected: FAIL

- [ ] **Step 3: Implement model name rendering in `activity.py` and populate `model_name` from runner**
Update `WorkerStatus` to hold `model_name` and `failover_rank`.
Update `worker_line()` and `detail_lines()` in `activity.py`.

- [ ] **Step 4: Run test to verify it passes**
Run: `pytest tests/test_worker_line_model_display.py -v`
Expected: PASS

- [ ] **Step 5: Commit**
```bash
git add translator/tui/widgets/activity.py translator/scheduling/scheduler.py tests/test_worker_line_model_display.py
git commit -m "feat(ui): display model names and failover badges in Workers panel"
```

---

### Task 5: Full Integration & Regression Suite Verification

- [ ] **Step 1: Run complete pytest suite**
Run: `pytest`
Expected: 100% PASS (all existing and new tests)

- [ ] **Step 2: Final cleanup and checkpoint commit**
```bash
git status
```
