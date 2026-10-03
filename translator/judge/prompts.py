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
    "You are a rigorous and impartial senior translation arbiter and quality evaluator. "
    "Your mandate is to maintain high translation standards and detect genuine defects—including "
    "clear typos, grammatical errors, mistranslations, broken nuance, and placeholder corruption—while "
    "distinguishing real issues from pedantic nitpicking or prior reviewer hallucinations. "
    "You review only what is in front of you, you never rewrite the source, "
    "and you report a line whenever something is actually wrong with it.\n"
    "CORE PRINCIPLES OF ARBITRATION:\n"
    "1. PRESUMPTION OF ADEQUACY & DEFECT SENSITIVITY: While natural alternative phrasings are allowed, "
    "you MUST actively catch and flag genuine defects. Typographical errors (typos), misspelled words, "
    "reversed meanings, inaccurate terminology, and unnatural machine translation phrasing are NOT acceptable "
    "and must be flagged.\n"
    "2. SYNONYM AND REGISTER TOLERANCE: Natural languages have multiple valid ways to render emotional nuance. "
    "Legitimate synonym alternatives with identical meaning are acceptable, but do NOT confuse a mistranslation, "
    "awkward translation, or register clash with acceptable synonym choice.\n"
    "3. ARBITRATION OF PREVIOUS FINDINGS (HIGH SCRUTINY FOR RE-JUDGE): When a row includes a PREVIOUS FINDING or is under review:\n"
    "   - Rigorously inspect whether the identified flaw has TRULY been resolved in the current text.\n"
    "   - If the defect (or a new defect such as a typo, dropped word, or mistranslation) is STILL present, "
    "YOU MUST FLAG IT AGAIN. Do NOT prematurely pass lines that remain defective.\n"
    "   - Only DISMISS the finding (by omitting the row) if the current translation conveys the source accurately, "
    "flawlessly, and naturally without remaining errors.\n"
    "   - Avoid circular critique (recommending Word A when Word B is used, then Word B when Word A is used) "
    "only when both wordings are equally correct and accurate.\n"
    "4. OBJECTIVE ERROR THRESHOLD: Any line with a clear typo, misspelling, grammar fault, reversed meaning, "
    "omitted/hallucinated content, broken placeholder/tag, or incorrect tone is an objective defect and MUST be reported.\n"
    "Judge every line purely against its OWN source. Several lines are batched "
    "into one request for efficiency only: they are not a passage, they are not "
    "context for one another, and they may come from unrelated parts of the "
    "file. What another line in this request says, and how another line was "
    "translated, MUST NOT affect the verdict on this line. A line is wrong only "
    "if it is wrong against the SOURCE printed directly above it."
)

# The chain-mode counterpart of _JUDGE_SYS: the rows of a request are treated
# as what they physically are — consecutive lines of the file — and the model
# is allowed (and asked) to reason across them. The scene-break guard is the
# difference between "chain context" and "the old false-positive machine":
# game scripts switch scenes mid-batch, and a model that force-connects every
# pair of neighbours re-invents the 'Missing Black Cat' bug this file's
# isolation rule was written to kill.
_JUDGE_SYS_CHAIN = (
    "You are a rigorous and impartial senior translation arbiter and quality evaluator. "
    "Your mandate is to maintain high translation standards and detect genuine defects—including "
    "clear typos, grammatical errors, mistranslations, broken nuance, and placeholder corruption—while "
    "distinguishing real issues from pedantic nitpicking or prior reviewer hallucinations. "
    "You review only what is in front of you, you never rewrite the source, "
    "and you report a line whenever something is actually wrong with it.\n"
    "CORE PRINCIPLES OF ARBITRATION:\n"
    "1. PRESUMPTION OF ADEQUACY & DEFECT SENSITIVITY: While natural alternative phrasings are allowed, "
    "you MUST actively catch and flag genuine defects. Typographical errors (typos), misspelled words, "
    "reversed meanings, inaccurate terminology, and unnatural machine translation phrasing are NOT acceptable "
    "and must be flagged.\n"
    "2. SYNONYM AND REGISTER TOLERANCE: Natural languages have multiple valid ways to render emotional nuance. "
    "Legitimate synonym alternatives with identical meaning are acceptable, but do NOT confuse a mistranslation, "
    "awkward translation, or register clash with acceptable synonym choice.\n"
    "3. ARBITRATION OF PREVIOUS FINDINGS (HIGH SCRUTINY FOR RE-JUDGE): When a row includes a PREVIOUS FINDING or is under review:\n"
    "   - Rigorously inspect whether the identified flaw has TRULY been resolved in the current text.\n"
    "   - If the defect (or a new defect such as a typo, dropped word, or mistranslation) is STILL present, "
    "YOU MUST FLAG IT AGAIN. Do NOT prematurely pass lines that remain defective.\n"
    "   - Only DISMISS the finding (by omitting the row) if the current translation conveys the source accurately, "
    "flawlessly, and naturally without remaining errors.\n"
    "   - Avoid circular critique (recommending Word A when Word B is used, then Word B when Word A is used) "
    "only when both wordings are equally correct and accurate.\n"
    "4. OBJECTIVE ERROR THRESHOLD: Any line with a clear typo, misspelling, grammar fault, reversed meaning, "
    "omitted/hallucinated content, broken placeholder/tag, or incorrect tone is an objective defect and MUST be reported.\n"
    "The rows below are CONSECUTIVE lines of the same work, in file order: a "
    "dialogue stream or narrative sequence. Use the surrounding lines as "
    "context — pronoun references, speaker reactions, callbacks, terminology "
    "and register continuity — when judging each line against its OWN source. "
    "A line may be wrong because it breaks what the surrounding lines "
    "established (a name the scene already fixed, a pronoun whose referent is "
    "the previous line, a register that clashes with its neighbours).\n"
    "Scene breaks may occur anywhere: when the topic, speaker or setting "
    "clearly shifts, do NOT force connections across the break — a line after "
    "the break is judged against its own source and the lines on its side of "
    "the break only.\n"
    "When your finding relies on another line, quote BOTH halves in the "
    "comment: this line's text and the neighbouring line it contradicts or "
    "fails to follow."
)

