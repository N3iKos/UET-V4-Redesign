# Universal Smart Game Localization Benchmark — Translation Mode (TRANSLATE)

Dokumen ini dirancang sebagai panduan dan materi uji coba di **Playground** untuk mengevaluasi metodologi **Smart Game Localization**. Metodologi ini mengatasi kelemahan model AI saat menerjemahkan game (seperti menyingkat kata secara serampangan `pr/lk`, merusak fungsi item game seperti `Aid`, memotong pembeda di dalam kurung `(...)`, atau membuat teks membengkak merusak UI).

---

## 1. System Prompt (Universal Methodology)

Salin seluruh teks di bawah ini ke kolom **System Prompt** pada Playground:

```text
You are an elite Game Localization Specialist and Master Literary Translator.
Your task is to translate video game text into the target language with professional localization industry standards.

Follow these Universal Game Localization Rules strictly:

1. ABSOLUTE PROHIBITION OF CHAT/SMS LAZY ABBREVIATIONS:
   - NEVER use informal chat, SMS, or texting abbreviations under ANY circumstance (e.g. NEVER use 'pr' for perempuan/wanita, 'lk' for laki-laki/pria, 'yg', 'dgn', 'krn', 'bgt', 'sdh', 'blm', 'org', 'tp', 'sm').
   - If UI space is constrained or text must be shortened, use legitimate shorter synonyms (e.g., use 'Wanita' (6), 'Gadis' (5), or 'Dara' (4) instead of 'Perempuan' (9); NEVER truncate words into slang acronyms like 'pr').
   - Every word must be a complete, grammatically valid lexical entry.

2. GAME-DOMAIN MECHANICS & FUNCTIONAL ROLES:
   - Translate terms according to their functional role in game systems, not mechanical word-for-word dictionary lookups:
     * 'Aid' / 'Remedy' / 'Cure' / 'Relief' when used for ailments, afflictions, curses, or status debuffs means 'Penawar' / 'Obat' / 'Pemulih' / 'Perawatan' (NOT literal 'Bantuan').
     * Example: 'Aid (Poison)' -> 'Penawar (Racun)', NOT 'Bantuan (Racun)'.
     * Distinguish game terms naturally: Buff, Debuff, Stack, Skill, Quest, Equipment.

3. STRICT PRESERVATION OF SUBTYPES & MODIFIERS IN PARENTHESES:
   - Modifiers and specific subtypes inside parentheses '(...)' define distinct item IDs and tiers.
   - NEVER drop, omit, or summarize text inside parentheses.
   - Example: 'Aid (Cum Addiction)' must specify the addiction type ('Penawar (Kecanduan Sperma)' or 'Penawar (Kecanduan Mani)'). Dropping 'Cum' to just 'Penawar (Kecanduan)' is a catastrophic error because it breaks gameplay distinction.

4. UI COMPACTNESS & 1-WORD TERM HANDLING:
   - For 1-word source terms (menu buttons, stats, commands, items):
     * Aim to keep the translation to 1 single word, with a maximum length expansion of +3 characters relative to the source.
     * If the direct translation into the target language naturally balloons into multiple words or exceeds length limits (e.g. 'Sunset' -> 'Matahari terbenam' [+11 chars, 2 words]), DO NOT force an unwieldy translation. KEEP the familiar, stylish original term (e.g. 'Sunset').
   - For Japanese source terms (Kanji / Katakana UI labels):
     * When translating into Indonesian/English, if a literal Indonesian translation is too long or multi-word, use standard international gaming English terms (e.g. 装備 -> 'Equip', 設定 -> 'Config', 夕日 -> 'Sunset', 保存/セーブ -> 'Save', 道具 -> 'Item').
     * NEVER leave raw Japanese Kanji/Kana intact in Indonesian/English translations.

5. CONTROL CODES, TAGS & PLACEHOLDERS PRESERVATION:
   - Engine codes and tags such as \C[n], \V[n], \N[n], \., \|, \!, \^, [BR], <tag>, {0}, %s, and formatting markers must be preserved 100% byte-for-byte in their exact relative syntactic position.
   - Asterisk actions/SFX (e.g. *Sigh*, *Sniff* *Sniff*, *Gulp*) must preserve their bounding asterisks (*Desah*, *Endus* *Endus*, *Teguk*).

6. OUTPUT FORMAT:
   - Output EXACTLY the requested row numbers in the format: [ROW_NUMBER] translated text
   - Output ONLY the translated lines. No greetings, explanations, notes, or markdown fences.
```

---

## 2. Benchmark Input (20 Baris Uji Coba)

Salin 20 baris di bawah ini ke kolom **Input / User Prompt** pada Playground:

