"""
parsing/recovery.py — ParseRecoveryPipeline.

Ported from v3.6's parser.py unchanged. Pure function, no I/O.

Stages: strict [N] prefix -> loose N./N) -> merge -> positional fallback.
Each stage short-circuits once the result is good enough.
"""

from __future__ import annotations

import json as _json
import re

from .. import config
from .placeholders import decode_placeholders


class ParseRecoveryPipeline:
    """Raw LLM text + expected row numbers -> {row_num: translated}."""

    GOOD_RATIO = config.PARSE_GOOD_RATIO

    def recover(
        self,
        text: str,
        expected_row_nums: list[int],
        source_map: dict[int, str] | None = None,
    ) -> dict[int, str]:
        n = len(expected_row_nums)
        if n == 0:
            return {}

        expected = set(expected_row_nums)

        stage1 = self._fuzzy_remap_rows(self._parse_strict(text, source_map), expected)
        if self._good(stage1, n):
            return stage1

        stage2 = self._fuzzy_remap_rows(self._parse_loose(text, expected_row_nums, source_map), expected)
        if self._good(stage2, n):
            return stage2

        merged = {**stage2, **stage1}      # strict format wins on conflict
        if self._good(merged, n):
            return merged

        return self._parse_positional(text, expected_row_nums, source_map)

    def recover_best_effort(
        self,
        text: str,
        expected_row_nums: list[int],
        source_map: dict[int, str] | None = None,
    ) -> dict[int, str]:
        """
        Everything the stages can find, with no good-enough gate.

        `recover` throws a partial batch away on purpose: half a parsed batch
        is how desync corruption starts, and the scheduler has a safe retry
        path to fall back on. Callers with no such path and the opposite need
        — the playground, where "this model only answered two of three lines"
        is the finding rather than a problem — want the partial.

        Not for the translation pipeline. Nothing here is written to a sheet.
        """
        if not expected_row_nums:
            return {}
        expected = set(expected_row_nums)
        strict = self._fuzzy_remap_rows(self._parse_strict(text, source_map), expected)
        loose = self._fuzzy_remap_rows(self._parse_loose(text, expected_row_nums, source_map), expected)
        return {**loose, **strict}          # strict format wins on conflict

    def missing_rows(self, parsed: dict[int, str], expected: list[int]) -> list[int]:
        return [rn for rn in expected if not parsed.get(rn, "").strip()]

    # ── Fuzzy row-number correction ───────────────────────────────────────────

    @staticmethod
    def _fuzzy_remap_rows(parsed: dict[int, str], expected: set[int]) -> dict[int, str]:
        """
        Repair model-mangled row numbers by last-2-digit matching
        (2780 -> 280, 9410 -> 94010). Collision-free for batches <= 50.
        """
        remapped: dict[int, str] = {}
        suffix_map: dict[str, int] = {str(e)[-2:]: e for e in expected}
        used: set[int] = set()

        for rn, val in parsed.items():
            if rn in expected:
                remapped[rn] = val
                used.add(rn)
                continue
            candidate = suffix_map.get(str(rn)[-2:])
            if candidate is not None and candidate not in used:
                remapped[candidate] = val
                used.add(candidate)
            else:
                remapped[rn] = val
        return remapped

    # ── Stages ────────────────────────────────────────────────────────────────

    def _parse_strict(self, text: str, source_map: dict[int, str] | None = None) -> dict[int, str]:
        result: dict[int, str] = {}
        # Matches [N], **[N]**, [Row N], [Line N], with optional :, -, or markdown wrappers
        pattern = r"(?:^|\n)\s*\*{0,2}\[(?:Row\s+|Line\s+)?(\d+)\]\*{0,2}[:\-]?\s*(.*?)(?=\n\s*\*{0,2}\[(?:Row\s+|Line\s+)?\d+\]|\Z)"
        for match in re.finditer(pattern, text, re.DOTALL):
            rn = int(match.group(1))
            source_text = (source_map or {}).get(rn, "")
            value = self._clean(match.group(2), source_text=source_text)
            if value:
                result[rn] = decode_placeholders(value)
        return result

    def _parse_loose(self, text: str, expected: list[int], source_map: dict[int, str] | None = None) -> dict[int, str]:
        result: dict[int, str] = {}
        expected_set = set(expected)
        # Matches 1., 1), 1:, 1 -, (1), **1.**, Row 1:, Line 1:
        pattern = r"^\s*(?:\*{0,2}|\(?)(?:\[|\b)(?:Row\s+|Line\s+)?(\d+)(?:\]|\)|\.|\:|\-|\*{0,2}\:?)\s*(.*)"
        for match in re.finditer(pattern, text, re.MULTILINE):
            rn = int(match.group(1))
            source_text = (source_map or {}).get(rn, "")
            raw_val = re.sub(r"^[\]:\-\*]+\s*", "", match.group(2))
            value = self._clean(raw_val, source_text=source_text)
            if rn in expected_set and value:
                result[rn] = decode_placeholders(value)
        return result

    def _parse_positional(self, text: str, expected: list[int], source_map: dict[int, str] | None = None) -> dict[int, str]:
        """
        Last resort: map lines by position. Only ever allowed for a single
        row — using it on a batch is exactly how desync corruption happens.
        """
        if len(expected) > 1:
            return {}
        if text.count('": "') >= 2 or (text.count('{"') >= 1 and text.count('":') >= 2):
            return {}      # JSON leaked into a plain-text parse

        lines = [line.strip() for line in text.strip().split("\n") if line.strip()]
        result: dict[int, str] = {}
        for index, rn in enumerate(expected):
            if index < len(lines):
                source_text = (source_map or {}).get(rn, "")
                value = self._clean(lines[index], source_text=source_text)
                if value:
                    result[rn] = decode_placeholders(value)
        return result

    def _clean(self, value: str, source_text: str = "") -> str:
        value = value.strip()
        value = re.sub(r"^(\[[\dN]+\]\s*)+", "", value).strip()
        # Clean leading markdown bold, colon, or dashes like ": ", "- ", "** "
        value = re.sub(r"^(?:\*{1,2}|[:\-])\s*", "", value).strip()
        # If the source text itself starts with a numbered bullet (e.g. "1. " or "2) "),
        # preserve the number in the translation! Only strip if source has no leading number.
        has_source_num = bool(re.match(r"^\d+[.)]\s+", (source_text or "").strip()))
        if not has_source_num:
            value = re.sub(r"^\d+[.)]\s+", "", value).strip()
        return value

    def _good(self, result: dict[int, str], n: int) -> bool:
        return len(result) >= n * self.GOOD_RATIO


