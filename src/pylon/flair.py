"""Driver country flags, from iRacing's `FlairName`.

`DriverInfo.FlairName` is the per-driver country iRacing shows beside a name
("United Kingdom", "Poland"). It is a display string, not a code, so this module
maps it to an ISO 3166-1 alpha-2 code and then to a flag emoji.

Why an emoji and not an image: a flag emoji is one regional-indicator pair, so the
overlay carries no image assets and no per-flag markup, and OBS's browser source
renders it from the system emoji font (Noto Color Emoji on the Linux box). A name
we cannot resolve yields "" and the overlay falls back to printing the name.

The name table is generated from iso-codes' iso_3166-1.json (short/common spellings)
plus the aliases below for iRacing's own wording. Regenerating it is a one-off: this
is static reference data, not something to look up at runtime.
"""

from __future__ import annotations

# Non-country flairs iRacing offers, plus the pace car's placeholder. These are real
# values, not errors: they simply have no flag.
_NOT_A_COUNTRY = {"", "-none-", "none", "global", "unaffiliated"}

# The UK home nations are not in ISO 3166-1, but Unicode has subdivision flags for
# them as tag sequences. iRacing offers Scotland/England/Wales as distinct flairs.
_SUBDIVISION_FLAGS = {
    "scotland": "\U0001F3F4\U000E0067\U000E0062\U000E0073\U000E0063\U000E0074\U000E007F",
    "england": "\U0001F3F4\U000E0067\U000E0062\U000E0065\U000E006E\U000E0067\U000E007F",
    "wales": "\U0001F3F4\U000E0067\U000E0062\U000E0077\U000E006C\U000E0073\U000E007F",
}

_ISO_BY_NAME = {
    "afghanistan": "AF", "albania": "AL", "algeria": "DZ", "american samoa": "AS",
    "andorra": "AD", "angola": "AO", "anguilla": "AI", "antarctica": "AQ",
    "antigua and barbuda": "AG", "argentina": "AR", "armenia": "AM", "aruba": "AW",
    "australia": "AU", "austria": "AT", "azerbaijan": "AZ", "bahamas": "BS",
    "bahrain": "BH", "bangladesh": "BD", "barbados": "BB", "belarus": "BY",
    "belgium": "BE", "belize": "BZ", "benin": "BJ", "bermuda": "BM",
    "bhutan": "BT", "bolivia": "BO", "bolivia, plurinational state of": "BO", "bonaire, sint eustatius and saba": "BQ",
    "bosnia and herzegovina": "BA", "botswana": "BW", "bouvet island": "BV", "brazil": "BR",
    "british indian ocean territory": "IO", "brunei": "BN", "brunei darussalam": "BN", "bulgaria": "BG",
    "burkina faso": "BF", "burma": "MM", "burundi": "BI", "cabo verde": "CV",
    "cambodia": "KH", "cameroon": "CM", "canada": "CA", "cape verde": "CV",
    "cayman islands": "KY", "central african republic": "CF", "chad": "TD", "chile": "CL",
    "china": "CN", "christmas island": "CX", "cocos (keeling) islands": "CC", "colombia": "CO",
    "comoros": "KM", "congo": "CG", "congo, the democratic republic of the": "CD", "cook islands": "CK",
    "costa rica": "CR", "croatia": "HR", "cuba": "CU", "curaçao": "CW",
    "cyprus": "CY", "czech republic": "CZ", "czechia": "CZ", "côte d'ivoire": "CI",
    "denmark": "DK", "djibouti": "DJ", "dominica": "DM", "dominican republic": "DO",
    "east timor": "TL", "ecuador": "EC", "egypt": "EG", "el salvador": "SV",
    "equatorial guinea": "GQ", "eritrea": "ER", "estonia": "EE", "eswatini": "SZ",
    "ethiopia": "ET", "falkland islands (malvinas)": "FK", "faroe islands": "FO", "fiji": "FJ",
    "finland": "FI", "france": "FR", "french guiana": "GF", "french polynesia": "PF",
    "french southern territories": "TF", "gabon": "GA", "gambia": "GM", "georgia": "GE",
    "germany": "DE", "ghana": "GH", "gibraltar": "GI", "great britain": "GB",
    "greece": "GR", "greenland": "GL", "grenada": "GD", "guadeloupe": "GP",
    "guam": "GU", "guatemala": "GT", "guernsey": "GG", "guinea": "GN",
    "guinea-bissau": "GW", "guyana": "GY", "haiti": "HT", "heard island and mcdonald islands": "HM",
    "holland": "NL", "holy see (vatican city state)": "VA", "honduras": "HN", "hong kong": "HK",
    "hungary": "HU", "iceland": "IS", "india": "IN", "indonesia": "ID",
    "iran": "IR", "iran, islamic republic of": "IR", "iraq": "IQ", "ireland": "IE",
    "isle of man": "IM", "israel": "IL", "italy": "IT", "ivory coast": "CI",
    "jamaica": "JM", "japan": "JP", "jersey": "JE", "jordan": "JO",
    "kazakhstan": "KZ", "kenya": "KE", "kiribati": "KI", "korea, democratic people's republic of": "KP",
    "korea, republic of": "KR", "kuwait": "KW", "kyrgyzstan": "KG", "lao people's democratic republic": "LA",
    "laos": "LA", "latvia": "LV", "lebanon": "LB", "lesotho": "LS",
    "liberia": "LR", "libya": "LY", "liechtenstein": "LI", "lithuania": "LT",
    "luxembourg": "LU", "macao": "MO", "macau": "MO", "macedonia": "MK",
    "madagascar": "MG", "malawi": "MW", "malaysia": "MY", "maldives": "MV",
    "mali": "ML", "malta": "MT", "marshall islands": "MH", "martinique": "MQ",
    "mauritania": "MR", "mauritius": "MU", "mayotte": "YT", "mexico": "MX",
    "micronesia, federated states of": "FM", "moldova": "MD", "moldova, republic of": "MD", "monaco": "MC",
    "mongolia": "MN", "montenegro": "ME", "montserrat": "MS", "morocco": "MA",
    "mozambique": "MZ", "myanmar": "MM", "namibia": "NA", "nauru": "NR",
    "nepal": "NP", "netherlands": "NL", "new caledonia": "NC", "new zealand": "NZ",
    "nicaragua": "NI", "niger": "NE", "nigeria": "NG", "niue": "NU",
    "norfolk island": "NF", "north korea": "KP", "north macedonia": "MK", "northern mariana islands": "MP",
    "norway": "NO", "oman": "OM", "pakistan": "PK", "palau": "PW",
    "palestine": "PS", "palestine, state of": "PS", "panama": "PA", "papua new guinea": "PG",
    "paraguay": "PY", "peru": "PE", "philippines": "PH", "pitcairn": "PN",
    "poland": "PL", "portugal": "PT", "puerto rico": "PR", "qatar": "QA",
    "romania": "RO", "russia": "RU", "russian federation": "RU", "rwanda": "RW",
    "réunion": "RE", "saint barthélemy": "BL", "saint helena, ascension and tristan da cunha": "SH", "saint kitts and nevis": "KN",
    "saint lucia": "LC", "saint martin (french part)": "MF", "saint pierre and miquelon": "PM", "saint vincent and the grenadines": "VC",
    "samoa": "WS", "san marino": "SM", "sao tome and principe": "ST", "saudi arabia": "SA",
    "senegal": "SN", "serbia": "RS", "seychelles": "SC", "sierra leone": "SL",
    "singapore": "SG", "sint maarten (dutch part)": "SX", "slovakia": "SK", "slovenia": "SI",
    "solomon islands": "SB", "somalia": "SO", "south africa": "ZA", "south georgia and the south sandwich islands": "GS",
    "south korea": "KR", "south sudan": "SS", "spain": "ES", "sri lanka": "LK",
    "sudan": "SD", "suriname": "SR", "svalbard and jan mayen": "SJ", "swaziland": "SZ",
    "sweden": "SE", "switzerland": "CH", "syria": "SY", "syrian arab republic": "SY",
    "taiwan": "TW", "taiwan, province of china": "TW", "tajikistan": "TJ", "tanzania": "TZ",
    "tanzania, united republic of": "TZ", "thailand": "TH", "the netherlands": "NL", "timor-leste": "TL",
    "togo": "TG", "tokelau": "TK", "tonga": "TO", "trinidad and tobago": "TT",
    "tunisia": "TN", "turkey": "TR", "turkiye": "TR", "turkmenistan": "TM",
    "turks and caicos islands": "TC", "tuvalu": "TV", "türkiye": "TR", "uae": "AE",
    "uganda": "UG", "ukraine": "UA", "united arab emirates": "AE", "united kingdom": "GB",
    "united states": "US", "united states minor outlying islands": "UM", "united states of america": "US", "uruguay": "UY",
    "uzbekistan": "UZ", "vanuatu": "VU", "vatican city": "VA", "venezuela": "VE",
    "venezuela, bolivarian republic of": "VE", "viet nam": "VN", "vietnam": "VN", "virgin islands, british": "VG",
    "virgin islands, u.s.": "VI", "wallis and futuna": "WF", "western sahara": "EH", "yemen": "YE",
    "zambia": "ZM", "zimbabwe": "ZW", "åland islands": "AX",
}


