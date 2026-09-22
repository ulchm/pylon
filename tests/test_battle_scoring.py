"""Battle detection & scoring: the criteria that decide whether a pair is a fight
worth watching, and the world-model signals those criteria rest on.

Regression backstory: the director used to score battles off CarIdxF2Time. On real
data that channel is a per-lap staircase (flat between line crossings, 0 until a car
completes a lap), so the whole field read as a 0.00s side-by-side battle at the
start and every mid-lap catch was invisible. SyntheticSource fakes F2Time
*continuously*, which is why the tests never caught it. These tests use hand-built
frames whose F2Time is deliberately frozen, plus direct snapshots, to pin the fix.
"""

from pylon.camera.angles import Angle, AnglePolicy
from pylon.director import Director, DirectorConfig
from pylon.director.core import candidates
from pylon.director.model import ShotFlavor, ShotKind
from pylon.telemetry.constants import (
    MAX_CARS,
    SessionFlag,
    SessionState,
    TrackSurface,
)
from pylon.telemetry.frame import Frame, SessionInfo
from pylon.world.builder import WorldModel
from pylon.world.model import (
    CarState,
    Event,
    EventKind,
    IncidentSeverity,
    SessionSnapshot,
    WorldSnapshot,
)

# --------------------------------------------------------------------------- #
# World model: on-track gap + closing rate must survive a frozen F2Time signal.
# --------------------------------------------------------------------------- #

TRACK_M = 5000.0


def _session_info(n_cars: int) -> SessionInfo:
    return SessionInfo(
        {
            "WeekendInfo": {"TrackDisplayName": "Test", "TrackLength": "5.00 km",
                            "EventType": "Race"},
            "DriverInfo": {"Drivers": [
                {"CarIdx": i, "CarNumber": str(i + 1), "UserName": f"D{i}",
                 "CarClassID": 1, "CarIsPaceCar": False, "CarIsAI": True}
                for i in range(n_cars)
            ]},
        }
    )


