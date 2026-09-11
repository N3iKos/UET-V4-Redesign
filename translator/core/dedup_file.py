from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from ..io.excel_io import load_workbook, translated_path

MAP_VERSION = 2

PREFIX_TAG_PATTERN = re.compile(r"^(\\[a-zA-Z]+\[\d+\])(.*)$", re.DOTALL)


def _extract_prefix_tag(text: str) -> tuple[str, str]:
    if not text or not isinstance(text, str):
        return "", text if text is not None else ""
    match = PREFIX_TAG_PATTERN.match(text)
    if match:
        return match.group(1), match.group(2)
    return "", text


def _source_fingerprint(path: str, src_col: int, start_row: int, num_rows: int) -> str:
    digest = hashlib.sha256()
    workbook = load_workbook(path, data_only=False, read_only=True)
    try:
        sheet = workbook.active
        last = sheet.max_row or 0
        if num_rows > 0:
            last = min(last, start_row + num_rows - 1)
        digest.update(f"{src_col}:{start_row}:{last}".encode())
        for row in range(start_row, last + 1):
            value = sheet.cell(row, src_col).value
            digest.update(b"\0")
            digest.update((str(value) if value is not None else "").encode("utf-8"))
    finally:
        workbook.close()
    return digest.hexdigest()


def _cache_dir(source: Path) -> Path:
    local = source.parent / ".uet_cache"
    try:
        local.mkdir(exist_ok=True)
        probe = local / f".{os.getpid()}.probe"
        probe.touch()
        probe.unlink()
        return local
    except OSError:
        fallback = Path.home() / ".config" / "excel-translator-v4" / "cache"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def _atomic_save_workbook(workbook, path: Path) -> None:
    suffix = path.suffix or ".tmp"
    fd, temporary = tempfile.mkstemp(prefix=path.stem + ".", suffix=suffix, dir=path.parent)
    os.close(fd)
    try:
        workbook.save(temporary)
        os.replace(temporary, path)
    except Exception:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


def _atomic_json(payload: dict, path: Path) -> None:
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    except Exception:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


def prepare_unique_file(
    src_path: str,
    src_col: int,
    dst_col: int,
    start_row: int = 2,
    num_rows: int = 0,
    full_out_path: str = "",
) -> tuple[str, str, int, int]:
    source = Path(src_path).resolve()
    if not source.exists():
        raise FileNotFoundError(src_path)
    fingerprint = _source_fingerprint(str(source), src_col, start_row, num_rows)
    workbook = load_workbook(str(source), data_only=False)
    try:
        sheet = workbook.active
        last = sheet.max_row or 0
        if num_rows > 0:
            last = min(last, start_row + num_rows - 1)
        groups: dict[str, list[int]] = {}
        representatives: list[int] = []
        row_tags: dict[str, str] = {}
        for row in range(start_row, last + 1):
            value = sheet.cell(row=row, column=src_col).value
            text = str(value).strip() if value is not None else ""
            if not text:
                continue
            tag, clean_text = _extract_prefix_tag(text)
            row_tags[str(row)] = tag
            group_key = clean_text if clean_text else text
            if group_key not in groups:
                groups[group_key] = []
                representatives.append(row)
            groups[group_key].append(row)
        total = sum(len(rows) for rows in groups.values())
        unique_count = len(representatives)
        if total == unique_count:
            return str(source), "", total, total

        cache = _cache_dir(source)
        key = fingerprint[:12]
        unique_path = cache / f"{source.stem}.{key}.unique{source.suffix}"
        map_path = cache / f"{source.stem}.{key}.map.json"
        output_source = Path(full_out_path).resolve() if full_out_path and Path(full_out_path).exists() else None
        output_book = load_workbook(str(output_source), data_only=False) if output_source else None
        try:
            unique_book = load_workbook(str(source), data_only=False)
            unique_sheet = unique_book.active
            if output_book:
                output_sheet = output_book.active
                for representative in representatives:
                    completed = ""
                    rep_text = str(sheet.cell(representative, src_col).value).strip()
                    _, rep_clean = _extract_prefix_tag(rep_text)
                    group_key = rep_clean if rep_clean else rep_text
                    for sibling in groups[group_key]:
                        value = output_sheet.cell(sibling, dst_col).value
                        if value is not None and str(value).strip():
                            completed = value
                            break
                    if completed != "":
                        unique_sheet.cell(representative, dst_col, completed)
            kept_rows = list(range(1, start_row)) + representatives
            if hasattr(unique_sheet, "_rows"):
                unique_sheet._rows = [unique_sheet._rows[row - 1] for row in kept_rows]
            else:
                delete = [row for row in range(start_row, (unique_sheet.max_row or 0) + 1) if row not in representatives]
                for row in reversed(delete):
                    unique_sheet.delete_rows(row)
            _atomic_save_workbook(unique_book, unique_path)
        finally:
            if output_book:
                output_book.close()

        rep_to_full = {}
        for index, row in enumerate(representatives):
            rep_text = str(sheet.cell(row, src_col).value).strip()
            _, rep_clean = _extract_prefix_tag(rep_text)
            group_key = rep_clean if rep_clean else rep_text
            rep_to_full[str(start_row + index)] = groups[group_key]

        payload = {
            "version": MAP_VERSION,
            "original_file": str(source),
            "source_fingerprint": fingerprint,
            "unique_file": str(unique_path.resolve()),
            "src_col": src_col,
            "dst_col": dst_col,
            "start_row": start_row,
            "num_rows": num_rows,
            "total_rows": total,
            "unique_rows": unique_count,
            "representatives": representatives,
            "row_tags": row_tags,
            "source_texts": [str(sheet.cell(row, src_col).value).strip() for row in representatives],
            "rep_to_full": rep_to_full,
        }
        _atomic_json(payload, map_path)
        return str(unique_path.resolve()), str(map_path.resolve()), total, unique_count
    finally:
        workbook.close()