# Rows injected as neighbours in a re-judge: shown, never judged. Stated as a
# hard contract because the row numbers are real sheet numbers and a verdict
# aimed at a neighbour would corrupt the report.
_CHAIN_NEIGHBOUR_RULE = (
    "Rows marked CONTEXT ONLY are there so you can see the conversation around "
    "the rows under review. They are NOT under review: they must NEVER appear "
    "in the \"lines\" array, no matter what you think of them. Only rows "
    "marked UNDER REVIEW may be reported."
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
    "You are an expert localization editor and precision repair system.\n"
    "Your ONLY job is to correct the flawed TRANSLATION using the stated PROBLEM / CORRECTION NOTE as your primary guide.\n\n"
    "CRITICAL OPERATIONAL RULES:\n"
    "1. MANDATORY DEFECT RESOLUTION FIRST:\n"
    "   Read the CORRECTION NOTE carefully. It is written as: WHAT is wrong, WHY it is wrong, and a FIX suggestion. The suggestion names the reviewer's recommended wording — treat it as the preferred answer, not as one option among many.\n"
    "   Your absolute highest priority is to resolve the primary defect identified in the note (missing placeholders, inverted meaning, or dropped clauses). Do NOT get distracted making subjective stylistic edits to unrelated words if the primary defect remains unaddressed.\n"
    "   When TRANSLATION is empty (or failed/missing), generate a fresh and accurate translation directly from SOURCE into the target language.\n"
    "   A repair that returns the flagged wording unchanged is a failed repair: the flagged words may NOT survive into your output.\n"
    "2. ABSOLUTE PLACEHOLDER PRESERVATION:\n"
    "   Text contains programmatic tokens, engine codes, or format placeholders (e.g. \\n[1], [2], {name}, %s, %d, <tag>, [BR][...]).\n"
    "   - These are code tokens, NOT prose. Never omit, translate, or strip leading characters/backslashes from them.\n"
    "   - If the CORRECTION NOTE states a placeholder is missing or corrupted, you MUST restore that exact token into its proper syntactic position.\n"
    "3. EXECUTE EXPLICIT FIX DIRECTIVES:\n"
    "   When the FIX suggestion names specific words — \"consider using X instead of Y\", \"restore [BR][NL]\", \"the name should be Z\" — apply that wording.\n"
    "   If you are certain the suggested wording is unnatural in context, you may adjust it minimally — but the flagged words still must not survive.\n"
    "4. When the FIX suggestion is a direction only — register, tone, style — choose the wording yourself, in the natural voice of the target language for this line and its context. Never output a line that sounds machine-translated or dictionary-literal.\n"
    "5. The corrected line must accurately reflect the meaning of SOURCE in the target language. OUTPUT LANGUAGE must exactly match the Translated text language. Do NOT translate into another language.\n"
    "   When TRANSLATION is empty or failed, generate a fresh translation directly from SOURCE into the target language.\n"
    "   SOURCE may contain intentional glitch text, corrupted-looking fragments, unusual spacing, repeated characters, symbols, or literal escape sequences such as \\n. Preserve that authored effect in the translation: do not sanitize, normalize, reconstruct, or omit it. Translate readable language while retaining its spacing pattern, escapes, symbols, fragmentation, and visual corruption as closely as the target language permits.\n"
    "6. CLEAN OUTPUT CONTRACT:\n"
    "   Return ONLY the corrected text. Do NOT explain, do NOT add notes or conversational remarks.\n"
    "   Change only what is wrong. A line's register, tone and terminology must stay consistent with the surrounding work."
)

