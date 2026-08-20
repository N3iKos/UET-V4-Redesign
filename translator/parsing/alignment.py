"""
parsing/alignment.py — Anti-desync validation.

Ported from v3.6's parser.py unchanged. This is the multi-layer protection
that prevents row-shifting corruption, and 00_OVERVIEW.md §3 lists it as
must-preserve. Pure function, no I/O.

Layers:
  1. hard shift / source-echo detection (whole batch rejected)
  2. batch-level shift correlation (skipped for short JP/CN sources in auto)
  2b. line-merge detection
  3. per-row heuristics, plus a sequential-failure streak guard
"""

from __future__ import annotations

import re


def _line_sig(text: str) -> tuple:
    """Structural fingerprint of a line, used for shift correlation."""
    t = text.strip()
    if not t:
        return ("", False, False, False, False, 0)
    return (
        t[-1],                          # ending char
        "?" in t,
        "!" in t,
        ("「" in t or "『" in t),
        ("」" in t or "』" in t),
        len(t),
    )


def validate_alignment(
    parsed: dict[int, str],
    source_map: dict[int, str],
    mode: str = "auto",     # "off" | "auto" | "strict"
) -> dict[int, str]:
    """
    Return only the rows that pass desync checks. An empty dict means
    "reject the whole batch" — the caller then retries via mini-batch/solo.
    """
    if not parsed:
        return {}
    if mode == "off":
        return parsed

    strict_quotes = mode == "strict"
    sorted_rows = sorted(parsed.keys())

    src_lengths = [
        len(source_map.get(rn, "").strip())
        for rn in sorted_rows
        if source_map.get(rn, "").strip()
    ]
    avg_src_len = sum(src_lengths) / max(len(src_lengths), 1)
    is_short_source = avg_src_len < 15      # JP/CN heuristic

    # ── Layer 1: hard shift / source echo ────────────────────────────────────
    for index, rn in enumerate(sorted_rows):
        trans = parsed[rn].strip()
        src = source_map.get(rn, "").strip()
        if len(trans) <= 5:
            continue
        if index + 1 < len(sorted_rows):
            next_src = source_map.get(sorted_rows[index + 1], "").strip()
            if trans == next_src and src != next_src:
                return {}
        if index > 0:
            prev_src = source_map.get(sorted_rows[index - 1], "").strip()
            if trans == prev_src and src != prev_src:
                return {}

    # ── Layer 2: batch-level shift correlation ───────────────────────────────
    #
    # v3.6 skipped this entirely in auto mode for short (JP/CN) sources,
    # because 5 JP chars -> 40 ID chars makes the length ratio meaningless.
    # Measured consequence: a fully shifted batch of Japanese dialogue passed
    # validation untouched. So instead of skipping the layer, v4 skips only
    # the *length* signal for short sources and scores on the structural
    # signals that survive translation — trailing punctuation, ? and !, and
    # the JP quote pairs, which are copied through verbatim by any competent
    # translation. Behaviour for long sources and for strict mode is
    # unchanged from v3.6.
    use_length = strict_quotes or not is_short_source
    use_quotes = strict_quotes or is_short_source
    min_len = 5 if use_length else 2

    if len(sorted_rows) >= 4:
        normal_hits = shifted_hits = 0.0
        checked = 0

        for index in range(len(sorted_rows) - 1):
            rn = sorted_rows[index]
            next_rn = sorted_rows[index + 1]
            src = source_map.get(rn, "").strip()
            next_src = source_map.get(next_rn, "").strip()
            trans = parsed.get(rn, "").strip()

            if len(src) < min_len or len(trans) < min_len or len(next_src) < min_len:
                continue
            if src == next_src:
                continue      # identical neighbours can't discriminate a shift

            checked += 1
            s_src, s_next, s_tr = _line_sig(src), _line_sig(next_src), _line_sig(trans)

            if use_length:
                normal_ratio = s_tr[5] / max(s_src[5], 1)
                shifted_ratio = s_tr[5] / max(s_next[5], 1)
                normal_hits += (1.0 / (1.0 + abs(normal_ratio - 1.0))) * 3
                shifted_hits += (1.0 / (1.0 + abs(shifted_ratio - 1.0))) * 3

            if s_tr[0] == s_src[0]:
                normal_hits += 2
            if s_tr[0] == s_next[0]:
                shifted_hits += 2
            if s_tr[1] == s_src[1]:
                normal_hits += 1
            if s_tr[1] == s_next[1]:
                shifted_hits += 1
            if s_tr[2] == s_src[2]:
                normal_hits += 1
            if s_tr[2] == s_next[2]:
                shifted_hits += 1

            if use_quotes:
                if s_tr[3] == s_src[3]:
                    normal_hits += 1
                if s_tr[3] == s_next[3]:
                    shifted_hits += 1
                if s_tr[4] == s_src[4]:
                    normal_hits += 1
                if s_tr[4] == s_next[4]:
                    shifted_hits += 1

        if checked >= 3 and shifted_hits > normal_hits * 1.15:
            return {}

    # ── Layer 2b: line-merge detection ───────────────────────────────────────
    merge_reject: set[int] = set()
    merge_ratio = 3.0 if is_short_source else 2.0
    for index in range(len(sorted_rows) - 1):
        rn = sorted_rows[index]
        next_rn = sorted_rows[index + 1]
        src = source_map.get(rn, "").strip()
        next_src = source_map.get(next_rn, "").strip()
        trans = parsed.get(rn, "").strip()
        next_trans = parsed.get(next_rn, "").strip()
        if not src or not next_src or not trans or not next_trans:
            continue
        if len(trans) > len(src) * merge_ratio and len(next_trans) <= 3 and len(next_src) > 3:
            merge_reject.add(rn)
            merge_reject.add(next_rn)
    for rn in merge_reject:
        parsed.pop(rn, None)

    # ── Layer 3: per-row heuristics ──────────────────────────────────────────
    validated: dict[int, str] = {}
    max_ratio = 8.0 if not is_short_source else 12.0
    min_ratio = 0.12 if not is_short_source else 0.05

    for rn, trans in parsed.items():
        src = source_map.get(rn, "")
        if not src or not trans:
            continue

        is_valid = True

        if len(src) > 10 and len(trans) > 10:
            ratio = len(trans) / len(src)
            if ratio > max_ratio or ratio < min_ratio:
                is_valid = False

        src_tokens = set(re.findall(r"\[BR\]\[\w+\]", src))
        if src_tokens and not set(re.findall(r"\[BR\]\[\w+\]", trans)):
            is_valid = False

        if src.count("!") >= 2 and trans.count("!") == 0:
            is_valid = False

        if strict_quotes:
            for quote in ("「", "」", "『", "』"):
                if (quote in src) != (quote in trans):
                    is_valid = False
                    break

        if is_valid:
            validated[rn] = trans

    # Two consecutive rejects means the batch is structurally suspect.
    failure_streak = 0
    for rn in sorted_rows:
        failure_streak = 0 if rn in validated else failure_streak + 1
        if failure_streak >= 2:
            return {}

    return validated
