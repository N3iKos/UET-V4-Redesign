"""
core/multi_file.py — Multi-file draft state management and smart shared rate limit allocation.

Rules:
1. File at index 0 is the LEADER. Settings made to the Leader automatically
   propagate to non-leader files that have not been explicitly customized.
2. Non-leader files can be independently customized (overridden). Once a key
   is overridden on a non-leader file, leader changes to that key no longer
   overwrite it, and non-leader changes NEVER propagate to the leader or other files.
3. Smart Shared Rate Limit:
   - If files use DIFFERENT models/providers: each file runs Individual with its own full rate limit.
   - If 2+ files use the SAME model/provider: they automatically SHARE the rate limit / workers
     so that the API account is never overloaded with duplicate concurrent worker pools.
"""

from __future__ import annotations

import os
from typing import Any


class MultiFileDraftManager:
    """Manages drafts for 1 to 4 files with Leader inheritance and custom overrides."""

    def __init__(self, paths: list[str]) -> None:
        self.paths = [os.path.abspath(p) for p in paths] if paths else []
        self.leader_index = 0
        self.drafts: list[dict[str, Any]] = [
            {"path": p, "src_path": p, "out_path": ""} for p in self.paths
        ]
        self._overrides: list[set[str]] = [set() for _ in self.paths]

    def get_draft(self, index: int) -> dict[str, Any]:
        if 0 <= index < len(self.drafts):
            return self.drafts[index]
        return {}

    def update_leader(self, **kwargs) -> None:
        """Update the leader draft and propagate non-overridden fields to followers."""
        if not self.drafts:
            return
        leader = self.drafts[self.leader_index]
        leader.update(kwargs)
        for i in range(len(self.drafts)):
            if i == self.leader_index:
                continue
            follower = self.drafts[i]
            overridden = self._overrides[i]
            for k, v in kwargs.items():
                if k not in overridden:
                    if k in ("num_rows", "file_total_rows") and "user_row_limit" in kwargs:
                        continue
                    follower[k] = v

    def update_file(self, index: int, **kwargs) -> None:
        """Update a specific file. If it's not the leader, mark updated fields as custom overrides."""
        if not (0 <= index < len(self.drafts)):
            return
        if index == self.leader_index:
            self.update_leader(**kwargs)
            return
        target = self.drafts[index]
        target.update(kwargs)
        self._overrides[index].update(kwargs.keys())

    def reset_file_override(self, index: int, key: str | None = None) -> None:
        """Reset custom overrides so the file re-inherits from the leader."""
        if not (0 <= index < len(self.drafts)) or index == self.leader_index:
            return
        leader = self.drafts[self.leader_index]
        if key is None:
            self._overrides[index].clear()
            for k, v in leader.items():
                if k not in ("path", "src_path", "out_path", "full_path", "map_path", "num_rows", "file_total_rows"):
                    self.drafts[index][k] = v
        else:
            self._overrides[index].discard(key)
            if key in leader:
                self.drafts[index][key] = leader[key]


def distribute_workers_smart(drafts: list[dict[str, Any]]) -> list[int]:
    """
    Smart shared rate limit worker allocator:
    - Group files by (provider_id, model_id).
    - If a model is used by only 1 file: Individual mode (full worker allocation).
    - If a model is used by 2+ files: Shared mode (total workers for that model split equally across them).
    """
    if not drafts:
        return []

    # Map model key -> list of draft indices
    groups: dict[tuple[str, str], list[int]] = {}
    for idx, d in enumerate(drafts):
        prov = str(d.get("provider_id", "") or "").strip()
        model = str(d.get("model_id", "") or "").strip()
        groups.setdefault((prov, model), []).append(idx)

    allocations = [1] * len(drafts)

    for (prov, model), indices in groups.items():
        if len(indices) == 1:
            # Individual mode
            idx = indices[0]
            w = max(1, int(drafts[idx].get("n_workers", 1) or 1))
            allocations[idx] = w
        else:
            # Shared mode: find the model's total worker capacity
            # Use the max of workers requested in the group (usually leader's or highest setting)
            total_workers = max(
                max(1, int(drafts[idx].get("n_workers", 1) or 1))
                for idx in indices
            )
            # Split equally
            count = len(indices)
            base_share = max(1, total_workers // count)
            remainder = total_workers % count
            for i, idx in enumerate(indices):
                # Distribute remainder to first files
                allocations[idx] = base_share + (1 if i < remainder else 0)

    return allocations
