"""Favourite cars: "show me #64", without turning the director into a spotter.

The feature exists because most people running this are not broadcasting a
championship, they are broadcasting their friends. The design tension is the whole
test file: a favourite has to get screen time it would not otherwise earn, and must
not be able to hold the camera through a crash somewhere else.
"""

from __future__ import annotations

from pylon.director import Director, DirectorConfig
from pylon.director.core import candidates
from pylon.director.model import ShotKind
from pylon.telemetry.constants import TrackSurface
from pylon.world import CarState, SessionKind, WorldSnapshot
from pylon.world.model import SessionSnapshot


def _car(idx, pos, number, *, gap=9.0):
    prog = 3.5 - 0.1 * pos
    return CarState(
        idx=idx, number=number, name=f"Driver {number}", class_id=1, position=pos,
        class_position=pos, lap=int(prog) + 1, lap_completed=int(prog),
        lap_dist_pct=prog % 1.0, progress=prog, speed=60.0,
        on_pit_road=False, surface=TrackSurface.ON_TRACK, on_track=True,
        gap_ahead=None, gap_behind=None, to_leader=None,
        track_gap_ahead=gap, closing_rate=None, car_ahead_idx=None,
    )


def _snap(cars, t=100.0):
    session = SessionSnapshot(
        session_time=t, flags=0, state=None, is_green=True, is_yellow=False,
        is_red=False, is_checkered=False, time_remaining=None, laps_remaining=None,
        is_last_lap=False, event_type="Race", session_kind=SessionKind.RACE)
    return WorldSnapshot(tick=int(t * 10), session_time=t, session=session,
                         cars={c.idx: c for c in cars},
                         order=[c.idx for c in sorted(cars, key=lambda c: c.position)],
                         events=[])


def _scored(director, snap):
    """(key -> score) for one pass of the director's own scoring, favourites and all."""
    cands = candidates(snap, director.cfg)
    if director.cfg.favourites:
        cands = [(s, sc + director.cfg.favourite_bonus, i)
                 if (not i and director._is_favourite(snap, s)) else (s, sc, i)
                 for (s, sc, i) in cands]
    return {s.key: sc for s, sc, _i in cands}


#: A leader running alone, then a close pair behind: enough shots on offer that a
#: favourite can be lifted while others are checked for not moving.
def _field():
    return _snap([_car(0, 1, "1"), _car(1, 2, "64", gap=0.4), _car(2, 3, "17", gap=0.5)])


def test_a_favourite_shot_scores_higher_than_the_same_shot_without_the_list():
    snap = _field()
    plain = Director(DirectorConfig())
    fav = Director(DirectorConfig(favourites=frozenset({"17"})))
    base, lifted = _scored(plain, snap), _scored(fav, snap)

    mine = [k for k, s in ((s.key, s) for s, _, _ in candidates(snap, plain.cfg))
            if fav._is_favourite(snap, s)]
    assert mine == ["battle:1:2"], mine
    for key in base:
        want = base[key] + (fav.cfg.favourite_bonus if key in mine else 0.0)
        assert lifted[key] == want, key


def test_a_number_nobody_on_the_grid_wears_changes_nothing():
    snap = _field()
    plain = Director(DirectorConfig())
    fav = Director(DirectorConfig(favourites=frozenset({"99"})))
    assert _scored(fav, snap) == _scored(plain, snap)


def test_both_cars_of_a_battle_count():
    """"Show me #64" means the fight it is having as much as the car on its own, so
    a pair lifts whichever side of it the favourite is on."""
    snap = _field()
    d = Director(DirectorConfig(favourites=frozenset({"64"})))     # car index 1
    battles = {s.key: s for s, _sc, _i in candidates(snap, d.cfg) if s.kind == ShotKind.BATTLE}
    assert set(battles) == {"battle:0:1", "battle:1:2"}
    assert d._is_favourite(snap, battles["battle:0:1"]), "the car behind in the pair"
    assert d._is_favourite(snap, battles["battle:1:2"]), "and the car ahead in the next"


def test_a_padded_number_is_a_different_car_exactly_as_on_the_timing_screen():
    snap = _snap([_car(0, 1, "07"), _car(1, 2, "7", gap=0.4)])
    d = Director(DirectorConfig(favourites=frozenset({"7"})))
    leader = next(s for s, _, _ in candidates(snap, d.cfg) if s.kind == ShotKind.LEADER)
    assert leader.target_idx == 0 and not d._is_favourite(snap, leader), "#07 is not #7"
    pair = next(s for s, _, _ in candidates(snap, d.cfg) if s.kind == ShotKind.BATTLE)
    assert d._is_favourite(snap, pair), "#7 is in this pair"


def test_a_number_with_stray_spaces_still_matches():
    """A feed can pad it; a person typing into a settings box never does."""
    snap = _snap([_car(0, 1, " 64 "), _car(1, 2, "17")])
    d = Director(DirectorConfig(favourites=frozenset({"64"})))
    shot = next(s for s, _, _ in candidates(snap, d.cfg) if s.kind == ShotKind.LEADER)
    assert d._is_favourite(snap, shot)


def test_the_bonus_beats_an_ordinary_shot_but_loses_to_real_drama():
    """The line this feature walks. A director that simply follows one car is not
    directing, so the bonus has to be bigger than the cut margin and far smaller
    than what an incident scores."""
    cfg = DirectorConfig(favourites=frozenset({"64"}))
    assert cfg.favourite_bonus > cfg.cut_margin, "otherwise it can never take the camera"
    assert cfg.favourite_bonus < cfg.incident_base, "a crash always outranks a friend"
    assert cfg.favourite_bonus < cfg.trouble_base


def test_no_favourites_is_the_default_and_the_pass_is_skipped_entirely():
    """Most shows have none, and the scoring pass is skipped rather than adding zero
    to every candidate on every tick."""
    assert not DirectorConfig().favourites


def test_the_cli_turns_a_typed_line_into_the_set_the_director_uses():
    """What someone types in the settings box is "64, 17", with whatever spacing."""
    typed = "64, 17 ,, 7 "
    assert frozenset(n.strip() for n in typed.split(",") if n.strip()) \
        == frozenset({"64", "17", "7"})
