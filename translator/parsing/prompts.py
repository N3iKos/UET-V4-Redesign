"""
parsing/prompts.py — Prompt builders.

Ported from v3.6's parser.py. The only change: `prompt_format="json"` is no
longer tied to one specific provider — it is a per-model opt-in the user
enables when they know their endpoint supports structured output
(02_DATA_MODELS.md §3 / §7 item 3). The plain-text path remains the default
for every provider.
"""

from __future__ import annotations

import json as _json

from .placeholders import encode_placeholders

_TRANSLATE_SYS = (
    "You are a professional translator. "
    "Output ONLY the translated lines with their [N] prefix — "
    "no explanations, no notes, no reasoning, no commentary."
)

_REPAIR_SYS = (
    "You are a professional translation editor. "
    "Your ONLY job is to correct the TRANSLATED text — never the original. "
    "OUTPUT LANGUAGE must exactly match the Translated text language. "
    "CRITICAL: Do NOT translate the text into another language. "
    "CRITICAL: Do NOT output the source/original language. "
    "CRITICAL: If the Translated text is already correct, return it unchanged. "
    "Return ONLY the corrected numbered lines with [N] prefix. "
    "Do NOT explain, do NOT comment."
)


def build_translate_prompt(
    rows: list[tuple[int, str]],
    src_lang: str,
    tgt_lang: str,
    style_prompt: str,
    context_rows: list[tuple[int, str]] | None = None,
    global_context: str = "",
    custom_placeholders: str = "",
    prompt_format: str = "plain",
    batch_context: bool = False,
) -> str:
    """
    One batch prompt. `batch_context` (Settings, default off) tells the model
    the rows are consecutive lines of the same work and asks it to keep
    terminology, tone and continuity across them — the rows are physically
    consecutive either way, so this is purely a prompt-mode switch. The
    anti-merge rule stays on in both modes: [N]-prefixed output requires each
    row's answer to stay in its own line.
    """
    n = len(rows)
    encoded_rows = [(rn, encode_placeholders(txt)) for rn, txt in rows]

    ctx_block = ""
    if global_context.strip():
        ctx_block += f"\nGlobal context (applies to all lines):\n{global_context.strip()}\n"
    if context_rows:
        ctx_lines = "\n".join(
            f"[{rn}] {encode_placeholders(ctx)}" for rn, ctx in context_rows
        )
        ctx_block += f"\nPer-row context (same row numbers):\n{ctx_lines}\n"

    ph_note = ""
    if custom_placeholders.strip():
        ph_note = f"\nAdditional placeholders to preserve exactly: {custom_placeholders.strip()}"

    lang_note = ""
    if src_lang and src_lang.lower() not in ("auto", "detect", ""):
        lang_note = (
            f"- TARGET SOURCE LANGUAGE: {src_lang} ONLY.\n"
            f"- TARGET OUTPUT LANGUAGE: {tgt_lang} ONLY.\n"
            f"- CRITICAL: Do NOT translate any other languages found in the text.\n"
            f"- If a line or part of a line contains Japanese, Chinese, or any language "
            f"other than {src_lang}, COPY IT EXACTLY character by character.\n"
        )

    if batch_context:
        lines_rule = (
            f"- The lines below are CONSECUTIVE lines of the same work, in order. "
            f"Keep terminology, names, register and tone consistent across them: "
            f"a line may refer back to an earlier one, and the same term must be "
            f"translated the same way throughout.\n"
        )
    else:
        lines_rule = (
            f"- Each line is COMPLETELY INDEPENDENT — translate it exactly as-is\n"
        )

    rules = (
        f"RULES:\n"
        f"- Output EXACTLY {n} lines in the same order\n"
        f"{lines_rule}"
        f"- Do NOT merge, combine, or absorb content from adjacent lines — "
        f"if a sentence is split across two lines, translate each fragment SEPARATELY\n"
        f"- Do NOT complete, fix, or extend incomplete lines — "
        f"if the source has no closing quote, the output must not have one either\n"
        f"- Do NOT add punctuation (! . ? etc.) that does not exist in the source line\n"
        f"- Use ONLY straight quotes (\") and straight apostrophes ('). "
        f"Do NOT use curly/typographic quotes (\u201c \u201d \u2018 \u2019)\n"
        f"- Preserve hyphens (-- or -) exactly as they appear. "
        f"Do NOT replace -- with em-dash (\u2014) or en-dash (\u2013)\n"
        f"- [BR][XX] tokens are engine codes — copy them byte-for-byte at the same position\n"
        f"- Do NOT add or remove [BR][DQ] tokens — only copy what is in that line\n"
        f"{lang_note}"
        f"- Format codes (%s %d %f %02d), brace tokens ({{0}} {{name}}), "
        f"tags ([tag] <tag>) — copy as-is\n"
    )

    if prompt_format == "json":
        input_json = _json.dumps(
            {str(rn): txt for rn, txt in encoded_rows}, ensure_ascii=False
        )
        return (
            f"{_TRANSLATE_SYS}\n\n"
            f"Translate {n} line(s) from {src_lang} to {tgt_lang}.\n"
            f"Style: {style_prompt}\n"
            f"{ctx_block}{ph_note}\n"
            f"{rules}"
            f"- Input and output are JSON objects with the same numeric string keys\n"
            f"- Output ONLY the JSON object — no explanation, no markdown\n\n"
            f"Input:\n{input_json}\n\nOutput:"
        )

    numbered = "\n".join(f"[{rn}] {txt}" for rn, txt in encoded_rows)
    return (
        f"{_TRANSLATE_SYS}\n\n"
        f"Translate {n} line(s) from {src_lang} to {tgt_lang}.\n"
        f"Style: {style_prompt}\n"
        f"{ctx_block}{ph_note}\n"
        f"{rules}"
        f"- Every output line MUST begin with its original [N] prefix\n\n"
        f"Input:\n{numbered}\n\nOutput:"
    )


