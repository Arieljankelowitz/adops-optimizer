"""Versioned accepted country references for DQ10.

A label outside the selected reference is *unverified*, not invalid: it is
reported, never dropped. Matching is case-insensitive.
"""

ISO3166_ALPHA2_V1 = frozenset("""
AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL
BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV
CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD
GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM
IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK
LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW
MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR
PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS
ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY
UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW XK
""".split())

COUNTRY_REFERENCES: dict[str, frozenset[str]] = {
    "iso3166_alpha2_v1": ISO3166_ALPHA2_V1,
}


def accepted_countries(reference: str, extra: tuple[str, ...] = ()) -> frozenset[str]:
    """Upper-cased accepted labels for ``reference`` plus documented extras."""
    try:
        base = COUNTRY_REFERENCES[reference]
    except KeyError:
        raise ValueError(f"unknown country reference: {reference!r}") from None
    return base | {label.strip().upper() for label in extra}
