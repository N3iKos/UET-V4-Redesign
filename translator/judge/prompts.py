"""
judge/prompts.py — What to ask a model about a translation.

Four prompts, and the difference between them is what the model is allowed to
change:

  build_judge_prompt        review N rows, report only what is wrong
  build_judge_single_prompt review one row after a fix (Judge Again)
  build_fix_prompt          repair N flagged rows, keeping the [N] contract
  build_fix_solo_prompt     repair one row (Fix It)

These decisions shape all of them.

**Only the chosen criteria are mentioned.** A prompt that lists `context` when
no context column exists invites the model to invent context errors, so the
criterion is dropped from the instructions entirely rather than mentioned and
excused (FR-103 / AC-102).

**Style is judged objectively, or not judged.** A Judge run usually has no style
prompt — it reviews a finished file, and the prompt that produced it is gone. So
when `style` is chosen without a stated style, the model is told to infer the
register from the text and then judge only what can be checked: consistency of
terminology and register, fitness for the kind of line, and whether it reads as
written rather than translated. Preference is ruled out explicitly, because a
reviewer grading on taste makes the score depend on which model happened to
review it rather than on the translation.

**Every line is judged against its own source, and nothing else.** A request
carries 20-100 rows because batching is cheaper, not because they form a
passage — sections are split by token budget, so the rows in one request may
come from unrelated scenes. A model left to its own devices reads them as
continuous prose and starts flagging correct lines for disagreeing with their
neighbours: "Missing Black Cat" reported because a later row mentions a pink
cat, or a name copied exactly as the source spells it reported because another
row spells it differently — an inconsistency in the *source file*, not in the
translation. So cross-line reasoning is forbidden for `mistranslation`, allowed
for `style` only where the compared sources are identical, and every comment
must quote both halves of the comparison it claims to have made.

**Silence means correct.** The model reports only problem rows. Asking for a
verdict on every row triples the reply length, and the interesting rows are the
ones a reviewer would actually read.

**Placeholders are described by example, not enumerated.** The app's own
`[BR][XX]` engine tokens, `{name}`, `%s`/`%d`, `{0}`, `<tag>` — a game script
contains patterns nobody listed, so the model is told what a placeholder *is*
and shown the ones that recur here (FR-104). Any extra patterns the user named
in the job are appended verbatim.

Reused rather than reinvented: `[N]` numbering (parsing/prompts.py) so a reply
can be read by the same recovery pipeline, and `encode_placeholders` so a
backslash sequence survives the round trip.
"""

from __future__ import annotations

from ..parsing.placeholders import encode_placeholders
from .models import CRITERIA, SEVERITIES

_JUDGE_SYS = (
    "You are a strict, impartial translation reviewer. "
    "You review only what is in front of you, you never rewrite the source, "
    "and you report a line only when something is actually wrong with it.\n"
    "Judge every line purely against its OWN source. Several lines are batched "
    "into one request for efficiency only: they are not a passage, they are not "
    "context for one another, and they may come from unrelated parts of the "
    "file. What another line in this request says, and how another line was "
    "translated, MUST NOT affect the verdict on this line. A line is wrong only "
    "if it is wrong against the SOURCE printed directly above it."
)

# Hard output-contract for every evaluation reply. Models that answer in prose
# or wrap the JSON in explanation waste a whole request and leave the section
# unevaluated, so the contract is stated as commands, not preferences.
_OUTPUT_CONTRACT = (
    "STRICT OUTPUT CONTRACT — failure to follow it makes your reply unusable:\n"
    "- Reply with EXACTLY ONE JSON object and NOTHING else. No prose, no "
    "explanation, no preamble, no closing remarks, no markdown code fence, no "
    "backticks, no bullet list — the response must begin with { and end with }.\n"
    "- Every row in this request must be covered by the reply. Either a row is "
    "omitted because it is acceptable, or it appears in the \"lines\" array "
    "with its verdict. Never leave a row unaddressed.\n"
    "- The JSON must be well-formed and parseable as-is by a JSON parser.\n"
    "- Do not summarise what you did, do not thank, do not comment on your own "
    "review. The object IS your entire answer."
)

