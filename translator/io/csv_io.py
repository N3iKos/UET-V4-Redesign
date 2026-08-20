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

# csv caps fields at 128 KB by default; translation rows can exceed that.
csv.field_size_limit(10 * 1024 * 1024)


def is_csv(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in (".csv", ".tsv", ".txt")


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
            raw = handle.read(64 * 1024)
    except OSError:
        return CsvFormat()

    bom = raw.startswith(b"\xef\xbb\xbf")

    text = ""
    encoding = "utf-8"
    for candidate in _ENCODINGS:
        try:
            # Decode the window incrementally with final=False. The window is a
            # fixed 64 KB slice of the file, so a multi-byte character can be cut
            # in half at its edge — a real UTF-8 file then fails a *strict*
            # decode purely because of where we happened to stop reading, and
            # the sniff falls through to latin-1, which decodes any byte and so
            # never fails. That misread is what turns 日本語 into "æ¥æ¬èª":
            # the file is UTF-8, gets read as latin-1, and every following write
            # re-encodes the mojibake. Tolerating a dangling trailing sequence
            # keeps UTF-8 from losing to latin-1 on that technicality.
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
