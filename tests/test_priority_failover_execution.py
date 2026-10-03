"""
tests/test_priority_failover_execution.py — Execution of Priority Failover ladder.
"""

from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from translator.core.models import JobConfig, ProviderError
from translator.providers.client import GenerateResult
from translator.scheduling.retry import FailoverTarget, RetryEvent, RetryExecutor


class _DummyKeyPool:
    def __init__(self):
        self.stop_event = None

    def acquire(self, timeout=600.0):
        class _State:
            key = "key1"
        return _State()

    def record_success(self, *a, **k): pass
    def record_error(self, *a, **k): pass
    def record_503(self, *a, **k): pass
    def record_latency(self, *a, **k): pass
    def record_rate_limit(self, *a, **k): pass
    def cooldown_remaining(self): return 0.0


class _DummyLease:
    name = "PrimaryConn"
    connection_id = "c1"
    base_url = "http://primary.com"
    model_id = "model-primary"
    key_pool = _DummyKeyPool()

    class connection:
        @staticmethod
        def find_model(model_id): return None


class _DummyPool:
    def __init__(self):
        self.stop_event = None
        self.wave_index = 0

    def acquire(self, rows=0):
        return _DummyLease()

    def report_error(self, *a, **k): pass
    def report_success(self, *a, **k): pass
    def report_cooldown(self, *a, **k): pass
    def report_exhausted(self, *a, **k): pass


def test_failover_cascades_to_priority_target(monkeypatch):
    """
    When primary model fails reaches retry threshold (e.g. 2 retries),
    RetryExecutor cascades to Priority #1 and succeeds.
    """
    job = JobConfig(
        provider_id="p1", model_id="model-primary", connection_id="c1",
        src_path="x", out_path="y", batch_size=1, n_workers=1,
        max_attempts=3, retry_mode="fixed", retry_delay_err=0, retry_delay_503=0,
    )

    failover_target = FailoverTarget(
        rank=1,
        model_id="model-failover-1",
        model_name="Priority 1 Model",
        base_url="http://failover.com",
        key="fkey1",
        connection_name="FailoverConn",
        reasoning=None,
    )

    events: list[RetryEvent] = []

    executor = RetryExecutor(
        connection_pool=_DummyPool(),
        job=job,
        base_url="http://primary.com",
        on_event=lambda ev: events.append(ev),
        failover_targets=[failover_target],
        failover_threshold=2,
    )

    calls = []

    def fake_generate(self, model, prompt, **kwargs):
        calls.append(model)
        if model == "model-primary":
            raise ProviderError("Primary model failed", status_code=500)
        # Failover model succeeds
        return GenerateResult(content="Fixed Translation", tokens=10)

    from translator.providers.client import ProviderClient
    monkeypatch.setattr(ProviderClient, "generate", fake_generate)

    res = executor.execute("Translate this", label="batch 1")
    assert res.content == "Fixed Translation"
    assert calls.count("model-primary") == 2
    assert calls.count("model-failover-1") == 1
    # Check that failover event was emitted
    assert any("Failover" in (ev.message or ev.label) for ev in events)
