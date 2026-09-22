"""Who and what is on the tower: names, TLAs, car classes and broadcast credentials.

Everything here is resolved once per session from SessionInfo and never from a frame:
the surname a TLA is built from, the given-name/surname split the battle bar uses, the
class label and colour a car wears (from CarClassColor when the sim has one, else a
palette), and the licence chip and iRating the pop-in shows.
"""

from __future__ import annotations

import re

from ..flair import flag as country_flag
from ..telemetry.frame import SessionInfo

# Fallback class colours, tuned for the dark overlay, used when iRacing's
# CarClassColor is absent or the same for every class (common in single-class).
#
# INDEX 0 IS THE IMPORTANT ONE. A single-make field (which GOW is, and which is
# the common case for a league) has exactly one class, so every car on the board
# wears this first colour: the bar down each row, and the driver names in the
# battle bar (`--cls` in broadcast-overlay.html). It therefore has to be clear of
# every colour that MEANS something, or the whole grid spends the race wearing a
# signal.
#
# It used to be 0xE6194B, which was fine while the brand was amber and is not now:
# the package repainted to the league's crimson #E11D48, and those two are the same
# colour to a viewer. So the crimson is gone from the list entirely rather than
# merely demoted (an 8-class field would have put it back on screen beside the
# brand chrome), and a slate-blue takes the empty slot to keep eight separable
# hues.
#
# The rest of the list still borrows from the semantic set (0x2FD46B is --flag-green,
# 0xB98CFF is --fast, 0x35E3FF is --accent). That is a much older tension and a
# narrower one: it only bites a field with four or more classes, where the class bar
# is a thin strip in a fixed place rather than the colour of a name.
_CLASS_PALETTE = [0x3CB4E6, 0xF5B301, 0x2FD46B, 0xB98CFF, 0xFF8A3D, 0xFF5FA2, 0x35E3FF, 0x8DA0B8]
_DEFAULT_CLASS_COLOR = 0x4C586A


# Generational suffixes that iRacing carries inside UserName. The TLA is built from the
# last name token, so a real entry ("Lucas Bayle II") put "II" on the timing tower.
_NAME_SUFFIXES = {"jr", "sr", "i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x",
                  "2nd", "3rd", "4th"}


def _surname(name: str | None) -> str:
    """The token a TLA should be built from: the last one that isn't a generational
    suffix. iRacing's duplicate-name digits go first ("Bayle2" -> "Bayle"), and a
    one-word name is never stripped, so a driver actually called "V" survives."""
    words = [w for w in (re.sub(r"\d+$", "", t) for t in (name or "").split()) if w]
    while len(words) > 1 and words[-1].strip(".,").lower() in _NAME_SUFFIXES:
        words.pop()
    return words[-1] if words else ""


def _tla(name: str | None, number: str) -> str:
    last = _surname(name)
    return last[:3].upper() if last else "#" + str(number)


def tla_sheet(cars) -> dict[int, str]:
    """Three-letter codes for a whole field, distinct where the surnames are not.

    `_tla` is a pure function of one name, so two drivers whose surnames share their
    first three letters get the same code and the tower shows it twice (Round 1,
    2026-09-20: Josh Wilson and Sjaak Willems, both WIL, two rows apart). The
    convention viewers already know is F1's for the Schumachers: the given name's
    initial and two of the surname, MSC and RSC. So only a clashing pair changes, to
    JWI and SWI, and every other code on the sheet is exactly what `_tla` says.

    `cars` is anything mapping car index to an object with `.name` and `.number`
    (WorldSnapshot.cars). A code chosen for a clash must not itself collide with a
    code already on the sheet; if the initial route cannot separate them either (same
    initial, same surname start), the surname's second and fourth letters stand in,
    and past that the number, which is at least never ambiguous.
    """
    base = {idx: _tla(c.name, c.number) for idx, c in cars.items()}
    out = dict(base)
    counts: dict[str, int] = {}
    for code in base.values():
        counts[code] = counts.get(code, 0) + 1
    for idx in sorted(base, key=lambda i: (str(cars[i].name or ""), i)):
        code = base[idx]
        if counts[code] < 2:
            continue
        first, last = _name_parts(cars[idx].name)
        candidates = [
            (first[:1] + last[:2]).upper() if first and len(last) >= 2 else "",
            (last[:2] + last[3:4]).upper() if len(last) >= 4 else "",
            "#" + str(cars[idx].number),
        ]
        used = {v for i, v in out.items() if i != idx}
        for alt in candidates:
            if alt and alt not in used:
                out[idx] = alt
                break
    return out