def validate_mapping(map_file_path: str, unique_path: str = "") -> dict:
    with open(map_file_path, encoding="utf-8") as handle:
        mapping = json.load(handle)
    required = {"version", "original_file", "source_fingerprint", "unique_file", "src_col", "dst_col", "rep_to_full", "source_texts"}
    if not isinstance(mapping, dict) or not required.issubset(mapping) or mapping["version"] not in (1, 2):
        raise ValueError("Invalid deduplication mapping")
    original = mapping["original_file"]
    if not os.path.exists(original) or _source_fingerprint(
        original, int(mapping["src_col"]), int(mapping["start_row"]),
        int(mapping.get("num_rows", 0)),
    ) != mapping["source_fingerprint"]:
        raise ValueError("Stale deduplication mapping")
    if unique_path and os.path.abspath(unique_path) != os.path.abspath(mapping["unique_file"]):
        translated = translated_path(mapping["unique_file"])
        if os.path.abspath(unique_path) != os.path.abspath(translated):
            raise ValueError("Unique file does not match mapping")
    return mapping


def sync_unique_to_full(map_file_path: str, unique_out_path: str, full_out_path: str, dst_col: int) -> int:
    mapping = validate_mapping(map_file_path, unique_out_path)
    if int(dst_col) != int(mapping["dst_col"]):
        raise ValueError("Destination column does not match mapping")
    row_tags: dict[str, str] = mapping.get("row_tags", {})
    unique_book = load_workbook(unique_out_path, data_only=False)
    try:
        unique_sheet = unique_book.active
        texts = mapping["source_texts"]
        rows = list(mapping["rep_to_full"].items())
        if len(rows) != len(texts):
            raise ValueError("Invalid deduplication mapping rows")
        translations: list[tuple[list[int], object]] = []
        for index, (unique_row, siblings) in enumerate(rows):
            source = unique_sheet.cell(int(unique_row), int(mapping["src_col"])).value
            if (str(source).strip() if source is not None else "") != texts[index]:
                raise ValueError("Unique file order does not match mapping")
            translations.append(([int(row) for row in siblings], unique_sheet.cell(int(unique_row), dst_col).value))
    finally:
        unique_book.close()

    original = mapping["original_file"]
    destination = Path(full_out_path).resolve()
    if destination.exists():
        base = destination
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        base = Path(original)
    workbook = load_workbook(str(base), data_only=False)
    updated = 0
    try:
        sheet = workbook.active
        for siblings, value in translations:
            if value is None or not str(value).strip():
                continue
            val_str = str(value)
            _, val_clean = _extract_prefix_tag(val_str)
            for row in siblings:
                row_tag = row_tags.get(str(row), "")
                if row_tag:
                    final_val = f"{row_tag}{val_clean}"
                else:
                    final_val = value
                sheet.cell(row, dst_col, final_val)
                updated += 1
        _atomic_save_workbook(workbook, destination)
    finally:
        workbook.close()
    return updated
