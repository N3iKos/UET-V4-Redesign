# UET-V4 Features & Capabilities Catalog

## 1. Feature Matrix Overview

Universal Excel Translator v4 (UET-V4) mengintegrasikan kapabilitas pemrosesan batch bervolume tinggi, kecerdasan buatan semantik, dan keandalan sistem tingkat enterprise. Tabel berikut merangkum seluruh kemampuan inti sistem:

| Kategori | Fitur Utama | Deskripsi Singkat |
| :--- | :--- | :--- |
| **Core Translation** | High-Throughput Batch Engine | Pemrosesan XLSX dan CSV berskala besar dengan batching adaptif berbasis RPM/TPM. |
| | Concurrency Modes | Tiga mesin eksekusi: *Sequential*, *Parallel* (dengan Watchdog), dan *Wave* (Lockstep). |
| | Multi-File Processing | Menerjemahkan hingga 4 file secara simultan dengan alokasi pekerja cerdas (*smart worker distribution*). |
| **Resiliency & LB** | Connection Pooling | Load balancing koneksi dengan strategi *Round-Robin* (berbasis baris) dan *Fallback*. |
| | Multi-Key Pool | Rotasi API key berbasis *least-used*, pelacakan latensi, cooldown otomatis, dan deteksi kuota habis. |
| | Model Groups (Combos) | Pengelompokan lintas-provider untuk failover otomatis berjenjang (*Priority Failover*). |
| **Data Safety** | Anti-Desync Protection | Validasi keselarasan 3-lapis untuk mencegah pergeseran baris, penggabungan baris, dan halusinasi. |
| | Recovery Ladder | Pemulihan otomatis bertingkat: *Mini-Batch* untuk baris hilang dan *Solo-Task* (1:1 shift-proof). |
| | Placeholder Masking | Perlindungan karakter kontrol game (`\n`, `\@`) dan kode RPG Maker (`\c[2]`) via token `[BR]`. |
| **Cost & Efficiency** | Smart Deduplication | Ekstraksi baris unik otomatis dengan pelestarian tag dialog game, memangkas token hingga 70%. |
| | SQLite Translation Memory | Cache terjemahan persis (*Exact Match*) lokal di `tm.db` untuk eliminasi pemanggilan API berulang. |
| **Storage & I/O** | Zero Data Loss Async Writer | Penulisan disk asinkron terisolasi dengan checkpoint berkala dan penggantian atomik (`os.replace`). |
| | Excel Lock Recovery | Deteksi konflik file terbuka (Excel lock) dengan retry eksponensial dan forking otomatis ke file baru. |
| | Sidecar Crash Cache | Penyimpanan nomor baris gagal di `.uet_cache/` untuk *resume* dan *retranslate-failed* instan. |
| **Quality Control** | The Judge Subsystem | Evaluasi mutu terjemahan mandiri dengan 5 kriteria linguistik dan grading keparahan 3-tingkat. |
| | Autonomous Repair & Fix | Fitur *Fix All* dan *Single Row Fix* di memori dengan persetujuan manusia sebelum simpan. |
| | Anti-Oscillation Re-Judge | Evaluasi ulang otomatis dengan proteksi siklus kritik berulang (*anti-flip ping-pong guard*). |
| **Playground** | Comparative Model Benchmark | Uji coba berdampingan hingga 3 model (*blind testing*) lengkap dengan metrik TTFT dan throughput. |
| | Blinded AI Judging | Evaluasi komparatif independen oleh LLM arbiter untuk menentukan model pemenang. |
| | Streaming Chat | Uji coba interaktif dengan inspeksi jejak pemikiran (*collapsible reasoning trace*). |
| **Security** | Encrypted Vault | Enkripsi PBKDF2-HMAC-SHA256 (310.000 iterasi) + AES-CTR keystream + HMAC-SHA256 tag. |
| | Dynamic Reasoning Engine | Tidak ada nama provider/model hardcoded; injeksi parameter reasoning diatur secara deklaratif. |

---

## 2. Core Translation & High-Throughput Engine

### 2.1. Adaptif Batching & Rate-Limit Tuning
- Engine tidak membatasi ukuran batch pada nilai kaku. Sistem menghitung ukuran batch optimal (`batch_size`) dan jumlah thread pekerja (`workers`) secara dinamis berdasarkan preset batas laju (*Rate Limit Preset*):
  - **Requests Per Minute (RPM)**
  - **Tokens Per Minute (TPM)**
  - **Estimated Tokens Per Row (Source + Completion)**
- Formula penentuan kapasitas memastikan request tidak melampaui batas burst provider, meminimalkan kemunculan HTTP 429 (Too Many Requests).

