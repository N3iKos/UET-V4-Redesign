# Design Spec: RPG Maker Text Fitting — Pra-Inject Overflow Audit & Judge Criterion "Fitting"

> Revisi 2 — memuat amandemen hasil diskusi: FileEngineScreen gabungan,
> flag mesin pre-AI, newline-tag dari source (bukan config), whitelist
> pasca-fix, kalibrasi rasio per pasangan bahasa, dan daftar limitasi.

## Overview

Terjemahan Bahasa Indonesia membengkak 30–60% dari sumber Jepang, sedangkan RPG
Maker vanilla **tidak auto-wrap** (teks tembus ke kanan layar) dan kotak dialog
maksimal **4 baris** (baris ke-5+ tenggelam/tak dirender). Satu-satunya cara
mengetahui hari ini adalah playtest manual berulang. Fitur ini memindahkan
verifikasi tersebut ke dalam UET dengan pengukuran deterministik + verdict AI.

Prinsip arsitektur: **UET mengukur, AI menilai.** Model LLM tidak bisa mengukur
pixel — semua metrik dihitung deterministik oleh modul `fitting/` murni, lalu
angka faktual itu disuntikkan ke prompt translate/judge/fix. Flag faktual
(overflow/sinking) diputuskan **mesin sebelum AI melihat barisnya**; AI hanya
menulis saran perbaikan dan menilai keringkasan. Verdict yang menyangkut rasa
bahasa tetap milik AI + user.

Empat lapis saling melengkapi, minum dari satu sumber pengukuran:

| Lapis | Kapan | Apa | Biaya |
|---|---|---|---|
| 1. Wizard translate | Saat translate | Engine profile → budget disuntik ke prompt (soft) | +2–3 baris prompt |
| 2. Judge kriteria `fitting` | Saat review | Metrik per baris (flag sudah oleh mesin) + saran AI | 1 kriteria |
| 3. Fix All / Fix It | Saat perbaikan | AI memperpendek / sisip tag line-break, re-measure wajib | Alur default |
| 4. Layar Audit | Kapan saja | Word-wrap mekanis massal, file lama | Gratis, instan |

**Siapa memutuskan apa** (anti-halu, ringkas):

| Keputusan | Pemutus |
|---|---|
| Berapa lebar/berapa baris suatu teks | Mesin (`fitting/`) |
| Flag overflow / sinking | Mesin (pre-AI) |
| Baris bertele-tele perlu diringkas | AI (wilayah judgment, wajib kutip rasio mesin) |
| Saran peringkasan / titik sisip tag | AI (proposal saja) |
| Saran diterima atau tidak | Re-measure mesin + user |

---

## 1. Modul `translator/fitting/` (baru — fungsi murni, pola `parsing/`)

### 1.1 `profiles.py` — EngineProfile
```python
@dataclass(frozen=True)
class EngineProfile:
    name: str                    # "rpgm-mv-mz", "rpgm-vxace", ...
    token_patterns: tuple[str, ...]   # regex control codes yang di-strip saat mengukur
    char_widths: dict[str, float]     # metrik font default, unit em, WAJIB termasuk lebar CJK/fullwidth
    window_cols: float                 # lebar window dalam em
    max_lines: int                    # 4
    safe_margin: float                # 0.90
    ratio_threshold: float            # ambang ratio per pasangan bahasa (lihat 1.4b)
```
- `char_widths` wajib meng-cover: Latin (i≈0.25, n≈0.5, m/w≈0.85, W≈1.0,
  spasi≈0.3, digit≈0.5), **CJK/fullwidth≈1.0** (karakter Jepang di source adalah
  mayoritas kasus — tanpa ini semua rasio salah), tanda baca umum.
- **Tidak ada field `newline_token`.** Tag line-break engine tidak diketahui
  dari config — dideteksi dari source per baris (lihat 1.2b). Ini keputusan:
  sumber kebenaran adalah bukti di teks, bukan asumsi UET.
- V1 ships: `rpgm-mv-mz`, `rpgm-vxace` (XP berbagi profil), `rpgm-2000`,
  `kirikiri`. V1 divalidasi nyata hanya di `rpgm-mv-mz` + `kirikiri` (ada
  sample data); sisanya best-effort, ditandai "unverified" di UI profile.
