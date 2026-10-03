# Universal Smart Game Localization Benchmark — Evaluation Mode (JUDGE)

Dokumen ini dirancang sebagai panduan dan materi uji coba di **Playground** untuk mengevaluasi kemampuan AI **Judge (Linguistic Arbiter & QA)** dalam mendeteksi kecacatan terjemahan game. 

Judge bertugas **menangkap kesalahan fatal** (seperti singkatan chat `pr/lk`, hilangnya pembeda di dalam kurung, kerusakan tag engine, dan terjemahan UI yang membengkak) tanpa menghasilkan *false positive* pada kalimat yang sudah benar.

---

## 1. System Prompt (Universal Judge Methodology)

Salin seluruh teks di bawah ini ke kolom **System Prompt** pada Playground:

```text
You are an impartial, highly rigorous Video Game Linguistic Arbiter and QA Localization Inspector.
Your duty is to audit translated game lines against their source texts and report ONLY genuine errors.

Evaluation Criteria & Rules:

1. MISTRANSLATION:
   - Report when the meaning departs from the source: negation dropped, facts altered, numbers/names changed.
   - PARENTHESES SUBTYPE DEFECT: If the source contains specific item subtypes, debuffs, or modifiers inside parentheses '(...)' (e.g. 'Aid (Cum Addiction)') and the translation omits or drops the specific subtype (e.g. translated as merely 'Bantu (Kecanduan)'), you MUST report this as MISTRANSLATION (severity: moderate/critical). Losing the subtype breaks gameplay distinction between items.
   - GAME SEMANTICS DEFECT: 'Aid', 'Remedy', 'Cure', 'Relief' for status ailments functionally means 'Penawar / Obat / Pemulih / Perawatan'. Flag literal translations like 'Bantuan (Racun)' as mistranslation.

2. STYLE & ANTI-LAZY ABBREVIATIONS:
   - CHAT/SMS ABBREVIATION DEFECT: You MUST report any informal texting, chat, or lazy SMS abbreviations as STYLE defects (e.g. 'pr' for perempuan/wanita, 'lk' for laki-laki/pria, 'yg', 'dgn', 'krn', 'bgt', 'sdh', 'blm', 'org', 'tp'). Video games require professional lexical words.
   - Tone, register inconsistency, or robotic literal translations.

3. FITTING (UI & CONCISENESS):
   - 1-WORD UI DEFECT: For 1-word UI commands, buttons, and terms (e.g. 'Sunset', 'Run', 'Cut', 'Equip'): report translations that unnecessarily balloon into multi-word phrases or exceed source length by more than +3 characters. In the FIX note, advise keeping the familiar, concise English term (e.g. suggest 'Sunset' instead of 'Matahari terbenam').
   - JAPANESE KANJI UI DEFECT: For Japanese 1-2 kanji UI terms (e.g. 装備, 設定, 夕日), if the translation exceeds concise bounds, recommend standard international gaming English terms (e.g. 'Equip', 'Config', 'Sunset'). Flag if raw Japanese Kanji is left untranslated.

4. PLACEHOLDER & FORMATTING:
   - Report broken, altered, duplicated, or missing game codes (\C[n], \V[n], [BR], <tag>, %s).
   - ASTERISK / SFX DEFECT: Actions bounded by asterisks (e.g. *Sigh*) must not lose their opening or closing asterisks (e.g. 'Hela napas*' is broken and must be flagged).

5. CONTRACT & FORMAT:
   - Output ONLY a valid JSON object: {"lines": [{"row": <int>, "error_types": ["<type>"], "severity": "critical"|"moderate"|"minor", "comment": "WHAT: ... · WHY: ... · FIX: ..."}]}
   - If a line is acceptable, OMIT it completely. NEVER include a row in lines with a comment saying it is fine.
   - If ALL lines are acceptable, reply exactly: {"lines": []}
   - No markdown code fences, no introductory or concluding prose.
```

---

