import asyncio
import contextlib
import json
from dataclasses import replace
from itertools import pairwise

from conftest import capture_or_skip

from pylon.camera import BridgeClient, BridgeServer
from pylon.overlay.identity import (
    _car_class_token,
    _display_name,
    _strip_model,
    _tla,
    class_meta,
    driver_meta,
)
from pylon.overlay.model import (
    LocalDirector,
    TowerModel,
    _focus_group,
    snapshot_to_model,
)
from pylon.overlay.transport import _OverlayHTTP, _pump_models, serve_live
from pylon.telemetry import SyntheticSource
from pylon.telemetry.frame import SessionInfo
from pylon.world import WorldModel
from pylon.world.model import (
    CarState,
    SessionKind,
    SessionSnapshot,
    WorldSnapshot,
)

MULTICLASS = [("GTP", 0x1FA0FF, 4), ("LMP2", 0x18C29C, 6), ("GTD", 0xF5B301, 10)]


def _last_snapshot(**kw):
    src = SyntheticSource(**kw)
    wm = WorldModel(src.session_info())
    snap = None
    for fr in src.frames():
        snap = wm.update(fr)
    return snap


def test_fonts_get_a_pinned_content_type_on_every_platform():
    """mimetypes reads the REGISTRY on Windows, where .woff2 is normally absent, so
    without pinning, the sim rig serves the bundled flag font as octet-stream while
    Linux serves font/woff2. Nothing enforces a type for @font-face today, which is
    precisely why the difference would go unnoticed until something did."""
    h = _OverlayHTTP.__new__(_OverlayHTTP)  # guess_type touches no instance state
    assert h.guess_type("brand/flags.woff2") == "font/woff2"
    assert h.guess_type("BRAND/FLAGS.WOFF2") == "font/woff2"  # extension case is not data
    # and the charset behaviour it was already doing must survive
    assert "charset=utf-8" in h.guess_type("broadcast-overlay.html")


def test_display_name_drops_initials_and_dup_digits():
    # middle initials look like a stray "|" in the pop-in's uppercase font
    assert _display_name("Dan J Smith") == "Dan Smith"
    assert _display_name("Hugo R. Dias") == "Hugo Dias"
    assert _display_name("Josh Wilson8") == "Josh Wilson"
    assert _display_name("Kai-Marvin Haberlander") == "Kai-Marvin Haberlander"
    assert _display_name("Mj Alvega") == "Mj Alvega"      # two letters is a name, keep it
    assert _display_name(None) == ""


def test_name_splits_into_given_name_and_surname_for_the_battle_bar():
    """The battle bar puts the given name small over the SURNAME large, so it needs the
    two halves. Split here rather than on a last-space in the page: a surname is not the
    last word when iRacing is carrying a generational suffix, and `_surname` already knows
    that. A single-word name yields no given name at all, so the card shows one line
    instead of an empty slot above it."""
    from pylon.overlay.identity import _name_parts

    assert _name_parts("Mike Ulch") == ("Mike", "Ulch")
    assert _name_parts("Dale Earnhardt Jr") == ("Dale", "Earnhardt")   # not ("Dale Earnhardt", "Jr")
    assert _name_parts("Dan J Smith") == ("Dan", "Smith")              # initial dropped with it
    assert _name_parts("Josh Wilson8") == ("Josh", "Wilson")           # dup-name digits gone
    assert _name_parts("Kai-Marvin Haberlander") == ("Kai-Marvin", "Haberlander")
    assert _name_parts("Ulch") == ("", "Ulch")                         # one word: no given name
    assert _name_parts(None) == ("", "")
    assert _name_parts("") == ("", "")


def test_the_popin_carries_the_name_split_and_no_kind_label():
    """The focus cards carry first/last; the tower rows deliberately do not (the row shows
    a TLA). Both "" for an unnamed car, which sends the card back to the one-line form."""
    from dataclasses import replace

    from pylon.director.model import Shot, ShotKind

    cars = [_mk_car(0, 1, 5.5), _mk_car(1, 2, 5.4, gap_ahead=1.0, car_ahead_idx=0)]
    cars[0] = replace(cars[0], name="Mike Ulch")
    cars[1] = replace(cars[1], name=None)
    session = SessionSnapshot(session_time=100.0, flags=0, state=None, is_green=True,
                              is_yellow=False, is_red=False, is_checkered=False,
                              time_remaining=None, laps_remaining=None,
                              is_last_lap=False, event_type="Race")
    snap = WorldSnapshot(tick=1, session_time=100.0, session=session,
                         cars={c.idx: c for c in cars}, order=[0, 1], events=[])

    shot = Shot(ShotKind.BATTLE, "", 1, "", pair=(0, 1))
    group = _focus_group(shot, snap, {}, 4000.0, None)
    named, unnamed = group["cars"]
    assert (named["first"], named["last"]) == ("Mike", "Ulch")
    assert (unnamed["first"], unnamed["last"]) == ("", "")
    # the kind still selects the card's treatment; there is no label text any more
    assert group["kind"] == ShotKind.BATTLE


def test_snapshot_to_model_shape():
    snap = _last_snapshot(num_cars=10, duration_s=30.0, hz=10.0, seed=2)
    m = snapshot_to_model(snap, track="Test Circuit")

    assert m["session"]["track"] == "Test Circuit"
    assert m["session"]["flag"] in {"green", "yellow", "red", "checkered", "none"}
    assert "focus" in m

    cars = m["cars"]
    assert len(cars) == 10
    assert [c["pos"] for c in cars] == list(range(1, 11))       # running order, P1 first
    assert cars[0]["isLeader"] and cars[0]["toLeader"] == 0.0

    # to-leader is non-decreasing down the order
    tl = [c["toLeader"] for c in cars]
    assert all(a <= b + 1e-6 for a, b in pairwise(tl))

    # every row carries what the overlay needs (incl. the new class/lap fields)
    need = {"id", "pos", "num", "tla", "interval", "toLeader", "onPit",
            "classId", "className", "classColor", "classPos", "classLeader", "lapsDown"}
    for c in cars:
        assert need <= c.keys()
        assert isinstance(c["num"], str)
        assert isinstance(c["classColor"], int)
        assert c["lapsDown"] >= 0


def test_multiclass_classes_and_positions():
    """class_meta gives distinct colours per class, and class positions/leaders are
    computed per class from the running order."""
    src = SyntheticSource(num_cars=20, duration_s=40.0, hz=10.0, seed=3, classes=MULTICLASS)
    info = src.session_info()
    meta = class_meta(info)

    # three classes, each a distinct colour taken from the real CarClassColor
    colors = {m["color"] for m in meta.values()}
    names = {m["name"] for m in meta.values()}
    assert names == {"GTP", "LMP2", "GTD"}
    assert len(colors) == 3
    assert 0x1FA0FF in colors and 0xF5B301 in colors

    wm = WorldModel(info)
    snap = None
    for fr in src.frames():
        snap = wm.update(fr)
    cars = snapshot_to_model(snap, classes=meta)["cars"]

    # exactly one class leader per class, and each is that class's first row
    for cls in ("GTP", "LMP2", "GTD"):
        rows = [c for c in cars if c["className"] == cls]
        assert rows, cls
        assert rows[0]["classLeader"] and rows[0]["classPos"] == 1
        assert sum(1 for c in rows if c["classLeader"]) == 1
        assert [c["classPos"] for c in rows] == list(range(1, len(rows) + 1))


def test_class_meta_palette_fallback_without_car_class_color():
    """Single-class / no CarClassColor still yields a usable colour (from the palette)."""
    src = SyntheticSource(num_cars=8, duration_s=10.0, hz=10.0, seed=1)  # default: one class, no color
    meta = class_meta(src.session_info())
    assert meta
    assert all(isinstance(m["color"], int) and m["color"] != 0 for m in meta.values())


def _driver(idx, cid, model, path):
    # Mirrors real iRacing data: CarClassShortName is blank, so the label has to come
    # from the car model (GT3/GT4 carry the token) or CarPath (GTP prototypes).
    return {"CarIdx": idx, "CarNumber": str(idx + 1), "UserName": f"D{idx}",
            "CarClassID": cid, "CarScreenNameShort": model, "CarPath": path,
            "CarClassShortName": "", "CarClassColor": "0xffffff"}


def test_class_labels_resolve_gtp_and_lmp_from_model_and_path():
    """The reported bug: GT3 labelled fine (token in the model name) but GTP not.
    GTP/LMP2 prototypes carry the class only in CarPath or not at all, so we detect
    via path + a prototype hint map."""
    raw = {
        "WeekendInfo": {"TrackDisplayName": "Sebring", "TrackLength": "6.02 km",
                        "EventType": "Race"},
        "DriverInfo": {"Drivers": [
            _driver(0, 10, "Cadillac V-Series.R", "cadillacvseriesrgtp"),  # path has "gtp"
            _driver(1, 10, "BMW M Hybrid V8", "bmwmhybridv8"),             # hint "m hybrid"
            _driver(2, 20, "Dallara P217", "dallarap217"),                 # hint "p217" -> LMP2
            _driver(3, 30, "Porsche 911 GT3 R", "porsche992rgt3"),         # token in model
        ]},
    }
    meta = class_meta(SessionInfo(raw))
    assert meta[0]["name"] == "GTP" and meta[0]["tag"] == "GTP"
    assert meta[1]["name"] == "GTP" and meta[1]["tag"] == "GTP"  # detected without a path token
    assert meta[2]["name"] == "LMP2" and meta[2]["tag"] == "LMP2"
    assert meta[3]["name"] == "GT3" and meta[3]["tag"] == "GT3"


# The real IMSA grid off a live official session (Road America, 60 entries): every
# distinct (CarScreenNameShort, CarPath) pair, which is exactly what _car_class_token
# sees. Kept verbatim: inventing these strings is how the LMDh case was missed.
REAL_IMSA_GRID = [
    ("Acura ARX-06", "acuraarx06gtp", "GTP"),
    ("BMW M Hybrid V8 (EVO)", "bmwlmdh", "GTP"),      # <- path says LMDh, never GTP
    ("Ferrari 499P", "ferrari499p", "GTP"),           # <- neither field says either
    ("Porsche 963", "porsche963gtp", "GTP"),
    ("Dallara P217 LMP2", "dallarap217", "LMP2"),
    ("BMW M4 GT3 EVO", "bmwm4gt3", "GT3"),
    ("Ferrari 296 GT3", "ferrari296gt3", "GT3"),
    ("Ford Mustang GT3", "fordmustanggt3", "GT3"),
    ("McLaren 720S GT3 EVO", "mclaren720sgt3", "GT3"),
    ("Mercedes GT3 2020", "mercedesamgevogt3", "GT3"),
]


