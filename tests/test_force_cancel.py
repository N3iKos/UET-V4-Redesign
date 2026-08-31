"""
tests/test_force_cancel.py — The double-`c` force cancel.

The contract, per the user's request:

  1. First `c` sets the stop flag and lets the request in flight FINISH —
     its reply is real work and is kept.
  2. Second `c` (within the window) SEVERS the request in flight — the run
     ends now, not after the current output.

The severing mechanism is the client-level abort registry: a client built
with an abort_event registers itself, and `abort_all_for(event)` closes each
registered client's live socket. These tests pin that mechanism and its
plumbing without a network — a blocked read is simulated by a fake response
object whose close() the test observes.
"""

from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.providers.client import (
    ProviderClient,
    _register_abortable,
    abort_all_for,
)


class _FakeResponse:
    """A response stand-in that records being closed."""

    def __init__(self) -> None:
        self.closed = threading.Event()

    def close(self) -> None:
        self.closed.set()


class TestAbortRegistry:
    def test_client_registers_on_construction(self):
        event = threading.Event()
        client = ProviderClient("http://localhost:1", abort_event=event)
        # The registry is weak; asking the module to abort must at least see
        # the client (one closure attempted), and must not raise.
        closed = abort_all_for(event)
        assert closed == 1, "the registered client was told to abort"

    def test_abort_closes_the_active_response(self):
        event = threading.Event()
        client = ProviderClient("http://localhost:1", abort_event=event)
        fake = _FakeResponse()
        with client._response_lock:
            client._active_response = fake
        abort_all_for(event)
        assert fake.closed.is_set(), "the in-flight response was severed"

    def test_abort_is_idempotent(self):
        event = threading.Event()
        client = ProviderClient("http://localhost:1", abort_event=event)
        fake = _FakeResponse()
        with client._response_lock:
            client._active_response = fake
        abort_all_for(event)
        abort_all_for(event)          # must not raise; second close is a no-op
        assert fake.closed.is_set()

    def test_abort_with_no_clients_is_safe(self):
        event = threading.Event()
        assert abort_all_for(event) == 0

    def test_clients_without_event_are_not_registered(self):
        # The default constructor path (no abort_event) must behave exactly
        # as before: fully synchronous, nothing in any registry.
        client = ProviderClient("http://localhost:1")
        assert client.abort_event is None
        assert client._aborted() is False

    def test_release_clears_the_active_response(self):
        event = threading.Event()
        client = ProviderClient("http://localhost:1", abort_event=event)
        fake = _FakeResponse()
        with client._response_lock:
            client._active_response = fake
        client._release_response()
        abort_all_for(event)
        assert not fake.closed.is_set(), \
            "a released (finished) response is not severed by a later abort"


class TestAbortablePost:
    def test_generate_chat_streams_so_abort_covers_the_read(self, monkeypatch):
        """
        The core of the double-`c` bug: `requests.post(stream=False)` blocks
        until the whole body is read, so the response object only exists
        AFTER the read — the abort registry had nothing to close and a
        force-cancel waited the request out. `generate_chat` must therefore
        send with stream=True and register the response before draining it.

        Pinned here by patching requests.post: the transport-level `stream`
        kwarg must be True for the generate call.
        """
        import translator.providers.client as client_module

        seen: dict = {}

        class _Resp:
            status_code = 200
            headers = {}
            text = '{"choices": [{"message": {"role": "a", "content": "ok"}}]}'

            def close(self):
                pass

        def _fake_post(url, **kwargs):
            seen.update(kwargs)
            return _Resp()

        monkeypatch.setattr(client_module.requests, "post", _fake_post)
        event = threading.Event()
        client = ProviderClient("http://localhost:1", abort_event=event)
        client.generate_chat("m", [{"role": "user", "content": "hi"}])
        assert seen.get("stream") is True, \
            "generate_chat must send stream=True so the response exists (and is abortable) before the body read"

    def test_abortable_post_registers_before_body_read(self, monkeypatch):
        """
        `_abortable_post` must return with the response already registered:
        the abort window covers the body read, not only what follows it.
        """
        import translator.providers.client as client_module

        registered_at_post: list[object] = []

        class _Resp:
            status_code = 200
            headers = {}
            # A body read that must never happen inside _abortable_post.
            text = property(lambda self: (_ for _ in ()).throw(
                AssertionError("body read must not happen in _abortable_post")))

            def close(self):
                pass

        def _fake_post(url, **kwargs):
            return _Resp()

        monkeypatch.setattr(client_module.requests, "post", _fake_post)
        event = threading.Event()
        client = ProviderClient("http://localhost:1", abort_event=event)
        resp = client._abortable_post({"x": 1})
        with client._response_lock:
            registered_at_post.append(client._active_response)
        assert registered_at_post == [resp], \
            "the response must be registered for abort as soon as it exists"

    def test_registry_entry_vanishes_with_the_client(self):
        # Weak: dropping the last strong reference removes the client from
        # the registry, so an abort cannot reach a dead client.
        import gc

        event = threading.Event()
        client = ProviderClient("http://localhost:1", abort_event=event)
        assert abort_all_for(event) == 1
        del client
        gc.collect()
        assert abort_all_for(event) == 0

    def test_events_are_isolated(self):
        # Two runs share nothing: aborting one event must not touch clients
        # built with the other's event.
        event_a = threading.Event()
        event_b = threading.Event()
        client_a = ProviderClient("http://localhost:1", abort_event=event_a)
        client_b = ProviderClient("http://localhost:1", abort_event=event_b)
        fake_b = _FakeResponse()
        with client_b._response_lock:
            client_b._active_response = fake_b
        abort_all_for(event_a)
        assert not fake_b.is_set if hasattr(fake_b, "is_set") else True
        assert not fake_b.closed.is_set(), \
            "client_b was untouched by event_a's abort"