# How a comment must be written. Vague comments ("awkward phrasing") are
# unactionable: a reviewer cannot verify them and a repair model cannot use
# them. The comment is the *instruction a repair model will execute*, so it is
# written as one: WHAT is wrong (with both halves of the comparison quoted so
# the claim is checkable), WHY it is wrong against this line's own source, and
# a FIX directive whose shape depends on the user's comment-style setting.
#
# Quoting the SOURCE as well as the TRANSLATION is not decoration. A comment
# that only quotes the translation can be written without ever looking at the
# source, and that is exactly how a correct line gets flagged for contradicting
# some *other* line in the batch. Being forced to write the source fragment out
# makes the comparison the comment claims to have made visible — and a reviewer
# who cannot produce that pair has no finding to report.
def _comment_rules(comment_style: str = "auto") -> str:
    """
    The WHAT/WHY/FIX contract, with the FIX clause shaped by the setting.

    `auto`     — the model picks per finding: exact words when unambiguous,
                 direction-only for style (the shipped behaviour).
    `explicit` — the exact replacement is REQUIRED whenever the fix names
                 specific words; the repair must be a precise instruction.
    `critical` — dictating words is FORBIDDEN; only the critique and the
                 direction may be stated, and the repair model rewrites freely.
    """
    head = (
        "For every reported line, write the comment on a single line as a repair instruction with "
        "THREE parts separated by ' · ' (never use unescaped double quotes inside the comment; use single quotes '...'):\n"
        "  WHAT: SOURCE says '...' but TRANSLATION says '...' — quote the exact "
        "word or phrase at fault from the TRANSLATION, and the part of THAT LINE'S "
        "OWN SOURCE it fails to render.\n"
        "  WHY: one sentence stating what is wrong in relation to that SOURCE "
        "(meaning changed, negation dropped, placeholder broken, register clash).\n"
    )
    tail = (
        "The FIX part is what a repair model executes verbatim: make it concrete "
        "enough that following it fixes the line. Never write only a judgement "
        "(\"awkward\", \"unnatural\") with no instruction.\n"
        "If you cannot fill in WHAT and WHY from this line alone, you have no "
        "finding — do not report the line."
    )
    if comment_style == "critical":
        fix_clause = (
            "  FIX: state the critique and the direction of the correction ONLY. "
            "Do NOT dictate exact replacement words or rewrite the line yourself — "
            "never name a specific replacement. Describe what is wrong and what the "
            "corrected line must achieve (meaning, register, tone, the placeholder "
            "that must be present), and leave the wording to the repairer.\n"
        )
    elif comment_style == "explicit":
        fix_clause = (
            "  FIX: the exact correction a repairer can apply without guessing, "
            "phrased as a recommendation —\n"
            "    - whenever specific words are at fault, you MUST give the exact "
            "recommended wording or the exact term/name/number that should appear "
            "instead, e.g. FIX: consider using 'kucing hitam' instead of "
            "'kucing merah'. A finding about specific words without the specific "
            "recommendation is an incomplete comment;\n"
            "    - when a placeholder is broken, name the exact token that should be "
            "restored, e.g. FIX: restore [BR][NL] before 'misi';\n"
            "    - only for pure style/register with no specific word at fault, "
            "describe the direction, e.g. FIX: use casual register matching the "
            "surrounding lines, keep the meaning unchanged.\n"
        )
    else:      # auto
        fix_clause = (
            "  FIX: the correction a repairer can apply without guessing, phrased "
            "as a recommendation —\n"
            "    - when the fix is unambiguous, give the exact recommended wording "
            "or the exact term/name/number that should appear instead, e.g. FIX: "
            "consider using 'kucing hitam' instead of 'kucing merah';\n"
            "    - when a placeholder is broken, name the exact token that should be "
            "restored, e.g. FIX: restore [BR][NL] before 'misi';\n"
            "    - when several valid phrasings exist (pure style/register), describe "
            "the direction, e.g. FIX: use casual register matching the surrounding "
            "lines, keep the meaning unchanged.\n"
        )
    return head + fix_clause + tail

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
    "fitting": (
        "fitting — the line does not fit the dialog window or lacks natural conciseness. "
        "When FITTING data is printed with a row, it is measured mechanically: "
        "rows marked OVERFLOW or SINKING must be reported. Rows marked LONG or CONCISENESS are notably longer/wider "
        "than their source beyond normal translation expansion; report lines that are bloated, padded, or overly wordy, "
        "and quote the ratio.\n"
        "    The goal is natural conciseness: the translation should stay reasonably close in length to the original "
        "without unnecessary bloat, while flowing naturally in the target language. In the FIX: shorten the line naturally "
        "(never by dropping facts, names, numbers or core meaning — tighten phrasing and grammar, do not cut content), or "
        "insert the proven line-break tag if one is shown. Keep phrasing flowing and natural, not stiff or truncated."
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


def _naturalness_rule(tgt_lang: str) -> str:
    """
    The guard against dictionary-literal suggestions.

    Written against the target language the user chose — never a named
    language — because "natural" means "what a native speaker of THAT
    language writes in this genre", and the judge cannot verify a suggestion
    it never checked. The FIX directive is the part a repair model executes
    verbatim, so an unnatural suggestion propagates straight into the file
    (the 'kayuhan for goresan' class of fix).
    """
    target = (tgt_lang or "").strip() or "the target language"
    return (
        f"Naturalness is judged as a native speaker of {target} would read the "
        f"line in this genre — not against dictionary equivalence. Before "
        f"writing the FIX, verify the corrected wording you propose is a word "
        f"or phrasing a native speaker of {target} would genuinely use for "
        f"this thing in this context. If the current translation already "
        f"reads naturally in {target} and preserves the source's meaning, a "
        f"merely different phrasing is NOT a finding — do not report the line. "
        f"Never propose a stiffer or more literal wording than the source "
        f"genre calls for. Write every comment in {target}, the language of "
        f"the TRANSLATION, so whoever repairs the row reads the note natively."
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


def _criteria_block(
    criteria: list[str],
    with_context: bool,
    fitting_mode: str = "normal",
    fitting_tolerance: int = 3,
) -> str:
    """The instruction list for the criteria in play."""
    out = []
    for name in _chosen_criteria(criteria, with_context):
        if name == "fitting" and fitting_mode == "strict":
            rule = (
                f"fitting (STRICT / UI COMPACT) — the translation is too long or violates strict UI/dialog bounds.\n"
                f"    When FITTING data is printed with a row, it is measured mechanically: rows marked "
                f"OVERFLOW, SINKING, STRICT_1WORD_LIMIT, or STRICT_LENGTH_LIMIT must be reported.\n"
                f"    * For 1-WORD source texts (UI buttons, menu labels, item names, stats): translation must remain concise "
                f"(maximum +{fitting_tolerance} characters longer than source) and stay 1 word. "
                f"If translating into the target language is naturally too long or becomes multiple words (e.g. 'Sunset' -> 'Matahari terbenam'), "
                f"do NOT force a lengthy translation. In FIX, KEEP the familiar English or original term (e.g. 'Sunset'). For Japanese source texts, "
                f"ALWAYS use standard familiar English gaming terms (e.g. 'Equip', 'Config', 'Save', 'Load') if the target language is too long or multi-word; "
                f"NEVER leave raw Japanese Kanji/Katakana intact if target language is Indonesian/English.\n"
                f"    * For DIALOGUE / MULTI-WORD lines: tighten phrasing and grammar so the translation length is equal to or shorter than the source text (≤ 1.0x length) without losing core facts, numbers, or names."
            )
        else:
            rule = _CRITERION_RULES[name]
        out.append(f"  - {rule}")
    return "\n".join(out)


def _rows_block(
    rows: list[tuple[int, str, str]],
    context_map: dict[int, str] | None = None,
    fitting_map: dict[int, str] | None = None,
    history_map: dict[int, str] | None = None,
) -> str:
    """Source and translation per row, numbered, with context and prior findings where present."""
    blocks: list[str] = []
    for row, source, translation in rows:
        lines = [f"[{row}]"]
        if context_map and (context_map.get(row) or "").strip():
            lines.append(f"  CONTEXT     : {encode_placeholders(context_map[row])}")
        lines.append(f"  SOURCE      : {encode_placeholders(source or '')}")
        if history_map and (history_map.get(row) or "").strip():
            lines.append(f"  PREVIOUS FINDING : {encode_placeholders(history_map[row])}")
        lines.append(f"  TRANSLATION : {encode_placeholders(translation or '')}")
        if fitting_map and (fitting_map.get(row) or "").strip():
            lines.append(f"  FITTING     : {fitting_map[row]}")
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
    criteria: list[str] | None,
    severities: list[str] | None,
    allow_leave_intact: bool = True,
) -> str:
    """
    What this repair run is allowed to touch.

    Stated to the model as well as filtered by the caller. A model sent one row
    to fix a broken placeholder will often "improve" its register on the way
    past, and that unrequested rewrite is exactly what makes a Fix All result
    untrustworthy.
    """
    parts: list[str] = []
    allowed = set(CRITERIA) | {"other", "failed", "empty", "unjudged"}
    chosen = [name for name in criteria or () if name in allowed]
    if chosen:
        if allow_leave_intact:
            parts.append(
                "Fix ONLY these error categories: " + ", ".join(chosen) + ". "
                "Leave anything else about the line exactly as it is."
            )
        else:
            parts.append(
                "Fix ONLY these error categories: " + ", ".join(chosen) + "."
            )
    if "empty" in chosen:
        parts.append(
            "- For empty rows (or rows where translation is missing): translate the SOURCE directly into the target language."
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
    chain: bool = False,
    comment_style: str = "auto",
    fitting_map: dict[int, str] | None = None,
    history_map: dict[int, str] | None = None,
    fitting_mode: str = "normal",
    fitting_tolerance: int = 3,
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

    `chain` swaps the per-line isolation for dialogue-stream mode: the rows are
    consecutive lines of the file, neighbours may be used as context, and the
    scene-break guard replaces the blanket ban. Off (the default) keeps the
    isolation that protects against cross-line false positives.

    `comment_style` shapes the FIX clause of every comment (Settings):
    `auto` (default) lets the model choose per finding, `explicit` demands
    exact replacement words, `critical` forbids dictating words entirely.
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
        + '"comment": "WHAT: SOURCE says \'black cat\' but TRANSLATION says '
        '\'kucing merah\' · WHY: the colour is reversed, a player would '
        'misread the story · FIX: consider using \'kucing hitam\' instead '
        'of \'kucing merah\'"}]}'
    )

    system = _JUDGE_SYS_CHAIN if chain else _JUDGE_SYS
    if chain:
        lines_rule = (
            f"- Judge each line against its own SOURCE, using the surrounding "
            f"lines as context where they clearly continue the same scene.\n"
            f"- A cross-line finding must quote both halves: this line and the "
            f"neighbour it contradicts or fails to follow.\n"
            f"- Do NOT force connections across a clear scene break.\n"
        )
    else:
        lines_rule = (
            f"- Judge each line against its own SOURCE only. The other lines in "
            f"this request are unrelated work batched together; they are not "
            f"context, and a disagreement between two lines is not evidence "
            f"that either is wrong.\n"
        )

    return (
        f"{system}\n\n"
        f"{_OUTPUT_CONTRACT}\n\n"
        f"Review {len(rows)} translated line(s).\n"
        f"{_lang_line(src_lang, tgt_lang)}"
        f"{_style_line(style_prompt)}"
        f"\nJudge each line ONLY on these criteria:\n"
        f"{_criteria_block(criteria, has_context, fitting_mode=fitting_mode, fitting_tolerance=fitting_tolerance)}\n"
        f"{_style_guidance(style_prompt, chosen)}"
        f"\n{_PLACEHOLDER_RULE}\n"
        f"{_extra_placeholders(custom_placeholders)}"
        f"\n{_comment_rules(comment_style)}\n"
        f"\n{_naturalness_rule(tgt_lang)}\n"
        + (f"\n{_SEVERITY_RULES}\n" if graded else "")
        + f"\nRules:\n"
        f"- Report a line ONLY if it is wrong on one of the criteria above.\n"
        f"{lines_rule}"
        f"- Say nothing about lines that are acceptable — omit them entirely.\n"
        f"- If your analysis concludes the line is acceptable, NEVER include it "
        f"in the lines array with a comment saying it is fine. A verdict of "
        f"\"acceptable\" means OMIT, not report.\n"
        f"- Do not penalise a valid alternative wording that means the same thing.\n"
        f"- Do not penalise a name or term kept in the source's own spelling.\n"
        f"- Do not rewrite the line here; describe what is wrong, briefly.\n"
        f"- Use the row numbers exactly as given.\n"
        f"\nReply with ONLY a JSON object, no prose around it and no code fence:\n"
        f"{example}\n"
        f'If every line is acceptable, reply exactly: {{"lines": []}}\n'
        f"\nLines:\n{_rows_block(rows, context_map, fitting_map, history_map)}\n\n"
        f"CRITICAL: Output ONLY the JSON object starting with {{ and ending with }}. "
        f"Do not write any preamble, introduction, markdown code fence, or explanation. "
        f'If all lines are acceptable, reply exactly: {{"lines": []}}.\n'
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
    fitting: str = "",
    history: str = "",
    fitting_mode: str = "normal",
    fitting_tolerance: int = 3,
) -> str:
    """
    Re-check one row after a fix (FR-112).

    Same JSON shape as a section, so one parser reads both — an empty `lines`
    means the row passed and can leave the problem list.

    `fitting` is the row's machine-measured FITTING line. It must travel with
    the row whenever `fitting` is among the criteria: the criterion's rule
    tells the model to quote measured data, so a prompt that lists the
    criterion without the data is hallucination bait (the model would either
    pass an overflowing line or invent a measurement).

    `history` is the previous critique/finding for this row, if known. Providing
    it enables the arbiter to test whether the fix satisfied the issue rather
    than shifting goalposts or ping-ponging synonyms.
    """
    fitting_map = {row: fitting} if (fitting or "").strip() else None
    history_map = {row: history} if (history or "").strip() else None
    return build_judge_prompt(
        rows=[(row, source, translation)],
        criteria=criteria,
        src_lang=src_lang,
        tgt_lang=tgt_lang,
        style_prompt=style_prompt,
        context_map={row: context} if (context or "").strip() else None,
        custom_placeholders=custom_placeholders,
        grading_mode=grading_mode,
        fitting_map=fitting_map,
        history_map=history_map,
        fitting_mode=fitting_mode,
        fitting_tolerance=fitting_tolerance,
    )


def build_judge_rechain_prompt(
    rows: list[tuple[int, str, str]],
    neighbours: dict[int, tuple[str, str]],
    criteria: list[str],
    src_lang: str = "",
    tgt_lang: str = "",
    style_prompt: str = "",
    custom_placeholders: str = "",
    grading_mode: str = "severity",
    comment_style: str = "auto",
    fitting_map: dict[int, str] | None = None,
    history_map: dict[int, str] | None = None,
    fitting_mode: str = "normal",
    fitting_tolerance: int = 3,
) -> str:
    """
    Re-check flagged rows with their physical neighbours as context.

    The rows under review are the flagged ones and nothing else — they are the
    batch, the progress figure and the verdicts. `neighbours` carries the
    rows physically adjacent to them in the file (row ± window), read by the
    caller from the sheet: they are printed as CONTEXT ONLY rows, marked so in
    the block, and the hard rule above the lines keeps the model from
    reporting them. A neighbour that is itself flagged appears once, as an
    UNDER REVIEW row, from whichever side owns it — the caller de-duplicates.

    Same JSON reply shape as a section, so the same parser reads it; a verdict
    aimed at a CONTEXT ONLY row is dropped by the caller, which knows the
    difference.
    """
    has_review = bool(rows)
    if not has_review:
        return build_judge_prompt(
            rows=[], criteria=criteria, src_lang=src_lang, tgt_lang=tgt_lang,
            style_prompt=style_prompt, custom_placeholders=custom_placeholders,
            grading_mode=grading_mode, chain=True, comment_style=comment_style,
        )

    graded = grading_mode == "severity"
    example_row = rows[0][0]

    example = (
        f'{{"lines": [{{"row": {example_row}, '
        f'"error_types": ["mistranslation"], '
        + ('"severity": "critical", ' if graded else "")
        + '"comment": "WHAT: SOURCE says \'black cat\' but TRANSLATION says '
        '\'kucing merah\' · WHY: the colour is reversed, a player would '
        'misread the story · FIX: consider using \'kucing hitam\' instead '
        'of \'kucing merah\'"}]}'
    )

    # One merged block, in row-number order, so the stream reads as the file
    # does. A neighbour colliding with a reviewed row is printed by its
    # reviewed entry alone.
    review_numbers = {row for row, _s, _t in rows}
    entries: list[tuple[int, str]] = []
    for row, source, translation in rows:
        fitting_line_txt = (fitting_map or {}).get(row, "")
        prev_finding = (history_map or {}).get(row, "")
        lines = [
            f"[{row}] — UNDER REVIEW",
            f"  SOURCE      : {encode_placeholders(source or '')}",
        ]
        if prev_finding and prev_finding.strip():
            lines.append(f"  PREVIOUS FINDING : {encode_placeholders(prev_finding.strip())}")
        lines.append(f"  TRANSLATION : {encode_placeholders(translation or '')}")
        if fitting_line_txt and fitting_line_txt.strip():
            lines.append(f"  FITTING     : {fitting_line_txt.strip()}")
        entries.append((row, "\n".join(lines)))
    for row, (source, translation) in neighbours.items():
        if row in review_numbers:
            continue
        entries.append((row, (
            f"[{row}] — CONTEXT ONLY, not under review\n"
            f"  SOURCE      : {encode_placeholders(source or '')}\n"
            f"  TRANSLATION : {encode_placeholders(translation or '')}"
        )))
    entries.sort(key=lambda pair: pair[0])
    lines_block = "\n".join(text for _row, text in entries)

    return (
        f"{_JUDGE_SYS_CHAIN}\n\n"
        f"{_CHAIN_NEIGHBOUR_RULE}\n\n"
        f"{_OUTPUT_CONTRACT}\n\n"
        f"Re-check {len(rows)} flagged line(s), each shown with its neighbouring "
        f"lines from the file.\n"
        f"{_lang_line(src_lang, tgt_lang)}"
        f"{_style_line(style_prompt)}"
        f"\nJudge each UNDER REVIEW line ONLY on these criteria:\n"
        f"{_criteria_block(criteria, False, fitting_mode=fitting_mode, fitting_tolerance=fitting_tolerance)}\n"
        f"{_style_guidance(style_prompt, _chosen_criteria(criteria, False))}"
        f"\n{_PLACEHOLDER_RULE}\n"
        f"{_extra_placeholders(custom_placeholders)}"
        f"\n{_comment_rules(comment_style)}\n"
        f"\n{_naturalness_rule(tgt_lang)}\n"
        + (f"\n{_SEVERITY_RULES}\n" if graded else "")
        + f"\nRules:\n"
        f"- Report an UNDER REVIEW line ONLY if it is wrong on one of the "
        f"criteria above.\n"
        f"- Use the CONTEXT ONLY rows to understand the conversation; never "
        f"report them, and never treat a neighbour's wording as the standard a "
        f"reviewed line must copy.\n"
        f"- A cross-line finding must quote both halves: the reviewed line and "
        f"the neighbour it contradicts or fails to follow.\n"
        f"- Do NOT force connections across a clear scene break.\n"
        f"- Say nothing about lines that are acceptable — omit them entirely.\n"
        f"- If your analysis concludes the line is acceptable, NEVER include it "
        f"in the lines array with a comment saying it is fine. A verdict of "
        f"\"acceptable\" means OMIT, not report.\n"
        f"- Do not penalise a valid alternative wording that means the same thing.\n"
        f"- RE-JUDGE STRICTNESS: Inspect UNDER REVIEW lines with high scrutiny. If a line still has a typo, misspelling, mistranslation, unnatural grammar, or unresolved defect, you MUST flag it. Do NOT overlook remaining flaws or assume a fix succeeded without verifying.\n"
        f"- Do not rewrite the line here; describe what is wrong, briefly.\n"
        f"- Use the row numbers exactly as given.\n"
        f"\nReply with ONLY a JSON object, no prose around it and no code fence:\n"
        f"{example}\n"
        f'If every reviewed line is acceptable, reply exactly: {{"lines": []}}\n'
        f"\nLines:\n{lines_block}\n\n"
        f"CRITICAL: Output ONLY the JSON object starting with {{ and ending with }}. "
        f"Do not write any preamble, introduction, markdown code fence, or explanation. "
        f'If all reviewed lines are acceptable, reply exactly: {{"lines": []}}.\n'
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
    fitting_map: dict[int, str] | None = None,
    fitting_mode: str = "normal",
    fitting_tolerance: int = 3,
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
    target = (tgt_lang or "").strip() or "the target language"
    blocks: list[str] = []
    for row, source, translation in rows:
        lines = [f"[{row}]"]
        if context_map and (context_map.get(row) or "").strip():
            lines.append(f"  CONTEXT     : {encode_placeholders(context_map[row])}")
        lines.append(f"  SOURCE      : {encode_placeholders(source or '')}")
        lines.append(f"  TRANSLATION : {encode_placeholders(translation or '')}")
        note = ((comments or {}).get(row) or "").strip()
        from ..parsing.recovery import is_error_output
        if not (translation or "").strip():
            if not note or "empty" in note.lower() or is_error_output(note):
                note = f"Empty cell — translate the original SOURCE into {target}."
        elif note and is_error_output(note):
            note = "Translate accurately and naturally from the SOURCE text."
        if note:
            lines.append(f"  CORRECTION NOTE : {note}")
        if fitting_map and (fitting_map.get(row) or "").strip():
            lines.append(f"  FITTING         : {fitting_map[row]}")
        blocks.append("\n".join(lines))

    numbers = ", ".join(str(row) for row, _s, _t in rows)
    example = rows[0][0] if rows else 1

    prompt = (
        f"{_FIX_SYS}\n\n"
        f"Correct {len(rows)} translated line(s).\n"
        f"{_lang_line(src_lang, tgt_lang)}"
        f"{_style_line(style_prompt)}"
        f"{_fix_style_rule(style_prompt)}"
        f"{_fix_scope_rule(criteria, severities)}"
        f"\nEach line below provides the SOURCE, the flawed TRANSLATION, and the CORRECTION NOTE.\n"
        f"Apply the correction specified in the note, in the wording a native speaker of "
        f"{target} would actually use for this line.\n"
        f"- If a line has an empty TRANSLATION (or note indicates an empty cell or failed translation), translate the SOURCE text directly into {target} following the style and context rules.\n"
        f"\n{_PLACEHOLDER_RULE}\n"
        f"{_extra_placeholders(custom_placeholders)}"
        f"\nOutput format:\n"
        f"- Output EXACTLY {len(rows)} line(s), in this order: {numbers}\n"
        f"- Each line MUST be on its own separate line as: [ROW_NUMBER] corrected translation\n"
        f"- Always put a real newline before every [ROW_NUMBER] bracket so lines never merge together\n"
        f"- Example: [{example}] the corrected line in {target}\n"
        f"- Output ONLY the corrected translation — never the source, never an explanation\n"
        f"- Keep every placeholder byte-for-byte\n"
    )
    # The fitting rule is only stated when at least one row carries measured
    # FITTING data: printed unconditionally (engine off) it invited the model
    # to insert "the line-break tag shown in that row" for rows that show no
    # tag — and with no profile there is no gate to catch the invention.
    if fitting_map and any((fitting_map.get(row) or "").strip() for row, _s, _t in rows):
        if fitting_mode == "strict":
            prompt += (
                f"- A FITTING note (STRICT) requires strictly compact output:\n"
                f"  * For 1-word source texts (UI/buttons/terms): must stay 1 single word and within +{fitting_tolerance} characters of source length. "
                f"If translating into the target language is naturally too long or becomes multiple words (e.g. 'Sunset' -> 'Matahari terbenam'), "
                f"do NOT force a lengthy translation: KEEP the familiar English or original term (e.g. 'Sunset'). For Japanese source texts, "
                f"use standard international English gaming terms (e.g. 'Equip', 'Config', 'Save', 'Load'), NEVER leave raw Japanese Kanji/Katakana intact if target is Indonesian/English.\n"
                f"  * For dialogue / multi-word lines: tighten phrasing and grammar so length does not exceed the source text (≤ 1.0x length), without dropping core facts or names.\n"
            )
        else:
            prompt += (
                f"- A FITTING line means the text must end up within the window: "
                f"shorten it naturally or insert the line-break tag shown in that "
                f"row at a natural pause; the result must not exceed 4 lines and "
                f"must never split a control code or invent a tag\n"
            )
    prompt += f"\nLines:\n" + "\n".join(blocks) + "\n\nOutput:"
    return prompt


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
    fitting: str = "",
    fitting_tolerance: int = 3,
) -> str:
    """
    Repair one row (Fix It).

    No `[N]` prefix is asked for: one line in, one line out, and the reply is
    the corrected text itself. Prefixes are stripped on the way back anyway, so
    asking for one would only give the model something else to get wrong.
    """
    target = (tgt_lang or "").strip() or "the target language"
    is_empty = not (translation or "").strip()
    scope_rule = _fix_scope_rule(criteria, severities, allow_leave_intact=not is_empty)
    if is_empty:
        scope_rule = scope_rule.replace(" Leave anything else about the line exactly as it is.", "")
        scope_rule = scope_rule.replace("Leave anything else about the line exactly as it is.", "")

    parts = [f"{_FIX_SYS}\n", _lang_line(src_lang, tgt_lang), _style_line(style_prompt),
             _fix_style_rule(style_prompt), scope_rule]
    if (context or "").strip():
        parts.append(f"Context    : {encode_placeholders(context)}\n")
    parts.append(f"Source     : {encode_placeholders(source or '')}\n")
    parts.append(f"Translation: {encode_placeholders(translation or '')}\n")
    from ..parsing.recovery import is_error_output
    note = (comment or "").strip()
    if is_empty:
        if not note or "empty" in note.lower() or is_error_output(note):
            note = f"Empty cell — translate the original SOURCE into {target}."
    elif note and is_error_output(note):
        note = "Translate accurately and naturally from the SOURCE text."
    if note:
        parts.append(f"Correction Note: {note}\n")
    if (fitting or "").strip():
        parts.append(f"FITTING    : {fitting.strip()}\n")
    parts.append(f"\n{_PLACEHOLDER_RULE}\n")
    parts.append(_extra_placeholders(custom_placeholders))
    fit_rule = ""
    if (fitting or "").strip():
        if "STRICT" in fitting:
            fit_rule = (
                f"The Fitting note (STRICT) requires strictly compact output: "
                f"For 1-word source texts, stay 1 word and within +{fitting_tolerance} chars of source length; "
                f"if the translation is too long or becomes multi-word (e.g. 'Sunset' -> 'Matahari terbenam'), keep the familiar English or original term (e.g. 'Sunset'). "
                f"For Japanese source texts, use standard international English gaming terms (e.g. 'Equip', 'Config', 'Save', 'Load'), never leave raw Kanji. "
                f"For dialogue / multi-word lines, tighten phrasing and grammar so length does not exceed the source text without dropping core facts or names.\n"
            )
        else:
            fit_rule = (
                "The Fitting note means the text must end up within the game's dialog "
                "window: shorten it naturally or insert the line-break tag shown in the note "
                "at a natural pause; the result must not exceed 4 lines and must never split "
                "a control code or invent a tag.\n"
            )
    parts.append(
        f"\nInstruction:\n"
        f"Apply the correction from the Correction Note, in the wording a native "
        f"speaker of {target} would actually use for this line — if the note's "
        f"suggested wording is stiff or unnatural, write the natural {target} "
        f"wording yourself while still fixing the problem the note identified.\n"
        f"Do not keep the wrong words that were flagged.\n"
        f"- If a line has an empty TRANSLATION (or note indicates an empty cell or failed translation), translate the SOURCE text directly into {target} following the style and context rules.\n"
        f"{fit_rule}"
        f"Return ONLY the corrected translation — no prefix, no quotes, no explanation.\n"
        f"Keep every placeholder byte-for-byte.\n"
        f"\nCorrected:"
    )
    return "".join(parts)