def test_class_badge_never_renders_a_regulation_name():
    """The reported bug: the BMW M Hybrid badged 'LMDH'. Its CarPath is 'bmwlmdh' and
    carries no 'gtp', so the token scan matched LMDH and returned that key verbatim as
    the label: beating the hint map, which had the car right all along.

    LMDh is the chassis regulation; GTP is the class. The badge names the class."""
    for model, path, want in REAL_IMSA_GRID:
        got = _car_class_token(_strip_model(model), path)
        assert got == want, f"{model} ({path}) -> {got!r}, want {want!r}"
        assert got not in {"LMDH", "HYPERCAR", "GTD"}, f"{model} shows a regulation name"


def test_hypercar_and_gtd_normalise_to_the_class_name():
    """WEC/IMSA aliases for the same machinery. No car in the grid above exercises
    these, so they are guarded directly rather than left to be discovered on air."""
    assert _car_class_token("Toyota GR010 Hybrid", "toyotagr010hypercar") == "GTP"
    assert _car_class_token("Some GTD entry", "somegtd") == "GT3"


def test_real_multiclass_grid_labels_all_three_classes():
    """End to end through class_meta on the real grid, with iRacing's actual class
    short names present (they ARE populated in an official session, but as 'IMSA23'
    for the GT3 class, which is why the per-row tag is resolved separately)."""
    drivers = []
    for i, (model, path, _want) in enumerate(REAL_IMSA_GRID):
        d = _driver(i, {"GTP": 4029, "LMP2": 2523, "GT3": 4011}[_want], model, path)
        drivers.append(d)
    meta = class_meta(SessionInfo({"WeekendInfo": {}, "DriverInfo": {"Drivers": drivers}}))
    assert [meta[i]["tag"] for i in range(len(REAL_IMSA_GRID))] == \
        [want for _m, _p, want in REAL_IMSA_GRID]


# Verbatim DriverInfo credential fields off a real OFFICIAL session (IMSA at Road
# America). The AI capture in recordings/ cannot validate any of this: it reports
# IRating 0 and LicString "R 0.00" for every entry, with LicColor the string
# "0xundefined", which is exactly why these are pinned here from a live official one.
REAL_CREDENTIALS = [
    # (IRating, LicString, LicColor, FlairName)
    (4245, "A 3.15", 87003, "Italy"),
    (8247, "A 2.61", 50946, "Christmas Island"),   # LicColor disagrees with the letter
    (5023, "B 3.46", 50946, "United Kingdom"),
    (941, "C 1.20", 16706564, "Poland"),
]


def _person(idx, irating, lic, lic_color, flair, **extra):
    return {"CarIdx": idx, "CarNumber": str(idx + 1), "UserName": f"Driver {idx}",
            "CarScreenNameShort": "Ferrari 296 GT3", "CarPath": "ferrari296gt3",
            "CarClassID": 4011, "IRating": irating, "LicString": lic,
            "LicColor": lic_color, "FlairName": flair, **extra}


def _info(drivers):
    return SessionInfo({"WeekendInfo": {}, "DriverInfo": {"Drivers": drivers}})


def test_driver_meta_reads_real_official_credentials():
    """iRating, licence and country all come straight out of DriverInfo: no new
    telemetry channel, and (contrary to the issue's assumption) no /data API call:
    FlairName is iRacing's per-driver country. ClubName is NOT it: it is the literal
    string "None" for all 60 entries even in this official session."""
    info = _info([_person(i, *c, ClubName="None") for i, c in enumerate(REAL_CREDENTIALS)])
    meta = driver_meta(info)

    assert meta[0] == {"irating": "4.2k", "licClass": "A", "licSR": "3.15",
                       "licColor": 0x0153DB, "country": "Italy", "flag": "\U0001F1EE\U0001F1F9"}
    assert meta[1]["irating"] == "8.2k" and meta[1]["country"] == "Christmas Island"
    assert meta[2]["licClass"] == "B" and meta[2]["licColor"] == 0x00C702
    assert meta[3]["irating"] == "941"          # under 1000 stays exact, no "0.9k"
    assert meta[3]["licColor"] == 0xFEEC04      # C class yellow


def test_licence_colour_follows_the_letter_not_liccolor():
    """LicColor is not trustworthy. It is the string "0xundefined" offline, and in the
    real official session two A-class drivers carried the B-class green. We print the
    letter, so the letter picks the colour: a blue 'A' beside a green swatch reads
    as a bug, and a malformed LicColor must not break rendering."""
    _ir, lic, bad_color, flair = REAL_CREDENTIALS[1]
    assert lic.startswith("A") and bad_color == 0x00C702     # iRacing's own contradiction
    meta = driver_meta(_info([_person(0, 8247, lic, bad_color, flair)]))
    assert meta[0]["licColor"] == 0x0153DB                   # A blue, per the letter

    # the offline sentinel: a string where an int belongs, and it must not raise
    meta = driver_meta(_info([_person(0, 0, "R 0.00", "0xundefined", "-none-")]))
    assert meta[0] == {"irating": "", "licClass": "", "licSR": "", "licColor": 0,
                       "country": "", "flag": ""}


def test_ai_and_unrated_entries_show_nothing_not_zero():
    """The AI capture's placeholders must render as absent, never as "0" or "R 0.00"."""
    meta = driver_meta(_info([
        _person(0, 0, "R 0.00", "0xundefined", "", CarIsAI=1),
        _person(1, 1, "R 0.01", 0, "None"),          # the human in that AI race
    ]))
    assert meta[0]["irating"] == "" and meta[0]["licClass"] == ""
    assert meta[1]["irating"] == "1"                 # a real (if silly) rating survives
    assert meta[1]["country"] == ""                  # "None" is a sentinel, not a country


def test_credentials_reach_both_the_tower_rows_and_the_pop_in():
    """Both surfaces the issue asks for, off one resolver on the 60s refresh path."""
    info = _info([_person(i, *c) for i, c in enumerate(REAL_CREDENTIALS)])
    tower = TowerModel(info, shots=LocalDirector())
    cars = [_mk_car(i, i + 1, 5.0 - i * 0.1) for i in range(len(REAL_CREDENTIALS))]
    session = SessionSnapshot(session_time=100.0, flags=0, state=None, is_green=True,
                              is_yellow=False, is_red=False, is_checkered=False,
                              time_remaining=None, laps_remaining=None, is_last_lap=False,
                              event_type="Race")
    snap = WorldSnapshot(tick=1, session_time=100.0, session=session,
                         cars={c.idx: c for c in cars}, order=[0, 1, 2, 3], events=[])
    model = tower.build(snap)

    assert model["cars"][0]["irating"] == "4.2k"
    assert model["cars"][0]["licClass"] == "A" and model["cars"][0]["country"] == "Italy"
    for car in model["focus"]["cars"]:
        assert {"irating", "licClass", "licSR", "licColor", "country"} <= car.keys()


def test_refresh_info_swaps_credentials_with_the_driver():
    """A team driver swap renames the car AND changes whose licence is on screen."""
    tower = TowerModel(_info([_person(0, 4245, "A 3.15", 87003, "Italy")]))
    assert tower.people[0]["irating"] == "4.2k"
    tower.refresh_info(_info([_person(0, 1041, "C 2.00", 16706564, "Japan")]))
    assert tower.people[0] == {"irating": "1.0k", "licClass": "C", "licSR": "2.00",
                               "licColor": 0xFEEC04, "country": "Japan",
                               "flag": "\U0001F1EF\U0001F1F5"}


def _mk_car(idx, pos, progress, *, track_gap_ahead=None, gap_ahead=None, car_ahead_idx=None):
    return CarState(
        idx=idx, number=str(idx), name=f"D{idx}", class_id=1, position=pos,
        class_position=pos, lap=int(progress) + 1, lap_completed=int(progress),
        lap_dist_pct=progress % 1.0, progress=progress, speed=60.0, on_pit_road=False,
        surface=3, on_track=True, gap_ahead=gap_ahead, gap_behind=None, to_leader=None,
        track_gap_ahead=track_gap_ahead, closing_rate=None, car_ahead_idx=car_ahead_idx,
    )


def _session(kind="race", **kw):
    from pylon.world import SessionKind

    return SessionSnapshot(session_time=100.0, flags=0, state=None, is_green=True,
                           is_yellow=False, is_red=False, is_checkered=False,
                           time_remaining=None, laps_remaining=None, is_last_lap=False,
                           event_type="Race",
                           session_kind=getattr(SessionKind, kind.upper()), **kw)


def test_a_garaged_car_keeps_its_row_and_its_purple_in_practice():
    """#46: a car in the garage is classified on the time it set, so it holds its place on
    the tower: including the top of it, and including the fastest-lap marker. The world
    model leaves it out of the running order (it is racing nobody), so the tower has to
    classify off the CARS rather than the order."""
    parked = replace(_mk_car(0, 0, 4.0), best_lap=130.5, last_lap=131.0,
                     in_garage=True, on_track=False, speed=0.0, surface=-1)
    running = [replace(_mk_car(1, 0, 5.5), best_lap=131.8, last_lap=132.0),
               replace(_mk_car(2, 0, 5.4), best_lap=133.2, last_lap=133.4)]
    snap = WorldSnapshot(tick=1, session_time=100.0, session=_session("practice"),
                         cars={c.idx: c for c in [parked, *running]},
                         order=[1, 2], events=[])        # the garaged car is NOT in order
    rows = snapshot_to_model(snap)["cars"]

    assert [r["id"] for r in rows] == [0, 1, 2]      # classified by lap time: 130.5 first
    top = rows[0]
    assert top["garage"] is True and top["fastest"] is True
    assert top["bestLap"] == 130.5 and top["lastLap"] == 131.0
    assert top["onPit"] is False and top["battle"] is False
    assert top["toLeader"] == 0.0                     # not in the order: no gap to anyone
    assert all(r["garage"] is False for r in rows[1:])


def test_the_race_tower_is_still_exactly_the_running_order():
    """The regression the above is most likely to cause: in a race the rows are the running
    order and nothing else, in that order, however the field's lap times compare."""
    cars = [_mk_car(0, 1, 5.5), _mk_car(1, 2, 5.4, gap_ahead=1.0, car_ahead_idx=0),
            _mk_car(2, 3, 5.3, gap_ahead=1.0, car_ahead_idx=1)]
    cars[2] = replace(cars[2], best_lap=120.0)        # the quickest lap, still P3
    snap = WorldSnapshot(tick=1, session_time=100.0, session=_session("race"),
                         cars={c.idx: c for c in cars}, order=[0, 1, 2], events=[])
    rows = snapshot_to_model(snap)["cars"]

    assert [r["id"] for r in rows] == [0, 1, 2]
    assert [r["pos"] for r in rows] == [1, 2, 3]
    assert rows[2]["fastest"] is True and not any(r["garage"] for r in rows)


