# UET-V4 Workflow & Processing Engine Specification

## 1. End-to-End Translation Lifecycle Pipeline

Siklus hidup pemrosesan terjemahan di UET-V4 dirancang bertingkat (*multi-stage pipeline*) dengan batasan isolasi data yang ketat. Diagram alur berikut menggambarkan jalur data dari file masukan mentah hingga file luaran final:

```
[ Input File (XLSX / CSV) ]
             │
             ▼
[ 1. Pre-Flight Inspection ] ──────────► (Deteksi Header, Delimiter CSV, & Cek Kolom)
             │
             ▼
[ 2. Deduplication Engine ] ───────────► (Opsional: Buat Unique File jika duplikat tinggi)
             │
             ▼
[ 3. Row Classification ] ────────────► (Filter Baris Kosong & Skip-Filled Resume)
             │
             ▼
[ 4. Local TM Lookup ] ────────────────► (Kecocokan Persis langsung ditulis dari SQLite tm.db)
             │ (Baris belum terjemah)
             ▼
[ 5. Batch Slicing ] ──────────────────► (Membagi baris ke dalam BatchTask [N..M])
             │
             ▼
[ 6. Strategy Scheduler ] ─────────────► (Sequential | Parallel + Watchdog | Wave)
             │
             ▼
[ 7. RetryExecutor & Pool Lease ] ─────► (Key Rotation, Cooldown, Failover Model)
             │
             ▼
[ 8. ProviderClient HTTP Call ] ───────► (OpenAI-compatible Chat Completion / JSON Mode)
             │
             ▼
[ 9. Parse Recovery Pipeline ] ────────► (Strict [N] ──► Loose N. ──► Fuzzy Remap)
             │
             ▼
[ 10. Anti-Desync Validation ] ────────► (Layer 1 Echo ──► Layer 2 Shift ──► Layer 2b Merge)
             │
             ├──► [ Lulus ] ───────────► [ 12. Async ExcelWriter Buffer ]
             │                                        │
             └──► [ Baris Hilang/Gagal ]              ▼
                         │              [ 13. Atomic Checkpoint (.tmp -> replace) ]
                         ▼                            │
             [ 11. Recovery Ladder ]                  ▼
             (Mini-Batch ──► Solo-Task) ──► [ 14. Failed Cache Sidecar JSON ]
                         │
                         ▼
             [ 15. Auto-Sweep Passes ] ───► (Hingga 2 putaran coba ulang baris bermasalah)
                         │
                         ▼
             [ 16. Dedup Sync Broadcast ] ─► (Menyalin hasil unik ke seluruh baris duplikat)
                         │
                         ▼
             [ 17. Final Commit & Summary ]
```

---

## 2. Detailed Pipeline Execution Stages

### Tahap 1: Pre-Flight Inspection & File Preparation
1. **Pemeriksaan Dimensi & Lock:** `ExcelReader._dimensions` membaca ukuran dan waktu modifikasi file (`mtime`). File sumber dibuka secara read-only.
2. **Adapter CSV:** Jika file berupa CSV, modul `csv_io` mendeteksi encoding (UTF-8, UTF-8-BOM, CP1252) dan dialek (delimiter koma/titik-koma/tab, quote char) menggunakan sniffer khusus, lalu membungkusnya dalam objek `_CsvWorkbook` yang kompatibel dengan antarmuka openpyxl.
3. **Persiapan Berkas Luaran (`prepare_output`):**
   - Jika berkas tujuan belum ada: Berkas sumber disalin utuh ke berkas tujuan (`shutil.copy2`), memastikan seluruh format sel, formula, warna, dan struktur sheet tetap terjaga 100%.
   - Jika berkas tujuan sudah ada: Sistem menandai sesi sebagai *Resumed Run* dan mempertahankan isi berkas tujuan yang sudah ada.

