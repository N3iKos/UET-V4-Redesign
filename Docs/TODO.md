# UET UX Redesign — TODO

Rencana kerja ini diturunkan dari `Docs/UET_UX_TECHNICAL_PLAN.md` dan
`Docs/UET_UX_TASKS.md`, dicocokkan dengan kondisi source saat ini. Prinsip:
tidak mengubah business logic yang sudah jalan, tidak menghapus fitur kecuali
digantikan flow baru, dan **fungsi yang sama wajib memakai komponen bersama**.

---

## 0. Peta Source Saat Ini

Entry point: `main.py` → `translator/tui/app.py` (`UETApp`).

Kerangka layar: `tui/app.py`
- `BaseScreen` (`app.py:38`) — header/body/footer + breadcrumb + navigasi Back/Esc.
- `MenuScreen` (`app.py:145`) — body berupa satu `Picker`.
- `MainMenuScreen` (`app.py:175`) — menu utama (flat, 7 item).

Widget bersama yang SUDAH ada dan bagus (dipertahankan):
`tui/widgets/common.py`
- `Picker` + `Option` (`:65/:50`) — list, multi-select, klik penuh, hover/focus.
- `LabeledInput` (`:334`), `LabeledTextArea`/`PasteArea` (`:640/:571`).
- `KeyHints` (`:445`) + `StatusBar` (`:685`) — footer, tiap chip = tombol klik.
- `Spinner` (`:296`), `ErrorBox` (`:712`), `TranslationFeed` (`:753`).
- `ColumnPreview` + `ColumnRole` (`:904/:896`) — grid spreadsheet, klik-isi kolom.
- helper cell/paint (`fit_width`, `pad_cells`, `strip_line`, `truncate`).

`tui/widgets/compare.py` → `CompareGrid` + `Column` — grid daftar+catatan.
`tui/widgets/activity.py` → `ActivityPanel` — panel worker.
`tui/theme.py` → role warna + CSS global (hover/select/focus sudah konsisten).

Logic bisnis (JANGAN diubah untuk UX): `core/engine.py`, `scheduling/*`,
`providers/*`, `judge/runner.py`, `judge/prompts.py`, `judge/estimator.py`,
`io/excel_io.py`, `data/*`, `providers/selection.py` (`ModelChoice`).

---

## 1. Inventaris Komponen — Keep / Refactor / Replace

### Pertahankan apa adanya
- `Picker`, `Option`, `LabeledInput`, `LabeledTextArea`, `KeyHints`,
  `StatusBar`, `Spinner`, `ErrorBox`, `ColumnPreview`, `CompareGrid`,
  `ActivityPanel`, `TranslationFeed`, `theme.py`.
- `providers/selection.py::ModelChoice` — sudah menyatukan resolusi model/combo.
- Seluruh engine, scheduler, runner, io, vault.

### Refactor jadi komponen bersama (duplikasi yang harus disatukan)
- **Model Picker** — 3 implementasi terpisah:
  - Translate: `wizard.py::ModelSelectScreen` (`:178`)
  - Judge: `judge.py::JudgeModelScreen` (`:384`)
  - Fix All: `judge.py::FixAllSetupScreen._model_options` (`:4083`, flat)
  - Combo: `combos.py::ComboMemberModal` (`:657`)
  → Ekstrak satu `ModelPicker` (widget) berbasis `provider_column_options` /
    `combo_column_options` + `ModelChoice`. Dipakai Translate, Judge, Fix All.
- **Processing Form** — 3 implementasi terpisah:
  - Translate: `wizard.py::PacingScreen` (`:1108`)
  - Judge: `judge.py::JudgePacingScreen` (`:1311`)
  - Fix All: `judge.py::FixAllSetupScreen` (`:3948`)
  → Satu `ProcessingForm` bersama (Rows/Request, Workers, Delay/Pacing).