def test_a_battle_is_a_race_thing_only():
    """#53: two cars nose to tail in practice or qualifying are a hot lap and an out lap,
    not a fight. Neither the row highlight nor the two-car pop-in bar may appear there,
    and the director keeps sending pairs in practice until #49 lands, so the overlay has to
    be right about a shot it should never have been handed."""
    from pylon.director.model import Shot, ShotKind

    def model(kind):
        cars = [replace(_mk_car(0, 0 if kind != "race" else 1, 5.50), best_lap=131.0),
                replace(_mk_car(1, 0 if kind != "race" else 2, 5.49,
                                track_gap_ahead=0.31, gap_ahead=0.4, car_ahead_idx=0),
                        best_lap=131.4)]
        snap = WorldSnapshot(tick=1, session_time=100.0, session=_session(kind),
                             cars={c.idx: c for c in cars}, order=[0, 1], events=[])
        shot = Shot(ShotKind.BATTLE, "b", 1, "", pair=(0, 1))
        return snapshot_to_model(snap, focus=_focus_group(shot, snap, {}, 4000.0, None))

    race = model("race")
    assert [r["battle"] for r in race["cars"]] == [False, True]   # the chasing car's row
    assert len(race["focus"]["cars"]) == 2 and "gap" in race["focus"]

    for kind in ("practice", "qualify", "warmup", "testing"):
        m = model(kind)
        assert not any(r["battle"] for r in m["cars"]), kind
        # one card, on the camera's own car, and no gap chip to imply a fight
        assert [c["id"] for c in m["focus"]["cars"]] == [1], kind
        assert m["focus"]["cars"][0]["onCam"] is True, kind
        assert "gap" not in m["focus"], kind


def test_interval_column_is_gap_to_car_ahead_not_leader():
    """Endurance fix: the gap column is the interval to the car directly ahead
    (prefer the live on-track gap), and a lapped car reads '+1 L' to the car ahead
    rather than '+5 LAPS' to the leader."""
    cars = [
        _mk_car(0, 1, 5.50),
        _mk_car(1, 2, 5.40, track_gap_ahead=0.8, gap_ahead=1.2, car_ahead_idx=0),
        _mk_car(2, 3, 4.30, gap_ahead=30.0, car_ahead_idx=1),  # a lap down, no live gap
    ]
    session = SessionSnapshot(session_time=100.0, flags=0, state=None, is_green=True,
                              is_yellow=False, is_red=False, is_checkered=False,
                              time_remaining=None, laps_remaining=None, is_last_lap=False,
                              event_type="Race")
    snap = WorldSnapshot(tick=1, session_time=100.0, session=session,
                         cars={c.idx: c for c in cars}, order=[0, 1, 2], events=[])
    rows = {c["id"]: c for c in snapshot_to_model(snap)["cars"]}

    assert rows[1]["interval"] == 0.8 and rows[1]["intervalLaps"] == 0   # prefers live gap
    assert rows[2]["interval"] == 30.0 and rows[2]["intervalLaps"] == 1  # +1 L to the car ahead


def test_session_model_carries_clock_and_replay_flag():
    """The overlay header (event clock + LIVE/REPLAY badge) needs these fields."""
    cars = [_mk_car(0, 1, 5.5), _mk_car(1, 2, 5.4, gap_ahead=1.0, car_ahead_idx=0)]
    session = SessionSnapshot(session_time=340.0, flags=0, state=None, is_green=True,
                              is_yellow=False, is_red=False, is_checkered=False,
                              time_remaining=1460.0, laps_remaining=None, is_last_lap=False,
                              event_type="Race", time_total=1800.0, is_replay=True)
    snap = WorldSnapshot(tick=1, session_time=340.0, session=session,
                         cars={c.idx: c for c in cars}, order=[0, 1], events=[])
    s = snapshot_to_model(snap)["session"]

    assert s["replay"] is True
    assert s["timeRemaining"] == 1460.0 and s["timeTotal"] == 1800.0
    assert s["elapsed"] == 340.0            # total - remaining for a timed race


def _last_lap_session(**kw):
    """A session snapshot with the flag/last-lap knobs exposed, everything else quiet."""
    base = {"session_time": 340.0, "flags": 0, "state": None, "is_green": True,
            "is_yellow": False, "is_red": False, "is_checkered": False,
            "time_remaining": 60.0, "laps_remaining": 0, "is_last_lap": True,
            "event_type": "Race", "time_total": 1800.0,
            "session_kind": SessionKind.RACE}
    return SessionSnapshot(**{**base, **kw})


def _session_model(session):
    cars = [_mk_car(0, 1, 5.5), _mk_car(1, 2, 5.4, gap_ahead=1.0, car_ahead_idx=0)]
    snap = WorldSnapshot(tick=1, session_time=340.0, session=session,
                         cars={c.idx: c for c in cars}, order=[0, 1], events=[])
    return snapshot_to_model(snap)["session"]


def test_the_last_lap_reaches_the_cap_as_words_and_a_white_bar():
    """#65: the director acted on is_last_lap and the graphics did
    not, so the voice called the last lap while the tower showed green."""
    s = _session_model(_last_lap_session())
    assert s["lastLap"] is True
    assert s["flag"] == "white"


def test_a_caution_on_the_last_lap_keeps_the_caution():
    """White is not a state of the TRACK (it says how much racing is left), so the
    more urgent condition keeps the bar. The words carry the last lap regardless, which
    is the whole reason the two are separate fields."""
    s = _session_model(_last_lap_session(is_yellow=True, is_green=False))
    assert s["flag"] == "yellow"
    assert s["lastLap"] is True


def test_the_checkered_ends_the_last_lap_rather_than_sharing_it_with_white():
    """Once the flag is out "one lap to go" is false, and --flag-checkered is a
    near-white: a cap still saying LAST LAP as the winner crosses is wrong on air."""
    s = _session_model(_last_lap_session(is_checkered=True, is_green=False))
    assert s["flag"] == "checkered"
    assert s["lastLap"] is False


def test_a_practice_session_never_wears_a_final_lap():
    """is_last_lap is true at the end of a practice session too (laps_remaining hits 0),
    and there is no last lap there in any sense a viewer means."""
    for kind in (SessionKind.PRACTICE, SessionKind.QUALIFY):
        s = _session_model(_last_lap_session(session_kind=kind))
        assert s["lastLap"] is False, kind
        assert s["flag"] == "green", kind


def test_tower_model_tracks_camera_focus():
    """The recording path runs a local director so each model is tagged with the
    on-camera car(s); a battle yields two, everything else one."""
    src = SyntheticSource(num_cars=16, duration_s=60.0, hz=10.0, seed=5, classes=MULTICLASS)
    info = src.session_info()
    tower = TowerModel(info, shots=LocalDirector())  # a recording: mirror the director
    wm = WorldModel(info)
    focuses = []
    for fr in src.frames():
        m = tower.build(wm.update(fr))
        if m["focus"] is not None:
            focuses.append(m["focus"])

    assert focuses, "director never produced a focused shot"
    car_keys = {"id", "num", "tla", "full", "pos", "classPos", "className",
                "carTag", "carModel", "classColor", "onCam"}
    for f in focuses:
        assert f["kind"] in {"leader", "battle", "follow", "incident", "trouble"}
        assert len(f["cars"]) == (2 if f["kind"] == "battle" else 1)   # battle = two pop-ins
        assert sum(1 for c in f["cars"] if c["onCam"]) == 1            # exactly one on camera
        assert f["onCamId"] in {c["id"] for c in f["cars"]}
        for c in f["cars"]:
            assert car_keys <= c.keys()
            assert 0 <= c["id"] < 16


def test_focus_group_shapes_by_shot():
    """_focus_group: a battle carries both pair cars (target flagged onCam); else one."""
    from pylon.director.model import Shot, ShotKind
    from pylon.overlay.model import _focus_group

    src = SyntheticSource(num_cars=12, duration_s=20.0, hz=10.0, seed=2, classes=MULTICLASS)
    info = src.session_info()
    classes = class_meta(info)
    wm = WorldModel(info)
    snap = None
    for fr in src.frames():
        snap = wm.update(fr)

    a, b = snap.order[3], snap.order[4]
    battle = _focus_group(Shot(ShotKind.BATTLE, "b", b, "", pair=(a, b)), snap, classes)
    assert battle["kind"] == "battle" and battle["onCamId"] == b
    assert [c["id"] for c in battle["cars"]] == [a, b]
    assert [c["onCam"] for c in battle["cars"]] == [False, True]

    leader = _focus_group(Shot(ShotKind.LEADER, "l", snap.order[0], ""), snap, classes)
    assert len(leader["cars"]) == 1 and leader["cars"][0]["onCam"]
    assert _focus_group(None, snap, classes) is None


def _battle_snap(*, ahead_progress: float, behind_progress: float):
    """A two-car battle: car 0 is P1 on the TIMING SHEET, car 1 is P2.

    Built through the world model's own `live_order` / `live_places`, so the snapshot
    is the one the builder would really emit for this pair: including the fact that
    a completed on-track pass turns the running order over before the sheet does.
    Hand-writing `position` here instead would let these tests keep passing against a
    snapshot the pipeline can no longer produce.
    """
    from pylon.telemetry.constants import TrackSurface
    from pylon.world.builder import live_order, live_places
    from pylon.world.model import CarState, SessionSnapshot, WorldSnapshot

    sheet = {0: 1, 1: 2}                       # CarIdxPosition, straight off the wire
    progress = {0: ahead_progress, 1: behind_progress}
    order, _held = live_order([0, 1], progress)
    places, class_places = live_places(order, sheet, {0: 1, 1: 1}, sheet)

    def car(idx):
        return CarState(
            idx=idx, number=str(idx + 1), name=f"Driver {idx}", class_id=1,
            position=places[idx], class_position=class_places[idx], lap=6, lap_completed=5,
            lap_dist_pct=progress[idx] % 1.0, progress=progress[idx], speed=60.0,
            on_pit_road=False, surface=TrackSurface.ON_TRACK, on_track=True, gap_ahead=None,
            gap_behind=None, to_leader=None, track_gap_ahead=0.1, closing_rate=None,
            car_ahead_idx=None,
        )

    session = SessionSnapshot(
        session_time=100.0, flags=0, state=None, is_green=True, is_yellow=False,
        is_red=False, is_checkered=False, time_remaining=None, laps_remaining=None,
        is_last_lap=False, event_type="Race",
    )
    return WorldSnapshot(tick=1, session_time=100.0, session=session,
                         cars={i: car(i) for i in (0, 1)}, order=order, events=[])