- **Kalibrasi `window_cols`**: angka awal `rpgm-mv-mz` = 32 em. WAJIB
  dikalibrasi sebelum rilis: render 5 teks uji di game MV vanilla asli,
  screenshot, bandingkan batas tembus vs prediksi UET; koreksi em-per-karakter
  dari delta. (Tugas implementasi, bukan asumsi.)

### 1.2 `measure.py` — pengukuran
```python
def strip_tokens(text: str, profile: EngineProfile) -> str
def measure_lines(text: str, profile: EngineProfile) -> list[float]   # lebar per segmen \n, em
def detect_newline_tag(source_row: str) -> str | None                # lihat 1.2b
```
- Control code di-strip sebelum mengukur (masalah #3: `len` biasa menghitung
  `\C[1]` sebagai 5 huruf). Pengukuran per segmen `\n`.

### 1.2b Deteksi tag line-break dari source (keputusan diskusi)
Dua hal berbeda yang tidak boleh tertukar:
- **Pemisah segmen (untuk pengukuran)**: karakter newline asli (0x0A) di dalam
  sel DAN tag teks — keduanya dianggap jeda oleh `measure_lines`/`wrap.py`.
- **Tag sisip (untuk saran AI)**: HANYA tag teks yang literal muncul di sel —
  sesuatu yang bisa AI ketik dalam output satu-baris dan aman round-trip
  lewat `encode_placeholders`. Karakter 0x0A asli BUKAN tag sisip: AI tidak
  bisa menghasilkannya lewat protokol `[N]` per baris.

`detect_newline_tag(source_row)` scan SATU baris source dan cari tag teks,
prioritas: `\n` literal (backslash-n) → `[r]` → `[br]` → `[BR]`. Rumah fungsi:
`measure.py`. Hasil:
- Tag teks ditemukan → AI Judge diberi tip verbatim per baris: "this game's
  line break token, as it appears in the source: `[r]` — if you suggest a
  line break, suggest exactly this token".
- Tidak ada tag teks (termasuk sel yang hanya punya 0x0A asli) → prompt
  **tidak menyebut line break sama sekali**; AI hanya boleh menyarankan
  peringkasan. Tanpa bukti yang bisa disisipkan, tanpa saran sisipan —
  konservatif; pemisah 0x0A tetap dihitung saat mengukur.
- Deteksi dilakukan per-baris pada source, bukan global file (file bisa
  campur event type).

### 1.3 `wrap.py` — simulasi word-wrap
```python
def wrap_lines(text: str, profile: EngineProfile) -> list[str]
def wrap_metrics(text: str, profile: EngineProfile) -> WrapMetrics
```
`WrapMetrics`: `lines`, `max_width`, `budget`, `overflow_lines`, `fits`,
`max_ok` (≤ max_lines). Greedy word-wrap: pecah di spasi; token tak boleh
terbelah antar baris.

### 1.4 `audit.py` — findings (flag oleh mesin)
```python
@dataclass
class Finding:
    row: int; source_wrap: WrapMetrics; target_wrap: WrapMetrics
    ratio: float; kinds: tuple[str, ...]

def audit_rows(rows, profile) -> list[Finding]
```
- `overflow` (**moderate**): ada baris > budget → tembus samping.
- `sinking` (**critical**): butuh > `max_lines` baris → baris ke-5+ tak terender.
- `ratio` (info, bukan flag — lihat 1.4b): target lebih panjang dari source.
- Severity ditetapkan di SINI oleh mesin, anchor tunggal untuk prompt & report:
  `sinking=critical`, `overflow=moderate`, `ratio=info`. (Satu sumber
  kebenaran severity — kontradiksi lama spec dihapus.)
- Baris dengan kolom Translated kosong → **skip** (bukan error, bukan flag):
  audit hanya menilai baris yang ada isinya.

### 1.4b Kalibrasi ratio per pasangan bahasa (perbaikan kritikal)
Rasio lebar target/source TIDAK bisa pakai satu ambang global: kanji ≈1.0em,
Latin ≈0.5em, sehingga terjemahan JP→ID yang sudah efisien pun secara normal
berkisar 130–160%. Ambang 130% flat = badai false positive.
- `ratio_threshold` hidup di **EngineProfile** dengan nilai awal
  **per pasangan bahasa** yang disuntik dari konfigurasi run (bukan hardcode
  global): default `ja→id = 1.8`, `en→id = 1.4` (ruang aman di atas ekspansi
  normal); profil lain menyusul sesuai kebutuhan nyata.
