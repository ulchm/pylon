"""The finish, as the camera should shoot it: the winner takes the flag, then the
field arrives one car at a time and a fight for the line is still a fight.

Round 1 (Watkins Glen, 2026-09-20): a 0.04s fight for P4 was cut off at the flag,
the winner held for 24s while that pair crossed unseen, then the camera walked down
the order as finishers reset to the pits. These pin what replaces that.
"""

from pylon.director import DirectorConfig
from pylon.director.core import Director, candidates
from pylon.director.model import ShotKind
from pylon.telemetry.constants import TrackSurface
from pylon.world.finish import FinishTracker
from pylon.world.model import CarState, SessionKind, SessionSnapshot, WorldSnapshot

EST_LAP = 100.0     # the track's ruler: est_time runs 0..100 and resets at the line


def _car(idx, position, pct, laps, *, surface=TrackSurface.ON_TRACK, gap=None, closing=0.0,
         ahead=None):
    return CarState(
        idx=idx, number=str(idx), name=f"D{idx}", class_id=1, position=position,
        class_position=position, lap=laps + 1, lap_completed=laps, lap_dist_pct=pct,
        progress=laps + pct, speed=50.0, on_pit_road=surface == TrackSurface.IN_PIT_STALL,
        surface=surface, on_track=surface == TrackSurface.ON_TRACK,
        gap_ahead=gap, gap_behind=None, to_leader=None, track_gap_ahead=gap,
        closing_rate=closing, car_ahead_idx=ahead, est_time=pct * EST_LAP,
    )


def _snap(cars, t, *, checkered=False, last_lap=False, green=True):
    session = SessionSnapshot(
        session_time=t, flags=0, state=5 if checkered else 4, is_green=green and not checkered,
        is_yellow=False, is_red=False, is_checkered=checkered, time_remaining=None,
        laps_remaining=None, is_last_lap=last_lap, event_type="Race",
        session_kind=SessionKind.RACE,
    )
    order = [c.idx for c in sorted(cars, key=lambda c: c.position)]
    return WorldSnapshot(tick=int(t * 10), session_time=t, session=session,
                         cars={c.idx: c for c in cars}, order=order, events=[],
                         est_lap=EST_LAP)


def _kinds(cands):
    return {s.key: (s, sc) for (s, sc, _i) in cands}


# ---------------------------------------------------------------- the tracker

def test_nobody_has_finished_until_the_flag():
    ft = FinishTracker()
    for i in range(20):
        ft.update(_snap([_car(1, 1, 0.5 + i * 0.01, 10)], i * 0.1), i * 0.1)
    assert not ft.flag_out and not ft.finished(1)


def test_a_crossing_on_the_flag_frame_is_the_winner_crossing():
    """A lap-count race: the state flips AS the leader crosses, so the flag frame
    already carries the leader's new lap count. That is a finish, not a car still
    to come."""
    ft = FinishTracker()
    t = 0.0
    for i in range(15):                                  # approaching the line
        t = i * 0.1
        ft.update(_snap([_car(1, 1, 0.98 + i * 0.001, 10), _car(2, 2, 0.90, 10)], t), t)
    t += 0.1
    crossed = ft.update(_snap([_car(1, 1, 0.001, 11), _car(2, 2, 0.92, 10)], t,
                              checkered=True), t)
    assert ft.flag_out and crossed == [1]
    assert ft.finished(1) and not ft.finished(2)
    # the second car arrives a few seconds later
    for i in range(1, 30):
        t2 = t + i * 0.1
        laps2 = 11 if t2 - t > 2.0 else 10
        crossed = ft.update(_snap([_car(1, 1, 0.05, 11), _car(2, 2, 0.92 + i * 0.003, laps2)],
                                  t2, checkered=True), t2)
        if laps2 == 11:
            break
    assert crossed == [2] and ft.finished(2)
    assert ft.crossed_within(2, t2, 1.0) and not ft.crossed_within(1, t2, 1.0)