# ── Structured-output (JSON) parsing — opt-in only ────────────────────────────

def build_json_schema(row_numbers: list[int]) -> dict:
    """
    Strict JSON schema for providers that support response_format.

    Only used when a ModelEntry explicitly opts into prompt_format="json"
    (02_DATA_MODELS.md §7 item 3) — many OpenAI-compatible servers reject it.
    """
    keys = [str(rn) for rn in row_numbers]
    return {
        "type": "object",
        "properties": {k: {"type": "string"} for k in keys},
        "required": keys,
        "additionalProperties": False,
    }


def parse_json_response(text: str, expected_row_nums: list[int]) -> dict[int, str]:
    """
    Parse a structured-output response, degrading gracefully:
    full JSON -> per-key regex (survives truncation) -> plain-text pipeline.
    """
    text = text.strip()

    if text.startswith("```"):
        text = "\n".join(
            line for line in text.splitlines() if not line.strip().startswith("```")
        ).strip()

    start, end = text.find("{"), text.rfind("}")
    candidate = text[start:end + 1] if start != -1 and end >= start else text

    try:
        data = _json.loads(candidate)
        result: dict[int, str] = {}
        for rn in expected_row_nums:
            value = str(data.get(str(rn), "")).strip()
            if value:
                result[rn] = decode_placeholders(value)
        return result
    except (_json.JSONDecodeError, AttributeError):
        pass

    result = {}
    for rn in expected_row_nums:
        match = re.search(rf'"{rn}"\s*:\s*"((?:[^"\\]|\\.)*)"', candidate)
        if match:
            try:
                value = _json.loads(f'"{match.group(1)}"').strip()
                if value:
                    result[rn] = decode_placeholders(value)
            except Exception:
                pass

    if len(result) >= len(expected_row_nums) * 0.3:
        return result

    return ParseRecoveryPipeline().recover(text, expected_row_nums)