def test_battle_popins_read_the_timing_sheet_while_nobody_has_passed():
    from pylon.director.model import Shot, ShotKind
    from pylon.overlay.model import _focus_group

    snap = _battle_snap(ahead_progress=5.500, behind_progress=5.4985)
    focus = _focus_group(Shot(ShotKind.BATTLE, "b", 1, "", pair=(0, 1)), snap, {})
    assert [c["id"] for c in focus["cars"]] == [0, 1]
    assert [c["pos"] for c in focus["cars"]] == [1, 2]


def test_battle_popins_turn_over_with_the_pass_not_with_the_timing_line():
    """The reported bug: the rear car goes through and the pop-ins keep showing the old
    order, because CarIdxPosition does not flip until the pair crosses the line: most
    of a lap later on a long circuit. The running order turns over on the move, so the
    graphic agrees with the picture."""
    from pylon.director.model import Shot, ShotKind
    from pylon.overlay.model import _focus_group

    # car 1 is clear ahead on the road; the sheet still calls it P2
    snap = _battle_snap(ahead_progress=5.500, behind_progress=5.504)
    focus = _focus_group(Shot(ShotKind.BATTLE, "b", 0, "", pair=(0, 1)), snap, {})

    assert [c["id"] for c in focus["cars"]] == [1, 0]      # the passer leads the cards
    assert [c["pos"] for c in focus["cars"]] == [1, 2]     # ...and holds the place it took
    assert [c["classPos"] for c in focus["cars"]] == [1, 2]
    assert focus["onCamId"] == 0                            # camera on the car that lost it


def test_battle_popins_and_the_tower_never_disagree_about_a_place():
    """The bug this whole path exists to prevent: the name bug saying P1 over a car the
    tower lists P2. Measured on capture2 before the running order went live: 60% of all
    battle ticks, and 85 seconds on one pass at Spa.

    Checked in both states (before anyone has passed, and with the move completed but
    the timing sheet not yet caught up), because the second is exactly where the two
    graphics used to be numbered from different sources.
    """
    from pylon.director.model import Shot, ShotKind
    from pylon.overlay.model import _focus_group, snapshot_to_model

    for behind in (5.4985, 5.504):            # nose-to-tail, then a completed pass
        snap = _battle_snap(ahead_progress=5.500, behind_progress=behind)
        focus = _focus_group(Shot(ShotKind.BATTLE, "b", 0, "", pair=(0, 1)), snap, {})
        model = snapshot_to_model(snap, "Spa", focus=focus)
        tower = {row["id"]: row for row in model["cars"]}
        for card in focus["cars"]:
            assert card["pos"] == tower[card["id"]]["pos"]
            assert card["classPos"] == tower[card["id"]]["classPos"]
        # ...and the place is a real one: no two cars wearing the same number
        places = [row["pos"] for row in model["cars"]]
        assert places == sorted(places) and len(set(places)) == len(places)


def test_a_nose_ahead_is_not_a_pass_for_the_popins():
    """Side-by-side cars trade inches all the way through a corner; the name bugs must
    not swap back and forth with them."""
    from pylon.director.model import Shot, ShotKind
    from pylon.overlay.model import _focus_group
    from pylon.world.builder import LIVE_PASS_TAKE

    nose = 5.500 + LIVE_PASS_TAKE / 2                       # a nose, no more
    snap = _battle_snap(ahead_progress=5.500, behind_progress=nose)
    focus = _focus_group(Shot(ShotKind.BATTLE, "b", 1, "", pair=(0, 1)), snap, {})
    assert [c["id"] for c in focus["cars"]] == [0, 1]
    assert [c["pos"] for c in focus["cars"]] == [1, 2]


def test_battle_popins_ignore_a_stale_pair_order_from_the_shot_link():
    """The overlay reads the shot over a file/bridge link, so its pair tuple can predate
    the last swap. The cards are ordered from the snapshot, never from that tuple."""
    from pylon.director.model import Shot, ShotKind
    from pylon.overlay.model import _focus_group

    snap = _battle_snap(ahead_progress=5.500, behind_progress=5.504)
    stale = Shot(ShotKind.BATTLE, "b", 0, "", pair=(1, 0))   # written before the flip
    focus = _focus_group(stale, snap, {})
    assert [c["id"] for c in focus["cars"]] == [1, 0]
    assert [c["pos"] for c in focus["cars"]] == [1, 2]


def test_bridge_echoes_real_shot_to_overlay():
    """The sync fix: the director's shot rides the bridge frame stream, so an overlay
    client sees the *real* on-camera car(s), not a simulated guess."""
    from pylon.director.model import Shot, ShotKind
    from pylon.overlay.model import _shot_from_echo

    async def go():
        server = BridgeServer(source=SyntheticSource(num_cars=8, duration_s=40.0, hz=10.0, seed=1),
                              host="127.0.0.1", port=8799, rate=100)
        async with server.ws_server():
            director = BridgeClient("ws://127.0.0.1:8799")
            await director.connect()
            await director.send_shot(Shot(ShotKind.BATTLE, "b", 5, "", pair=(4, 5)))
            await asyncio.sleep(0.1)               # let the server record the shot
            overlay = BridgeClient("ws://127.0.0.1:8799")
            await overlay.connect()
            seen = None
            async for _ in overlay.frames():
                if overlay.latest_shot is not None:
                    seen = overlay.latest_shot
                    break
            await overlay.aclose()
            await director.aclose()
            return seen

    seen = asyncio.run(go())
    assert seen is not None and seen["target"] == 5 and seen["pair"] == [4, 5]
    shot = _shot_from_echo(seen)
    assert shot.kind == "battle" and shot.target_idx == 5 and shot.pair == (4, 5)


def test_live_overlay_builds_models_from_bridge_stream():
    """The live overlay path: telemetry streamed off a bridge becomes tower models,
    same shape the browser renders, via the shared world model. No recording, no OBS."""
    def make():
        return SyntheticSource(num_cars=10, duration_s=20.0, hz=10.0, seed=4, classes=MULTICLASS)

    async def go():
        server = BridgeServer(source=make(), host="127.0.0.1", port=8798)
        models: list[dict] = []
        async with server.ws_server():
            client = BridgeClient("ws://127.0.0.1:8798")
            await client.connect()

            async def collect(model):
                models.append(model)

            await _pump_models(client, collect)
            await client.aclose()
        return models

    models = asyncio.run(go())
    assert len(models) > 10
    last = models[-1]
    assert last["session"]["track"] == "Test Circuit"
    assert len(last["cars"]) == 10
    assert [c["pos"] for c in last["cars"]] == list(range(1, 11))   # running order
    assert last["cars"][0]["isLeader"]
    assert last["cars"][0]["className"] == "GTP"       # class info survives the bridge


def test_the_overlay_world_model_is_not_fed_during_an_instant_replay():
    """The director's tape is elsewhere: the frames describe the replayed moment. The
    page keeps the last live board under the letterbox rather than a board that
    re-counts the excursion and derives every speed across the seek home (#18, the
    same rule the director applies)."""
    from pylon.overlay import transport as T

    class Client:
        """Frames with their own clock, and the director's echo per frame."""
        def __init__(self):
            src = SyntheticSource(num_cars=6, duration_s=30.0, hz=10.0, seed=2)
            self.info = src.session_info()
            self.tape = list(src.frames())
            self.latest_shot = None

        async def frames(self):
            live = {"kind": "leader", "target": 0, "pair": None}
            for k in range(100):
                self.latest_shot = live
                yield self.tape[k]
            for k in range(40, 60):                 # the replay: back to t=4..6
                self.latest_shot = {**live, "replay": {"active": True, "phase": "rolling",
                                                       "key": "contact:1:5.0", "kind": "contact"}}
                yield self.tape[k]
            for k in range(100, 160):
                self.latest_shot = {**live, "replay": None}
                yield self.tape[k]

    fed: list[float] = []
    resyncs: list[int] = []

    class Spy(T.WorldModel):
        def update(self, frame):
            fed.append(frame.session_time)
            return super().update(frame)

        def resync(self):
            resyncs.append(len(fed))
            super().resync()

    models: list[dict] = []

    async def collect(model):
        models.append(model)

    # the director's echo is the client's; a shotlink file another test left behind
    # must not stand in for it
    real, T.WorldModel = T.WorldModel, Spy
    real_read, T.read_shot = T.read_shot, lambda: None
    try:
        asyncio.run(_pump_models(Client(), collect))
    finally:
        T.WorldModel, T.read_shot = real, real_read

    assert fed == sorted(fed), "a replayed (earlier) frame reached the overlay's model"
    assert len(fed) == 160
    assert resyncs == [100]
    # every frame still produced a model for the page, the replayed ones carrying the
    # banner over the last LIVE board
    assert len(models) == 180
    during = models[100:120]
    assert all(m["instantReplay"] and m["instantReplay"]["active"] for m in during)
    assert all(m["cars"] == models[99]["cars"] for m in during)
    assert models[120]["instantReplay"] is None


def test_serve_live_is_the_whole_worker_not_just_the_pump():
    """`pylon overlay --bridge` IS serve_live, and it once failed on its first line
    with a ModuleNotFoundError that nothing here caught: the pump above was tested,
    the entry point around it was not, and a lazy relative import went stale when
    overlay.py became a package. So run the real thing: a bridge, serve_live against
    it, one browser-shaped client on its WebSocket, one model out."""
    import websockets

    def make():
        return SyntheticSource(num_cars=6, duration_s=20.0, hz=10.0, seed=4)

    async def go():
        server = BridgeServer(source=make(), host="127.0.0.1", port=8806)
        async with server.ws_server():
            overlay = asyncio.create_task(serve_live(
                "ws://127.0.0.1:8806", host="127.0.0.1", ws_port=8807, http_port=8808))
            try:
                for _ in range(50):        # the ws port opens a beat after the task starts
                    try:
                        ws = await websockets.connect("ws://127.0.0.1:8807")
                        break
                    except OSError:
                        await asyncio.sleep(0.1)
                async with ws:
                    return json.loads(await asyncio.wait_for(ws.recv(), 10))
            finally:
                overlay.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await overlay

    model = asyncio.run(go())
    assert model["session"]["track"] == "Test Circuit"
    assert [c["pos"] for c in model["cars"]] == list(range(1, 7))


