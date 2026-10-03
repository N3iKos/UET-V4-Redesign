from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.core.crash_logger import (
    get_crash_log_path,
    install_global_crash_handler,
    log_exception,
    sanitize_text,
)


def test_sanitize_text_masks_secrets():
    text = "Error connecting to https://api.example.com with key sk-1234567890abcdef and Bearer secret_token_xyz"
    clean = sanitize_text(text)
    assert "sk-1234567890abcdef" not in clean
    assert "secret_token_xyz" not in clean


def test_log_exception_records_metadata_and_trace(tmp_path):
    log_file = tmp_path / "test_crash.log"
    try:
        raise ValueError("Simulated failure in translation")
    except ValueError as exc:
        p = log_exception(
            exc,
            log_path=str(log_file),
            context={
                "operation": "retranslate_failed",
                "file": "test.csv",
                "row": 123,
                "stage": "translation_worker",
                "secret_key": "sk-should-be-masked-12345",
            },
        )
        assert p == str(log_file)

    assert log_file.exists()
    content = log_file.read_text(encoding="utf-8")
    assert "ValueError: Simulated failure in translation" in content
    assert "operation: retranslate_failed" in content
    assert "file: test.csv" in content
    assert "stage: translation_worker" in content
    assert "Traceback" in content
    assert "sk-should-be-masked-12345" not in content


def test_threading_excepthook_records_unhandled_thread_crash(tmp_path):
    log_file = tmp_path / "test_thread_crash.log"
    install_global_crash_handler(log_path=str(log_file))

    def bad_thread_fn():
        raise RuntimeError("Fatal worker error in background")

    t = threading.Thread(target=bad_thread_fn, name="uet-test-worker")
    t.start()
    t.join()

    assert log_file.exists()
    content = log_file.read_text(encoding="utf-8")
    assert "RuntimeError: Fatal worker error in background" in content
    assert "uet-test-worker" in content
