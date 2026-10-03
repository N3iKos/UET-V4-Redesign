# UET-V4 System Architecture Specification

## 1. Executive Summary & System Purpose

**Universal Excel Translator v4 (UET-V4)** adalah platform lokalisasi spreadsheet berbasis Text User Interface (TUI) berperforma tinggi. Sistem ini dirancang khusus untuk memproses penerjemahan, perbaikan (*repair*), dan evaluasi mutu (*judging*) batch spreadsheet (XLSX, CSV) berskala besar (ribuan hingga puluhan ribu baris) menggunakan LLM eksternal berbasis endpoint OpenAI-compatible.

Berbeda dari script otomatisasi penerjemahan tradisional yang rentan terhadap desinkronisasi baris, rate limit API, kegagalan jaringan, atau kebocoran kunci API, UET-V4 dibangun dengan arsitektur enterprise-grade yang mengedepankan prinsip:
1. **Zero Data Loss & Resiliency:** Seluruh operasi penulisan disk bersifat asinkron dan atomik (`.tmp` + `os.replace`), dilengkapi mekanisme pemulihan kunci (*lock-recovery*), pemulihan crash (*sidecar cache*), dan *in-flight batch safety*.
2. **Provider Agnostic & Zero Hardcoded Topology:** Kode sumber tidak memiliki referensi hardcoded ke nama provider mana pun. Seluruh hirarki `Provider -> Connection -> Model` disimpan terenkripsi dalam *vault* lokal dan didefinisikan secara dinamis oleh pengguna saat runtime.
3. **Anti-Desynchronization Pipeline:** Memiliki mesin validasi 3 lapis untuk mendeteksi pergeseran baris (*row shifting*), penggabungan baris (*row merging*), halusinasi, dan degradasi placeholder token game sebelum hasil ditulis ke spreadsheet.
4. **Autonomous Arbitration & Quality Control (The Judge):** Subsistem evaluasi linguistik terpisah yang mampu membedah terjemahan terhadap sumber, menilai tingkat keparahan (*severity*), merekomendasikan perbaikan (*Fix All*), dan melakukan re-judging dengan proteksi anti-osilasi (*anti-flip cycle guard*).

---

## 2. High-Level Architectural Decomposition

UET-V4 mengadopsi arsitektur berlapis (*layered, decoupled architecture*) di mana lapisan UI tidak pernah memblokir thread eksekusi, dan lapisan engine tidak memiliki ketergantungan langsung ke framework visual:

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                TEXTUAL TUI LAYER                                       │
│  UETApp (Screen Stack: Vault -> MainMenu -> Wizard -> Run -> Summary / Judge / Play)  │
│  Shared Widgets: ModelPicker | FileTabBar | ActivityPanel | EventsPanel | CompareGrid  │
└──────────────────────────────────────────┬─────────────────────────────────────────────┘
                                           │ Thread boundary (@work(thread=True) / call_from_thread)
┌──────────────────────────────────────────▼─────────────────────────────────────────────┐
│                          CORE PROCESSING ENGINE SUBSYSTEM                              │
│  - ProcessingEngine: Row Classifier, Range Slicer, Resume/Sweep, Repair Phase         │
│  - MultiFileDraftManager: Leader/Follower Inheritance, Smart Worker Allocator         │
│  - DedupManager: Hash Fingerprinting, Unique File Generation, Tagged Mapping          │
│  - TranslationMemory (TM): Persistent SQLite (tm.db) Normalized Cache                 │
└─────────────────────┬────────────────────────────────────────────────────┬─────────────┘
                      │ Batches (BatchTask)                                │ Raw Text
┌─────────────────────▼────────────────────────────┐ ┌─────────────────────▼─────────────┐
│          SCHEDULING & RESILIENCY ENGINE          │ │      PARSING & ALIGNMENT ENGINE       │
│  - Scheduler: Sequential | Parallel | Wave       │ │  - ParseRecoveryPipeline:             │
│  - Watchdog Monitor: Timeout & Generation Bump   │ │      Strict [N] -> Loose -> Solo      │
│  - RetryExecutor: Priority Failover & Backoff    │ │  - AlignmentValidator:               │
│  - ConnectionPool & ComboPool: LB & Rotation     │ │      Echo -> Metric Shift -> Token    │
│  - KeyPool: Least-Used, Latency, Quota Machine   │ │  - PlaceholderEngine: Game Token Enc  │
└─────────────────────┬────────────────────────────┘ └─────────────────────┬─────────────┘
                      │ Leases & HTTP Calls                                │ Filtered Results
