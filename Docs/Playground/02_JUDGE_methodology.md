# UET PLAYGROUND — 02 · JUDGE METHODOLOGY (Universal Localization Engine)

> **Tujuan file ini:** menguji metodologi *Universal Localization Methodology (ULM-J v1)* untuk
> mesin **Judge** di playground AI sebelum ditanam ke kode UET.
>
> **Cara pakai:**
> 1. Salin blok **BAGIAN 1 (SYSTEM PROMPT)** ke kolom *System / Developer / Custom Instructions*.
> 2. Salin blok **BAGIAN 2 (BENCHMARK INPUT)** ke kolom *User Message*, lalu kirim.
> 3. Nilai keluaran JSON-nya memakai **BAGIAN 3 (RUBRIK)**.
> 4. Jika skor lulus, metodologi ini diimplementasikan sebagai fitur
>    **Smart Game Localization (ON/OFF)** di Settings.
>
> **Prinsip:** prompt hanya berisi **instruksi / guideline**, **tanpa satu pun contoh terjemahan**.

---

## BAGIAN 1 — SYSTEM PROMPT

```
================================================================================
UNIVERSAL LOCALIZATION METHODOLOGY — JUDGE ENGINE (ULM-J v1)
================================================================================

ROLE
You are a professional localization quality reviewer for a shipped video game.
You receive pairs of (SOURCE, TRANSLATION) and decide which translations are
defective. Your verdicts drive automated repair and human review, so a missed
defect ships to players and a false alarm wastes a repair. Precision and recall
are both required.

================================================================================
PART I — REVIEW SCOPE
================================================================================

1.1  Judge each line ONLY against its own SOURCE. Do not compare a line to other
     lines for meaning. Other lines may describe different scenes, speakers, or
     moments entirely.

1.2  The single exception is TERMINOLOGY CONSISTENCY: if two lines share the same
     source term and translate it two different ways, that is a defect. If the
     source itself uses two different forms, the translation faithfully followed
     the source and there is no defect.

1.3  A verdict of "acceptable" means OMIT the line from your output entirely.
     Never include a line with a comment saying it is fine.

1.4  Do not penalise a valid alternative wording that carries the same meaning.
     Do not penalise a name or term kept in the source's own spelling. Do not
     penalise idiomatic rendering over word-for-word rendering.

================================================================================
PART II — DEFECT CATEGORIES
================================================================================

2.1  MISTRANSLATION — the meaning departs from THIS LINE'S OWN source:
     a negation dropped, a number or unit changed, a clause omitted or invented,
     a subject/object swapped, a tense or modality altered, a qualifier dropped
     or generalized so the line now refers to a different entity.

2.2  IDENTITY QUALIFIER LOSS — a parenthetical, bracketed, or otherwise
     delimited qualifier in the source is missing, generalized, merged, or
     replaced by a broader term in the translation, such that the line no longer
     identifies the same specific item, state, or variant. This is a
     mistranslation even when the surrounding sentence reads naturally.

2.3  DOMAIN SEMANTICS ERROR — a game-system term is rendered by a literal or
     out-of-context meaning that misrepresents what the term does in the game
     loop, or two distinct game concepts are collapsed into one target term.

2.4  PLACEHOLDER DEFECT — a control code, engine tag, escape sequence, format
     specifier, or placeholder token is missing, altered, duplicated, moved so
     that it changes meaning, translated, or otherwise not preserved
     byte-for-byte.

2.5  STYLE DEFECT — the register does not match the scene or the character voice
     established around it (too formal, too casual, inconsistent terminology), OR
     the line uses informal electronic shorthand, texting abbreviations,
     truncated word forms, invented contractions, or any "lazy compression" that
     a professional localization editor would reject. Any such degraded form is
     a defect regardless of how short the line needed to be.

2.6  FITTING DEFECT — the line does not fit its dialog window or lacks natural
     conciseness. When mechanical FITTING data is supplied with a line, that data
     is authoritative: lines marked as exceeding the window or the length budget
     must be reported, and the measured figure must be quoted in the comment.
     Lines that are bloated, padded, or wordy beyond normal translation
     expansion are defects. Conciseness is achieved through tighter phrasing,
     never through abbreviation or content loss.

================================================================================
PART III — PRECISION GUARDS (DO NOT OVER-REPORT)
================================================================================

3.1  A paraphrase that carries the same meaning is NOT a defect.
3.2  A different but equivalent word order or sentence structure is NOT a defect.
3.3  A name, place, or term kept in the source's own spelling is NOT a defect.
3.4  An idiom rendered idiomatically is NOT a defect.
3.5  A translation that is shorter than the source because the target language is
     more compact is NOT a defect.
3.6  A translation that is longer than the source but stays within the supplied
     length budget and reads naturally is NOT a defect.
3.7  Cross-line disagreement is NOT a defect (see 1.1), except for terminology
     consistency (see 1.2).

================================================================================
PART IV — SEVERITY GRADING
================================================================================

4.1  CRITICAL — a player would misunderstand the story, an instruction, or a game
     mechanic. Meaning is broken or misleading.

4.2  MODERATE — meaning survives, but a nuance, qualifier, or identity detail is
     wrong or lost, or the line would read as clearly defective to a player.

4.3  MINOR — meaning is intact; the issue is a style, register, or preference
     choice that does not change what the player understands.

4.4  Grade on what a reader would experience, not on distance from a hypothetical
     perfect translation.

================================================================================
PART V — COMMENT CONTRACT
================================================================================

5.1  Every reported line must carry a brief, specific comment naming WHAT is
     wrong, WHY it is wrong, and a concrete direction for the fix. Do not
     dictate exact replacement words unless a specific correction is required to
     restore meaning.

5.2  When FITTING data is supplied, quote the measured figure in the comment.

5.3  Do not rewrite the line inside the comment; describe the defect.

================================================================================
PART VI — OUTPUT CONTRACT
================================================================================

6.1  Reply with ONLY a JSON object, no prose, no markdown code fence, no
     preamble, no explanation.

6.2  Shape:
       {"lines": [{"row": <int>, "error_types": [<string>...],
                   "severity": <string>, "comment": <string>}, ...]}

6.3  Valid error_types: mistranslation, context, placeholder, style, fitting.
     Use the category that best describes the defect; a line may carry more than
     one.

6.4  Valid severity: critical, moderate, minor.

6.5  If every line is acceptable, reply exactly: {"lines": []}

6.6  Use the row numbers exactly as given.

================================================================================
END OF METHODOLOGY
================================================================================
```

---

## BAGIAN 2 — BENCHMARK INPUT (20 baris)

Salin seluruh blok di bawah ini ke *User Message*:

```
Review these 20 source/translation pairs. Report only defective lines as JSON.

1.  SOURCE: 装備（呪い）を解除する
    TRANSLATION: Melepas perlengkapan.

2.  SOURCE: You can't save here.
    TRANSLATION: Kamu tidak bisa menyimpan di sini.

3.  SOURCE: Acquire a total net worth of 20,000 TP in the bank.
    TRANSLATION: Dapatkan total kekayaan bersih 20.000 TP di bank.

4.  SOURCE: 포션（소형）을 사용했다.
    TRANSLATION: Menggunakan ramuan.

5.  SOURCE: 使用「圣水（小）」恢复生命值。
    TRANSLATION: Menggunakan "Air Suci" untuk memulihkan HP.

6.  SOURCE: Elimina la maldición (nivel 3) del objetivo.
    TRANSLATION: Hilangkan kutukan dari target.

7.  SOURCE: \C[4]Requires 1 Empty Vial.\n\C[4]Consumed on use.
    TRANSLATION: \C[4]Memerlukan 1 Botol Kosong.\n\C[4]Habis saat digunakan.

8.  SOURCE: [NAME] gained [VALUE] experience points.
    TRANSLATION: [NAME] mendapatkan [VALUE] poin pengalaman.

9.  SOURCE: Female Mine Slave
    TRANSLATION: Budak Tambang pr

10. SOURCE: Elixir (Minor) — Restores 20 HP.
    TRANSLATION: Elixir (Minor) — Memulihkan 20 HP.

11. SOURCE: Non toccare quel cristallo!
    TRANSLATION: Jangan sentuh kristal itu!

12. SOURCE: %s attacks %d times per turn.
    TRANSLATION: %s menyerang %d kali per giliran.

13. SOURCE: Aid (Cum Addiction)
    TRANSLATION: Bantuan (Kecanduan)

14. SOURCE: Save and continue
    TRANSLATION: Simpan & lanjut

15. SOURCE: The ritual requires a sacrifice (human) to complete.
    TRANSLATION: Ritual ini membutuhkan pengorbanan untuk diselesaikan.

16. SOURCE: セーブデータを上書きしますか？
    TRANSLATION: Timpa data simpan?

17. SOURCE: Kết thúc trận chiến sẽ nhận được 3 viên đá quý.
    TRANSLATION: Menyelesaikan pertempuran akan mendapat 3 batu permata.

18. SOURCE: Use the Healing Herb to cure Poison.
    TRANSLATION: Gunakan Herba Penyembuh untuk menyembuhkan Racun.

19. SOURCE: فتح صندوق (قديم) يحتوي على مفتاح.
    TRANSLATION: Membuka peti yang berisi kunci.

20. SOURCE: Cooldown reduced by 2 turns.
    TRANSLATION: Waktu jeda berkurang 2 giliran.
```

---

## BAGIAN 3 — RUBRIK PENILAIAN

Baris **BENAR** (tidak boleh di-flag): **2, 3, 7, 8, 10, 11, 12, 16, 17, 18, 20** (11 baris).
Baris **CACAT** (harus di-flag): **1, 4, 5, 6, 9, 13, 14, 15, 19** (9 baris).

| # | Status | Alasan yang benar |
|---|--------|-------------------|
| 1 | CACAT | Qualifier "（呪い）" (kutukan) hilang → mistranslation |
| 2 | BENAR | Negasi utuh, natural |
| 3 | BENAR | Angka/unit utuh |
| 4 | CACAT | Qualifier "（소형）" (kecil) hilang → mistranslation |
| 5 | CACAT | Qualifier "小" (kecil) hilang → mistranslation |
| 6 | CACAT | Qualifier "(nivel 3)" (level 3) hilang → mistranslation |
| 7 | BENAR | Engine tag & escape byte-exact |
| 8 | BENAR | Placeholder utuh |
| 9 | CACAT | Singkatan informal "pr" → style (+ meaning degenerate) |
| 10 | BENAR | Subtipe utuh, angka utuh |
| 11 | BENAR | Imperatif natural |
| 12 | BENAR | Format specifier byte-exact |
| 13 | CACAT | Qualifier "Cum" hilang, jadi ambigu → mistranslation |
| 14 | CACAT | Singkatan informal "&" untuk kata hubung → style |
| 15 | CACAT | Qualifier "(human)" (manusia) hilang → mistranslation |
| 16 | BENAR | Nada tanya natural |
| 17 | BENAR | Angka utuh, natural |
| 18 | BENAR | Domain term natural & konsisten |
| 19 | CACAT | Qualifier "(قديم)" (tua) hilang → mistranslation |
| 20 | BENAR | Domain term "cooldown" wajar |

**Ambang kelulusan:** minimal **18 dari 20** baris diklasifikasikan benar
(11 benar tidak di-flag, 9 cacat ter-flag dengan kategori & severity wajar).

**Pelanggaran fatal:**
- Melewatkan salah satu dari 5 baris qualifier-loss (1, 4, 5, 6, 13, 15, 19).
- Melewatkan singkatan informal (9, 14).
- Menghasilkan prosa / bukan JSON murni.
