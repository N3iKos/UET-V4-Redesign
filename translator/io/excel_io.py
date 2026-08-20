"""
io/excel_io.py — Excel read/write layer.

Ported from v3.6's excel_io.py near as-is; this module has no provider
coupling at all. The single change: the write thread's "file is locked,
retrying" messages used to call ui.log_warn/log_ok directly. In v4 they go
through an optional callback so the module prints nothing (04_TUI_SPEC §3).

ExcelWriter behaviour is unchanged:
  safe     — flush() enqueues immediately, background thread owns the disk
  buffered — accumulate buffer_size batches, then enqueue one job
Both write atomically (.tmp then os.replace) and checkpoint every
CHECKPOINT_ROWS rows.
"""

from __future__ import annotations

import glob
import os
import queue
import re
import shutil
import threading
import time
from typing import Callable

from openpyxl import load_workbook as _load_xlsx
from openpyxl.utils import column_index_from_string, get_column_letter

from .. import config
from . import csv_io


def load_workbook(filepath: str, data_only: bool = True, read_only: bool = False):
    """
    Open a spreadsheet regardless of format.

    CSV files come back wrapped in a workbook-shaped adapter, so every caller
    keeps using the same 1-based cell/iter_rows idiom and no code above this
    layer needs to know which format it is dealing with.
    """
    if csv_io.is_csv(filepath):
        return csv_io.load_workbook(filepath)
    return _load_xlsx(filepath, data_only=data_only, read_only=read_only)

CHECKPOINT_ROWS = config.CHECKPOINT_ROWS

# Lock retry ladder. A virus scanner or sync client holding the freshly
# written .tmp clears in ~1ms, so start there and grow; only a genuinely
# open workbook survives long enough to be worth telling the user about.
LOCK_RETRY_MIN_DELAY = 0.05
LOCK_RETRY_MAX_DELAY = 2.0
LOCK_WARN_AFTER = 3.0

# How long to keep trying the real target before escaping to a numbered sibling
# (see config for the rationale). Kept in config so it sits beside the range the
# user actually tunes.
LOCK_MAX_RETRY_BEFORE_FALLBACK = config.LOCK_MAX_RETRY_BEFORE_FALLBACK

# Windows raises "The process cannot access the file because it is being used by
# another process" as WinError 32 (errno EACCES=13) and a sharing violation as
# WinError 33. PermissionError covers the common case cross-platform; the
# winerror check catches the sharing-violation flavour that arrives as a plain
# OSError. Anything else (a bad path, a full disk) is *not* a lock and must not
# be reported as one.
_LOCK_WINERRORS = {32, 33}


def _is_lock_error(exc: Exception) -> bool:
    """True when `exc` is a file-in-use lock rather than a different I/O fault."""
    if isinstance(exc, PermissionError):
        return True
    winerror = getattr(exc, "winerror", None)
    if winerror in _LOCK_WINERRORS:
        return True
    return False

# ("locked", message) while a write is blocked, ("recovered", message) after.
WriterEvent = Callable[[str, str], None]


# ── Reader ────────────────────────────────────────────────────────────────────

# (row, column) counts keyed by path+mtime+size. Opening a workbook just to ask
# "how many rows?" costs ~400ms on a 3.7k-row sheet, and the wizard asks on
# every keystroke; the file cannot change under us mid-keystroke.
_DIM_CACHE: dict[str, tuple[float, int, int, int]] = {}


def _dimensions(filepath: str) -> tuple[int, int]:
    """(max_row, max_column), cached against the file's mtime and size."""
    try:
        stat = os.stat(filepath)
    except OSError:
        return 0, 0

    cached = _DIM_CACHE.get(filepath)
    if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2], cached[3]

    wb = load_workbook(filepath, data_only=True, read_only=True)
    try:
        ws = wb.active
        rows, cols = ws.max_row or 0, ws.max_column or 0
    finally:
        wb.close()

    _DIM_CACHE[filepath] = (stat.st_mtime, stat.st_size, rows, cols)
    return rows, cols