┌─────────────────────▼────────────────────────────┐ ┌─────────────────────▼─────────────┐
│            PROVIDER & SECURITY LAYER             │ │             I/O & RECOVERY                │
│  - ProviderClient: Generic OpenAI HTTP/Stream    │ │  - ExcelReader: Stream Column / CSV   │
│  - Vault: AES-CTR + PBKDF2 (310k) + HMAC-SHA256  │ │  - ExcelWriter: Async Worker Thread   │
│  - ProviderRegistry: In-Memory Decrypted Cache   │ │  - Lock Recovery: Sibling Forking     │
│  - ReasoningConfig: User-Defined Meta Paths      │ │  - FailedCache: Atomic Sidecar JSON   │
└──────────────────────────────────────────────────┘ └───────────────────────────────────┘
```

---

## 3. Subsystem Breakdown & Component Contracts

### 3.1. Textual TUI Layer (`translator/tui/`)
- **`UETApp` (`app.py`):** Root application shell turunan dari `textual.app.App`. Memegang state global sesi yang tidak di-persist ke disk (instance `ProviderRegistry` yang sudah di-unlock, `JobConfig` draft untuk Translate dan Judge, serta session-scoped state untuk evaluasi baris tunggal).
- **Navigation Lifecycle:** Menggunakan push/pop screen stack (`push_screen`, `pop_screen`). Menekan `Esc` atau `Left` pada screen anak secara deterministik mengembalikan pengguna ke screen sebelumnya tanpa kehilangan data form yang sedang diisi.
- **Thread Isolation:** Seluruh interaksi jaringan dan pemrosesan spreadsheet dijalankan di latar belakang menggunakan decorator `@work(thread=True)`. Perubahan state UI dari thread pekerja wajib diposting kembali ke UI thread melalui `app.call_from_thread()`.
- **Theme & Markup Safety (`theme.py`):** Menggunakan semantic theme khusus terminal dark mode. Seluruh string yang berasal dari provider atau teks spreadsheet dilewatkan melalui `rich.text.Text` atau di-escape untuk mencegah karakter kurung siku (seperti `[tag]` atau `[N]`) salah diparse sebagai format markup Rich/Textual.

### 3.2. Core Processing Engine (`translator/core/`)
- **`ProcessingEngine` (`engine.py`):** Koordinator orkestrasi siklus hidup pekerjaan.
  - Membaca baris sumber melalui `ExcelReader`.
  - Mengklasifikasikan status baris: apakah kosong, sudah terisi (pada mode *resume/skip-filled*), atau tercatat gagal pada *sidecar cache*.
  - Melakukan lookup awal ke SQLite Translation Memory (`tm.py`). Baris dengan kecocokan persis langsung diterjemahkan secara lokal tanpa memanggil API LLM.
  - Membangun antrean `BatchTask` terurut sesuai konfigurasi `batch_size`.
  - Menginstansiasi `Scheduler` dan menghubungkannya dengan event listener UI.
  - Menjalankan fase *Sweep* otomatis (hingga 2 putaran) untuk mencoba kembali baris-baris yang gagal akibat anomali sesaat sebelum pekerjaan dinyatakan selesai.
  - Melakukan chaining opsional ke fase *Repair* jika diaktifkan.
- **`MultiFileDraftManager` (`multi_file.py`):** Mengelola sesi hingga 4 file secara simultan. File 0 bertindak sebagai *Leader*; perubahan pengaturan pada Leader secara otomatis diwariskan ke *Follower* (file 1-3) kecuali kolom atau konfigurasi tertentu telah di-override secara eksplisit oleh pengguna. Menghitung alokasi konkurensi pekerja (*worker distribution*) berbasis pasangan `(provider_id, model_id)`.
- **`DedupManager` (`dedup_file.py`):** Mesin deduplikasi cerdas. Menghitung fingerprint hash kolom sumber. Jika terdapat banyak baris teks berulang (misalnya dialog game NPC umum atau frasa UI), mesin membuat file sementara berisikan teks unik saja (`.uet_cache/unique_*.xlsx`). Setelah diterjemahkan, hasilnya disebarkan (*broadcast*) kembali ke seluruh baris duplikat di file target dengan tetap mempertahankan prefix tag game.

### 3.3. Scheduling & Resiliency Layer (`translator/scheduling/`)
- **`Scheduler` (`scheduler.py`):** Mengelola eksekusi batch berdasarkan 3 strategi:
  - `sequential`: Memproses satu batch dalam satu waktu dengan jeda waktu tertentu (`batch_delay`). Sangat aman untuk model tier gratis dengan rate limit ketat.
  - `parallel`: Menjalankan *N* thread pekerja daemon independen yang mengonsumsi antrean batch bersama (`Queue`). Dilengkapi *Watchdog Thread* yang memantau pekerja yang hang atau stuck, menaikkan nomor generasi batch (`generation`), dan mengantrekan ulang pekerjaan ke pekerja lain.
  - `wave`: Menjalankan pekerja secara serentak dalam satu gelombang (lockstep). Gelombang berikutnya hanya berjalan jika seluruh batch dalam gelombang saat ini telah selesai atau timeout, dengan jeda antar gelombang (`wave_delay`).
- **`Recovery Ladder` (Tangga Pemulihan):** Jika sebuah batch gagal diparse atau mengalami desinkronisasi:
  1. Scheduler membuat *mini-task* yang hanya berisi baris-baris yang hilang dari batch tersebut.
  2. Jika mini-task masih menyisakan baris yang gagal, sistem turun ke level *solo-task* (satu request per satu baris). Solo-task memiliki kekebalan mutlak terhadap desinkronisasi baris karena pemetaan barisnya bersifat 1:1.
- **`RetryExecutor` (`retry.py`):** Menangani loop percobaan ulang per-request, klasifikasi error (rate-limit, kuota, auth, server 5xx), backoff eksponensial, dan pengalihan ke provider cadangan (*Priority Failover*).
- **`ConnectionPool` & `ComboPool` (`connection_pool.py`, `combo_pool.py`):** Mengatur load balancing antar koneksi. Mendukung strategi `fallback` (beralih hanya saat koneksi aktif gagal) dan `round_robin` (berganti koneksi secara teratur berdasarkan jumlah baris yang telah diselesaikan).
- **`KeyPool` (`key_pool.py`):** Manajemen multi-API key per koneksi. Memilih key aktif berdasarkan prinsip *least-used* dan *lowest latency*, serta melacak cooldown lokal dan status kehabisan kuota harian (*daily exhaustion*).

### 3.4. Parsing, Placeholders & Anti-Desynchronization (`translator/parsing/`)
- **`ParseRecoveryPipeline` (`recovery.py`):** Mengonversi teks keluaran LLM menjadi mapping nomor baris ke terjemahan:
  - *Strict Match:* Mencari pola `[N]`, `[Row N]`, atau `[Line N]`.
  - *Loose Match:* Mendeteksi format angka standar seperti `N.`, `N)`, `(N)`, atau `Row N:`.
  - *Fuzzy Match:* Memperbaiki nomor baris yang typo/bergeser berdasarkan pencocokan 2 digit terakhir nomor baris pada batch kecil (≤ 50 baris).
  - *Positional Fallback:* Hanya diperbolehkan untuk batch 1 baris. Dilarang keras untuk multi-baris guna mencegah pergeseran baris terselubung.
- **`validate_alignment` (`alignment.py`):** Mesin anti-desinkronisasi:
  - *Layer 1:* Menolak batch jika hasil terjemahan menggemakan persis teks sumber baris tetangganya.
  - *Layer 2 (Statistical Shift Detection):* Menganalisis korelasi panjang teks, tanda baca terminal (`?`, `!`, `.`), dan tanda petik antara terjemahan baris ke-i dengan sumber baris ke-i versus baris ke-(i+1). Jika skor kemiripan lebih condong ke baris berikutnya, batch dicurigai bergeser.
  - *Layer 2b (Merge Detection):* Mendeteksi model yang menggabungkan terjemahan baris A dan B menjadi satu di baris A, lalu mengosongkan baris B.
  - *Layer 3:* Memverifikasi integritas token game (`[BR]`) dan rasio panjang karakter yang wajar.
- **`placeholders.py`:** Mengenkode karakter kontrol game (seperti `\n`, `\@`, RPG Maker codes `\c[2]`, `\v[1]`, dll.) menjadi representasi inert `[BR][XX]` sebelum dikirim ke LLM, dan mendekodenya kembali setelah parsing berhasil.

### 3.5. Quality Arbitration Subsystem / The Judge (`translator/judge/`)
- Subsistem independen yang bertugas meninjau kualitas terjemahan yang sudah ada di spreadsheet.
- Mengirimkan teks sumber berdampingan dengan terjemahan ke LLM arbiter dengan instruksi netral dan obyektif.
- Mengklasifikasikan isu ke dalam kriteria: `mistranslation`, `context`, `placeholder`, `style`, dan `fitting`.
- Mengelompokkan tingkat keparahan (*severity*): `critical`, `moderate`, dan `minor`.
- Memiliki alur *Fix All* yang menghasilkan *staged fixes* di memori; teks yang diperbaiki **tidak pernah** langsung menimpa file sebelum disetujui secara eksplisit oleh pengguna (*human approval*).
- Memiliki *Re-Judge Loop* dengan proteksi anti-osilasi: jika model A mengkritik kata X diganti Y, dan saat di-rejudge model mengkritik kata Y diganti X, detektor stabilitas menghentikan loop otomatis dan menyerahkan keputusan ke tinjauan manual pengguna.

### 3.6. I/O & Persistence Layer (`translator/io/`)
- **`ExcelReader` (`excel_io.py`):** Membaca XLSX via `openpyxl` secara streaming (`read_only=True`) untuk meminimalkan alokasi memori pada file raksasa. Membaca CSV via adapter `_CsvWorkbook` yang memelihara dialect, encoding (UTF-8, UTF-8-BOM, CP1252), delimiter, dan quote character.
- **`ExcelWriter` (`excel_io.py`):** Berjalan di thread tersendiri (`_AsyncWriteThread`).
  - Mengakumulasi buffer hasil terjemahan dan melakukan checkpoint berkala ke disk (setiap `CHECKPOINT_ROWS` atau saat idle).
  - Menulis file sementara (`.tmp`) lalu melakukan `os.replace` atomik.
  - **Lock Recovery:** Jika file output sedang dibuka atau dikunci oleh program lain (misalnya Microsoft Excel), writer mencoba ulang dengan backoff eksponensial. Jika tetap terkunci setelah batas toleransi, writer otomatis membuat file saudara bernomor baru (misal: `output_1.xlsx`) dan mengalihkan penulisan ke sana tanpa mematikan proses penerjemahan.
- **`FailedCache` (`translator/core/failed_cache.py`):** Menyimpan daftar nomor baris yang gagal dalam file JSON sidecar terpisah di folder `.uet_cache/`. Memungkinkan fitur *Resume* dan *Retranslate Failed Rows* bekerja instan tanpa harus memindai ulang seluruh isi spreadsheet.

### 3.7. Security & Provider Storage (`translator/vault/`)
- **Enkripsi File Lokal (`vault.py`):** File konfigurasi provider dan API key (`vault.enc`) dienkripsi menggunakan:
  - Derivasi Kunci: PBKDF2-HMAC-SHA256 dengan 310.000 iterasi dan salt acak 16 byte kriptografis.
  - Cipher: AES-CTR emulation via SHA-256 keystream XOR blocks dengan nonce acak 16 byte.
  - Integritas Data: HMAC-SHA256 tag untuk autentikasi ciphertext (*encrypt-then-MAC*).
- **Proteksi Brute-force:** Batas maksimal 3 kali percobaan kata sandi. Setelah itu, antarmuka mengharuskan pengguna mengetik kata `DELETE` secara eksplisit jika ingin mereset vault.

---

## 4. Architectural Invariants

1. **Non-Destructive Source Policy:** File spreadsheet sumber (`src_path`) bersifat *strictly read-only*. Engine tidak pernah membuka file sumber dalam mode tulis atau memodifikasi sel di dalamnya.
2. **Atomic Write Guarantee:** Tidak ada penulisan langsung ke file output tujuan secara in-place. Seluruh operasi simpan selalu melalui file temporer dan di-swap menggunakan `os.replace` atomik sistem operasi.
3. **Approval-Gated Staging:** Fitur perbaikan (*Fix All* / *Repair*) dalam subsistem Judge tidak pernah memodifikasi sel spreadsheet tanpa persetujuan eksplisit pengguna di antarmuka.
4. **Provider-Agnostic Core:** Tidak ada percabangan kode `if provider == "openai"` atau `if model == "gemini"` di dalam logika inti engine atau scheduler. Perbedaan payload (seperti format reasoning/thinking) diakomodasi melalui metadata deklaratif `ReasoningConfig`.
5. **Generational Task Safety:** Pekerjaan yang dialihkan oleh watchdog akibat timeout atau thread stuck ditandai dengan kenaikan nomor `generation`. Hasil yang terlambat tiba dari thread lama akan diabaikan secara otomatis untuk mencegah *race condition* dan data korup.
