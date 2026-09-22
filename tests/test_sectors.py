"""A quick lap, as it happens: the world measures it, the director goes to it and holds
to the line.

iRacing publishes sector times for the player only, and the broadcast box's player is
the spectator; every car's sectors are MEASURED here from lap distance and the clock at
the boundaries SplitTimeInfo gives (world/sectors.py). Checked against the sim on
recordings/capture2: 27 flying laps measured line-to-line against CarIdxLastLapTime,
mean 0.000s, sd 0.001s, worst 0.003s.
"""

from __future__ import annotations

import pytest

from pylon.director import Director, DirectorConfig, ShotKind
from pylon.telemetry.frame import Frame, SessionInfo
from pylon.world import WorldModel
from pylon.world.sectors import SectorTracker, boundaries_from_info

BOUNDS = [0.0, 0.3, 0.6]          # three sectors: 30%, 30%, 40% of the lap
HZ = 10.0


def _weekend(kind="Practice", *, sectors=True, num_cars=4):
    w = {
        "WeekendInfo": {"TrackDisplayShortName": "Spa", "TrackLength": "6.9293 km",
                        "EventType": "Race"},
        "SessionInfo": {"CurrentSessionNum": 0, "Sessions": [
            {"SessionNum": 0, "SessionType": kind, "SessionName": kind.upper()},
        ]},
        "DriverInfo": {"Drivers": [
            {"CarIdx": i, "CarNumber": str(10 + i), "UserName": f"Driver {i}"}
            for i in range(num_cars)
        ]},
    }
    if sectors:
        w["SplitTimeInfo"] = {"Sectors": [{"SectorNum": k, "SectorStartPct": b}
                                          for k, b in enumerate(BOUNDS)]}
    return w


def _lengths():
    return [b2 - b1 for b1, b2 in zip(BOUNDS, BOUNDS[1:] + [1.0])]


class _Car:
    """A car driven to a schedule: `laps` is a list of laps, each a list of sector
    durations in seconds (one per sector), starting at the line at t=0."""

    def __init__(self, laps, *, pit_laps=()):
        self.laps = laps
        self.pit_laps = set(pit_laps)     # laps (1-based) driven through the pit lane

    def at(self, t):
        """(lap number 1-based, fraction of the lap, laps completed) at session time t."""
        lengths = _lengths()
        lap_no, t0 = 1, 0.0
        for durs in self.laps:
            total = sum(durs)
            if t < t0 + total:
                dt, frac = t - t0, 0.0
                for length, d in zip(lengths, durs):
                    if dt < d:
                        frac += length * dt / d
                        break
                    frac += length
                    dt -= d
                return lap_no, min(frac, 0.99999), lap_no - 1
            t0 += total
            lap_no += 1
        return lap_no, 0.99999, lap_no - 1

    def lap_time(self, lap_no):
        return sum(self.laps[lap_no - 1]) if 0 < lap_no <= len(self.laps) else None


def _frames(cars: list[_Car], duration: float, *, hz=HZ, session_state=4, num=0):
    n = len(cars)
    best = [-1.0] * n
    last = [-1.0] * n
    f = 0
    t = 0.0
    while t <= duration:
        f += 1
        t = f / hz
        pct, lap, done, pit = [], [], [], []
        for i, c in enumerate(cars):
            ln, frac, completed = c.at(t)
            pct.append(frac)
            lap.append(ln)
            done.append(completed)
            pit.append(ln in c.pit_laps)
            # the sim's board turns over at the line, with the lap just completed
            prev = c.lap_time(ln - 1)
            if prev is not None and (ln - 1) not in c.pit_laps and (ln - 2) not in c.pit_laps:
                last[i] = prev
                best[i] = prev if best[i] < 0 else min(best[i], prev)
        yield Frame(tick=f, session_time=t, values={
            "SessionTime": t, "SessionNum": num, "SessionState": session_state,
            "SessionFlags": 0,
            "CarIdxLapDistPct": pct,
            "CarIdxPosition": [0] * n, "CarIdxClassPosition": [0] * n,
            "CarIdxTrackSurface": [1 if pit[i] else 3 for i in range(n)],
            "CarIdxLap": lap, "CarIdxLapCompleted": done,
            "CarIdxOnPitRoad": pit,
            "CarIdxBestLapTime": list(best), "CarIdxLastLapTime": list(last),
        })