_FIX_SYS = (
    "You are a professional translation editor.\n"
    "Your ONLY job is to correct the flawed TRANSLATION using the stated PROBLEM / CORRECTION NOTE as your primary guide.\n"
    "CRITICAL RULES:\n"
    "1. Read the CORRECTION NOTE carefully. It is written as: WHAT is wrong, WHY it is wrong, and a FIX directive. Execute the FIX directive exactly — it names the words to replace, the placeholder to restore, or the direction to move the register in.\n"
    "2. Follow the FIX directive strictly. Replace the incorrect words/meanings identified in the note with the correct ones the note specifies.\n"
    "3. Do NOT repeat or keep the wrong words that were flagged in the translation.\n"
    "4. The corrected line must accurately reflect the meaning of SOURCE in the target language.\n"
    "5. OUTPUT LANGUAGE must exactly match the Translated text language. Do NOT translate into another language.\n"
    "6. Return ONLY the corrected text. Do NOT explain, do NOT add notes or conversational remarks."
)

# How a comment must be written. Vague comments ("awkward phrasing") are
# unactionable: a reviewer cannot verify them and a repair model cannot use
# them. The comment is the *instruction a repair model will execute*, so it is
# written as one: WHAT is wrong (with both halves of the comparison quoted so
# the claim is checkable), WHY it is wrong against this line's own source, and
# a FIX directive stating what the corrected line must contain — the exact
# words, a placeholder, or the direction to move in when several valid
# phrasings exist.
#
# Quoting the SOURCE as well as the TRANSLATION is not decoration. A comment
# that only quotes the translation can be written without ever looking at the
# source, and that is exactly how a correct line gets flagged for contradicting
# some *other* line in the batch. Being forced to write the source fragment out
# makes the comparison the comment claims to have made visible — and a reviewer
# who cannot produce that pair has no finding to report.
_COMMENT_RULES = (
    "For every reported line, write the comment as a repair instruction with "
    "THREE parts, in this exact shape:\n"
    "  WHAT: SOURCE says \"...\" but TRANSLATION says \"...\" — quote the exact "
    "word or phrase at fault from the TRANSLATION, and the part of THAT LINE'S "
    "OWN SOURCE it fails to render.\n"
    "  WHY: one sentence stating what is wrong in relation to that SOURCE "
    "(meaning changed, negation dropped, placeholder broken, register clash).\n"
    "  FIX: the correction a repairer can apply without guessing —\n"
    "    - when the fix is unambiguous, give the exact corrected wording or the "
    "exact term/name/number that must appear instead, e.g. FIX: replace "
    "\"kucing merah\" with \"kucing hitam\";\n"
    "    - when a placeholder is broken, name the exact token that must be "
    "restored, e.g. FIX: restore [BR][NL] before \"misi\";\n"
    "    - when several valid phrasings exist (pure style/register), describe "
    "the direction, e.g. FIX: use casual register matching the surrounding "
    "lines, keep the meaning unchanged.\n"
    "The FIX part is what a repair model executes verbatim: make it concrete "
    "enough that following it fixes the line. Never write only a judgement "
    "(\"awkward\", \"unnatural\") with no instruction.\n"
    "If you cannot fill in WHAT and WHY from this line alone, you have no "
    "finding — do not report the line."
)

# Severity is graded on impact to a reader, not on distance from a hypothetical
# perfect translation. Left to its own devices a model grades the latter and
# calls everything "minor", because a sentence that reads smoothly feels
# harmless even when its meaning is reversed. Tie-breaking upward is deliberate:
# under-reporting a meaning change is the expensive mistake.
_SEVERITY_RULES = (
    "For every reported line, also assign a severity level based on impact to "
    "a player reading the line — not on how far it is from a \"perfect\" "
    "translation:\n"
    "  - critical — a player would misunderstand the story or an instruction "
    "(meaning reversed, wrong name or number, a broken placeholder that changes "
    "what the game shows or does)\n"
    "  - moderate — the meaning still comes across but a nuance is wrong, "
    "weakened, or lost\n"
    "  - minor — meaning is fully intact; this is purely a style or register "
    "choice\n"
    "When in doubt between two levels, prefer the higher severity. Do not "
    "downgrade a real meaning change to \"minor\" just because the sentence "
    "still reads naturally."
)

