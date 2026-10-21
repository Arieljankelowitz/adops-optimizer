"""Planted detector slices clear the default configuration gates they target.

This keeps the fixture useful for the detector lanes: if a default threshold
or the generator changes, the planted examples must still trigger.
"""

import json
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from contracts.config import AnalyticsConfig, DetectorConfig
from contracts.enums import REQUIRED_COLUMNS, InputColumn as C

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic" / "ad_performance.csv"
KEY = [c.value for c in REQUIRED_COLUMNS[1:6]]
ANALYTICS = AnalyticsConfig()
DETECTORS = DetectorConfig()


@pytest.fixture(scope="module")
def frame() -> pd.DataFrame:
    df = pd.read_csv(FIXTURE, dtype=str, keep_default_na=False)
    for column in (*REQUIRED_COLUMNS[6:],):
        df[column.value] = df[column.value].map(Decimal)
    return df


@pytest.fixture(scope="module")
def planted() -> dict:
    traps = json.loads(FIXTURE.with_name("ad_performance.traps.json").read_text())
    return traps["planted_slices"]


def _totals(frame: pd.DataFrame, key: dict) -> pd.Series:
    mask = pd.Series(True, index=frame.index)
    for column, value in key.items():
        mask &= frame[column] == value
    return frame[mask][[c.value for c in REQUIRED_COLUMNS[6:]]].sum()


def _profit_per_million(t: pd.Series) -> Decimal:
    return t[C.PROFIT.value] / t[C.DEMAND_REQUESTS.value] * 1_000_000


def test_scale_winner(frame: pd.DataFrame, planted: dict) -> None:
    t = _totals(frame, planted["scale_winner"]["key"])
    account = frame[[c.value for c in REQUIRED_COLUMNS[6:]]].sum()
    assert t[C.DEMAND_REQUESTS.value] >= ANALYTICS.min_requests
    assert t[C.REVENUE.value] >= ANALYTICS.min_revenue
    share = t[C.DEMAND_REQUESTS.value] / account[C.DEMAND_REQUESTS.value]
    assert share < DETECTORS.scale_max_request_share
    assert _profit_per_million(t) >= DETECTORS.scale_yield_multiple * _profit_per_million(account)


def test_wasted_requests(frame: pd.DataFrame, planted: dict) -> None:
    t = _totals(frame, planted["wasted_requests"]["key"])
    assert t[C.DEMAND_REQUESTS.value] >= ANALYTICS.waste_min_requests
    per_million = t[C.REVENUE.value] / t[C.DEMAND_REQUESTS.value] * 1_000_000
    assert per_million < DETECTORS.waste_max_revenue_per_million


def test_negative_margin(frame: pd.DataFrame, planted: dict) -> None:
    t = _totals(frame, planted["negative_margin"]["key"])
    assert t[C.REVENUE.value] >= ANALYTICS.min_revenue
    assert t[C.PROFIT.value] < 0


def test_high_bid_floor(frame: pd.DataFrame, planted: dict) -> None:
    key = planted["high_bid_floor"]["key"]
    t = _totals(frame, key)
    assert t[C.DEMAND_REQUESTS.value] >= ANALYTICS.min_requests
    assert t[C.REVENUE.value] >= ANALYTICS.min_revenue
    rows = frame.merge(pd.DataFrame([key]), on=KEY)
    weights = rows[C.DEMAND_RESPONSES.value]
    bid = (weights * rows[C.DEMAND_BIDPRICE.value]).sum() / weights.sum()
    floor = (weights * rows[C.OUR_BIDFLOOR.value]).sum() / weights.sum()
    assert bid / floor >= DETECTORS.high_bid_floor_ratio


def test_wins_impressions_gap(frame: pd.DataFrame, planted: dict) -> None:
    t = _totals(frame, planted["wins_impressions_gap"]["key"])
    assert t[C.DEMAND_WINS.value] >= ANALYTICS.wins_min_wins
    assert t[C.REVENUE.value] >= ANALYTICS.min_revenue
    ratio = t[C.SUPPLY_IMPRESSIONS.value] / t[C.DEMAND_WINS.value]
    assert ratio < DETECTORS.impressions_per_win_floor