def test_live_overlay_badges_live_even_though_the_sim_says_replay():
    """The live path must badge LIVE. A spectator client watches through iRacing's
    replay viewer parked at the live edge, so every frame carries IsReplayPlaying=1
    and the badge sat on REPLAY through a wholly live broadcast (#44). The badge is a
    director decision, not a sim state, so the live path forces it off."""
    class _SpectatorSource:
        """A synthetic feed stamped the way a broadcast client's really arrives."""
        def __init__(self):
            self._inner = SyntheticSource(num_cars=8, duration_s=8.0, hz=10.0, seed=7)

        def session_info(self):
            return self._inner.session_info()

        def frames(self):
            for fr in self._inner.frames():
                yield replace(fr, values={**fr.values, "IsReplayPlaying": 1})

    async def go():
        server = BridgeServer(source=_SpectatorSource(), host="127.0.0.1", port=8796)
        models: list[dict] = []
        async with server.ws_server():
            client = BridgeClient("ws://127.0.0.1:8796")
            await client.connect()

            async def collect(model):
                models.append(model)

            await _pump_models(client, collect)
            await client.aclose()
        return models

    models = asyncio.run(go())
    assert models, "live pump produced no models"
    # the sim really is reporting a replay...
    assert all(not m["session"]["replay"] for m in models)   # ...and the badge still reads LIVE


def test_recording_playback_still_badges_replay():
    """The other half of #44: forcing the live badge off must not disarm serve()'s
    force_replay=True. Playing back a capture IS tape, whatever the captured flag said."""
    src = SyntheticSource(num_cars=8, duration_s=8.0, hz=10.0, seed=7)
    info = src.session_info()
    tower = TowerModel(info, shots=LocalDirector(), force_replay=True)  # serve(), per pass
    wm = WorldModel(info)
    models = [tower.build(wm.update(fr)) for fr in src.frames()]

    assert models and all(m["session"]["replay"] for m in models)


def test_tla_skips_generational_suffixes():
    """A real entry, "Lucas Bayle II", showed as "II" on the timing tower: the TLA is
    built from the last name token, and his last token is a generational suffix."""
    assert _tla("Lucas Bayle II", "14") == "BAY"
    assert _tla("Dale Earnhardt Jr", "3") == "EAR"
    assert _tla("Dale Earnhardt Jr.", "3") == "EAR"
    assert _tla("Thurston Howell III", "7") == "HOW"
    # the full name keeps the suffix (it is part of how the driver is announced
    assert _display_name("Lucas Bayle II") == "Lucas Bayle II"
    # unaffected cases
    assert _tla("Bartek Kaptur", "11") == "KAP"
    assert _tla("Joao Pedro Teixeira", "9") == "TEI"
    assert _tla("Hector M Alvarez", "16") == "ALV"
    assert _tla("Bayle2", "14") == "BAY"          # iRacing duplicate-name digits
    assert _tla("V", "5") == "V"                  # one-word name is never stripped
    assert _tla("", "42") == "#42"
    assert _tla(None, "42") == "#42"


def test_session_model_names_which_session_is_running():
    """The header has to say PRACTICE / QUALIFYING out loud: a practice tower has the
    same rows, gaps and clock as a race one, and nothing else on screen distinguishes
    them (practice does not even report positions) DESIGN.md 14)."""
    from pylon.world import SessionKind

    cars = [_mk_car(0, 1, 5.5), _mk_car(1, 2, 5.4, gap_ahead=1.0, car_ahead_idx=0)]

    def _session(**kw):
        s = SessionSnapshot(session_time=100.0, flags=0, state=None, is_green=True,
                            is_yellow=False, is_red=False, is_checkered=False,
                            time_remaining=None, laps_remaining=None, is_last_lap=False,
                            event_type="Race", **kw)
        snap = WorldSnapshot(tick=1, session_time=100.0, session=s,
                             cars={c.idx: c for c in cars}, order=[0, 1], events=[])
        return snapshot_to_model(snap)["session"]

    quali = _session(session_kind=SessionKind.QUALIFY, session_type="Lone Qualify")
    assert quali["type"] == "qualify"
    assert quali["typeLabel"] == "QUALIFYING"     # broadcast wording, not "Lone Qualify"

    assert _session(session_kind=SessionKind.PRACTICE)["typeLabel"] == "PRACTICE"
    assert _session(session_kind=SessionKind.RACE)["typeLabel"] == "RACE"

    # an iRacing session type we do not classify is still printed, not dropped
    odd = _session(session_kind=SessionKind.UNKNOWN, session_type="Heat Shootout")
    assert odd["typeLabel"] == "HEAT SHOOTOUT" and odd["type"] is None


def _practice_snap(best_laps, *, kind="practice", positions=None):
    """A non-race snapshot. `best_laps` is per-car (None = no valid lap yet), and
    positions default to 0 for the whole field, which is what practice really reports.

    `kind` takes the constant directly rather than defaulting through `or`: SessionKind
    .UNKNOWN is the empty string, so `kind or SessionKind.PRACTICE` silently turns the
    unknown-session case into a practice one, which is the case that most needs testing,
    because UNKNOWN deliberately counts as a race.
    """
    from dataclasses import replace

    n = len(best_laps)
    pos = positions if positions is not None else [0] * n
    # progress DESCENDING, so `snap.order` (which falls back to track progress when
    # nobody is scored) is the opposite of the lap-time order for every case below:
    # any test that passes by accident would have to pass through that.
    cars = [_mk_car(i, pos[i], 5.9 - i * 0.1) for i in range(n)]
    cars = [replace(c, best_lap=bl) for c, bl in zip(cars, best_laps)]
    session = SessionSnapshot(session_time=100.0, flags=0, state=None, is_green=True,
                             is_yellow=False, is_red=False, is_checkered=False,
                             time_remaining=600.0, laps_remaining=None,
                             is_last_lap=False, event_type="Race", session_kind=kind)
    return WorldSnapshot(tick=1, session_time=100.0, session=session,
                         cars={c.idx: c for c in cars},
                         order=[c.idx for c in cars], events=[])


def test_a_non_race_tower_is_classified_by_lap_time_not_track_progress():
    """Practice reports `CarIdxPosition == 0` for the whole field, so the running order
    can only fall back to track progress: P1 becomes whoever is furthest around the lap
    at this instant, it reorders constantly, and it means nothing. Every real practice and
    qualifying timing screen classifies by lap time instead."""
    model = snapshot_to_model(_practice_snap([137.0, 135.5, 136.2]))
    assert [r["id"] for r in model["cars"]] == [1, 2, 0]      # quickest first
    assert [r["lapGap"] for r in model["cars"]] == [0.0, 0.7, 1.5]
    assert [r["bestLap"] for r in model["cars"]] == [135.5, 136.2, 137.0]
    # nobody LEADS a practice session, so no row wears the leader treatment...
    assert not any(r["isLeader"] for r in model["cars"])
    # ...and the quickest car is marked by the purple it earned instead. In a lap-time
    # classification the fastest-lap holder is the top row by construction.
    assert model["cars"][0]["fastest"] and model["session"]["fastestLap"] == 135.5
    # the race relatives are not quietly left holding a number the page might print
    assert [r["interval"] for r in model["cars"]] == [0.0, 0.0, 0.0]
    assert [r["lapsDown"] for r in model["cars"]] == [0, 0, 0]
    # no lap COUNT either: every car is on its own lap, so the top row's is nobody else's
    assert model["session"]["lap"] == 0


def test_cars_with_no_lap_yet_go_to_the_bottom_and_stay_put():
    """The `-1.0` sentinel is the case most likely to be got wrong and the only one the
    practice capture can exercise: a naive sort puts it FIRST and tops the tower with
    whoever has not turned a wheel. `best_lap` is already None by then, so the risk here
    is instead sorting None to the top, and the no-time block must be STABLE, or it is
    the same churn moved down the tower."""
    model = snapshot_to_model(_practice_snap([None, 136.0, None, 135.0]))
    ids = [r["id"] for r in model["cars"]]
    assert ids[:2] == [3, 1]            # the two with times, quickest first
    assert ids[2:] == [0, 2]            # then the lapless, in a fixed order (by idx)
    assert [r["lapGap"] for r in model["cars"]] == [0.0, 1.0, None, None]
    assert [r["bestLap"] for r in model["cars"]][2:] == [None, None]

    # The all-sentinel case: nobody has set a time. This is the whole of the practice
    # capture (all 64 slots of all 3020 frames) and the normal opening of every session.
    # The tower must not blank, must not invent an order, and must not top itself with a
    # car that has no lap: every row simply has nothing to say yet.
    blank = snapshot_to_model(_practice_snap([None] * 4))
    assert [r["id"] for r in blank["cars"]] == [0, 1, 2, 3]
    assert all(r["lapGap"] is None and r["bestLap"] is None for r in blank["cars"])
    assert not any(r["fastest"] for r in blank["cars"])
    assert blank["session"]["fastestLap"] is None


def test_qualifying_warmup_and_testing_read_the_same_way_as_practice():
    """One predicate decides this, and it is `world.is_race_kind`: the SAME one the
    the director gates its cuts on. The tower and the camera disagreeing about whether this is
    a race would be exactly the failure that put one definition in world/model.py."""
    from pylon.world import SessionKind

    for kind in (SessionKind.QUALIFY, SessionKind.WARMUP, SessionKind.TESTING):
        model = snapshot_to_model(_practice_snap([137.0, 135.5], kind=kind))
        assert [r["id"] for r in model["cars"]] == [1, 0], kind
        assert not any(r["isLeader"] for r in model["cars"]), kind

    # UNKNOWN counts as a race, deliberately: the synthetic source lands there and it
    # generates races, so it keeps the running order rather than falling back to a
    # classification nobody has set a time for.
    unknown = snapshot_to_model(_practice_snap([137.0, 135.5], kind=SessionKind.UNKNOWN))
    assert [r["id"] for r in unknown["cars"]] == [0, 1]
    assert unknown["cars"][0]["isLeader"]
    assert unknown["cars"][0]["bestLap"] is None      # a race does not classify by lap


