"""
fitting/audit.py — turn rows into findings, and findings into prompt payload.

The severity anchor lives HERE and nowhere else (spec 1.4): sinking (needs
more lines than the window shows — the tail is invisible to the player) is
critical, overflow (runs off the right edge) is moderate, a ratio over the
profile's threshold is minor. The judge prompt and the fix loop quote these;
they are never re-derived there.

`ratio` compares *rendered widths*, not character counts, and only flags past
the per-engine threshold: JP→ID normally expands 130–160% in width, and a
flat cutoff would flag nearly every line of a healthy translation (spec 1.4b).

Rows whose translation is empty are skipped, not reported: an empty target is
the translate pipeline's problem (and Judge's `empty` condition), not a
fitting one.
"""

from __future__ import annotations

from dataclasses import dataclass

from .measure import detect_newline_tag
from .profiles import EngineProfile
from .wrap import WrapMetrics, wrap_metrics

@dataclass
class Finding:
    row: int
    source_wrap: WrapMetrics
    target_wrap: WrapMetrics
    ratio: float
    kinds: tuple[str, ...]
    severity: str
    newline_tag: str | None

def audit_rows(rows: list[tuple[int, str, str]], profile: EngineProfile) -> list[Finding]:
    findings: list[Finding] = []
    for row, source, translation in rows:
        if not (translation or "").strip():
            continue
        sw = wrap_metrics(source or "", profile)
        tw = wrap_metrics(translation, profile)
        base = max(sw.max_width, 1.0)
        ratio = tw.max_width / base
        kinds: list[str] = []
        if not tw.max_ok:
            kinds.append("sinking")
        if tw.overflow_lines:
            kinds.append("overflow")
        if ratio > profile.ratio_threshold:
            kinds.append("ratio")
        if not kinds:
            continue
        if "sinking" in kinds:
            severity = "critical"
        elif "overflow" in kinds:
            severity = "moderate"
        else:
            severity = "minor"
        findings.append(Finding(
            row=row, source_wrap=sw, target_wrap=tw, ratio=ratio,
            kinds=tuple(kinds), severity=severity,
            newline_tag=detect_newline_tag(source or ""),
        ))
    return findings

def is_ratio_flag(f: Finding) -> bool:
    return "ratio" in f.kinds

def fitting_line(f: Finding) -> str:
    pct = f.target_wrap.max_width / max(f.target_wrap.budget, 0.01) * 100
    src_pct = f.source_wrap.max_width / max(f.source_wrap.budget, 0.01) * 100
    head = (f"source {f.source_wrap.lines} lines/{src_pct:.0f}% · "
            f"translation {f.target_wrap.lines} lines/{pct:.0f}%")
    labels = {"overflow": "OVERFLOW", "sinking": "SINKING", "ratio": "LONG"}
    tail = " · ".join(labels[k] for k in f.kinds)
    tag = f" · line-break tag in source: {f.newline_tag}" if f.newline_tag else ""
    return f"{head} · {tail}{tag}"