- **File + Column Picker** — `wizard.py::FileSelectScreen`+`ColumnsScreen`
  (`:564/:655`) vs `judge.py::JudgeFileScreen`+`JudgeColumnsScreen`
  (`:970/:1092`). Keduanya pakai `ColumnPreview` tapi layar terpisah.
  → Satu `FileColumnsScreen`/`FileColumnPicker` bersama.
- **Review Screen** — `wizard.py::PreviewScreen` (`:1369`) vs
  `judge.py::JudgePreviewScreen` (`:1573`). → Satu `ReviewScreen`.
- **Summary Screen** — `run.py::SummaryScreen` (`:574`) vs
  `judge.py::JudgeSummaryScreen` (`:4597`). → Satu `SummaryScreen` konfigurable.
- **Filter Bar** — `judge.py::FilterBar` (`:1841`) → pindah ke widgets bersama.
- **Confirm bar idiom** — `#confirm-bar` Picker diulang di banyak screen
  (`wizard.ColumnsScreen`, `JudgeCriteriaScreen`, dst). → helper `compose_confirm_bar()`.
- **Popup/Modal** — banyak `ModalScreen` ad-hoc. → base `Popup` + KeyHints footer.

### Ganti / rombak besar
- `MainMenuScreen` (`app.py:175`) → dibagi Work / Configure.
- `WizardModeScreen` (`wizard.py:155`) dan `JudgeModeScreen` (`judge.py:287`)
  → **hapus** pilihan Quick/Full.
- `judge.py` (4343 baris) → dipecah: `judge/setup`, `judge/results`,
  `judge/widgets`, `judge/popups` (Phase 9).
- Fix single & Fix All flow → berbasis popup (Phase 8).

---

## 2. Gap Analysis Ringkas (rencana vs implementasi)

| Area | Status sekarang | Aksi |
|---|---|---|
| Multi-connection per provider | SUDAH ada (`Provider.connections`, strategy) | pertahankan, rapikan UI |
| Round Robin / Fallback provider | SUDAH ada (`registry.set_strategy`) | pertahankan |
| Fetch models | SUDAH ada (`ConnectionDetailScreen.action_fetch`) | pertahankan |
| Rate limit per **connection** (independent/shared) | HANYA per-model | **buat baru** |
| Model Groups (label "Combo") | ada, bernama "Combo" | **rename** ke Model Groups |
| Model Group di Model Picker | SUDAH ada (`provider_column_options`) | pertahankan |
| Auto-detect kolom header | **belum ada** | **buat baru** |
| Quick/Full Translate & Judge | ada | **hapus** |
| Processing preferred 15 / max 100 | belum ada preferensi ini | **buat baru** di Settings |
| Nilai rekomendasi redup + override warning | belum ada | **buat baru** |
| Review dengan Edit per bagian | hanya Start/Back | **buat baru** |
| Judge multi-select criteria | SUDAH ada | pertahankan |
| Judge Results filter All/Critical/… | ada (all/problems/severity/fixed) | selaraskan label |
| Fix 1 row via popup model + "don't show again" | belum (langsung pakai judge model) | **buat baru** |
| Fix All popup (scope+model+"don't show again") | full-screen wizard | **jadikan popup** |
| Rejudge row fixed | SUDAH ada | pertahankan |
| Manual Edit → Fixed | SUDAH ada (`RowEditModal`) | pertahankan |
| Confirm & Save (Problems/Fixed/Remaining) | parsial (`JudgeFixReviewScreen`) | **lengkapi** |
| Judge Summary + Judge Again | SUDAH ada | pertahankan |

---

## 3. Tugas per Phase

### Phase 1 — UI Foundation
- [ ] Buat modul `tui/widgets/` bersama baru / perluas `common.py`:
  - [ ] `ModelPicker` (dari 3 implementasi lama, pakai `ModelChoice`).
  - [ ] `ProcessingForm` (Rows/Request, Workers, Delay/Pacing).
  - [ ] `FileColumnPicker` (file + `ColumnPreview` + mapping).
  - [ ] `ReviewPanel` (baris ringkasan + aksi Edit).
  - [ ] `SummaryPanel` (konfigurable aksi bawah).
  - [ ] pindah `FilterBar` ke widgets bersama.
  - [ ] base `Popup` + helper `compose_confirm_bar()`.
