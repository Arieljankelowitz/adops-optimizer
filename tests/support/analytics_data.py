"""Test-only builders for analytics inputs.

``loaded_from_rows`` turns plain dicts into a ``LoadedData`` with the frame
conventions the analytics lane relies on: header-named columns, ``Day`` as a
date, integer counters and Decimal money and rates.
"""

import hashlib
from collections.abc import Iterable, Mapping
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd

from contracts.config import InputConfig, RunConfig, config_hash
from contracts.enums import COUNTER_COLUMNS, MONEY_COLUMNS, RATE_COLUMNS, QualityPass
from contracts.enums import InputColumn as C
from contracts.ids import make_namespace
from contracts.models import QualityIssue, QualityReport, QualityRuleResult
from contracts.runtime import LoadedData, PrecisionMetadata
from pipeline.load import load_csv

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic" / "ad_performance.csv"


def _typed(row: Mapping[str, object]) -> dict[str, object]:
    out: dict[str, object] = {}
    for column in C:
        value = row[column.value]
        if column is C.DAY:
            out[column.value] = value if isinstance(value, date) else date.fromisoformat(str(value))
        elif column in COUNTER_COLUMNS:
            out[column.value] = int(value)
        elif column in MONEY_COLUMNS or column in RATE_COLUMNS:
            out[column.value] = Decimal(str(value))
        else:
            out[column.value] = str(value)
    return out


def loaded_from_rows(rows: Iterable[Mapping[str, object]], label: str = "synthetic") -> LoadedData:
    typed = [_typed(r) for r in rows]
    frame = pd.DataFrame(typed, columns=[c.value for c in C])
    return LoadedData(
        frame=frame,
        input_sha256=hashlib.sha256(label.encode()).hexdigest(),
        source_lines={i: i + 2 for i in range(len(frame))},
        normalization_audit={},
        precision=PrecisionMetadata(money_quantum=Decimal("0.01"), rate_quantum=Decimal("0.01")),
        dates=tuple(sorted({r[C.DAY.value] for r in typed})),
    )


def load_fixture(path: Path = FIXTURE) -> LoadedData:
    """The committed synthetic fixture through the real loader."""
    with path.open("rb") as source:
        return load_csv(source, InputConfig())


def quality_for(
    data: LoadedData,
    issues: Iterable[QualityIssue] = (),
    rule_results: Iterable[QualityRuleResult] = (),
) -> QualityReport:
    cfg = config_hash(RunConfig())
    return QualityReport(
        run_namespace=make_namespace(data.input_sha256, cfg, "1"),
        quality_pass=QualityPass.INITIAL,
        config_hash=cfg,
        rule_results=list(rule_results),
        issues=list(issues),
    )


DAYS = tuple(date(2031, 3, d) for d in range(3, 10))


def row(
    day: date,
    key: tuple[str, str, str, str, str],
    requests: int,
    responses: int,
    wins: int,
    impressions: int,
    revenue: Decimal | str,
    cost: Decimal | str,
    floor: Decimal | str = "0.50",
    bid: Decimal | str = "0.60",
    profit: Decimal | str | None = None,
) -> dict[str, object]:
    """One export row; derived exported rates are filled but never read by analytics."""
    revenue, cost = Decimal(str(revenue)), Decimal(str(cost))
    return {
        C.DAY.value: day,
        C.SUPPLY_ID.value: key[0],
        C.COUNTRY.value: key[1],
        C.SIZE.value: key[2],
        C.PLACEMENT_TYPE.value: key[3],
        C.DEVICE_TYPE.value: key[4],
        C.DEMAND_REQUESTS.value: requests,
        C.DEMAND_RESPONSES.value: responses,
        C.OUR_BIDFLOOR.value: Decimal(str(floor)),
        C.DEMAND_BIDPRICE.value: Decimal(str(bid)),
        C.DEMAND_SRPM.value: Decimal(0),
        C.DEMAND_WINS.value: wins,
        C.SUPPLY_IMPRESSIONS.value: impressions,
        C.REVENUE.value: revenue,
        C.COST.value: cost,
        C.DEMAND_ECPM.value: Decimal(0),
        C.PROFIT.value: revenue - cost if profit is None else Decimal(str(profit)),
    }


def slice_rows(
    key: tuple[str, str, str, str, str],
    requests: int,
    revenue: Decimal | str,
    margin: Decimal | str = "0.30",
    response_rate: Decimal | str = "0.30",
    win_rate: Decimal | str = "0.30",
    impressions_per_win: Decimal | str = "0.90",
    floor: Decimal | str = "0.50",
    bid: Decimal | str = "0.60",
    days: tuple[date, ...] = DAYS,
    revenue_by_day: Mapping[date, Decimal | str] | None = None,
) -> list[dict[str, object]]:
    """Constant daily rows for one five-dimension slice (exact integer counters)."""
    out = []
    for day in days:
        rev = Decimal(str((revenue_by_day or {}).get(day, revenue)))
        responses = int(requests * Decimal(str(response_rate)))
        wins = int(responses * Decimal(str(win_rate)))
        impressions = int(wins * Decimal(str(impressions_per_win)))
        cost = (rev * (1 - Decimal(str(margin)))).quantize(Decimal("0.01"))
        out.append(row(day, key, requests, responses, wins, impressions, rev, cost, floor, bid))
    return out


def background() -> list[dict[str, object]]:
    """Four large ordinary slices: revenue 1.00 and profit 0.30 per million requests."""
    rows = []
    for i in range(4):
        key = (f"9000{i}", "US", "300x250", "Banner", "Desktop" if i % 2 else "Mobile")
        rows += slice_rows(key, 250_000_000, "250.00")
    return rows
