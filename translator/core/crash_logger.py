from __future__ import annotations

import os
import re
import sys
import threading
import time
import traceback
from datetime import datetime
from typing import Any

# Default crash log path in application root
DEFAULT_CRASH_LOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "uet_crash.log",
)

_ACTIVE_LOG_PATH: str = DEFAULT_CRASH_LOG_PATH
_CRASH_LOCK = threading.Lock()

# Regex patterns to sanitize sensitive tokens, keys, and credentials
_SECRET_PATTERNS = [
    re.compile(r"(sk-[a-zA-Z0-9_\-]{8,})"),
    re.compile(r"(Bearer\s+[a-zA-Z0-9_\-\.]{8,})", re.IGNORECASE),
    re.compile(r"(key[=:]\s*['\"]?[a-zA-Z0-9_\-]{8,}['\"]?)", re.IGNORECASE),
    re.compile(r"(token[=:]\s*['\"]?[a-zA-Z0-9_\-]{8,}['\"]?)", re.IGNORECASE),
    re.compile(r"(api[-_]?key[=:]\s*['\"]?[a-zA-Z0-9_\-]{8,}['\"]?)", re.IGNORECASE),
    re.compile(r"(secret[=:]\s*['\"]?[a-zA-Z0-9_\-]{8,}['\"]?)", re.IGNORECASE),
]


def sanitize_text(text: str) -> str:
    """Mask sensitive tokens, credentials, and API keys from log strings."""
    if not text:
        return ""
    clean = text
    for pat in _SECRET_PATTERNS:
        clean = pat.sub("[REDACTED_SECRET]", clean)
    return clean


def get_crash_log_path() -> str:
    return _ACTIVE_LOG_PATH


def set_crash_log_path(path: str) -> None:
    global _ACTIVE_LOG_PATH
    _ACTIVE_LOG_PATH = os.path.abspath(path)


def log_exception(
    exc: BaseException | None = None,
    log_path: str | None = None,
    context: dict[str, Any] | None = None,
    stage: str = "",
    operation: str = "",
    file_path: str = "",
    thread_name: str | None = None,
) -> str:
    """
    Append full exception traceback and diagnostic metadata to persistent crash log file.
    Thread-safe and guaranteed never to throw an unhandled exception.
    """
    target_path = log_path or _ACTIVE_LOG_PATH
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    current_thread = thread_name or threading.current_thread().name

    ctx = dict(context or {})
    if stage and "stage" not in ctx:
        ctx["stage"] = stage
    if operation and "operation" not in ctx:
        ctx["operation"] = operation
    if file_path and "file" not in ctx:
        ctx["file"] = file_path

    # Sanitize context dictionary
    sanitized_ctx: list[str] = []
    for k, v in ctx.items():
        if any(secret_word in k.lower() for secret_word in ("key", "token", "secret", "password", "auth")):
            sanitized_ctx.append(f"  {k}: [REDACTED_SECRET]")
        else:
            sanitized_ctx.append(f"  {k}: {sanitize_text(str(v))}")

    if exc is not None:
        exc_type = type(exc).__name__
        exc_msg = sanitize_text(str(exc))
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        tb_clean = sanitize_text(tb)
    else:
        exc_type = "UnknownException"
        exc_msg = "No exception instance provided"
        tb_clean = sanitize_text(traceback.format_exc())

    entry_lines = [
        "=" * 80,
        f"[{now_str}] FATAL / UNCAUGHT EXCEPTION",
        f"Thread: {current_thread}",
    ]
    if sanitized_ctx:
        entry_lines.append("Context:")
        entry_lines.extend(sanitized_ctx)
    entry_lines.append(f"Exception: {exc_type}: {exc_msg}")
    entry_lines.append("Traceback (most recent call last):")
    entry_lines.append(tb_clean.strip())
    entry_lines.append("=" * 80)
    entry_lines.append("")

    entry_text = "\n".join(entry_lines)

    with _CRASH_LOCK:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)
            with open(target_path, "a", encoding="utf-8") as f:
                f.write(entry_text)
                f.flush()
        except Exception as write_err:
            try:
                sys.stderr.write(f"[CRASH_LOGGER_FAILURE] Failed to write crash log: {write_err}\n")
                sys.stderr.write(entry_text)
            except Exception:
                pass

    return target_path


def _sys_excepthook(exc_type, exc_value, exc_traceback) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    log_exception(
        exc=exc_value,
        context={"stage": "sys_excepthook", "operation": "main_thread"},
    )
    sys.__excepthook__(exc_type, exc_value, exc_traceback)


def _threading_excepthook(args: threading.ExceptHookArgs) -> None:
    if issubclass(args.exc_type, KeyboardInterrupt):
        return
    thread_name = args.thread.name if args.thread else "unknown_thread"
    log_exception(
        exc=args.exc_value,
        thread_name=thread_name,
        context={"stage": "threading_excepthook", "thread": thread_name},
    )


def install_global_crash_handler(log_path: str | None = None) -> None:
    """Install global crash hooks on sys and threading."""
    if log_path:
        set_crash_log_path(log_path)
    sys.excepthook = _sys_excepthook
    if hasattr(threading, "excepthook"):
        threading.excepthook = _threading_excepthook