- [ ] Pastikan hover/focus/selected/disabled/click konsisten (sebagian besar
      sudah lewat `Picker`/`theme.py` — verifikasi tiap komponen baru).
- [ ] Semua komponen jalan dengan keyboard + mouse.

### Phase 2 — Home & Navigation
- [ ] `MainMenuScreen` → seksi **Work** (Translate, Judge, Playground) dan
      **Configure** (Providers & Models, Model Groups, Settings) (`app.py:175`).
- [ ] Rapikan breadcrumb `_header_text` (`app.py:125`, `wizard.py:140`,
      `judge.py:237`) jadi format konsisten `UET > Area > Step`.
- [ ] Standarkan aksi footer: Back, Continue, Edit, Confirm, `? More`.
- [ ] Kurangi shortcut yang tampil langsung; sisanya via `? More`.

### Phase 3 — Provider & Models
- [ ] Rapikan flow `ProviderListScreen → AddProviderScreen →
      ConnectionDetailScreen` (`providers.py`).
- [ ] Tambah **rate limit per connection** (independent) dan opsi **shared
      across provider**; sesuaikan perhitungan kapasitas processing.
      (baru — sekarang hanya per-model via `PresetModal`/`set_model_preset`).
- [ ] Pertahankan Fetch Models, Round Robin/Fallback, RPM/TPM/reasoning/status.
- [ ] Sesuaikan `providers/ratelimit.py` + `scheduling/*` bila perlu untuk
      kapasitas shared (hati-hati: business logic).

### Phase 4 — Model Groups
- [ ] Rename semua string user-facing "Combo" → "Model Groups"
      (`combos.py` label/breadcrumb/hint: `:89,:154,:166,:196,:283,:379,…`;
      `app.py:202` menu; `data/combos.py::COMBO_PROVIDER_LABEL`).
      Simpan `combo_id`/id internal tetap agar tidak memecah vault/logic.
- [ ] Add Model menampilkan seluruh model terdaftar lintas provider/connection
      (`ComboMemberModal`); pertahankan strategi Fallback/Round Robin.
- [ ] Pastikan Model Group tetap muncul di Model Picker bersama.

### Phase 5 — Column Auto Detection (baru)
- [ ] Buat util deteksi header (mis. `parsing/columns.py` atau `io`):
  - source keywords: `original`, `source`, `input`, `src`.
  - output keywords: `translation`, `translated`, `target`, `output`.
  - case-insensitive; abaikan spasi/`_`/`-`/gabungan (`OriginalText`).
  - duplikat → pilih pasangan paling kiri.
  - tidak yakin → kosong, minta pilih manual.
- [ ] Panggil dari `FileColumnPicker` (Translate & Judge) via `ExcelReader.grid`.

### Phase 6 — Translate (end-to-end)
- [ ] Hapus Quick/Full (`WizardModeScreen` `:155`); flow jadi
      `Model → File & Columns → Language & Style → Processing → Review → Run → Summary`.
- [ ] Gabung File + Columns (dari `FileSelectScreen`+`ColumnsScreen`).
- [ ] Gabung Language + Style (dari `LanguageScreen`+`StyleScreen`;
      Operation mode & Reasoning tetap ada tapi diintegrasikan, tidak dihapus).
- [ ] Processing otomatis dari RPM/TPM/koneksi aktif/panjang teks/token/worker;
      default preferred 15, maximum 100 (tambah di `app_prefs.py`+`config.py`).
- [ ] Tampilkan nilai rekomendasi redup di field; Enter = terima; ketik = override
      + warning bila lewat kapasitas (jangan blokir).
