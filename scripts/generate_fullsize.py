"""Generate a full-size synthetic export: ~118,000 rows over 7 days.

The small fixture (``generate_synthetic.py``) has 39 slices; a real export has
tens of thousands, so slices at every aggregation level overlap heavily and
quality issues touch many of them. This file reproduces that scale with
invented IDs, labels and numbers from a fixed seed; nothing is derived from a
real export. Supply, country, size and device effects multiply, so detectors
fire at several levels at once (a supply, that supply in one country, one of
its five-key slices), as they do on real data.

It is never committed (``*.csv`` is gitignored): generate it when needed.

Usage:
    uv run python scripts/generate_fullsize.py --out /tmp/fullsize.csv [--seed N] [--rows N]
"""

import argparse
import csv
import math
import random
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from contracts.enums import REQUIRED_COLUMNS, InputColumn  # noqa: E402
from scripts.generate_synthetic import (  # noqa: E402
    CENT,
    DAY_COUNT,
    PARTIAL_DAY_FACTOR,
    REQUEST_GRANULARITY,
    START_DAY,
    _dec,
    _fmt,
    _money,
    _row,
)

DEFAULT_SEED = 20310417
DEFAULT_ROWS = 118_000

COUNTRIES = (
    "USA", "DEU", "BRA", "IND", "GBR", "FRA", "ITA", "ESP", "MEX", "CAN", "JPN", "KOR", "AUS", "NLD",
    "POL", "TUR", "IDN", "PHL", "THA", "VNM", "ARG", "COL", "CHL", "PER", "ZAF", "NGA", "EGY", "SAU",
    "ARE", "SWE", "NOR", "DNK", "FIN", "BEL", "AUT", "CHE", "PRT", "GRC", "ROU", "CZE", "HUN", "UKR",
    "MYS", "SGP", "PAK",
)
SIZES = (
    "300x250", "728x90", "320x50", "300x600", "160x600", "970x250", "320x100",
    "336x280", "468x60", "300x50", "250x250", "120x600", "970x90", "640x480",
)
PLACEMENTS = ("Banner", "Video")
DEVICES = ("Desktop", "Mobile", "Tablet")
SUPPLY_COUNT = 140


def _weighted(rng: random.Random, items, weights):
    return rng.choices(items, weights=weights, k=1)[0]


def _profiles(rng: random.Random):
    """Multiplicative effects per label, so patterns appear at coarse levels too."""
    supplies = sorted({f"{rng.randint(10000, 99999)}" for _ in range(SUPPLY_COUNT * 2)})
    supplies = supplies[:SUPPLY_COUNT]
    supply = {
        s: dict(
            weight=rng.lognormvariate(0, 1.3),
            ecpm=rng.lognormvariate(0, 0.45),
            response=rng.uniform(0.6, 1.4),
            margin=rng.uniform(0.22, 0.42),
            floor=rng.uniform(0.6, 1.4),
            bid_to_floor=rng.uniform(1.05, 1.7),
        )
        for s in supplies
    }
    # Planted supply-level behaviour, each on a distinct supply ID.
    planted = rng.sample(supplies, 9)
    supply[planted[0]].update(weight=40.0, response=0.004, ecpm=0.05)  # wasted requests
    supply[planted[1]].update(weight=25.0, response=0.006, ecpm=0.08)  # wasted requests
    supply[planted[2]].update(margin=-0.12)  # negative margin
    supply[planted[3]].update(margin=0.08)  # thin margin
    supply[planted[4]].update(bid_to_floor=4.5)  # bids far above floor
    supply[planted[5]].update(bid_to_floor=0.7, response=0.3)  # bids below floor
    supply[planted[6]].update(ecpm=3.5, weight=0.6)  # small, very high yield
    supply[planted[7]].update(impressions_per_win=0.04)  # wins without impressions
    supply[planted[8]].update(spike=True)  # two-day revenue spike
    country = {
        c: dict(weight=rng.lognormvariate(0, 1.0), ecpm=rng.lognormvariate(0, 0.35))
        for c in COUNTRIES
    }
    country["USA"]["weight"] *= 6
    country["USA"]["ecpm"] *= 2.2
    size = {z: dict(weight=rng.lognormvariate(0, 0.8), ecpm=rng.lognormvariate(0, 0.3)) for z in SIZES}
    device = {
        "Desktop": dict(weight=1.0, ecpm=1.3),
        "Mobile": dict(weight=2.4, ecpm=0.8),
        "Tablet": dict(weight=0.3, ecpm=1.0),
    }
    placement = {"Banner": dict(weight=1.0, ecpm=1.0), "Video": dict(weight=0.35, ecpm=3.0)}
    return supplies, supply, country, size, device, placement


