"""Pit stops: a car that DROVE into the lane against one that was put there.

The sim reports both the same way (`CarIdxOnPitRoad` goes true), and until 2026-09-21
the world model did too: PIT_ENTRY fired for a reset after the flag, for a car back
from a tow, and for an Escape mid-race, and each of them was reported as a pit stop.
The capture has one of each shape worth having: car 7 drives in at Spa through three
seconds of APPROACHING_PITS, and car 0 goes from ON_TRACK at 24.7% of the lap to
IN_PIT_STALL at 3.5% in one frame. The tow has no capture, so it is hand-built like
the rest here.
"""

from pylon.telemetry.constants import (
    MAX_CARS,
    SessionFlag,
    SessionState,
    TrackSurface,
)
from pylon.telemetry.frame import Frame, SessionInfo
from pylon.world import EventKind, WorldModel

TRACK_M = 5000.0
DT = 0.1
PACE = 50.0
PIT_BOX = 150.0        # metres past the line, where every stall is in these frames


def _info(n=3, kind="Race") -> SessionInfo:
    return SessionInfo({
        "WeekendInfo": {"TrackDisplayName": "Test", "TrackLength": "5.00 km",
                        "EventType": kind},
        "DriverInfo": {"Drivers": [
            {"CarIdx": i, "CarNumber": str(i + 1), "UserName": f"D{i}", "CarClassID": 1}
            for i in range(n)]},
        "SessionInfo": {"Sessions": [{"SessionNum": 0, "SessionType": kind}]},
    })


