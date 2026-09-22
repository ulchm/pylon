"""A car out of the world in a race: on the board, inert, falling back as the field passes.

The old rule dropped a NOT_IN_WORLD car from the snapshot in a race, because out of the
world usually means retired. A tow is the same signal for thirty seconds, and the row
vanished and came back. Decided 2026-09-02: the car stays, marked TOW, holding its spot
in the order by the track position it left from, so the cars behind pass it one by one
exactly as they would a car stopped on the road. Nothing films it, races it, or calls a
pass on it. No capture holds a tow, so the frames are hand-built.
"""

from pylon.director import Director, DirectorConfig, candidates
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


def _info(n=3) -> SessionInfo:
    return SessionInfo({
        "WeekendInfo": {"TrackDisplayName": "Test", "TrackLength": "5.00 km",
                        "EventType": "Race"},
        "DriverInfo": {"Drivers": [
            {"CarIdx": i, "CarNumber": str(i + 1), "UserName": f"D{i}", "CarClassID": 1}
            for i in range(n)]},
    })


def _frame(k: int, dist: dict[int, float], sheet: dict[int, int], out: set[int],
           est: bool = True) -> Frame:
    """Sheet positions are what the timing line last said; `out` is who is not in the
    world. The sim keeps reporting a position for a towed car; its distance and times go
    to the sentinels."""
    ldp = [-1.0] * MAX_CARS
    lapc = [-1] * MAX_CARS
    pos = [0] * MAX_CARS
    surf = [TrackSurface.NOT_IN_WORLD] * MAX_CARS
    est_ch = [-1.0] * MAX_CARS
    for i, d in dist.items():
        pos[i] = sheet[i]
        if i in out:
            continue
        ldp[i] = (d % TRACK_M) / TRACK_M
        lapc[i] = int(d // TRACK_M)
        surf[i] = TrackSurface.ON_TRACK
        est_ch[i] = (d % TRACK_M) / PACE
    est_ch[10] = TRACK_M / PACE   # an empty slot carrying the span
    t = k * DT
    return Frame(tick=k, session_time=t, values={
        "SessionTime": t, "SessionTick": k, "SessionNum": 0,
        "SessionState": SessionState.RACING, "SessionFlags": SessionFlag.GREEN,
        "CarIdxLapDistPct": ldp, "CarIdxLapCompleted": lapc, "CarIdxLap": [3] * MAX_CARS,
        "CarIdxPosition": pos, "CarIdxClassPosition": pos, "CarIdxClass": [1] * MAX_CARS,
        "CarIdxOnPitRoad": [False] * MAX_CARS, "CarIdxTrackSurface": surf,
        "CarIdxF2Time": [-1.0] * MAX_CARS, "CarIdxEstTime": est_ch,
    })


def _tow_race(out_from: float = 2.0, back_at: float = 8.0, seconds: float = 10.0):
    """Three cars 100 m and 300 m apart at 50 m/s; the leader leaves the world at
    `out_from`, frozen where it stopped, and reappears at `back_at` further up
    the road (the tow puts it down at its pit box). The sheet never updates: nobody
    crosses the line in these ten seconds."""
    wm = WorldModel(_info())
    dist = {0: 12600.0, 1: 12500.0, 2: 12300.0}
    sheet = {0: 1, 1: 2, 2: 3}
    snaps = []
    for k in range(int(seconds / DT) + 1):
        t = k * DT
        out = {0} if out_from <= t < back_at else set()
        for i in dist:
            if i not in out:
                dist[i] += PACE * DT
        if t >= back_at and k and (k - 1) * DT < back_at:
            dist[0] = 13100.0      # dropped at the pit box, ahead of the car that passed it
        snaps.append(wm.update(_frame(k, dist, sheet, out)))
    return snaps


def _at(snaps, t):
    return snaps[round(t / DT)]


def test_a_towed_car_stays_on_the_board_and_falls_back_as_the_field_passes():
    snaps = _tow_race()
    before, towed, passed, back = _at(snaps, 1.5), _at(snaps, 2.5), _at(snaps, 5.0), _at(snaps, 9.0)

    assert before.order == [0, 1, 2] and not before.cars[0].towed
    # out of the world: still on the board, inert, still P1 by the spot it stopped at
    c0 = towed.cars[0]
    assert c0.towed and towed.order == [0, 1, 2] and c0.position == 1
    assert c0.speed == 0.0 and c0.track_gap_ahead is None and c0.closing_rate is None
    assert c0.est_time is None and c0.gap_ahead is None and c0.gap_behind is None
    assert c0.surface == TrackSurface.NOT_IN_WORLD and not c0.on_track
    # the car behind measures nothing against a flatbed...
    assert towed.cars[1].track_gap_ahead is None and towed.cars[1].car_ahead_idx is None
    # ...and P3 measures its gap to P2, the next car actually on the road
    assert towed.cars[2].car_ahead_idx == 1 and towed.cars[2].track_gap_ahead is not None
    # car 1 drives past where car 0 stopped: the order turns over, the places follow
    assert passed.order == [1, 0, 2]
    assert (passed.cars[1].position, passed.cars[0].position, passed.cars[2].position) == (1, 2, 3)
    assert passed.cars[0].towed
    # back in the world at its pit box, up the road again: live, and ahead once more
    assert not back.cars[0].towed and back.cars[0].on_track
    assert back.order[0] == 0 and back.cars[0].position == 1


def test_driving_past_a_flatbed_is_not_an_overtake():
    snaps = _tow_race()
    passes = [e for s in snaps for e in s.events if e.kind == EventKind.OVERTAKE]
    assert passes == [], passes


def test_the_director_never_films_a_towed_car():
    cfg = DirectorConfig()
    d = Director(cfg)
    for s in _tow_race():
        d.update(s)
        c0 = s.cars[0]
        if c0.towed:
            assert 0 not in d.trouble, "a stopped car that was at pace is trouble; a towed one is not"
            for shot, _score, _interrupt in candidates(s, cfg):
                assert shot.target_idx != 0 and (not shot.pair or 0 not in shot.pair), shot
            assert s.cars[1].track_gap_ahead is None    # nothing to score a battle on


