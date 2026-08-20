"""
judge/__init__.py — Translation quality evaluation (FEATURE-001).

The feature is deliberately thin: everything that talks to a provider, retries,
rotates a connection or writes a spreadsheet already exists and is reused
unchanged (ADR-001). What lives here is only the part that did not:

  prompts.py    what to ask, per criterion, with adaptive placeholder detection
  parser.py     how to read the answer back, tolerantly
  estimator.py  how many tokens that will cost, and where to split
  runner.py     evaluation, Fix All (staged), Fix It, Judge Again
  models.py     the shapes the screens render

Nothing in this package writes a report to disk (FR-114), and nothing writes a
fix without explicit approval (FR-111).
"""

from __future__ import annotations