def test_a_timed_race_flags_with_the_leader_still_to_cross():
    """The Spa shape: CHECKERED at the time limit, the leader mid-lap. Nobody has
    finished; the leader finishes at his next crossing."""
    ft = FinishTracker()
    for i in range(10):
        ft.update(_snap([_car(1, 1, 0.4 + i * 0.01, 10)], i * 0.1), i * 0.1)
    ft.update(_snap([_car(1, 1, 0.5, 10)], 1.0, checkered=True), 1.0)
    assert ft.flag_out and not ft.finished(1)
    ft.update(_snap([_car(1, 1, 0.9, 10)], 40.0, checkered=True), 40.0)
    assert not ft.finished(1)
    assert ft.update(_snap([_car(1, 1, 0.01, 11)], 50.0, checkered=True), 50.0) == [1]


def test_the_flag_going_away_forgets_the_finish():
    ft = FinishTracker()
    ft.update(_snap([_car(1, 1, 0.5, 10)], 0.0), 0.0)
    ft.update(_snap([_car(1, 1, 0.01, 11)], 1.0, checkered=True), 1.0)
    assert ft.flag_out
    ft.update(_snap([_car(1, 1, 0.1, 0)], 2.0), 2.0)          # a new session
    assert not ft.flag_out and not ft.finished(1)


# ---------------------------------------------------------------- candidates

def _finish_after(snaps):
    ft = FinishTracker()
    for s in snaps:
        ft.update(s, s.session_time)
    return ft


def test_the_winners_last_seconds_to_the_line_are_the_flag_shot():
    cfg = DirectorConfig()
    ft = FinishTracker()
    far = _snap([_car(1, 1, 0.5, 10), _car(2, 2, 0.4, 10)], 0.0, last_lap=True)
    assert not any(s.kind == ShotKind.FINISH for (s, _sc, _i) in candidates(far, cfg, finish=ft))
    near = _snap([_car(1, 1, 0.95, 10), _car(2, 2, 0.4, 10)], 1.0, last_lap=True)
    by = _kinds(candidates(near, cfg, finish=ft))
    assert by["finish:1"][1] == cfg.flag_base
    # and the leader's ordinary shot is still there underneath it
    assert "leader:1" in by


def test_without_a_tracker_the_chequer_ends_the_racing_as_before():
    """The pure form the older tests use: no finish, no battles under the flag."""
    cfg = DirectorConfig()
    snap = _snap([_car(1, 1, 0.05, 11), _car(4, 4, 0.7, 10, gap=0.1, ahead=5),
                  _car(5, 5, 0.69, 10)], 5.0, checkered=True)
    kinds = {s.kind for (s, _sc, _i) in candidates(snap, cfg)}
    assert kinds == {ShotKind.LEADER}


def test_a_fight_for_the_line_is_still_a_fight_under_the_flag():
    cfg = DirectorConfig()
    before = _snap([_car(1, 1, 0.98, 10), _car(4, 4, 0.60, 10), _car(5, 5, 0.59, 10)], 0.0)
    flag = _snap([_car(1, 1, 0.01, 11), _car(4, 4, 0.62, 10), _car(5, 5, 0.61, 10)], 1.0,
                 checkered=True)
    ft = _finish_after([before, flag])
    assert ft.finished(1) and not ft.finished(4)

    # a second after the flag: the winner just across, the P4 pair still racing
    now = _snap([_car(1, 1, 0.03, 11), _car(5, 5, 0.66, 10, gap=0.1, closing=0.05, ahead=4),
                 _car(4, 4, 0.67, 10)], 2.0, checkered=True)
    by = _kinds(candidates(now, cfg, finish=ft))
    assert by["finish:1"][1] == cfg.flag_base               # the flag, lingering
    battle = next(v for k, v in by.items() if k.startswith("battle:"))
    assert battle[0].pair == (4, 5)
    # the chequer counts as the last lap for the pair's stakes
    green = _kinds(candidates(_snap(list(now.cars.values()), 2.0), cfg))
    assert battle[1] > next(v for k, v in green.items() if k.startswith("battle:"))[1]

    # the linger is over: the next car to the line is the shot, and the fight outranks it
    later = _snap([_car(1, 1, 0.10, 11), _car(5, 5, 0.80, 10, gap=0.1, closing=0.05, ahead=4),
                   _car(4, 4, 0.81, 10), _car(2, 2, 0.95, 10)], 2.0 + cfg.finish_linger + 1.0,
                  checkered=True)
    by = _kinds(candidates(later, cfg, finish=ft))
    assert "finish:1" not in by
    assert by["finish:2"][1] == cfg.finish_base * (1.0 + cfg.pos_k / 2)
    assert battle[1] > by["finish:2"][1]