def _name_parts(name: str | None) -> tuple[str, str]:
    """(given name, surname), for the battle bar's two-line name treatment.

    Split HERE and not in the page, because the surname is not "the last word": iRacing
    carries generational suffixes, and `_surname` already knows that "Dale Earnhardt Jr"
    is an Earnhardt. Re-deriving that in JavaScript would be a second definition of a rule
    this file already owns, and the two would drift.

    ("", name) for a single-word name, so the bar shows one line rather than an empty slot
    above the name. The suffix itself is dropped, exactly as the TLA drops it: a name bug
    is two lines of big type, not a legal document.
    """
    disp = _display_name(name)
    last = _surname(name)
    words = disp.split()
    if last and last in words:
        return " ".join(words[:words.index(last)]), last
    return "", disp


def _display_name(name: str | None) -> str:
    """Broadcast display name: drop iRacing's duplicate-name digit suffixes and
    single-letter middle initials ("Dan J Smith2" -> "Dan Smith"). TV graphics
    run first + last only - and in the pop-in's uppercase font a lone middle
    initial reads as a stray "|" between the names, which is how this earned
    a fix."""
    words = [w for w in (re.sub(r"\d+$", "", t) for t in (name or "").split()) if w]
    if len(words) > 2:
        words = [words[0]] + [w for w in words[1:-1] if len(w.rstrip(".")) > 1] + [words[-1]]
    return " ".join(words)


# Recognisable iRacing class tokens as (match key, displayed label) pairs, most
# specific first so a car model like "Ferrari 296 GT3" yields "GT3". GT3/GT4/GTE/TCR
# carry the token in the model name; GTP/LMDh prototypes ("Cadillac V-Series.R",
# "Porsche 963") do NOT, but their CarPath usually does ("...gtp"), which is why we
# search the path too, with the hint map below as a final net.
#
# Key and label are deliberately SEPARATE. We match against an uppercased haystack, so
# returning the matched key as the label put a raw "LMDH" on screen: the BMW M Hybrid's
# CarPath is literally "bmwlmdh" and carries no "gtp" anywhere.
#
# GTP, LMDh and Hypercar are also not three classes: they are one kind of car.
# LMDh is the chassis regulation, GTP is IMSA's name for the class it races in, and
# Hypercar is the WEC equivalent; a Porsche 963 is an LMDh car entered as GTP in IMSA
# and as Hypercar in WEC. GTD is likewise IMSA's name for GT3 machinery. We broadcast
# IMSA-style content, so those normalise to GTP and GT3. Please don't split them back
# apart: the regulation is not the class, and the badge should name the class.
_CLASS_TOKENS = [
    ("GTP", "GTP"), ("LMDH", "GTP"), ("HYPERCAR", "GTP"),
    ("LMP2", "LMP2"), ("LMP3", "LMP3"),
    ("GTE", "GTE"), ("GT4", "GT4"), ("GT3", "GT3"), ("GTD", "GT3"), ("TCR", "TCR"),
]

# Prototypes whose model name AND path may carry no class token: map a distinctive
# id substring (matched lowercased against "model path") to a class. iRacing's exact
# strings vary by content, so this is a best-effort net: extend as new cars land.
_MODEL_CLASS_HINTS = [
    # GTP / LMDh: display names often omit the class token and CarPath may be absent in
    # a capture, so key off the manufacturer/model too (each of these races only GTP in
    # current iRacing content, so the make alone is a safe signal).
    ("v-series", "GTP"), ("vseries", "GTP"), ("cadillac", "GTP"),
    ("963", "GTP"), ("m hybrid", "GTP"), ("mhybrid", "GTP"),
    ("arx-06", "GTP"), ("arx06", "GTP"), ("acura", "GTP"),
    ("499p", "GTP"), ("sc63", "GTP"), ("valkyrie", "GTP"), ("a424", "GTP"),
    ("p217", "LMP2"), ("oreca", "LMP2"), ("gibson", "LMP2"),
    ("ligier", "LMP3"), ("js p3", "LMP3"), ("jsp3", "LMP3"), ("duqueine", "LMP3"),
]


def _strip_model(model: str | None) -> str:
    return (model or "").split("(")[0].strip()


def _car_class_token(model: str | None, path: str | None = None) -> str:
    """Class label (GT3/GTP/LMP2/...) for one car, from its model name and CarPath.
    The path is decisive for prototypes whose display name omits the class. Returns the
    canonical label to display, never the raw matched substring."""
    hay = f"{model or ''} {path or ''}".upper()
    for tok, label in _CLASS_TOKENS:
        if tok in hay:
            return label
    low = hay.lower()
    for kw, tok in _MODEL_CLASS_HINTS:
        if kw in low:
            return tok
    return ""