### Tahap 2: Deduplication Engine (`prepare_unique_file`)
1. Jika opsi deduplikasi aktif, sistem menghitung hash fingerprint dari kolom sumber.
2. Dialog atau teks yang berulang (misalnya pilihan menu game: "Ya", "Tidak", atau nama item yang muncul ratusan kali) dikelompokkan berdasarkan teks sumber yang dinormalisasi.
3. Sistem membuat berkas sementara `.uet_cache/unique_<hash>.xlsx` yang hanya berisi satu baris representatif untuk setiap teks unik.
4. Prefix tag game (seperti kode warna RPG Maker `\c[2]Hello`) dipisahkan secara cerdas sehingga variasi tag pada teks yang sama tetap dapat dipertahankan.
5. Pemetaan baris disimpan dalam format JSON atomik `.uet_cache/map_<hash>.json`. Jika penghematan baris signifikan, engine menerjemahkan berkas unik ini terlebih dahulu, menghemat token dan kuota hingga 40-70%.

### Tahap 3: Row Classification & Filtering
Engine membaca kolom sumber dan kolom tujuan secara streaming:
- **Baris Kosong:** Baris tanpa teks sumber diabaikan dan tidak dimasukkan ke dalam total pekerjaan.
- **Skip-Filled:** Jika mode *skip-filled* aktif, sel tujuan yang sudah terisi dan tidak berstatus `FAIL_MARKER` maupun tercatat di `failed_cache.json` akan ditandai sebagai *Skipped*.
- **Baris Kandidat:** Hanya baris yang belum memiliki terjemahan valid yang dimasukkan ke dalam daftar antrean aktif.

### Tahap 4: Translation Memory (TM) Instant Lookup
Sebelum menyusun panggilan jaringan:
1. Engine mengambil seluruh string sumber dari baris kandidat dan membaginya ke dalam batch query 500 entri ke SQLite `tm.db`.
2. Jika ditemukan pasangan `(src_lang, tgt_lang, normalized_source)` yang persis sama, hasil terjemahan dari database langsung diambil.
3. Hasil TM ditulis langsung ke writer tanpa perlu membuat request ke LLM, menghasilkan penghematan biaya dan peningkatan kecepatan instan.

### Tahap 5: Batch Task Construction
Baris-baris yang tersisa dikelompokkan ke dalam objek `BatchTask`:
- Setiap `BatchTask` memiliki nomor batch unik, daftar pasangan `(row_number, source_text)`, nomor `generation` (mulai dari 0), serta metadata baris konteks (jika kolom konteks diaktifkan).
- Ukuran batch diatur oleh parameter `batch_size` (misal 10 hingga 50 baris per request).

---

## 3. Concurrency & Scheduling Strategies

UET-V4 menyediakan tiga strategi penjadwalan eksekusi di `translator/scheduling/scheduler.py`:

| Fitur | Sequential | Parallel | Wave |
| :--- | :--- | :--- | :--- |
| **Konsep Dasar** | Satu request dalam satu waktu. | *N* thread pekerja mengonsumsi antrean batch bersama. | *N* pekerja berjalan serentak dalam satu gelombang lockstep. |
| **Kelebihan** | Resiko rate-limit sangat minim; urutan baris teratur. | Throughput maksimal; utilisasi bandwidth dan token tinggi. | Pacing rate limit sangat presisi; jeda gelombang teratur. |
| **Kelemahan** | Lambat untuk berkas besar. | Memerlukan penanganan watchdog untuk thread stuck. | Terikat pada pekerja terlambat dalam satu gelombang. |
| **Penanganan Stuck** | Timeout request langsung memicu retry. | **Watchdog Thread** mendeteksi thread hang, menaikkan `generation`, dan mengantrekan ulang task. | Task yang stuck dipindahkan ke awal gelombang berikutnya, slot diistirahatkan 1 siklus. |
| **Kasus Penggunaan** | Model tier gratis (Free Tier) dengan kuota RPM/TPM rendah. | Model berbayar tier tinggi dengan concurrency tak terbatas. | Provider dengan burst rate limit yang membutuhkan waktu istirahat teratur (*cooling off*). |

