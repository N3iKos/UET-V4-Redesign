"""
providers/models.py — Provider topology: Provider -> Connection -> ModelEntry.

This is the spine of v4: provider topology is *data* the user creates at
runtime, never code. Nothing in this module knows the name of any real
provider — and that now includes reasoning: which models can think, and how
that is switched, is metadata on ModelEntry rather than a table in the source
(ADR-003). A model with no such metadata sends no reasoning parameter at all.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Literal

from .ratelimit import RateLimitPreset

Strategy   = Literal["round_robin", "fallback"]
TestStatus = Literal["untested", "ok", "error"]
# How a model lets reasoning be controlled (FEATURE-004 FR-401):
#   toggle    — can be switched on and off, via reasoning_parameter_path
#   effort    — only a level is offered (reasoning_effort: low/medium/high)
#   always_on — reasoning cannot be turned off; send no parameter for it
#   none      — no reasoning at all (the safe default for an unknown model)
ReasoningControl = Literal["toggle", "effort", "always_on", "none"]

REASONING_CONTROLS: tuple[str, ...] = ("toggle", "effort", "always_on", "none")

# Keys the vault may carry for one reasoning setting. Both spellings are read:
# Doc 04 writes snake_case, Doc 09's sample writes camelCase, and a vault
# hand-edited from either must load (AC-404).
_REASONING_ALIASES: dict[str, tuple[str, ...]] = {
    "supported":       ("supported", "is_supported"),
    "control":         ("control", "reasoning_control"),
    "parameter_path":  ("parameter_path", "parameterPath", "path"),
    "on_value":        ("on_value", "onValue"),
    "off_value":       ("off_value", "offValue"),
    "default_enabled": ("default_enabled", "defaultEnabled", "default"),
    "options":         ("options", "levels", "reasoning_options"),
}


def _pick(data: dict, key: str, fallback=None):
    """First present alias for one reasoning key, else `fallback`."""
    for alias in _REASONING_ALIASES[key]:
        if alias in data:
            return data[alias]
    return fallback


def new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class ModelEntry:
    id:                   str                    # model id used in API calls
    display_name:         str = ""               # optional label; falls back to id
    preset:               RateLimitPreset = field(default_factory=RateLimitPreset.default)
    prompt_format:        Literal["plain", "json"] = "plain"   # per-model opt-in
    # ── Reasoning metadata (FEATURE-004) ──────────────────────────────────────
    # Capability and controllability are separate questions: a model can reason
    # without letting you turn it off. Everything below is *user data* stored in
    # the vault — there is no table of model capabilities anywhere in the code
    # (ADR-003), and an unconfigured model sends no reasoning parameter at all.
    reasoning_supported:      bool = False
    reasoning_control:        ReasoningControl = "none"
    reasoning_default:        bool = False        # what Quick mode uses
    reasoning_parameter_path: str = ""            # e.g. chat_template_kwargs.enable_thinking
    reasoning_on_value:       object = True       # inserted verbatim; bool/int/str
    reasoning_off_value:      object = False
    reasoning_options:        list[str] = field(default_factory=list)   # effort levels
    # Transient test state — persisted for convenience, safe to lose.
    last_test_latency_ms: float | None = None
    last_test_status:     TestStatus = "untested"
    last_test_error:      str = ""

    @property
    def label(self) -> str:
        return self.display_name or self.id

    @property
    def supports_reasoning(self) -> bool:
        """
        Derived, and kept for every caller that predates the metadata: a model
        supports reasoning when its control says so. `reasoning_supported` is
        honoured as an explicit "no" so a half-filled form cannot claim
        otherwise.
        """
        return self.reasoning_control != "none" and self.reasoning_supported

    @property
    def reasoning_toggleable(self) -> bool:
        """True only when reasoning can genuinely be switched off (FR-404)."""
        return self.supports_reasoning and self.reasoning_control == "toggle"

    def reasoning_dict(self) -> dict:
        """The `reasoning` block as it is stored in the vault (Doc 04)."""
        return {
            "supported": self.reasoning_supported,
            "control": self.reasoning_control,
            "parameter_path": self.reasoning_parameter_path,
            "on_value": self.reasoning_on_value,
            "off_value": self.reasoning_off_value,
            "default_enabled": self.reasoning_default,
            "options": list(self.reasoning_options),
        }

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "preset": self.preset.to_dict(),
            # Written for one release only, so a vault saved by this build can
            # still be read by the previous one. The nested block below is the
            # source of truth on the way back in.
            "supports_reasoning": self.supports_reasoning,
            "reasoning_options": list(self.reasoning_options),
            "reasoning": self.reasoning_dict(),
            "prompt_format": self.prompt_format,
            "last_test_latency_ms": self.last_test_latency_ms,
            "last_test_status": self.last_test_status,
            "last_test_error": self.last_test_error,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ModelEntry":
        """
        Tolerant on the way in (AC-404). Three shapes have to load:

        * a v1 vault with no reasoning fields at all → control "none", no
          parameter is ever sent;
        * a v1 vault with the old `supports_reasoning: true` → control
          "effort", which is what that flag actually drove (a `reasoning_effort`
          level), so existing behaviour survives untouched;
        * a vault written by this build, whose nested `reasoning` block wins
          over the legacy flag.
        """
        legacy_supported = bool(data.get("supports_reasoning", False))
        legacy_options = [str(v) for v in (data.get("reasoning_options") or [])]

        block = data.get("reasoning")
        if isinstance(block, dict):
            control = str(_pick(block, "control", "none") or "none").strip().lower()
            if control not in REASONING_CONTROLS:
                control = "none"
            supported = _pick(block, "supported")
            supported = bool(supported) if supported is not None else control != "none"
            options = _pick(block, "options")
            options = (
                [str(v) for v in options] if isinstance(options, (list, tuple))
                else legacy_options
            )
            return cls(
                id=data["id"],
                display_name=data.get("display_name", ""),
                preset=RateLimitPreset.from_dict(data.get("preset")),
                prompt_format=data.get("prompt_format", "plain"),
                reasoning_supported=supported,
                reasoning_control=control,          # type: ignore[arg-type]
                reasoning_default=bool(_pick(block, "default_enabled", False)),
                reasoning_parameter_path=str(_pick(block, "parameter_path", "") or ""),
                reasoning_on_value=_pick(block, "on_value", True),
                reasoning_off_value=_pick(block, "off_value", False),
                reasoning_options=options,
                last_test_latency_ms=data.get("last_test_latency_ms"),
                last_test_status=data.get("last_test_status", "untested"),
                last_test_error=data.get("last_test_error", ""),
            )

        return cls(
            id=data["id"],
            display_name=data.get("display_name", ""),
            preset=RateLimitPreset.from_dict(data.get("preset")),
            prompt_format=data.get("prompt_format", "plain"),
            reasoning_supported=legacy_supported,
            reasoning_control="effort" if legacy_supported else "none",
            reasoning_default=False,
            reasoning_parameter_path="",
            reasoning_options=legacy_options,
            last_test_latency_ms=data.get("last_test_latency_ms"),
            last_test_status=data.get("last_test_status", "untested"),
            last_test_error=data.get("last_test_error", ""),
        )


@dataclass
class Connection:
    id:               str = field(default_factory=new_id)
    name:             str = ""
    api_key:          str | None = None    # None/"" = no auth (local providers)
    is_valid:         bool = False
    validation_error: str = ""
    models:           list[ModelEntry] = field(default_factory=list)
    # Optional per-connection rate limit (UET_UX §3). None = the connection has
    # no limit of its own and each model's own preset paces it, the pre-feature
    # behaviour. When set, and the provider's scope is "independent", this is the
    # allowance this connection contributes to the pool's capacity.
    rate_limit:       RateLimitPreset | None = None

    def find_model(self, model_id: str) -> ModelEntry | None:
        return next((m for m in self.models if m.id == model_id), None)

    def masked_key(self) -> str:
        key = (self.api_key or "").strip()
        if not key:
            return "(no key)"
        if len(key) > 8:
            return f"{key[:4]}{'*' * (len(key) - 8)}{key[-4:]}"
        return "****"

    def to_dict(self) -> dict:
        data = {
            "id": self.id,
            "name": self.name,
            "api_key": self.api_key,
            "is_valid": self.is_valid,
            "validation_error": self.validation_error,
            "models": [m.to_dict() for m in self.models],
        }
        if self.rate_limit is not None:
            data["rate_limit"] = self.rate_limit.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Connection":
        rate_limit = None
        if isinstance(data.get("rate_limit"), dict):
            rate_limit = RateLimitPreset.from_dict(data["rate_limit"])
        return cls(
            id=data.get("id") or new_id(),
            name=data.get("name", ""),
            api_key=data.get("api_key"),
            is_valid=bool(data.get("is_valid", False)),
            validation_error=data.get("validation_error", ""),
            models=[ModelEntry.from_dict(m) for m in data.get("models", [])],
            rate_limit=rate_limit,
        )


@dataclass
class Provider:
    id:          str = field(default_factory=new_id)
    name:        str = ""
    base_url:    str = ""
    strategy:    Strategy = "fallback"     # inert when len(connections) < 2
    connections: list[Connection] = field(default_factory=list)
    # How per-connection rate limits combine across the provider (UET_UX §3):
    #   "independent" — each connection's limit is its own; the pool's capacity
    #                   is the sum, which is right for separate accounts/keys.
    #   "shared"      — one allowance covers every connection, right when they
    #                   are the same account behind different keys.
    rate_limit_scope: Literal["independent", "shared"] = "independent"

    @property
    def strategy_active(self) -> bool:
        return len(self.connections) >= 2

    def find_connection(self, connection_id: str) -> Connection | None:
        return next((c for c in self.connections if c.id == connection_id), None)

    def connections_with_model(self, model_id: str) -> list[Connection]:
        return [c for c in self.connections if c.find_model(model_id)]

    def all_model_ids(self) -> list[str]:
        seen: list[str] = []
        for conn in self.connections:
            for model in conn.models:
                if model.id not in seen:
                    seen.append(model.id)
        return seen

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "base_url": self.base_url,
            "strategy": self.strategy,
            "rate_limit_scope": self.rate_limit_scope,
            "connections": [c.to_dict() for c in self.connections],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Provider":
        strategy = data.get("strategy", "fallback")
        scope = data.get("rate_limit_scope", "independent")
        return cls(
            id=data.get("id") or new_id(),
            name=data.get("name", ""),
            base_url=data.get("base_url", ""),
            strategy=strategy if strategy in ("round_robin", "fallback") else "fallback",
            rate_limit_scope=scope if scope in ("independent", "shared") else "independent",
            connections=[Connection.from_dict(c) for c in data.get("connections", [])],
        )