def test_a_race_tower_is_untouched_by_the_non_race_classification():
    """The regression guard for this change: a race keeps the running order, the interval
    column, the leader chip, and no lap-time classification at all."""
    from pylon.world import SessionKind

    model = snapshot_to_model(_practice_snap([137.0, 135.5, 136.2],
                                             kind=SessionKind.RACE,
                                             positions=[1, 2, 3]))
    assert [r["id"] for r in model["cars"]] == [0, 1, 2]     # the ORDER, not the times
    assert model["cars"][0]["isLeader"]
    assert all(r["bestLap"] is None and r["lapGap"] is None for r in model["cars"])
    assert model["session"]["lap"] == 6                      # the leader's lap, as before
    # the fastest-lap marker still works in a race: it is not part of the classification
    assert [r["fastest"] for r in model["cars"]] == [False, True, False]


def test_pitting_car_keeps_its_gap_and_the_outrigger_carries_pit():
    """The point of the outrigger (#11). PIT used to REPLACE the gap, and a pit cycle is
    exactly when positions change and the viewer most wants the number. The row now
    reports both: a real gap AND onPit for the outrigger to render."""
    from dataclasses import replace

    cars = [_mk_car(0, 1, 5.50),
            _mk_car(1, 2, 5.40, track_gap_ahead=8.4, gap_ahead=8.4, car_ahead_idx=0)]
    cars[1] = replace(cars[1], on_pit_road=True)
    session = SessionSnapshot(session_time=100.0, flags=0, state=None, is_green=True,
                              is_yellow=False, is_red=False, is_checkered=False,
                              time_remaining=None, laps_remaining=None,
                              is_last_lap=False, event_type="Race")
    snap = WorldSnapshot(tick=1, session_time=100.0, session=session,
                         cars={c.idx: c for c in cars}, order=[0, 1], events=[])

    pitting = snapshot_to_model(snap)["cars"][1]
    assert pitting["onPit"] is True
    assert pitting["interval"] == 8.4      # the gap survives the pit stop
    assert pitting["battle"] is False      # ...but a car on pit road is not racing anyone


def test_last_lap_reaches_the_overlay_and_no_lap_stays_null():
    """A car with no completed lap must send null, never iRacing's -1.0 and never 0:
    the overlay renders nothing at all for null, and a sentinel that reaches the wire
    is a sentinel printed on a broadcast (DESIGN.md 14)."""
    from dataclasses import replace

    cars = [_mk_car(0, 1, 5.50), _mk_car(1, 2, 5.40, gap_ahead=1.0, car_ahead_idx=0)]
    cars[0] = replace(cars[0], last_lap=135.4823)
    session = SessionSnapshot(session_time=100.0, flags=0, state=None, is_green=True,
                              is_yellow=False, is_red=False, is_checkered=False,
                              time_remaining=None, laps_remaining=None,
                              is_last_lap=False, event_type="Race")
    snap = WorldSnapshot(tick=1, session_time=100.0, session=session,
                         cars={c.idx: c for c in cars}, order=[0, 1], events=[])

    rows = snapshot_to_model(snap)["cars"]
    assert rows[0]["lastLap"] == 135.482   # thousandths, the way a timing screen writes it
    assert rows[1]["lastLap"] is None      # no lap yet -> nothing, not -1.0 and not 0


def test_last_lap_survives_a_real_race_capture_without_a_single_sentinel():
    """End-to-end over the Spa race (recordings/capture2), the capture the -1.0 sentinel
    was measured on. Every value the overlay is handed is either null or a plausible lap;
    nothing negative and nothing zero ever reaches the wire.

    The walk has to run deep into the capture, not sample the front of it: the recording
    opens at the race start, so the whole field reads the sentinel until the first car
    completes a lap around frame 1975 (~132s, one lap of Spa). A test that only checked
    the first few hundred frames would pass while measuring nothing at all.

    Skips where the capture is absent: recordings/ is gitignored, so CI has none."""
    from pylon.telemetry import PlaybackSource

    src = PlaybackSource(capture_or_skip("capture2.jsonl.gz"))
    info = src.session_info()
    wm, tower = WorldModel(info), TowerModel(info)

    real = 0
    for frame in src.frames():
        for row in tower.build(wm.update(frame))["cars"]:
            lap = row["lastLap"]
            if lap is None:
                continue
            real += 1
            assert 60.0 < lap < 300.0, f"implausible lap time on the wire: {lap}"
        if real > 500:      # enough real laps seen; the rest of the capture is more of it
            break

    assert real > 500, "the race capture should carry real lap times to check"


def _lap_snap(*best_laps):
    """A field whose only interesting property is each car's best lap (None = no time)."""
    from dataclasses import replace

    cars = [_mk_car(i, i + 1, 5.5 - i * 0.1) for i in range(len(best_laps))]
    cars = [replace(c, best_lap=bl) for c, bl in zip(cars, best_laps)]
    session = SessionSnapshot(session_time=100.0, flags=0, state=None, is_green=True,
                             is_yellow=False, is_red=False, is_checkered=False,
                             time_remaining=None, laps_remaining=None,
                             is_last_lap=False, event_type="Race")
    return WorldSnapshot(tick=1, session_time=100.0, session=session,
                         cars={c.idx: c for c in cars},
                         order=[c.idx for c in cars], events=[])


def test_the_fastest_lap_marker_goes_to_the_quickest_real_lap():
    """One holder, chosen off the quickest time, and nobody at all until a time exists.

    The furniture for this has been in the page since it was written and the feed sent a
    hardcoded False, so none of it had ever fired. The bug it is wired up to avoid is a
    plain min() over the raw channel: iRacing spells "no valid lap yet" as -1.0, which is
    the SMALLEST value in the array, so the naive read hands the fastest lap of the race
    to whichever car has not turned a wheel."""
    model = snapshot_to_model(_lap_snap(135.482, 134.001, None, 136.9))
    assert [r["fastest"] for r in model["cars"]] == [False, True, False, False]
    assert model["session"]["fastestLap"] == 134.001

    # Nobody has set a time: no marker anywhere, and no time on the session either. This
    # is the NORMAL state for the opening of every session, not an edge case: on the
    # practice capture it is all 64 slots of all 3020 frames.
    blank = snapshot_to_model(_lap_snap(None, None, None))
    assert not any(r["fastest"] for r in blank["cars"])
    assert blank["session"]["fastestLap"] is None

    # A dead heat on the same thousandth still yields exactly ONE marker, and it goes to
    # the car running higher: the walk is in running order, so the tie is not a coin toss
    # that flickers the purple between two rows frame to frame.
    tied = snapshot_to_model(_lap_snap(134.5, 134.5))
    assert [r["fastest"] for r in tied["cars"]] == [True, False]


def test_the_fastest_lap_holder_is_unique_across_a_real_race_capture():
    """End-to-end over the Spa race: at most one purple row per frame, the marked car
    really does hold the minimum, and no car without a lap is ever marked.

    Runs deep into the capture on purpose. It opens at the race start, so the whole field
    reads the sentinel until the first car completes a lap around frame 1975 (~132s, one
    lap of Spa): a test that stopped early would pass having measured nothing.

    Skips where the capture is absent: recordings/ is gitignored, so CI has none."""
    from pylon.telemetry import PlaybackSource

    src = PlaybackSource(capture_or_skip("capture2.jsonl.gz"))
    info = src.session_info()
    wm, tower = WorldModel(info), TowerModel(info)

    marked_frames = 0
    for frame in src.frames():
        model = tower.build(wm.update(frame))
        held = [r for r in model["cars"] if r["fastest"]]
        assert len(held) <= 1, f"{len(held)} cars wearing the fastest-lap marker at once"
        best = model["session"]["fastestLap"]
        if not held:
            assert best is None
            continue
        marked_frames += 1
        assert best is not None and 60.0 < best < 300.0, f"implausible session best: {best}"
        # ...and it really is the quickest lap on the wire: no car's completed lap may be
        # under the time the session is calling its best.
        for row in model["cars"]:
            lap = row["lastLap"]
            assert lap is None or lap >= best - 0.0005, (
                f"#{row['num']} lapped in {lap} while the session best reads {best}")
        if marked_frames > 500:
            break

    assert marked_frames > 500, "the race capture should hand out a fastest lap to check"


def _flag_snap(*flag_values):
    """A field where each car carries one raw CarIdxSessionFlags value, pre-masked the
    way the builder would hand it over."""
    from dataclasses import replace

    from pylon.telemetry.constants import DRIVER_FLAG_MASK

    cars = [_mk_car(i, i + 1, 5.5 - i * 0.1) for i in range(len(flag_values))]
    cars = [replace(c, driver_flags=v & DRIVER_FLAG_MASK)
            for c, v in zip(cars, flag_values)]
    session = SessionSnapshot(session_time=100.0, flags=0, state=None, is_green=True,
                             is_yellow=False, is_red=False, is_checkered=False,
                             time_remaining=None, laps_remaining=None,
                             is_last_lap=False, event_type="Race")
    return WorldSnapshot(tick=1, session_time=100.0, session=session,
                         cars={c.idx: c for c in cars},
                         order=[c.idx for c in cars], events=[])


def test_a_flagged_car_is_named_by_its_flag_and_servicible_is_not_one():
    """The trap this indicator exists to avoid: `servicible` is NOT a flag (the SDK's own
    comment says so) and it is set on essentially every car for essentially the whole
    race: 119,994 car-frames of it on capture2. Anything that reads "flags != 0" as
    "this car is flagged" therefore marks the entire field from lights to flag."""
    from pylon.telemetry.constants import SessionFlag

    rows = snapshot_to_model(_flag_snap(
        SessionFlag.SERVICIBLE,                              # the normal state: nothing
        SessionFlag.SERVICIBLE | SessionFlag.REPAIR,         # a real meatball
        SessionFlag.SERVICIBLE | SessionFlag.FURLED,         # a real furled black
        SessionFlag.SERVICIBLE | SessionFlag.BLACK,
        0,
    ))["cars"]
    assert [r["penalty"] for r in rows] == ["", "repair", "warn", "black", ""]


def test_a_disqualification_outranks_the_black_flag_that_carries_it():
    """`dq_scoring_invalid` sets `disqualify` too, and a served black flag becomes a
    disqualification, so more than one bit is normal. One word comes out, and it is the
    one the viewer needs: DSQ over BLACK, a standing penalty over a mechanical order."""
    from pylon.telemetry.constants import SessionFlag

    rows = snapshot_to_model(_flag_snap(
        SessionFlag.DQ_SCORING_INVALID | SessionFlag.DISQUALIFY,
        SessionFlag.BLACK | SessionFlag.DISQUALIFY,
        SessionFlag.BLACK | SessionFlag.REPAIR,
        SessionFlag.REPAIR | SessionFlag.FURLED,
    ))["cars"]
    assert [r["penalty"] for r in rows] == ["dsq", "dsq", "black", "repair"]


