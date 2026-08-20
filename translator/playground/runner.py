"""
playground/runner.py — Send one job to several models at once.

Deliberately *not* built on Scheduler/RetryExecutor/ConnectionPool. Those
exist to spread a long batch over connections, park failing ones on cooldowns
and pace against a rate limit; none of that belongs in an interactive
comparison, where a failure is an answer rather than something to hide. A
slot that fails keeps the provider's own words and is shown alongside the
ones that worked.

What it does reuse is the part that matters for the comparison to mean
anything: the same prompt builder and the same output parser the real
translation path uses, so a model is judged on the job it would actually be
given.

It is also the one place reasoning is *observable*. Each result records what
was asked of the model (the exact payload fragment) alongside what the model
actually did (reasoning content, and reasoning tokens if the server itemises
them), because a misconfigured toggle looks exactly like a working one until
those two are shown side by side.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from ..core.models import ProviderError
from ..parsing.prompts import build_translate_prompt
from ..parsing.recovery import ParseRecoveryPipeline

MAX_SLOTS = 3

# Long enough for a slow model on a big paste, short enough that a dead
# endpoint does not hold the screen for the two minutes a batch job allows.
REQUEST_TIMEOUT = 90.0

# A comparison is a handful of lines, not a batch. Cap generation so one
# runaway model cannot burn the budget the other two were sharing.
MAX_COMPLETION_TOKENS = 4096


@dataclass
class Slot:
    """One model in the comparison. Three ids, because a model id alone is
    not unique — the same id can sit on several connections and providers."""
    provider_id: str
    connection_id: str
    model_id: str
    provider_name: str = ""
    connection_name: str = ""
    letter: str = ""            # A / B / C — what the judge sees instead of a name

    @property
    def label(self) -> str:
        return self.model_id

    @property
    def where(self) -> str:
        parts = [p for p in (self.provider_name, self.connection_name) if p]
        return " · ".join(parts)

    def to_pref(self) -> dict:
        return {"provider_id": self.provider_id,
                "connection_id": self.connection_id,
                "model_id": self.model_id}


@dataclass
class SlotResult:
    slot: Slot
    rows: dict[int, str] = field(default_factory=dict)
    missing: list[int] = field(default_factory=list)
    raw: str = ""
    error: str = ""
    latency_ms: float = 0.0
    tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    positional: bool = False    # the model ignored [N]; lines matched by order
    # What the model was asked to do about reasoning, and what it actually did.
    # Both are needed: the request proves the parameter went out, the reply
    # proves the server acted on it. A toggle that is configured wrongly looks
    # identical to one that works until you can see the two side by side.
    reasoning: str = ""
    reasoning_tokens: int = 0
    reasoning_requested: bool = False   # a reasoning parameter was sent, on
    reasoning_control: str = "none"     # toggle | effort | always_on | none
    reasoning_detail: str = ""          # the exact payload fragment, for the UI

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def thought(self) -> bool:
        """Did the model demonstrably reason on this call?"""
        return bool(self.reasoning.strip()) or self.reasoning_tokens > 0

    @property
    def reasoning_verdict(self) -> str:
        """
        One phrase for the column header: did reasoning do what was asked?

        Blunt on purpose. "on, no trace" is the interesting case — the parameter
        was accepted and the model still did not think, which usually means the
        path or the value is wrong for that model.
        """
        if self.reasoning_control == "none":
            return "think n/a"
        if self.reasoning_control == "always_on":
            return "think always" + ("" if self.thought else ", no trace")
        if self.reasoning_requested:
            return "think on" + ("" if self.thought else ", no trace")
        return "think off" + (" — but it did" if self.thought else "")

    @property
    def tokens_per_second(self) -> float:
        seconds = self.latency_ms / 1000.0
        if seconds <= 0 or not self.completion_tokens:
            return 0.0
        return self.completion_tokens / seconds


def letter_for(index: int) -> str:
    return chr(ord("A") + index)


def _cheapest_level(model) -> str:
    """
    The lowest effort level a model lists, for a comparison run.

    A comparison is about output quality per second, so it should not pay for
    the deepest reasoning a model offers. `low` is preferred when the model
    lists it; otherwise the first level it declares. Nothing is invented for a
    model that declares none.
    """
    if model is None or getattr(model, "reasoning_control", "none") != "effort":
        return ""
    levels = [str(v) for v in (getattr(model, "reasoning_options", None) or [])]
    if not levels:
        return ""
    return "low" if "low" in levels else levels[0]


def run_compare(
    registry,
    slots: list[Slot],
    rows: list[tuple[int, str]],
    src_lang: str,
    tgt_lang: str,
    style_prompt: str,
    on_result: Callable[[SlotResult], None] | None = None,
    stop_event: threading.Event | None = None,
    reasoning_override: bool | None = None,
) -> list[SlotResult]:
    """
    Run every slot concurrently and return the results in slot order.

    `on_result` fires from a worker thread as each slot lands, so the screen
    can fill in the fastest model without waiting for the slowest. Blocking.

    `reasoning_override` forces reasoning on or off for every slot that can be
    told (`control == "toggle"`); `None` follows each model's own default. This
    is what makes the comparison usable for checking a reasoning setup: run the
    same lines twice, once each way, and the difference — or the absence of one
    — is the answer.
    """
    expected = [number for number, _text in rows]
    prompt = build_translate_prompt(
        rows=rows, src_lang=src_lang, tgt_lang=tgt_lang, style_prompt=style_prompt
    )

    def run_one(slot: Slot) -> SlotResult:
        if stop_event is not None and stop_event.is_set():
            return SlotResult(slot=slot, error="Stopped before this model was reached.")
        return _run_slot(registry, slot, prompt, expected, reasoning_override)

    results: list[SlotResult] = []
    with ThreadPoolExecutor(max_workers=max(1, len(slots))) as pool:
        for result in pool.map(run_one, slots):
            results.append(result)
            if on_result is not None:
                on_result(result)
    return results


def _run_slot(
    registry,
    slot: Slot,
    prompt: str,
    expected: list[int],
    reasoning_override: bool | None = None,
) -> SlotResult:
    started = time.time()
    try:
        provider = registry.get_provider(slot.provider_id)
        connection = registry.get_connection(slot.provider_id, slot.connection_id)
        model = connection.find_model(slot.model_id)
    except Exception as exc:
        return SlotResult(slot=slot, error=f"{type(exc).__name__}: {exc}")

    from ..providers.client import ProviderClient
    from ..providers.reasoning import describe, resolve_for

    # Resolved once, so the result can report what was actually asked for
    # rather than what the UI hoped for.
    resolved = resolve_for(
        model, override=reasoning_override, level=_cheapest_level(model)
    )
    payload = resolved.payload()
    control = resolved.config.control if resolved.config.is_supported else "none"

    client = ProviderClient(provider.base_url, connection.api_key, timeout=REQUEST_TIMEOUT)
    try:
        reply = client.generate(
            model=slot.model_id,
            prompt=prompt,
            # The model's own metadata decides unless the screen overrode it:
            # a toggle model follows its default, an effort model gets the
            # cheapest level it lists, and a model with no metadata is sent no
            # reasoning parameter at all.
            reasoning=resolved,
            max_completion_tokens=MAX_COMPLETION_TOKENS,
        )
    except ProviderError as exc:
        # The provider's own words, verbatim — that is usually the whole
        # answer to "why did this model do worse".
        return SlotResult(
            slot=slot,
            error=exc.response_text.strip() or str(exc),
            latency_ms=(time.time() - started) * 1000.0,
            reasoning_requested=resolved.enabled,
            reasoning_control=control,
            reasoning_detail=_payload_text(payload) or describe(resolved),
        )
    except Exception as exc:
        return SlotResult(slot=slot, error=f"{type(exc).__name__}: {exc}",
                          latency_ms=(time.time() - started) * 1000.0,
                          reasoning_requested=resolved.enabled,
                          reasoning_control=control,
                          reasoning_detail=_payload_text(payload))

    parsed = ParseRecoveryPipeline().recover_best_effort(reply.content, expected)
    positional = False
    if not parsed:
        parsed = _by_position(reply.content, expected)
        positional = bool(parsed)

    missing = [number for number in expected if not parsed.get(number, "").strip()]
    return SlotResult(
        slot=slot,
        rows=parsed,
        missing=missing,
        raw=reply.content,
        latency_ms=reply.latency_ms,
        tokens=reply.tokens,
        prompt_tokens=reply.prompt_tokens,
        completion_tokens=reply.completion_tokens,
        positional=positional,
        reasoning=reply.reasoning,
        reasoning_tokens=reply.reasoning_tokens,
        reasoning_requested=resolved.enabled,
        reasoning_control=control,
        reasoning_detail=_payload_text(payload),
    )


def _payload_text(payload: dict) -> str:
    """
    The reasoning fragment as it went into the body, flattened for display.

    `{"chat_template_kwargs": {"enable_thinking": true}}` reads as
    `chat_template_kwargs.enable_thinking=true` — short enough for a status line
    and specific enough to spot a wrong path.
    """
    if not payload:
        return ""

    def walk(node, prefix=""):
        for key, value in node.items():
            path = f"{prefix}{key}"
            if isinstance(value, dict):
                yield from walk(value, f"{path}.")
            else:
                shown = "true" if value is True else "false" if value is False else value
                yield f"{path}={shown}"

    return " ".join(walk(payload))


def _by_position(text: str, expected: list[int]) -> dict[int, str]:
    """
    Last resort for a model that ignored the [N] contract entirely and just
    returned the lines. Only when the count matches exactly, and the caller
    flags it in the UI — lining rows up by position is precisely how desync
    happens, which is why the translation path refuses to do it. Here nothing
    is written to a sheet and a visible mismatch is the useful answer.
    """
    lines = [line.strip() for line in (text or "").strip().split("\n") if line.strip()]
    if len(lines) != len(expected):
        return {}
    return dict(zip(expected, lines))