# What each criterion asks the model to look for. Written as instructions
# rather than labels: "mistranslation" alone means different things to
# different models.
#
# `mistranslation` carries the precision guard `style` has always had. Without
# it a reviewer reads the batch as a passage and reports a correct line for
# disagreeing with a neighbour — "Missing Black Cat" → "Kucing Hitam yang
# Hilang" flagged because a later line mentions a pink cat, or a name copied
# exactly as the source spells it flagged because another line spells it
# differently. Neither is a translation error: the first is cross-line reading,
# the second is an inconsistency in the source file.
_CRITERION_RULES: dict[str, str] = {
    "mistranslation": (
        "mistranslation — the meaning departs from THIS LINE'S OWN source: a "
        "negation dropped, a number or name changed, a clause omitted or "
        "invented.\n"
        "    Compare the line only against the SOURCE printed with it. NEVER "
        "report a mistranslation on the grounds that the line disagrees with, "
        "contradicts, or is inconsistent with another line in this request — "
        "cross-line comparison is not a mistranslation check, and other lines "
        "may describe a different scene, character or moment entirely.\n"
        "    Do NOT report a line for: a paraphrase that carries the same "
        "meaning; a different word order or sentence structure that means the "
        "same thing; a name, place or term left in the source's own spelling; "
        "or an idiom rendered idiomatically rather than word for word. Only a "
        "genuine change in what the line means is a mistranslation"
    ),
    "context": (
        "context — the wording contradicts the context given for that row "
        "(speaker, scene, or subject)"
    ),
    "placeholder": (
        "placeholder — a placeholder is missing, altered, duplicated, "
        "translated, or moved somewhere it changes meaning"
    ),
    "style": (
        "style — the register does not match the style that was requested "
        "(too formal, too casual, or inconsistent terminology).\n"
        "    This is the ONLY criterion that may look across lines, and only "
        "for terminology consistency: if two lines in this request have the "
        "SAME source text or the same source term and translate it two "
        "different ways, that is a style inconsistency. If the sources "
        "themselves differ — the source file itself spells a name two ways — "
        "do NOT report it at all: the translation faithfully followed each "
        "source, and the inconsistency is in the source file, not in the work "
        "under review"
    ),
}

# What "style" means when nobody stated a style.
#
# A Judge run usually has no style prompt: it reviews a finished file, and the
# prompt that produced it is long gone. Asking about style anyway with nothing
# to compare against invites the model to impose its own taste — one reviewer
# calls a line too casual, the next calls the same line natural, and the score
# moves for reasons that have nothing to do with the translation.
#
# So the model is told to infer the register from the text itself and to judge
# only *internal consistency and fitness*, which are checkable properties: a
# line that is formal where its neighbours are casual is wrong regardless of
# which register was intended, and a line that reads as translated rather than
# written is wrong in any register. Preference is explicitly ruled out.
_STYLE_SELF_DIRECTED = (
    "No target style was specified for this file, so do not assume one. "
    "First infer the register the translation is actually written in — from the "
    "source material, the vocabulary, and how the surrounding lines read — then "
    "judge each line against that, on properties that can be checked rather "
    "than preferred:\n"
    "  - consistency: the same term, name, honorific or form of address is "
    "translated the same way throughout, and one line's register does not "
    "clash with its neighbours'. Report an inconsistency ONLY when the lines "
    "you are comparing have the same source text or term. Where the SOURCE "
    "lines themselves differ — the file spells a name two ways — the "
    "translation followed each source correctly and there is nothing to "
    "report;\n"
    "  - fitness: dialogue reads as speech, UI text reads as a label, a system "
    "message reads as a system message;\n"
    "  - naturalness: the line reads as though written in the target language, "
    "not carried over word by word from the source.\n"
    "Do NOT report a line merely because you would have phrased it "
    "differently, because it is more or less formal than you would choose, or "
    "because it keeps or drops profanity present in the source. A defensible "
    "alternative wording is not an error."
)

_PLACEHOLDER_RULE = (
    "A placeholder is any token the game or program substitutes at run time, "
    "and it must survive translation byte-for-byte. Recognise them by shape, "
    "not from a fixed list: [BR][XX] engine codes such as [BR][NL] or "
    "[BR][AT], brace tokens such as {name} or {0}, printf codes such as %s "
    "%d %02d, tag markup such as <color> or [tag], and any other bracketed, "
    "prefixed or escaped token that clearly is not prose. If you are unsure "
    "whether something is a placeholder, treat it as one."
)


