"""
playground/judging.py — Have a model grade the comparison.

The judge is shown the source, the style that was asked for, and each
candidate's output under a bare letter — A, B, C. It is never told which
model produced which column. That is not decoration: a judge that can see
"gpt-4o" against "qwen-0.5b" grades the names, and the whole point of running
the comparison was to find out which one is actually better at *your* text.

Structured output is requested only from a model that has opted into it,
because plenty of OpenAI-compatible servers reject `response_format`
outright. Everything else is asked for JSON in the prompt and parsed
leniently, and a judge that answers in prose is reported as such rather than
silently scored zero.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from ..core.models import ProviderError
from .runner import REQUEST_TIMEOUT, Slot, SlotResult

JUDGE_MAX_TOKENS = 4096

_SYS = (
    "You are a strict, impartial translation reviewer. "
    "You judge only what is in front of you and you never guess which system "
    "produced which candidate."
)


@dataclass
class ModelScore:
    letter: str
    score: float = 0.0
    verdict: str = ""


@dataclass
class LineNote:
    row: int
    note: str


@dataclass
class JudgeVerdict:
    scores: list[ModelScore] = field(default_factory=list)
    notes: list[LineNote] = field(default_factory=list)
    winner: str = ""
    raw: str = ""
    error: str = ""
    unparsed: bool = False       # the judge answered, but not in JSON
    latency_ms: float = 0.0
    tokens: int = 0

    @property
    def ok(self) -> bool:
        return not self.error

    def score_for(self, letter: str) -> ModelScore | None:
        return next((s for s in self.scores if s.letter == letter), None)


def build_judge_prompt(
    rows: list[tuple[int, str]],
    results: list[SlotResult],
    src_lang: str,
    tgt_lang: str,
    style_prompt: str,
) -> str:
    """`results` must already carry the letters the judge will see."""
    graded = [r for r in results if r.ok]
    letters = [r.slot.letter for r in graded]

    blocks: list[str] = []
    for number, source in rows:
        lines = [f"[{number}] SOURCE: {source}"]
        for result in graded:
            text = (result.rows.get(number) or "").strip() or "(no output for this line)"
            lines.append(f"    {result.slot.letter}: {text}")
        blocks.append("\n".join(lines))

    style_line = f"Style that was asked for: {style_prompt}\n" if style_prompt else ""
    example = ", ".join(
        f'{{"id": "{letter}", "score": 0-10, "verdict": "one sentence"}}'
        for letter in letters
    )

    return (
        f"{_SYS}\n\n"
        f"Below are {len(graded)} candidate translations of the same "
        f"{len(rows)} line(s), from {src_lang} to {tgt_lang}.\n"
        f"{style_line}\n"
        "Judge on: accuracy of meaning, natural phrasing in the target "
        "language, consistency of terminology, and whether names, numbers, "
        "placeholders and punctuation survived intact. Do not reward "
        "verbosity, and do not penalise a candidate for matching another.\n\n"
        f"{chr(10).join(blocks)}\n\n"
        "Reply with ONLY a JSON object, no prose around it, no code fence:\n"
        "{\n"
        f'  "models": [{example}],\n'
        f'  "winner": "{letters[0] if letters else "A"}",\n'
        '  "lines": [{"row": <line number>, "note": "what differs and which '
        'candidate handled it better"}]\n'
        "}\n\n"
        "Only include a line in \"lines\" where the candidates meaningfully "
        "differ — skip the ones where they agree."
    )


def _judge_level(model) -> str:
    """
    The effort level a grading model should use, from what it declares.

    Judging is the one place extra thinking is worth paying for, so `medium` is
    preferred; otherwise the model's own middle option. Nothing is invented for
    a model that declares no levels — guessing one gets the request rejected.
    """
    if model is None or getattr(model, "reasoning_control", "none") != "effort":
        return ""
    levels = [str(v) for v in (getattr(model, "reasoning_options", None) or [])]
    if not levels:
        return ""
    return "medium" if "medium" in levels else levels[len(levels) // 2]


def run_judge(registry, slot: Slot, prompt: str) -> JudgeVerdict:
    """Blocking. Never raises: a judge that fails is reported, not thrown."""
    started = time.time()
    try:
        provider = registry.get_provider(slot.provider_id)
        connection = registry.get_connection(slot.provider_id, slot.connection_id)
        model = connection.find_model(slot.model_id)
    except Exception as exc:
        return JudgeVerdict(error=f"{type(exc).__name__}: {exc}")

    from ..providers.client import ProviderClient
    from ..providers.reasoning import resolve_for

    client = ProviderClient(provider.base_url, connection.api_key, timeout=REQUEST_TIMEOUT)
    response_format = None
    if model is not None and model.prompt_format == "json":
        response_format = {"type": "json_object"}

    try:
        reply = client.generate_chat(
            model=slot.model_id,
            messages=[
                {"role": "system", "content": _SYS},
                {"role": "user", "content": prompt},
            ],
            # Grading benefits from thinking, so the model's own default is
            # used with no override; an effort model gets a mid level if it
            # lists one. A model with no metadata is sent nothing.
            reasoning=resolve_for(model, level=_judge_level(model)),
            max_completion_tokens=JUDGE_MAX_TOKENS,
            response_format=response_format,
        )
    except ProviderError as exc:
        return JudgeVerdict(
            error=exc.response_text.strip() or str(exc),
            latency_ms=(time.time() - started) * 1000.0,
        )
    except Exception as exc:
        return JudgeVerdict(error=f"{type(exc).__name__}: {exc}",
                            latency_ms=(time.time() - started) * 1000.0)

    verdict = parse_verdict(reply.content)
    verdict.latency_ms = reply.latency_ms
    verdict.tokens = reply.tokens
    return verdict


def parse_verdict(text: str) -> JudgeVerdict:
    """
    Tolerant on the way in: models fence their JSON, prefix it with "Here is
    my verdict:", or answer in prose. The first two are recoverable; the
    third is reported as unparsed with the reply kept, because an opinion in
    prose is still worth reading.
    """
    verdict = JudgeVerdict(raw=text or "")
    data = _extract_json(text or "")
    if data is None:
        verdict.unparsed = True
        return verdict

    entries = data.get("models")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            letter = str(entry.get("id") or entry.get("letter") or "").strip().upper()[:1]
            if not letter:
                continue
            verdict.scores.append(ModelScore(
                letter=letter,
                score=_as_score(entry.get("score")),
                verdict=str(entry.get("verdict") or entry.get("reason") or "").strip(),
            ))

    notes = data.get("lines")
    if isinstance(notes, list):
        for entry in notes:
            if not isinstance(entry, dict):
                continue
            try:
                row = int(entry.get("row"))
            except (TypeError, ValueError):
                continue
            note = str(entry.get("note") or "").strip()
            if note:
                verdict.notes.append(LineNote(row=row, note=note))

    winner = str(data.get("winner") or "").strip().upper()[:1]
    if winner:
        verdict.winner = winner
    elif verdict.scores:
        verdict.winner = max(verdict.scores, key=lambda s: s.score).letter

    if not verdict.scores and not verdict.notes:
        verdict.unparsed = True
    return verdict


def _as_score(value) -> float:
    """Accepts 8, "8", "8/10", 8.5 — and refuses to invent one."""
    if isinstance(value, (int, float)):
        return max(0.0, min(10.0, float(value)))
    match = re.search(r"\d+(?:\.\d+)?", str(value or ""))
    if not match:
        return 0.0
    return max(0.0, min(10.0, float(match.group())))


def _extract_json(text: str) -> dict | None:
    """Same salvage order recovery.py uses: whole body, then fenced, then the
    widest {...} span in the reply."""
    body = (text or "").strip()
    if not body:
        return None

    for candidate in _candidates(body):
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _candidates(body: str):
    yield body
    fenced = re.search(r"```(?:json)?\s*(.*?)```", body, re.DOTALL)
    if fenced:
        yield fenced.group(1).strip()
    start, end = body.find("{"), body.rfind("}")
    if 0 <= start < end:
        yield body[start:end + 1]
