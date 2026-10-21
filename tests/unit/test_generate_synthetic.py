"""The synthetic fixture is reproducible and carries every planted trap."""

import importlib.util
import json
import statistics
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from contracts.enums import REQUIRED_COLUMNS, InputColumn as C

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "synthetic" / "ad_performance.csv"
TRAPS = FIXTURE.with_name("ad_performance.traps.json")
KEY = [c.value for c in REQUIRED_COLUMNS[:6]]


def _generator():
    spec = importlib.util.spec_from_file_location(
        "generate_synthetic", REPO_ROOT / "scripts" / "generate_synthetic.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def frame() -> pd.DataFrame:
    return pd.read_csv(FIXTURE, dtype=str, keep_default_na=False)


@pytest.fixture(scope="module")
def traps() -> dict:
    return json.loads(TRAPS.read_text())


def _num(frame: pd.DataFrame, column: C) -> pd.Series:
    return frame[column.value].map(Decimal)


def _rows(frame: pd.DataFrame, refs: list[dict]) -> pd.DataFrame:
    keys = pd.DataFrame(refs)
    return frame.merge(keys, on=list(keys.columns), how="inner")


def test_regeneration_is_byte_identical(tmp_path: Path) -> None:
    gen = _generator()
    out = tmp_path / "ad_performance.csv"
    gen.write(gen.DEFAULT_SEED, out)
    assert out.read_bytes() == FIXTURE.read_bytes()
    assert gen.traps_path(out).read_bytes() == TRAPS.read_bytes()


def test_other_seed_differs() -> None:
    gen = _generator()
    assert gen.to_csv(gen.generate(1)[0]) != FIXTURE.read_text()


def test_header_is_the_exact_17_column_contract(frame: pd.DataFrame, traps: dict) -> None:
    assert list(frame.columns) == [c.value for c in REQUIRED_COLUMNS]
    assert len(frame.columns) == 17
    assert traps["header"] == list(frame.columns)
    assert traps["synthetic"] is True


def test_unique_grain_and_row_count(frame: pd.DataFrame, traps: dict) -> None:
    assert not frame.duplicated(subset=KEY).any()
    assert len(frame) == traps["row_count"]


def test_supply_ids_are_opaque_strings(frame: pd.DataFrame) -> None:
    assert "07731" in set(frame[C.SUPPLY_ID.value])


def test_partial_final_day(frame: pd.DataFrame, traps: dict) -> None:
    daily = _num(frame, C.DEMAND_REQUESTS).groupby(frame[C.DAY.value]).sum()
    assert list(daily.index) == traps["dates"]
    last = daily[traps["partial_final_day"]["day"]]
    others = daily.drop(traps["partial_final_day"]["day"])
    assert last < Decimal("0.5") * statistics.median(others)


def test_two_day_spike_concentrates_revenue(frame: pd.DataFrame, traps: dict) -> None:
    spike = traps["planted_slices"]["two_day_spike"]
    rows = _rows(frame, [spike["key"]])
    revenue = _num(rows, C.REVENUE).groupby(rows[C.DAY.value]).sum()
    top_two = revenue.sort_values(ascending=False).iloc[:2]
    assert sorted(top_two.index) == spike["spike_days"]
    assert top_two.sum() / revenue.sum() >= Decimal("0.8")
    assert revenue.sum() >= 100


def test_placeholders_present(frame: pd.DataFrame, traps: dict) -> None:
    assert set(traps["placeholders"]["country"]) <= set(frame[C.COUNTRY.value])
    assert set(traps["placeholders"]["size"]) <= set(frame[C.SIZE.value])


def test_responses_over_requests(frame: pd.DataFrame, traps: dict) -> None:
    bad = _num(frame, C.DEMAND_RESPONSES) > _num(frame, C.DEMAND_REQUESTS)
    flagged = frame[bad][KEY].to_dict("records")
    assert sorted(map(str, flagged)) == sorted(
        map(str, traps["row_traps"]["responses_over_requests"])
    )


def test_impressions_without_requests(frame: pd.DataFrame, traps: dict) -> None:
    bad = (_num(frame, C.SUPPLY_IMPRESSIONS) > 0) & (_num(frame, C.DEMAND_REQUESTS) == 0)
    assert len(frame[bad]) == len(traps["row_traps"]["impressions_without_requests"]) == 2
    assert (_num(frame[bad], C.REVENUE) > 0).all()


def test_requests_look_scaled(frame: pd.DataFrame, traps: dict) -> None:
    requests = _num(frame, C.DEMAND_REQUESTS)
    positive = requests[requests > 0]
    granularity = traps["request_granularity"]
    assert len(positive) >= 100
    assert (positive % granularity == 0).mean() >= 0.95
    responses = _num(frame, C.DEMAND_RESPONSES)
    assert (responses[responses > 0] % granularity == 0).mean() < 0.5


def test_srpm_is_per_million_requests(frame: pd.DataFrame) -> None:
    rows = frame[_num(frame, C.DEMAND_REQUESTS) > 0]
    requests = _num(rows, C.DEMAND_REQUESTS)
    revenue = _num(rows, C.REVENUE)
    srpm = _num(rows, C.DEMAND_SRPM)
    per_million = (srpm * requests / 1_000_000 - revenue).abs()
    tolerance = Decimal("0.005") + Decimal("0.005") * requests / 1_000_000
    assert (per_million <= tolerance).all()
    per_thousand = (srpm * requests / 1_000 - revenue).abs()
    assert (per_thousand[revenue > 1] > 1).all()


def test_profit_reconciliation_traps(frame: pd.DataFrame, traps: dict) -> None:
    residual = (
        _num(frame, C.PROFIT) - (_num(frame, C.REVENUE) - _num(frame, C.COST))
    ).abs()
    within = frame[(residual > 0) & (residual <= Decimal("0.015"))][KEY].to_dict("records")
    beyond = frame[residual > Decimal("0.015")][KEY].to_dict("records")
    assert sorted(map(str, within)) == sorted(
        map(str, traps["row_traps"]["profit_cent_discrepancy"])
    )
    assert sorted(map(str, beyond)) == sorted(
        map(str, traps["row_traps"]["profit_over_rounding_bound"])
    )


def test_rounding_zero_revenue_written_as_zero(frame: pd.DataFrame, traps: dict) -> None:
    rows = _rows(frame, traps["row_traps"]["rounding_zero_revenue"])
    assert len(rows) == 3
    assert (rows[C.REVENUE.value] == "0").all()
    assert (_num(rows, C.SUPPLY_IMPRESSIONS) > 0).all()


def test_negative_profit_slice(frame: pd.DataFrame, traps: dict) -> None:
    rows = _rows(frame, [traps["planted_slices"]["negative_margin"]["key"]])
    assert _num(rows, C.PROFIT).sum() < 0


def test_intermittent_slice_is_absent_on_some_days(frame: pd.DataFrame, traps: dict) -> None:
    planted = traps["planted_slices"]["intermittent"]
    rows = _rows(frame, [planted["key"]])
    assert sorted(rows[C.DAY.value]) == planted["days"]
    assert len(planted["days"]) < len(traps["dates"])