def _stream_column(
    filepath: str, col_idx: int, first_row: int, last_row: int
) -> dict[int, str]:
    """
    {row: text} for one column over [first_row, last_row], skipping blanks.
    read_only streams the sheet XML instead of building every Cell object.
    """
    if not os.path.exists(filepath) or last_row < first_row:
        return {}

    total_rows, _ = _dimensions(filepath)
    last = min(last_row, total_rows)
    if last < first_row:
        return {}

    out: dict[int, str] = {}
    wb = load_workbook(filepath, data_only=True, read_only=True)
    try:
        ws = wb.active
        for offset, values in enumerate(
            ws.iter_rows(min_row=first_row, max_row=last,
                         min_col=col_idx, max_col=col_idx, values_only=True)
        ):
            val = values[0]
            if val is not None:
                text = str(val).strip()
                if text:
                    out[first_row + offset] = text
    finally:
        wb.close()
    return out

class ExcelReader:
    """Stateless read helpers. Each method opens/closes the workbook."""

    @staticmethod
    def load_column(
        filepath: str, col_idx: int, start_row: int, num_rows: int
    ) -> list[tuple[int, str]]:
        """
        One column as (row, text). Streams a single column rather than
        materialising every cell — the whole-workbook load costs seconds on
        a 30k-row sheet and this runs before the first batch is even sent.
        Rows past the end of the sheet come back as "", as callers expect.
        """
        end = start_row + num_rows - 1
        values = _stream_column(filepath, col_idx, start_row, end)
        return [(r, values.get(r, "")) for r in range(start_row, end + 1)]

    @staticmethod
    def load_rows_at_offsets(
        filepath: str, col_idx: int, start_row: int, offsets: list[int]
    ) -> list[tuple[int, str]]:
        wb = load_workbook(filepath, data_only=True)
        ws = wb.active
        out: list[tuple[int, str]] = []
        for off in offsets:
            r = start_row + off
            val = ws.cell(row=r, column=col_idx).value
            out.append((r, str(val).strip() if val is not None else ""))
        wb.close()
        return out

    @staticmethod
    def filled_rows(filepath: str, col_idx: int, start_row: int, num_rows: int) -> set[int]:
        end = start_row + num_rows - 1
        return {
            r for r, val in _stream_column(filepath, col_idx, start_row, end).items()
            if val
        }

    @staticmethod
    def count_filled(filepath: str, col_idx: int, start_row: int, num_rows: int) -> int:
        return len(ExcelReader.filled_rows(filepath, col_idx, start_row, num_rows))

    @staticmethod
    def row_count(filepath: str) -> int:
        return _dimensions(filepath)[0]

    @staticmethod
    def preview(
        filepath: str, col_labels: list[tuple[int, str]], start_row: int, n: int = 3
    ) -> list[dict]:
        wb = load_workbook(filepath, data_only=True)
        ws = wb.active
        rows: list[dict] = []
        for r in range(start_row, start_row + n):
            row_data: dict = {"_row": r}
            for col_idx, label in col_labels:
                val = ws.cell(row=r, column=col_idx).value
                row_data[label] = str(val).strip() if val is not None else "(empty)"
            rows.append(row_data)
        wb.close()
        return rows

    @staticmethod
    def grid(
        filepath: str, start_row: int, n_rows: int = 4, max_cols: int = 12
    ) -> tuple[list[str], list[str], list[list[str]]]:
        """
        A spreadsheet-shaped look at the file, for picking columns by eye:
        returns (column letters, header row 1, sample rows starting at start_row).

        This runs on every keystroke in the wizard, so it must never load the
        whole workbook: read_only streams, and we stop at the last row we need.
        """
        total_rows, total_cols = _dimensions(filepath)
        cols = min(total_cols or 1, max_cols)
        letters = [get_column_letter(c) for c in range(1, cols + 1)]

        def clean(val) -> str:
            return str(val).strip().replace("\n", " ") if val is not None else ""

        last = min(start_row + n_rows - 1, total_rows) if total_rows else 0
        headers: list[str] = [""] * cols
        rows: list[list[str]] = []
        if last < 1:                       # missing, empty, or start past EOF
            return letters, headers, rows

        wb = load_workbook(filepath, data_only=True, read_only=True)
        try:
            ws = wb.active
            for index, values in enumerate(
                ws.iter_rows(min_row=1, max_row=last, max_col=cols,
                             values_only=True),
                start=1,
            ):
                if index == 1:
                    headers = [clean(v) for v in values] + [""] * (
                        cols - len(values)
                    )
                if index >= start_row:
                    row = [clean(v) for v in values]
                    rows.append(row + [""] * (cols - len(row)))
        finally:
            wb.close()
        return letters, headers, rows

    @staticmethod
    def scan_directory(directory: str = ".") -> list[str]:
        files: list[str] = []
        for pattern in ("*.xlsx", "*.xls", "*.csv", "*.tsv"):
            files += glob.glob(os.path.join(directory, pattern))
        return sorted(os.path.basename(f) for f in files)

    @staticmethod
    def parse_cell_ref(ref: str) -> tuple[int | None, int | None]:
        """'D15217' -> (4, 15217); 'D' -> (4, None); anything else -> (None, None)."""
        match = re.match(r"^([A-Za-z]{1,3})(\d*)$", ref.strip())
        if not match:
            return None, None
        try:
            col = column_index_from_string(match.group(1).upper())
            row_str = match.group(2)
            return col, (int(row_str) if row_str else None)
        except Exception:
            return None, None

    @staticmethod
    def find_first_empty_row(filepath: str, col_idx: int, start_row: int = 2) -> int:
        if not os.path.exists(filepath):
            return start_row
        try:
            wb = load_workbook(filepath, data_only=True)
            ws = wb.active
            curr = start_row
            while True:
                val = ws.cell(row=curr, column=col_idx).value
                if val is None or not str(val).strip():
                    break
                curr += 1
            wb.close()
            return curr
        except Exception:
            return start_row

    @staticmethod
    def find_first_failed_row(
        filepath: str, col_idx: int, start_row: int = 2
    ) -> tuple[int, str | None]:
        """Returns (row, issue) where issue is TRANSLATE_FAILED/empty/too_short/None."""
        if not os.path.exists(filepath):
            return start_row, None
        try:
            wb = load_workbook(filepath, data_only=True)
            ws = wb.active
            max_r = ws.max_row or start_row
            for r in range(start_row, max_r + 1):
                val = ws.cell(row=r, column=col_idx).value
                if val is None or not str(val).strip():
                    wb.close()
                    return r, "empty"
                text = str(val).strip()
                if text == config.FAIL_MARKER:
                    wb.close()
                    return r, "TRANSLATE_FAILED"
                if len(text) < 3:
                    wb.close()
                    return r, "too_short"
            wb.close()
            return start_row, None
        except Exception:
            return start_row, None

    @staticmethod
    def col_letter(col_idx: int) -> str:
        return get_column_letter(col_idx)