- `ratio` di report tampil sebagai **kolom info** (angkanya selalu terlihat),
  sedangkan FLAG `ratio` hanya jika melewati `ratio_threshold`. AI Judge
  menggunakan angka ini sebagai bahan WHAT wajib — bukan penentu flag sendiri.
- Angka default di atas adalah titik awal konservatif; kalibrasi ulang
  berbasis data dump nyata adalah tugas implementasi (cek distribusi rasio
  file dialog riil, potong ambang di atas persentil wajar).

### 1.5 `apply.py` — tulis balik sugesti audit
Menerima (row → teks wrap hasil UET) + path → backup `<name>.bak` → tulis
kolom Translated. Dipakai layar audit; Fix All tetap pakai jalur runner.

### 1.6 File lainnya
`fitting/__init__.py` export publik. Unit tests per modul di `tests/`.

---

## 2. Lapis 1 — FileEngineScreen (gabungan pilih file + engine profile)

### 2.1 Layar (menggantikan FileSelectScreen terpisah di Translate **dan** Judge)
Komponen bersama `FileEngineScreen` (dipakai Translate wizard & Judge file
screen — prinsip TODO: fungsi sama = komponen sama):

- Daftar file `.xlsx/.csv` di **box scroll tinggi tetap: maksimal 5 baris
  terlihat** (`max_visible=5`, properti, menyusut kalau file < 5). Tidak ada
  layar yang memanjang karena folder penuh.
- **Klik file → TIDAK langsung lanjut**; file terpilih dimasukkan ke kolom
  "path" di bawah. Kolom path tetap bisa diketik manual.
- Di bawah path: **select Engine** (rapi & compact): `General` (default),
  `RPG Maker MV/MZ`, `RPG Maker VX Ace/XP`, `RPG Maker 2000/2003`, `Kirikiri`,
  `AliceSoft`, `Wolf RPG` — sesuai inisiasi; yang belum terverifikasi diberi
  tanda.
- **Tombol "Confirm & Next"** — satu gerbang konfirmasi: file valid + engine
  tercatat → lanjut ke layar kolom.
- Keyboard flow utuh: `⏎` di list = pilih → fokus pindah ke path; `⏎` di
  path = Confirm & Next. Mouse user pakai tombol; keyboard user dua-enter.
- Output note ("Writes to ...") tetap seperti sekarang.

### 2.2 Engine "General"
- Default. Perilaku = UET hari ini persis: tidak ada pengukuran, tidak ada
  budget di prompt, kriteria Judge `fitting` tidak muncul, layar audit
  menolak file tanpa engine non-General.
- Bukan "profile kosong" — General adalah **mode off yang eksplisit**, tanpa
  objek profile di pipeline.

### 2.3 Ingatan per project (persistence)
Pilihan engine disimpan **di app prefs** (`data/app_prefs.py`) sebagai map
`{resolved_absolute_src_path: engine_name}`, LRU maksimal 50 entri. Saat
FileEngineScreen dibuka dan file terpilih (klik/-highlight): kalau path ada
di map → select engine otomatis ke nilai tersimpan (user bisa ganti). File
baru → General. Keputusan: app prefs (bukan sidecar file) supaya tidak
menambah file di folder kerja user dan konsisten dengan prefs lain.

### 2.4 Injeksi budget ke prompt translate (soft, guardrail makna)
Saat engine ≠ General, `build_translate_prompt` & `build_translate_solo_prompt`
menerima `fitting_budget` (teks ringkas: jumlah baris + lebar em + tag
line-break terdeteksi dari sampling baris-baris source di batch itu) dan
menyuntik:
```
- Text fitting: the game's dialog window fits {lines} lines x {cols} em.
  AIM to fit, but NEVER sacrifice meaning or drop content to fit — if both
  are impossible, keep the meaning; a later review pass will flag it.
- Do NOT insert line breaks yourself; keep each translation as one line.
```
- Sampling tag line-break untuk prompt: `detect_newline_tag` dijalankan pada
  semua source rows batch; hanya tag yang benar-benar muncul yang disebut.
- Terjemahan multi-baris dari sumber multi-baris tetap wajar: aturan "one
  line" berlaku pada struktur baris CSV (satu baris CSV = satu output row),
  bukan larangan `\n` di dalam sel — sel keluaran meniru struktur segmen
  source.
- `core/engine.py` meneruskan `fitting_budget` dari config wizard → prompts.
  Penulisan hasil translate tidak berubah.