### 2.2. Concurrency Engines
1. **Sequential Engine:**
   - Menjamin urutan request 100% linear dengan jeda waktu (`batch_delay`).
   - Ideal untuk tier gratisan (seperti Google Gemini Free Tier atau Groq Free Tier) yang memiliki batas RPM ketat.
2. **Parallel Engine dengan Generational Watchdog:**
   - Mempekerjakan *N* thread pekerja paralel yang mengambil task dari antrean bersama.
   - Dilengkapi **Watchdog Thread** independen yang mengawasi umur task in-flight. Jika pekerja mengalami kebuntuan jaringan (*socket hang*), watchdog menaikkan nomor `generation` task dan mengalihkan task tersebut ke antrean pekerja lain.
   - Hasil yang tiba terlambat dari thread lama otomatis dibuang (*discarded*).
3. **Wave Engine (Lockstep Pacing):**
   - Menjalankan batch dalam satu gelombang serentak.
   - Gelombang berikutnya hanya dipicu setelah seluruh pekerja dalam gelombang saat ini selesai, diikuti jeda istirahat gelombang (`wave_delay`).
   - Sangat efektif untuk provider yang memberlakukan kuota TPM berulang per menit.

### 2.3. Multi-File Processing (Hingga 4 File Simultan)
- Pengguna dapat memproses hingga 4 file spreadsheet secara bersamaan.
- **Pewarisan Leader-Follower:** Pengaturan pada File 0 (Leader) otomatis diterapkan pada File 1-3 (Follower), kecuali kolom atau parameter tertentu diubah secara manual.
- **Smart Worker Allocation:** Mengelompokkan draft berdasarkan pasangan `(provider_id, model_id)`. Jika file menggunakan model yang sama, kapasitas pekerja dibagi secara proporsional guna mencegah persaingan kuota internal.

---

## 3. High-Availability Failover & Resilient Pooling

### 3.1. Connection Pooling & Load Balancing
- Pengguna dapat mendaftarkan beberapa endpoint/koneksi untuk satu provider yang sama.
- **Strategi Round-Robin Berbasis Baris:** Berganti koneksi secara teratur setelah sejumlah baris tertentu selesai diproses. Perhitungan berbasis jumlah baris riil menjamin distribusi beban yang merata meskipun thread pekerja berjalan paralel.
- **Strategi Fallback:** Terus menggunakan koneksi utama hingga terjadi error atau cooldown, lalu beralih ke koneksi cadangan. Menghindari *flapping* (bolak-balik koneksi) dengan masa tunggu minimum `CONNECTION_ERROR_COOLDOWN`.

### 3.2. Multi-API Key Pooling
- Setiap koneksi dapat menampung banyak API key.
- **Algoritma Least-Used & Latency-Aware:** Sistem selalu memilih key yang memiliki penggunaan token harian (`tpd_used`) paling rendah dan rata-rata latensi respons paling cepat.
- **Deteksi Kuota Harian Habis (*Confirmed Quota Exhaustion*):** Jika provider mengembalikan respons kuota habis permanen (misal: "insufficient_quota"), key tersebut ditandai berstatus `exhausted` dan diparkir hingga hari berikutnya, mencegah percobaan sia-sia yang membuang waktu.
- **Inline vs Parking Cooldown:** Error rate-limit singkat (< 10 detik) diselesaikan dengan tidur singkat di tempat (*inline sleep*). Delay yang lebih panjang akan memarkir key dan merotasi request ke key lain yang tersedia.

### 3.3. Model Groups (Combos) & Priority Failover
- Pengguna dapat menggabungkan model dari provider berbeda ke dalam satu grup (*Model Group*), misalnya:
  `Primary: Claude 3.5 Sonnet -> Backup 1: GPT-4o -> Backup 2: DeepSeek-V3`.
- Setiap model dalam combo mempertahankan konfigurasi koneksi dan API key masing-masing.
- Saat request gagal mencapai batas percobaan (*retry attempts*), sistem secara otomatis mengalihkan pekerjaan ke model cadangan berikutnya berdasarkan ranking prioritas tanpa menghentikan pekerjaan penerjemahan.

---

## 4. Anti-Desynchronization & Alignment Verification

Masalah terbesar pada penerjemahan spreadsheet dengan LLM adalah **pergeseran baris (*row shifting*)**, di mana LLM melewatkan satu baris sehingga terjemahan baris ke-10 bergeser ke baris ke-9. UET-V4 memecahkan masalah ini dengan sistem pertahanan berlapis:

