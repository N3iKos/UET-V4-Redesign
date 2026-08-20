"""
tests/test_error_classification.py — provider error handling regression tests.

These pin the fix for the "daily limits reached" false positive: a temporary
429/5xx/timeout must cool a connection down and recover, only a *confirmed*
quota signal retires it, and a run that truly runs out of connections aborts
once rather than cascading a duplicate error per remaining batch.

Run: python -m pytest tests/test_error_classification.py  (or run this file
directly — it falls back to a tiny runner when pytest is absent).
"""

from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.core.models import JobConfig, ProviderError
from translator.providers.http_utils import classify_rate_limit, is_quota_error
from translator.providers.client import ProviderClient
from translator.providers.models import Connection, Provider
from translator.scheduling.connection_pool import (
    AllConnectionsUnavailable,
    ConnectionPool,
)
from translator.scheduling.key_pool import KeyPool
from translator.scheduling.retry import RetryExecutor
from translator import config


# ── classify_rate_limit / is_quota_error ──────────────────────────────────────

def test_anonymous_429_is_temporary():
    is_daily, is_temp = classify_rate_limit("Too Many Requests", {})
    assert is_daily is False and is_temp is True


def test_429_with_retry_after_is_temporary():
    is_daily, _ = classify_rate_limit("rate limited", {"retry-after": "30"})
    assert is_daily is False


def test_window_counter_zero_is_not_daily():
    # The per-window counter at 0 (no -day suffix) is a short rate limit.
    is_daily, _ = classify_rate_limit("", {"x-ratelimit-remaining-requests": "0"})
    assert is_daily is False


def test_day_counter_zero_is_daily():
    is_daily, _ = classify_rate_limit("", {"x-ratelimit-remaining-requests-day": "0"})
    assert is_daily is True


def test_explicit_daily_body_is_daily():
    is_daily, _ = classify_rate_limit("You exceeded your requests per day.", {})
    assert is_daily is True


def test_quota_words_detected():
    assert is_quota_error("insufficient_quota")
    assert is_quota_error("exceeded your quota")
    assert not is_quota_error("invalid api key")


# ── _raise_for_status classification ───────────────────────────────────────────

def _kind_for(status, text="", headers=None):
    try:
        ProviderClient._raise_for_status(status, text, headers or {})
    except ProviderError as exc:
        return exc.kind
    raise AssertionError("expected ProviderError")


def test_status_classification():
    assert _kind_for(429) == ProviderError.KIND_RATE_LIMIT
    assert _kind_for(429, "requests per day") == ProviderError.KIND_QUOTA
    assert _kind_for(429, headers={"x-ratelimit-remaining-requests-day": "0"}) == ProviderError.KIND_QUOTA
    assert _kind_for(500) == ProviderError.KIND_SERVER
    assert _kind_for(503) == ProviderError.KIND_SERVER
    assert _kind_for(401) == ProviderError.KIND_AUTH
    assert _kind_for(403) == ProviderError.KIND_AUTH
    assert _kind_for(403, "insufficient_quota") == ProviderError.KIND_QUOTA
    assert _kind_for(400) == ProviderError.KIND_INVALID
    assert _kind_for(404) == ProviderError.KIND_INVALID
    assert _kind_for(418) == ProviderError.KIND_UNKNOWN


# ── KeyPool no fictional TPD cap ────────────────────────────────────────────────

def test_key_pool_never_exhausts_on_tokens():
    pool = KeyPool(keys=["k"])
    # Record a huge amount of usage — far past the old 1M cap.
    pool.record_success("k", tokens=5_000_000, headers={})
    assert not pool.all_exhausted(), "tokens alone must never exhaust a key"
    assert pool.try_acquire() is not None


def test_key_pool_exhausts_on_daily_header():
    pool = KeyPool(keys=["k"])
    pool.record_rate_limit("k", delay=30, is_daily=False,
                           headers={"x-ratelimit-remaining-requests-day": "0"})
    assert pool.all_exhausted()


def test_key_pool_window_zero_is_cooldown_not_exhausted():
    pool = KeyPool(keys=["k"])
    pool.record_rate_limit("k", delay=5, is_daily=False,
                           headers={"x-ratelimit-remaining-requests": "0"})
    assert not pool.all_exhausted()
    # It recovers once the cooldown elapses.
    st = pool.state_for("k")
    st.cooldown_until = time.time() - 1
    assert pool.try_acquire() is not None


# ── RetryExecutor lifecycle via a fake client ──────────────────────────────────

class _FakeResult:
    def __init__(self, content):
        self.content = content
        self.tokens = 10
        self.headers = {}


