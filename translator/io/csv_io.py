"""
io/csv_io.py — CSV support, presented to the rest of the app as a workbook.

A CSV has no cells, no sheets and no types, but every caller in v4 already
speaks the openpyxl idiom (1-based row/column, `ws.cell(...).value`). Rather
than teach the engine, wizard and reader about a second file format, this
module adapts CSV to that idiom:

    load_workbook(path) -> _CsvWorkbook -> .active -> _CsvSheet

so `excel_io` can dispatch on the extension and nothing above it changes.

Round-trip fidelity is the whole point — a translated CSV must differ from
the original in exactly the cells we wrote and nowhere else. The dialect
(delimiter, quote char, line terminator), the encoding and any BOM are all
sniffed on read and reproduced on write.
"""

from __future__ import annotations

import codecs
import csv
import os
from typing import Iterator

# Encodings to try, in order. utf-8-sig strips a BOM if present and is the
# common case for spreadsheets exported on Windows.
_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")


def is_csv(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in (".csv", ".tsv", ".txt")


def _decode_whole_file(raw: bytes) -> tuple[str, str] | None:
    """
    Strict-decode the WHOLE file as one UTF-8 family encoding.

    The 64 KB sniff window can only prove a file is not UTF-8 by failing, and
    a partial multi-byte character at the window's edge made strict decodes
    fail on files that were perfectly fine. Worse, cp1252 accepts nearly
    every byte sequence, so it "wins" far too easily — and once a real UTF-8
    file is read as cp1252 and written back, the mojibake
    (\u2026 becomes "â¦", — becomes "Ã¢Â\x80Â\x94") is baked into the file and
    every later pass deepens it. Decoding the whole file removes both failure
    modes: there is no window edge, and UTF-8 wins whenever the entire file
    genuinely is UTF-8 — which for this app's files is the overwhelmingly
    common case.

    Returns (text, encoding) or None when no UTF-8 family decode succeeds.
    """
    for candidate in ("utf-8-sig", "utf-8"):
        try:
            text = raw.decode(candidate, errors="strict")
            return text, "utf-8" if candidate == "utf-8-sig" else "utf-8"
        except UnicodeDecodeError:
            continue
    return None


class CsvFormat:
    """Everything needed to rewrite a file the way we found it."""

    __slots__ = ("encoding", "delimiter", "quotechar", "lineterminator", "bom")

    def __init__(
        self,
        encoding: str = "utf-8",
        delimiter: str = ",",
        quotechar: str = '"',
        lineterminator: str = "\r\n",
        bom: bool = False,
    ) -> None:
        self.encoding = encoding
        self.delimiter = delimiter
        self.quotechar = quotechar
        self.lineterminator = lineterminator
        self.bom = bom

    def writer_kwargs(self) -> dict:
        return {
            "delimiter": self.delimiter,
            "quotechar": self.quotechar,
            "lineterminator": self.lineterminator,
            "quoting": csv.QUOTE_MINIMAL,
        }


def sniff(path: str) -> CsvFormat:
    """
    Detect encoding, delimiter and line ending from the head of the file.

    Falls back to sane defaults rather than raising: a file we cannot sniff
    is still better opened as plain comma-separated UTF-8 than refused.
    """
    raw = b""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError:
        return CsvFormat()

    bom = raw.startswith(b"\xef\xbb\xbf")

    # Whole-file decode: cp1252 accepts nearly every byte, so a window-based
    # sniff let it "win" over UTF-8 far too easily (a dangling sequence at the
    # window edge, or one bad byte) — and one cp1252 read + UTF-8 write of a
    # UTF-8 file bakes mojibake in permanently. UTF-8 now wins whenever the
    # ENTIRE file is valid UTF-8, which is the overwhelmingly common case for
    # this app's files. Only a file with genuinely non-UTF-8 bytes falls
    # through to the legacy single-byte encodings.
    decoded = _decode_whole_file(raw)
    if decoded is not None:
        text, encoding = decoded
    else:
        text = ""
        encoding = "utf-8"
        for candidate in _ENCODINGS:
            try:
                decoder = codecs.getincrementaldecoder(candidate)("strict")
                text = decoder.decode(raw, False)
                encoding = "utf-8" if candidate == "utf-8-sig" else candidate
                break
            except UnicodeDecodeError:
                continue

    # Line terminator: look at the first break we see, before any parsing.
    lineterminator = "\r\n" if "\r\n" in text else ("\n" if "\n" in text else "\r\n")

    # Delimiter: prefer the sniffer, but fall back to counting candidates on
    # the header line — the sniffer throws on single-column files.
    delimiter = ","
    quotechar = '"'
    sample = text[:16384]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
        quotechar = dialect.quotechar or '"'
        # csv.Sniffer notoriously misidentifies apostrophes in English text/contractions
        # (e.g. You've, don't, it's, tag '<...>') as quotechar="'".
        # Standard CSV (RFC 4180) and spreadsheet software always use double quotes '"'.
        if quotechar != '"':
            quotechar = '"'
    except csv.Error:
        header = sample.splitlines()[0] if sample.splitlines() else ""
        counts = {d: header.count(d) for d in (",", ";", "\t", "|")}
        best = max(counts, key=counts.get)
        if counts[best] > 0:
            delimiter = best
        elif os.path.splitext(path)[1].lower() == ".tsv":
            delimiter = "\t"

    return CsvFormat(encoding, delimiter, quotechar, lineterminator, bom)


def read_rows(path: str, fmt: CsvFormat | None = None) -> list[list[str]]:
    """Whole file as a rectangular-ish list of rows (ragged rows preserved)."""
    fmt = fmt or sniff(path)
    encoding = "utf-8-sig" if fmt.bom and fmt.encoding == "utf-8" else fmt.encoding
    with open(path, "r", encoding=encoding, newline="") as handle:
        reader = csv.reader(handle, delimiter=fmt.delimiter, quotechar=fmt.quotechar)
        return [row for row in reader]


def iter_rows(path: str, fmt: CsvFormat | None = None) -> Iterator[list[str]]:
    """Streaming read — used for the wizard preview and column loads."""
    fmt = fmt or sniff(path)
    encoding = "utf-8-sig" if fmt.bom and fmt.encoding == "utf-8" else fmt.encoding
    with open(path, "r", encoding=encoding, newline="") as handle:
        reader = csv.reader(handle, delimiter=fmt.delimiter, quotechar=fmt.quotechar)
        for row in reader:
            yield row


def repair_mojibake(text: str) -> str:
    """
    Undo the cp1252-misread of UTF-8, one layer deep.

    A UTF-8 file read as cp1252 and written back as UTF-8 turns
    "\u2026" into "â¦" and "\\u2014" into "Ã¢\\x80\\x94". Re-encoding the
    damaged text as cp1252 (latin-1 for the bytes cp1252 cannot produce)
    recovers the original bytes, which then decode cleanly as UTF-8.
    Only applied when the round trip is lossless and the result actually
    decodes — text that was never mojibake is returned untouched, and a
    mix (one pass already damaged, one clean) is repaired where it can be
    and left alone where it cannot.
    """
    if not text or all(ord(ch) < 128 for ch in text):
        return text          # pure ASCII: nothing to repair, nothing to break
    try:
        raw = text.encode("cp1252")
    except UnicodeEncodeError:
        try:
            raw = text.encode("latin-1")
        except UnicodeEncodeError:
            return text      # characters outside both: not this damage pattern
    try:
        repaired = raw.decode("utf-8")
    except UnicodeDecodeError:
        return text          # the bytes are not valid UTF-8: original stands
    return repaired


def looks_mojibake(text: str) -> bool:
    """
    Quick heuristic: does this text carry the byte-shaped scars of a
    cp1252 misread of UTF-8? Used to decide whether a repair pass is worth
    running on a whole file rather than per cell.
    """
    markers = ("Ã¢", "Ã¢Â", "â€", "Â ", "Â·", "Â—", "â€™", "â€œ", "â€\x9d")
    return any(marker in text for marker in markers)


def write_rows(path: str, rows: list[list[str]], fmt: CsvFormat) -> None:
    """
    Rewrite the file, preserving the sniffed dialect and encoding.

    Round-trip fidelity is the goal, but *correctness* outranks it: a file read
    as latin-1 or cp1252 cannot necessarily encode the translation written back
    into it — a Japanese source rendered into Indonesian, or any text carrying a
    character outside the source's byte range, would raise UnicodeEncodeError and
    (before this) wedge the writer into an unrecoverable retry loop.

    So the sniffed encoding is tried first, and on a genuine encode failure the
    output falls back to UTF-8, which represents every character the model can
    produce. UTF-8 is a superset of ASCII, so a plain file is byte-identical
    either way; only a file that actually needed the wider encoding changes, and
    changing it is the whole point.
    """
    primary = "utf-8-sig" if fmt.bom and fmt.encoding == "utf-8" else fmt.encoding
    try:
        _write_rows_encoded(path, rows, fmt, primary)
    except UnicodeEncodeError:
        if primary in ("utf-8", "utf-8-sig"):
            raise
        # The source encoding cannot hold the translation. Keep a BOM only if the
        # file had one, so a Windows-exported file still opens cleanly in Excel.
        fallback = "utf-8-sig" if fmt.bom else "utf-8"
        _write_rows_encoded(path, rows, fmt, fallback)


def _write_rows_encoded(
    path: str, rows: list[list[str]], fmt: CsvFormat, encoding: str
) -> None:
    with open(path, "w", encoding=encoding, newline="") as handle:
        writer = csv.writer(handle, **fmt.writer_kwargs())
        writer.writerows(rows)


# ── openpyxl-shaped adapter ───────────────────────────────────────────────────

class _Cell:
    __slots__ = ("value",)

    def __init__(self, value) -> None:
        self.value = value


class _CsvSheet:
    """
    The openpyxl worksheet surface that v4 actually uses: `cell()`,
    `iter_rows()`, `max_row`, `max_column`. 1-based indexing throughout.
    """

    def __init__(self, rows: list[list[str]]) -> None:
        self._rows = rows

    @property
    def max_row(self) -> int:
        return len(self._rows)

    @property
    def max_column(self) -> int:
        return max((len(r) for r in self._rows), default=0)

    def cell(self, row: int, column: int, value=None):
        if value is not None:
            while len(self._rows) < row:
                self._rows.append([])
            target = self._rows[row - 1]
            while len(target) < column:
                target.append("")
            target[column - 1] = value
            return _Cell(value)

        if 1 <= row <= len(self._rows):
            line = self._rows[row - 1]
            if 1 <= column <= len(line):
                raw = line[column - 1]
                return _Cell(raw if raw != "" else None)
        return _Cell(None)

    def iter_rows(
        self,
        min_row: int = 1,
        max_row: int | None = None,
        min_col: int = 1,
        max_col: int | None = None,
        values_only: bool = False,
    ):
        last = min(max_row or self.max_row, self.max_row)
        width = max_col or self.max_column
        for index in range(min_row, last + 1):
            line = self._rows[index - 1]
            chunk = [
                (line[c - 1] if c - 1 < len(line) and line[c - 1] != "" else None)
                for c in range(min_col, width + 1)
            ]
            yield tuple(chunk) if values_only else tuple(_Cell(v) for v in chunk)


class _CsvWorkbook:
    """Stands in for an openpyxl Workbook over a CSV file."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.format = sniff(path)
        self.active = _CsvSheet(read_rows(path, self.format))

    def save(self, path: str) -> None:
        write_rows(path, self.active._rows, self.format)

    def close(self) -> None:
        self.active = None


def load_workbook(filepath: str, data_only: bool = True, read_only: bool = False):
    """Signature-compatible with openpyxl's, so excel_io can swap either in."""
    return _CsvWorkbook(filepath)