```
Hasil Mentah LLM
       │
       ▼
[ Layer 1: Echo Detection ]
  Apakah terjemahan baris [N] menjiplak persis teks sumber baris [N-1] atau [N+1]?
  ──► Jika YA: TOLAK SELURUH BATCH!
       │
       ▼
[ Layer 2: Statistical Shift Correlation ]
  Analisis metrik struktural (panjang karakter, tanda ?, !, tanda petik)
  terhadap baris aktif vs baris berikutnya di sepanjang minimal 3 baris tetangga.
  ──► Jika skor korelasi lebih condong ke baris berikutnya: TOLAK BATCH!
       │
       ▼
[ Layer 2b: Row Merge Detection ]
  Apakah model menggabungkan terjemahan baris A dan B menjadi satu di baris A,
  lalu mengosongkan baris B?
  ──► Jika YA: TOLAK BATCH!
       │
       ▼
[ Layer 3: Game Token & Placeholder Integrity ]
  Verifikasi rasio panjang karakter yang wajar dan keberadaan token [BR] utuh.
       │
       ▼
[ Lulus Semua Layer: Masuk ke Hasil Valid ]
```

### The Recovery Ladder (Tangga Pemulihan Cerdas)
Jika sebuah batch ditolak atau menyisakan baris yang tidak lengkap:
1. **Mini-Batch Task:** Sistem secara otomatis merangkai batch baru yang hanya berisi baris-baris yang hilang dari batch tersebut.
2. **Solo-Task Mode (Shift-Proof):** Jika mini-batch masih gagal, sistem memecah baris yang tersisa menjadi request individual 1 baris per request. Mode solo ini kebal 100% terhadap pergeseran baris karena hubungan input dan output selalu 1:1.

---

## 5. Cost Optimization & Smart Deduplication

### 5.1. Smart File Deduplication (`dedup_file.py`)
- Pada proyek lokalisasi game dan software, seringkali terdapat ribuan baris teks yang berulang sama persis (misal pilihan "Ya", "Tidak", "Serang", "Batal", atau dialog NPC umum).
- Fitur deduplikasi UET-V4:
  1. Memisahkan prefix tag game (misal `\c[1]Ya` dan `\c[2]Ya` diidentifikasi memiliki teks inti yang sama: "Ya").
  2. Mengekstrak hanya satu baris representatif ke file sementara `.uet_cache/unique_*.xlsx`.
  3. Mengirimkan hanya baris unik ke LLM, menghemat biaya token dan waktu hingga 40-70%.
  4. Menyebarkan hasil terjemahan kembali ke seluruh baris duplikat di file asli secara asinkron (background sync setiap 30 detik dan final synchronous broadcast), dengan mengembalikan prefix tag masing-masing baris secara utuh.

### 5.2. Persistent SQLite Translation Memory (`tm.py`)
- Database lokal berbasis SQLite terindeks di `tm.db`.
- Menyimpan setiap pasangan terjemahan yang berhasil: `(src_lang, tgt_lang, normalized_source) -> target_text`.
- Setiap kali pekerjaan baru dijalankan, sistem melakukan query batch 500 entri ke TM. Baris yang cocok langsung diterjemahkan secara lokal dengan latensi 0 ms dan biaya $0.
- Transaksi database menggunakan lock proses tunggal untuk menjamin integritas data dalam lingkungan multi-thread.

---

## 6. Storage, Crash Recovery & Zero-Data-Loss Guarantees

### 6.1. Thread Penulis Asinkron & Checkpoint Atomik
- Pekerja penerjemahan tidak pernah terhambat oleh operasi penulisan disk. Hasil pekerjaan dikirim ke antrean memori `ExcelWriter`.
- Writer mengumpulkan pembaruan sel dan melakukan penulisan ke disk pada kondisi:
  - Telah terkumpul sejumlah `CHECKPOINT_ROWS` (default: 50 baris).
  - Sistem berada dalam kondisi idle selama 0.5 detik.
  - Sebelum eksekusi sweep atau penutupan sesi.
- Setiap checkpoint ditulis ke file sementara `.tmp` dengan PID proses, lalu di-swap menggunakan `os.replace` sistem operasi, menjamin file spreadsheet tidak akan pernah rusak (*corrupt*) meskipun listrik padam di tengah penulisan.

### 6.2. Excel Lock Recovery (Sibling Fallback)
- Jika file spreadsheet tujuan sedang dibuka oleh pengguna di Microsoft Excel (sehingga OS mengunci file dan memicu *PermissionError*):
  - Writer tidak akan crash atau membuang data terjemahan.
  - Writer mencoba ulang dengan backoff eksponensial.
  - Jika kunci tidak dilepas, sistem secara otomatis membuat file saudara bernomor baru (misal `output_1.xlsx`), menyalin seluruh progres yang telah dicapai, dan melanjutkan penulisan ke file baru tersebut.

