"""
judge/estimator.py — How much will this cost, and where do we cut?

Two jobs, both pure and both deterministic:

  * **estimate.** Roughly four characters to a token, with a floor of 150 per
    row because a short row still carries prompt scaffolding, plus a flat
    overhead for the instructions, plus 25% headroom. These are the same
    heuristics the app already uses for `max_completion_tokens`, not
    measurements — Doc 06 "Estimasi token" states them as assumptions and
    NFR-102 accepts ±20%.
  * **split.** Group rows so each request's *estimate* stays under the cap
    (`MAX_TOKENS_PER_REQUEST`, adjustable in Settings), and never more than 50
    rows in one section, matching the batch clamp the scheduler already uses.

Deliberate consequences:

  * a single row too big for the cap becomes its own section rather than being
    dropped or truncated. It may well overrun — that is what NFR-102's "re-split
    on the next pass" is for — but silently discarding a row would corrupt the
    report;
  * the estimate is *not* the actual cost. Actual usage comes back on the
    response and is recorded per section, so estimate and reality can be
    compared in the debug log.

Nothing here does I/O, raises, or knows what a provider is.
"""

from __future__ import annotations

from .. import config

# Flat cost of the instruction block a section carries: criteria list, output
# format, placeholder rules. Measured against the prompts in prompts.py and
# rounded up — it only has to be the right order of magnitude, and being a
# little pessimistic is the safe direction for a cap.
PROMPT_OVERHEAD_TOKENS = 400

# One request never carries more rows than this, however small they are: it is
# the same ceiling `ratelimit.BATCH_MAX` puts on a translate batch, and a reply
# listing 200 row numbers is where models start losing track of them.
MAX_ROWS_PER_SECTION = 50
MIN_ROWS_PER_SECTION = 1

Row = tuple


def row_text(row: Row) -> str:
    """
    Every text a row contributes to the prompt, concatenated.

    Rows arrive in two shapes and both are legitimate: `(row, text)` for a
    single column, `(row, source, translation)` — plus an optional context —
    for an evaluation. Anything past the row number counts.
    """
    if not isinstance(row, (tuple, list)) or len(row) < 2:
        return str(row or "")
    return "".join(str(part or "") for part in row[1:])


def estimate_row(row: Row) -> int:
    """
    Tokens one row is expected to cost, prompt and reply together.

    `max(150, len/4)`: the divisor is the usual four-characters-a-token rule of
    thumb, and the floor covers the row number, the comment the model may write
    back, and the JSON around both.
    """
    characters = len(row_text(row))
    return max(
        config.TOKEN_EST_MIN_PER_ROW,
        round(characters / config.TOKENS_PER_CHAR_DIVISOR),
    )


def estimate_rows(rows: list[Row]) -> int:
    """The rows' own cost, without instruction overhead or margin."""
    return sum(estimate_row(row) for row in rows)


def estimate_prompt_tokens(rows: list[Row], overhead: int = PROMPT_OVERHEAD_TOKENS) -> int:
    """One request's estimate: rows + instructions, before the safety margin."""
    if not rows:
        return 0
    return overhead + estimate_rows(rows)


def estimate_total(rows: list[Row], overhead: int = PROMPT_OVERHEAD_TOKENS) -> int:
    """
    What one section is expected to cost, margin included.

    This is the number compared against the cap, so the margin belongs inside
    it: a section that only fits without headroom is a section that overruns.
    """
    base = estimate_prompt_tokens(rows, overhead)
    if not base:
        return 0
    return int(round(base * (1.0 + config.TOKEN_EST_SAFETY_MARGIN)))


def estimate_sections(sections: list[list[Row]]) -> int:
    """The whole evaluation's estimate — every section, plus its overhead."""
    return sum(estimate_total(section) for section in sections)


def split_sections(
    rows: list[Row],
    max_tokens: int | None = None,
    max_rows: int | None = None,
    overhead: int = PROMPT_OVERHEAD_TOKENS,
) -> list[list[Row]]:
    """
    Group rows into sections that each fit both row and token caps, order preserved.

    `max_rows` is primary (Settings: max_rows_per_request, default 25, range 5-100);
    `max_tokens` is the safety net (DEFAULT 10,000) to prevent Context Exceeded.

    The 5-100 range belongs to the *setting*, so it is applied only when the
    figure came from prefs. A caller that names a size means it: Fix All asks for
    small batches on purpose — a repair reply is a whole rewritten line per row —
    and clamping its 2 up to 5 would quietly send larger requests than the wizard
    said it would.
    """
    if not rows:
        return []

    cap = int(max_tokens or config.MAX_TOKENS_PER_REQUEST)
    cap = max(1, cap)

    if max_rows is None:
        try:
            from ..data.app_prefs import max_rows_per_request
            max_rows = max_rows_per_request()
        except Exception:
            max_rows = config.DEFAULT_MAX_ROWS_PER_REQUEST
        row_cap = max(config.MIN_MAX_ROWS, min(int(max_rows), config.MAX_MAX_ROWS))
    else:
        row_cap = max(1, min(int(max_rows), config.MAX_MAX_ROWS))

    sections: list[list[Row]] = []
    current: list[Row] = []
    for row in rows:
        candidate = current + [row]
        fits = estimate_total(candidate, overhead) <= cap and len(candidate) <= row_cap
        if fits:
            current = candidate
            continue
        if current:
            sections.append(current)
            current = []
        if estimate_total([row], overhead) <= cap:
            current = [row]
        else:
            sections.append([row])
    if current:
        sections.append(current)
    return sections


def is_warned(total_tokens: int) -> bool:
    """
    Is this big enough to ask the user first? (FR-106 / ASM-003.)

    Not a limit — a confirmation. The threshold is a constant precisely so it
    can be argued with later without touching this logic.
    """
    return int(total_tokens or 0) > config.TOKEN_WARN_THRESHOLD


def summarise(sections: list[list[Row]], max_tokens: int | None = None) -> str:
    """
    One line for the setup screen: what will be sent, and roughly what it costs.

    Phrased as an estimate because it is one, and the user is about to spend
    real tokens on the strength of it.
    """
    if not sections:
        return "Nothing to evaluate."
    rows = sum(len(section) for section in sections)
    total = estimate_sections(sections)
    cap = int(max_tokens or config.MAX_TOKENS_PER_REQUEST)
    largest = max(estimate_total(section) for section in sections)
    return (
        f"{rows:,} row(s) in {len(sections)} request(s) · "
        f"~{total:,} tokens estimated · largest request ~{largest:,} of {cap:,}"
    )
