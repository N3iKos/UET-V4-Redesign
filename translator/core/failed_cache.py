from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path


def failed_cache_path(out_path: str, dst_col: int) -> str:
    clean_p = out_path
    if ".uet_cache" in clean_p:
        clean_p = re.sub(r"(\.[0-9a-f]{8,})?\.unique", "", clean_p)
    p = Path(clean_p).resolve()
    if p.name == ".uet_cache":
        cache_dir = p
    else:
        cache_dir = None
        for parent in p.parents:
            if parent.name == ".uet_cache":
                cache_dir = parent
                break
        if cache_dir is None:
            cache_dir = p.parent / ".uet_cache"
    base = p.stem
    return str(cache_dir / f"{base}_col{dst_col}_failed.json")


def _atomic_write_json(path: str, data: dict) -> None:
    cache_dir = os.path.dirname(os.path.abspath(path))
    os.makedirs(cache_dir, exist_ok=True)
    tf = tempfile.NamedTemporaryFile("w", dir=cache_dir, delete=False, encoding="utf-8", suffix=".tmp")
    temp_name = tf.name
    try:
        json.dump(data, tf, indent=2)
        tf.flush()
        tf.close()
        os.replace(temp_name, path)
    except Exception:
        if os.path.exists(temp_name):
            try:
                os.remove(temp_name)
            except OSError:
                pass
        raise


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
    existing = load_failed_rows(out_path, dst_col)
    existing.update(failed_rows)
    data = {
        "file": os.path.abspath(out_path),
        "dst_col": dst_col,
        "failed_rows": sorted(existing),
        "updated_at": time.time(),
    }
    _atomic_write_json(path, data)


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
    _atomic_write_json(path, data)