# ── Output file ───────────────────────────────────────────────────────────────

def translated_path(src_path: str) -> str:
    """`terms.xlsx` -> `terms_Translated.xlsx`, same folder and extension."""
    stem, ext = os.path.splitext(src_path)
    if stem.endswith(config.TRANSLATED_SUFFIX):
        return src_path            # already an output file; keep working on it
    return f"{stem}{config.TRANSLATED_SUFFIX}{ext}"


def prepare_output(src_path: str, out_path: str) -> tuple[bool, int]:
    """
    Make sure `out_path` exists and is ready to be written into.

    The source file is never modified — the output starts as a byte-for-byte
    copy, so every untouched column survives exactly as it was. If the output
    already exists it is left alone, which is what makes resume work: filled
    rows are detected there and skipped.

    Returns (resumed, existing_row_count).
    """
    if os.path.abspath(src_path) == os.path.abspath(out_path):
        return False, 0
    if os.path.exists(out_path):
        return True, row_count_of(out_path)
    shutil.copy2(src_path, out_path)
    _DIM_CACHE.pop(out_path, None)
    return False, 0


def row_count_of(path: str) -> int:
    _DIM_CACHE.pop(path, None)
    return ExcelReader.row_count(path)


# ── Async write queue ─────────────────────────────────────────────────────────