def _shared_class_token(drivers: list) -> str:
    """The class token the cars in a class agree on. A CarClassID is ONE real racing
    class, so a plurality is enough: if a third of them (min one) resolve to the same
    token, use it. That way a GTP grid of mixed manufacturers still labels 'GTP' even
    when iRacing supplies no short name and only some models carry a detectable token
    (the old majority vote fell back to the lead car's make ('Cadillac') instead)."""
    toks = [t for d in drivers if (t := _car_class_token(d.car, d.car_path))]
    if not toks:
        return ""
    best = max(set(toks), key=toks.count)
    return best if toks.count(best) >= max(1, len(drivers) // 3) else ""


def _car_tag(model: str, path: str | None = None) -> str:
    """A short per-row chip: the class token if we can detect one, else the make."""
    return _car_class_token(model, path) or (model.upper().split()[0] if model.split() else "")


def class_meta(info: SessionInfo) -> dict[int, dict]:
    """Map each car index to its class {name, tag, model, color, class_id}.

    Prefers iRacing's real CarClassColor, but falls back to a distinct palette by
    class when the colour is missing or identical across classes (so a multiclass
    field is always visually separable). iRacing often leaves CarClassShortName
    null, so the class *name* falls back to a detected token (GT3/GTP) or the
    class's representative car model; the per-row *tag* is the token or the make."""
    drivers = info.drivers()
    class_ids: list[int | None] = []
    by_class: dict[int | None, list] = {}
    for d in drivers:
        if d.class_id not in class_ids:
            class_ids.append(d.class_id)
        by_class.setdefault(d.class_id, []).append(d)

    # one label per class: real short name, else a detected token (GT3/GTP/...), else
    # the lead car's model
    class_label: dict[int | None, str] = {}
    for cid, ds in by_class.items():
        real = next((x.class_name for x in ds if x.class_name), None)
        class_label[cid] = real or _shared_class_token(ds) or _strip_model(ds[0].car)

    real = {d.class_color for d in drivers if d.class_color}
    # White (0xffffff) is iRacing's "unset" default; treat all-white / all-same as no colour.
    usable = real - {0xFFFFFF}
    use_real = len(class_ids) <= 1 or len(usable) >= max(2, len(class_ids))

    meta: dict[int, dict] = {}
    for d in drivers:
        if use_real and d.class_color and d.class_color != 0xFFFFFF:
            color = d.class_color & 0xFFFFFF
        elif d.class_id in class_ids:
            color = _CLASS_PALETTE[class_ids.index(d.class_id) % len(_CLASS_PALETTE)]
        else:
            color = _DEFAULT_CLASS_COLOR
        model = _strip_model(d.car)
        meta[d.car_idx] = {
            "name": class_label[d.class_id],
            "tag": _car_tag(model, d.car_path),
            "model": model,
            "color": color,
            "class_id": d.class_id,
        }
    return meta


# iRacing's own licence-class colours, read straight off a real official session:
# LicColor was verbatim 0x0153db for A, 0x00c702 for B and 0xfeec04 for C there. R and
# D are the published rookie/orange values (no rookie or D entry in that field to check).
#
# We key the colour off the licence class LETTER rather than reading LicColor, for two
# reasons found in real data: LicColor is the string "0xundefined" in an offline/AI
# session, and even in the official one it disagreed with LicString for 2 of 59 drivers
# (A-class licences carrying the B-class green). The letter is what we print, so the
# letter is what has to pick the colour: a blue "A" beside a green swatch reads as a bug.
_LIC_COLORS = {"R": 0xFC0706, "D": 0xFC8A27, "C": 0xFEEC04, "B": 0x00C702, "A": 0x0153DB}
_LIC_PRO = 0xE9ECF2  # Pro / Pro-WC render as the plain light chip


def _split_licence(lic: str | None) -> tuple[str, str]:
    """"A 3.15" -> ("A", "3.15"). Anything unparseable yields ("", "")."""
    parts = (lic or "").split()
    if not parts:
        return "", ""
    cls = parts[0].upper()
    if not cls.isalpha():
        return "", ""
    return cls, (parts[1] if len(parts) > 1 else "")


def _fmt_irating(v: int | None) -> str:
    """4247 -> "4.2k" for a tight column; under 1000 stays exact. No rating -> "" so
    an AI or unrated entry shows nothing rather than a misleading "0"."""
    if not v or v <= 0:
        return ""
    return f"{v / 1000:.1f}k" if v >= 1000 else str(v)


def driver_meta(info: SessionInfo) -> dict[int, dict]:
    """Map each car index to its driver's broadcast credentials.

    Rides the same session-info refresh path as class_meta (see TowerModel), because
    DriverInfo is re-read every 60s for team driver swaps, and a swap changes the
    credentials as much as it changes the name.
    """
    meta: dict[int, dict] = {}
    for d in info.drivers():
        cls, sr = _split_licence(d.lic_string)
        meta[d.car_idx] = {
            "irating": _fmt_irating(d.irating),
            "licClass": cls,
            "licSR": sr,
            "licColor": _LIC_COLORS.get(cls, _LIC_PRO) if cls else 0,
            "country": d.country or "",
            # a flag reads instantly where a country name has to be squinted at; "" for
            # the non-country flairs ("Global") and the overlay falls back to the name
            "flag": country_flag(d.country),
        }
    return meta
