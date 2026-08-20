"""
data/combos.py — Combos: named groups of models, run fallback or round-robin.

A Combo is an *entity beside Provider*, not a field inside one (FEATURE-005). It
names an ordered list of models that may come from different providers, states
how to move between them, and carries a rate limit of its own.

Why the file lives here rather than in the vault: a combo stores nothing secret.
Every member is three ids — provider, connection, model — that already exist in
the vault, plus a name, an order, a strategy and an rpm/tpm pair. Losing a
provider's API key to a forgotten master password is a security property;
losing the sentence "try the big model first, then the small one" to it would
just be annoying. So combos are plain JSON beside the vault, written atomically
(`.tmp` + `os.replace`), exactly as `presets.py` writes styles.

The consequence is deliberate and worth stating: a combo can outlive the models
it names. Resolution against the registry therefore always happens at use time
and always reports what is missing — see `scheduling/combo_pool.py` and
`Combo.describe_missing`. A dangling member is dropped from a run rather than
silently treated as a working one.

Nothing here talks to a provider, a registry or a screen. Reading never raises:
a missing or hand-mangled file means "no combos", which is a first-run state
rather than an error.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, field

from .. import config
from ..providers.ratelimit import RateLimitPreset

SCHEMA_VERSION = 1

# The pseudo-provider a combo is offered under in the model pickers. Not a real
# provider id and deliberately unable to collide with one: registry ids are
# `uuid4().hex`, so no provider can ever be named this by accident.
COMBO_PROVIDER_ID = "__combo__"
COMBO_PROVIDER_LABEL = "Model Groups"

# Same two strategies a Provider has, and for the same reasons — a third would
# need a reason of its own.
STRATEGIES = ("fallback", "round_robin")
STRATEGY_LABELS = {
    "fallback": "Fallback — try in order, move on when one fails",
    "round_robin": "Round robin — share the work across every model",
}


def new_id() -> str:
    return uuid.uuid4().hex


class ComboError(Exception):
    """An invalid combo request (empty name, duplicate name, no members)."""


@dataclass
class ComboMember:
    """
    One model in a combo, stored as the three ids that locate it.

    References rather than copies: a model's own rate limit, reasoning metadata
    and prompt format stay in the vault where they are edited, so a combo cannot
    drift out of date with the model it points at. What a combo *does* own is
    stated on the combo itself.
    """

    provider_id:   str
    connection_id: str
    model_id:      str

    @property
    def key(self) -> str:
        """
        Identity within one combo.

        The three ids together, because the same model on two different
        connections is two members — that is the whole point of a combo that
        spans keys — while the same triple twice is one.
        """
        return f"{self.provider_id}/{self.connection_id}/{self.model_id}"

    def to_dict(self) -> dict:
        return {
            "provider_id": self.provider_id,
            "connection_id": self.connection_id,
            "model_id": self.model_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ComboMember | None":
        """None for a row missing any of the three ids — one bad row is dropped
        on its own rather than taking the combo down with it."""
        if not isinstance(data, dict):
            return None
        provider_id = str(data.get("provider_id") or "").strip()
        connection_id = str(data.get("connection_id") or "").strip()
        model_id = str(data.get("model_id") or "").strip()
        if not provider_id or not connection_id or not model_id:
            return None
        return cls(provider_id, connection_id, model_id)


@dataclass
class Combo:
    """
    A named group of models with a strategy and a rate limit of its own.

    `preset` is the combo's rate limit, *not* a summary of its members'. The two
    are different questions: a member's limit describes one endpoint, while the
    combo's describes how hard the group as a whole may be pushed, and deriving
    one from the other would mean guessing whether limits add up (they do for
    separate accounts, they do not for two models on one key). So it is asked
    for, and `rpm = 0` means no limit — the same rule as everywhere else
    (CR-001).
    """

    id:       str = field(default_factory=new_id)
    name:     str = ""
    strategy: str = "fallback"
    members:  list[ComboMember] = field(default_factory=list)
    preset:   RateLimitPreset = field(
        default_factory=lambda: RateLimitPreset.custom(0, 0)
    )

    @property
    def label(self) -> str:
        return self.name or "(unnamed combo)"

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def strategy_label(self) -> str:
        return STRATEGY_LABELS.get(self.strategy, self.strategy)

    def find(self, key: str) -> ComboMember | None:
        return next((m for m in self.members if m.key == key), None)

    def add_member(self, member: ComboMember) -> bool:
        """False when the member is already in this combo — never duplicated."""
        if self.find(member.key) is not None:
            return False
        self.members.append(member)
        return True

    def remove_member(self, key: str) -> bool:
        member = self.find(key)
        if member is None:
            return False
        self.members.remove(member)
        return True

    def move_member(self, key: str, offset: int) -> bool:
        """
        Shift one member up or down. Order is what Fallback means, so this is a
        functional edit rather than a cosmetic one.
        """
        member = self.find(key)
        if member is None:
            return False
        index = self.members.index(member)
        target = index + offset
        if not 0 <= target < len(self.members):
            return False
        self.members.insert(target, self.members.pop(index))
        return True

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "strategy": self.strategy,
            "members": [m.to_dict() for m in self.members],
            "preset": self.preset.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Combo | None":
        if not isinstance(data, dict):
            return None
        name = str(data.get("name") or "").strip()
        if not name:
            return None
        strategy = str(data.get("strategy") or "fallback").strip().lower()
        members = [
            member
            for member in (
                ComboMember.from_dict(raw) for raw in (data.get("members") or [])
            )
            if member is not None
        ]
        # Deduplicate on the way in: a hand-edited file that lists the same
        # model twice would otherwise round-robin onto itself.
        seen: set[str] = set()
        unique: list[ComboMember] = []
        for member in members:
            if member.key not in seen:
                seen.add(member.key)
                unique.append(member)
        return cls(
            id=str(data.get("id") or "") or new_id(),
            name=name,
            strategy=strategy if strategy in STRATEGIES else "fallback",
            members=unique,
            # `from_dict` keeps 0 as 0, so "no limit" survives a round trip.
            preset=RateLimitPreset.from_dict(data.get("preset")),
        )


# ── Store ─────────────────────────────────────────────────────────────────────

def load_combos() -> list[Combo]:
    """
    Every saved combo, in the order they were created. Never raises.

    A malformed entry is dropped individually rather than failing the file, for
    the same reason `presets.py` does it: one bad row must not hide the rest.
    """
    try:
        with open(config.COMBOS_FILE, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return []
    entries = payload.get("combos") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return []
    combos: list[Combo] = []
    seen_ids: set[str] = set()
    for raw in entries:
        combo = Combo.from_dict(raw)
        if combo is None or combo.id in seen_ids:
            continue
        seen_ids.add(combo.id)
        combos.append(combo)
    return combos


def find_combo(combo_id: str) -> Combo | None:
    target = (combo_id or "").strip()
    if not target:
        return None
    return next((c for c in load_combos() if c.id == target), None)


def find_combo_by_name(name: str) -> Combo | None:
    target = (name or "").strip().lower()
    if not target:
        return None
    return next((c for c in load_combos() if c.name.strip().lower() == target), None)


def save_combo(combo: Combo) -> Combo:
    """
    Create or replace one combo, matched by id. Raises ComboError on an invalid
    combo and OSError when the file cannot be written.

    Validation is refused rather than repaired: a combo with no members would be
    a model choice that cannot run, and a duplicate name would make the model
    picker ambiguous — both are worth saying out loud at the moment they are
    made.
    """
    name = (combo.name or "").strip()
    if not name:
        raise ComboError("A combo needs a name.")
    if not combo.members:
        raise ComboError("A combo needs at least one model.")
    if combo.strategy not in STRATEGIES:
        raise ComboError(f"unknown strategy {combo.strategy!r}")

    combos = load_combos()
    clash = next(
        (c for c in combos if c.name.strip().lower() == name.lower() and c.id != combo.id),
        None,
    )
    if clash is not None:
        raise ComboError(f"A combo named {name!r} already exists.")

    combo.name = name
    replaced = False
    for index, existing in enumerate(combos):
        if existing.id == combo.id:
            combos[index] = combo
            replaced = True
            break
    if not replaced:
        combos.append(combo)
    _write(combos)
    return combo


def delete_combo(combo_id: str) -> bool:
    """False when there was no such combo."""
    combos = load_combos()
    remaining = [c for c in combos if c.id != combo_id]
    if len(remaining) == len(combos):
        return False
    _write(remaining)
    return True


def unique_name(base: str) -> str:
    """A name not already taken, for the 'duplicate this combo' case."""
    taken = {c.name.strip().lower() for c in load_combos()}
    stem = re.sub(r"\s+", " ", (base or "Combo").strip()) or "Combo"
    if stem.lower() not in taken:
        return stem
    suffix = 2
    while f"{stem} {suffix}".lower() in taken:
        suffix += 1
    return f"{stem} {suffix}"


def _write(combos: list[Combo]) -> None:
    """Replace the file in one step, so a crash mid-write cannot truncate it."""
    directory = os.path.dirname(config.COMBOS_FILE)
    if directory:
        os.makedirs(directory, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory or None, delete=False, suffix=".tmp"
    )
    try:
        with handle:
            json.dump(
                {"version": SCHEMA_VERSION, "combos": [c.to_dict() for c in combos]},
                handle, ensure_ascii=False, indent=2,
            )
        os.replace(handle.name, config.COMBOS_FILE)
    except Exception:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise
