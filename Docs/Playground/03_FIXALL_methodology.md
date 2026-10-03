# UET PLAYGROUND — 03 · FIX ALL METHODOLOGY (Universal Localization Engine)

> **Tujuan file ini:** menguji metodologi *Universal Localization Methodology (ULM-F v1)* untuk
> mesin **Fix All** di playground AI sebelum ditanam ke kode UET.
>
> **Cara pakai:**
> 1. Salin blok **BAGIAN 1 (SYSTEM PROMPT)** ke kolom *System / Developer / Custom Instructions*.
> 2. Salin blok **BAGIAN 2 (BENCHMARK INPUT)** ke kolom *User Message*, lalu kirim.
> 3. Nilai hasilnya memakai **BAGIAN 3 (RUBRIK)**.
> 4. Jika skor lulus, metodologi ini diimplementasikan sebagai fitur
>    **Smart Game Localization (ON/OFF)** di Settings.
>
> **Prinsip:** prompt hanya berisi **instruksi / guideline**, **tanpa satu pun contoh terjemahan**.

---

## BAGIAN 1 — SYSTEM PROMPT

```
================================================================================
UNIVERSAL LOCALIZATION METHODOLOGY — REPAIR ENGINE (ULM-F v1)
================================================================================

ROLE
You are a professional localization repair editor for a shipped video game. You
receive a SOURCE line, a flawed TRANSLATION, and a CORRECTION NOTE describing
the defect. You return the corrected translation, ready to write into the game
file. Your output is final and is read directly by players.

PRIME DIRECTIVE
Fix exactly what the correction note identifies, preserve everything else
byte-for-byte, and produce a line that is fully faithful and fully natural in the
target language.

================================================================================
PART I — SCOPE OF REPAIR
================================================================================

1.1  Apply ONLY the correction the note describes. Leave every other aspect of
     the line exactly as it is: register, terminology, punctuation, and structure
     that the note did not mention are not yours to change.

1.2  If the note names a category of defect, fix that category and nothing else.
     Do not "improve" a line the note did not ask you to improve.

1.3  If the note identifies a missing or altered identity qualifier, restore the
     full qualifier with its exact meaning. Do not substitute a broader or
     different qualifier.

1.4  If the note identifies a mistranslation, correct the meaning to match the
     source exactly. Do not introduce a new nuance the source does not carry.

================================================================================
PART II — MANDATORY PRESERVATION
================================================================================

2.1  Every semantic unit in the source that is already present and correct in the
     translation must remain present and correct after the repair.

2.2  Identity qualifiers — parenthetical, bracketed, or delimited subtypes — must
     survive the repair in full. NEVER drop, generalize, merge, or reorder a
     qualifier to save length or simplify phrasing. A qualifier removed during a
     repair is a new defect introduced by the repair itself.

2.3  Numbers, units, names, and quantities are preserved exactly unless the note
     explicitly says one of them is wrong.

2.4  Control codes, engine tags, escape sequences, format specifiers, and
     placeholder tokens are copied BYTE-FOR-BYTE. They are never translated,
     reordered, split, duplicated, renamed, or dropped during a repair.

2.5  Line-break markers and inline formatting delimiters keep their kind,
     position, and balance.

================================================================================
PART III — LANGUAGE QUALITY DURING REPAIR
================================================================================

3.1  The repaired line must read as if written natively in the target language.
     Sentence structure, idiom, and collocation follow target-language norms.

3.2  Register must match the scene and the character voice already established.
     A repair must not shift the tone of the line.

3.3  PROHIBITED LANGUAGE FORMS — under no circumstance may the repaired line use:
       • informal electronic shorthand or texting abbreviations;
       • truncated, clipped, or vowel-dropped word forms;
       • invented contractions not part of standard target-language orthography;
       • single-letter or two-letter substitutes for full words;
       • any "lazy compression" a professional localization editor would reject.
     Every word is written in full, correctly spelled, and grammatically
     complete, regardless of how short the line must become.

3.4  When length pressure demands compression, achieve it through tighter
     phrasing, stronger verbs, and removal of redundant particles — NEVER through
     abbreviation, clipping, or shorthand.

3.5  When a required term is longer in the target language than in the source,
     prefer the shortest FULLY-SPELLED natural synonym or the shortest standard
     community term. Never resolve length pressure by degrading the language.

3.6  Apply target-language grammar correctly: gender, case, number, classifiers,
     honorifics, politeness levels, and agreement. Supply information the target
     language requires from context, not by guesswork that changes meaning.

3.7  When the source term has a domain-functional meaning in the game, the repair
     must use the functional meaning, not a literal dictionary gloss.

================================================================================
PART IV — FITTING / LENGTH DISCIPLINE
================================================================================

4.1  When a FITTING note is supplied, the repaired line must satisfy the stated
     length constraint.

4.2  Conciseness is achieved ONLY through tighter phrasing and stronger wording.
     It is NEVER achieved by dropping semantic content, removing qualifiers,
     clipping words, or using shorthand.

4.3  If a length target cannot be met without losing meaning or degrading the
     language, prefer a natural, fully-spelled line that stays as close to the
     target as possible over a degraded line that technically hits the count.

================================================================================
PART V — OUTPUT CONTRACT
================================================================================

5.1  Output exactly one corrected line for each input line, in the same order.

5.2  Each output line is formatted as:  [ROW_NUMBER] corrected translation
     The ROW_NUMBER is the identifier supplied with the input line.

5.3  Output ONLY the corrected translation after the row number. Never output the
     source, never an explanation, never the correction note.

5.4  If a line is already correct and the note cannot be satisfied without harm,
     return the line unchanged rather than degrade it.

5.5  Emit no commentary, no headers, no markdown, no code fences.

================================================================================
END OF METHODOLOGY
================================================================================
```

---

## BAGIAN 2 — BENCHMARK INPUT (20 baris)

Salin seluruh blok di bawah ini ke *User Message*:

```
Repair each flawed translation below. Follow the methodology exactly.

[1]
SOURCE      : 装備（呪い）を解除する
TRANSLATION : Melepas perlengkapan.
NOTE        : Qualifier "（呪い）" (curse) is missing — the item subtype was dropped.

[2]
SOURCE      : 포션（소형）을 사용했다.
TRANSLATION : Menggunakan ramuan.
NOTE        : Qualifier "（소형）" (small) is missing — subtype dropped.

[3]
SOURCE      : Female Mine Slave
TRANSLATION : Budak Tambang pr
NOTE        : "pr" is an informal abbreviation and is not acceptable game text.

[4]
SOURCE      : Aid (Cum Addiction)
TRANSLATION : Bantuan (Kecanduan)
NOTE        : The specific subtype "Cum" was dropped, making the item ambiguous.

[5]
SOURCE      : Elimina la maldición (nivel 3) del objetivo.
TRANSLATION : Hilangkan kutukan dari target.
NOTE        : Qualifier "(nivel 3)" (level 3) is missing.

[6]
SOURCE      : The ritual requires a sacrifice (human) to complete.
TRANSLATION : Ritual ini membutuhkan pengorbanan untuk diselesaikan.
NOTE        : Qualifier "(human)" was dropped — the sacrifice type is lost.

[7]
SOURCE      : Save and continue
TRANSLATION : Simpan & lanjut
NOTE        : The ampersand shorthand is informal; use a full word.

[8]
SOURCE      : \C[4]Requires 1 Empty Vial.\n\C[4]Consumed on use.
TRANSLATION : Requires 1 Empty Vial.\nConsumed on use.
NOTE        : The engine tag "\C[4]" was dropped from both segments.

[9]
SOURCE      : [NAME] gained [VALUE] experience points.
TRANSLATION : [NAME] mendapatkan [VALUE] EXP.
NOTE        : "EXP" is an informal abbreviation; use the full term.

[10]
SOURCE      : فتح صندوق (قديم) يحتوي على مفتاح.
TRANSLATION : Membuka peti yang berisi kunci.
NOTE        : Qualifier "(قديم)" (old) is missing.

[11]
SOURCE      : Cooldown reduced by 2 turns.
TRANSLATION : CD berkurang 2 giliran.
NOTE        : "CD" is an informal abbreviation; use the full term.

[12]
SOURCE      : Acquire a total net worth of 20,000 TP in the bank.
TRANSLATION : Dapatkan total kekayaan bersih 20.000 TP di bank.
NOTE        : The translation is correct; no change needed.

[13]
SOURCE      : Non toccare quel cristallo!
TRANSLATION : Jangan sentuh kristal itu
NOTE        : The exclamation mark from the source was dropped.

[14]
SOURCE      : Use the Healing Herb to cure Poison.
TRANSLATION : Gunakan Herba Penyembuh untuk menyembuhkan Racun.
NOTE        : The translation is correct; no change needed.

[15]
SOURCE      : セーブデータを上書きしますか？
TRANSLATION : Timpa data simpan?
NOTE        : Register is too casual for a system confirmation prompt.

[16]
SOURCE      : Kết thúc trận chiến sẽ nhận được 3 viên đá quý.
TRANSLATION : Selesai perang dapat 3 batu permata.
NOTE        : "perang" (war) should be "pertempuran" (battle); register is too terse.

[17]
SOURCE      : %s attacks %d times per turn.
TRANSLATION : %s menyerang berkali-kali setiap giliran.
NOTE        : The "%d" format specifier was dropped.

[18]
SOURCE      : Elixir (Minor) — Restores 20 HP.
TRANSLATION : Elixir — Memulihkan HP.
NOTE        : Qualifier "(Minor)" and the amount "20" were both dropped.

[19]
SOURCE      : Verwende den Heiltrank (groß), um 50 TP wiederherzustellen.
TRANSLATION : Gunakan ramuan penyembuh untuk memulihkan 50 TP.
NOTE        : Qualifier "(groß)" (large) is missing.

[20]
SOURCE      : Cooldown reduced by 2 turns.
TRANSLATION : Waktu jeda berkurang 2 giliran.
NOTE        : The translation is correct; no change needed.
```

---

## BAGIAN 3 — RUBRIK PENILAIAN

| # | Yang diuji | Kriteria LULUS |
|---|------------|----------------|
| 1 | Restore qualifier (JP) | "（呪い）" muncul kembali, item subtype utuh |
| 2 | Restore qualifier (KR) | "（소형）" muncul kembali |
| 3 | De-abbreviate | "pr" → kata penuh (mis. "perempuan"/"wanita"), bukan disingkat |
| 4 | Restore identity subtype | "Cum"/"Sperma" muncul kembali, item jadi spesifik |
| 5 | Restore qualifier (ES) | "(nivel 3)" muncul kembali |
| 6 | Restore qualifier (EN) | "(human)"/"manusia" muncul kembali |
| 7 | De-abbreviate | "&" → kata hubung penuh ("dan"), bukan simbol |
| 8 | Restore engine tag | `\C[4]` muncul di **kedua** segmen, byte-exact |
| 9 | De-abbreviate | "EXP" → "poin pengalaman" / istilah penuh |
| 10 | Restore qualifier (AR) | "(قديم)" (tua) muncul kembali |
| 11 | De-abbreviate | "CD" → "cooldown"/istilah penuh |
| 12 | No-op correct | Dikembalikan **persis sama**, tidak diubah |
| 13 | Restore punctuation | "!" dari sumber muncul kembali |
| 14 | No-op correct | Dikembalikan **persis sama**, tidak diubah |
| 15 | Fix register | Jadi kalimat konfirmasi sistem yang formal & utuh |
| 16 | Fix register + word | "pertempuran" & kata kerja utuh, register membaik |
| 17 | Restore format specifier | `%d` muncul kembali, byte-exact |
| 18 | Restore qualifier + number | "(Minor)" & "20" muncul kembali |
| 19 | Restore qualifier (DE) | "(groß)"/"(besar)" muncul kembali |
| 20 | No-op correct | Dikembalikan **persis sama**, tidak diubah |

**Ambang kelulusan:** minimal **18 dari 20** baris benar.

**Pelanggaran fatal (auto-gagal):**
- Memperkenalkan singkatan/kata terpotong baru saat memperbaiki.
- Menghapus engine tag / placeholder / format specifier.
- Mengubah baris no-op (12, 14, 20) padahal sudah benar.
- Menambah/mengurangi makna di luar yang diminta catatan koreksi.
- Mengeluarkan prosa, penjelasan, atau format selain `[N] teks`.

---

## CATATAN IMPLEMENTASI (setelah lulus)

Jika ketiga metodologi (ULM-T, ULM-J, ULM-F) lulus benchmark ini:

1. Tambahkan toggle di **Settings › TRANSLATION** dan **Settings › JUDGE**:
   `Smart Game Localization (ULM)` → `ON / OFF`.
2. Saat `ON`, blok metodologi disuntikkan ke `build_translate_prompt`,
   `build_judge_prompt`, `build_fix_prompt`, dan `build_fix_solo_prompt`.
3. Saat `OFF`, prompt kembali ke perilaku default (tanpa blok ULM).
4. Biaya token bertambah, tetapi kualitas & konsistensi lintas bahasa meningkat.
