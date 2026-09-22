"""Incident severity: the bar an off has to clear before it earns the camera.

The complaint these tests pin: a quick driver running wide as part of a fast lap
was yanking the camera off good racing. Every surface transition to OffTrack became
a full INCIDENT, and INCIDENT rides the interrupt lane, which steps over min_shot.

The captures cannot validate the fix. `capture2` is an AI race (`Official: 0`,
`CarIsAI: 1` for 27 of 28 drivers) and AI drivers do not exploit track limits, which
is exactly why the original "genuine off-tracks are rare" reading held up; and a real
multi-car wreck never happened in 355 seconds. So the scenarios are hand-built here,
the way the frozen-F2Time staircase was pinned down in test_battle_scoring.py.
"""

from pylon.director import Director, DirectorConfig
from pylon.director import run as run_director
from pylon.director.model import ShotKind
from pylon.telemetry import SyntheticSource
from pylon.telemetry.constants import (
    MAX_CARS,
    SessionFlag,
    SessionState,
    TrackSurface,
)
from pylon.telemetry.frame import Frame, SessionInfo
from pylon.world.builder import WorldModel
from pylon.world.incidents import INCIDENT_CONFIRM
from pylon.world.model import (
    CarState,
    Event,
    EventKind,
    IncidentSeverity,
    SessionSnapshot,
    WorldSnapshot,
)

TRACK_M = 4300.0
DT = 0.1
PACE = 55.0  # m/s, racing speed


def _session_info(n_cars: int) -> SessionInfo:
    return SessionInfo({
        "WeekendInfo": {"TrackDisplayName": "Test", "TrackLength": "4.30 km",
                        "EventType": "Race"},
        "DriverInfo": {"Drivers": [
            {"CarIdx": i, "CarNumber": str(i + 1), "UserName": f"D{i}",
             "CarClassID": 1, "CarIsPaceCar": False, "CarIsAI": True}
            for i in range(n_cars)
        ]},
    })