class _WriteJob:
    __slots__ = ("writes",)

    def __init__(self, writes: dict[int, dict[int, str]]) -> None:
        self.writes = writes


_SENTINEL = object()


class _FlushBarrier:
    """Forces a disk flush and signals when it has actually landed."""
    __slots__ = ("done",)

    def __init__(self) -> None:
        self.done = threading.Event()


class _AsyncWriteThread(threading.Thread):
    """
    Background thread that owns every disk write.

    Workers only enqueue; this thread accumulates {col: {row: text}} and
    flushes when CHECKPOINT_ROWS is reached, on idle, and on shutdown.
    """

    def __init__(
        self,
        filepath: str,
        on_event: WriterEvent | None = None,
        fallback_range: int = 0,
    ) -> None:
        super().__init__(daemon=True, name="excel-writer")
        self._filepath = filepath
        # The file this thread actually writes to. It starts as the requested
        # target and is redirected to a numbered sibling the first time the real
        # target turns out to be locked — after which every later checkpoint
        # lands in the sibling too, so one run never fragments across files.
        self._active_path = filepath
        self._on_event = on_event
        # How many numbered siblings we may fall back to (0 = retry forever).
        self._fallback_range = max(0, int(fallback_range))
        self._queue: queue.Queue = queue.Queue()
        self._pending: dict[int, dict[int, str]] = {}
        self._pending_rows = 0
        self._error: Exception | None = None

    @property
    def active_path(self) -> str:
        """Where results are actually being written (may be a fallback sibling)."""
        return self._active_path

    def enqueue(self, writes: dict[int, dict[int, str]]) -> None:
        self._queue.put(_WriteJob(writes))

    def sync(self, timeout: float | None = None) -> bool:
        """Block until everything queued so far is on disk."""
        barrier = _FlushBarrier()
        self._queue.put(barrier)
        return barrier.done.wait(timeout=timeout)

    def stop_and_join(self) -> None:
        self._queue.put(_SENTINEL)
        self.join()
        if self._error:
            raise self._error

    def run(self) -> None:
        try:
            while True:
                try:
                    item = self._queue.get(timeout=0.5)
                except queue.Empty:
                    if self._pending_rows > 0:
                        self._flush_to_disk()
                    continue

                if item is _SENTINEL:
                    if self._pending_rows > 0:
                        self._flush_to_disk()
                    break

                if isinstance(item, _FlushBarrier):
                    if self._pending_rows > 0:
                        self._flush_to_disk()
                    item.done.set()
                    continue

                job: _WriteJob = item
                for col_idx, results in job.writes.items():
                    self._pending.setdefault(col_idx, {}).update(results)
                    self._pending_rows += len(results)

                if self._pending_rows >= CHECKPOINT_ROWS:
                    self._flush_to_disk()
        except Exception as exc:
            self._error = exc

    def _flush_to_disk(self) -> None:
        """
        Atomic write: load -> patch -> save .tmp -> replace.

        Never drops the pending rows — letting this thread die would silently
        lose every row written since the last checkpoint. Failures are handled
        by kind:

          * transient lock — an antivirus/indexer/sync client opening the
            freshly written .tmp for a scan. Clears in milliseconds, so back off
            fast and stay quiet.
          * real lock — the workbook is open in Excel. Waited on the real target
            for LOCK_MAX_RETRY_BEFORE_FALLBACK seconds, then (if the user allowed
            a fallback range) redirected to a numbered sibling so the run is not
            held hostage by a spreadsheet someone forgot to close.
          * anything else — a bad path, a full disk, a permission the file will
            never gain by waiting. Reported as what it actually is rather than
            blamed on an open file, so the user is not told to "close Excel" for
            a problem closing Excel cannot fix.
        """
        if not self._pending:
            return

        started = time.monotonic()
        warned = False
        fell_back = False
        attempt = 0
        delay = LOCK_RETRY_MIN_DELAY

        while True:
            try:
                self._write_pending_to(self._active_path)
                if warned or fell_back:
                    self._emit(
                        "recovered",
                        f"Write successful to "
                        f"{os.path.basename(self._active_path)}"
                        + (f" after {time.monotonic() - started:.0f}s."
                           if warned else "."),
                    )
                break
            except Exception as exc:
                attempt += 1
                waited = time.monotonic() - started
                locked = _is_lock_error(exc)

                # Once the target has been unavailable longer than any
                # background scanner would hold it, escape to a numbered sibling
                # rather than wait on it forever. Enabled for any persistent
                # failure, not just a lock: a fresh path is a reasonable way out
                # of a per-file permission problem too.
                if (self._fallback_range > 0
                        and waited >= LOCK_MAX_RETRY_BEFORE_FALLBACK):
                    sibling = self._redirect_to_sibling()
                    if sibling is not None:
                        old = os.path.basename(self._active_path)
                        self._active_path = sibling
                        fell_back = True
                        self._emit(
                            "warn",
                            f"Could not write to {old} — saved to "
                            f"{os.path.basename(sibling)} instead. The run "
                            f"continues there; nothing is lost.",
                        )
                        return
                    # No sibling could be written either (e.g. a full disk or a
                    # locked folder): fall through and keep trying the target.

                if locked:
                    if not warned and waited >= LOCK_WARN_AFTER:
                        warned = True
                        self._emit(
                            "locked",
                            f"Cannot write to "
                            f"{os.path.basename(self._active_path)} — the file "
                            f"looks open in another program. Close it and the "
                            f"run continues by itself (nothing is lost): {exc}",
                        )
                    elif warned and attempt % 30 == 0:
                        self._emit(
                            "locked",
                            f"Still waiting for "
                            f"{os.path.basename(self._active_path)} to be closed "
                            f"({waited:.0f}s so far) — nothing is lost.",
                        )
                else:
                    # Not a lock, so "close the file" would be misleading. Say
                    # what actually went wrong, once and then periodically.
                    if attempt == 1 or attempt % 30 == 0:
                        self._emit(
                            "error",
                            f"Write to "
                            f"{os.path.basename(self._active_path)} failed "
                            f"({type(exc).__name__}): {exc}. Retrying — nothing "
                            f"is lost.",
                        )

                time.sleep(delay)
                delay = min(delay * 2, LOCK_RETRY_MAX_DELAY)

        self._pending.clear()
        self._pending_rows = 0

    def _write_pending_to(self, path: str, base_path: str | None = None) -> None:
        """
        Patch the pending cells into `path` atomically (.tmp then os.replace).

        `base_path`, when given, is the workbook to read the existing rows from —
        used when redirecting to a brand-new sibling that does not exist yet, so
        it inherits every row already written to the original before the lock.
        Raises on any failure; the caller decides whether to retry or redirect.
        """
        read_from = base_path if base_path is not None else path
        tmp = f"{path}.{os.getpid()}.tmp"
        try:
            wb = load_workbook(read_from)
            ws = wb.active
            for col_idx, results in self._pending.items():
                for row_num, text in results.items():
                    ws.cell(row=row_num, column=col_idx, value=text)
            wb.save(tmp)
            wb.close()
            os.replace(tmp, path)
        except Exception:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            raise

    def _redirect_to_sibling(self) -> str | None:
        """
        Write the pending rows to the first numbered sibling we can, seeding it
        from the original target so no earlier row is lost.

        Numbering is anchored to the *original* filepath (foo_1, foo_2, …), not
        the current active path, so a run that has already fallen back to foo_1
        escalates to foo_2 rather than foo_1_1. Returns the sibling path on
        success, or None if every candidate in range was itself unwritable.
        """
        stem, ext = os.path.splitext(self._filepath)
        # Seed from whatever holds the most rows we have landed so far: the
        # current active file if it exists, else the original target.
        base = self._active_path if os.path.exists(self._active_path) else self._filepath
        for n in range(1, self._fallback_range + 1):
            candidate = f"{stem}_{n}{ext}"
            if os.path.abspath(candidate) == os.path.abspath(self._active_path):
                continue
            try:
                seed = base if not os.path.exists(candidate) else None
                self._write_pending_to(candidate, base_path=seed)
                return candidate
            except Exception:
                continue
        return None

    def _emit(self, kind: str, message: str) -> None:
        if self._on_event is not None:
            self._on_event(kind, message)


