# Design Spec: RPG Maker Text Fitting — Pra-Inject Overflow Audit & Judge Criterion "Fitting"

## Overview

Terjemahan Bahasa Indonesia membengkak 30–60% dari sumber Jepang, sedangkan RPG
Maker vanilla **tidak auto-wrap** (teks tembus ke kanan layar) dan kotak dialog
maksimal **4 baris** (baris ke-5+ tenggelam/tak dirender). Satu-satunya cara
mengetahui hari ini adalah playtest manual berulang. Fitur ini memindahkan
verifikasi tersebut ke dalam UET dengan pengukuran deterministik + verdict AI.

Prinsip arsitektur: **UET mengukur, AI menilai.** Model LLM tidak bisa mengukur
pixel — semua metrik dihitung deterministik oleh modul `fitting/` murni, lalu
angka faktual itu disuntikkan ke prompt translate/judge/fix. Verdict dan
perbaikan yang menyangkut rasa bahasa tetap milik AI + user.

Empat lapis saling melengkapi, minum dari satu sumber pengukuran:

| Lapis | Kapan | Apa | Biaya |
|---|---|---|---|
| 1. Wizard translate | Saat translate | Engine profile → budget disuntik ke prompt (soft) | +2–3 baris prompt |
| 2. Judge kriteria `fitting` | Saat review | Metrik per baris + verdict AI: flag + saran | 1 kriteria |
| 3. Fix All / Fix It | Saat perbaikan | AI memperpendek / sisip `\n`, re-measure wajib | Alur default |
| 4. Layar Audit | Kapan saja | Word-wrap mekanis massal, file lama | Gratis, instan |

---

## 1. Modul `translator/fitting/` (baru — fungsi murni, pola `parsing/`)

### 1.1 `profiles.py` — EngineProfile
```python
@dataclass(frozen=True)
class EngineProfile:
    name: str                    # "rpgm-mv-mz", "rpgm-vxace", "kirikiri", ...
    token_patterns: tuple[str, ...]   # regex: r"\\V\[\d+\]", r"\\C\[\d+\]", r"\\N\[\d+\]", r"\\FS\[\d+\]", r"\\.", r"\|", r"\\!", r"\\>", r"\\^", r"\\\{", r"\\\}"
    char_widths: dict[str, float]     # default font metrics, unit = em (fraksi ukuran font)
    window_cols: int                  # lebar window dalam em (mis. 32)
    max_lines: int                    # 4
    safe_margin: float                # 0.90 — angka "muat" dihitung 90% dari kapasitas
```
- V1 ships: `rpgm-mv-mz`, `rpgm-vxace` (XP berbagi profil dengan VX Ace),
  `rpgm-2000`, `kirikiri`. Terlihat di tabel tapi V1 fokus validasi
  `rpgm-mv-mz`; lainnya menyusul sesuai kebutuhan project nyata.
- `char_widths` default: model letter-rata (`'n'`=0.5em, `'i'`=0.25em,
  `'m'`/`'w'`=0.85em, `'W'`=1.0em, spasi=0.3em, digit=0.5em). Tidak perlu file
  font game (keputusan: font default + margin aman).
- Profil dibangun sebagai data, bukan config user, V1.

### 1.2 `measure.py` — pengukuran lebar render
```python
def strip_tokens(text: str, profile: EngineProfile) -> str
def visible_length(text: str, profile: EngineProfile) -> float          # em, per segmen \n dijumlahkan TIDAK — lihat di bawah
def measure_lines(text: str, profile: EngineProfile) -> list[float]     # lebar per segmen \n
```
- Control code di-strip sebelum mengukur (masalah #3: `len` biasa menghitung
  `\C[1]` sebagai 5 huruf).
- Multi-line: per segmen `\n` (keputusan: "Per segmen \n").

### 1.3 `wrap.py` — simulasi word-wrap
```python
def wrap_lines(text: str, profile: EngineProfile) -> list[str]     # baris hasil greedy wrap
def wrap_metrics(text: str, profile: EngineProfile) -> WrapMetrics
```
`WrapMetrics`: `lines` (jumlah baris render), `max_width`, `budget`,
`overflow_lines` (baris yang tembus), `fits: bool`, `max_ok: bool` (≤ max_lines).