def build_translate_solo_prompt(
    text: str,
    src_lang: str,
    tgt_lang: str,
    style_prompt: str,
    context: str = "",
    global_context: str = "",
    custom_placeholders: str = "",
) -> str:
    encoded_text = encode_placeholders(text)

    ctx = ""
    if global_context.strip():
        ctx += f"Global context: {global_context.strip()}\n"
    if context.strip():
        ctx += f"Row context: {context.strip()}\n"

    ph_note = ""
    if custom_placeholders.strip():
        ph_note = f"Preserve these placeholders exactly: {custom_placeholders.strip()}\n"

    lang_note = ""
    if src_lang and src_lang.lower() not in ("auto", "detect", ""):
        lang_note = (
            f"TARGET SOURCE LANGUAGE: {src_lang} ONLY.\n"
            f"TARGET OUTPUT LANGUAGE: {tgt_lang} ONLY.\n"
            f"CRITICAL: Do NOT translate any other languages found in the text.\n"
            f"If the text or part of the text contains Japanese, Chinese, or any language "
            f"other than {src_lang}, COPY IT EXACTLY character by character.\n"
        )

    return (
        f"You are a professional translator.\n"
        f"Translate from {src_lang} to {tgt_lang}.\n"
        f"Style: {style_prompt}\n"
        f"{ctx}{ph_note}"
        f"{lang_note}"
        "Copy [BR][XX] engine tokens, format codes (%s %d %f), and brace tokens "
        "({0} {name}) exactly as-is.\n"
        "Do NOT add punctuation that does not exist in the source.\n"
        "Use ONLY straight quotes (\") and apostrophes ('). Do NOT use curly/typographic quotes.\n"
        "Preserve hyphens (-- or -) exactly. Do NOT replace with em-dash or en-dash.\n"
        f"Return ONLY the translated text — no explanation, no prefix.\n\n"
        f"Text: {encoded_text}\n\nTranslation:"
    )


