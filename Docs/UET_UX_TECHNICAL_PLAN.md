# UET UX Redesign — Technical Plan

## Tujuan

Rombak TUI UET agar lebih konsisten, rapi, cepat digunakan power user, dan mudah dikembangkan tanpa membuat tampilan antarfitur berbeda-beda.

Fokus utama:
- satu pola UI untuk fungsi yang sama;
- satu flow Translate dan satu flow Judge;
- keyboard dan mouse sama-sama nyaman;
- processing otomatis tetapi tetap bisa dioverride;
- popup dipakai untuk aksi lokal agar user tidak sering pindah screen;
- tampilan hasil dan progress dibuat lebih minimal.

---

## 1. Aturan UI Global

Buat komponen bersama dan gunakan ulang di seluruh aplikasi.

Komponen utama:
- Model Picker
- File Picker
- Column Picker
- Language Picker
- Checkbox / Multi Select
- Text Input
- Popup / Modal
- Processing Form
- Review Screen
- Progress Panel
- Filter Bar
- Summary Screen
- Action Bar / Footer

Aturan:
- fitur baru tidak boleh membuat bentuk baru jika fungsi UI-nya sudah tersedia;
- hover, focus, selected, active, dan disabled harus konsisten;
- seluruh baris yang terlihat dapat dipilih harus dapat diklik;
- shortcut utama tetap tersedia;
- footer hanya menampilkan aksi penting, sisanya melalui `? More`.

---

## 2. Struktur Navigasi

Home dibagi menjadi:

### Work
- Translate
- Judge
- Playground

### Configure
- Providers & Models
- Model Groups
- Settings

Breadcrumb tetap digunakan agar user selalu tahu posisi.

Contoh:

`UET > Translate > Review`

---

## 3. First Setup dan Provider

Flow:

`Create Vault → Add Provider → Add Connection → Test → Fetch Models → Configure Model → Home`

Provider dapat memiliki banyak connection.

Setiap connection menyimpan:
- nama;
- API key;
- status;
- rate limit jika diperlukan.

Model menyimpan:
- model ID;
- RPM;
- TPM;
- reasoning setting;
- batas lain yang diperlukan.

Provider dengan banyak connection dapat memakai:
- Round Robin
- Fallback

Rate limit harus mendukung:
- independent per connection;
- shared across provider.

Perhitungan kapasitas processing mengikuti pilihan ini.

---

## 4. Model Groups

Ganti nama tampilan Combo menjadi **Model Groups**.

Flow:

`New Group → Add Model → Select Strategy → Save`

Saat Add Model dibuka, tampilkan seluruh model yang sudah terdaftar dari semua provider dan connection.

Satu group dapat berisi model dari:
- provider yang sama;
- connection berbeda;
- provider berbeda.

Strategy:
- Fallback
- Round Robin

Model Group harus dapat dipilih melalui Model Picker yang sama dengan model biasa.

---

## 5. Translate

Hapus Quick dan Full.

Flow baru:

`Select Model → File & Columns → Language & Style → Processing → Review → Run → Summary`

### File & Columns

File dan column mapping berada di screen yang sama.

Setelah file dibuka:
- tampilkan preview;
- deteksi Source dan Output otomatis;
- user dapat mengganti hasil deteksi.

Auto-detection header:

Source keywords:
- `original`
- `source`
- `input`
- `src`

Output keywords:
- `translation`
- `translated`
- `target`
- `output`

Pencocokan:
- case-insensitive;
- abaikan perbedaan spasi, `_`, `-`, dan penulisan gabungan;
- contoh `Original Text`, `original_text`, `OriginalText` tetap dianggap kandidat Source.

Jika ada kandidat setara pada tabel yang terduplikasi, pilih pasangan paling kiri.

Jika tidak ada kandidat yang cukup jelas, jangan menebak. Minta user memilih manual.

Column Picker yang sama harus dipakai oleh Judge dan fitur lain.

### Language & Style

Gabungkan:
- source language;
- target language;
- style;
- context bila digunakan;
- custom prompt bila digunakan.

### Processing

Nilai default dihitung otomatis berdasarkan:
- RPM;
- TPM;
- jumlah connection aktif;
- panjang teks;
- estimasi token;
- jumlah worker;
- batas model.

Settings menyediakan:
- preferred rows/request: default 15;
- maximum rows/request: default 100.

Preferred adalah target, bukan angka wajib.

Jika kapasitas aman di bawah preferred, gunakan hasil kalkulasi yang lebih rendah.

Nilai hasil kalkulasi tampil redup di field. User cukup Enter untuk menerima.

Jika user mengubah nilai, tandai sebagai manual override.

Jika override berisiko melewati kapasitas, tampilkan warning tetapi jangan blokir power user.

### Review

Review menampilkan:
- file;
- row range;
- source/output column;
- language;
- model/group;
- style;
- processing;
- output file.