def test_the_penalty_token_is_not_clobbered_by_the_country_flag():
    """Two different things wanted to be called `flag` on a row: the driver's country
    emoji (which got there first, in the credentials spread) and this. The credentials are
    spread LAST, so naming the penalty `flag` did not conflict loudly: it silently lost
    every time a car had a country."""
    from pylon.telemetry.constants import SessionFlag

    snap = _flag_snap(SessionFlag.SERVICIBLE | SessionFlag.BLACK)
    people = {0: {"country": "United Kingdom", "flag": "\U0001f1ec\U0001f1e7"}}
    row = snapshot_to_model(snap, people=people)["cars"][0]
    assert row["penalty"] == "black"
    assert row["flag"] == "\U0001f1ec\U0001f1e7"


def test_real_per_car_flags_survive_the_race_capture_and_stay_rare():
    """capture2 holds both a real meatball and a real furled black, so the mechanism can
    be checked end to end rather than only against hand-built values.

    The counts are the point: 115 car-frames of meatball and 3 of furled against 119,994
    of `servicible`. If the masking regressed, `flagged` would be in the six figures.

    Skips where the capture is absent: recordings/ is gitignored, so CI has none."""
    from collections import Counter

    from pylon.telemetry import PlaybackSource

    src = PlaybackSource(capture_or_skip("capture2.jsonl.gz"))
    info = src.session_info()
    wm, tower = WorldModel(info), TowerModel(info)

    seen: Counter = Counter()
    rows_total = 0
    for frame in src.frames():
        for row in tower.build(wm.update(frame))["cars"]:
            rows_total += 1
            if row["penalty"]:
                seen[row["penalty"]] += 1

    assert rows_total > 100_000, "the whole capture should have been walked"
    # the two that are really in there, and nothing else invented
    assert set(seen) == {"repair", "warn"}, seen
    assert seen["repair"] == 115
    assert seen["warn"] == 3


def test_track_name_prefers_iracings_short_broadcast_name():
    """The tower header is ~190px wide and "Circuit de Spa-Francorchamps" needs ~280,
    so the long name was being ellipsised mid-word. iRacing already carries the name the
    sport actually says: TrackDisplayShortName. Falls back to the long one, because an
    ellipsised name still beats a blank header."""
    def _track(**weekend):
        return SessionInfo({"WeekendInfo": weekend, "DriverInfo": {"Drivers": []}}).track_name

    assert _track(TrackDisplayShortName="Spa",
                  TrackDisplayName="Circuit de Spa-Francorchamps") == "Spa"
    # no short name -> the long one is still better than nothing
    assert _track(TrackDisplayName="Circuit de Spa-Francorchamps") \
        == "Circuit de Spa-Francorchamps"
    # iRacing's several spellings of "empty" must not win over a real long name
    assert _track(TrackDisplayShortName="", TrackDisplayName="Sebring") == "Sebring"
    assert _track(TrackDisplayShortName="none", TrackDisplayName="Sebring") == "Sebring"
    assert _track() is None


def test_track_config_names_the_layout_that_is_actually_loaded():
    """The short name does not identify a lap. "Watkins Glen" is the Glen with the
    Inner Loop and the Glen without it, and a fact about the chicane is true of only
    one of them, so anything keyed on a track needs the configuration as well as the
    name. Verified against recordings/capture2, which reads "Grand Prix"."""
    def _config(**weekend):
        return SessionInfo({"WeekendInfo": weekend, "DriverInfo": {"Drivers": []}}).track_config

    assert _config(TrackConfigName="Grand Prix") == "Grand Prix"
    assert _config(TrackConfigName="Classic Boot") == "Classic Boot"
    # None is the answer that has to be safe: a caller who cannot read the layout
    # says nothing about it rather than assuming the usual one.
    assert _config() is None
    assert _config(TrackConfigName="") is None
    assert _config(TrackConfigName="none") is None


# ---------------------------------------------------------------------------
# #55: the interval column is measured with ONE ruler (CarIdxEstTime).
#
# These construct CarState directly rather than replaying a capture, on purpose:
# recordings/ is gitignored, so a test that needs one passes on this machine and
# vanishes in CI (see tests/conftest.py). The capture-backed acceptance numbers
# live in the issue; what must never regress silently is the arithmetic below.
# ---------------------------------------------------------------------------

SPA_EST_LAP = 126.63     # the real learned span at Spa, so the numbers here are lifelike


def _est_snap(pairs, *, est_lap=SPA_EST_LAP, kind="race"):
    """A race snapshot from (progress, est_time) pairs, leader first."""
    cars = []
    for rank, (progress, est) in enumerate(pairs):
        c = _mk_car(rank, rank + 1, progress, car_ahead_idx=(rank - 1 if rank else None))
        cars.append(replace(c, est_time=est))
    return WorldSnapshot(tick=1, session_time=100.0, session=_session(kind),
                         cars={c.idx: c for c in cars},
                         order=[c.idx for c in cars], events=[], est_lap=est_lap)


def test_interval_is_the_difference_of_two_est_times():
    """The whole fix in one line: a gap is where the two cars are on the track's own
    ruler, so it needs no speed, no leader-relative channel and no lap-boundary state."""
    rows = snapshot_to_model(_est_snap([(5.60, 70.0), (5.55, 68.5), (5.40, 61.25)]))["cars"]

    assert [r["interval"] for r in rows] == [0.0, 1.5, 7.25]
    assert all(r["intervalLaps"] == 0 for r in rows)


def test_the_interval_does_not_move_when_only_the_speed_does():
    """The mechanism behind the bug: the old column divided a distance by the TRAILING
    CAR'S INSTANTANEOUS speed, so an unchanged separation read ~4.6x bigger in La Source
    than on the Kemmel straight and every car breathed its gap once a lap. Same two track
    positions at hairpin speed and at full chat must now report the same number."""
    def interval_at(speed):
        snap = _est_snap([(5.60, 70.0), (5.55, 68.0)])
        slow = replace(snap.cars[1], speed=speed)
        snap = replace(snap, cars={**snap.cars, 1: slow})
        return snapshot_to_model(snap)["cars"][1]["interval"]

    assert interval_at(18.0) == interval_at(83.0) == 2.0


def test_a_gap_across_the_start_finish_line_wraps_by_the_lap_span():
    """The most visible place on the lap to get it wrong. The leader has crossed and reset
    to ~0.4s while the car behind is still at 125.6s on the previous lap: the raw
    difference is -125.2, and the answer is 1.4s."""
    rows = snapshot_to_model(_est_snap([(6.003, 0.4), (5.990, 125.63)]))["cars"]

    assert rows[1]["interval"] == 1.4
    assert rows[1]["intervalLaps"] == 0      # a wrap is not a lap down


def test_side_by_side_across_the_line_is_not_a_full_lap():
    """The trap that a "negative means wrapped" rule falls into, and it is not theoretical:
    on the Spa capture it fired for 20 of 28 cars, turning a dead heat into +126.6s.

    Two cars genuinely alongside can read a difference of about -0.002s from nothing more
    than which side of the line each is on. Direction is decided by `progress` (monotonic
    laps.fraction, never ambiguous), so this stays a dead heat."""
    rows = snapshot_to_model(_est_snap([(5.5000, 63.000), (5.4999, 63.002)]))["cars"]

    assert rows[1]["interval"] == 0.0
    assert rows[1]["interval"] < 1.0         # the bug produced ~126.63 here


def test_a_lapped_car_keeps_whole_laps_out_of_the_seconds():
    """`est_time` is within-lap only, so laps stay their own concept: the row reads "+1 L"
    plus the on-track seconds, never one number with a lap folded into it."""
    rows = snapshot_to_model(_est_snap([(7.10, 12.0), (6.05, 6.0)]))["cars"]

    assert rows[1]["intervalLaps"] == 1
    assert rows[1]["interval"] == 6.0        # the within-lap part only


def test_no_est_reading_falls_back_instead_of_reporting_a_zero_gap():
    """Sentinel discipline. A slot with no data must not become a 0.0s gap, which reads as
    a dead heat. A feed with no CarIdxEstTime at all (the synthetic source, an older
    capture) keeps the previous behaviour rather than emptying the column."""
    no_span = snapshot_to_model(_est_snap([(5.60, 70.0), (5.55, 68.5)], est_lap=None))
    assert no_span["cars"][1]["interval"] > 0.0      # fell through to the old estimate

    snap = _est_snap([(5.60, 70.0), (5.55, 68.5)])
    blind = replace(snap.cars[1], est_time=None, track_gap_ahead=1.75)
    snap = replace(snap, cars={**snap.cars, 1: blind})
    assert snapshot_to_model(snap)["cars"][1]["interval"] == 1.75


def test_the_battle_chip_is_measured_with_the_same_ruler():
    """The chip sits beside two cars the viewer can see are nose to tail, so it had the
    same flaw more visibly. It must agree with the tower rather than have its own opinion."""
    from pylon.director.model import Shot, ShotKind

    snap = _est_snap([(5.60, 70.0), (5.55, 69.1)])
    group = _focus_group(Shot(ShotKind.BATTLE, "", 1, "", pair=(0, 1)), snap, {}, 4000.0, None)

    assert group["gap"] == 0.9
    assert group["gap"] == snapshot_to_model(snap)["cars"][1]["interval"]


def test_the_battle_chip_holds_through_a_pass():
    """Measured from whichever car is behind ON TRACK, not from whichever the timing line
    has credited, so the chip closes to ~0 through a swap instead of sticking at 0.0."""
    from pylon.director.model import Shot, ShotKind

    # car 1 has drawn ahead on track while still listed P2
    snap = _est_snap([(5.5990, 69.98), (5.6000, 70.00)])
    group = _focus_group(Shot(ShotKind.BATTLE, "", 1, "", pair=(0, 1)), snap, {}, 4000.0, None)

    assert group["gap"] == 0.02