def _frame(tick: int, dt: float, dist: dict[int, float], f2: list[float]) -> Frame:
    """One frame from absolute per-car distances (metres) and a fixed F2Time list."""
    ldp = [-1.0] * MAX_CARS
    lap = [-1] * MAX_CARS
    lapc = [-1] * MAX_CARS
    pos = [0] * MAX_CARS
    cpos = [0] * MAX_CARS
    cls = [-1] * MAX_CARS
    onpit = [False] * MAX_CARS
    surf = [TrackSurface.NOT_IN_WORLD] * MAX_CARS
    f2full = [-1.0] * MAX_CARS

    order = sorted(dist, key=lambda i: dist[i], reverse=True)
    for p, i in enumerate(order, start=1):
        d = dist[i]
        ldp[i] = (d % TRACK_M) / TRACK_M
        lapc[i] = int(d // TRACK_M)
        lap[i] = lapc[i] + 1
        pos[i] = p
        cpos[i] = p
        cls[i] = 1
        surf[i] = TrackSurface.ON_TRACK
        f2full[i] = f2[i]
    return Frame(tick=tick, session_time=tick * dt, values={
        "SessionTime": tick * dt, "SessionTick": tick,
        "SessionState": SessionState.RACING, "SessionFlags": SessionFlag.GREEN,
        "SessionTimeRemain": 999.0, "SessionLapsRemain": -1,
        "CarIdxLapDistPct": ldp, "CarIdxLap": lap, "CarIdxLapCompleted": lapc,
        "CarIdxPosition": pos, "CarIdxClassPosition": cpos, "CarIdxClass": cls,
        "CarIdxOnPitRoad": onpit, "CarIdxTrackSurface": surf, "CarIdxF2Time": f2full,
    })


def test_ontrack_gap_and_closing_survive_frozen_f2time():
    """The exact real-data failure: F2Time frozen while a car reels another in. The
    on-track gap must shrink and closing_rate must go positive regardless."""
    wm = WorldModel(_session_info(3))
    dt = 0.1
    frozen_f2 = [0.0, 0.55, 1.30]  # never updates (the per-lap staircase between lines)

    snaps = []
    for k in range(40):
        t = k * dt
        dist = {
            0: 2500.0 + 55.0 * t,          # leader, 55 m/s
            1: 2470.0 + 55.6 * t,          # P2, 30 m back and closing 0.6 m/s
            2: 2400.0 + 55.0 * t,          # P3, holding station 70 m back
        }
        snaps.append(wm.update(_frame(k, dt, dist, frozen_f2)))

    first, last = snaps[5], snaps[-1]  # skip warm-up frames (speed EMA settling)
    p2_first, p2_last = first.cars[1], last.cars[1]

    # F2Time interval is the frozen staircase: it never moves (documents the bug).
    assert p2_first.gap_ahead == p2_last.gap_ahead == 0.55

    # the on-track gap is real, present, and shrinking as P2 closes.
    assert p2_first.track_gap_ahead is not None and p2_last.track_gap_ahead is not None
    assert p2_last.track_gap_ahead < p2_first.track_gap_ahead

    # and the closing rate the director scores on is positive (was ~0 off F2Time).
    assert p2_last.closing_rate is not None and p2_last.closing_rate > 0.0

    # the station-holder (P3) is not closing.
    assert abs(last.cars[2].closing_rate or 0.0) < 0.01


def _replay_run(samples, *, dt=1.4, n_cars=2):
    """Feed a WorldModel a sequence of replay-channel dicts, one per frame, and return
    the badge state (`is_replay`) it reported for each. One model throughout, because
    the tape's behaviour OVER TIME is the whole signal, and dt matters for the same
    reason: neither verdict is available until the settle window has passed. 1.4s is
    the spacing the Nordschleife samples below were actually taken at."""
    wm = WorldModel(_session_info(n_cars))
    out = []
    for tick, channels in enumerate(samples):
        fr = _frame(tick, dt, {0: 2600.0 + tick, 1: 2500.0 + tick}, [0.0, 2.0])
        vals = dict(fr.values)
        vals.update(channels)
        out.append(wm.update(Frame(tick=tick, session_time=tick * dt,
                                   values=vals)).session.is_replay)
    return out


def _tape(playing, pos, end, **extra):
    return {"IsReplayPlaying": playing, "ReplayFrameNum": pos,
            "ReplayFrameNumEnd": end, **extra}


def test_session_reads_replay_flag_time_and_lap_sentinels():
    """The overlay's live/replay badge + event clock come from the replay channels and
    the SessionTime* channels; iRacing's 32767-lap / week-long 'unlimited' sentinels
    must not leak through as real values."""
    wm = WorldModel(_session_info(2))
    fr = _frame(0, 0.1, {0: 2600.0, 1: 2500.0}, [0.0, 2.0])
    vals = dict(fr.values)
    vals.update({"IsReplayPlaying": 1, "SessionTimeTotal": 1800.0,
                 "SessionTimeRemain": 1471.7, "SessionLapsRemain": 32767})
    snap = wm.update(Frame(tick=0, session_time=0.0, values=vals))

    assert snap.session.time_total == 1800.0
    assert snap.session.time_remaining == 1471.7
    assert snap.session.laps_remaining is None  # the 32767 sentinel is dropped


def test_re_broadcasting_a_saved_replay_is_not_called_live():
    """#62: playing a .rpy over the bridge is not real time. A finished file has a
    FIXED length, so position and remainder trade off exactly: these are the
    Nordschleife 2026-07-30 samples, every one summing to 111250."""
    badges = _replay_run([_tape(True, 27215, 84035), _tape(True, 27299, 83951),
                          _tape(True, 27382, 83868), _tape(True, 27466, 83784),
                          _tape(True, 27550, 83700), _tape(True, 27634, 83616)])
    # Live until the flat tape has been flat long enough to mean something, then tape
    # for the rest of the broadcast. The gap falls to LIVE on purpose.
    assert badges[0] is False
    assert badges[-3:] == [True, True, True]


def test_a_live_race_is_not_called_a_replay_just_because_the_viewer_is_open():
    """The Spa 2026-07-26 regression, with the numbers that actually produced it.
    IsReplayPlaying is 1 for a spectator watching a race happening RIGHT NOW, and
    ReplayFrameNumEnd is NOT 0 at the live edge: it is whatever the viewer parks
    behind it (1201 here, flat). What gives it away is the tape still growing, at the
    60fps it is recorded at, so the badge must never reach REPLAY at all."""
    badges = _replay_run([_tape(True, 9911 + 84 * i, 1201) for i in range(6)])
    assert badges == [False] * 6


def test_a_tape_that_has_shown_it_is_growing_stays_live_when_it_stops():
    """The latch. iRacing's replay buffer is finite, so a long live session eventually
    stops gaining frames, that must not turn the race into a replay retrospectively,
    which is what a rule reading only the last few frames would do."""
    grow = [_tape(True, 1000 + 84 * i, 1201) for i in range(6)]
    capped = [_tape(True, 1420 + 84 * i, 1201 - 84 * i) for i in range(6)]
    assert _replay_run(grow + capped)[-1] is False


def _released(**channels):
    """Whether the world model calls the field released, off one hand-built frame."""
    wm = WorldModel(_session_info(2))
    fr = _frame(0, 0.1, {0: 2600.0, 1: 2500.0}, [0.0, 2.0])
    vals = dict(fr.values)
    vals.update(channels)
    return wm.update(Frame(tick=0, session_time=0.0, values=vals)).session.field_released


def test_the_field_is_not_released_until_it_is_let_go():
    """#63. Everything before the green is "not yet": the grid, the warm-up, and the
    parade laps a rolling start is led round on."""
    assert _released(SessionState=SessionState.GET_IN_CAR) is False
    assert _released(SessionState=SessionState.WARMUP) is False
    assert _released(SessionState=SessionState.PARADE_LAPS) is False
    assert _released(SessionState=SessionState.RACING) is True


def test_a_start_held_in_racing_is_still_not_released():
    """The case SessionState alone cannot see, and the reason this is two terms rather
    than one. On capture2 ONE_LAP_TO_GREEN is set for a frame after RACING begins, so
    the overlap is real; on a rolling start it is the whole final pace lap."""
    assert _released(SessionState=SessionState.RACING,
                     SessionFlags=SessionFlag.ONE_LAP_TO_GREEN) is False
    assert _released(SessionState=SessionState.RACING,
                     SessionFlags=SessionFlag.GREEN_HELD) is False
    assert _released(SessionState=SessionState.RACING,
                     SessionFlags=SessionFlag.GREEN) is True


def test_absence_of_a_parade_is_a_race_not_a_wait():
    """The trap this issue names: a standing start never reports PARADE_LAPS at all, and
    an older feed carries no SessionState. Both have to end up released, or the broadcast
    goes quiet for an entire race waiting for a green that already happened."""
    assert _released(SessionState=None) is True
    assert _released() is True


def test_a_feed_with_no_replay_channels_at_all_reads_live():
    """Absent is not evidence of tape. The synthetic source, an older capture and a
    sim-box agent built before these channels were streamed all carry nothing here,
    and badging every one of those broadcasts REPLAY is the worse lie."""
    assert _replay_run([{}, {}]) == [False, False]
    assert _replay_run([_tape(True, None, None)]) == [False]
    assert _replay_run([{"IsReplayPlaying": 1}]) == [False]


def test_lapped_pair_has_no_track_gap():
    """Two cars a full lap apart on track must not read as a nose-to-tail gap."""
    wm = WorldModel(_session_info(2))
    dt = 0.1
    for k in range(10):
        t = k * dt
        # car 1 is ~one whole lap behind car 0 (being lapped), though physically near.
        dist = {0: 6000.0 + 55.0 * t, 1: 1010.0 + 55.0 * t}
        snap = wm.update(_frame(k, dt, dist, [0.0, 90.0]))
    p2 = snap.cars[snap.order[1]]
    assert p2.track_gap_ahead is None


# --------------------------------------------------------------------------- #
# Director: what counts as a battle, and how a stale one is ranked.
# --------------------------------------------------------------------------- #


def _car(idx, position, *, class_id=1, lap_completed=3, progress=None, track_gap_ahead=None,
         closing_rate=None, car_ahead_idx=None, on_pit_road=False, speed=60.0,
         surface=TrackSurface.ON_TRACK):
    prog = lap_completed + 0.5 if progress is None else progress
    return CarState(
        idx=idx, number=str(idx), name=f"D{idx}", class_id=class_id, position=position,
        class_position=position, lap=int(prog) + 1, lap_completed=lap_completed,
        lap_dist_pct=prog % 1.0, progress=prog, speed=speed,
        on_pit_road=on_pit_road, surface=surface, on_track=(surface == TrackSurface.ON_TRACK),
        gap_ahead=None, gap_behind=None, to_leader=None,
        track_gap_ahead=track_gap_ahead, closing_rate=closing_rate,
        car_ahead_idx=car_ahead_idx,
    )


def _snap(cars, *, is_green=True, is_last_lap=False, t=100.0, events=None):
    session = SessionSnapshot(
        session_time=t, flags=0, state=None, is_green=is_green,
        is_yellow=not is_green, is_red=False, is_checkered=False, time_remaining=None,
        laps_remaining=None, is_last_lap=is_last_lap, event_type="Race",
    )
    return WorldSnapshot(tick=int(t), session_time=t, session=session,
                         cars={c.idx: c for c in cars},
                         order=[c.idx for c in cars], events=list(events or []))


def _battles(cars, **snap_kw):
    cfg = DirectorConfig()
    return [(s, sc) for (s, sc, _i) in candidates(_snap(cars, **snap_kw), cfg)
            if s.kind == ShotKind.BATTLE]


def _lead_battle_score(gap, closing):
    cars = [_car(0, 1), _car(1, 2, track_gap_ahead=gap, closing_rate=closing, car_ahead_idx=0)]
    bats = _battles(cars)
    assert bats, "expected a battle candidate"
    return bats[0][1]


def test_closing_pair_outscores_identical_stable_pair():
    """The core fix: a pair actively closing beats a pair sitting at the same gap
    doing nothing. Staring at the station-holder was the whole complaint."""
    active = _lead_battle_score(gap=0.5, closing=0.10)
    stale = _lead_battle_score(gap=0.5, closing=0.0)
    assert active > stale


def test_stale_midfield_battle_ranks_below_the_leader():
    """A static midfield gap is a train, not a fight: the leader shot should win, so
    the director isn't glued to a trailing car that's doing nothing."""
    cfg = DirectorConfig()
    cars = [_car(0, 1), _car(1, 2),  # leader pair, no track gap -> leader is 'solo'
            _car(2, 5), _car(3, 6, track_gap_ahead=0.6, closing_rate=0.0, car_ahead_idx=2)]
    cands = candidates(_snap(cars), cfg)
    leader = next(sc for (s, sc, _i) in cands if s.kind == ShotKind.LEADER)
    battle = next(sc for (s, sc, _i) in cands if s.kind == ShotKind.BATTLE)
    assert battle < leader


def test_side_by_side_pair_is_active_even_without_closing():
    """A wheel-to-wheel pair (sub-side_by_side_gap) is a fight even at a steady
    overlap, so it isn't damped like a wider static gap."""
    sbs = _lead_battle_score(gap=0.1, closing=0.0)      # overlapping, steady
    distant = _lead_battle_score(gap=0.9, closing=0.0)  # wide, static -> damped
    assert sbs > distant


def test_pair_wider_than_max_gap_is_not_a_battle():
    """More than battle_max_gap (1.0s) apart isn't a battle at all, even if closing."""
    cars = [_car(0, 1),
            _car(1, 2, track_gap_ahead=1.2, closing_rate=0.08, car_ahead_idx=0)]
    assert _battles(cars) == []


def test_close_pair_under_point_three_is_always_a_live_battle():
    """Under 0.3s it's ALWAYS a live battle: it outranks the lone leader even sitting
    at a steady gap (not closing), anywhere in the field."""
    cfg = DirectorConfig()
    cars = [_car(0, 1), _car(1, 2),                       # leader running solo
            _car(2, 8), _car(3, 9, track_gap_ahead=0.25, closing_rate=0.0, car_ahead_idx=2)]
    cands = candidates(_snap(cars), cfg)
    leader = next(sc for (s, sc, _i) in cands if s.kind == ShotKind.LEADER)
    battle = next(sc for (s, sc, _i) in cands if s.kind == ShotKind.BATTLE)
    assert battle > leader


def test_pit_stall_pair_is_not_a_battle():
    """A car in its pit stall (by track surface, even if the pit-road flag lags) isn't
    racing for position, so no battle."""
    cars = [_car(0, 1),
            _car(1, 2, track_gap_ahead=0.2, closing_rate=0.1, car_ahead_idx=0,
                 surface=TrackSurface.IN_PIT_STALL)]
    assert _battles(cars) == []


def test_lapped_pair_is_not_a_battle():
    """User's point: it's not a battle if they aren't on the same lap. Even with a
    tiny interval, a two-lap difference is a lapping, not a fight."""
    cars = [_car(0, 1, lap_completed=5),
            _car(1, 2, lap_completed=3, track_gap_ahead=0.2, closing_rate=0.1, car_ahead_idx=0)]
    assert _battles(cars) == []


def test_different_class_pair_is_not_a_battle():
    cars = [_car(0, 1, class_id=1),
            _car(1, 2, class_id=2, track_gap_ahead=0.2, closing_rate=0.1, car_ahead_idx=0)]
    assert _battles(cars) == []


def test_pit_road_pair_is_not_a_battle():
    cars = [_car(0, 1),
            _car(1, 2, track_gap_ahead=0.2, closing_rate=0.1, car_ahead_idx=0, on_pit_road=True)]
    assert _battles(cars) == []


def test_no_battles_under_yellow():
    """Cars bunched behind a safety car are close but going nowhere."""
    cars = [_car(0, 1),
            _car(1, 2, track_gap_ahead=0.1, closing_rate=0.05, car_ahead_idx=0)]
    assert _battles(cars, is_green=False) == []


def test_leader_shot_skips_a_pitting_leader():
    """The 'follow the leader' shot never targets a car sitting in its pit box: it falls
    through to the car actually leading on track, and keys by target so the camera
    follows the hand-over instead of staying parked on the pitting car."""
    cars = [_car(0, 1, on_pit_road=True), _car(1, 2), _car(2, 3)]
    cands = candidates(_snap(cars), DirectorConfig())
    leader = next(s for (s, _sc, _i) in cands if s.kind == ShotKind.LEADER)
    assert leader.target_idx == 1          # the on-track leader, not the pitting P1
    assert leader.key == "leader:1"


def test_no_leader_shot_when_everyone_is_pitting():
    cars = [_car(0, 1, on_pit_road=True), _car(1, 2, on_pit_road=True)]
    cands = candidates(_snap(cars), DirectorConfig())
    assert not any(s.kind == ShotKind.LEADER for (s, _sc, _i) in cands)


def test_leader_shot_skips_a_leader_sitting_in_its_pit_stall():
    """Same rule as battles: the pit-road FLAG lags the surface on entry and exit, so
    the leader shot judges 'in the pits' by surface too. Otherwise the one frame where
    the flag hasn't caught up parks the camera on a stationary car in its box."""
    cars = [_car(0, 1, surface=TrackSurface.IN_PIT_STALL), _car(1, 2), _car(2, 3)]
    cands = candidates(_snap(cars), DirectorConfig())
    leader = next(s for (s, _sc, _i) in cands if s.kind == ShotKind.LEADER)
    assert leader.target_idx == 1


# --------------------------------------------------------------------------- #
# Passes: hold the shot and swing to the look-back angle when a pair swaps.
# --------------------------------------------------------------------------- #


def _passing_pair(d: Director, *, flip_positions: bool, t0: float = 100.0,
                  hold: float = 1.0) -> tuple:
    """Open on a battle, then have car 1 go through on car 0.

    `flip_positions` says whether CarIdxPosition has caught up yet. It normally has
    NOT: the timing sheet only turns over at the line, which can be most of a lap
    after the move, so the interesting case is the one where only track position
    (progress) shows the pass. Returns (opening cut, decisions from the passing ticks)."""
    before = _snap([
        _car(0, 1, progress=5.500),
        _car(1, 2, progress=5.492, track_gap_ahead=0.15, closing_rate=0.05, car_ahead_idx=0),
    ], t=t0)
    opening = d.update(before)
    assert opening is not None and opening.shot.target_idx == 1  # on the attacker

    out = []
    for k in range(int(hold / 0.1) + 1):
        t = t0 + 5.0 + k * 0.1     # past min_shot, so a plain cut would be allowed
        # car 1 is now a length up the road; the sheet may or may not agree yet
        pos0, pos1 = (2, 1) if flip_positions else (1, 2)
        cars = [_car(1, pos1, progress=5.620, track_gap_ahead=0.10, car_ahead_idx=0),
                _car(0, pos0, progress=5.615, track_gap_ahead=0.10, car_ahead_idx=1)]
        # snapshot order follows the timing sheet, which is what the SDK gives us
        cars.sort(key=lambda c: c.position)
        out.append(d.update(_snap(cars, t=t)))
    return opening, out


def test_a_pass_repoints_to_the_car_that_lost_the_place():
    """The camera has to move at the pass: staying on the car that made the move
    frames a winner driving away up an empty road. The shot worth having is from the
    car that just lost the place, looking forward at the winner coming back across."""
    d = Director(DirectorConfig())
    _opening, ticks = _passing_pair(d, flip_positions=True)
    decisions = [x for x in ticks if x is not None]

    assert decisions, "the pass produced no cut at all"
    cut = decisions[0]
    assert cut.reason == "pass"
    assert cut.shot.kind == ShotKind.BATTLE
    assert cut.shot.target_idx == 0            # the car that got passed
    assert cut.shot.pair == (1, 0)             # pair ordered as the camera sees it
    assert cut.shot.flavor == ShotFlavor.PASS


def test_a_pass_repoints_before_the_timing_sheet_catches_up():
    """The bug this fixes: CarIdxPosition flips at the LINE. Keying the camera (and the
    pop-ins) off it meant the shot stayed on the old order for most of a lap after the
    viewer watched the move, so the overlay called the winner the attacker, and the
    camera sat on the wrong car. Track position turns over with the move."""
    d = Director(DirectorConfig())
    _opening, ticks = _passing_pair(d, flip_positions=False)
    decisions = [x for x in ticks if x is not None]

    assert decisions, "no cut: the pass went unnoticed until the timing line"
    cut = decisions[0]
    assert cut.reason == "pass"
    assert cut.shot.target_idx == 0            # framed from the car that lost the place
    assert cut.shot.pair == (1, 0)             # ...which is second on the road, first on the sheet
    assert cut.shot.flavor == ShotFlavor.PASS


def test_a_pass_does_not_re_key_the_battle_or_ping_pong_the_camera():
    """The pair keeps ONE identity across a swap (the key used to encode the running
    order, so a pass read as a brand new shot: it retired the fight's fatigue history
    and cut to a different battle at the exact moment this one paid off). And one pass
    is one cut, however many ticks it spans."""
    d = Director(DirectorConfig())
    opening, ticks = _passing_pair(d, flip_positions=False, hold=3.0)
    cuts = [x for x in ticks if x is not None]

    assert len(cuts) == 1, [c.reason for c in cuts]
    assert cuts[0].shot.key == opening.shot.key   # same fight, before and after the move
    assert opening.shot.key == "battle:0:1"       # order-free pair identity


def test_a_side_by_side_wobble_is_not_a_pass():
    """Cars running side by side trade the lead by inches through a corner. Every nose
    ahead must not re-point the camera, or a good scrap becomes a strobe."""
    d = Director(DirectorConfig())
    t0 = 100.0
    d.update(_snap([
        _car(0, 1, progress=5.500),
        _car(1, 2, progress=5.492, track_gap_ahead=0.15, closing_rate=0.05, car_ahead_idx=0),
    ], t=t0))

    cuts = []
    for k in range(12):                        # 1.2s of jostling, in 0.1s ticks
        t = t0 + 5.0 + k * 0.1
        # car 1's nose edges ahead for two ticks at a time, then falls back
        ahead = (k % 4) in (1, 2)
        p1 = 5.6025 if ahead else 5.5985
        cars = [_car(0, 1, progress=5.600, track_gap_ahead=0.10, car_ahead_idx=1),
                _car(1, 2, progress=p1, track_gap_ahead=0.05, car_ahead_idx=0)]
        cuts.append(d.update(_snap(cars, t=t)))

    assert not [c for c in cuts if c is not None and c.reason == "pass"]


def test_pass_flavor_frames_the_move_from_the_car_that_lost_the_place():
    """Every shipped personality renders a completed pass with a FORWARD-facing angle
    on the target (which is the passed car), so the winner is seen coming back across.
    A rear-facing angle there looks down an empty road."""
    forward = {Angle.CHASE, Angle.NOSE, Angle.COCKPIT}
    for policy in (AnglePolicy.classic(), AnglePolicy.onboard_forward(), AnglePolicy.cinematic()):
        ranked = policy.ranked(ShotKind.BATTLE, ShotFlavor.PASS)
        assert ranked[0] in forward, ranked
        assert Angle.REAR_CHASE not in ranked


# --------------------------------------------------------------------------- #
# Trouble: a car that crashed / stopped: cut to it and STAY.
# --------------------------------------------------------------------------- #


def _trouble_field(target_speed, target_surface, t):
    """Four cars flying + one target whose pace/surface we control."""
    cars = [
        _car(0, 1, progress=6.5, speed=45.0),
        _car(1, 2, progress=6.0, speed=45.0),
        _car(2, 3, progress=5.5, speed=45.0),
        _car(3, 4, progress=5.0, speed=45.0),
        _car(5, 5, progress=4.5, speed=target_speed, surface=target_surface),
    ]
    return _snap(cars, t=t)


def test_stranded_car_is_flagged_trouble_and_held():
    """A car racing then stopped off-track ('hit a wall') is flagged TROUBLE and the
    camera stays with it, rather than blipping back to the race the next tick."""
    d = Director(DirectorConfig())
    t = 100.0
    # 1) a couple of seconds of green racing so #5 is remembered as 'was racing'
    for _ in range(20):
        d.update(_trouble_field(45.0, TrackSurface.ON_TRACK, t)); t += 0.1
    assert d.current is not None and d.current.kind != ShotKind.TROUBLE

    # 2) #5 spins into the gravel: slow AND off-track, sustained past trouble_confirm
    for _ in range(15):
        d.update(_trouble_field(3.0, TrackSurface.OFF_TRACK, t)); t += 0.1
    assert d.current.kind == ShotKind.TROUBLE and d.current.target_idx == 5

    # 3) we stay with the stricken car: no cut back to the race for many seconds
    cuts_away = 0
    for _ in range(120):
        r = d.update(_trouble_field(3.0, TrackSurface.OFF_TRACK, t))
        if r is not None and r.shot.kind != ShotKind.TROUBLE:
            cuts_away += 1
        t += 0.1
    assert cuts_away == 0


def test_a_shown_wreck_never_grabs_the_camera_back():
    """The parked-wreck loop: a stricken car is held for trouble_hold, the backstop
    forces variety... and then the trouble candidate (score 12) instantly out-scored
    every battle in the normals lane and pulled the camera straight back. That cycled
    forever, 25s on the wreck / min_shot on the race, for the rest of the session."""
    d = Director(DirectorConfig())
    t = 100.0
    for _ in range(20):  # green running, so #5 is remembered as having been at pace
        d.update(_trouble_field(45.0, TrackSurface.ON_TRACK, t)); t += 0.1
    for _ in range(15):  # #5 stops in the gravel and stays there
        d.update(_trouble_field(3.0, TrackSurface.OFF_TRACK, t)); t += 0.1
    assert d.current.kind == ShotKind.TROUBLE

    kinds = []
    for _ in range(900):  # 90s, well past trouble_hold (25s)
        r = d.update(_trouble_field(3.0, TrackSurface.OFF_TRACK, t))
        if r is not None:
            kinds.append(r.shot.kind)
        t += 0.1

    # exactly one release off the wreck, and it never comes back
    assert kinds.count(ShotKind.TROUBLE) == 0, f"wreck re-grabbed the camera: {kinds}"
    assert ShotKind.LEADER in kinds


def test_a_recovered_car_can_be_flagged_trouble_again():
    """Retiring the shot key must not blacklist the car: if it drives away and later
    crashes again, that is a new incident and deserves the camera."""
    d = Director(DirectorConfig())
    t = 100.0
    for _ in range(20):
        d.update(_trouble_field(45.0, TrackSurface.ON_TRACK, t)); t += 0.1
    for _ in range(15):
        d.update(_trouble_field(3.0, TrackSurface.OFF_TRACK, t)); t += 0.1
    assert d.current.kind == ShotKind.TROUBLE
    for _ in range(400):  # held, then released by the backstop
        d.update(_trouble_field(3.0, TrackSurface.OFF_TRACK, t)); t += 0.1
    assert d.current.kind != ShotKind.TROUBLE

    for _ in range(40):   # recovers and rejoins at racing pace
        d.update(_trouble_field(45.0, TrackSurface.ON_TRACK, t)); t += 0.1
    for _ in range(20):   # ...and goes off again
        d.update(_trouble_field(3.0, TrackSurface.OFF_TRACK, t)); t += 0.1
    assert d.current.kind == ShotKind.TROUBLE and d.current.target_idx == 5


# --------------------------------------------------------------------------- #
# Incidents: a one-frame event that still has to be watchable.
# --------------------------------------------------------------------------- #


def _incident_field(t, *, incident=False):
    cars = [_car(0, 1, progress=6.5), _car(1, 2, progress=6.0),
            _car(2, 3, progress=5.5), _car(3, 4, progress=5.0)]
    # MAJOR: only an incident that clears the severity bar reaches the interrupt lane
    # at all, so that is what these latch/retire tests have to be built from.
    events = [Event(EventKind.INCIDENT, 2, other_idx=3, position=3, detail="contact",
                    severity=IncidentSeverity.MAJOR)] if incident else []
    return _snap(cars, t=t, events=events)


def _run_incident(cfg, ticks=400, at=50):
    d = Director(cfg)
    t, cuts = 100.0, []
    for k in range(ticks):
        r = d.update(_incident_field(t, incident=(k == at)))
        if r is not None:
            cuts.append((round(t, 1), r.shot.kind, round(r.prev_held, 1)))
        t += 0.1
    return cuts


def test_incident_shot_is_held_rather_than_cut_at_min_shot():
    """An incident is a single-frame event, so a candidate built from snap.events
    vanished the next tick: the state machine saw its score as None, called it 'shot
    ended', and cut away after exactly min_shot every single time. The latch keeps the
    shot alive for incident_hold so it can actually breathe."""
    cfg = DirectorConfig()
    cuts = _run_incident(cfg)
    incident_at = next(i for i, (_t, kind, _h) in enumerate(cuts) if kind == ShotKind.INCIDENT)
    held = cuts[incident_at + 1][2]   # prev_held recorded by the cut that follows it
    assert held > cfg.min_shot + 1.0, f"incident pinned to min_shot: {cuts}"
    assert held <= cfg.incident_hold + cfg.decision_interval + 0.5


def test_a_shown_incident_does_not_re_steal_the_camera():
    """Same failure mode as the wreck: incident_base (9) plus the leader bonus beats
    every battle, so a spent incident left in the candidate list would win the normals
    lane again and again until its latch expired."""
    cuts = _run_incident(DirectorConfig())
    assert sum(1 for (_t, kind, _h) in cuts if kind == ShotKind.INCIDENT) == 1, cuts


def test_grid_start_is_not_trouble():
    """Backmarkers sitting at a standing start (never fast yet) must NOT be flagged: a
    crash decelerates FROM racing speed, a gridded car was never racing."""
    d = Director(DirectorConfig())
    t = 100.0
    for _ in range(30):
        # leaders flying, the back of the grid still at a crawl off the line
        cars = [_car(i, i + 1, progress=6.0 - i * 0.2, speed=(45.0 if i < 2 else 1.0))
                for i in range(6)]
        d.update(_snap(cars, t=t)); t += 0.1
    assert d._detect_trouble(_snap(
        [_car(i, i + 1, progress=6.0 - i * 0.2, speed=(45.0 if i < 2 else 1.0)) for i in range(6)],
        t=t), t) == set()


def test_the_replay_badge_survives_a_scrub_back_on_the_tape():
    """A saved tape's total is constant, so a scrub back only moves session time. The
    settle window was measured from a start on the OLD timeline, so after the scrub the
    difference went negative and the badge fell to LIVE for as long as the scrub was."""
    wm = WorldModel(_session_info(2))

    def feed(tick, t, pos):
        fr = _frame(tick, 1.4, {0: 2600.0 + tick, 1: 2500.0 + tick}, [0.0, 2.0])
        vals = dict(fr.values)
        vals.update(_tape(True, pos, 111250 - pos))
        return wm.update(Frame(tick=tick, session_time=t, values=vals)).session.is_replay

    settled = [feed(k, 100.0 + 1.4 * k, 27215 + 84 * k) for k in range(6)]
    assert settled[-1] is True
    scrubbed = [feed(6 + k, 60.0 + 1.4 * k, 25000 + 84 * k) for k in range(6)]
    assert scrubbed[-3:] == [True, True, True], scrubbed