def _snaps(cars, duration, **kw):
    wm = WorldModel(SessionInfo(_weekend(**{k: v for k, v in kw.items() if k in ("kind", "sectors")})))
    return [wm.update(fr) for fr in _frames(cars, duration, **{k: v for k, v in kw.items()
                                                                if k not in ("kind", "sectors")})]


def _first(snaps, idx, *, sector, started=None, done=False):
    """The snapshot where car `idx` first reports pace at `sector` (of a given lap)."""
    for s in snaps:
        p = s.cars[idx].pace
        if (p is not None and p.sector == sector and p.done == done
                and (started is None or abs(p.started - started) < 0.5)):
            return s
    return None


STEADY = [[15.0, 15.0, 20.0]] * 6                # a 50s lap, every lap
FIELD = [[15.6, 15.6, 20.8]] * 6                 # the rest of the field: 52s laps


# --------------------------------------------------------------------------- #
# The world: measured sectors
# --------------------------------------------------------------------------- #

def test_boundaries_come_from_split_time_info_and_a_feed_without_them_has_no_pace():
    assert boundaries_from_info(SessionInfo(_weekend())) == BOUNDS
    assert boundaries_from_info(SessionInfo(_weekend(sectors=False))) == []
    assert not SectorTracker([]).enabled
    snaps = _snaps([_Car(STEADY)], 160.0, sectors=False)
    assert all(c.pace is None for s in snaps for c in s.cars.values())


def test_a_crossing_is_clocked_between_frames_to_within_hundredths():
    """The crossing lands between two 0.1s frames; the interpolation puts it where the
    car actually was. A 50s lap with 30/30/40 sectors of 15/15/20s: the boundaries of
    lap 2 fall at 65, 80 and 100s exactly."""
    car = _Car(STEADY)
    snaps = _snaps([car], 160.0)
    for sector, expect in ((1, 65.0), (2, 80.0)):
        p = _first(snaps, 0, sector=sector, started=50.0).cars[0].pace
        assert p.at == pytest.approx(expect, abs=0.01)
        assert p.elapsed == pytest.approx(expect - 50.0, abs=0.01)
    done = _first(snaps, 0, sector=3, started=50.0, done=True).cars[0].pace
    assert done.elapsed == pytest.approx(50.0, abs=0.01)


def test_a_quicker_lap_is_on_the_session_pace_at_every_boundary_and_a_slower_one_is_not():
    """Lap 2 is the first lap seen line to line, so it is the reference. Lap 3 is two
    seconds quicker: up on the best at every boundary, every sector the quickest of the
    session, and the result says by how much. Lap 4 is a second slower: off the pace
    from the first boundary, against the new best."""
    car = _Car([[15, 15, 20], [15, 15, 20], [14.5, 14.5, 19.0], [15.5, 15.5, 20.0]])
    snaps = _snaps([car], 210.0)
    quick = 100.0                      # lap 3 starts at 100s
    for sector, up in ((1, -0.5), (2, -1.0)):
        p = _first(snaps, 0, sector=sector, started=quick).cars[0].pace
        assert p.vs_best == pytest.approx(up, abs=0.02) and p.vs_own == pytest.approx(up, abs=0.02)
        assert p.purple and p.green
        assert p.on_session_pace(0.1)
    result = _first(snaps, 0, sector=3, started=quick, done=True).cars[0].pace
    assert result.done and result.vs_best == pytest.approx(-2.0, abs=0.02)
    slow = 148.0                       # lap 4
    p = _first(snaps, 0, sector=1, started=slow).cars[0].pace
    assert p.vs_best == pytest.approx(1.0, abs=0.02)      # against the 48s lap now
    assert not p.on_session_pace(0.1) and not p.purple


def test_a_lap_through_the_pits_is_not_a_flying_lap():
    """A lap that starts from the pit exit, or visits the lane, has no pace: the out
    lap is not a lap anybody is timing. The next lap from the line is."""
    car = _Car([[15, 15, 20]] * 5, pit_laps=(2,))
    snaps = _snaps([car], 260.0)
    assert all(s.cars[0].pace is None for s in snaps if 50.0 <= s.session_time < 100.0)
    # lap 3 is clean, and is the first reference (lap 2 never counted)
    assert _first(snaps, 0, sector=3, started=100.0, done=True) is not None
    p = _first(snaps, 0, sector=1, started=150.0).cars[0].pace
    assert p is not None and p.vs_own is not None


