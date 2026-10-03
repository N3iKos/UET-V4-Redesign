# UET-V4 Data Models & Technical Specifications

## 1. Vault Storage & Cryptographic Schema

Seluruh konfigurasi sensitif (endpoint provider, token/API key, model, dan parameter koneksi) dienkripsi di dalam berkas tunggal `vault.enc`.

### 1.1. Format Biner Berkas `vault.enc`
Struktur biner berkas diatur sebagai berikut:

```
┌─────────────────┬──────────┬──────────┬──────────┬────────────────────────┐
│  MAGIC HEADER   │   SALT   │  NONCE   │ HMAC TAG │   CIPHERTEXT (JSON)    │
│  "UET4VAULT\0"  │ 16 Bytes │ 16 Bytes │ 32 Bytes │        N Bytes         │
└─────────────────┴──────────┴──────────┴──────────┴────────────────────────┘
```

1. **Magic Header:** String biner pengenal format berkas (`config.VAULT_MAGIC`).
2. **Salt:** 16 byte acak kriptografis (`os.urandom(16)`) untuk derivasi kunci PBKDF2.
3. **Nonce:** 16 byte acak kriptografis (`os.urandom(16)`) untuk cipher stream counter.
4. **HMAC Tag:** 32 byte digest HMAC-SHA256 untuk memverifikasi integritas dan keaslian ciphertext (*encrypt-then-MAC*).
5. **Ciphertext:** Plaintext JSON yang dienkripsi menggunakan AES-CTR emulation via SHA-256 keystream XOR blocks.
6. **Derivasi Kunci:**
   `DerivedKey = PBKDF2_HMAC_SHA256(password, salt, iterations=310000, dklen=32)`.

### 1.2. Skema Plaintext JSON Vault (`VaultPayload`)
Setelah didekripsi, payload JSON merepresentasikan pohon topologi provider:

```json
{
  "version": 4,
  "providers": [
    {
      "id": "prov_a1b2c3d4",
      "display_name": "OpenAI Production",
      "connections": [
        {
          "id": "conn_e5f6g7h8",
          "display_name": "Primary Endpoint",
          "base_url": "https://api.openai.com/v1",
          "api_keys": ["sk-proj-...", "sk-proj-backup..."],
          "rate_limit": {
            "rpm": 500,
            "tpm": 200000,
            "tpd": 2000000,
            "rpd": 10000
          }
        }
      ],
      "models": [
        {
          "id": "gpt-4o",
          "display_name": "GPT-4o Omnimodel",
          "preset": {
            "rpm": 500,
            "tpm": 200000,
            "batch_size": 20,
            "workers": 5
          },
          "prompt_format": "plain",
          "priority_rank": 1,
          "reasoning": {
            "supported": false,
            "control": "none",
            "parameter_path": "",
            "on_value": true,
            "off_value": false,
            "default_enabled": false,
            "options": []
          }
        },
        {
          "id": "o3-mini",
          "display_name": "o3 Mini Reasoning",
          "preset": {
            "rpm": 100,
            "tpm": 100000,
            "batch_size": 10,
            "workers": 2
          },
          "prompt_format": "json",
          "priority_rank": 2,
          "reasoning": {
            "supported": true,
            "control": "effort",
            "parameter_path": "reasoning_effort",
            "on_value": "medium",
            "off_value": false,
            "default_enabled": true,
            "options": ["low", "medium", "high"]
          }
        }
      ]
    }
  ]
}
```

---

## 2. Core Job & Processing Contracts (`translator/core/models.py`)

### 2.1. `JobConfig` (Kontrak Konfigurasi Pekerjaan)
Objek dataclass lengkap yang mendefinisikan seluruh parameter pekerjaan penerjemahan:

| Field | Tipe | Keterangan |
| :--- | :--- | :--- |
| `provider_id` | `str` | ID provider aktif. |
| `connection_id` | `str \| None` | ID koneksi yang dipin (opsional). |
| `model_id` | `str` | ID model aktif untuk penerjemahan. |
| `repair_model_id`| `str \| None` | ID model khusus fase perbaikan (opsional). |
| `combo_id` | `str \| None` | ID Model Group / Combo jika menggunakan multi-provider pooling. |
| `src_path` | `str` | Path berkas sumber aktif (bisa berupa file deduplikasi sementara). |
| `out_path` | `str` | Path berkas tujuan aktif (tempat penulisan hasil). |
| `full_src_path` | `str \| None` | Path berkas sumber penuh asli (sebelum deduplikasi). |
| `full_out_path` | `str \| None` | Path berkas tujuan penuh asli. |
| `map_path` | `str \| None` | Path berkas peta pemetaan deduplikasi (`map_*.json`). |
| `src_col` | `int` | Indeks kolom sumber (1-based index). |
| `dst_col` | `int` | Indeks kolom tujuan penulisan (1-based index). |
| `ctx_col` | `int \| None` | Indeks kolom konteks opsional (1-based index). |
| `repair_col` | `int \| None` | Indeks kolom penulisan hasil perbaikan (1-based index). |
| `start_row` | `int` | Baris awal pemrosesan (default: 2, melewati header). |
| `num_rows` | `int` | Jumlah baris yang diproses (`<= 0` artinya hingga akhir berkas). |
| `batch_size` | `int` | Jumlah baris per request ke LLM (default: 20). |
| `workers` | `int` | Jumlah thread pekerja paralel (default: 3). |
| `strategy` | `Literal["sequential", "parallel", "wave"]` | Strategi konkurensi penjadwal. |
| `write_mode` | `Literal["safe", "buffered"]` | Mode penulisan writer disk. |
| `retry_mode` | `Literal["cooldown", "infinite"]` | Kebijakan toleransi pengulangan saat error. |
| `anti_desync` | `Literal["auto", "strict", "off"]` | Mode validasi keselarasan baris. |
| `prompt_format`| `Literal["plain", "json"]` | Format penomoran prompt (`[N]` vs JSON Schema). |
| `src_lang` | `str` | Bahasa sumber (misal: "Japanese", "English"). |
| `tgt_lang` | `str` | Bahasa target (misal: "Indonesian"). |
| `style_prompt` | `str` | Instruksi sistem / tone of voice terjemahan. |
| `reasoning_enabled` | `bool` | Status switch penalaran (thinking). |
| `reasoning_effort` | `str` | Tingkat effort reasoning (`low`, `medium`, `high`). |
| `skip_filled` | `bool` | Lewati baris yang sudah terisi dan valid. |

### 2.2. `BatchTask` & `BatchResult`
- **`BatchTask`:**
  - `batch_idx: int` — Nomor urut batch (0-based).
  - `rows: list[tuple[int, str]]` — Daftar tuple `(nomor_baris_spreadsheet, teks_sumber)`.
  - `extra: dict` — Data konteks atau baris perbaikan opsional.
  - `generation: int` — Nomor generasi task (dinaikkan jika dialihkan oleh watchdog).
- **`BatchResult`:**
  - `rows: dict[int, str]` — Pemetaan nomor baris spreadsheet ke hasil terjemahan yang telah divalidasi.
  - `failed_rows: list[int]` — Daftar nomor baris yang gagal diparse atau ditolak oleh anti-desync.
  - `tokens: int` — Total token yang dikonsumsi untuk batch ini.
  - `latency_ms: float` — Waktu round-trip request dalam milidetik.

### 2.3. `ProviderError`
Exception terstruktur untuk mengklasifikasikan kegagalan panggilan LLM:
- `status_code: int` — HTTP status code (misal: 429, 500, 503, 401).
- `message: str` — Pesan teks asli dari provider.
- `kind: Literal["rate_limit", "quota", "server", "auth", "invalid", "unknown"]` — Kategori error yang menentukan aksi retry executor.
- `is_timeout: bool` — Flag penanda apakah error terjadi akibat timeout koneksi/socket hang.

---

## 3. Scheduling & Pooling Data Structures

### 3.1. `KeyPool` State Machine (`translator/scheduling/key_pool.py`)
Setiap API key di dalam koneksi dikelola melalui state machine terisolasi:

```
                ┌──────────────────────────────────────┐
                │                                      │
                ▼                                      │
       ┌────────────────┐   Error 429 / Cooldown       │
       │     ACTIVE     ├──────────────────────────┐   │
       └───────┬────────┘                          │   │
               │                                   ▼   │
               │ Confirmed Quota (402/429 Quota) ┌──────────────┐
               │                                 │   COOLDOWN   │
               ▼                                 └──────┬───────┘
       ┌────────────────┐                               │ Waktu Cooldown Berakhir
       │   EXHAUSTED    │                               │
       │ (Hingga Besok) │◄──────────────────────────────┘
       └────────────────┘
```

- **Metrik Pemilihan Key:**
  Key aktif dipilih menggunakan sorting ganda: `min(tpd_used, avg_latency_ms)`.
- **Pelacakan Kuota:**
  - `tpd_used`: Total token yang telah dikonsumsi hari ini.
  - `requests_remaining`: Sisa kuota request dari header respons HTTP (misal: `x-ratelimit-remaining-requests`).