### 6.3. Sidecar Crash Cache & Instant Resume
- Setiap baris yang gagal secara persisten dicatat ke file JSON sidecar di `.uet_cache/failed_<stem>_<col>.json`.
- Jika sesi terhenti secara mendadak:
  - Pengguna cukup membuka file yang sama dan mengaktifkan opsi *Skip-Filled*.
  - Sistem langsung melanjutkan penerjemahan dari baris terakhir tanpa harus memindai ulang seluruh isi spreadsheet dari awal.
  - Fitur **Retranslate Failed Rows** dapat langsung dijalankan untuk mencoba kembali hanya baris-baris yang tercatat di sidecar cache.

---

## 7. The Judge: Autonomous Linguistic Quality Control

Subsistem *The Judge* adalah arbiter linguistik mandiri untuk mengevaluasi dan memperbaiki terjemahan yang sudah ada:

### 7.1. Kriteria Evaluasi Multidimensi
1. **Mistranslation:** Pergeseran makna, penghilangan informasi (*omission*), penambahan halusinasi (*addition*), atau kebalikan arti.
2. **Context:** Ketidaksesuaian makna terhadap konteks dialog atau pembicara yang tercantum di kolom konteks.
3. **Placeholder:** Kerusakan, hilangnya, atau salah tempatnya token kontrol game (`[BR]`, `%s`, `{name}`).
4. **Style:** Ketidakkonsistenan nada bicara, register bahasa (formal vs kasual), atau terminologi karakter.
5. **Fitting:** Kepatuhan terhadap batasan panjang teks (conciseness) agar dialog pas di dalam batas box UI game dan tidak meluap (*overflow*).

### 7.2. Severity Grading
- **Critical:** Kesalahan fatal yang mengubah alur cerita atau membuat instruksi tidak dapat dipahami pemain.
- **Moderate:** Makna inti selamat tetapi terdapat nuansa bahasa yang keliru atau canggung.
- **Minor:** Isu register atau preferensi stilistik minor tanpa merusak pemahaman.

### 7.3. Perbaikan Berbantuan AI & Proteksi Staging
- **In-Memory Staging:** Hasil rekomendasi perbaikan (*Fix All*) ditampung terlebih dahulu di memori. File spreadsheet tidak disentuh sampai pengguna menekan tombol persetujuan (*Confirm & Save*).
- **Single-Row Fix It:** Pengguna dapat mengklik baris bermasalah untuk meminta model memberikan alternatif perbaikan secara langsung atau mengedit teks secara manual.
- **Anti-Oscillation Guard:** Mencegah perdebatan kritik berulang antara model evaluator saat dilakukan *Re-Judge*, menghentikan loop otomatis jika penilaian sudah stabil.

---

## 8. Comparative Playground & Benchmarking

- **Blinded Triple-Slot Testing:** Membandingkan hingga 3 model sekaligus tanpa bias (diberi label Slot A, B, C).
- **Metrik Kinerja Real-Time:** Mengukur latensi request (*latency ms*), kecepatan pembentukan token (*tokens/sec*), dan waktu respons pertama (*Time-to-First-Token / TTFT*).
- **Inspeksi Penalaran (*Reasoning Inspection*):** Menampilkan pemikiran internal model (*chain-of-thought*) secara terpisah dari konten jawaban akhir.
- **Arbiter Evaluator Otomatis:** Meminta model ke-4 bertindak sebagai juri independen untuk menilai hasil terjemahan Slot A, B, dan C serta menentukan pemenang secara objektif.
- **Interactive Chat Tab:** Fasilitas interaktif untuk menguji prompt sistem dengan dukungan streaming teks dan pemikiran yang dapat dilipat/dibuka (*collapsible*).

---

## 9. Enterprise Security & Provider Management

- **Zero Hardcoded Providers:** Sistem tidak memiliki daftar provider baku di dalam kode. Seluruh endpoint, header, dan URL dikonfigurasi secara bebas oleh pengguna saat runtime.
- **Enkripsi Lokal Kelas Militer:** Konfigurasi provider, model, dan API key diamankan di dalam `vault.enc` menggunakan PBKDF2-HMAC-SHA256 (310.000 iterasi) + AES-CTR keystream + autentikasi HMAC-SHA256.
- **Proteksi Lockout & Reset Destruktif:** Salah memasukkan kata sandi 3 kali akan mengunci brankas. Pengguna diwajibkan mengetik kata `DELETE` secara sadar jika ingin mereset brankas yang terkunci.
- **Dynamic Reasoning Metadata:** Injeksi parameter berpikir (seperti `chat_template_kwargs.enable_thinking` atau `reasoning_effort`) didefinisikan sebagai metadata data model, menjamin kompatibilitas masa depan terhadap model AI baru tanpa perlu memperbarui kode aplikasi.