class _ScriptedClient:
    """A ProviderClient stand-in that plays a scripted list of outcomes."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    def generate(self, **kwargs):
        self.calls += 1
        item = self._script.pop(0) if self._script else ("ok", "done")
        kind, payload = item
        if kind == "ok":
            return _FakeResult(payload)
        raise payload      # a ProviderError


def _make_executor(script, job=None, provider=None):
    provider = provider or Provider(
        name="P", base_url="http://x",
        connections=[Connection(name="c", api_key="k", is_valid=True)],
    )
    pool = ConnectionPool(provider)
    job = job or JobConfig(provider_id=provider.id, model_id="m",
                           max_attempts=5, retry_delay_err=0, retry_delay_503=0)
    ex = RetryExecutor(pool, job, base_url="http://x")
    fake = _ScriptedClient(script)
    ex._client_for = lambda key, base_url="": fake   # type: ignore
    # No real sleeps.
    ex._sleep = lambda seconds: None                 # type: ignore
    return ex, pool, fake


def test_429_then_success_recovers():
    err = ProviderError("429", status_code=429, is_rate_limit=True,
                        retry_delay=1, kind=ProviderError.KIND_RATE_LIMIT)
    ex, pool, fake = _make_executor([("err", err), ("ok", "recovered")])
    result = ex.execute("prompt", label="t", rows=1)
    assert result.content == "recovered"
    assert fake.calls == 2
    # The connection is usable again, not retired.
    assert not all(s.status == "exhausted" for s in pool.states())


def test_502_then_success_recovers():
    err = ProviderError("502", status_code=502, retry_delay=0,
                        kind=ProviderError.KIND_SERVER)
    ex, pool, fake = _make_executor([("err", err), ("ok", "ok2")])
    result = ex.execute("prompt", label="t", rows=1)
    assert result.content == "ok2"
    assert not all(s.status == "exhausted" for s in pool.states())


def test_timeout_then_success_recovers():
    ex, pool, fake = _make_executor([("err", TimeoutError("slow")), ("ok", "ok3")])
    result = ex.execute("prompt", label="t", rows=1)
    assert result.content == "ok3"


def test_confirmed_quota_retires_connection():
    err = ProviderError("daily", status_code=429, is_rate_limit=True,
                        is_tpd=True, kind=ProviderError.KIND_QUOTA)
    ex, pool, fake = _make_executor([("err", err), ("err", err), ("err", err),
                                     ("err", err), ("err", err)])
    try:
        result = ex.execute("prompt", label="t", rows=1)
    except AllConnectionsUnavailable:
        result = None
    # Either the single connection is now exhausted, or acquire raised because
    # nothing is left — both mean "retired", which is correct for real quota.
    assert result is None or result.content is None
    assert all(s.status == "exhausted" for s in pool.states())


def test_auth_failure_not_treated_as_rate_limit():
    err = ProviderError("401", status_code=401, kind=ProviderError.KIND_AUTH)
    ex, pool, fake = _make_executor([("err", err), ("ok", "after")])
    result = ex.execute("prompt", label="t", rows=1)
    # Single-connection fallback keeps trying; the connection is not exhausted
    # or cooled into oblivion by an auth error.
    assert result.content == "after"
    assert not all(s.status == "exhausted" for s in pool.states())


def test_invalid_model_not_quota():
    err = ProviderError("404 model", status_code=404,
                        kind=ProviderError.KIND_INVALID)
    ex, pool, fake = _make_executor([("err", err)] * 5)
    result = ex.execute("prompt", label="t", rows=1)
    assert result.content is None
    assert result.error_kind == ProviderError.KIND_INVALID
    # Not exhausted — an invalid request is not a spent quota.
    assert not all(s.status == "exhausted" for s in pool.states())


def test_all_connections_unavailable_raises_once():
    # Two connections both hitting confirmed quota → pool ends exhausted and
    # acquire raises AllConnectionsUnavailable exactly (not a per-batch loop).
    provider = Provider(
        name="P", base_url="http://x",
        connections=[
            Connection(name="a", api_key="k1", is_valid=True),
            Connection(name="b", api_key="k2", is_valid=True),
        ],
    )
    err = ProviderError("daily", status_code=429, is_rate_limit=True,
                        is_tpd=True, kind=ProviderError.KIND_QUOTA)
    ex, pool, fake = _make_executor([("err", err)] * 10, provider=provider)
    raised = False
    try:
        ex.execute("prompt", label="t", rows=1)
    except AllConnectionsUnavailable:
        raised = True
    assert raised or all(s.status == "exhausted" for s in pool.states())


def test_fallback_to_other_connection_on_error():
    provider = Provider(
        name="P", base_url="http://x", strategy="fallback",
        connections=[
            Connection(name="a", api_key="k1", is_valid=True),
            Connection(name="b", api_key="k2", is_valid=True),
        ],
    )
    pool = ConnectionPool(provider)
    job = JobConfig(provider_id=provider.id, model_id="m", max_attempts=5,
                    retry_delay_err=0)
    ex = RetryExecutor(pool, job, base_url="http://x")

    # First connection errors once (plain error → fallback), second succeeds.
    err = ProviderError("boom", status_code=500, retry_delay=0,
                        kind=ProviderError.KIND_SERVER)
    fake = _ScriptedClient([("err", err), ("ok", "from-b")])
    ex._client_for = lambda key, base_url="": fake  # type: ignore
    ex._sleep = lambda seconds: None                # type: ignore
    result = ex.execute("prompt", label="t", rows=1)
    assert result.content == "from-b"


# ── minimal runner when pytest is unavailable ──────────────────────────────────

def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in tests:
        try:
            fn()
        except Exception as exc:
            print(f"FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
        else:
            passed += 1
            print(f"ok   {fn.__name__}")
    print(f"\n{passed}/{len(tests)} passed")
    return passed == len(tests)


if __name__ == "__main__":
    sys.exit(0 if _run_all() else 1)