### 3.2. `ConnectionPool` & `ComboPool` Leases
- **`ConnectionLease`:**
  Objek sewa sementara yang dipegang oleh thread pekerja saat menjalankan request:
  - `slot_id: str` — Identitas slot koneksi unik.
  - `base_url: str` — Endpoint target HTTP.
  - `model_id: str` — Model yang ditugaskan ke slot ini.
  - `api_key: str` — Key yang dipinjam dari `KeyPool`.
  - `reasoning_cfg: ReasoningConfig` — Parameter reasoning yang telah diselesaikan untuk slot ini.

---

## 4. The Judge Data Structures (`translator/judge/models.py`)

### 4.1. `RowIssue` (Satu Temuan Masalah Baris)
Representasi data untuk satu baris yang ditandai memiliki cacat terjemahan:
- `row_idx: int` — Nomor baris riil spreadsheet.
- `source_text: str` — Teks sumber asli.
- `translated_text: str` — Teks terjemahan yang dievaluasi.
- `criteria_failed: list[str]` — Kriteria yang dilanggar (`mistranslation`, `context`, `placeholder`, `style`, `fitting`).
- `severity: Literal["critical", "moderate", "minor"]` — Tingkat keparahan cacat.
- `comment: str` — Penjelasan rinci arbiter mengenai mengapa baris tersebut bermasalah.
- `suggested_fix: str \| None` — Rekomendasi perbaikan teks langsung dari model arbiter.

### 4.2. `JudgeSummary` & `JudgeReport`
- **`JudgeSummary`:**
  - `total_evaluated: int` — Jumlah total baris yang ditinjau.
  - `passed: int` — Jumlah baris yang bersih / lolos evaluasi.
  - `flagged: int` — Jumlah baris yang bermasalah.
  - `score: float` — Skor kualitas keseluruhan (0.0 hingga 100.0%).
  - `by_severity: dict[str, int]` — Distribusi jumlah masalah berdasarkan tingkat keparahan.
  - `by_criterion: dict[str, int]` — Distribusi jumlah masalah berdasarkan kriteria linguistik.
- **`StagedFix` (Penampung Perbaikan Sementara di Memori):**
  - `fixes: dict[int, str]` — Pemetaan nomor baris ke teks perbaikan yang siap ditulis.
  - `approved_rows: set[int]` — Himpunan nomor baris yang telah disetujui pengguna untuk disimpan ke disk.

---

## 5. Storage & Cache File Specifications

### 5.1. Skema JSON Pemetaan Deduplikasi (`.uet_cache/map_<hash>.json`)
File ini menjembatani hubungan antara berkas unik terjemahan dengan berkas penuh asli:

```json
{
  "version": 1,
  "fingerprint": "a9f8e4b3c2d1e0f9...",
  "source_path": "D:/Projects/Dialogues.xlsx",
  "unique_path": "D:/Projects/.uet_cache/unique_a9f8e4.xlsx",
  "source_column": 2,
  "total_rows": 12500,
  "unique_rows": 3200,
  "mapping": {
    "2": [2, 15, 84, 120, 450],
    "3": [3, 16],
    "4": [4]
  },
  "tags": {
    "2": "\\c[1]",
    "15": "\\c[2]",
    "84": "\\c[1]",
    "120": "",
    "450": "\\c[3]"
  }
}
```

### 5.2. Skema JSON Sidecar Cache Gagal (`.uet_cache/failed_<stem>_<col>.json`)
Menyimpan daftar nomor baris yang belum berhasil diselesaikan:

```json
{
  "version": 1,
  "output_file": "Dialogues_ID.xlsx",
  "dest_column": 3,
  "failed_rows": [104, 256, 1089, 4520],
  "last_updated": "2026-09-23T14:45:00Z"
}
```

### 5.3. Skema SQLite Translation Memory (`tm.db`)
Database lokal untuk cache terjemahan persis:

```sql
CREATE TABLE IF NOT EXISTS tm (
    src_lang   TEXT NOT NULL,
    tgt_lang   TEXT NOT NULL,
    source     TEXT NOT NULL,
    target     TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (src_lang, tgt_lang, source)
);

CREATE INDEX IF NOT EXISTS idx_tm_lookup 
ON tm (src_lang, tgt_lang, source);
```

### 5.4. Skema Konfigurasi Preferensi Pengguna (`app_prefs.json`)
Menyimpan konfigurasi default global antarmuka dan pemrosesan:

```json
{
  "processing_mode": "parallel",
  "default_workers": 4,
  "default_batch_size": 20,
  "anti_desync_mode": "auto",
  "write_mode": "safe",
  "checkpoint_rows": 50,
  "request_timeout_seconds": 60,
  "auto_sweep_enabled": true,
  "judge_grading_mode": "severity",
  "judge_max_rows_per_request": 25,
  "default_fix_batch_rows": 20,
  "rejudge_anti_oscillation_guard": true
}
```