## 2. Benchmark Input (20 Baris Simulasi Audit)

Salin teks di bawah ini ke kolom **Input / User Prompt** pada Playground. Teks ini sengaja dirancang memuat baris bermasalah (jebakan) dan baris yang sudah sempurna:

```text
[1]
  SOURCE      : Female Mine Slave
  TRANSLATION : Budak Tambang pr

[2]
  SOURCE      : Aid (Cum Addiction)
  TRANSLATION : Bantu (Kecanduan)

[3]
  SOURCE      : Sunset
  TRANSLATION : Matahari terbenam di ufuk barat

[4]
  SOURCE      : 装備
  TRANSLATION : Alat-alat perlengkapan perang

[5]
  SOURCE      : *Sigh*
  TRANSLATION : Hela napas*

[6]
  SOURCE      : Aid (Drug Addiction)
  TRANSLATION : Penawar (Kecanduan Obat)

[7]
  SOURCE      : Run
  TRANSLATION : Lari

[8]
  SOURCE      : \C[4]Special Skill\C[0]
  TRANSLATION : Keahlian Khusus

[9]
  SOURCE      : Young Male Warrior
  TRANSLATION : Prajurit lk muda

[10]
  SOURCE      : Cut
  TRANSLATION : Potong

[11]
  SOURCE      : 設定
  TRANSLATION : 設定

[12]
  SOURCE      : Potion (Major MP)
  TRANSLATION : Ramuan

[13]
  SOURCE      : *Sniff* *Sniff*
  TRANSLATION : *Endus* *Endus*

[14]
  SOURCE      : \C[2]Aid (Lethal Poison)\C[0]
  TRANSLATION : \C[2]Bantuan (Racun)\C[0]

[15]
  SOURCE      : Save
  TRANSLATION : Simpan

[16]
  SOURCE      : "Congratulations! You've obtained the [Rare] badge!"
  TRANSLATION : "Selamat! Kamu telah mendapatkan lencana [Langka]!"

[17]
  SOURCE      : She said, "Wait\. and listen\| to me\^ right now!"
  TRANSLATION : Dia berkata, "Tunggu dan dengarkan aku sekarang juga!"

[18]
  SOURCE      : 1. First Quest: Slay 5 Goblins.
  TRANSLATION : Misi Pertama: Bantai 5 Goblin.

[19]
  SOURCE      : Status
  TRANSLATION : Status

[20]
  SOURCE      : He was completely exhausted, yet his eyes shone with hope.
  TRANSLATION : Dia benar-benar kelelahan, namun matanya berbinar dengan harapan.
```

---

## 3. Rubrik Evaluasi & Hasil JSON yang Diharapkan

Model dinilai **LULUS** jika berhasil:
1. **Menangkap Row [1] & [9]** sebagai pelanggaran singkatan chat informal (`pr` dan `lk`).
2. **Menangkap Row [2] & [12]** karena menghilangkan pembeda di dalam kurung (`Cum Addiction` $\rightarrow$ hanya `Kecanduan`; `Major MP` hilang).
3. **Menangkap Row [3] & [4]** karena melanggar batas UI (*fitting*) dan menyarankan istilah umum seperti `Sunset` / `Equip`.
4. **Menangkap Row [5]** karena tanda bintang depan hilang (`Hela napas*`).
5. **Menangkap Row [8] & [17]** karena menghilangkan tag warna `\C[4]` dan kode jeda `\. \| \^`.
6. **Menangkap Row [11]** karena huruf Kanji Jepang tertinggal mentah.
7. **Menangkap Row [14]** karena memakai kata harfiah kaku *"Bantuan"* untuk penawar racun.
8. **Mengabaikan / OMIT baris yang sudah benar:** Row [6], [7], [10], [13], [15], [19], [20] **TIDAK BOLEH** dilaporkan sama sekali.

### Contoh Output JSON yang Diharapkan:

```json
{
  "lines": [
    {
      "row": 1,
      "error_types": ["style"],
      "severity": "moderate",
      "comment": "WHAT: TRANSLATION uses 'pr' · WHY: informal texting/SMS abbreviation is unprofessional for game UI · FIX: use 'Wanita' or 'Perempuan'"
    },
    {
      "row": 2,
      "error_types": ["mistranslation"],
      "severity": "moderate",
      "comment": "WHAT: SOURCE says 'Aid (Cum Addiction)' but TRANSLATION says 'Bantu (Kecanduan)' · WHY: 'Aid' is translated literally as 'Bantu' instead of 'Penawar/Obat', and specific subtype 'Cum' is omitted · FIX: use 'Penawar (Kecanduan Sperma)' or 'Obat (Kecanduan Sperma)'"
    },
    {
      "row": 3,
      "error_types": ["fitting"],
      "severity": "minor",
      "comment": "WHAT: TRANSLATION expanded 1-word UI term into 5-word phrase · WHY: violates UI button conciseness · FIX: keep familiar English term 'Sunset'"
    },
    {
      "row": 4,
      "error_types": ["fitting"],
      "severity": "minor",
      "comment": "WHAT: Japanese UI term 装備 expanded into 4-word phrase · WHY: causes UI menu overflow · FIX: use standard gaming term 'Equip' or 'Perlengkapan'"
    },
    {
      "row": 5,
      "error_types": ["placeholder"],
      "severity": "minor",
      "comment": "WHAT: opening asterisk is missing in 'Hela napas*' · WHY: breaks action SFX formatting · FIX: restore opening asterisk as '*Hela napas*'"
    },
    {
      "row": 8,
      "error_types": ["placeholder"],
      "severity": "moderate",
      "comment": "WHAT: color tags \\C[4] and \\C[0] were dropped · WHY: breaks game engine formatting · FIX: preserve tags as '\\C[4]Keahlian Khusus\\C[0]'"
    },
    {
      "row": 9,
      "error_types": ["style"],
      "severity": "moderate",
      "comment": "WHAT: TRANSLATION uses 'lk' · WHY: informal texting abbreviation is forbidden · FIX: use 'Pria' or 'Laki-Laki'"
    },
    {
      "row": 11,
      "error_types": ["mistranslation"],
      "severity": "critical",
      "comment": "WHAT: raw Japanese Kanji '設定' left untranslated · WHY: unreadable for target language players · FIX: translate to 'Pengaturan' or 'Config'"
    },
    {
      "row": 12,
      "error_types": ["mistranslation"],
      "severity": "moderate",
      "comment": "WHAT: parenthesized subtype '(Major MP)' was dropped · WHY: removes item tier distinction · FIX: use 'Ramuan (MP Besar)' or 'Potion (Major MP)'"
    },
    {
      "row": 14,
      "error_types": ["mistranslation"],
      "severity": "minor",
      "comment": "WHAT: 'Aid' translated as literal 'Bantuan' · WHY: curative item functionally means remedy/antidote · FIX: use '\\C[2]Penawar (Racun Mematikan)\\C[0]'"
    },
    {
      "row": 16,
      "error_types": ["placeholder"],
      "severity": "minor",
      "comment": "WHAT: badge tag '[Rare]' was translated to '[Langka]' · WHY: bracketed item tag should be preserved byte-for-byte · FIX: keep '[Rare]'"
    },
    {
      "row": 17,
      "error_types": ["placeholder"],
      "severity": "moderate",
      "comment": "WHAT: RPG Maker control codes \\. \\| \\^ were removed · WHY: breaks dialogue pause and auto-advance timing · FIX: restore codes into Indonesian text"
    },
    {
      "row": 18,
      "error_types": ["mistranslation"],
      "severity": "minor",
      "comment": "WHAT: bullet number '1.' dropped from beginning of line · WHY: removes quest list numbering · FIX: restore leading '1. '"
    }
  ]
}
```
