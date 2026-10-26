"""Deterministic, traffic-distinct selection shared by evidence and report cards."""

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from contracts.enums import DetectorFamily, Dimension


@dataclass(frozen=True)
class Lead:
    key: str
    family: DetectorFamily
    filters: Mapping[Dimension, str]
    period: str
    row_hash: str | None
    totals: tuple | None
    score: Decimal
    eligible: bool


def collapse(leads: list[Lead]) -> tuple[list[Lead], dict[str, list[str]]]:
    """Prefer the simplest label; retain absorbed IDs for the private audit.

    Equal totals are a rounding fallback, not an overlap claim. Periods must
    match and a totals signature must include requests, revenue and profit.
    """
    kept, merged, hashes, totals = [], {}, {}, {}
    for lead in sorted(leads, key=lambda x: (
        # A supply ID is the lever an operator acts on, so it names a tie.
        not x.eligible, len(x.filters), Dimension.SUPPLY_ID not in x.filters,
        -x.score, x.family.value,
        tuple((d.value, v) for d, v in x.filters.items()), x.key,
    )):
        h = (lead.period, lead.row_hash) if lead.row_hash else None
        t = (lead.period, lead.totals) if lead.totals else None
        target = hashes.get(h) if h else None
        target = target or (totals.get(t) if t else None)
        if target:
            merged.setdefault(target, []).append(lead.key)
        else:
            kept.append(lead)
            target = lead.key
        if h:
            hashes[h] = target
        if t:
            totals[t] = target
    return kept, merged


def select(leads: list[Lead], cap: int) -> list[Lead]:
    """Cover available families/country/size, cap supplies, then order by dollars.

    Coverage affects admission, not displayed dollar order. Eligible findings
    always consume available slots before exploratory investigations.
    """
    chosen, supplies, covered = [], Counter(), set()

    def tags(lead):
        return {lead.family, *(d for d in (Dimension.COUNTRY, Dimension.SIZE)
                               if d in lead.filters and len(lead.filters) <= 2)}

    def order(lead):
        return (not lead.eligible, -lead.score, len(lead.filters), lead.family.value, lead.key)

    for eligible in (True, False):
        pool = [x for x in leads if x.eligible is eligible]
        while pool and len(chosen) < cap:
            available = [x for x in pool if supplies[x.filters.get(Dimension.SUPPLY_ID)] < 2
                         or Dimension.SUPPLY_ID not in x.filters]
            if not available:
                break
            lead = min(available, key=lambda x: (-len(tags(x) - covered), *order(x)))
            chosen.append(lead)
            covered.update(tags(lead))
            if Dimension.SUPPLY_ID in lead.filters:
                supplies[lead.filters[Dimension.SUPPLY_ID]] += 1
            pool.remove(lead)
    return sorted(chosen, key=order)


def total_signature(requests, revenue, profit) -> tuple:
    return (int(requests), Decimal(str(revenue)).quantize(Decimal('.01')),
            Decimal(str(profit)).quantize(Decimal('.01')))
