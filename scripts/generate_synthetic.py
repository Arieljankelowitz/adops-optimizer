"""Generate an independent synthetic export with planted data-quality traps.

Every ID, label combination and number is invented from a fixed seed; nothing
is derived from a real export. The output has the exact 17-column header of
the expected input and a sidecar ``<stem>.traps.json`` describing each
planted trap so tests can assert detectors against known labels.

Usage:
    uv run python scripts/generate_synthetic.py [--seed N] [--out PATH]
"""

import argparse
import csv
import io
import json
import random
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from contracts.enums import REQUIRED_COLUMNS, InputColumn  # noqa: E402

DEFAULT_SEED = 20310303
DEFAULT_OUT = REPO_ROOT / "tests" / "fixtures" / "synthetic" / "ad_performance.csv"

START_DAY = date(2031, 3, 3)
DAY_COUNT = 7
PARTIAL_DAY_FACTOR = 0.3
REQUEST_GRANULARITY = 1000
CENT = Decimal("0.01")

SUPPLY_IDS = ("41007", "41022", "52310", "52311", "60894", "07731", "73005", "88140")
COUNTRIES = ("US", "DE", "BR", "IN", "GB")
SIZES = ("300x250", "728x90", "320x50", "300x600")
PLACEMENTS = ("Banner", "Video")
DEVICES = ("Desktop", "Mobile", "Tablet")

Key = tuple[str, str, str, str, str]


@dataclass(frozen=True)
class SliceProfile:
    key: Key
    daily_requests: int
    response_rate: float
    win_rate: float
    impressions_per_win: float
    ecpm: float
    margin: float
    floor: float
    bid_to_floor: float
    days: tuple[int, ...] = tuple(range(DAY_COUNT))
    # day index -> (request multiplier, eCPM multiplier)
    spikes: tuple[tuple[int, float, float], ...] = ()


# Planted slices: name -> profile. Keys are invented label combinations.
PLANTED: dict[str, SliceProfile] = {
    "wasted_requests": SliceProfile(
        ("60894", "US", "320x50", "Banner", "Mobile"),
        45_000_000, 0.002, 0.30, 0.90, 0.05, 0.30, 0.20, 1.10,
    ),
    "scale_winner": SliceProfile(
        ("41022", "DE", "300x250", "Banner", "Desktop"),
        1_800_000, 0.40, 0.30, 0.92, 6.00, 0.40, 0.80, 1.60,
    ),
    "two_day_spike": SliceProfile(
        ("52310", "BR", "728x90", "Banner", "Desktop"),
        300_000, 0.30, 0.30, 0.90, 0.20, 0.35, 0.10, 1.40,
        spikes=((2, 4.0, 25.0), (3, 4.0, 25.0)),
    ),
    "negative_margin": SliceProfile(
        ("73005", "IN", "300x250", "Video", "Mobile"),
        900_000, 0.35, 0.35, 0.90, 1.20, -0.15, 0.60, 1.20,
    ),
    "high_bid_floor": SliceProfile(
        ("88140", "GB", "300x600", "Banner", "Desktop"),
        2_000_000, 0.25, 0.30, 0.92, 2.50, 0.35, 0.50, 4.80,
    ),
    "wins_impressions_gap": SliceProfile(
        ("07731", "US", "728x90", "Video", "Tablet"),
        1_500_000, 0.60, 0.25, 0.05, 3.00, 0.30, 1.00, 1.30,
    ),
    "placeholder_size": SliceProfile(
        ("52311", "US", "9999x9999", "Banner", "Mobile"),
        700_000, 0.20, 0.30, 0.90, 0.90, 0.30, 0.30, 1.30,
    ),
    "placeholder_country": SliceProfile(
        ("41007", "-", "300x250", "Banner", "Mobile"),
        600_000, 0.20, 0.30, 0.90, 0.80, 0.30, 0.30, 1.30,
    ),
    "intermittent": SliceProfile(
        ("88140", "DE", "320x50", "Banner", "Mobile"),
        250_000, 0.20, 0.30, 0.90, 0.70, 0.30, 0.25, 1.30,
        days=(0, 3, 5),
    ),
}

GENERIC_SLICE_COUNT = 30


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _fmt(value: Decimal) -> str:
    # Exact zeros are written as "0", like many exports; the quantum stays 0.01.
    return "0" if value == 0 else f"{value:.2f}"


def _dec(x: float) -> Decimal:
    return Decimal(repr(x))