---

## 2a. Lapis 4 — Layar Audit terpisah

Item menu baru "Text Fitting Audit" (MainMenu).
- Pilih file (Picker) → **wajib pilih engine non-General** (General → pesan
  "audit butuh engine profile; General tidak bisa mengukur", tombol kembali).
- Jalankan `audit_rows` → grid findings (reuse `CompareGrid`): No, Source
  (wrap), Target (wrap), % budget, baris, kinds, sugesti wrap UET.
- Baris kosong kolom Translated → skip, tidak muncul di grid (lihat 1.4).
- Aksi: apply per baris (modal konfirmasi), Apply All, ekspor temuan CSV.
- **Fitur pemecahan baris hanya aktif kalau `detect_newline_tag` menemukan
  tag di baris source-nya** — tanpa tag, sugesti wrap UET disembunyikan dan
  diganti arahan "ringkas via Judge". UET tidak pernah menyisipkan tag yang
  tidak dibuktikan ada di file itu.
- `apply.py` backup sebelum menulis.

---

## 3. Lapis 2 — Judge criterion `fitting` (ke-5)

### 3.1 Data model (`judge/models.py`)
`CRITERIA += "fitting"`; label: "Fitting — the line does not fit the game
dialog window (overflow / too many lines / much longer than source)".

### 3.2 Kondisional seperti `context`
`fitting` hanya tampil di layar kriteria & masuk prompt saat engine profile
non-General dipilih pada setup Judge (FileEngineScreen) — pola preseden
`context`-tanpa-kolom (AC-102). Diingat bersama remembered settings per model
yang sudah ada.

### 3.3 Metrik & flag disuntik per baris (`_rows_block` extend)
```
[42] SOURCE      : ...
    TRANSLATION : ...
    FITTING     : source 2 lines/78% · translation 5 lines/131% · OVERFLOW
                  line-break tag in source: [r]
```
- Angka & label OVERFLOW/SINKING datang dari `fitting/` — ground truth.
- Baris yang mesin sudah flag overflow/sinking **dikirim ke AI dengan flag
  terpasang** dan instruksi: AI TIDAK menilai ulang status muat/tidak (bukan
  wewenangnya), hanya menulis comment: WHAT (kutip data FITTING), WHY
  (konsekuensi ke pemain), FIX (saran).
- Baris yang tidak di-flag mesin: AI menilai keringkasan (wilayah judgment),
  WAJIB mengutip rasio di WHAT kalau menandai; ambang flag rasio = mesin
  (`ratio_threshold`), AI tidak bisa flag tanpa data pendukung.
- Tag line-break per baris: hanya muncul kalau `detect_newline_tag(source)`
  menemukannya; AI disuruh meniru tag itu persis. Tanpa tag → instruksi
  peringkasan saja, opsi line break tidak disebut.

### 3.4 Rule kriteria (`_CRITERION_RULES["fitting"]`)
- Kepanjangan (data mesin overflow/sinking): wajib flag (baris sudah
  terkirim ber-flag; AI menulis saran).
- Keringkasan: bertele-tele dibanding sumber secara proporsional — hanya
  boleh dikutip dengan rasio mesin; jangan menghukum baris yang sumbernya
  memang panjang.
- FIX hanya dua bentuk: (a) rephrasing ringkas natural (dilarang membuang
  fakta/nama/angka/klausa; pengetatan tata bahasa, bukan pemotongan makna;
  rujuk naturalness rule), atau (b) sisip tag line-break **persis seperti
  yang tercetak di baris source** di titik jeda frasa alami.
- Dilarang: membelah control code; menurunkan severity anchor mesin;
  mengarang tag baru; menambah baris kelima.

### 3.5 Fix All / Fix It (Lapis 3) — alur default tidak berubah
- `build_fix_prompt` menerima block FITTING per baris (metrik + tag
  line-break terdeteksi + severity anchor).
- **Re-measure wajib**, posisi eksplisit di alur StagedFix:
  1. AI menghasilkan fix →
  2. mesin re-measure (`wrap_metrics`) + **whitelist token check** (token di
     hasil fix yang tidak ada di source DAN tidak ada di daftar token
     profile → auto-flag "AI invented a token", fix ditolak) →
  3. gagal → retry (maks 2 putaran) → masih gagal → marker
     `[FITTING_STILL_FAIL]`, baris dilempar ke user untuk keputusan manual,
     TIDAK pernah ditulis diam-diam →
  4. lolos → masuk **staging preview** dengan status re-measure terlihat,
     baru user approve → tulis (jalur runner existing).