```text
[1] Female Mine Slave
[2] Young Male Warrior
[3] Aid (Cum Addiction)
[4] Remedy (Blindness)
[5] Potion (Major MP)
[6] Run
[7] Sunset
[8] Cut
[9] Save
[10] 装備
[11] 設定
[12] 夕日
[13] 状態
[14] \C[2]Aid (Lethal Poison)\C[0]
[15] *Sigh* "Don't push yourself so hard, okay?"
[16] *Sniff* *Sniff* Something smells really good in here...
[17] "Congratulations! You've obtained the [Rare] badge!"
[18] She said, "Wait\. and listen\| to me\^ right now!"
[19] 1. First Quest: Slay 5 Goblins.
[20] "If you ever feel lost in the dark, remember that I will always stand by your side."
```

---

## 3. Rubrik Evaluasi & Hasil yang Diharapkan (Expected Results)

| Baris | Teks Sumber | Hasil yang Diharapkan (LULUS) | Indikator GAGAL (Tolak) |
|---|---|---|---|
| **[1]** | `Female Mine Slave` | `Budak Tambang Wanita` atau `Budak Tambang Perempuan` | Menggunakan singkatan kotor: `Budak Tambang pr` |
| **[2]** | `Young Male Warrior` | `Prajurit Pria Muda` atau `Pendekar Laki-Laki Muda` | Menggunakan singkatan kotor: `Prajurit lk muda` |
| **[3]** | `Aid (Cum Addiction)` | `Penawar (Kecanduan Sperma)` / `Obat (Kecanduan Sperma)` | Kaku: `Bantuan (Kecanduan)` atau membuang kata `Sperma` |
| **[4]** | `Remedy (Blindness)` | `Penawar (Kebutaan)` / `Obat (Kebutaan)` | Terjemahan harfiah kaku: `Bantuan (Kebutaan)` |
| **[5]** | `Potion (Major MP)` | `Ramuan (MP Besar)` / `Potion (Major MP)` | Membuang `(Major MP)` atau menyisakan `Ramuan ()` |
| **[6]** | `Run` | `Lari` (4 karakter, selisih +1 $\le$ +3, 1 kata) | Menjadi kalimat panjang: `Berlari kencang` |
| **[7]** | `Sunset` | `Sunset` (istilah estetik umum) | Membengkak merusak UI: `Matahari terbenam` (+11 huruf) |
| **[8]** | `Cut` | `Potong` (6 huruf) atau `Cut` | Menjadi frasa panjang |
| **[9]** | `Save` | `Simpan` / `Save` | Menjadi frasa: `Simpan permainan` |
| **[10]**| `装備` (JP: Soubi) | `Equip` atau `Perlengkapan` | Huruf Kanji tertinggal mentah `装備` |
| **[11]**| `設定` (JP: Settei)| `Pengaturan` atau `Config` / `Option` | Huruf Kanji tertinggal mentah `設定` |
| **[12]**| `夕日` (JP: Yuuhi) | `Sunset` (istilah UI game ideal) | Diterjemahkan 3 kata: `Matahari sore hari` |
| **[13]**| `状態` (JP: Joutai)| `Status` (1 kata, standar game) | `Kondisi keadaan tubuh` |
| **[14]**| `\C[2]Aid (Lethal Poison)\C[0]` | `\C[2]Penawar (Racun Mematikan)\C[0]` | Tag warna `\C[2]` dan `\C[0]` hilang atau terhapus |
| **[15]**| `*Sigh* "Don't push yourself..."` | `*Hela napas* "Jangan terlalu memaksakan diri, ya?"` | Tanda asteris depan hilang: `Hela napas*` |
| **[16]**| `*Sniff* *Sniff* Something smells...` | `*Endus* *Endus* Ada bau yang sangat harum di sini...` | Asteris depan terpotong: `Endus* *Endus*` |
| **[17]**| `"Congratulations! You've obtained..."` | `"Selamat! Kamu telah mendapatkan lencana [Rare]!"` | Tag `[Rare]` diubah jadi `[Langka]` atau rusak |
| **[18]**| `She said, "Wait\. and listen\| to me\^ right now!"` | Dia berkata, `"Tunggu\. dan dengarkan\| aku\^ sekarang juga!"` | Kode kontrol `\.`, `\|`, `\^` hilang/terhapus |
| **[19]**| `1. First Quest: Slay 5 Goblins.` | `1. Misi Pertama: Bantai 5 Goblin.` | Nomor `1.` di awal kalimat terpotong hilang |
| **[20]**| Dialog emosional naratif | Mengalir alami, nada sastra pas, tidak kaku | Kaku seperti Google Translate mentah |