# ── Writer ────────────────────────────────────────────────────────────────────

class ExcelWriter:
    """Thread-safe writer backed by the async write thread. flush() is O(1)."""

    def __init__(
        self,
        filepath: str,
        mode: str = "safe",
        buffer_size: int = 5,
        on_event: WriterEvent | None = None,
        fallback_range: int = 0,
    ) -> None:
        self._filepath = filepath
        self._mode = mode
        self._buffer_size = buffer_size
        self._lock = threading.Lock()
        self._writer = _AsyncWriteThread(
            filepath, on_event, fallback_range=fallback_range
        )
        self._writer.start()
        self._buffer: dict[int, dict[int, str]] = {}
        self._batch_count = 0

    @property
    def active_path(self) -> str:
        """
        The file results are actually landing in.

        Equals the requested path until the target turned out to be locked and
        the writer fell back to a numbered sibling; a re-read pass (auto-sweep,
        repair) must read from here, not from the original name, or it would
        rescan a file the run has stopped writing to.
        """
        return self._writer.active_path

    def flush(self, results: dict[int, str], col_idx: int) -> None:
        if not results:
            return
        if self._mode == "safe":
            self._writer.enqueue({col_idx: results})
            return
        with self._lock:
            self._buffer.setdefault(col_idx, {}).update(results)
            self._batch_count += 1
            if self._batch_count >= self._buffer_size:
                self._writer.enqueue(dict(self._buffer))
                self._buffer.clear()
                self._batch_count = 0

    def flush_multi(self, writes: dict[int, dict[int, str]]) -> None:
        if not writes:
            return
        if self._mode == "safe":
            self._writer.enqueue(writes)
            return
        with self._lock:
            for col_idx, results in writes.items():
                self._buffer.setdefault(col_idx, {}).update(results)
            self._batch_count += 1
            if self._batch_count >= self._buffer_size:
                self._writer.enqueue(dict(self._buffer))
                self._buffer.clear()
                self._batch_count = 0

    def sync(self, timeout: float | None = None) -> bool:
        """
        Wait until everything written so far has landed on disk, without
        shutting the writer down. Required before any pass that re-reads the
        output file (auto-sweep, repair phase) — otherwise it reads stale cells.
        """
        with self._lock:
            if self._buffer:
                self._writer.enqueue(dict(self._buffer))
                self._buffer.clear()
                self._batch_count = 0
        return self._writer.sync(timeout=timeout)

    def commit(self) -> None:
        """Flush everything buffered and wait for the write thread to finish."""
        with self._lock:
            if self._buffer:
                self._writer.enqueue(dict(self._buffer))
                self._buffer.clear()
                self._batch_count = 0
        self._writer.stop_and_join()

    def col_letter(self, col_idx: int) -> str:
        return get_column_letter(col_idx)