- `decode_placeholders` sudah memetakan `[BR][NL]` → `\n` (placeholders.py)
  — jalur aman tanpa perubahan.
- Fix hanya boleh menyisip tag line-break yang terdeteksi di source row-nya;
  tanpa tag → fix wajib berupa peringkasan.

### 3.6 Grading & report
`fitting` masuk grading normal dengan anchor: sinking=critical,
overflow=moderate (ditetapkan mesin di `audit.py`), ratio flag=minor (AI tidak
bisa menaikkan/menurunkan anchor mesin). Report grid & summary tampil otomatis
(ERROR_TYPES CRITERIA-driven).

---

## 4. Perubahan file (ringkas)

| File | Perubahan |
|---|---|
| `translator/fitting/*` (baru) | profiles, measure (incl. detect_newline_tag), wrap, audit, apply, __init__ |
| `translator/parsing/prompts.py` | param `fitting_budget` + blok soft rule (translate & solo) |
| `translator/core/engine.py` | teruskan fitting_budget |
| `translator/data/app_prefs.py` | map engine-per-project (LRU 50) |
| `translator/tui/screens/wizard.py` | FileEngineScreen menggantikan FileSelectScreen (box 5-file, klik→path, engine select, Confirm & Next) |
| `translator/tui/screens/judge.py` | pakai FileEngineScreen bersama; kriteria `fitting` tampil saat engine ≠ General; remembered settings |
| `translator/judge/models.py` | CRITERIA + label + severity anchor |
| `translator/judge/prompts.py` | rule fitting, FITTING line di _rows_block, fix prompt block, gating kondisional |
| `translator/judge/runner.py` | re-measure + whitelist + retry ≤2 + [FITTING_STILL_FAIL], posisi di StagedFix |
| `translator/tui/app.py` | item menu "Text Fitting Audit" |
| `translator/tui/screens/audit.py` (baru) | layar audit |
| `tests/*` | unit fitting + prompt snapshot + re-measure loop + whitelist |

## 5. Testing & Verification
- Unit: strip_tokens (RPGM + kirikiri), measure_lines (CJK vs Latin),
  detect_newline_tag (semua varian + tidak ada), wrap greedy (word boundary,
  token tak terbelah), audit kinds + ratio skip-empty, apply backup.
- Kalibrasi: uji rasio distribusi pada Dialog.csv riil; kalibrasi window_cols
  vs screenshot game MV vanilla (tugas implementasi, lihat 1.1/1.4b).
- Prompt snapshot: translate ±budget, judge ±profile, fix dengan FITTING
  block + tag verbatim.
- Loop fix: hasil overflow → flag ulang; whitelist menangkap token karangan;
  2 iterasi gagal → [FITTING_STILL_FAIL].
- UI manual: box 5-file scroll; klik→path; keyboard dua-enter; engine
  teringat saat buka ulang; audit menolak General; apply-all menulis .bak.
- Full `pytest`.

## 6. Limitasi yang diakui (bukan bug — batas desain V1)
- **Vanilla assumption**: game RPGM dengan plugin wordwrap (YEP Message Core
  dll.) membatalkan premis "tidak auto-wrap" — audit akan false-positive
  massal. Mitigasi: catatan di layar engine select ("fitting assumes the
  engine's default message window; wordwrap plugins make it inaccurate").
- **Hanya message window**: choice window / system message / battle log punya
  budget berbeda — di luar scope V1; dump per-tipe belum dibedakan.
- **Metrik font default + margin**: tidak membaca font game asli; presisi
  ±margin. Angka em dikalibrasi (1.1) tapi tetap estimasi.
- **Pecah row jadi dua row** (engine yang butuh baris CSV baru, bukan tag
  dalam-string) tidak diambil V1 — risiko alignment dump↔inject.
- `rpgm-2000`, `alice`, `wolf` unverified di V1 (best-effort profile).
- Auto-detect format dump variatif (masalah #5) tetap di luar V1.

## 7. Out of scope (V2 candidates)
- Custom profile user (lebar/font manual per project).
- Membaca font game asli untuk metrik presisi.
- Budget hard saat translate.
- Pembedaan budget per tipe window (choice/system/battle).
- Auto-apply wrap tanpa konfirmasi.