def test_a_pair_that_has_crossed_is_not_a_battle_any_more():
    cfg = DirectorConfig()
    before = _snap([_car(1, 1, 0.98, 10), _car(4, 4, 0.98, 10), _car(5, 5, 0.97, 10)], 0.0)
    flag = _snap([_car(1, 1, 0.01, 11), _car(4, 4, 0.99, 10), _car(5, 5, 0.98, 10)], 1.0,
                 checkered=True)
    home = _snap([_car(1, 1, 0.05, 11), _car(5, 5, 0.03, 11, gap=0.1, ahead=4),
                  _car(4, 4, 0.04, 11)], 4.0, checkered=True)
    ft = _finish_after([before, flag, home])
    assert ft.finished(4) and ft.finished(5)
    by = _kinds(candidates(home, cfg, finish=ft))
    assert not any(k.startswith("battle:") for k in by)


def test_when_everyone_is_home_the_picture_is_whoever_is_still_out_there():
    cfg = DirectorConfig()
    before = _snap([_car(1, 1, 0.98, 10), _car(2, 2, 0.97, 10)], 0.0)
    flag = _snap([_car(1, 1, 0.01, 11), _car(2, 2, 0.99, 10)], 1.0, checkered=True)
    both = _snap([_car(1, 1, 0.05, 11), _car(2, 2, 0.01, 11)], 3.0, checkered=True)
    ft = _finish_after([before, flag, both])
    # the winner has reset to the pits, the second car is on its slow-down lap
    later = _snap([_car(1, 1, 0.05, 11, surface=TrackSurface.IN_PIT_STALL),
                   _car(2, 2, 0.3, 11)], 3.0 + cfg.finish_linger + 1.0, checkered=True)
    by = _kinds(candidates(later, cfg, finish=ft))
    assert list(by) == ["leader:2"] and by["leader:2"][1] == cfg.leader_solo_base


# ---------------------------------------------------------------- the director

def test_the_director_shoots_the_flag_then_the_fight_for_the_line():
    """Round 1's finish, replayed the way it should have gone: the winner's crossing
    takes the camera off the P4 fight, holds through the flag, and the fight gets the
    camera back for ITS crossing."""
    cfg = DirectorConfig(min_shot=2.0, decision_interval=0.1)
    d = Director(cfg)
    cuts = []

    def feed(t, leader_pct, leader_laps, pair_pct, pair_laps, checkered):
        cars = [_car(1, 1, leader_pct, leader_laps),
                _car(4, 4, pair_pct + 0.001, pair_laps),
                _car(5, 5, pair_pct, pair_laps, gap=0.08, closing=0.05, ahead=4)]
        dec = d.update(_snap(cars, t, checkered=checkered, last_lap=not checkered))
        if dec is not None:
            cuts.append((round(t, 1), dec.shot.kind, dec.shot.target_idx))

    t = 0.0
    # the last lap: the leader 20s from the line, the P4 pair at it hammer and tongs
    while t < 12.0:
        feed(t, 0.80 + t * 0.01, 10, 0.60 + t * 0.01, 10, False)
        t += 0.1
    # the leader crosses: the flag
    while t < 20.0:
        feed(t, (0.80 + t * 0.01) % 1.0, 11, 0.60 + t * 0.01, 10, True)
        t += 0.1
    # the pair reaches the line at t~40
    while t < 46.0:
        pct = 0.60 + t * 0.01
        feed(t, 0.2, 11, pct % 1.0, 11 if pct >= 1.0 else 10, True)
        t += 0.1

    kinds = [k for (_t, k, _i) in cuts]
    assert kinds[0] == ShotKind.BATTLE                            # the fight, on the last lap
    i = kinds.index(ShotKind.FINISH)
    assert cuts[i][2] == 1 and 12.0 <= cuts[i][0] <= 13.0         # the winner, at the line
    assert ShotKind.BATTLE in kinds[i + 1:]                       # and back to the fight
    back = cuts[i + 1 + kinds[i + 1:].index(ShotKind.BATTLE)][0]
    assert back < 25.0                                            # well before they cross
