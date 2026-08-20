"""
logging_/vlogging.py — Raw API I/O logger.

Reused from v3.6 as-is. Records the exact prompt sent and the exact raw
response received, which is what makes desync/hallucination/truncation
diagnosable after the fact. Thread-safe.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime
from typing import TextIO


class RawLogger:
    """Thread-safe raw API input/output logger."""

    def __init__(self, log_path: str) -> None:
        self._lock = threading.Lock()
        self._file: TextIO | None = None
        try:
            log_dir = os.path.dirname(log_path)
            if log_dir:
                os.makedirs(log_dir, exist_ok=True)
            self._file = open(log_path, "a", encoding="utf-8")
            self._file.write(
                f"\n{'=' * 80}\n"
                f"  RAW API LOG — Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"{'=' * 80}\n\n"
            )
            self._file.flush()
        except Exception:
            self._file = None

    def log(
        self,
        label: str,
        prompt: str,
        response: str | None,
        latency: float = 0.0,
    ) -> None:
        if not self._file:
            return
        ts = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            try:
                self._file.write(
                    f"┌─── [{ts}] {label}  (latency: {latency:.2f}s) ───\n"
                    f"│ PROMPT ({len(prompt)} chars):\n"
                )
                for line in prompt.splitlines():
                    self._file.write(f"│  {line}\n")

                self._file.write(
                    f"│\n│ RESPONSE ({len(response) if response else 0} chars):\n"
                )
                if response:
                    for line in response.splitlines():
                        self._file.write(f"│  {line}\n")
                else:
                    self._file.write("│  (None — API returned no output)\n")

                self._file.write(f"└{'─' * 79}\n\n")
                self._file.flush()
            except Exception:
                pass

    def close(self) -> None:
        if self._file:
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None
