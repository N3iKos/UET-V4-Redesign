"""
providers/reasoning.py — The one place that knows how to ask a model to think.

A model's reasoning metadata is *data* (ModelEntry, stored in the vault), not a
table in the source: providers are user-defined in v4 and their reasoning
parameters differ wildly. Cloudflare Workers AI puts Gemma's switch at
`chat_template_kwargs.enable_thinking` and Kimi's at
`chat_template_kwargs.thinking`; OpenAI-style endpoints take a
`reasoning_effort` level; plenty of models cannot be switched off at all.

Two rules make that safe (ADR-003):

  * **never guess.** A model whose control is `always_on`, `none`, or whose
    `toggle` has no parameter path sends *no* reasoning parameter. A made-up
    parameter is worse than none: the server rejects the whole request.
  * **capability is not controllability.** `reasoning_effort` sets a level; it
    does not promise the level can be zero. So `effort` never grows a fake
    off switch (FR-405).

Priority for the effective value: the user's per-run override (Full mode or
Judge) → the model's `reasoning_default` → the safe fallback of sending
nothing (FR-403).

Pure functions. No HTTP, no printing, and nothing here raises: a malformed
path or an unknown control degrades to `{}`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Kept in sync with providers.models.REASONING_CONTROLS, but spelled out here
# so this module stays importable on its own (client.py depends on it).
CONTROLS: tuple[str, ...] = ("toggle", "effort", "always_on", "none")

# The parameter an `effort` model takes. This is the *only* hardcoded key in
# the module, and it is an OpenAI API field rather than a model name.
EFFORT_KEY = "reasoning_effort"

# Levels that mean "do not send the field at all" rather than a real level.
_EFFORT_OFF = ("", "none", "off")


@dataclass
class ReasoningConfig:
    """
    One model's reasoning metadata, decoupled from ModelEntry so the playground
    and the Judge can build a config from a plain dict just as easily.

    `level` is not stored on the model: it is the effort chosen for this run
    (wizard, or the model's own default list).
    """

    supported:       bool = False
    control:         str = "none"
    parameter_path:  str = ""
    on_value:        Any = True
    off_value:       Any = False
    default_enabled: bool = False
    options:         list[str] = field(default_factory=list)
    level:           str = ""

    @classmethod
    def coerce(cls, data: "ReasoningConfig | dict | None") -> "ReasoningConfig":
        """Accept a config, a raw dict (either spelling), or nothing."""
        if isinstance(data, cls):
            return data
        if not isinstance(data, dict):
            return cls()

        def pick(*names, fallback=None):
            for name in names:
                if name in data:
                    return data[name]
            return fallback

        control = str(pick("control", "reasoning_control", fallback="none") or "none")
        control = control.strip().lower()
        if control not in CONTROLS:
            control = "none"
        supported = pick("supported", "is_supported")
        options = pick("options", "levels", "reasoning_options", fallback=[])
        return cls(
            supported=bool(supported) if supported is not None else control != "none",
            control=control,
            parameter_path=str(
                pick("parameter_path", "parameterPath", "path", fallback="") or ""
            ),
            on_value=pick("on_value", "onValue", fallback=True),
            off_value=pick("off_value", "offValue", fallback=False),
            default_enabled=bool(
                pick("default_enabled", "defaultEnabled", "default", fallback=False)
            ),
            options=[str(v) for v in options] if isinstance(options, (list, tuple)) else [],
            level=str(pick("level", "effort", "reasoning_effort", fallback="") or ""),
        )

    # ── Questions the UI asks ─────────────────────────────────────────────────

    @property
    def is_supported(self) -> bool:
        return self.supported and self.control != "none"

    @property
    def toggleable(self) -> bool:
        """Show a reasoning on/off toggle? Only when it is genuinely one."""
        return self.is_supported and self.control == "toggle"

    @property
    def effort_based(self) -> bool:
        return self.is_supported and self.control == "effort"


@dataclass
class ResolvedReasoning:
    """
    What this request will actually do about reasoning.

    `enabled` is the decision; `active` is whether the model will be thinking
    (an `always_on` model is active even though nothing was requested), which
    is what the Activity Panel needs for its `think` phase and nothing else —
    the reasoning *content* is never surfaced (FR-409 / NFR-104).
    """

    config:  ReasoningConfig = field(default_factory=ReasoningConfig)
    enabled: bool = False
    active:  bool = False

    def payload(self) -> dict:
        """The body fragment to merge into the request (may be empty)."""
        return build_payload(self, self.config)

    @property
    def level(self) -> str:
        return self.config.level if self.config.effort_based else ""


# ── Resolution ────────────────────────────────────────────────────────────────

def resolve(
    config: ReasoningConfig | dict | None,
    override: bool | None = None,
    level: str | None = None,
) -> ResolvedReasoning:
    """
    Decide the effective reasoning state: override → model default → off.

    `override` is the user's per-run choice (`JobConfig.reasoning_enabled`,
    or the Judge setup); `None` means "no opinion, follow the model".
    `level` overrides the effort level for this run.
    """
    cfg = ReasoningConfig.coerce(config)
    if level is not None:
        cfg = ReasoningConfig(**{**cfg.__dict__, "level": str(level or "")})

    if not cfg.is_supported:
        # Unknown or unsupported: send nothing, claim nothing (FR-407).
        return ResolvedReasoning(config=cfg, enabled=False, active=False)

    if cfg.control == "always_on":
        # It cannot be turned off, so an override is meaningless — and no fake
        # parameter is sent to pretend otherwise (FR-406).
        return ResolvedReasoning(config=cfg, enabled=True, active=True)

    if cfg.control == "effort":
        # A level, not a switch. `override=False` does not invent an off value
        # the provider never documented (FR-405).
        has_level = cfg.level.strip().lower() not in _EFFORT_OFF
        enabled = has_level or (override is True) or (
            override is None and cfg.default_enabled
        )
        return ResolvedReasoning(config=cfg, enabled=enabled, active=has_level)

    # control == "toggle"
    enabled = cfg.default_enabled if override is None else bool(override)
    return ResolvedReasoning(config=cfg, enabled=enabled, active=enabled)


def from_model(model, level: str | None = None) -> ReasoningConfig:
    """
    ReasoningConfig for a ModelEntry (or None for an unknown model).

    Duck-typed on purpose: providers.models imports nothing from here, so this
    module has no import cycle to work around.
    """
    if model is None:
        return ReasoningConfig()
    control = str(getattr(model, "reasoning_control", "none") or "none").strip().lower()
    if control not in CONTROLS:
        control = "none"
    return ReasoningConfig(
        supported=bool(getattr(model, "reasoning_supported", False)),
        control=control,
        parameter_path=str(getattr(model, "reasoning_parameter_path", "") or ""),
        on_value=getattr(model, "reasoning_on_value", True),
        off_value=getattr(model, "reasoning_off_value", False),
        default_enabled=bool(getattr(model, "reasoning_default", False)),
        options=list(getattr(model, "reasoning_options", []) or []),
        level=str(level or ""),
    )


def resolve_for(
    model,
    override: bool | None = None,
    level: str | None = None,
) -> ResolvedReasoning:
    """`resolve(from_model(model), ...)` — what every caller actually wants."""
    return resolve(from_model(model, level=level), override=override, level=level)


# ── Payload ───────────────────────────────────────────────────────────────────

def build_payload(
    enabled: bool | ResolvedReasoning,
    config: ReasoningConfig | dict | None = None,
) -> dict:
    """
    The reasoning fragment for the request body, or `{}` to send nothing.

    `enabled` may be a plain bool (the caller already decided) or a
    ResolvedReasoning (which carries its own config). Values are inserted
    verbatim — bool, number or string, whatever the provider documents.

        >>> build_payload(True, {"control": "toggle",
        ...                      "parameter_path": "chat_template_kwargs.enable_thinking",
        ...                      "on_value": True, "off_value": False})
        {'chat_template_kwargs': {'enable_thinking': True}}
    """
    if isinstance(enabled, ResolvedReasoning):
        resolved, flag = enabled, enabled.enabled
        cfg = ReasoningConfig.coerce(config if config is not None else resolved.config)
    else:
        flag = bool(enabled)
        cfg = ReasoningConfig.coerce(config)

    if not cfg.is_supported:
        return {}
    if cfg.control in ("always_on", "none"):
        return {}

    if cfg.control == "effort":
        level = (cfg.level or "").strip()
        if not level or level.lower() in _EFFORT_OFF:
            return {}
        return {EFFORT_KEY: level}

    # control == "toggle": nested path, or nothing at all.
    keys = split_path(cfg.parameter_path)
    if not keys:
        return {}
    return _nest(keys, cfg.on_value if flag else cfg.off_value)


def split_path(parameter_path: str) -> list[str]:
    """
    `"chat_template_kwargs.enable_thinking"` → `["chat_template_kwargs",
    "enable_thinking"]`. Empty segments are dropped; a path that reduces to
    nothing means "no parameter" rather than an error (FR-407).
    """
    return [part for part in str(parameter_path or "").strip().split(".") if part]


def merge_payload(body: dict, fragment: dict) -> dict:
    """
    Merge the fragment into `body` in place, deep for nested dicts so a
    `chat_template_kwargs` the caller already set is extended rather than
    replaced.
    """
    for key, value in (fragment or {}).items():
        existing = body.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merge_payload(existing, value)
        else:
            body[key] = value
    return body


def describe(resolved: ResolvedReasoning) -> str:
    """One short line for the debug log — never the reasoning content."""
    cfg = resolved.config
    if not cfg.is_supported:
        return "reasoning: unsupported (no parameter sent)"
    if cfg.control == "effort":
        return f"reasoning: effort={cfg.level or 'off'}"
    state = "on" if resolved.enabled else "off"
    where = cfg.parameter_path or "(no path)"
    return f"reasoning: {cfg.control}={state} via {where}"


def _nest(keys: list[str], value: Any) -> dict:
    fragment: dict = {}
    cursor = fragment
    for key in keys[:-1]:
        cursor[key] = {}
        cursor = cursor[key]
    cursor[keys[-1]] = value
    return fragment