def _chosen_criteria(criteria: list[str], with_context: bool) -> list[str]:
    """
    The criteria actually in play, in a fixed order, with `context` dropped when
    the file has no context column (AC-102).
    """
    chosen = [name for name in CRITERIA if name in set(criteria or ())]
    if not with_context and "context" in chosen:
        chosen.remove("context")
    if not chosen:
        # The UI refuses an empty selection (business rule 6); if one still
        # arrives, meaning accuracy is the least surprising fallback.
        chosen = ["mistranslation"]
    return chosen


def _criteria_block(criteria: list[str], with_context: bool) -> str:
    """The instruction list for the criteria in play."""
    return "\n".join(
        f"  - {_CRITERION_RULES[name]}"
        for name in _chosen_criteria(criteria, with_context)
    )


def _rows_block(
    rows: list[tuple[int, str, str]],
    context_map: dict[int, str] | None = None,
) -> str:
    """Source and translation per row, numbered, with context where present."""
    blocks: list[str] = []
    for row, source, translation in rows:
        lines = [f"[{row}]"]
        if context_map and (context_map.get(row) or "").strip():
            lines.append(f"  CONTEXT     : {encode_placeholders(context_map[row])}")
        lines.append(f"  SOURCE      : {encode_placeholders(source or '')}")
        lines.append(f"  TRANSLATION : {encode_placeholders(translation or '')}")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def _extra_placeholders(custom_placeholders: str) -> str:
    text = (custom_placeholders or "").strip()
    if not text:
        return ""
    return f"\nAlso treat these as placeholders, exactly as written: {text}\n"


def _style_line(style_prompt: str) -> str:
    """The style to judge against, when one was stated."""
    text = (style_prompt or "").strip()
    return f"The style that was requested: {text}\n" if text else ""


def _style_guidance(style_prompt: str, criteria: list[str]) -> str:
    """
    The style instruction block, which depends on whether a style is known.

    With a style prompt, the model checks the translation against it. Without
    one — the normal case for a file being reviewed after the fact — the model
    works out the register from the text and judges consistency, fitness and
    naturalness instead. Only emitted when `style` was actually chosen; a
    paragraph about style in a run that is not judging style is just tokens.
    """
    if "style" not in set(criteria or ()):
        return ""
    if (style_prompt or "").strip():
        return ""
    return f"\nOn style:\n{_STYLE_SELF_DIRECTED}\n"


def _lang_line(src_lang: str, tgt_lang: str) -> str:
    source = (src_lang or "").strip() or "the source language"
    target = (tgt_lang or "").strip() or "the target language"
    return f"Translated from {source} into {target}.\n"


def _fix_style_rule(style_prompt: str) -> str:
    """
    How a repair should treat register when no style was stated.

    Without this a fix drifts: the model is being asked to improve a line, and
    with no stated style "improve" quietly becomes "write it the way I would".
    The surrounding lines are the only register the file actually has, so they
    are what a repair has to match.
    """
    if (style_prompt or "").strip():
        return ""
    return (
        "No target style was specified. Match the register the rest of the file "
        "already uses — keep the same level of formality, the same terminology, "
        "and the same treatment of names and honorifics. Do not make a line more "
        "formal, more polished or more literary than it was; change only what is "
        "listed as wrong with it.\n"
    )


def _fix_scope_rule(
    criteria: list[str] | None, severities: list[str] | None
) -> str:
    """
    What this repair run is allowed to touch.

    Stated to the model as well as filtered by the caller. A model sent one row
    to fix a broken placeholder will often "improve" its register on the way
    past, and that unrequested rewrite is exactly what makes a Fix All result
    untrustworthy.
    """
    parts: list[str] = []
    chosen = [name for name in CRITERIA if name in set(criteria or ())]
    if chosen:
        parts.append(
            "Fix ONLY these error categories: " + ", ".join(chosen) + ". "
            "Leave anything else about the line exactly as it is."
        )
    levels = [name for name in SEVERITIES if name in set(severities or ())]
    if levels:
        parts.append(
            "Only lines at these severity levels were selected for repair: "
            + ", ".join(levels) + "."
        )
    return "\n".join(parts) + "\n" if parts else ""


# ── Evaluation ────────────────────────────────────────────────────────────────

