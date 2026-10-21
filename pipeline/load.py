"""Strict CSV loading: header contract, typed parsing, input hash, source lines."""

import csv
import hashlib
import io
import re
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import BinaryIO

import pandas as pd

from contracts.config import InputConfig
from contracts.enums import DIMENSION_COLUMNS, REQUIRED_COLUMNS, InputColumn
from contracts.runtime import FileError, LoadedData, ParseAudit, PrecisionMetadata

CHUNK_BYTES = 1 << 20
NUMERIC_COLUMNS: tuple[InputColumn, ...] = REQUIRED_COLUMNS[6:]
# Plain decimal notation only: no exponents, separators, NaN or infinity.
_NUMBER = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)$")


def load_csv(source: BinaryIO, config: InputConfig) -> LoadedData:
    """Hash the raw bytes, then parse the 17-column export without coercion.

    Supply ID is read as an opaque string; dates parse strictly; invalid
    numerics are never replaced with zero. Size and row bounds are enforced
    before expensive parsing.

    The loader never raises on bad input: file-level failures return an empty
    frame and cell-level failures leave ``None`` cells, both recorded in
    ``parse_audit`` for DQ01/DQ02 to report.
    """
    digest = hashlib.sha256()
    size = 0
    chunks: list[bytes] = []
    while chunk := source.read(CHUNK_BYTES):
        digest.update(chunk)
        size += len(chunk)
        if size <= config.max_bytes:
            chunks.append(chunk)
    sha = digest.hexdigest()
    precision = PrecisionMetadata(config.money_quantum, config.rate_quantum)

    def rejected(error: FileError, **audit: object) -> LoadedData:
        return LoadedData(
            frame=_empty_frame(),
            input_sha256=sha,
            source_lines=MappingProxyType({}),
            normalization_audit=MappingProxyType({}),
            precision=precision,
            dates=(),
            parse_audit=ParseAudit(
                file_errors=(error,), byte_count=size,
                unknown_dimension_label=config.unknown_dimension_label, **audit,
            ),
        )

    if size > config.max_bytes:
        return rejected(FileError.BYTE_LIMIT)
    raw = b"".join(chunks)
    if not raw.strip():
        return rejected(FileError.EMPTY_FILE)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return rejected(FileError.UNDECODABLE)

    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        header = [name.strip() for name in next(reader)]
        names = set(header)
        duplicates = tuple(sorted(n for n in names if header.count(n) > 1))
        missing = tuple(c.value for c in REQUIRED_COLUMNS if c.value not in names)
        extra = tuple(n for n in header if n not in {c.value for c in REQUIRED_COLUMNS})
        if missing:
            return rejected(FileError.MISSING_COLUMNS, missing_columns=missing,
                            duplicate_columns=duplicates, extra_columns=extra)
        if duplicates:
            return rejected(FileError.DUPLICATE_COLUMNS, duplicate_columns=duplicates,
                            extra_columns=extra)
        positions = [header.index(c.value) for c in REQUIRED_COLUMNS]
        records: list[list[str]] = []
        lines: list[int] = []
        malformed: list[int] = []
        for row in reader:
            if not row:
                continue
            if len(records) + len(malformed) >= config.max_rows:
                return rejected(FileError.ROW_LIMIT, extra_columns=extra)
            if len(row) != len(header):
                malformed.append(reader.line_num)
                continue
            records.append([row[i] for i in positions])
            lines.append(reader.line_num)
    except csv.Error:
        return rejected(FileError.MALFORMED_CSV)
    if not records and not malformed:
        return rejected(FileError.NO_DATA_ROWS, extra_columns=extra)

    raw_frame = pd.DataFrame(records, columns=[c.value for c in REQUIRED_COLUMNS], dtype=object)
    frame = pd.DataFrame(index=raw_frame.index)
    invalid: dict[str, tuple[int, ...]] = {}

    days = _parse_column(raw_frame[InputColumn.DAY.value], lambda s: _parse_day(s, config))
    raw_days = raw_frame[InputColumn.DAY.value]
    missing_dates = tuple(i for i, s in enumerate(raw_days) if not s.strip())
    blank = set(missing_dates)
    bad_days = tuple(i for i, d in enumerate(days) if d is None and i not in blank)
    if bad_days:
        invalid[InputColumn.DAY.value] = bad_days
    frame[InputColumn.DAY.value] = pd.Series(days, index=raw_frame.index, dtype=object)

    audit: dict[str, dict[str, tuple[str, ...]]] = {}
    for dimension, column in DIMENSION_COLUMNS.items():
        variants: dict[str, set[str]] = {}
        labels = []
        for value in raw_frame[column.value]:
            label = value.strip() or config.unknown_dimension_label
            variants.setdefault(label, set()).add(value)
            labels.append(label)
        frame[column.value] = pd.Series(labels, index=raw_frame.index, dtype=object)
        audit[dimension.value] = MappingProxyType(
            {k: tuple(sorted(v)) for k, v in sorted(variants.items())}
        )

    for column in NUMERIC_COLUMNS:
        values = _parse_column(raw_frame[column.value], _parse_number)
        bad = tuple(i for i, v in enumerate(values) if v is None)
        if bad:
            invalid[column.value] = bad
        frame[column.value] = pd.Series(values, index=raw_frame.index, dtype=object)

    frame = frame[[c.value for c in REQUIRED_COLUMNS]]
    return LoadedData(
        frame=frame,
        input_sha256=sha,
        source_lines=MappingProxyType(dict(enumerate(lines))),
        normalization_audit=MappingProxyType(audit),
        precision=precision,
        dates=tuple(sorted({d for d in days if d is not None})),
        parse_audit=ParseAudit(
            byte_count=size,
            extra_columns=extra,
            malformed_lines=tuple(malformed),
            invalid_cells=MappingProxyType(invalid),
            missing_dates=missing_dates,
            unknown_dimension_label=config.unknown_dimension_label,
        ),
    )


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame({c.value: pd.Series(dtype=object) for c in REQUIRED_COLUMNS})


def _parse_column[T](raw: pd.Series, parse: Callable[[str], T | None]) -> list[T | None]:
    cache = {value: parse(value) for value in raw.unique()}
    return [cache[value] for value in raw]


def _parse_day(text: str, config: InputConfig) -> date | None:
    text = text.strip()
    try:
        parsed = datetime.strptime(text, config.date_format).date()
    except ValueError:
        return None
    # Round trip rejects lenient forms such as unpadded months.
    return parsed if parsed.strftime(config.date_format) == text else None


def _parse_number(text: str) -> Decimal | None:
    text = text.strip()
    return Decimal(text) if _NUMBER.match(text) else None