- [ ] Review pakai `ReviewPanel` dengan Edit per bagian (bukan hanya Start/Back).
- [ ] Run fokus progress/worker/retry/failed/speed/ETA (`run.py` sudah dekat);
      pindah raw activity/feed ke panel tambahan.
- [ ] Summary: Retry Failed, Judge Translation (sudah), tambah Translate Another.

### Phase 7 — Judge Setup
- [ ] Hapus Judge Quick/Full (`JudgeModeScreen` `:287`).
- [ ] Pakai ModelPicker, FileColumnPicker, LanguagePicker/Style, ProcessingForm,
      ReviewPanel bersama (samakan pola dengan Translate).
- [ ] Pertahankan multi-select Judge Criteria (`JudgeCriteriaScreen` `:607`).

### Phase 8 — Judge Results
- [ ] Layout daftar + detail (pakai `CompareGrid`; detail di panel yang sama).
- [ ] Filter: All / Critical / Moderate / Minor / Style / Fixed (selaraskan
      `FilterBar` `:1841` + `_apply_filter` `:2955`).
- [ ] Detail row tampil tanpa pindah screen (`RowDetailModal` → panel inline).
- [ ] Manual Edit → status Fixed + tanda Manual Edit (`RowEditModal` sudah ada).
- [ ] **Fix 1 row via popup**: model picker + `Don't show again this session`
      (baru — sekarang `action_fix_row` `:3214` langsung pakai judge model).
- [ ] **Fix All jadi popup**: scope (dari filter, tidak permanen) + model +
      `Don't show model again this session` (rombak `FixAllSetupScreen` `:3948`).
- [ ] Fix / Fix All realtime di panel yang sama (status Waiting/Fixing/Fixed/Failed).
- [ ] Rejudge untuk row Fixed (`action_judge_again*` `:3471/:3549` sudah ada).
- [ ] **Confirm & Save**: tampilkan Problems / Fixed / Remaining, lalu simpan
      (lengkapi dari `JudgeFixReviewScreen` `:4365`).
- [ ] Judge Summary + Judge Again (sudah, `JudgeSummaryScreen` `:4597`).

### Phase 9 — Cleanup
- [ ] Hapus UI lama yang tidak dipakai (Quick/Full screens, picker/form duplikat).
- [ ] Pecah `judge.py` (4343 baris) → setup / results / widgets / popups.
- [ ] Pindahkan logic proses keluar dari Screen bila tidak terkait render/input.
- [ ] Pastikan semua fitur memakai komponen UI bersama.

### Phase 10 — UX Validation
- [ ] Uji Translate (keyboard + mouse).
- [ ] Uji Judge (keyboard + mouse).
- [ ] Uji Provider + Model Groups.
- [ ] Uji auto-detect header; file tanpa header dikenali; tabel duplikat kiri/kanan.
- [ ] Verifikasi fungsi sama = pola UI sama.

---

## 4. Aturan Kerja
- Selesaikan satu flow sampai stabil sebelum pindah (urutan Phase 1→10).
- Tidak membuat picker/processing/summary baru bila komponen bersama sudah ada.
- Translate & Judge WAJIB memakai komponen bersama yang sama.
- Semua interaksi utama harus jalan via keyboard dan mouse.
- Jaga kompatibilitas vault/`combos.json`/`app_prefs.json` (jangan ubah skema
  yang memecah data lama tanpa migrasi).

---

## 5. Bug Judge UX & Processing — Status & Root Cause

Semua 10 masalah dikerjakan di `translator/tui/screens/judge.py`
(`JudgeRunScreen` + `JudgePacingScreen`), dengan komponen bersama
(`ProcessingForm`, `ProviderNotice`, `ActivityPanel`) dan tanpa mengubah
business logic scheduler/runner. `python compileall` bersih; 18 test
`tests/test_error_classification.py` tetap lulus.