def generate(seed: int = DEFAULT_SEED, target_rows: int = DEFAULT_ROWS) -> list[dict[str, object]]:
    """Rows sorted by grain; identical for the same seed and size."""
    rng = random.Random(seed)
    supplies, supply, country, size, device, placement = _profiles(rng)
    # Each active combination appears on ~6.6 of 7 days on average.
    combo_target = math.ceil(target_rows / 6.6)
    sw = [supply[s]["weight"] for s in supplies]
    cw = [country[c]["weight"] for c in COUNTRIES]
    zw = [size[z]["weight"] for z in SIZES]
    dw = [device[d]["weight"] for d in DEVICES]
    pw = [placement[p]["weight"] for p in PLACEMENTS]
    combos: set[tuple[str, str, str, str, str]] = set()
    while len(combos) < combo_target:
        combos.add(
            (
                _weighted(rng, supplies, sw),
                _weighted(rng, COUNTRIES, cw),
                _weighted(rng, SIZES, zw),
                _weighted(rng, PLACEMENTS, pw),
                _weighted(rng, DEVICES, dw),
            )
        )
    rows: list[dict[str, object]] = []
    for key in sorted(combos):
        s, c, z, p, d = key
        sp = supply[s]
        scale = sp["weight"] * country[c]["weight"] * zw[SIZES.index(z)] * dw[DEVICES.index(d)]
        daily = max(1, rng.lognormvariate(math.log(250_000), 1.5) * min(scale, 30) ** 0.5)
        ecpm = (
            sp["ecpm"] * country[c]["ecpm"] * size[z]["ecpm"] * device[d]["ecpm"]
            * placement[p]["ecpm"] * rng.lognormvariate(0, 0.25) * 0.9
        )
        response_rate = min(0.95, rng.uniform(0.08, 0.5) * sp["response"])
        win_rate = rng.uniform(0.12, 0.45)
        per_win = sp.get("impressions_per_win", rng.uniform(0.82, 0.97))
        # A format-wide commercial leak makes size coverage testable rather
        # than relying on incidental labels in five-key combinations.
        margin = sp["margin"] + rng.uniform(-0.03, 0.03) - (0.35 if z == "300x600" else 0)
        floor = _money(_dec(round(rng.uniform(0.1, 1.2) * sp["floor"], 2)) + CENT)
        bid = _money(floor * _dec(round(sp["bid_to_floor"] * rng.uniform(0.95, 1.05), 4)))
        if rng.random() < 0.85:
            days = tuple(range(DAY_COUNT))
        else:
            days = tuple(sorted(rng.sample(range(DAY_COUNT), rng.randint(1, DAY_COUNT - 1))))
        for day in days:
            factor = rng.uniform(0.85, 1.15) * (PARTIAL_DAY_FACTOR if day == DAY_COUNT - 1 else 1)
            day_ecpm = ecpm * rng.uniform(0.93, 1.07)
            if sp.get("spike") and day in (2, 3):
                factor *= 4
                day_ecpm *= 20
            requests = max(
                REQUEST_GRANULARITY,
                round(daily * factor / REQUEST_GRANULARITY) * REQUEST_GRANULARITY,
            )
            responses = int(requests * response_rate)
            wins = int(responses * win_rate)
            impressions = int(wins * per_win)
            rows.append(
                _row(
                    START_DAY + timedelta(days=day), key, requests, responses, wins, impressions,
                    floor, bid, _dec(round(day_ecpm, 4)), _dec(round(margin, 4)),
                )
            )
    _plant_row_traps(rng, rows)
    rows.sort(key=lambda r: tuple(str(r[col]) for col in REQUIRED_COLUMNS[:6]))
    return rows


def _plant_row_traps(rng: random.Random, rows: list[dict[str, object]]) -> None:
    """Row-level problems at real-export rates: rare, scattered, never fatal."""
    full_days = [
        r for r in rows
        if r[InputColumn.DAY] != (START_DAY + timedelta(days=DAY_COUNT - 1)).isoformat()
    ]
    picked = rng.sample(full_days, 260)
    for r in picked[:120]:  # responses above requests
        r[InputColumn.DEMAND_RESPONSES] = int(r[InputColumn.DEMAND_REQUESTS]) + rng.randint(100, 900)
    for r in picked[120:220]:  # profit off by a cent
        r[InputColumn.PROFIT] = _fmt(Decimal(r[InputColumn.PROFIT]) + CENT)
    for r in picked[220:230]:  # profit beyond the rounding bound
        r[InputColumn.PROFIT] = _fmt(Decimal(r[InputColumn.PROFIT]) + Decimal("0.25"))
    # Placeholder country and size labels on a few existing grains.
    taken = {tuple(str(r[c]) for c in REQUIRED_COLUMNS[:6]) for r in rows}
    for r in picked[230:260]:
        label = (InputColumn.COUNTRY, "-") if rng.random() < 0.5 else (InputColumn.SIZE, "9999x9999")
        moved = dict(r)
        moved[label[0]] = label[1]
        grain = tuple(str(moved[c]) for c in REQUIRED_COLUMNS[:6])
        if grain not in taken:
            taken.add(grain)
            rows.append(moved)


def write(seed: int, rows: int, out: Path) -> int:
    data = generate(seed, rows)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow([c.value for c in REQUIRED_COLUMNS])
        for r in data:
            writer.writerow([r[c] for c in REQUIRED_COLUMNS])
    return len(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    count = write(args.seed, args.rows, args.out)
    print(f"{count} synthetic rows written to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