### 1.4 `audit.py` — findings
```python
@dataclass
class Finding:
    row: int; source_wrap: WrapMetrics; target_wrap: WrapMetrics
    ratio: float          # lebar render target / lebar render source
    kinds: tuple[str, ...]   # "overflow" | "sinking" | "ratio"

def audit_rows(rows: list[tuple[int, str, str]], profile: EngineProfile) -> list[Finding]
```
- `overflow` (moderate): suatu baris > budget → tembus samping.
- `sinking` (critical): butuh > `max_lines` baris → baris ke-5+ tak terender.
- `ratio` (minor): target > 130% lebar render source — indikasi peringkasan
  perlu, meski masih muat (guardrail #2: sumber panjang tidak dihukum absolut).

### 1.5 `apply.py` — tulis balik sugesti audit
Menerima daftar (row → teks wrap hasil UET) + path CSV → backup dulu (copy ke
`<name>.bak` sisi file, tidak perlu vault) → tulis kolom Translated. Dipakai
layar audit; Fix All tetap pakai jalur tulis balik runner yang sudah ada.

### 1.6 File lainnya
- `fitting/__init__.py` — export publik.
- Unit terkait di `tests/` mengikuti pola existing (test file per modul).

---

## 2. Lapis 1 — Opsi Fitting di Wizard Translate

Di `wizard.py`, satu opsi baru setelah pemilihan kolom (sebelum Pacing):
**"Text fitting (engine profile)"**.
- Default: **None** (perilaku hari ini, nol perubahan untuk user yang tidak pakai).
- Pilihan: engine profile dari `fitting/profiles.py` + tombol Skip.
- Tidak ada konfigurasi lebar/line di V1 — profile bawaan sudah mewakili default
  engine; user yang butuh custom menunggu profil baru, bukan form custom.

Saat aktif, `build_translate_prompt` menerima parameter baru
`fitting_budget` (opsional). Suntikan ke rules (guardrail "soft"):
```
- Text fitting: dialog window budget is {lines} lines x {cols} em. AIM to fit
  within it, but NEVER sacrifice meaning to fit — if both are impossible, keep
  the meaning; a later review pass will flag it.
- Do NOT insert line breaks yourself; keep the translation as one flow.
```
- Catatan: "jangan sisip \n sendiri" di translate supaya alur output tetap
  satu-baris-per-row seperti hari ini — pemecahan baris terjadi di fix/audit,
  bukan translate.
- `build_translate_solo_prompt`: suntikan blok yang sama.

### 2.1 Payload data
- `core/engine.py` meneruskan `fitting_budget` dari setup wizard ke
  `parsing/prompts.py` (thread-safe, hanya string konfigurasi).
- Tidak ada perubahan penulisan hasil translate — kolom output tetap satu.

---

## 2a. Lapis 4 — Layar Audit terpisah (dibangun sebelum Judge agar urutan pakai natural)

Item menu baru di MainMenu: **"Text Fitting Audit"**.
- Pilih file CSV/Excel → pilih engine profile → jalankan `audit_rows`.
- Grid findings (reuse `CompareGrid`): kolom = No, Source (wrap), Target (wrap),
  em/budget %, baris, kind, sugesti wrap UET.
- Aksi: Terapkan sugesti (per baris, modal konfirmasi via pattern `ModalScreen`
  existing), Apply All, Ekspor temuan ke CSV.
- `apply.py` backup sebelum menulis.

---

## 3. Lapis 2 — Judge criterion `fitting` (ke-5)

### 3.1 Data model (`judge/models.py`)
- `CRITERIA = (..., "fitting")`; masuk `ERROR_TYPES` otomatis.
- `CRITERION_LABELS["fitting"] = "Fitting — the line does not fit the game
  dialog window (overflow / too many lines / much longer than source)"`.

### 3.2 Kondisional seperti `context` (`prompts.py:360`)
`fitting` hanya muncul di layar kriteria Judge **dan hanya disuntik ke prompt**
saat user memilih engine profile di setup Judge (field baru "Engine profile"
di JudgeCriteriaScreen / re-judge; persisted bersama remembered settings per
model yang sudah ada). Tanpa profile → kriteria disembunyikan, persis pola
`context`-tanpa-kolom-context (AC-102 preseden).

### 3.3 Metrik disuntik per baris (`_rows_block` extend)
Baris baru setelah TRANSLATION, hanya saat fitting aktif:
```
[{row}]
  SOURCE      : ...
  TRANSLATION : ...
  FITTING     : source 2 lines/78% of budget · translation 5 lines/131% · overflow
```
Angka deterministik dari `fitting/`; AI tidak diminta menghitung.

### 3.4 Rule kriteria baru (`_CRITERION_RULES`)
Dua fokus verdict sesuai permintaan user:
- **Kepanjangan**: translation tidak muat budget window (data FITTING
  menunjukkan overflow/sinking) → WAJIB flag.
- **Keringkasan**: translation jauh lebih panjang dari source secara
  proporsional (ratio > 130%) meski masih muat → flag dengan saran peringkasan
  yang tetap natural.
- Saran perbaikan wajib format WHAT/WHY/FIX sesuai `comment_style` yang ada:
  FIX boleh berupa (a) rephrasing lebih ringkas yang tetap mengalir, atau
  (b) titik sisip `\n` eksplisit di frasa alami.
- Guardrail: jangan flag baris yang sumbernya memang panjang (ratio
  proporsional), jangan usulkan pemenggalan yang membelah control code, dan
  jangan usulkan word-for-word stiffening (harus tetap natural, rujuk
  naturalness rule yang sudah ada di prompts.py:334).

### 3.5 Fix All / Fix It (Lapis 3)
**Alur default tidak diubah** — baris ter-flag `fitting` masuk Fix All seperti
error type lain, diperbaiki AI dengan konteks/saran:
- `build_fix_prompt` menerima block `FITTING` per baris (suntikan metrik + rule:
  "shorten naturally OR insert [BR][NL] / \n at a natural phrase boundary;
  result must not exceed {max_lines} lines; never split a control code").
- **Re-measure wajib** di runner setelah fix: hasil fix dijalankan lewat
  `wrap_metrics`; kalau masih overflow/sinking → baris di-flag ulang dengan
  comment khusus ("fix tidak lolos re-measure"), tidak ditulis diam-diam.
- Karena output fix harus bisa berisi `\n` multi-baris, `decode_placeholders`
  sudah menangani `[BR][NL]` → `\n` (placeholders.py:16) — jalur ini aman
  tanpa perubahan.
- Iterasi fix di-batasi 2 putaran re-measure (mencegah loop) — baris yang
  tetap gagal setelah 2 putaran diberi marker "[FITTING_STILL_FAIL]" dan
  dilaporkan ke user untuk keputusan manual.

### 3.6 Grading & report
- `fitting` masuk severity grading normal (overflow=sinking → critical; ratio →
  minor/moderate oleh AI, anchor di rule).
- Report grid & summary: error type baru tampil otomatis (ERROR_TYPES sudah
  CRITERIA-driven), tidak perlu layar baru.

---

## 4. Perubahan file (ringkas)

| File | Perubahan |
|---|---|
| `translator/fitting/*` (baru) | profiles, measure, wrap, audit, apply, __init__ |
| `translator/parsing/prompts.py` | param `fitting_budget` + blok soft rule (translate & solo) |
| `translator/core/engine.py` | teruskan fitting_budget dari wizard config |
| `translator/tui/screens/wizard.py` | layar opsi Engine Profile (setelah kolom, sebelum pacing) |
| `translator/tui/screens/judge.py` | field Engine Profile + kriteria `fitting` di criteria screen & re-judge remembered settings |
| `translator/judge/models.py` | CRITERIA + labels + (fitting muncul di ERROR_TYPES otomatis) |
| `translator/judge/prompts.py` | `_CRITERION_RULES["fitting"]`, `_rows_block` FITTING line, `_fix_scope_rule`/fix prompt block, conditional gating |
| `translator/judge/runner.py` | re-measure post-fix, flag ulang, `[FITTING_STILL_FAIL]` |
| `translator/tui/app.py` | item menu "Text Fitting Audit" |
| `tui/screens/audit.py` (baru) | layar audit (grid + apply) |
| `tests/*` | unit per modul fitting + prompt snapshot + re-measure loop |

## 5. Testing & Verification
- Unit: strip_tokens (RPGM + kirikiri), visible_length per segmen, wrap greedy
  (word boundary, token tak terbelah), audit kinds & ratio, apply backup.
- Prompt snapshot: translate dengan/tanpa budget, judge dengan/ tanpa profile,
  fix prompt berisi FITTING block.
- Re-measure loop: fix yang menghasilkan teks masih-overflow → flag ulang;
  2 iterasi gagal → [FITTING_STILL_FAIL].
- UI manual: wizard dengan profile → prompt berisi budget; Judge criteria screen
  menampilkan fitting hanya saat profile dipilih; layar audit apply-all +
  backup file .bak ada.
- Full `pytest`.

## 6. Out of scope (V2 candidates)
- Custom profile user (lebar/font manual per project).
- Membaca font game asli untuk metrik presisi.
- Auto-detect format dump variatif (masalah #5) — V1 tetap format terkenal.
- Budget hard pada translate (memaksa LLM memotong makna).
- Word-wrap otomatis tanpa konfirmasi user saat translate selesai.