def _generic_slices(rng: random.Random) -> list[SliceProfile]:
    taken = {p.key for p in PLANTED.values()}
    slices: list[SliceProfile] = []
    while len(slices) < GENERIC_SLICE_COUNT:
        key = (
            rng.choice(SUPPLY_IDS),
            rng.choice(COUNTRIES),
            rng.choice(SIZES),
            rng.choice(PLACEMENTS),
            rng.choice(DEVICES),
        )
        if key in taken:
            continue
        taken.add(key)
        slices.append(
            SliceProfile(
                key,
                daily_requests=rng.randint(50, 3000) * REQUEST_GRANULARITY,
                response_rate=round(rng.uniform(0.05, 0.6), 4),
                win_rate=round(rng.uniform(0.1, 0.5), 4),
                impressions_per_win=round(rng.uniform(0.8, 0.98), 4),
                ecpm=round(rng.uniform(0.3, 4.0), 4),
                margin=round(rng.uniform(0.2, 0.45), 4),
                floor=round(rng.uniform(0.1, 1.5), 2),
                bid_to_floor=round(rng.uniform(1.0, 1.8), 4),
            )
        )
    return slices


def _row(day: date, key: Key, requests: int, responses: int, wins: int, impressions: int,
         floor: Decimal, bid: Decimal, ecpm: Decimal, margin: Decimal,
         profit_offset: Decimal = Decimal(0)) -> dict[str, object]:
    revenue = _money(Decimal(impressions) * ecpm / 1000)
    cost = _money(revenue * (1 - margin))
    profit = revenue - cost + profit_offset
    srpm = _money(revenue / requests * 1_000_000) if requests else Decimal(0)
    exported_ecpm = _money(revenue / impressions * 1000) if impressions else Decimal(0)
    return {
        InputColumn.DAY: day.isoformat(),
        InputColumn.SUPPLY_ID: key[0],
        InputColumn.COUNTRY: key[1],
        InputColumn.SIZE: key[2],
        InputColumn.PLACEMENT_TYPE: key[3],
        InputColumn.DEVICE_TYPE: key[4],
        InputColumn.DEMAND_REQUESTS: requests,
        InputColumn.DEMAND_RESPONSES: responses,
        InputColumn.OUR_BIDFLOOR: _fmt(floor),
        InputColumn.DEMAND_BIDPRICE: _fmt(bid if responses else Decimal(0)),
        InputColumn.DEMAND_SRPM: _fmt(srpm),
        InputColumn.DEMAND_WINS: wins,
        InputColumn.SUPPLY_IMPRESSIONS: impressions,
        InputColumn.REVENUE: _fmt(revenue),
        InputColumn.COST: _fmt(cost),
        InputColumn.DEMAND_ECPM: _fmt(exported_ecpm),
        InputColumn.PROFIT: _fmt(profit),
    }


def _slice_rows(rng: random.Random, profile: SliceProfile) -> list[dict[str, object]]:
    spikes = {d: (rm, em) for d, rm, em in profile.spikes}
    floor = _money(_dec(profile.floor))
    bid = _money(_dec(profile.floor * profile.bid_to_floor))
    rows = []
    for d in profile.days:
        req_mult, ecpm_mult = spikes.get(d, (1.0, 1.0))
        noise = rng.uniform(0.85, 1.15)
        factor = noise * req_mult * (PARTIAL_DAY_FACTOR if d == DAY_COUNT - 1 else 1.0)
        requests = round(profile.daily_requests * factor / REQUEST_GRANULARITY) * REQUEST_GRANULARITY
        responses = int(requests * profile.response_rate)
        wins = int(responses * profile.win_rate)
        impressions = int(wins * profile.impressions_per_win)
        ecpm = _dec(round(profile.ecpm * ecpm_mult * rng.uniform(0.95, 1.05), 4))
        rows.append(
            _row(START_DAY + timedelta(days=d), profile.key, requests, responses, wins,
                 impressions, floor, bid, ecpm, _dec(profile.margin))
        )
    return rows


