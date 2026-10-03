# Universal Smart Game Localization Benchmark — Repair Mode (FIX ALL)

Dokumen ini dirancang sebagai panduan dan materi uji coba di **Playground** untuk mengevaluasi kemampuan AI **Fix All (Repair Engine)** dalam memperbaiki baris terjemahan yang bermasalah.

Fix All bertugas **mengeksekusi perbaikan secara presisi** sesuai catatan koreksi (*Correction Note*) tanpa merusak bagian kalimat lain, **tidak menggunakan singkatan kotor (`pr/lk`)**, mengembalikan pembeda kurung, dan menjaga keutuhan kode kontrol game.

---

## 1. System Prompt (Universal Fix All Methodology)

Salin seluruh teks di bawah ini ke kolom **System Prompt** pada Playground:

```text
You are a Precision Game Localization Repair Specialist.
Your task is to correct flawed game translations based strictly on the provided SOURCE, faulty TRANSLATION, and CORRECTION NOTE.

Follow these Universal Repair Rules strictly:

1. OBEY THE CORRECTION NOTE PRECISELY:
   - Address the specific defect stated in the note.
   - Do NOT rewrite unrelated parts of the sentence or alter the character's tone unless requested.

2. ABSOLUTE BAN ON CHAT/SMS LAZY ABBREVIATIONS:
   - Even when instructed to shorten or compact text, NEVER use chat or texting abbreviations (e.g. NEVER use 'pr', 'lk', 'yg', 'dgn', 'krn', 'bgt', 'sdh', 'blm', 'org', 'tp').
   - Use legitimate full words (e.g., use 'Wanita' or 'Perempuan', NEVER 'pr'; use 'Pria', NEVER 'lk').

3. PRESERVE & RESTORE PARENTHESIZED DISTINCTIONS:
   - When repairing items, spells, or debuffs with parentheses '(...)', preserve the specific subtype or affliction (e.g. '(Kecanduan Sperma)' or '(Kecanduan Mani)', NEVER drop it to merely '(Kecanduan)').
   - Translate game functions naturally: 'Aid/Remedy/Cure' -> 'Penawar / Obat / Pemulih' (NOT literal 'Bantuan').

4. UI COMPACTNESS & FITTING REPAIR:
   - For 1-word UI terms: Keep to 1 single word. If translating into the target language naturally balloons into multiple words (e.g. 'Sunset' -> 'Matahari terbenam'), KEEP the familiar, stylish English term ('Sunset').
   - For Japanese UI terms (Kanji like 装備, 設定, 夕日): If natural target translation is overly long, use standard international gaming English ('Equip', 'Config', 'Sunset'). NEVER leave raw Japanese Kanji intact.

5. CONTROL CODES & ASTERISK FORMATTING:
   - Preserve all game engine tags (\C[n], \V[n], \., \|, \!, \^, [BR], <tag>, %s) exactly as shown.
   - If an action was missing its opening asterisk (e.g. 'Hela napas*'), restore it cleanly: '*Hela napas*'.

6. OUTPUT FORMAT CONTRACT:
   - Output EXACTLY the requested rows, in numerical order, using the format: [ROW_NUMBER] corrected translation
   - Put a newline before every [ROW_NUMBER] bracket.
   - Output ONLY the corrected translation lines. No markdown code fences, no explanations, no chat preamble.
```

---

## 2. Benchmark Input (20 Baris Uji Coba Perbaikan)

Salin teks di bawah ini ke kolom **Input / User Prompt** pada Playground:

```text
[1]
  SOURCE          : Female Mine Slave
  TRANSLATION     : Budak Tambang pr
  CORRECTION NOTE : WHAT: informal texting abbreviation 'pr' used · WHY: unprofessional for game UI · FIX: use 'Budak Tambang Wanita' or 'Budak Tambang Perempuan'

[2]
  SOURCE          : Aid (Cum Addiction)
  TRANSLATION     : Bantu (Kecanduan)
  CORRECTION NOTE : WHAT: 'Aid' translated literally as 'Bantu' and subtype 'Cum' was omitted · WHY: breaks gameplay distinction · FIX: use 'Penawar (Kecanduan Sperma)' or 'Obat (Kecanduan Sperma)'

[3]
  SOURCE          : Sunset
  TRANSLATION     : Matahari terbenam di ufuk barat
  CORRECTION NOTE : WHAT: 1-word UI term expanded into 5-word sentence · WHY: causes UI button overflow · FIX: keep familiar term 'Sunset'

[4]
  SOURCE          : 装備
  TRANSLATION     : Alat-alat perlengkapan perang
  CORRECTION NOTE : WHAT: Japanese UI term bloated into 4 words · WHY: UI menu overflow · FIX: use standard gaming term 'Equip' or 'Perlengkapan'

[5]
  SOURCE          : *Sigh*
  TRANSLATION     : Hela napas*
  CORRECTION NOTE : WHAT: opening asterisk is missing · WHY: action SFX formatting broken · FIX: restore opening asterisk as '*Hela napas*'

[6]
  SOURCE          : Young Male Warrior
  TRANSLATION     : Prajurit lk muda
  CORRECTION NOTE : WHAT: informal abbreviation 'lk' used · WHY: unprofessional chat slang · FIX: use 'Prajurit Pria Muda'

[7]
  SOURCE          : \C[4]Special Skill\C[0]
  TRANSLATION     : Keahlian Khusus
  CORRECTION NOTE : WHAT: color tags \C[4] and \C[0] were dropped · WHY: breaks game engine formatting · FIX: restore tags as '\C[4]Keahlian Khusus\C[0]'

[8]
  SOURCE          : 設定
  TRANSLATION     : 設定
  CORRECTION NOTE : WHAT: raw Japanese Kanji left untranslated · WHY: unreadable for players · FIX: translate to 'Pengaturan' or 'Config'

[9]
  SOURCE          : Potion (Major MP)
  TRANSLATION     : Ramuan
  CORRECTION NOTE : WHAT: parenthesized tier '(Major MP)' dropped · WHY: removes item distinction · FIX: restore as 'Ramuan (MP Besar)' or 'Potion (Major MP)'

[10]
  SOURCE          : \C[2]Aid (Lethal Poison)\C[0]
  TRANSLATION     : \C[2]Bantuan (Racun)\C[0]
  CORRECTION NOTE : WHAT: 'Aid' translated literally as 'Bantuan' and 'Lethal' dropped · WHY: curative item · FIX: use '\C[2]Penawar (Racun Mematikan)\C[0]'

[11]
  SOURCE          : "Congratulations! You've obtained the [Rare] badge!"
  TRANSLATION     : "Selamat! Kamu telah mendapatkan lencana [Langka]!"
  CORRECTION NOTE : WHAT: badge tag '[Rare]' was translated · WHY: game item tags must remain byte-for-byte · FIX: keep '[Rare]'

[12]
  SOURCE          : She said, "Wait\. and listen\| to me\^ right now!"
  TRANSLATION     : Dia berkata, "Tunggu dan dengarkan aku sekarang juga!"
  CORRECTION NOTE : WHAT: control codes \. \| \^ removed · WHY: breaks dialogue pause timing · FIX: restore codes into Indonesian text

[13]
  SOURCE          : 1. First Quest: Slay 5 Goblins.
  TRANSLATION     : Misi Pertama: Bantai 5 Goblin.
  CORRECTION NOTE : WHAT: bullet number '1.' dropped · WHY: quest numbering lost · FIX: restore leading '1. '

[14]
  SOURCE          : *Sniff* *Sniff*
  TRANSLATION     : Endus* *Endus*
  CORRECTION NOTE : WHAT: leading asterisk missing from first word · WHY: SFX formatting broken · FIX: restore as '*Endus* *Endus*'

[15]
  SOURCE          : Cut
  TRANSLATION     : Pemotongan bagian tertentu
  CORRECTION NOTE : WHAT: UI action term bloated into 3 words · WHY: UI button overflow · FIX: shorten to 1 word 'Potong' or keep 'Cut'

[16]
  SOURCE          : Remedy (Blindness)
  TRANSLATION     : Bantuan (Kebutaan)
  CORRECTION NOTE : WHAT: curative item translated as literal 'Bantuan' · WHY: status effect cure · FIX: use 'Penawar (Kebutaan)' or 'Obat (Kebutaan)'

[17]
  SOURCE          : 夕日
  TRANSLATION     : Matahari sore hari
  CORRECTION NOTE : WHAT: Japanese 2-kanji term expanded to 3 words · WHY: UI overflow · FIX: use familiar term 'Sunset'

[18]
  SOURCE          : *Pfft* "That's absurd."
  TRANSLATION     : Pfft* "Konyol sekali."
  CORRECTION NOTE : WHAT: leading asterisk missing · WHY: SFX broken · FIX: restore as '*Pfft* "Konyol sekali."'

[19]
  SOURCE          : Aid (Drug Addiction)
  TRANSLATION     : Bantu (Kecanduan)
  CORRECTION NOTE : WHAT: specific subtype 'Drug' omitted and 'Aid' translated literally · WHY: item confusion · FIX: use 'Penawar (Kecanduan Obat)'

[20]
  SOURCE          : Female Guard
  TRANSLATION     : Penjaga pr
  CORRECTION NOTE : WHAT: lazy slang abbreviation 'pr' used · WHY: forbidden chat abbreviation · FIX: use 'Penjaga Wanita' or 'Penjaga Perempuan'
```

---

## 3. Rubrik Evaluasi & Hasil Perbaikan yang Diharapkan

Model dinilai **LULUS** jika hasil outputnya persis mengikuti format `[ROW_NUMBER] corrected text` dan menerapkan solusi yang elegan tanpa singkatan chat:

```text
[1] Budak Tambang Wanita
[2] Penawar (Kecanduan Sperma)
[3] Sunset
[4] Equip
[5] *Hela napas*
[6] Prajurit Pria Muda
[7] \C[4]Keahlian Khusus\C[0]
[8] Config
[9] Ramuan (MP Besar)
[10] \C[2]Penawar (Racun Mematikan)\C[0]
[11] "Selamat! Kamu telah mendapatkan lencana [Rare]!"
[12] Dia berkata, "Tunggu\. dan dengarkan\| aku\^ sekarang juga!"
[13] 1. Misi Pertama: Bantai 5 Goblin.
[14] *Endus* *Endus*
[15] Potong
[16] Penawar (Kebutaan)
[17] Sunset
[18] *Pfft* "Konyol sekali."
[19] Penawar (Kecanduan Obat)
[20] Penjaga Wanita
```

### Kriteria Kelulusan Mutlak:
1. **Zero Chat Slang:** Tidak ada lagi kata `pr` atau `lk` di baris [1], [6], dan [20].
2. **Parentheses Restored:** Baris [2] dan [19] memiliki nama spesifik `(Kecanduan Sperma)` dan `(Kecanduan Obat)` secara lengkap dengan fungsi `Penawar/Obat` (bukan `Bantu`).
3. **Tags Restored:** Tag `\C[4]`, `\C[0]`, `\.`, `\|`, `\^`, `[Rare]` terpasang utuh.
4. **Asterisks Restored:** Baris [5], [14], dan [18] diawali tanda asteris `*`.
5. **Compact UI Clean:** Baris [3], [4], [8], [15], [17] sangat rapi dan siap masuk ke kotak tombol UI game tanpa *text overflow*.
