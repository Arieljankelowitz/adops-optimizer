"""Strict loader: header contract, typed parsing without coercion, bounds and audit."""

import hashlib
import io
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from contracts.config import InputConfig
from contracts.enums import InputColumn as C
from contracts.runtime import FileError
from pipeline.load import load_csv
from tests.quality_support import HEADER, clean_rows, load, make_row, to_csv

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic" / "ad_performance.csv"


def _load_bytes(raw: bytes, config: InputConfig = InputConfig()):
    return load_csv(io.BytesIO(raw), config)


def test_fixture_loads_every_row_with_typed_columns() -> None:
    raw = FIXTURE.read_bytes()
    data = _load_bytes(raw)
    assert data.parse_audit.readable
    assert data.input_sha256 == hashlib.sha256(raw).hexdigest()
    assert list(data.frame.columns) == HEADER
    assert len(data.frame) == raw.count(b"\n") - 1
    assert all(isinstance(d, date) for d in data.frame[C.DAY.value])
    assert all(isinstance(x, Decimal) for x in data.frame[C.REVENUE.value])
    assert len(data.dates) == 7
    assert data.source_lines[0] == 2
    assert data.precision.money_quantum == Decimal("0.01")


def test_supply_id_is_an_opaque_string() -> None:
    rows = clean_rows(days=1)
    rows[0][C.SUPPLY_ID.value] = "007"
    data = load(rows)
    assert data.frame[C.SUPPLY_ID.value][0] == "007"


def test_money_keeps_source_precision_and_zero_rows_are_retained() -> None:
    rows = clean_rows(days=1)
    rows[0][C.REVENUE.value] = "1.10"
    rows.append(make_row(date(2031, 5, 1), ("9999", "US", "300x250", "Banner", "Tablet"), 0,
                         Decimal(0)))
    data = load(rows)
    assert str(data.frame[C.REVENUE.value][0]) == "1.10"
    assert data.frame[C.DEMAND_REQUESTS.value].iloc[-1] == 0
    assert len(data.frame) == len(rows)


def test_whitespace_is_normalized_with_reversible_audit() -> None:
    rows = clean_rows(days=1)
    for row in rows:
        row[C.COUNTRY.value] = f" {row[C.COUNTRY.value]} "
    data = load(rows)
    assert set(data.frame[C.COUNTRY.value]) == {"US", "DE", "FR", "JP"}
    assert data.normalization_audit["country"]["US"] == (" US ",)


def test_blank_dimension_becomes_tagged_unknown_bucket() -> None:
    rows = clean_rows(days=1)
    rows[0][C.SIZE.value] = ""
    data = load(rows)
    assert data.frame[C.SIZE.value][0] == "UNKNOWN"
    assert data.normalization_audit["size"]["UNKNOWN"] == ("",)


@pytest.mark.parametrize("bad", ["2031-5-01", "2031-02-30", "05/01/2031", "yesterday"])
def test_bad_dates_are_recorded_not_guessed(bad: str) -> None:
    rows = clean_rows(days=1)
    rows[2][C.DAY.value] = bad
    data = load(rows)
    assert data.frame[C.DAY.value][2] is None
    assert data.parse_audit.invalid_cells[C.DAY.value] == (2,)
    assert data.parse_audit.missing_dates == ()


def test_blank_date_is_recorded_as_missing() -> None:
    rows = clean_rows(days=1)
    rows[1][C.DAY.value] = " "
    data = load(rows)
    assert data.parse_audit.missing_dates == (1,)
    assert C.DAY.value not in data.parse_audit.invalid_cells


@pytest.mark.parametrize("bad", ["abc", "", "NaN", "inf", "-Infinity", "1e5", "\"1,000\"", "--1"])
def test_non_numeric_cells_are_never_coerced_to_zero(bad: str) -> None:
    rows = clean_rows(days=1)
    rows[3][C.COST.value] = bad
    data = load(rows)
    assert data.frame[C.COST.value][3] is None
    assert data.parse_audit.invalid_cells == {C.COST.value: (3,)}


def test_missing_required_header_rejects_file() -> None:
    header = [h for h in HEADER if h != C.COST.value]
    data = _load_bytes(to_csv(clean_rows(days=1), header))
    assert data.parse_audit.file_errors == (FileError.MISSING_COLUMNS,)
    assert data.parse_audit.missing_columns == (C.COST.value,)
    assert data.frame.empty and data.dates == ()


def test_duplicate_header_rejects_file() -> None:
    raw = (",".join([*HEADER, C.REVENUE.value]) + "\n").encode()
    data = _load_bytes(raw + b"x\n")
    assert data.parse_audit.file_errors == (FileError.DUPLICATE_COLUMNS,)
    assert data.parse_audit.duplicate_columns == (C.REVENUE.value,)


def test_extra_header_is_ignored_explicitly() -> None:
    rows = clean_rows(days=1)
    for row in rows:
        row["Notes"] = "n"
    data = _load_bytes(to_csv(rows, [*HEADER, "Notes"]))
    assert data.parse_audit.readable
    assert data.parse_audit.extra_columns == ("Notes",)
    assert "Notes" not in data.frame.columns


def test_header_order_and_surrounding_whitespace_do_not_matter() -> None:
    rows = clean_rows(days=1)
    header = [f" {h} " for h in reversed(HEADER)]
    buf = ",".join(header) + "\n" + "".join(
        ",".join(r[h] for h in reversed(HEADER)) + "\n" for r in rows)
    data = _load_bytes(buf.encode())
    assert data.parse_audit.readable
    assert list(data.frame.columns) == HEADER


def test_ragged_line_is_recorded_with_its_source_line() -> None:
    raw = to_csv(clean_rows(days=1)) + b"2031-05-01,1,2\n"
    data = _load_bytes(raw)
    assert data.parse_audit.malformed_lines == (raw.count(b"\n"),)
    assert len(data.frame) == 16


def test_byte_limit_rejects_before_parsing_but_still_hashes() -> None:
    raw = to_csv(clean_rows(days=1))
    data = _load_bytes(raw, InputConfig(max_bytes=100))
    assert data.parse_audit.file_errors == (FileError.BYTE_LIMIT,)
    assert data.input_sha256 == hashlib.sha256(raw).hexdigest()


def test_row_limit_rejects_file() -> None:
    data = _load_bytes(to_csv(clean_rows(days=1)), InputConfig(max_rows=5))
    assert data.parse_audit.file_errors == (FileError.ROW_LIMIT,)


@pytest.mark.parametrize("raw,error", [
    (b"", FileError.EMPTY_FILE),
    (b"  \n", FileError.EMPTY_FILE),
    ((",".join(HEADER) + "\n").encode(), FileError.NO_DATA_ROWS),
    (b"\xff\xfe\x00bad", FileError.UNDECODABLE),
    ((",".join(HEADER) + '\n"unterminated,1\n').encode(), FileError.MALFORMED_CSV),
])
def test_unreadable_files(raw: bytes, error: FileError) -> None:
    assert _load_bytes(raw).parse_audit.file_errors == (error,)


def test_utf8_bom_and_crlf_are_accepted() -> None:
    raw = b"\xef\xbb\xbf" + to_csv(clean_rows(days=1)).replace(b"\n", b"\r\n")
    data = _load_bytes(raw)
    assert data.parse_audit.readable
    assert len(data.frame) == 16
