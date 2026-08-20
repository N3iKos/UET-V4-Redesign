"""
judge/parser.py — Reading the review back.

Models fence their JSON, prefix it with "Here is my review:", answer in prose,
or return a bare list instead of the object they were asked for. The first three
are recoverable and the salvage order is the one `playground/judging.py` and
`parsing/recovery.py` already use: the whole body, then a fenced block, then the
widest `{...}` span.

Two rules that matter more than the parsing:

  * **never raise.** A review is not worth an exception in front of someone
    mid-evaluation. Every failure mode has a value: unreadable JSON becomes
    `unparsed=True` with the reply kept, and a garbage entry is skipped rather
    than taking its siblings with it.
  * **prose is not zero.** A model that reviewed the rows and answered in
    sentences has said something worth reading. Scoring that as "everything
    failed" would be a lie about the translation, so it is reported as
    unparsed and the section is excluded from the score instead.

An unrecognised error type is kept as `other` rather than dropped: the comment
is still true even when the label is not one of ours.
"""

from __future__ import annotations

import json
import re

from .models import ERROR_TYPES, JudgeResult, RowIssue, normalise_severity

# Spellings models actually return for our four criteria. The mapping is
# deliberately generous — the label is cosmetic, the comment is the content.
_ALIASES: dict[str, str] = {
    "mistranslation": "mistranslation",
    "mistranslate": "mistranslation",
    "meaning": "mistranslation",
    "accuracy": "mistranslation",
    "inaccurate": "mistranslation",
    "wrong": "mistranslation",
    "omission": "mistranslation",
    "addition": "mistranslation",
    "context": "context",
    "contextual": "context",
    "placeholder": "placeholder",
    "placeholders": "placeholder",
    "tag": "placeholder",
    "token": "placeholder",
    "format": "placeholder",
    "style": "style",
    "tone": "style",
    "register": "style",
    "terminology": "style",
    "fluency": "style",
    "grammar": "style",
    "empty": "empty",
    "failed": "failed",
}


def normalise_error_type(value) -> str:
    """
    One reported error type, mapped onto ours.

    Substring matching after the exact lookup, because models answer
    "placeholder error" and "mistranslation (meaning)" as often as the bare
    word. Anything genuinely unrecognised becomes `other`.
    """
    text = str(value or "").strip().lower()
    if not text:
        return "other"
    if text in ERROR_TYPES:
        return text
    if text in _ALIASES:
        return _ALIASES[text]
    for alias, mapped in _ALIASES.items():
        if alias in text:
            return mapped
    return "other"


def normalise_error_types(values) -> list[str]:
    """Deduplicated, order preserved, never empty."""
    if isinstance(values, str):
        values = re.split(r"[,;/|]+", values)
    if not isinstance(values, (list, tuple, set)):
        values = [values]
    seen: list[str] = []
    for value in values:
        mapped = normalise_error_type(value)
        if mapped not in seen:
            seen.append(mapped)
    return seen or ["other"]


def parse_evaluation(text: str, graded: bool = True) -> JudgeResult:
    """
    A section's reply → the rows it flagged.

    An empty `lines` list is a real answer, not a failure: it means every row
    passed, and the report needs to be able to say so (Doc 06 edge case: score
    100%, no fix list).

    `graded` says whether this run asked for severity. When it did not, the
    field is left empty rather than defaulted, so a screen can tell "this run
    did not grade" apart from "nothing was critical" — and a model that volunteers
    a severity anyway is ignored, because a ranking the user did not ask for
    would be applied inconsistently across sections.
    """
    result = JudgeResult(raw=text or "")
    data = _extract_json(text or "")
    if data is None:
        result.unparsed = True
        return result

    entries = data.get("lines")
    if entries is None:
        # Some models answer with the list at the top level, or name the key
        # differently. Try the obvious alternatives before giving up.
        for key in ("issues", "problems", "rows", "results"):
            if isinstance(data.get(key), list):
                entries = data[key]
                break
    if not isinstance(entries, list):
        result.unparsed = True
        return result

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        row = _as_row(entry.get("row", entry.get("line", entry.get("id"))))
        if row is None:
            continue
        # Whatever else is wrong with the reply, this row was actually judged.
        # It counts as seen so a section that failed once and recovered can
        # still mark its recovered rows as evaluated rather than unevaluated.
        result.rows_seen.add(row)
        comment = str(
            entry.get("comment") or entry.get("note") or entry.get("reason") or ""
        ).strip()
        types = entry.get("error_types", entry.get("error_type", entry.get("types")))
        result.issues[row] = RowIssue(
            row=row,
            error_types=normalise_error_types(types),
            comment=comment,
            severity=_severity_of(entry) if graded else "",
        )
    return result


def _severity_of(entry: dict) -> str:
    """
    The severity a model reported, under whichever key it chose.

    A flagged row with no readable severity becomes `moderate`
    (`normalise_severity`), never `minor`: the model has already said the row is
    wrong, and grading that as cosmetic would hide it from a critical-only
    repair run — the one place the ranking has to be trusted.
    """
    for key in ("severity", "level", "impact", "priority"):
        if key in entry:
            return normalise_severity(entry[key])
    return normalise_severity(None)


