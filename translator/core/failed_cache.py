from __future__ import annotations

import json
import os
import time


def failed_cache_path(out_path: str, dst_col: int) -> str:
    clean_p = out_path
    if ".uet_cache" in clean_p:
        import re
        clean_p = re.sub(r"\.[0-9a-f]{8,}\.unique", "", clean_p)
    folder = os.path.dirname(os.path.abspath(clean_p))
    cache_dir = os.path.join(folder, ".uet_cache")
    base = os.path.splitext(os.path.basename(clean_p))[0]
    return os.path.join(cache_dir, f"{base}_col{dst_col}_failed.json")


def load_failed_rows(out_path: str, dst_col: int) -> set[int]:
    path = failed_cache_path(out_path, dst_col)
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(int(r) for r in data.get("failed_rows", []))
    except Exception:
        return set()


def record_failed_rows(out_path: str, dst_col: int, failed_rows: set[int] | list[int]) -> None:
    if not failed_rows:
        return
    path = failed_cache_path(out_path, dst_col)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    existing = load_failed_rows(out_path, dst_col)
    existing.update(failed_rows)
    data = {
        "file": os.path.abspath(out_path),
        "dst_col": dst_col,
        "failed_rows": sorted(existing),
        "updated_at": time.time(),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def clear_failed_rows(out_path: str, dst_col: int, resolved_rows: set[int] | list[int]) -> None:
    if not resolved_rows:
        return
    path = failed_cache_path(out_path, dst_col)
    if not os.path.exists(path):
        return
    existing = load_failed_rows(out_path, dst_col)
    existing.difference_update(resolved_rows)
    if not existing:
        try:
            os.remove(path)
        except OSError:
            pass
        return
    data = {
        "file": os.path.abspath(out_path),
        "dst_col": dst_col,
        "failed_rows": sorted(existing),
        "updated_at": time.time(),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