### Watchdog & Generational Reassignment (Parallel Mode)
Pada mode paralel, jika suatu request jaringan hang melebihi `stuck_limit` (dihitung dari `request_timeout * attempts + 90s`):
1. Thread Watchdog mendeteksi batch yang mengalami keterlambatan.
2. Nomor `generation` pada `BatchTask` dinaikkan (`generation += 1`).
3. Task dimasukkan kembali ke antrean pekerjaan (`task_queue`) untuk diambil oleh pekerja lain yang sedang idle.
4. Jika pekerja lama yang hang akhirnya merespons di kemudian waktu, nilainya akan ditolak (*stale generation discard*) sehingga tidak menimpa data yang sedang atau sudah diproses oleh pekerja baru.
5. Jika sebuah task telah dialihkan sebanyak `MAX_TASK_REASSIGNS` (3 kali) tanpa hasil, task tersebut dinyatakan gagal.

---

## 4. Prompting & Token Protection Pipeline

### 4.1. Masking & Placeholder Protection
Sebelum teks dikirimkan ke model:
- Karakter kontrol game seperti `\n`, `\r`, `\t`, `\"`, `\@`, serta kode RPG Maker seperti `\c[2]`, `\v[1]`, `\n[3]`, `\g` dienkode menjadi token inert:
  ```text
  \c[2]Halo Petualang!\nSiap berangkat?
  ──► [BR][C2]Halo Petualang![BR][LF]Siap berangkat?
  ```
- Ini mencegah LLM menginterpretasikan karakter kontrol sebagai sintaks prompt atau menghapusnya karena dianggap sampah.
- Token `[BR]` dikembalikan ke format aslinya setelah proses parsing terjemahan selesai.

### 4.2. Prompt Construction
- **Mode Plain (Numbered List):**
  Instruksi sistem menetapkan peran sebagai penerjemah profesional, aturan pelestarian nama karakter, gaya bahasa, dan kontrak penomoran baris yang ketat:
  ```text
  [1] First line text
  [2] Second line text
  [3] Third line text
  ```
- **Mode JSON Schema:**
  Untuk model yang mendukung response format JSON, UET-V4 menyuntikkan skema objek ketat di mana setiap nomor baris menjadi key wajib:
  ```json
  {
    "1": "Terjemahan baris 1",
    "2": "Terjemahan baris 2"
  }
  ```

---

## 5. Resiliency, Pool Leasing & Failover Mechanism

Eksekusi request dilakukan melalui `RetryExecutor` yang bekerja sama dengan hirarki Pool:

```
[ RetryExecutor ]
        │
        ├──► 1. Minta Lease dari [ ConnectionPool / ComboPool ]
        │           │
        │           └──► Evaluasi Strategi:
        │                   - Round Robin: Rotasi setelah N baris (wave size)
        │                   - Fallback: Pakai koneksi utama sampai error
        │
        ├──► 2. Minta API Key dari [ KeyPool ]
        │           │
        │           └──► Algoritma Least-Used:
        │                   Pilih key dengan (tpd_used terendah, latency terendah)
        │                   Lewati key yang sedang Cooldown atau Kuota Habis
        │
        ├──► 3. Panggil [ ProviderClient.generate() ]
        │           │
        │           ├──► Sukses: Kembalikan hasil, update latency & token
        │           │
        │           └──► Gagal: Klasifikasikan error (ProviderError)
        │                   - Rate Limit (429): Set key cooldown, sleep backoff
        │                   - Quota Exhausted: Tandai key daily-exhausted permanen
        │                   - Server Error (5xx): Rotasi koneksi, delay 503
        │                   - Timeout: Langsung rotasi koneksi tanpa extra delay
        │
        └──► 4. Priority Failover (Jika Percobaan Mencapai Threshold)
                    │
                    └──► Beralih ke model/koneksi berikutnya berdasarkan rank (1, 2, 3...)
```

---

## 6. The Recovery Ladder: Menghadapi Hasil Parsing Parsial

Jika output LLM yang diparse oleh `ParseRecoveryPipeline` tidak menghasilkan seluruh baris dalam batch (misal dari 20 baris, hanya 16 baris yang berhasil diparse dengan aman):