### [x] #2 Execution mode Judge = Translate
- Root cause: `JudgePacingScreen` dulu memakai `ProcessingForm` tanpa
  `advanced`, jadi grup parallel/wave/sequential + write/anti-desync/logging
  tidak pernah ditampilkan dan tidak masuk draft.
- Fix: `JudgePacingScreen.compose_body` → `ProcessingForm(advanced=True,
  draft=self.draft)`; `_accept` menyimpan `processing_mode/write_mode/
  anti_desync/skip_filled/debug_log/raw_log` ke draft (komponen yang sama
  dengan Translate, bukan implementasi kedua).

### [x] #3 Manual worker override tidak dipakai runtime
- Root cause: `JudgeRunScreen._job()` selalu `job.n_workers =
  preset.workers_for(...)` sehingga menimpa override; dan `_job()` sempat
  membaca `self.draft` padahal `JudgeRunScreen` bukan wizard (tak punya draft).
- Fix: `_confirmed` meneruskan pilihan Processing via argumen konstruktor
  `processing={...}`; `_job()` membaca `self._processing`, lalu
  `target_workers = workers or self._chosen_workers` meng-override
  `job.n_workers` **tanpa** cap 8. Diverifikasi 8→8, 10→10, 15→15.

### [x] #4 Endpoint failure lifecycle (tidak stuck)
- Root cause: hanya ada satu flag `_busy_batch`; saat `aborted` (semua koneksi
  habis via `AllConnectionsUnavailable`, retry sudah 3x di `RetryExecutor`),
  UI tetap terlihat "processing". Tidak ada state Paused / aksi Retry.
- Fix: state eksplisit `_run_state` (running/retrying/paused/completed/failed/
  cancelled). `_on_event` memetakan `retry→retrying`, `batch_done→running`,
  `aborted→paused` (+`_aborted_run`). `_evaluated` mendarat `paused` bila
  aborted, menyimpan semua row selesai. Aksi baru `R` = `retry_unfinished`
  hanya mengirim ulang row yang belum punya verdict (`_unfinished_rows`).
  Retry otomatis + batas 3x tetap milik scheduler (tidak diubah), jadi tidak
  ada spam request saat endpoint diketahui gagal.

### [x] #5 Animasi processing stuck satu frame
- Root cause: worker panel diberi `self.grid._tick`, tapi `CompareGrid.tick()`
  hanya menaikkan tick bila ada kolom `PENDING`. Kolom Judge selalu `done`,
  jadi tick=0 selamanya → spinner beku, terlihat hang.
- Fix: tick animasi mandiri `_anim_tick` yang naik tiap repaint selama
  `_busy_batch` (independen dari kedatangan row), dipakai
  `activity.set_statuses(..., self._anim_tick)`. Selesai → timer berhenti
  memajukan (`_busy_batch=False`) dan rail menampilkan Completed.

### [x] #6 Follow latest saat scroll
- Root cause: tidak ada shortcut kembali-ke-terbaru; hanya `End`.
- Fix: binding `F` = `action_follow_latest` (re-attach follow + scroll_end +
  status footer), mengikuti pola `RunScreen`. Scroll ke atas tetap men-detach
  follow tanpa memaksa turun; footer memberi tahu status follow.

### [x] #7 Elapsed + ETA
- Root cause: rail live Judge tidak punya info waktu.
- Fix: `_started_at/_elapsed` (monotonic, hanya jalan saat running),
  `_timing_line(done,total)` → "elapsed · ~left", "estimating…" saat data
  belum cukup, dan "paused" saat paused (tidak menghitung seolah aktif).
  Dipakai di review/repair/rejudge feed. Pola sama seperti `run.py`.

### [x] #8 Flicker panel
- Root cause: dua updater menulis rail di frame yang sama — timer `_repaint`
  memanggil `_paint_rail` tiap tick DAN `_log` memanggil `_paint_rail` dari
  callback worker.
