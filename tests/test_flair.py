"""Country flags from iRacing's FlairName.

Every name here is a real value observed in a live official session's DriverInfo
(IMSA at Road America, 60 entries) unless noted.
"""


from pylon.flair import flag, iso_code

# the full set of distinct FlairName values on that grid
REAL_FLAIRS = [
    "-none-", "Austria", "Barbados", "Brazil", "Canada", "Chile", "Christmas Island",
    "Denmark", "France", "Germany", "Global", "Hungary", "Israel", "Italy", "Malaysia",
    "Netherlands", "Poland", "Portugal", "Puerto Rico", "Scotland", "Slovakia", "Spain",
    "TÃ¼rkiye", "Unaffiliated", "United Kingdom", "United States",
]
# the ones that legitimately have no flag: the pace car placeholder and iRacing's
# two non-country flairs
NO_FLAG = {"-none-", "Global", "Unaffiliated"}


def test_every_flair_on_a_real_grid_resolves():
    for name in REAL_FLAIRS:
        got = flag(name)
        if name in NO_FLAG:
            assert got == "", f"{name!r} should have no flag, got {got!r}"
        else:
            assert got, f"{name!r} resolved to no flag"


def test_regional_indicator_pairs():
    assert flag("United Kingdom") == "\U0001F1EC\U0001F1E7"
    assert flag("United States") == "\U0001F1FA\U0001F1F8"
    assert iso_code("Christmas Island") == "CX"        # a real entry on that grid
    assert iso_code("Puerto Rico") == "PR"


def test_mojibake_flair_is_repaired():
    """iRacing's own data ships "Türkiye" double-encoded as "TÃ¼rkiye": verified in
    the session blob, where every other accented string (driver names with umlauts)
    decodes correctly. The repair must survive lower-casing: "Ã".lower() is "ã", which
    no longer round-trips through latin-1."""
    assert iso_code("TÃ¼rkiye") == "TR"
    assert iso_code("Türkiye") == "TR"
    assert flag("TÃ¼rkiye") == flag("Türkiye") != ""


def test_uk_home_nations_use_subdivision_flags():
    """Scotland is a real iRacing flair and is not in ISO 3166-1, so it needs the
    Unicode tag sequence rather than a regional-indicator pair."""
    assert iso_code("Scotland") == ""
    assert flag("Scotland").startswith("\U0001F3F4")
    assert flag("Scotland") != flag("Wales") != flag("England")


def test_common_alternate_spellings():
    """iRacing's wording does not always match the ISO short name."""
    for name, code in [("Russia", "RU"), ("South Korea", "KR"), ("Czech Republic", "CZ"),
                       ("Turkey", "TR"), ("Vietnam", "VN"), ("Taiwan", "TW"),
                       ("Hong Kong", "HK"), ("Ivory Coast", "CI")]:
        assert iso_code(name) == code, name


def test_unknown_and_empty_degrade_to_no_flag():
    for name in ("", None, "   ", "Atlantis", "Not A Country"):
        assert flag(name) == ""
        assert iso_code(name) == ""


# --------------------------------------------------------------------------- #
# Demonyms (#34): the other half of a flair. flair.py resolves a country, and
# Resolving a name to a flag is a display question, decided here and nowhere else.
# --------------------------------------------------------------------------- #