def _repair_mojibake(name: str) -> str:
    """Undo a UTF-8-read-as-latin-1 round trip.

    iRacing's session blob declares ISO_8859_1 and is mostly decoded correctly (driver
    names with umlauts arrive intact), but at least one flair is double-encoded at
    source: "Türkiye" arrives as "TÃ¼rkiye". Repairing it here is cheap and only ever
    applied as a fallback, so a correctly-encoded name is never touched.
    """
    try:
        return name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def _lookup_keys(flair: str | None) -> list[str]:
    """The spellings to try, in order. The mojibake repair runs on the ORIGINAL casing:
    lower-casing first turns "Ã" into "ã", which no longer round-trips through latin-1
    and silently defeats the repair."""
    raw = (flair or "").strip()
    keys = [raw.lower()]
    repaired = _repair_mojibake(raw)
    if repaired != raw:
        keys.append(repaired.lower())
    return keys


def iso_code(flair: str | None) -> str:
    """ISO 3166-1 alpha-2 for an iRacing FlairName, or "" if it isn't a country."""
    for key in _lookup_keys(flair):
        if key in _NOT_A_COUNTRY:
            return ""
        if key in _ISO_BY_NAME:
            return _ISO_BY_NAME[key]
    return ""


def flag(flair: str | None) -> str:
    """Flag emoji for an iRacing FlairName, or "" if we can't resolve one.

    "" is a normal outcome (the "Global" flair, an unknown spelling, an AI entry), and
    the overlay falls back to the country name rather than showing an empty slot.
    """
    for key in _lookup_keys(flair):
        if key in _NOT_A_COUNTRY:
            return ""
        if key in _SUBDIVISION_FLAGS:
            return _SUBDIVISION_FLAGS[key]
    code = iso_code(flair)
    # regional indicators: 'GB' -> U+1F1EC U+1F1E7
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in code) if code else ""
