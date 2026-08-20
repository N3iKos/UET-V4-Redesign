#!/usr/bin/env python3
"""
Universal Excel Translator v4 — entry point.

Requires : Python 3.10+, openpyxl, requests, textual
Install  : pip install openpyxl requests textual
Run      : python main.py
"""

from __future__ import annotations

import os
import signal
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _check_dependencies() -> None:
    for module, package in (
        ("requests", "requests"),
        ("openpyxl", "openpyxl"),
        ("textual", "textual"),
    ):
        try:
            __import__(module)
        except ImportError:
            sys.exit(f"ERROR: '{module}' not installed.  Run: pip install {package}")


def main() -> None:
    # Textual installs its own handler; this only covers the pre-TUI phase.
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    _check_dependencies()

    from translator.tui.app import UETApp

    UETApp().run()


if __name__ == "__main__":
    main()