Sediakan:
- Edit
- Start Translation

Edit membuka pilihan bagian yang ingin diubah tanpa memaksa user kembali screen demi screen.

### Run

Tampilan utama fokus ke:
- progress;
- completed;
- success;
- retry;
- failed;
- speed;
- ETA;
- worker state.

Raw activity dan row feed dipindahkan ke panel tambahan.

### Summary

Tampilkan hasil pekerjaan dan aksi:
- Retry Failed
- Judge Translation
- Translate Another
- Home

---

## 6. Judge

Hapus Quick dan Full.

Flow:

`Select Model → File & Columns → Judge Criteria → Language & Style → Processing → Review → Run → Results → Confirm & Save → Summary`

Gunakan komponen Model Picker, Column Picker, Processing, Review, dan Summary yang sama dengan Translate.

### Judge Criteria

Gunakan multi-select untuk memilih pemeriksaan:
- mistranslation;
- context;
- placeholder;
- style;
- kategori lain yang sudah didukung.

### Judge Results

Ubah menjadi layout minimal berbasis daftar + detail.

Bagian atas:
- Problems
- Fixed

Filter cepat:
- All
- Critical
- Moderate
- Minor
- Style
- Fixed

Data tetap dibedakan menjadi:
- Severity
- Category
- Status

Ketika row dipilih, tampilkan detail pada panel yang sama.

Tidak perlu pindah screen.

### Manual Edit

Translation pada row terpilih dapat diedit langsung.

Setelah Save:
- status menjadi Fixed;
- tandai sebagai Manual Edit.

### Fix Satu Row

Klik Fix → tampil popup pemilihan model.

Popup memiliki:
- model picker;
- `Don't show again this session`.

Jika dicentang, model Fix dipakai selama sesi Judge saat ini.

Saat membuka Judge baru, pilihan di-reset.

Fix berjalan realtime di panel Judge Results tanpa pindah screen.

### Fix All

Klik Fix All → popup selalu tampil.

Popup berisi:
- filter/scope row yang ingin diperbaiki;
- model;
- `Don't show model again this session`.

Scope tidak boleh disimpan permanen.

Fix All berjalan realtime di Judge Results.

Status row dapat berubah:
- Waiting
- Fixing
- Fixed
- Failed

### Rejudge

Row yang sudah Fixed melalui:
- Fix;
- Fix All;
- Manual Edit

dapat menjalankan Rejudge.

Rejudge dilakukan terhadap row fixed tanpa pindah screen.

### Confirm & Save

Setelah user puas:
- tampilkan Problems;
- Fixed;
- Remaining;
- Confirm & Save.

Setelah save, tampilkan Summary.

Judge Again tetap tersedia dari Summary sebagai evaluasi penuh baru.

---

## 7. Processing Bersama

Translate dan Judge harus memakai satu pola Processing.

Field utama:
- Rows / Request
- Workers
- Request Delay / Pacing

Nilai otomatis berasal dari rate limit dan kondisi job.

Contoh perilaku:
- preferred 15;
- sistem menghitung aman 8;
- field default menjadi 8;
- user Enter untuk menerima;
- user dapat mengetik 15 atau angka lain sebagai override.

Jangan membuat Processing UI terpisah antara Translate dan Judge.

---

## 8. Keyboard dan Mouse

Keyboard:
- `↑ ↓` memilih;
- `Enter` select / continue;
- `Esc` back;
- `Tab` pindah bagian;
- `Space` toggle;
- `?` command tambahan.

Mouse:
- seluruh row pilihan dapat diklik;
- checkbox, tab, button, filter, popup, dan table row dapat diklik;
- hover state wajib terlihat.

Jangan membuat fungsi penting hanya tersedia melalui shortcut.

---

## 9. Refactor Source yang Mendukung UX

Pisahkan UI dari proses utama agar perubahan tampilan tidak merusak logic.

Target utama:
- kurangi logic dari Screen besar;
- pecah Judge screen menjadi setup, results, widgets, dan popup;
- gunakan state yang jelas untuk Translate dan Judge;
- gunakan komponen UI bersama;
- jangan membuat ulang picker atau processing form pada fitur lain;
- UI hanya mengirim aksi dan menerima state/progress.

Prioritaskan rewrite pada screen yang terlalu besar, bukan rewrite seluruh engine.

---

## 10. Urutan Implementasi

1. Buat komponen UI bersama.
2. Rombak navigation dan Home.
3. Rombak Provider & Model.
4. Rombak Model Groups.
5. Rombak Translate end-to-end.
6. Rombak shared Processing.
7. Rombak Judge setup.
8. Rombak Judge Results, Fix, Fix All, Rejudge.
9. Rapikan Summary dan footer.
10. Uji seluruh flow dengan keyboard dan mouse.

Selesaikan satu flow sampai stabil sebelum berpindah ke flow berikutnya.
