"""translator/fitting — deterministic text-fit measurement for game dialog windows."""

from .profiles import ENGINE_LABELS, ENGINE_ORDER, EngineProfile, get_profile
from .measure import detect_newline_tag, measure_lines, strip_tokens
from .wrap import wrap_metrics, wrap_lines, suggest_wrap
from .audit import Finding, audit_rows, fitting_line, is_ratio_flag
from .apply import apply_wrapped

__all__ = [
    "ENGINE_LABELS", "ENGINE_ORDER", "EngineProfile", "get_profile",
    "detect_newline_tag", "measure_lines", "strip_tokens",
    "wrap_metrics", "wrap_lines", "suggest_wrap",
    "Finding", "audit_rows", "fitting_line", "is_ratio_flag", "apply_wrapped",
]