```
Batch Asli (20 baris)
        │
        ▼ (Hanya 16 baris lulus parsing & validasi alignment)
[ Hasil Parsial: 16 baris tersimpan ]
        │
        ▼ (Sisa 4 baris yang hilang)
[ Tangga 1: Mini-Batch Task ]
        │  Membangun batch kecil baru khusus 4 baris tersebut
        │
        ├──► Sukses: Gabungkan ke hasil batch utama.
        │
        └──► Sebagian Masih Gagal (misal sisa 2 baris):
                    │
                    ▼
        [ Tangga 2: Solo-Task Mode ]
           Menerbitkan 1 request terpisah untuk setiap baris yang tersisa.
           Setiap baris diproses secara individual dengan prompt tunggal.
           *Keunggulan Solo-Task:* Bebas dari resiko pergeseran baris (shift-proof)
           karena hanya ada 1 baris sumber dan 1 baris target.
```

---

## 7. Storage, Locking, and Checkpoint Architecture

Penulisan ke disk ditangani secara terisolasi oleh `ExcelWriter` melalui thread `_AsyncWriteThread`:
1. **Thread Pemisah:** Thread pekerja terjemahan tidak pernah menunggu disk IO. Hasil terjemahan cukup di-*flush* ke antrean memori writer.
2. **Batch Coalescing:** Writer menggabungkan pembaruan sel ke dalam peta memori `{column: {row: text}}`.
3. **Pemicu Simpan (Flush Triggers):**
   - Jumlah baris terakumulasi mencapai ambang batas `CHECKPOINT_ROWS` (default: 50 baris).
   - Thread idle selama lebih dari 0.5 detik.
   - Panggilan eksplisit barrier `sync()` sebelum membaca ulang file (misalnya saat sweep).
   - Penutupan pekerjaan (`commit()`).
4. **Mekanisme Tulis Atomik:**
   - Membaca file target aktif ke memori.
   - Menulis perubahan sel ke file temporer dengan PID proses: `output.xlsx.<pid>.tmp`.
   - Melakukan `os.replace` atomik untuk menimpa file target.
5. **Penanganan File Terkunci (Excel Open / Lock Recovery):**
   - Jika `os.replace` gagal karena file sedang dibuka oleh pengguna di Microsoft Excel (PermissionError / Lock error):
   - Writer mencoba kembali hingga beberapa kali dengan backoff eksponensial (50ms hingga 2s).
   - Jika tetap terkunci, writer mengaktifkan **Sibling Fallback**:
     Membuat file baru bernomor urut di direktori yang sama (misal `output_1.xlsx`), menyalin seluruh progres yang sudah dicapai ke file baru tersebut, dan mengalihkan seluruh penulisan berikutnya ke file saudara ini.
   - Informasi file luaran aktif ini diteruskan ke UI sehingga pengguna mengetahui lokasi file terjemahan yang sebenarnya.

---

## 8. Deduplication Broadcast & Post-Processing

1. **Throttled Background Sync:** Selama proses penerjemahan berjalan, jika deduplikasi aktif, sistem menjalankan sinkronisasi latar belakang setiap 30 detik untuk memperbarui file penuh (`full_out_path`) tanpa menghambat kecepatan pekerja.
2. **Final Broadcast:** Setelah seluruh batch unik selesai:
   - Sistem membaca berkas unik terjemahan.
   - Mengiterasi seluruh baris duplikat di file asli berdasarkan `map_<hash>.json`.
   - Mengembalikan prefix tag asli masing-masing baris.
   - Menyimpan berkas penuh final secara atomik.
3. **Cleanup:** File sementara `.uet_cache/unique_*` hanya dihapus jika proses sinkronisasi final sukses 100% tanpa baris gagal yang tersisa.
4. **Auto-Sweep & Chained Repair:**
   - Jika masih ada baris yang berstatus gagal setelah batch utama selesai, engine secara otomatis menjalankan fase *Auto-Sweep* (hingga 2 putaran) untuk mencoba kembali baris-baris gagal tersebut.
   - Jika opsi perbaikan otomatis aktif (`repair_col`), engine langsung beralih memproses perbaikan kualitas ke kolom perbaikan menggunakan model evaluator yang dipilih.
