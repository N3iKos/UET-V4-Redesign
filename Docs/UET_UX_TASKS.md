# UET UX Redesign — Tasks

## Phase 1 — UI Foundation
- [ ] Buat style dan layout global yang seragam.
- [ ] Buat reusable Model Picker, File + Column Picker, Language Picker, Processing Form.
- [ ] Buat reusable Review, Progress, Summary, Popup, Filter, dan Footer.
- [ ] Samakan hover, focus, selected, disabled, dan click area.
- [ ] Pastikan semua komponen nyaman dipakai mouse dan keyboard.

## Phase 2 — Home & Navigation
- [ ] Pisahkan Home menjadi Work dan Configure.
- [ ] Rapikan breadcrumb.
- [ ] Standarkan Back, Continue, Edit, Confirm, dan More Commands.
- [ ] Kurangi shortcut yang tampil langsung di footer.

## Phase 3 — Provider & Models
- [ ] Rapikan flow Vault → Provider → Connection → Model.
- [ ] Dukung banyak connection dalam satu provider.
- [ ] Tambahkan Round Robin / Fallback.
- [ ] Tambahkan rate limit independent/shared.
- [ ] Pertahankan Fetch Models dari endpoint.
- [ ] Rapikan detail RPM, TPM, reasoning, dan status.

## Phase 4 — Model Groups
- [ ] Ubah label Combo menjadi Model Groups.
- [ ] Add Model menampilkan model yang sudah terdaftar.
- [ ] Izinkan model dari provider/connection berbeda.
- [ ] Dukung Fallback dan Round Robin.
- [ ] Model Group muncul di Model Picker biasa.

## Phase 5 — Column Auto Detection
- [ ] Source keywords: `original`, `source`, `input`, `src`.
- [ ] Output keywords: `translation`, `translated`, `target`, `output`.
- [ ] Case-insensitive dan abaikan spasi, `_`, `-`, serta penulisan gabungan.
- [ ] Prioritaskan kandidat paling kiri jika ada tabel duplikat.
- [ ] Jika tidak yakin, biarkan kosong dan minta user select manual.
- [ ] Gunakan Column Picker yang sama pada Translate dan Judge.

## Phase 6 — Translate
- [ ] Hapus Quick / Full.
- [ ] Flow: Model → File & Columns → Language & Style → Processing → Review → Run → Summary.
- [ ] Gabungkan file selection dan column mapping.
- [ ] Terapkan processing otomatis berdasarkan RPM/TPM.
- [ ] Default preferred rows/request 15, maximum 100.
- [ ] Tampilkan nilai rekomendasi redup di field.
- [ ] Izinkan manual override dengan warning.
- [ ] Review memiliki Edit per bagian.
- [ ] Run fokus pada progress, worker, retry, failed, speed, ETA.
- [ ] Pindahkan raw activity ke panel tambahan.
- [ ] Summary menyediakan Retry Failed dan Judge Translation.

## Phase 7 — Judge Setup
- [ ] Hapus Judge Quick / Full.
- [ ] Gunakan shared Model Picker, Column Picker, Language, Processing, dan Review.
- [ ] Tambahkan multi-select Judge Criteria.
- [ ] Samakan pola setup dengan Translate.

## Phase 8 — Judge Results
- [ ] Buat layout daftar + detail.
- [ ] Tambahkan filter All / Critical / Moderate / Minor / Style / Fixed.
- [ ] Pisahkan Severity, Category, dan Status.
- [ ] Detail row tampil tanpa pindah screen.
- [ ] Manual Edit → status Fixed.
- [ ] Fix satu row melalui popup model.
- [ ] Tambahkan `Don't show again this session`.
- [ ] Fix All popup berisi scope dan model.
- [ ] Fix/Fix All berjalan realtime pada panel yang sama.
- [ ] Tambahkan Rejudge untuk row Fixed.
- [ ] Tambahkan Confirm & Save.
- [ ] Tambahkan Judge Summary dan Judge Again.

## Phase 9 — Cleanup
- [ ] Hilangkan UI lama yang sudah tidak dipakai.
- [ ] Hilangkan duplicate picker/form.
- [ ] Pecah Judge screen yang terlalu besar.
- [ ] Pindahkan logic proses keluar dari Screen bila tidak terkait render/input.
- [ ] Pastikan fitur baru memakai komponen UI bersama.

## Phase 10 — UX Validation
- [ ] Tes Translate dengan keyboard dan mouse.
- [ ] Tes Judge dengan keyboard dan mouse.
- [ ] Tes Provider dan Model Groups.
- [ ] Tes header auto-detect.
- [ ] Tes file tanpa header yang dapat dikenali.
- [ ] Tes tabel duplikat kiri/kanan.
- [ ] Pastikan fungsi yang sama selalu memakai pola UI yang sama.