def parse_single(text: str, row: int, graded: bool = True) -> JudgeResult:
    """
    A one-row re-check.

    The row number is supplied because a model reviewing a single line often
    drops it from the reply, and the caller already knows which row it asked
    about. Anything the model *did* number is trusted over that.
    """
    result = parse_evaluation(text, graded=graded)
    if result.unparsed or not result.issues:
        return result
    if row not in result.issues and len(result.issues) == 1:
        only = next(iter(result.issues.values()))
        result.issues = {row: RowIssue(row=row, error_types=only.error_types,
                                       comment=only.comment,
                                       severity=only.severity)}
    return result


def parse_fixed_line(text: str) -> str:
    """
    One repaired line, from a reply that may still carry its prefix.

    Same cleanup `recovery._clean` does: strip a leading `[N]` or `3.`, take the
    first non-empty line, and undo the placeholder encoding the prompt applied.
    A model that adds "Corrected:" is trimmed too.
    """
    from ..parsing.placeholders import decode_placeholders

    body = (text or "").strip()
    if not body:
        return ""
    body = re.sub(r"^(corrected|output|result)\s*:\s*", "", body, flags=re.IGNORECASE)
    for line in body.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        candidate = re.sub(r"^(\[\d+\]\s*)+", "", candidate).strip()
        candidate = re.sub(r"^\d+[.)]\s+", "", candidate).strip()
        if candidate:
            return decode_placeholders(candidate)
    return ""


# ── JSON salvage ──────────────────────────────────────────────────────────────

def _as_row(value) -> int | None:
    """`7`, `"7"`, `"[7]"`, `"row 7"` → 7. Anything else → None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    match = re.search(r"\d+", str(value or ""))
    return int(match.group()) if match else None


def _extract_json(text: str) -> dict | None:
    """
    Whole body, then fenced, then the widest `{...}` span — and never raises.

    A reply can also be valid JSON that strict `json.loads` rejects for small
    reasons: trailing commas in an array or object, single-quoted strings, a
    code fence the fence-regex missed, or a stray final line after the object.
    Those are recoverable without guessing anything, so the widest span is
    re-tried through a lenient reader before the reply is declared unparsed.
    Prose without a JSON shape still comes back `None` — a verdict is never
    invented out of free text.
    """
    body = (text or "").strip()
    if not body:
        return None
    for candidate in _candidates(body):
        data = _loads(candidate)
        if data is None:
            # The span is real JSON that failed strict parsing. Repair only
            # mechanical damage (trailing commas, quotes, fences) and try the
            # *same span* again — leniency is salvage, not archaeology.
            data = _loads_lenient(candidate)
        if data is None:
            continue
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            # A bare list of entries is close enough to what was asked for.
            return {"lines": data}
    return None


def _loads(candidate: str):
    try:
        return json.loads(candidate)
    except ValueError:
        return None


def _loads_lenient(candidate: str) -> dict | list | None:
    """
    Strict JSON, then a sequence of small mechanical repairs — never invention.

    Each repair is safe (it changes only quoting or punctuation, never values)
    and the parse is re-attempted after each one, so an object with two
    problems — trailing commas *and* single quotes — still gets recovered.
    """
    text = candidate
    if not text:
        return None

    # A fence the `_candidates` regex missed (different fence language, or a
    # fence with trailing whitespace after the marker).
    if "```" in text:
        text = "\n".join(
            line for line in text.splitlines()
            if not line.strip().startswith("```")
        ).strip()
        data = _loads(text)
        if data is not None:
            return data

    # The JSON is wrapped in "here is my review:" prose but the widest span
    # could not be cut because the closing brace is followed by one more line.
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        span = text[start:end + 1]
        data = _loads(span)
        if data is not None:
            return data

    data = _loads(_strip_trailing_commas(text))
    if data is not None:
        return data

    # Model-produced JSON with single quotes instead of double quotes, or a
    # Python-ish dict (`True`/`False`/`None`).
    unquoted = re.sub(r"'", '"', text)
    unquoted = unquoted.replace("True", "true").replace("False", "false")
    unquoted = unquoted.replace("None", "null")
    data = _loads(_strip_trailing_commas(unquoted))
    if data is not None:
        return data

    # Newline-delimited JSON or a list of bare JSON entries, one per line —
    # the shape models produce when the prompt asked for an object and they
    # answer with one object per row.
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) > 1:
        parsed = [_loads(line) for line in lines]
        if parsed and all(item is not None for item in parsed):
            return {"lines": parsed}
    return None


def _strip_trailing_commas(text: str) -> str:
    """Remove `,` right before `}` or `]` — the classic model-generated slip."""
    return re.sub(r",(\s*[}\]])", r"\1", text)


def _candidates(body: str):
    yield body
    fenced = re.search(r"```(?:json)?\s*(.*?)```", body, re.DOTALL)
    if fenced:
        yield fenced.group(1).strip()
    start, end = body.find("{"), body.rfind("}")
    if 0 <= start < end:
        yield body[start:end + 1]
    start, end = body.find("["), body.rfind("]")
    if 0 <= start < end:
        yield body[start:end + 1]