- Fix: `_log` hanya memanggil `_paint_rail` saat **tidak** `_busy_batch`;
  selama batch, timer satu-satunya updater. Grid tetap pakai `batched()` +
  anchor restore (sudah ada) sehingga hanya bagian berubah yang di-rebuild.

### [x] #1 & #9 Notification / ownership model per operasi
- Root cause: `_classify_notice`, `_show_retry`, `_on_notice` selalu memakai
  `self._model_id()` (model Judge), jadi saat Fix/Rejudge pakai model lain
  status tetap menampilkan model Judge. Kartu lama juga tidak dibersihkan.
- Fix: konteks operasi `(_op_label, _op_slot)` di-set via `_begin_op()` pada
  setiap awal operasi (Judge/Fix Row/Fix All/Rejudge) dan direset ke Judge saat
  selesai; semua status membaca `_op_model_id()`. `_begin_op` juga
  `notice.dismiss()` agar kartu operasi sebelumnya tidak menempel. Notifikasi
  tetap `ProviderNotice` bersama (dock bottom-right, merge+count, auto-fade),
  tidak mengubah layout utama; raw error tetap ke events rail/log saja.

### [x] #10 Rejudge popup model + Don't ask again terpisah
- Root cause: `action_judge_again`/`action_judge_again_fixed` langsung pakai
  runner model Judge; tidak ada popup dan tidak ada preferensi Rejudge.
- Fix: `FixModelPopup` diberi parameter `title` dan checkbox jadi "Don't ask
  again this session". `_with_rejudge_model` memakai `app.judge_rejudge_slot`
  (baru, TERPISAH dari `judge_fix_slot`), popup muncul bila belum diingat.
  Kedua slot direset di `JudgeSetupScreen.on_screen_ready` (sesi/job baru minta
  lagi). Rejudge single & batch keduanya lewat popup ini.

### Catatan verifikasi
- Worker override arithmetic diuji langsung (None/8/10/15) → runtime cocok,
  processing_mode/write_mode/anti_desync/debug_log ikut dari Processing.
- Belum diuji end-to-end dengan provider nyata (perlu kunci/koneksi); jalur
  lifecycle `aborted→paused→retry` mengandalkan event scheduler yang sudah
  tercakup di `tests/test_error_classification.py`.

---

## 6. Bug Runtime Translate & Judge — Status & Root Cause

Semua dikerjakan di `scheduling/scheduler.py`, `tui/widgets/activity.py`,
`tui/screens/run.py`, `tui/screens/judge.py`, `config.py`. Retry cap 3x
(business logic `RetryExecutor`/`JobConfig.max_attempts`) TIDAK diubah. Compile
bersih; 18 test error-classification tetap lulus; helper diverifikasi terpisah.

### [x] #1 Retry loop tak berujung → pause
- Root cause: `RetryExecutor` sudah membatasi 3 attempt per request, TAPI
  `Scheduler` melanjutkan ke batch berikutnya tanpa henti. Bila kegagalan
  sistemik (provider down / PROHIBITED_CONTENT / key salah), tiap batch gagal
  lagi setelah retry-nya habis dan `batch_failed` dicetak berulang — terlihat
  seperti loop error tanpa akhir.
- Fix: circuit breaker di Scheduler. `_note_failure`/`_note_success` menghitung
  kegagalan batch beruntun; setelah `config.MAX_CONSECUTIVE_FAILURES` (3) tanpa
  satu pun sukses, run di-`_stop.set()` + emit `aborted` sekali. Judge/Run
  screen sudah memetakan `aborted→paused`, progress dipertahankan, dan tidak
  ada request baru sampai user Retry/Resume/Stop. Sukses apa pun mereset
  counter (batch flaky tunggal di antara sukses tidak memicu pause).