def test_the_reference_is_withheld_while_the_sim_board_holds_a_quicker_lap():
    """A broadcast that joins mid-session measures its first clean lap and holds it as the
    session best; the sim's board knows better. Car 1 has a 45.0 on the board from
    before we were watching: nothing car 0 does at a 50s pace is 'on the session pace'
    until a lap we measured agrees with the board."""
    quick = _Car([[13.5, 13.5, 18.0]] * 6)          # 45s laps, but never measured...
    tracker = SectorTracker(BOUNDS)
    steady = _Car(STEADY)
    t = 0.0
    while t < 160.0:
        t += 0.1
        _ln, frac, done = steady.at(t)
        tracker.update(t, {0: (done + frac, False)}, {0: 50.0 if t > 100.0 else None, 1: 45.0})
    p = tracker.pace.get(0)
    assert p is not None and p.vs_own is not None      # our own reference for car 0 agrees
    assert p.vs_best is None                            # ...the session one does not
    del quick


def test_a_scrub_drops_the_lap_in_hand_and_keeps_the_references():
    tracker = SectorTracker(BOUNDS)
    car = _Car(STEADY)
    t = 0.0
    while t < 120.0:
        t += 0.1
        _ln, frac, done = car.at(t)
        tracker.update(t, {0: (done + frac, False)}, {0: None})
    assert tracker.session_best is not None and tracker.pace.get(0) is not None
    tracker.reset(keep_refs=True)
    assert tracker.session_best is not None and tracker.pace.get(0) is None
    tracker.reset(keep_refs=False)
    assert tracker.session_best is None


# --------------------------------------------------------------------------- #
# The director: go there, and stay to the line
# --------------------------------------------------------------------------- #

def _cuts(snaps, cfg=None):
    d = Director(cfg or DirectorConfig())
    out = []
    for s in snaps:
        dec = d.update(s)
        if dec is not None:
            out.append(dec)
    return out


def test_the_camera_goes_to_a_car_on_the_session_pace_and_holds_it_to_the_line():
    """Four cars touring at 50s laps. On its third lap car 2 goes two seconds quicker:
    the camera is on it within seconds of the first boundary, is not rotated off by the
    variety timer with the lap half done, and is still there through the result."""
    field = [_Car(FIELD) for _ in range(4)]
    field[2] = _Car([[15, 15, 20], [15, 15, 20], [14.5, 14.5, 19.0], [15, 15, 20], [15, 15, 20]])
    snaps = _snaps(field, 230.0)
    cuts = _cuts(snaps)
    first_boundary = 100.0 + 14.5                    # lap 3 of car 2, sector 1 done
    line = 100.0 + 48.0
    on = [c for c in cuts if c.shot.kind == ShotKind.FOLLOW and c.shot.target_idx == 2
          and first_boundary <= c.time <= first_boundary + 3.0]
    assert on, [(c.time, c.shot.key, c.reason) for c in cuts]
    # nothing else takes the camera between the call and the line + the result window
    later = [c for c in cuts if on[0].time < c.time <= line + DirectorConfig().follow_hot_window]
    assert not later, [(c.time, c.shot.key, c.reason) for c in later]
    # ...and the tour resumes afterwards rather than parking on the car for good
    after = [c for c in cuts if c.time > line + DirectorConfig().follow_hot_window]
    assert after and after[0].shot.target_idx != 2


def test_a_car_merely_up_on_its_own_best_does_not_steal_the_camera():
    """Early in a session everybody improves on every lap; a personal-best pace is a
    tie-break for the rotation, not a reason to cut."""
    cfg = DirectorConfig()
    field = [_Car(FIELD) for _ in range(4)]
    field[0] = _Car(STEADY)                      # the session best is a 50
    # car 3 improves on its own 55s laps, still well off the best
    field[3] = _Car([[16.5, 16.5, 22], [16.5, 16.5, 22], [16, 16, 21], [15.8, 15.8, 20.8]])
    snaps = _snaps(field, 230.0)
    steals = [c for c in _cuts(snaps, cfg) if c.reason == "stronger" and c.shot.target_idx == 3]
    assert not steals, [(c.time, c.shot.label) for c in steals]