def _frame(k: int, dist: dict[int, float], surf_of: dict[int, int],
           pit_of: dict[int, bool]) -> Frame:
    """`dist` is metres along the road; a car missing from `surf_of` is NOT_IN_WORLD."""
    ldp = [-1.0] * MAX_CARS
    lapc = [-1] * MAX_CARS
    pos = [0] * MAX_CARS
    surf = [TrackSurface.NOT_IN_WORLD] * MAX_CARS
    pits = [False] * MAX_CARS
    est_ch = [-1.0] * MAX_CARS
    for i, d in dist.items():
        pos[i] = i + 1
        s = surf_of.get(i, TrackSurface.NOT_IN_WORLD)
        surf[i] = s
        if s == TrackSurface.NOT_IN_WORLD:
            continue
        ldp[i] = (d % TRACK_M) / TRACK_M
        lapc[i] = int(d // TRACK_M)
        pits[i] = pit_of.get(i, False)
        est_ch[i] = (d % TRACK_M) / PACE
    est_ch[10] = TRACK_M / PACE
    t = k * DT
    return Frame(tick=k, session_time=t, values={
        "SessionTime": t, "SessionTick": k, "SessionNum": 0,
        "SessionState": SessionState.RACING, "SessionFlags": SessionFlag.GREEN,
        "CarIdxLapDistPct": ldp, "CarIdxLapCompleted": lapc, "CarIdxLap": [3] * MAX_CARS,
        "CarIdxPosition": pos, "CarIdxClassPosition": pos, "CarIdxClass": [1] * MAX_CARS,
        "CarIdxOnPitRoad": pits, "CarIdxTrackSurface": surf,
        "CarIdxF2Time": [-1.0] * MAX_CARS, "CarIdxEstTime": est_ch,
    })


def _events(snaps, *kinds):
    return [(round(s.session_time, 1), e.kind, e.car_idx)
            for s in snaps for e in s.events if e.kind in kinds]


PIT_KINDS = (EventKind.PIT_ENTRY, EventKind.PIT_RESET, EventKind.PIT_EXIT)


def _run(script, seconds=12.0, kind="Race"):
    """`script(t, dist, surf_of, pit_of)` mutates the three per frame. Three cars start
    on the road 100 m apart; car 0 is the one the scripts do things to."""
    wm = WorldModel(_info(kind=kind))
    dist = {0: 12300.0, 1: 12200.0, 2: 12100.0}
    surf_of = {i: TrackSurface.ON_TRACK for i in dist}
    pit_of: dict[int, bool] = {}
    snaps = []
    for k in range(int(seconds / DT) + 1):
        t = k * DT
        for i in dist:
            if surf_of.get(i) == TrackSurface.ON_TRACK:
                dist[i] += PACE * DT
        script(t, dist, surf_of, pit_of)
        snaps.append(wm.update(_frame(k, dist, surf_of, pit_of)))
    return snaps


def test_a_car_that_drives_in_is_a_pit_entry():
    """Approach road first, then the flag, then the box: the shape capture2's car 7 has."""
    def script(t, dist, surf_of, pit_of):
        if 2.0 <= t < 4.0:
            surf_of[0] = TrackSurface.APPROACHING_PITS
            dist[0] += 20.0 * DT             # pit-lane speed
        elif 4.0 <= t < 8.0:
            surf_of[0] = TrackSurface.IN_PIT_STALL
            pit_of[0] = True
        elif 8.0 <= t < 9.0:
            surf_of[0] = TrackSurface.APPROACHING_PITS
            pit_of[0] = True
            dist[0] += 20.0 * DT
        elif t >= 9.0:
            surf_of[0] = TrackSurface.ON_TRACK
            pit_of[0] = False
        if 3.0 <= t < 4.0:
            pit_of[0] = True                 # the flag lags the surface by a second
    evs = _events(_run(script), *PIT_KINDS)
    assert evs == [(3.0, EventKind.PIT_ENTRY, 0), (9.0, EventKind.PIT_EXIT, 0)], evs


def test_an_escape_to_the_pit_box_is_a_reset_not_a_stop():
    """capture2, car 0 at 350.6s: ON_TRACK one frame, IN_PIT_STALL a fifth of a lap
    away the next. Both halves of the test catch it; either alone would."""
    def script(t, dist, surf_of, pit_of):
        if t >= 5.0:
            dist[0] = 3 * TRACK_M + PIT_BOX
            surf_of[0] = TrackSurface.IN_PIT_STALL
            pit_of[0] = True
    evs = _events(_run(script), *PIT_KINDS)
    assert evs == [(5.0, EventKind.PIT_RESET, 0)], evs


def test_a_reset_from_beside_the_pit_entry_is_still_a_reset():
    """Too short a jump for the step test on its own: the stall on the first frame is
    what gives it away."""
    def script(t, dist, surf_of, pit_of):
        if t >= 5.0:
            dist[0] = dist[0] if surf_of[0] != TrackSurface.ON_TRACK else dist[0] + 30.0
            surf_of[0] = TrackSurface.IN_PIT_STALL
            pit_of[0] = True
    evs = _events(_run(script), *PIT_KINDS)
    assert evs == [(5.0, EventKind.PIT_RESET, 0)], evs


def test_back_from_a_tow_onto_pit_road_is_a_reset():
    """Out of the world for three seconds, then in the box: nobody drove there."""
    def script(t, dist, surf_of, pit_of):
        if 3.0 <= t < 6.0:
            surf_of[0] = TrackSurface.NOT_IN_WORLD
        elif t >= 6.0:
            dist[0] = 3 * TRACK_M + PIT_BOX
            surf_of[0] = TrackSurface.IN_PIT_STALL
            pit_of[0] = True
    evs = _events(_run(script), *PIT_KINDS)
    assert evs == [(6.0, EventKind.PIT_RESET, 0)], evs


def test_a_reset_car_leaving_the_lane_is_still_a_pit_exit():
    """The way OUT is always driven, so the exit fires whatever the arrival was: the
    graphic saying "rejoins in eighteenth" is true of a car that was reset."""
    def script(t, dist, surf_of, pit_of):
        if 3.0 <= t < 7.0:
            dist[0] = 3 * TRACK_M + PIT_BOX
            surf_of[0] = TrackSurface.IN_PIT_STALL
            pit_of[0] = True
        elif t >= 7.0:
            surf_of[0] = TrackSurface.ON_TRACK
            pit_of[0] = False
    evs = _events(_run(script), *PIT_KINDS)
    assert evs == [(3.0, EventKind.PIT_RESET, 0), (7.0, EventKind.PIT_EXIT, 0)], evs


def test_a_slot_filling_at_session_start_says_nothing():
    """Every car spawns NOT_IN_WORLD -> IN_PIT_STALL (all 24 on recordings/capture): an
    arrival, but not one that follows anything, so it is not a reset either."""
    wm = WorldModel(_info())
    dist = {0: PIT_BOX, 1: PIT_BOX + 10, 2: PIT_BOX + 20}
    snaps = []
    for k in range(40):
        surf = ({} if k < 10 else {i: TrackSurface.IN_PIT_STALL for i in dist})
        pits = {i: True for i in dist} if k >= 10 else {}
        snaps.append(wm.update(_frame(k, dist, surf, pits)))
    assert _events(snaps, *PIT_KINDS) == []


def test_nothing_fires_across_a_time_jump():
    """After a scrub the last frame is another timeline: the reset detector must not
    read a car sitting on pit road as having just arrived there."""
    def script(t, dist, surf_of, pit_of):
        if t >= 3.0:
            dist[0] = 3 * TRACK_M + PIT_BOX
            surf_of[0] = TrackSurface.IN_PIT_STALL
            pit_of[0] = True
    wm = WorldModel(_info())
    dist = {0: 12300.0, 1: 12200.0, 2: 12100.0}
    surf_of = {i: TrackSurface.ON_TRACK for i in dist}
    pit_of: dict[int, bool] = {}
    snaps = []
    for k in range(60):
        t = k * DT
        for i in dist:
            if surf_of.get(i) == TrackSurface.ON_TRACK:
                dist[i] += PACE * DT
        script(t, dist, surf_of, pit_of)
        fr = _frame(k, dist, surf_of, pit_of)
        if k == 45:   # the clock jumps back an hour with car 0 already in the box
            fr = Frame(tick=k, session_time=t - 3600.0,
                       values={**fr.values, "SessionTime": t - 3600.0})
        snaps.append(wm.update(fr))
    evs = _events(snaps, *PIT_KINDS)
    assert evs == [(3.0, EventKind.PIT_RESET, 0)], evs


# --------------------------------------------------------------------------- #
# The director: a stop is a shot, held while it lasts, then the rejoin.
# --------------------------------------------------------------------------- #

from pylon.director import Director, DirectorConfig, candidates
from pylon.director.model import ShotFlavor, ShotKind
from pylon.world import CarState, Event, SessionKind, WorldSnapshot
from pylon.world.model import SessionSnapshot


def _car(idx, position, *, on_pit_road=False, surface=TrackSurface.ON_TRACK,
         track_gap_ahead=None, car_ahead_idx=None, closing_rate=None, speed=60.0,
         progress=None) -> CarState:
    prog = 3.5 - 0.1 * position if progress is None else progress
    return CarState(
        idx=idx, number=str(idx + 1), name=f"D{idx}", class_id=1, position=position,
        class_position=position, lap=int(prog) + 1, lap_completed=int(prog),
        lap_dist_pct=prog % 1.0, progress=prog, speed=speed,
        on_pit_road=on_pit_road, surface=surface, on_track=surface == TrackSurface.ON_TRACK,
        gap_ahead=None, gap_behind=None, to_leader=None,
        track_gap_ahead=track_gap_ahead, closing_rate=closing_rate,
        car_ahead_idx=car_ahead_idx,
    )


def _snap(cars, *, t, events=(), is_green=True, is_checkered=False,
          kind=SessionKind.RACE) -> WorldSnapshot:
    session = SessionSnapshot(
        session_time=t, flags=0, state=None, is_green=is_green, is_yellow=not is_green,
        is_red=False, is_checkered=is_checkered, time_remaining=None, laps_remaining=None,
        is_last_lap=False, event_type="Race", session_kind=kind)
    return WorldSnapshot(tick=int(t * 10), session_time=t, session=session,
                         cars={c.idx: c for c in cars}, order=[c.idx for c in cars],
                         events=list(events))


def _stop_frames(*, entry=100.0, box=(110.0, 125.0), exit_=130.0, end=150.0,
                 arrival=EventKind.PIT_ENTRY, is_green=True, kind=SessionKind.RACE,
                 start=90.0, who=0):
    """A three-car race with nobody fighting anybody: the leader runs alone until it
    pits at `entry`, sits in its box for `box`, leaves the lane at `exit_`."""
    t = start
    while t <= end + 1e-9:
        events = []
        if abs(t - entry) < 1e-9:
            events.append(Event(arrival, who))
        if abs(t - exit_) < 1e-9:
            events.append(Event(EventKind.PIT_EXIT, who))
        in_lane = entry <= t < exit_
        in_box = box[0] <= t < box[1]
        surface = (TrackSurface.IN_PIT_STALL if in_box else
                   TrackSurface.APPROACHING_PITS if in_lane else TrackSurface.ON_TRACK)
        cars = [_car(0, 1), _car(1, 2), _car(2, 3)]
        cars[who] = _car(who, who + 1, on_pit_road=in_lane, surface=surface,
                         speed=0.0 if in_box else 20.0 if in_lane else 60.0)
        yield _snap(cars, t=round(t, 1), events=events, is_green=is_green, kind=kind)
        t += 0.5


def _direct(frames):
    d = Director(DirectorConfig())
    cuts = []
    for s in frames:
        dec = d.update(s)
        if dec is not None:
            cuts.append((s.session_time, dec.shot.kind, dec.shot.flavor, dec.shot.target_idx,
                         dec.reason))
    return cuts


def test_the_leaders_stop_takes_the_camera_holds_and_then_follows_the_rejoin():
    cuts = _direct(_stop_frames())
    kinds = [(k, f, i) for (_t, k, f, i, _r) in cuts]
    assert kinds[0] == (ShotKind.LEADER, ShotFlavor.SOLO, 0)
    # the stop: cut on the entry, one shot for the whole thirty seconds. (The reason is
    # "shot ended" rather than "stronger": the leader shot is keyed by car, and the
    # moment the leader is in the lane the leader is somebody else.)
    pit = [c for c in cuts if c[1] == ShotKind.PIT]
    assert len(pit) == 1 and pit[0][3] == 0, cuts
    assert 100.0 <= pit[0][0] <= 101.0, pit
    # nothing cuts away while the car is in the lane: the variety timer is off
    between = [c for c in cuts if pit[0][0] < c[0] < 130.0]
    assert between == [], between
    # ...then the same car is followed out, and only for a beat
    out = [c for c in cuts if c[1] == ShotKind.FOLLOW]
    assert len(out) == 1 and out[0][2] == ShotFlavor.REJOIN and out[0][3] == 0, cuts
    assert 130.0 <= out[0][0] <= 131.0
    after = [c for c in cuts if c[0] > out[0][0]]
    assert after and after[0][1] == ShotKind.LEADER, cuts
    assert 136.0 <= after[0][0] <= 139.0, after      # pit_rejoin_hold, then the leader again


def test_a_reset_is_never_a_pit_shot():
    """The car is in the lane for thirty seconds either way; only a drive-in is a stop."""
    cuts = _direct(_stop_frames(arrival=EventKind.PIT_RESET))
    assert all(c[1] not in (ShotKind.PIT,) for c in cuts), cuts
    assert all(c[2] != ShotFlavor.REJOIN for c in cuts), cuts


def test_a_midfield_stop_never_beats_a_live_fight_at_the_front():
    cfg = DirectorConfig()
    from pylon.director.core import PitVisit
    cars = [_car(0, 1), _car(1, 2, track_gap_ahead=0.2, closing_rate=0.05, car_ahead_idx=0),
            _car(9, 10, on_pit_road=True, surface=TrackSurface.APPROACHING_PITS)]
    snap = _snap(cars, t=100.0)
    scored = {s.kind: sc for (s, sc, _i) in candidates(snap, cfg, pits={9: PitVisit(99.0, 10)})}
    assert ShotKind.PIT in scored
    assert scored[ShotKind.BATTLE] > scored[ShotKind.PIT] + cfg.cut_margin
    # ...while the leader's own stop is worth more than that fight
    lead = {s.kind: sc for (s, sc, _i) in candidates(
        _snap([_car(0, 1, on_pit_road=True, surface=TrackSurface.APPROACHING_PITS),
               _car(1, 2), _car(2, 3)], t=100.0), cfg, pits={0: PitVisit(99.0, 1)})}
    assert lead[ShotKind.PIT] > scored[ShotKind.BATTLE]


def test_a_stop_under_the_caution_is_still_a_shot():
    cuts = _direct(_stop_frames(is_green=False))
    assert any(c[1] == ShotKind.PIT for c in cuts), cuts


def test_no_pit_shot_outside_a_race_or_once_the_flag_is_out():
    cuts = _direct(_stop_frames(kind=SessionKind.PRACTICE))
    assert all(c[1] != ShotKind.PIT for c in cuts), cuts

    d = Director(DirectorConfig())
    seen = []
    for s in _stop_frames():
        s = _snap(list(s.cars.values()), t=s.session_time, events=s.events, is_checkered=True)
        dec = d.update(s)
        if dec is not None:
            seen.append(dec.shot.kind)
    assert ShotKind.PIT not in seen, seen


def test_a_stop_that_drags_on_is_released():
    """Forty-five seconds is a rebuild, not a stop: the candidate retires and the
    camera moves on, even with the car still in its box."""
    cuts = _direct(_stop_frames(box=(110.0, 170.0), exit_=175.0, end=190.0))
    pit = next(c for c in cuts if c[1] == ShotKind.PIT)
    after = [c for c in cuts if c[0] > pit[0]]
    assert after and after[0][0] <= pit[0] + DirectorConfig().pit_max_hold + 1.0, cuts
    assert after[0][1] != ShotKind.PIT