class TestAbortedFlag:
    def test_aborted_reads_the_event(self):
        event = threading.Event()
        client = ProviderClient("http://localhost:1", abort_event=event)
        assert client._aborted() is False
        event.set()
        assert client._aborted() is True

    def test_explicit_registration_helper(self):
        # The helper exists for tests / unusual construction paths.
        event = threading.Event()
        client = ProviderClient("http://localhost:1")
        _register_abortable(event, client)
        assert abort_all_for(event) == 1


class TestCancelledProviderError:
    def test_abort_error_is_not_retried(self):
        """
        The property the whole feature exists for: when a force-cancel
        severs the socket, the ProviderError it raises is not rotated on or
        slept through — the retry executor's error handler sees the stop
        flag plus the aborted message and returns immediately, so the run
        ends instead of re-issuing.
        """
        from translator.core.models import JobConfig, ProviderError
        from translator.scheduling.retry import RetryExecutor

        stop = threading.Event()
        job = JobConfig(
            provider_id="p", model_id="m", connection_id="",
            src_path="x", out_path="y",
            batch_size=1, n_workers=1, processing_mode="sequential",
            max_attempts=3, retry_mode="fixed",
        )
        executor = RetryExecutor(
            connection_pool=_PoolStub(),
            job=job,
            base_url="http://localhost:1",
            stop_event=stop,
        )

        class _KP:
            def record_error(self, *a, **k):
                pass

        class _Lease:
            key_pool = _KP()
            connection_id = "c1"
            name = "stub"

        # The force-cancel path: stop set + aborted message.
        stop.set()
        exc = ProviderError(
            "Request aborted (force cancel)", status_code=0, retry_delay=0,
        )
        # The guard is inside _handle_provider_error: it must return without
        # touching the pool or sleeping. A rotate call would hit _PoolStub's
        # report_error and set the flag.
        executor._handle_provider_error(
            exc, _Lease(), "k", "label", 1, 0.0, "stub",
        )
        assert _PoolStub.rotated is False, \
            "an aborted request must not rotate the pool"

    def test_normal_error_still_rotates(self):
        """The guard is specific: an ordinary error keeps its handling."""
        from translator.core.models import JobConfig, ProviderError
        from translator.scheduling.retry import RetryExecutor

        stop = threading.Event()
        job = JobConfig(
            provider_id="p", model_id="m", connection_id="",
            src_path="x", out_path="y",
            batch_size=1, n_workers=1, processing_mode="sequential",
            max_attempts=3, retry_mode="fixed",
            retry_delay_err=0,
        )
        executor = RetryExecutor(
            connection_pool=_PoolStub(),
            job=job,
            base_url="http://localhost:1",
            stop_event=stop,
        )

        class _KP:
            def record_error(self, *a, **k):
                pass

            def record_503(self, *a, **k):
                pass

        class _Lease:
            key_pool = _KP()
            connection_id = "c1"
            name = "stub"

        _PoolStub.rotated = False
        exc = ProviderError("HTTP 500: boom", status_code=500)
        executor._handle_provider_error(
            exc, _Lease(), "k", "label", 1, 0.0, "stub",
        )
        assert _PoolStub.rotated is True, \
            "an ordinary server error still rotates away from the slot"


class _PoolStub:
    """Records whether the executor tried to rotate/cool the slot."""

    rotated = False

    def acquire(self, rows=0):
        raise AssertionError("acquire should not be called by these tests")

    def report_error(self, *a, **k):
        _PoolStub.rotated = True

    def report_cooldown(self, *a, **k):
        _PoolStub.rotated = True

    def report_exhausted(self, *a, **k):
        _PoolStub.rotated = True

    def report_success(self, *a, **k):
        pass