### [x] #2 Stop dengan `c`
- Root cause: RunScreen hanya punya `p`/Ctrl+C; Judge memakai `^C`.
- Fix: RunScreen tambah binding `c` → `action_pause` (set `_stop`, worker
  berhenti setelah batch berjalan, progress tersimpan). Judge: `c`/`C` →
  `action_c_key` yang saat batch berjalan memanggil `action_leave` (set
  `_stop`, state `cancelled`), dan saat report selesai memanggil
  `confirm & save`. Fix/Fix All/Rejudge semuanya lewat `_busy_batch`/`_stop`
  yang sama sehingga ikut berhenti.

### [x] #3 Follow latest cukup `f`
- Root cause: Judge memakai `F` (shift) untuk follow; `f` untuk fix all.
- Fix: satu binding `f` → `action_f_key`, dispatch berdasarkan state — saat
  `_busy_batch` = follow latest, saat report = fix all. `F` dihapus. Translate
  `f` sudah benar (`toggle_follow`), dipertahankan. Behavior sama di kedua
  layar: scroll atas → follow OFF, `f` → follow ON + ikut row terbaru.

### [x] #4 Flicker live preview kiri Judge
- Root cause: `_restore_anchor` saat follow memanggil `pane.scroll_end()`
  **inline** sebelum layout rebuild grid selesai; view melompat ke bottom lama
  lalu row baru menata ulang → lurch/flicker tiap result masuk.
- Fix: saat follow, scroll ditunda via `call_after_refresh(_scroll_pane_end)`
  sehingga terjadi tepat sekali setelah layout final. Grid tetap `batched()`
  (satu rebuild untuk set_columns+add_rows+set_notes) dan merge in-place, bukan
  recreate. Dikombinasi dengan #8 (`_log` tak memicu paint kedua saat batch).

### [x] #5 `Success` Translate terlalu cepat & #6 timer lompat ke 0 & #7 Retrying tak terlihat
- Root cause bersama: worker paralel langsung ambil batch berikutnya, jadi
  `WorkerStatus` `done`/`failed`/`retry` ditimpa `generating` dalam interval
  repaint yang sama; ActivityPanel hanya menggambar snapshot terakhir sehingga
  frame transisi hilang → "Success" berkedip, timer 11s→0s tanpa perantara,
  retry tak terlihat.
- Fix: `WorkerLedger` (UI-only) di `activity.py`. `on_status` memanggil
  `ledger.note()` untuk MENGAMATI tiap transisi (termasuk yang di-throttle),
  dan menahan state `done`/`failed`/`retry` ~1 detik (`HOLD_SECONDS=1.0`)
  meski worker sudah lanjut. `statuses()` menyuplai panel. Elapsed request yang
  baru selesai dibawa ke snapshot `done` (bukan 0.0s). Label `done` diubah jadi
  "success". **Engine tidak ditahan** — tidak ada sleep, batch berikut jalan
  langsung; hanya tampilan yang dipertahankan. Dipakai Translate & Judge
  (universal untuk semua worker: Translate/Judge/Fix/Rejudge).

### [x] #8 Error memenuhi UI
- Root cause: `_log` (run.py & judge.py) meng-append tiap pesan; error
  PROHIBITED_CONTENT identik dari tiap request memenuhi events rail.
- Fix: `_log` menggabung pesan identik berturut-turut jadi satu baris
  ber-counter (`… ×N`) alih-alih menumpuk. Raw error tetap masuk log/Activity;
  `ProviderNotice` (kartu ringkas merge+count) tetap menangani panel utama.
  Diverifikasi: 3 pesan sama → 1 baris `×3`.

### Catatan verifikasi tambahan
- Circuit breaker diuji terpisah: 2 gagal = jalan, 3 gagal beruntun = aborted +
  stop, sukses mereset counter.
- WorkerLedger diuji: `done` tetap tampil setelah langsung `generating`, hilang
  setelah hold, `retry` juga ditahan, label "success".
- Dedup `_log` diuji untuk Translate & Judge.
- Cap retry 3x tetap di `RetryExecutor.execute` (`attempt > job.max_attempts`),
  tidak diubah — pause terjadi di atasnya (level scheduler antar-batch).
