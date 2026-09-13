# Design Spec: Priority Failover & Model Transparency

## Overview
Priority Failover introduces an automatic, per-worker model cascading failover mechanism. When a worker fails to complete a batch using the primary selected model (after exceeding a configurable retry threshold), instead of failing the batch or immediately dropping to solo recovery, the batch is automatically re-routed to the highest-priority failover model (Rank #1), then #2, and so on.

Additionally, the Workers panel UI is updated to display concise Model Names instead of generic Connection names for better observability.

---

## 1. Model Priority Ranking (Data & Providers)
- In `ModelEntry` data model (`translator/providers/models.py`):
  - Add `priority_rank: int = 0` (where `0` = None/Normal model, `1, 2, 3...` = Failover Rank).
- In Model Edit / Add Form (`translator/tui/screens/providers.py`):
  - Add input field `Priority Rank (0 = None, 1, 2, 3...)`.
  - **Validation:** Priority numbers > 0 must be unique across all providers/connections. If a number is duplicated, show an error: `"Priority Rank #X is already assigned to <Model Name>"`.
- In `Vault` / Storage:
  - Persist `priority_rank` along with model metadata.

---

## 2. Settings Configuration (Settings Screen & App Prefs)
- In `translator/data/app_prefs.py`:
  - `priority_failover_enabled: bool = True` (default `True`)
  - `failover_retry_threshold: int = 3` (default `3`, clamped `1..5`)
- In `translator/tui/screens/settings.py`:
  - Add `Priority failover` toggle (`On` / `Off`).
  - Add `Failover after retries` input (`1` - `5` retries before switching to next priority model).

---

## 3. Execution & Scheduler Engine (RetryExecutor & Scheduler)
- In `translator/scheduling/retry.py` and `translator/scheduling/scheduler.py`:
  - When a task batch fails on the active model after reaching `failover_retry_threshold`:
    1. Check if `priority_failover_enabled` is active.
    2. Lookup available failover models sorted by `priority_rank ASC` (`1, 2, 3...`).
    3. If available, switch the worker's execution target for that task to Priority #1 model, resetting the attempt counter for that new model.
    4. If Priority #1 also fails $\rightarrow$ cascade to Priority #2, etc.
    5. If all priority models are exhausted $\rightarrow$ fall through to standard batch failure / solo recovery.
  - **Worker Isolation:** The failover only alters the execution of the failing task. Other workers continue with the user-selected primary model.

---

## 4. UI Updates
- **Workers Panel (`translator/tui/widgets/activity.py`):**
  - Update `worker_line()` to render the **Model Name** (shortened/padded) instead of the connection name (e.g. `gemma4:31b` or `deepseek-v4`).
  - When running under a failover model, display an accent indicator (e.g., `[P1]`, `[P2]`).
  - In `detail_lines()`, explicitly show both `model`, `connection`, and `failover_status`.
- **Events Panel:**
  - Log clean, informative transition events:
    `"batch 42 · <Model A> failed (3/3) → Failover to #1 (<Model B>)"`
    `"batch 42 · recovered on Priority #1 (<Model B>)"`.

---

## 5. Testing & Verification
- Unit test for model priority rank validation and uniqueness.
- Unit test for `app_prefs` priority failover settings.
- Integration test for `RetryExecutor` failover ladder: Primary Model fails $\rightarrow$ Priority #1 succeeds $\rightarrow$ verified output.
- Full `pytest` test suite verification.