def build_judge_prompt(
    rows: list[tuple[int, str, str]],
    criteria: list[str],
    src_lang: str = "",
    tgt_lang: str = "",
    style_prompt: str = "",
    context_map: dict[int, str] | None = None,
    custom_placeholders: str = "",
    grading_mode: str = "severity",
) -> str:
    """
    Review one section. The reply is JSON listing only the problem rows.

    Row numbers are the sheet's own, and they are what ties a comment back to a
    cell — which is why the anti-desync heuristics are not used here: there is
    nothing positional to get wrong (Doc 06 edge cases).

    `grading_mode` decides whether each flagged row also carries a severity.
    `"none"` is the original behaviour — a row is flagged or it is not — and is
    kept because it is cheaper and because a caller may not want the extra
    judgement call. `"severity"` is the default: knowing which of 300 flagged
    rows would actually confuse a player is the difference between a report and
    a list.
    """
    has_context = bool(context_map) and any(
        (context_map.get(row) or "").strip() for row, _s, _t in rows
    )
    example_row = rows[0][0] if rows else 1
    chosen = _chosen_criteria(criteria, has_context)
    graded = grading_mode == "severity"

    example = (
        f'{{"lines": [{{"row": {example_row}, '
        f'"error_types": ["mistranslation"], '
        + ('"severity": "critical", ' if graded else "")
        + '"comment": "WHAT: SOURCE says \\"black cat\\" but TRANSLATION says '
        '\\"kucing merah\\" · WHY: the colour is reversed, a player would '
        'misread the story · FIX: replace \\"kucing merah\\" with \\"kucing '
        'hitam\\""}]}'
    )

    return (
        f"{_JUDGE_SYS}\n\n"
        f"{_OUTPUT_CONTRACT}\n\n"
        f"Review {len(rows)} translated line(s).\n"
        f"{_lang_line(src_lang, tgt_lang)}"
        f"{_style_line(style_prompt)}"
        f"\nJudge each line ONLY on these criteria:\n"
        f"{_criteria_block(criteria, has_context)}\n"
        f"{_style_guidance(style_prompt, chosen)}"
        f"\n{_PLACEHOLDER_RULE}\n"
        f"{_extra_placeholders(custom_placeholders)}"
        f"\n{_COMMENT_RULES}\n"
        + (f"\n{_SEVERITY_RULES}\n" if graded else "")
        + f"\nRules:\n"
        f"- Report a line ONLY if it is wrong on one of the criteria above.\n"
        f"- Judge each line against its own SOURCE only. The other lines in this "
        f"request are unrelated work batched together; they are not context, and "
        f"a disagreement between two lines is not evidence that either is "
        f"wrong.\n"
        f"- Say nothing about lines that are acceptable — omit them entirely.\n"
        f"- Do not penalise a valid alternative wording that means the same thing.\n"
        f"- Do not penalise a name or term kept in the source's own spelling.\n"
        f"- Do not rewrite the line here; describe what is wrong, briefly.\n"
        f"- Use the row numbers exactly as given.\n"
        f"\nReply with ONLY a JSON object, no prose around it and no code fence:\n"
        f"{example}\n"
        f'If every line is acceptable, reply exactly: {{"lines": []}}\n'
        f"\nLines:\n{_rows_block(rows, context_map)}\n"
    )


def build_judge_single_prompt(
    row: int,
    source: str,
    translation: str,
    criteria: list[str],
    src_lang: str = "",
    tgt_lang: str = "",
    style_prompt: str = "",
    context: str = "",
    custom_placeholders: str = "",
    grading_mode: str = "severity",
) -> str:
    """
    Re-check one row after a fix (FR-112).

    Same JSON shape as a section, so one parser reads both — an empty `lines`
    means the row passed and can leave the problem list.
    """
    return build_judge_prompt(
        rows=[(row, source, translation)],
        criteria=criteria,
        src_lang=src_lang,
        tgt_lang=tgt_lang,
        style_prompt=style_prompt,
        context_map={row: context} if (context or "").strip() else None,
        custom_placeholders=custom_placeholders,
        grading_mode=grading_mode,
    )


# ── Repair ────────────────────────────────────────────────────────────────────