def build_repair_prompt(
    rows_data: list[dict],
    repair_mode: str,
    src_lang: str = "",
    tgt_lang: str = "",
) -> str:
    n = len(rows_data)
    row_nums = [d["row_num"] for d in rows_data]
    example = row_nums[0]
    lang_note = (
        f"Translation language: {tgt_lang}. Source language: {src_lang}. " if tgt_lang else ""
    )

    if repair_mode == "repair":
        instruction = (
            f"{lang_note}"
            f"Review each TRANSLATED line for typos, grammar errors, and unnatural phrasing. "
            f"Output must be in {tgt_lang or 'the same language as the translation'}. "
            f"Do NOT translate back to the source language."
        )
        lines = [
            f"[{d['row_num']}] {encode_placeholders(d['translated'])}" for d in rows_data
        ]

    elif repair_mode == "repair_compare":
        instruction = (
            f"{lang_note}"
            f"The 'Original' is the SOURCE text. The 'Translated' is the TARGET text in "
            f"{tgt_lang or 'target language'}. "
            f"Compare them for meaning accuracy, then fix typos and unnatural phrasing "
            f"in the TRANSLATED text. "
            f"Your output MUST be in {tgt_lang or 'the translation language'} — "
            f"never in the source language."
        )
        lines = [
            f"[{d['row_num']}]\n"
            f"  Original   : {encode_placeholders(d.get('original', ''))}\n"
            f"  Translated : {encode_placeholders(d['translated'])}"
            for d in rows_data
        ]

    else:   # repair_compare_context
        instruction = (
            f"{lang_note}"
            f"The 'Original' is SOURCE text. The 'Translated' is the TARGET text in "
            f"{tgt_lang or 'target language'}. "
            f"Use the Context and Original to verify meaning accuracy, then fix any errors "
            f"in the TRANSLATED text. "
            f"Your output MUST be in {tgt_lang or 'the translation language'} — "
            f"never in the source language."
        )
        lines = [
            f"[{d['row_num']}]\n"
            f"  Context    : {encode_placeholders(d.get('context', ''))}\n"
            f"  Original   : {encode_placeholders(d.get('original', ''))}\n"
            f"  Translated : {encode_placeholders(d['translated'])}"
            for d in rows_data
        ]

    numbered = "\n".join(lines)
    nums_str = ", ".join(str(r) for r in row_nums)
    return (
        f"{_REPAIR_SYS}\n\n"
        f"{instruction}\n\n"
        f"CRITICAL OUTPUT FORMAT:\n"
        f"- You MUST output EXACTLY {n} line(s) with [N] prefix\n"
        f"- Row numbers to output (in order): {nums_str}\n"
        f"- Format each line EXACTLY as: [ROW_NUMBER] corrected text here\n"
        f"- Example: [{example}] Corrected translation in "
        f"{tgt_lang or 'target language'} here.\n"
        f"- Do NOT skip the [N] prefix — it is required for parsing\n"
        f"- Do NOT output the Original — output ONLY the corrected Translated text\n"
        f"- If a translation is already correct, return it unchanged with the [N] prefix\n"
        f"- Preserve all placeholder patterns exactly (e.g. {{0}}, %s, \\n)\n\n"
        f"Input:\n{numbered}\n\nOutput:"
    )


def build_repair_solo_prompt(
    translated: str,
    repair_mode: str,
    original: str = "",
    context: str = "",
    src_lang: str = "",
    tgt_lang: str = "",
) -> str:
    lang_note = f"Output language: {tgt_lang}. Do NOT output in {src_lang}.\n" if tgt_lang else ""
    enc_translated = encode_placeholders(translated)
    enc_original = encode_placeholders(original)
    enc_context = encode_placeholders(context)

    if repair_mode == "repair":
        instruction = (
            f"{lang_note}"
            f"Fix typos, grammar errors, and unnatural phrasing in this translation. "
            f"Output must be in {tgt_lang or 'the same language as the translation'}."
        )
        body = f"Translation: {enc_translated}"
    elif repair_mode == "repair_compare":
        instruction = (
            f"{lang_note}"
            f"The Original is the SOURCE. The Translation is in "
            f"{tgt_lang or 'target language'}. "
            f"Fix inaccuracies and unnatural phrasing in the Translation. "
            f"Output ONLY the corrected Translation in {tgt_lang or 'target language'}."
        )
        body = f"Original   : {enc_original}\nTranslation: {enc_translated}"
    else:
        instruction = (
            f"{lang_note}"
            f"The Original is the SOURCE. The Translation is in "
            f"{tgt_lang or 'target language'}. "
            f"Use Context and Original to verify meaning, then fix the Translation. "
            f"Output ONLY the corrected Translation in {tgt_lang or 'target language'}."
        )
        body = (
            f"Context    : {enc_context}\n"
            f"Original   : {enc_original}\n"
            f"Translation: {enc_translated}"
        )

    return (
        f"You are a professional translation editor.\n"
        f"{instruction}\n\n"
        f"{body}\n\n"
        f"Return ONLY the corrected translation — nothing else.\n\nCorrected:"
    )