def test_the_line_crossing_frame_where_pct_saturates_but_est_has_reset():
    """The frame that makes the tidy rule wrong, taken verbatim from capture2 at st=165.35.

    iRacing reports `lap_dist_pct` saturating at exactly 1.00000 for one frame while
    `est_time` has ALREADY reset to ~0.0001. So the two channels disagree about which lap
    the car is on at the only moment it matters, and any wrap rule that reads the answer
    off track position adds a phantom lap here: car 20 shows 0.32s, then 126.95s, then
    0.32s again. The gap between these two is a third of a second and must stay one."""
    behind = replace(_mk_car(0, 2, 1.0), est_time=0.00012322051043156534)
    behind = replace(behind, lap_dist_pct=1.00000, lap_completed=0, progress=1.00000)
    ahead = replace(_mk_car(1, 1, 1.00228), est_time=0.3237292468547821)
    ahead = replace(ahead, lap_dist_pct=0.00228, lap_completed=1, progress=1.00228)
    snap = WorldSnapshot(tick=1, session_time=165.35, session=_session("race"),
                         cars={1: ahead, 0: behind}, order=[1, 0], events=[],
                         est_lap=126.627)

    assert snapshot_to_model(snap)["cars"][1]["interval"] == 0.32


def test_a_gap_reported_the_frame_before_a_crossing_matches_the_frame_after():
    """Continuity across the line, which is what the viewer actually sees. The same pair,
    holding station, sampled either side of the leader's crossing: one number, not a step."""
    def interval(prog_a, est_a, prog_b, est_b):
        snap = _est_snap([(prog_a, est_a), (prog_b, est_b)])
        return snapshot_to_model(snap)["cars"][1]["interval"]

    before = interval(5.985, 124.83, 5.973, 123.33)   # both still on the old lap
    after = interval(6.004, 0.50, 5.992, 125.63)      # leader across, second car not
    assert before == after == 1.5


def _tow_frames(surfaces0):
    """A two-car race where car 0 holds the fastest lap and reads NOT_IN_WORLD (towed)
    for the frames its surface says so. The sim stops reporting its times meanwhile."""
    from pylon.telemetry.frame import Frame

    for t, s0 in enumerate(surfaces0, start=1):
        yield Frame(tick=t, session_time=float(t), values={
            "SessionTime": float(t), "SessionNum": 0,
            "CarIdxLapDistPct": [0.5 if s0 == 3 else -1.0, 0.25],
            "CarIdxPosition": [1, 2], "CarIdxClassPosition": [1, 2],
            "CarIdxTrackSurface": [s0, 3], "CarIdxLap": [3, 3], "CarIdxLapCompleted": [2, 2],
            "CarIdxOnPitRoad": [False, False],
            "CarIdxLastLapTime": [131.0 if s0 == 3 else -1.0, 133.5],
            "CarIdxBestLapTime": [130.5 if s0 == 3 else -1.0, 133.0],
        })


def test_a_towed_fastest_lap_holder_keeps_the_purple_and_the_row_says_tow():
    """The builder used to drop a NOT_IN_WORLD car from the board in a race. The purple
    then moved to the next-best car and the page toasted FASTEST LAP for a lap nobody had
    just set, then again in reverse when the tow brought the car back. Now the car stays
    on the board, marked TOW, and the session's quickest lap stays where it was earned:
    the holder never changes, so the page has nothing to toast."""
    info = SessionInfo({"WeekendInfo": {"EventType": "Race"}, "DriverInfo": {"Drivers": [
        {"CarIdx": 0, "CarNumber": "1"}, {"CarIdx": 1, "CarNumber": "2"}]}})
    wm, tower = WorldModel(info), TowerModel(info)
    models = [tower.build(wm.update(fr)) for fr in _tow_frames([3, 3, -1, -1, 3])]

    def holder(m):
        return [c["id"] for c in m["cars"] if c["fastest"]]

    def row(m, idx):
        return next(c for c in m["cars"] if c["id"] == idx)

    assert [holder(m) for m in models] == [[0]] * 5
    assert models[2]["session"]["fastestLap"] == 130.5
    towed, back = row(models[2], 0), row(models[4], 0)
    assert towed["tow"] and not towed["isLeader"] and not towed["onPit"]
    assert towed["pos"] == 1                 # still P1 in the order until somebody passes
    assert row(models[2], 1)["isLeader"] is False   # ...so nobody wears LEADER meanwhile
    assert not back["tow"] and back["isLeader"]


def test_the_live_pump_never_simulates_a_shot_and_keeps_the_last_real_one(monkeypatch):
    """When the director goes quiet the pop-in used to follow a LOCAL director's
    fantasy shot while the sim's camera sat frozen on the real one: the exact bug the
    shot file was built to kill. No shot means no pop-in; a shot that goes stale means
    the last real one, which is where the camera still is."""
    from pylon.overlay import transport as ov

    calls = {"n": 0}

    def fake_read_shot():
        calls["n"] += 1
        if 20 <= calls["n"] < 40:
            return {"kind": "leader", "target": 2, "pair": None, "replay": None}
        return None

    monkeypatch.setattr(ov, "read_shot", fake_read_shot)

    async def go():
        src = SyntheticSource(num_cars=10, duration_s=12.0, hz=10.0, seed=4)
        server = BridgeServer(source=src, host="127.0.0.1", port=8784)
        models: list[dict] = []
        async with server.ws_server():
            client = BridgeClient("ws://127.0.0.1:8784")
            await client.connect()

            async def collect(model):
                models.append(model)

            await _pump_models(client, collect)
            await client.aclose()
        return models

    models = asyncio.run(go())
    assert len(models) > 60
    assert all(m["focus"] is None for m in models[:19])              # nothing to show
    assert models[25]["focus"]["onCamId"] == 2                        # the real shot
    assert all(m["focus"] and m["focus"]["onCamId"] == 2 for m in models[40:])


# --- the show's identity on the pages ----------------------------------------
#
# The tower is pushed a model fifteen times a second; the holding cards are static
# browser sources that OBS points at a URL and forgets. Both need to know what the
# show is called and what colour it wears, and those are constants, so they are
# served once as JSON rather than repeated in every frame of a wire format built
# for things that change.


def test_the_page_view_is_what_a_browser_may_know_and_nothing_more():
    """A REDUCTION of the settings, not a copy of them.

    The pages need a name, a round line and a colour. A browser renders them and
    the file behind them holds a stream key, so the fact that no key, no password
    and no port is in here is the test.
    """
    from pylon.config import Config, LookConfig, ObsConfig, ShowConfig

    cfg = Config(show=ShowConfig(name="Thursday Night Racing", tag="TNR", round="Round 7"),
                 look=LookConfig(colour="#E11D48"),
                 obs=ObsConfig(password="hunter2", stream_key="live_12345_secret"))
    view = cfg.page_view()
    assert view["name"] == "Thursday Night Racing"
    assert view["tag"] == "TNR"
    assert view["round"] == "Round 7"
    assert view["colour"] == "#E11D48"
    body = str(view)
    assert "hunter2" not in body and "live_12345_secret" not in body
    assert "password" not in view and "stream_key" not in view


def test_an_unconfigured_show_still_gives_a_page_view_the_pages_can_use():
    """Empty strings, not a missing document: every page is written to draw nothing
    for an empty value, and that is the ordinary first-run case, not a degraded one."""
    from pylon.config import Config

    view = Config().page_view()
    assert view["name"] == "" and view["round"] == ""
    assert view["colour"]          # a default colour is always there to paint with
    assert view["tower"] is True and view["bug"] is True


def test_the_show_route_serves_json_and_the_static_tree_still_works():
    import functools
    import json
    import socketserver
    import threading
    import urllib.request

    from pylon.config import Config, ShowConfig
    from pylon.overlay.transport import _OverlayHTTP
    from pylon.settings import SHOW

    _OverlayHTTP.show = Config(show=ShowConfig(name="TNR", round="Round 7")).page_view()
    handler = functools.partial(_OverlayHTTP, directory=str(SHOW.overlays_dir))

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    srv = Server(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{base}/show.json", timeout=5) as r:
            assert r.headers["Content-Type"].startswith("application/json")
            assert json.loads(r.read())["round"] == "Round 7"
        # the file tree is untouched by the dynamic route
        with urllib.request.urlopen(f"{base}/cards.html", timeout=5) as r:
            assert b"card__circuit" in r.read()
        with urllib.request.urlopen(f"{base}/tracks/cota.svg", timeout=5) as r:
            assert b"<svg" in r.read()
    finally:
        srv.shutdown()
        _OverlayHTTP.show = {}


def test_every_synced_circuit_is_transparent_and_keeps_its_title():
    """The one edit the sync makes, and the one thing it must not lose.

    The opaque backdrop has to go or a circuit punches a flat hole through the
    holding card's own artwork; the <title> is what an alt text is built from.
    """
    from pylon.settings import SHOW

    svgs = sorted((SHOW.overlays_dir / "tracks").glob("*.svg"))
    assert len(svgs) >= 10, "the circuit artwork has not been synced"
    for svg in svgs:
        text = svg.read_text(encoding="utf-8")
        assert "<title>" in text, svg.name
        assert 'width="1600" height="900" fill=' not in text, f"{svg.name} kept its backdrop"


def test_two_surnames_that_share_three_letters_get_distinct_codes():
    """Round 1, 2026-09-20: Josh Wilson and Sjaak Willems were both WIL on the tower.
    The Schumacher convention separates a clash by the given name's initial, and only
    the clash changes: everyone else keeps the code `_tla` gives them."""
    from types import SimpleNamespace

    from pylon.overlay.identity import tla_sheet

    cars = {
        1: SimpleNamespace(name="Josh Wilson", number="11"),
        2: SimpleNamespace(name="Sjaak Willems", number="23"),
        3: SimpleNamespace(name="Mike Ulch", number="64"),
        4: SimpleNamespace(name="William Huntley", number="10"),   # HUN, untouched
    }
    assert tla_sheet(cars) == {1: "JWI", 2: "SWI", 3: "ULC", 4: "HUN"}
    # the same initial too: the first (by name) takes the initial route, and the
    # second, finding SWI taken, keeps its surname code, which is now free
    cars = {1: SimpleNamespace(name="Sam Wilson", number="11"),
            2: SimpleNamespace(name="Sjaak Willems", number="23")}
    assert tla_sheet(cars) == {1: "SWI", 2: "WIL"}
    # a chosen code never lands on someone else's: SWI is taken, so the next rule
    cars = {1: SimpleNamespace(name="Josh Wilson", number="11"),
            2: SimpleNamespace(name="Sjaak Willems", number="23"),
            3: SimpleNamespace(name="Ann Swift", number="5")}
    sheet = tla_sheet(cars)
    assert sheet[3] == "SWI" and sheet[2] != "SWI" and len(set(sheet.values())) == 3
    # no clash, no change, whatever the sheet
    cars = {1: SimpleNamespace(name="Josh Wilson", number="11"),
            3: SimpleNamespace(name="Mike Ulch", number="64")}
    assert tla_sheet(cars) == {1: "WIL", 3: "ULC"}