def build_fix_prompt(
    rows: list[tuple[int, str, str]],
    comments: dict[int, str] | None = None,
    criteria: list[str] | None = None,
    src_lang: str = "",
    tgt_lang: str = "",
    style_prompt: str = "",
    context_map: dict[int, str] | None = None,
    custom_placeholders: str = "",
    severities: list[str] | None = None,
) -> str:
    """
    Repair the flagged rows, guided by the comments the evaluation produced.

    The reply must be `[N] text` per row, because it goes through the same
    recovery pipeline the translation path uses — which is also why the model
    is told to return a row unchanged rather than omit it: a missing row is
    indistinguishable from a dropped one.

    `criteria` and `severities` are stated in the prompt as well as filtered in
    the caller. Belt and braces on purpose: the caller decides which rows are
    sent, and telling the model what it is allowed to touch stops it from
    "helpfully" rewriting a line's register when it was sent to fix a
    placeholder.
    """
    blocks: list[str] = []
    for row, source, translation in rows:
        lines = [f"[{row}]"]
        if context_map and (context_map.get(row) or "").strip():
            lines.append(f"  CONTEXT     : {encode_placeholders(context_map[row])}")
        lines.append(f"  SOURCE      : {encode_placeholders(source or '')}")
        lines.append(f"  TRANSLATION : {encode_placeholders(translation or '')}")
        note = ((comments or {}).get(row) or "").strip()
        if note:
            lines.append(f"  CORRECTION NOTE : {note}")
        blocks.append("\n".join(lines))

    target = (tgt_lang or "").strip() or "the target language"
    numbers = ", ".join(str(row) for row, _s, _t in rows)
    example = rows[0][0] if rows else 1

    return (
        f"{_FIX_SYS}\n\n"
        f"Correct {len(rows)} translated line(s).\n"
        f"{_lang_line(src_lang, tgt_lang)}"
        f"{_style_line(style_prompt)}"
        f"{_fix_style_rule(style_prompt)}"
        f"{_fix_scope_rule(criteria, severities)}"
        f"\nEach line below provides the SOURCE, the flawed TRANSLATION, and the CORRECTION NOTE.\n"
        f"Apply the correction specified in the note directly to fix the translation.\n"
        f"\n{_PLACEHOLDER_RULE}\n"
        f"{_extra_placeholders(custom_placeholders)}"
        f"\nOutput format:\n"
        f"- Output EXACTLY {len(rows)} line(s), in this order: {numbers}\n"
        f"- Each line as: [ROW_NUMBER] corrected translation\n"
        f"- Example: [{example}] the corrected line in {target}\n"
        f"- Output ONLY the corrected translation — never the source, never an explanation\n"
        f"- Keep every placeholder byte-for-byte\n"
        f"\nLines:\n" + "\n".join(blocks) + "\n\nOutput:"
    )


def build_fix_solo_prompt(
    row: int,
    source: str,
    translation: str,
    comment: str = "",
    src_lang: str = "",
    tgt_lang: str = "",
    style_prompt: str = "",
    context: str = "",
    custom_placeholders: str = "",
    criteria: list[str] | None = None,
    severities: list[str] | None = None,
) -> str:
    """
    Repair one row (Fix It).

    No `[N]` prefix is asked for: one line in, one line out, and the reply is
    the corrected text itself. Prefixes are stripped on the way back anyway, so
    asking for one would only give the model something else to get wrong.
    """
    target = (tgt_lang or "").strip() or "the target language"
    parts = [f"{_FIX_SYS}\n", _lang_line(src_lang, tgt_lang), _style_line(style_prompt),
             _fix_style_rule(style_prompt), _fix_scope_rule(criteria, severities)]
    if (context or "").strip():
        parts.append(f"Context    : {encode_placeholders(context)}\n")
    parts.append(f"Source     : {encode_placeholders(source or '')}\n")
    parts.append(f"Translation: {encode_placeholders(translation or '')}\n")
    if (comment or "").strip():
        parts.append(f"Correction Note: {comment.strip()}\n")
    parts.append(f"\n{_PLACEHOLDER_RULE}\n")
    parts.append(_extra_placeholders(custom_placeholders))
    parts.append(
        f"\nInstruction:\n"
        f"Apply the correction from the Correction Note to produce the corrected translation in {target}.\n"
        f"Do not keep the wrong words that were flagged.\n"
        f"Return ONLY the corrected translation — no prefix, no quotes, no explanation.\n"
        f"Keep every placeholder byte-for-byte.\n"
        f"\nCorrected:"
    )
    return "".join(parts)