def _frame(tick: int, dist: dict[int, float], off: set[int]) -> Frame:
    """One frame from absolute per-car distances (metres) and who is off the road."""
    ldp = [-1.0] * MAX_CARS
    lap = [-1] * MAX_CARS
    lapc = [-1] * MAX_CARS
    pos = [0] * MAX_CARS
    cpos = [0] * MAX_CARS
    cls = [-1] * MAX_CARS
    onpit = [False] * MAX_CARS
    surf = [TrackSurface.NOT_IN_WORLD] * MAX_CARS
    f2 = [-1.0] * MAX_CARS

    for p, i in enumerate(sorted(dist, key=lambda i: dist[i], reverse=True), start=1):
        ldp[i] = (dist[i] % TRACK_M) / TRACK_M
        lapc[i] = int(dist[i] // TRACK_M)
        lap[i] = lapc[i] + 1
        pos[i] = cpos[i] = p
        cls[i] = 1
        surf[i] = TrackSurface.OFF_TRACK if i in off else TrackSurface.ON_TRACK
    return Frame(tick=tick, session_time=tick * DT, values={
        "SessionTime": tick * DT, "SessionTick": tick,
        "SessionState": SessionState.RACING, "SessionFlags": SessionFlag.GREEN,
        "SessionTimeRemain": 999.0, "SessionLapsRemain": -1,
        "CarIdxLapDistPct": ldp, "CarIdxLap": lap, "CarIdxLapCompleted": lapc,
        "CarIdxPosition": pos, "CarIdxClassPosition": cpos, "CarIdxClass": cls,
        "CarIdxOnPitRoad": onpit, "CarIdxTrackSurface": surf, "CarIdxF2Time": f2,
    })


def _drive(n_cars: int, ticks: int, script):
    """Run a scripted scenario through a world model.

    `script(tick, dist) -> (speeds, off)`: the per-car speed for this tick and the set
    of cars off the road. Yields (snapshot, incident events) per tick.
    """
    wm = WorldModel(_session_info(n_cars))
    dist = {i: 2000.0 - i * 40.0 for i in range(n_cars)}
    out = []
    for k in range(ticks):
        speeds, off = script(k, dist)
        for i in range(n_cars):
            dist[i] += speeds[i] * DT
        snap = wm.update(_frame(k, dist, off))
        out.append((snap, [e for e in snap.events if e.kind == EventKind.INCIDENT]))
    return out


def _severities(runs) -> list[str]:
    return [e.severity for _snap, evs in runs for e in evs]


# --------------------------------------------------------------------------- #
# World model: classify the moment.
# --------------------------------------------------------------------------- #


def test_a_wheel_on_the_grass_at_speed_is_only_minor():
    """The whole complaint: a fast lap that uses the kerb and the exit road. The car
    never slows and is back on the road in half a second, that is driving, not an
    incident, and nothing above MINOR may come out of it."""
    def script(k, _dist):
        off = {1} if 20 <= k < 25 else set()      # 0.5s with two wheels on the grass
        return {i: PACE for i in range(3)}, off   # ...at unabated racing pace

    runs = _drive(3, 60, script)
    assert _severities(runs) == [IncidentSeverity.MINOR]


def test_still_off_and_slow_a_beat_later_escalates_to_moderate():
    """A spin or a trip through the gravel: off the road AND well down on the pace it
    carried in. That is a real single-car moment, and it is tiered above track limits
    (though still not enough to preempt the broadcast: see the director tests)."""
    def script(k, _dist):
        if k < 20:
            return {i: PACE for i in range(3)}, set()
        return {0: PACE, 1: 4.0, 2: PACE}, {1}    # into the gravel, and stays there

    runs = _drive(3, 80, script)
    # ONE event for the moment, at the tier it earned: a spin is never also filed as
    # a minor excursion, so nothing downstream can claim both about one moment.
    assert _severities(runs) == [IncidentSeverity.MODERATE]

    # ...and it waited before believing it, rather than firing on the transition frame
    off_at = next(snap.session_time for snap, _e in runs
                  for e in snap.events if e.kind == EventKind.OFF_TRACK)
    moderate_at = next(snap.session_time for snap, evs in runs for _e in evs)
    assert moderate_at - off_at >= INCIDENT_CONFIRM


def test_two_cars_off_in_the_same_place_is_contact():
    """The signal a per-car detector structurally cannot see. Two cars leaving the road
    together, at the same corner, is a collision, and it is classified on the spot,
    with no confirm delay, because a wreck must be able to take the camera instantly."""
    def script(k, _dist):
        off = {1, 2} if k >= 20 else set()
        return ({i: PACE for i in range(4)} if k < 20
                else {0: PACE, 1: 12.0, 2: 8.0, 3: PACE}), off

    runs = _drive(4, 40, script)
    majors = [(snap, e) for snap, evs in runs for e in evs
              if e.severity == IncidentSeverity.MAJOR]
    assert len(majors) == 1, "one shared moment, one event"
    snap, e = majors[0]
    assert {e.car_idx, e.other_idx} == {1, 2}
    assert e.detail == "contact"
    assert snap.tick == 20, "contact is called on the frame it happens, not later"
    # neither car also files a solo excursion for the same moment
    assert _severities(runs) == [IncidentSeverity.MAJOR]
    assert e.at == snap.session_time     # both left the road on this frame: no lag


def test_contact_carries_the_time_the_first_car_left_the_road():
    """The partner's off is what lets us call it contact, and that can come up to
    INCIDENT_PAIR_WINDOW after the first car went. The event is REPORTED on the second
    off and HAPPENED on the first, and anything that seeks the tape back to it (the
    replay machine) needs the second, not the frame: on air the difference was a
    replay whose slow motion opened after the hit."""
    def script(k, _dist):
        off = set()
        if k >= 20:
            off.add(1)                              # first car off at t=2.0
        if k >= 28:
            off.add(2)                              # partner off 0.8s later
        return ({i: PACE for i in range(4)} if k < 20
                else {0: PACE, 1: 12.0, 2: (8.0 if k >= 28 else PACE), 3: PACE}), off

    runs = _drive(4, 40, script)
    majors = [(snap, e) for snap, evs in runs for e in evs
              if e.severity == IncidentSeverity.MAJOR]
    assert len(majors) == 1
    snap, e = majors[0]
    assert snap.tick == 28, "called when the second car went"
    assert e.at == 2.0, "but it happened when the first one did"


def test_two_cars_off_at_opposite_ends_of_the_track_are_not_contact():
    """Same moment, different corners: two independent excursions, not a collision.
    Proximity is what makes it contact."""
    def script(k, _dist):
        off = {1, 2} if 20 <= k < 26 else set()
        return {i: PACE for i in range(4)}, off

    wm = WorldModel(_session_info(4))
    dist = {0: 3000.0, 1: 2900.0, 2: 2900.0 - TRACK_M / 2, 3: 500.0}  # 1 and 2 half a lap apart
    sevs = []
    for k in range(40):
        speeds, off = script(k, dist)
        for i in dist:
            dist[i] += speeds[i] * DT
        snap = wm.update(_frame(k, dist, off))
        sevs += [e.severity for e in snap.events if e.kind == EventKind.INCIDENT]
    assert sevs == [IncidentSeverity.MINOR, IncidentSeverity.MINOR]


# --------------------------------------------------------------------------- #
# Director: what may take the camera off live racing.
# --------------------------------------------------------------------------- #


def _car(idx, position, *, progress=None, speed=PACE, surface=TrackSurface.ON_TRACK):
    prog = 6.5 - idx * 0.5 if progress is None else progress
    return CarState(
        idx=idx, number=str(idx), name=f"D{idx}", class_id=1, position=position,
        class_position=position, lap=int(prog) + 1, lap_completed=int(prog),
        lap_dist_pct=prog % 1.0, progress=prog, speed=speed, on_pit_road=False,
        surface=surface, on_track=(surface == TrackSurface.ON_TRACK),
        gap_ahead=None, gap_behind=None, to_leader=None, track_gap_ahead=None,
        closing_rate=None, car_ahead_idx=None,
    )


def _snap(cars, t, events=()):
    session = SessionSnapshot(
        session_time=t, flags=0, state=None, is_green=True, is_yellow=False,
        is_red=False, is_checkered=False, time_remaining=None, laps_remaining=None,
        is_last_lap=False, event_type="Race",
    )
    return WorldSnapshot(tick=int(t * 10), session_time=t, session=session,
                         cars={c.idx: c for c in cars}, order=[c.idx for c in cars],
                         events=list(events))


def _run_with_incident(event: Event | None, ticks=200, at=60):
    """Four cars circulating; one incident event injected at tick `at`."""
    d = Director(DirectorConfig())
    t, cuts = 100.0, []
    for k in range(ticks):
        evs = [event] if (event is not None and k == at) else []
        dec = d.update(_snap([_car(i, i + 1) for i in range(4)], t, evs))
        if dec is not None:
            cuts.append((round(t, 1), dec.shot.kind, dec.reason, round(dec.prev_held, 1)))
        t += 0.1
    return cuts


def test_a_single_car_brush_with_the_grass_does_not_take_the_camera():
    """The fix, stated plainly. A MINOR incident is not merely ranked lower: it never
    becomes a candidate, because an interrupt scores nine to twelve and anything left in
    the running would win the normals lane the moment min_shot expired."""
    minor = Event(EventKind.INCIDENT, 2, position=3, detail="off-track",
                  severity=IncidentSeverity.MINOR)
    cuts = _run_with_incident(minor)
    assert not any(kind == ShotKind.INCIDENT for _t, kind, _r, _h in cuts)
    assert not any(reason == "incident" for _t, _k, reason, _h in cuts)
    # ...and the camera is not disturbed at all: same shot list as an incident-free race
    assert cuts == _run_with_incident(None)


def test_a_solo_spin_that_keeps_going_is_still_not_an_interrupt():
    """MODERATE is a real moment and the graphics mark it, but the camera bar is higher:
    the car is not gone and nobody else is involved."""
    moderate = Event(EventKind.INCIDENT, 2, position=3, detail="off, losing time",
                     severity=IncidentSeverity.MODERATE)
    assert _run_with_incident(moderate) == _run_with_incident(None)


def test_contact_preempts_the_broadcast_immediately():
    """The capability that must survive the higher bar: a genuine wreck still steps over
    min_shot and takes the camera on the frame it is classified."""
    cfg = DirectorConfig()
    major = Event(EventKind.INCIDENT, 2, other_idx=3, position=3, detail="contact",
                  severity=IncidentSeverity.MAJOR)
    # fire it two seconds into the opening shot, i.e. while min_shot still has a hold
    cuts = _run_with_incident(major, at=20)
    incident = [c for c in cuts if c[1] == ShotKind.INCIDENT]
    assert len(incident) == 1
    t, _kind, reason, prev_held = incident[0]
    assert reason == "incident"
    assert t == 102.0, "cut on the frame the contact was called"
    assert prev_held < cfg.min_shot, "an interrupt is allowed to step over min_shot"

    # ...and once there it breathes, rather than being dumped at min_shot on the dot
    after = cuts[cuts.index(incident[0]) + 1]
    assert after[3] > cfg.min_shot + 1.0, f"contact shot pinned to min_shot: {cuts}"


def test_a_car_stopped_in_the_gravel_still_takes_the_camera():
    """The other half of the bar: the car is GONE. That path is the trouble detector,
    which is unchanged, and now it gets there SOONER, because a bare off-track no
    longer burns the interrupt cooldown on the way."""
    def script(k, _dist):
        if k < 30:
            return {i: PACE for i in range(5)}, set()
        # #4 goes off and comes to rest; the rest of the field flies by
        return {0: PACE, 1: PACE, 2: PACE, 3: PACE, 4: 0.5}, {4}

    d = Director(DirectorConfig())
    kinds, trouble_at = [], None
    for snap, _evs in _drive(5, 90, script):
        dec = d.update(snap)
        if dec is None:
            continue
        kinds.append(dec.shot.kind)
        if dec.shot.kind == ShotKind.TROUBLE and trouble_at is None:
            trouble_at = snap.session_time

    assert ShotKind.INCIDENT not in kinds, "the off itself is not what earns the camera"
    assert trouble_at is not None, "a stricken car must still take the camera"
    assert trouble_at - 3.0 < 2.5, f"took {trouble_at - 3.0:.1f}s to reach the wreck"


def test_a_lone_excursion_never_produces_an_incident_shot_end_to_end():
    """Full pipeline, synthetic source: one car running wide for eight seconds cannot
    produce an INCIDENT cut, no matter how long it stays off."""
    src = SyntheticSource(num_cars=16, duration_s=120.0, hz=10.0, seed=7, incident_at=60.0)
    kinds = {d.shot.kind for d in run_director(src, DirectorConfig())}
    assert ShotKind.INCIDENT not in kinds
    assert ShotKind.BATTLE in kinds  # the racing keeps the camera instead


# --------------------------------------------------------------------------- #
# The episode machine's own lifecycle.
# --------------------------------------------------------------------------- #


def test_a_time_jump_drops_an_open_episode_without_a_verdict():
    """After a scrub the last frame belongs to another timeline. A car that was off the
    road before it must not be called anything when it appears back on the road after
    it: the MINOR verdict is "this excursion ended clean", and this one did not end."""
    wm = WorldModel(_session_info(2))
    dist = {0: 2000.0, 1: 1960.0}
    events = []
    for k in range(40):                                   # 4 s: car 0 off from 1.0 s
        for i in dist:
            dist[i] += PACE * DT
        snap = wm.update(_frame(k, dist, off={0} if k >= 10 else set()))
        events += [e for e in snap.events if e.kind == EventKind.INCIDENT]
    assert events == [], "still off the road: no verdict yet"
    # scrub back a minute, and the car is on the road where the tape lands
    for k in (5, 6, 7):
        snap = wm.update(_frame(k, dist, off=set()))
        events += [e for e in snap.events if e.kind == EventKind.INCIDENT]
    assert events == []
