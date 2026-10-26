"""Synthetic traffic aliases, dollar ordering and coverage admission."""
from decimal import Decimal

from contracts.enums import DetectorFamily as F, Dimension as D
from pipeline.shortlist import Lead, collapse, select, total_signature


def lead(key, family=F.SCALE_WINNER, filters=None, score=10, eligible=True, row_hash=None, totals=None):
    return Lead(key, family, filters or {D.SUPPLY_ID: key}, 'week', row_hash,
                totals, Decimal(score), eligible)


def test_identical_row_sets_collapse_to_simplest_label_across_families():
    supply = lead('simple', filters={D.SUPPLY_ID: 'X'}, row_hash='same')
    banner = lead('banner', F.FLOOR_HIGH_BID, {D.SUPPLY_ID: 'X', D.PLACEMENT_TYPE: 'Banner'}, row_hash='same')
    mobile = lead('mobile', filters={D.SUPPLY_ID: 'X', D.DEVICE_TYPE: 'Mobile'}, row_hash='same')
    kept, merged = collapse([mobile, banner, supply])
    assert kept == [supply]
    assert set(merged['simple']) == {'mobile', 'banner'}


def test_equal_totals_within_rounding_are_aliases_but_not_in_different_periods():
    a = lead('a', totals=total_signature(1000000, '10.001', '3.001'))
    b = lead('b', totals=total_signature(1000000, '10.002', '3.002'))
    kept, merged = collapse([a, b])
    assert len(kept) == 1 and merged == {'a': ['b']}
    c = Lead('c', b.family, b.filters, 'other-week', None, b.totals, b.score, True)
    assert len(collapse([a, c])[0]) == 2


def test_family_and_dimension_coverage_supply_cap_and_eligible_dollar_order():
    leads = [lead(str(i), score=1000-i, filters={D.SUPPLY_ID: 'big', D.COUNTRY: str(i)})
             for i in range(20)]
    leads += [lead('waste', F.WASTED_REQUESTS, score=0),
              lead('margin', F.MARGIN_LEAK, score=5),
              lead('floor', F.FLOOR_HIGH_BID, score=0),
              lead('lost', F.WINS_IMPRESSIONS_DISCREPANCY, score=20),
              lead('size', filters={D.SUPPLY_ID: 'small', D.SIZE: '300x250'}, score=3),
              lead('explore', F.COMBINATION_OUTLIER, score=100000, eligible=False)]
    chosen = select(leads, 8)
    assert sum(x.filters.get(D.SUPPLY_ID) == 'big' for x in chosen) <= 2
    assert {F.WASTED_REQUESTS, F.MARGIN_LEAK, F.FLOOR_HIGH_BID,
            F.WINS_IMPRESSIONS_DISCREPANCY} <= {x.family for x in chosen}
    assert any(D.SIZE in x.filters for x in chosen)
    assert chosen[-1].key == 'explore'
    assert [x.score for x in chosen[:-1]] == sorted((x.score for x in chosen[:-1]), reverse=True)


def test_no_exploratory_lead_displaces_an_eligible_lead():
    leads = [lead(str(i)) for i in range(10)]
    leads.append(lead('explore', F.MARGIN_LEAK, score=10000, eligible=False))
    assert all(x.eligible for x in select(leads, 10))