def generate(seed: int = DEFAULT_SEED) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Return ``(rows, traps)``; identical for the same seed."""
    rng = random.Random(seed)
    generic = _generic_slices(rng)
    rows: list[dict[str, object]] = []
    for profile in (*PLANTED.values(), *generic):
        rows.extend(_slice_rows(rng, profile))

    # Row-level traps on generic slices, on full days only.
    full_day_rows = [
        r for r in rows
        if tuple(r[c] for c in REQUIRED_COLUMNS[1:6]) in {g.key for g in generic}
        and r[InputColumn.DAY] != (START_DAY + timedelta(days=DAY_COUNT - 1)).isoformat()
    ]
    picked = rng.sample(full_day_rows, 8)

    responses_over_requests = picked[0:3]
    for r in responses_over_requests:
        r[InputColumn.DEMAND_RESPONSES] = int(r[InputColumn.DEMAND_REQUESTS]) + rng.randint(100, 900)

    cent_discrepancies = picked[3:5]
    for r in cent_discrepancies:
        r[InputColumn.PROFIT] = _fmt(Decimal(r[InputColumn.PROFIT]) + CENT)
    over_bound = picked[5]
    over_bound[InputColumn.PROFIT] = _fmt(Decimal(over_bound[InputColumn.PROFIT]) + Decimal("0.25"))

    # Extra rows: impressions without requests, and cent-rounded zero revenue.
    # Their sizes never occur in generic slices, so they cannot collide.
    extra_key_pool = [g.key for g in generic]
    impressions_without_requests = []
    for i in range(2):
        key = extra_key_pool[i]
        day = START_DAY + timedelta(days=1 + i)
        row = _row(day, (key[0], key[1], "468x60", "Video", key[4]), 0, 0, 40, 40,
                   Decimal("0.20"), Decimal("0.30"), Decimal("3.00"), Decimal("0.3"))
        impressions_without_requests.append(row)
    rounding_zero = []
    for i in range(3):
        key = extra_key_pool[2 + i]
        day = START_DAY + timedelta(days=i)
        row = _row(day, (key[0], key[1], "970x250", "Video", key[4]), 5_000, 30, 12, 12,
                   Decimal("0.10"), Decimal("0.15"), Decimal("0.20"), Decimal("0.3"))
        rounding_zero.append(row)
    rows.extend(impressions_without_requests + rounding_zero)

    def sort_key(r: dict[str, object]) -> tuple[str, ...]:
        return tuple(str(r[c]) for c in REQUIRED_COLUMNS[:6])

    rows.sort(key=sort_key)
    keys = [sort_key(r) for r in rows]
    if len(keys) != len(set(keys)):
        raise RuntimeError("generator produced a duplicate grain key")

    def ref(r: dict[str, object]) -> dict[str, str]:
        return {c.value: str(r[c]) for c in REQUIRED_COLUMNS[:6]}

    dates = [(START_DAY + timedelta(days=d)).isoformat() for d in range(DAY_COUNT)]
    traps: dict[str, object] = {
        "seed": seed,
        "synthetic": True,
        "header": [c.value for c in REQUIRED_COLUMNS],
        "dates": dates,
        "row_count": len(rows),
        "partial_final_day": {"day": dates[-1], "request_factor": PARTIAL_DAY_FACTOR},
        "request_granularity": REQUEST_GRANULARITY,
        "srpm_multiplier": 1_000_000,
        "planted_slices": {
            name: {
                "key": dict(zip([c.value for c in REQUIRED_COLUMNS[1:6]], p.key, strict=True)),
                "days": [dates[d] for d in p.days],
                "spike_days": [dates[d] for d, _, _ in p.spikes],
            }
            for name, p in PLANTED.items()
        },
        "row_traps": {
            "responses_over_requests": [ref(r) for r in responses_over_requests],
            "impressions_without_requests": [ref(r) for r in impressions_without_requests],
            "profit_cent_discrepancy": [ref(r) for r in cent_discrepancies],
            "profit_over_rounding_bound": [ref(over_bound)],
            "rounding_zero_revenue": [ref(r) for r in rounding_zero],
        },
        "placeholders": {"country": ["-"], "size": ["9999x9999"]},
    }
    return rows, traps


def to_csv(rows: list[dict[str, object]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow([c.value for c in REQUIRED_COLUMNS])
    for r in rows:
        writer.writerow([r[c] for c in REQUIRED_COLUMNS])
    return buf.getvalue()


def traps_path(csv_path: Path) -> Path:
    return csv_path.with_name(f"{csv_path.stem}.traps.json")


def write(seed: int, out: Path) -> None:
    rows, traps = generate(seed)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(to_csv(rows), encoding="utf-8")
    traps_path(out).write_text(json.dumps(traps, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    write(args.seed, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
