"""
tests/test_total_deadline.py — The wall-clock cap on one generate request.

The contract, from the user's report:

  `request_timeout` (Settings) is the MAXIMUM lifetime of ONE request — not an
  inactivity timeout. A server that trickles bytes every few seconds resets a
  socket read timeout forever; a "120s timeout" run measured 227s+. These tests
  pin the enforcement: a deadline timer is armed when the request is sent and
  severs the response the moment the cap is hit, whether or not data is still
  arriving, and the read error surfaces as an ordinary timeout
  (ProviderError, is_timeout=True) so the retry loop rotates and re-issues
  exactly as it always did.

No network: `requests.post` is patched with a fake whose body read blocks
until the deadline closes it — a trickling server in miniature.
"""

from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import translator.providers.client as client_module
from translator.core.models import ProviderError
from translator.providers.client import ProviderClient


class _TricklingResponse:
    """
    A response whose body never finishes on its own.

    `text` blocks on an event the deadline timer sets by calling close() —
    standing in for a server that keeps sending a byte every few seconds, so
    the socket read timeout never fires but the request outlives the user's
    cap. close() is what the real deadline calls from its timer thread.
    """

    status_code = 200
    headers = {}

    def __init__(self) -> None:
        self._closed = threading.Event()

    @property
    def text(self) -> str:
        self._closed.wait(timeout=30.0)      # test fails rather than hangs
        raise client_module.requests.exceptions.ConnectionError(
            "read severed mid-body"
        )

    def close(self) -> None:
        self._closed.set()


class _FastResponse:
    status_code = 200
    headers = {}
    text = '{"choices": [{"message": {"role": "a", "content": "ok"}}]}'

    def close(self) -> None:
        pass


class TestTotalDeadline:
    def test_trickling_server_is_cut_at_the_cap(self, monkeypatch):
        """
        The heart of the fix: bytes still arriving, request past the cap →
        severed and reported as a timeout, not left to run for minutes.
        """
        client = ProviderClient("http://localhost:1", timeout=1.0)
        monkeypatch.setattr(
            client_module.requests, "post",
            lambda url, **kwargs: _TricklingResponse(),
        )
        started = time.time()
        with pytest.raises(ProviderError) as raised:
            client.generate_chat("m", [{"role": "user", "content": "hi"}])
        elapsed = time.time() - started

        assert raised.value.is_timeout is True, \
            "a deadline hit is a timeout: the retry loop must rotate and re-issue"
        assert "total deadline" in raised.value.response_text or \
            "total deadline" in str(raised.value)
        assert elapsed < 5.0, \
            f"the request must be severed at ~1s, not run on ({elapsed:.1f}s)"

    def test_fast_reply_is_untouched_by_the_timer(self, monkeypatch):
        """
        The timer is cancelled the moment the body is read: a normal request
        never notices it existed.
        """
        client = ProviderClient("http://localhost:1", timeout=30.0)
        monkeypatch.setattr(
            client_module.requests, "post",
            lambda url, **kwargs: _FastResponse(),
        )
        result = client.generate_chat("m", [{"role": "user", "content": "hi"}])
        assert result.content == "ok"

    def test_deadline_does_not_override_force_cancel(self, monkeypatch):
        """
        When BOTH fire (the user cancels while past the deadline), the
        force-cancel wins: the message stays the cancellation, so the retry
        loop gives up quietly instead of rotating.
        """
        event = threading.Event()

        class _CancellyResponse(_TricklingResponse):
            @property
            def text(self) -> str:
                event.set()                        # the user pressed c twice
                self._closed.wait(timeout=30.0)
                raise client_module.requests.exceptions.ConnectionError(
                    "aborted"
                )

        client = ProviderClient(
            "http://localhost:1", timeout=1.0, abort_event=event
        )
        monkeypatch.setattr(
            client_module.requests, "post",
            lambda url, **kwargs: _CancellyResponse(),
        )
        with pytest.raises(ProviderError) as raised:
            client.generate_chat("m", [{"role": "user", "content": "hi"}])
        assert "force cancel" in (raised.value.response_text or str(raised.value))
        assert raised.value.is_timeout is False

    def test_cap_comes_from_the_clients_timeout(self):
        """
        The cap is the client's timeout — the same setting request the retry
        executor builds its clients from, so Settings' `request_timeout` is
        the one knob for the whole pipeline (all-for-one).
        """
        import inspect

        source = inspect.getsource(ProviderClient.generate_chat)
        assert "self.timeout" in source, \
            "the deadline must be armed from the client's timeout, not a constant"
