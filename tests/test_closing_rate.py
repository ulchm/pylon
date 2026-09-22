"""The closing rate follows the gap, not the trailing car's speed.

Two cars on the same trajectory 0.6 s apart are not catching each other, whatever the
road does. Measured off the est ruler their separation is a constant 0.6 s. Measured as
distance over the trailing car's instantaneous speed, it collapses the moment the car
ahead brakes for a corner (the distance shrinks while the car behind is still quick),
and the derivative of that reads as a catch at many times the director's aliveness gate.
Capture2 put the false-positive rate at 7.4% of battle-range pair-frames. This pins the
fix, and keeps the old ruler alive for a feed that carries no est channel at all.
"""

from bisect import bisect_left

from pylon.director import DirectorConfig
from pylon.telemetry.constants import (
    MAX_CARS,
    SessionFlag,
    SessionState,
    TrackSurface,
)
from pylon.telemetry.frame import Frame, SessionInfo
from pylon.world import WorldModel

TRACK_M = 5000.0
DT = 0.1
GAP_S = 0.6                  # the pair's fixed separation, in seconds of road
CORNER = (1000.0, 1300.0)    # a slow corner: half speed between these metres
STEP = 0.001                 # trajectory resolution, seconds


def _speed(x: float) -> float:
    return 30.0 if CORNER[0] <= x < CORNER[1] else 60.0


# One trajectory from the line, x(t) at STEP resolution. est(x) IS this function inverted:
# the track's ruler is "seconds from the line to here", which for a car driving the
# profile is simply how long it took. Both cars ride the same curve, 0.6 s apart, so
# their est readings differ by exactly 0.6 s at every instant, and only the road varies.
_T: list[float] = []
_X: list[float] = []
_t, _x = 0.0, 0.0
while _x < 2400.0:
    _T.append(_t)
    _X.append(_x)
    _x += _speed(_x) * STEP
    _t += STEP


def _x_at(tau: float) -> float:
    return _X[min(len(_X) - 1, max(0, round(tau / STEP)))]


def _est_of(x: float) -> float:
    return _T[min(len(_T) - 1, bisect_left(_X, x))]


def _info() -> SessionInfo:
    return SessionInfo({
        "WeekendInfo": {"TrackDisplayName": "Test", "TrackLength": "5.00 km",
                        "EventType": "Race"},
        "DriverInfo": {"Drivers": [
            {"CarIdx": i, "CarNumber": str(i + 1), "UserName": f"D{i}", "CarClassID": 1}
            for i in range(2)]},
    })


def _frame(tick: int, t: float, tau_a: float, tau_b: float, *, est: bool) -> Frame:
    ldp = [-1.0] * MAX_CARS
    lapc = [-1] * MAX_CARS
    pos = [0] * MAX_CARS
    surf = [TrackSurface.NOT_IN_WORLD] * MAX_CARS
    est_ch = [-1.0] * MAX_CARS
    for i, tau in ((0, tau_a), (1, tau_b)):
        ldp[i] = _x_at(tau) / TRACK_M
        lapc[i] = 0
        pos[i] = i + 1
        surf[i] = TrackSurface.ON_TRACK
        est_ch[i] = tau
    est_ch[2] = 120.0   # an empty slot carrying the span, so the ruler is learned at once
    values = {
        "SessionTime": t, "SessionTick": tick, "SessionNum": 0,
        "SessionState": SessionState.RACING, "SessionFlags": SessionFlag.GREEN,
        "CarIdxLapDistPct": ldp, "CarIdxLapCompleted": lapc, "CarIdxLap": [1] * MAX_CARS,
        "CarIdxPosition": pos, "CarIdxClassPosition": pos, "CarIdxClass": [1] * MAX_CARS,
        "CarIdxOnPitRoad": [False] * MAX_CARS, "CarIdxTrackSurface": surf,
        "CarIdxF2Time": [-1.0] * MAX_CARS,
    }
    if est:
        values["CarIdxEstTime"] = est_ch
    return Frame(tick=tick, session_time=t, values=values)


def _closing_rates(*, est: bool) -> list[tuple[float, float | None, float | None]]:
    """(t, gap, closing rate) of the car behind, through the corner."""
    wm = WorldModel(_info())
    start = _est_of(500.0)            # the car ahead starts 500 m up the road
    out = []
    for k in range(201):              # 20 s at 10 Hz; the corner is around t = 8..18 s
        t = round(k * DT, 3)
        tau_a = round(start + t, 3)
        snap = wm.update(_frame(k, t, tau_a, round(tau_a - GAP_S, 3), est=est))
        b = snap.cars[1]
        out.append((t, b.track_gap_ahead, b.closing_rate))
    return out


def test_a_fixed_separation_through_a_slow_corner_is_not_a_catch():
    thresh = DirectorConfig().closing_thresh
    rows = [r for r in _closing_rates(est=True) if r[0] > 2.0]
    assert all(r[1] is not None for r in rows)
    assert all(abs(r[1] - GAP_S) < 0.01 for r in rows), "the est gap is the fixed separation"
    worst = max(abs(r[2] or 0.0) for r in rows)
    assert worst < thresh, f"read a catch of {worst:.3f} s/s off a constant gap"


def test_without_an_est_channel_the_old_ruler_still_measures_and_still_breathes():
    """The fallback exists for feeds with no CarIdxEstTime; and this is the failure it
    carries, stated so nobody removes the ruler thinking the two were equivalent."""
    thresh = DirectorConfig().closing_thresh
    rows = [r for r in _closing_rates(est=False) if r[0] > 2.0]
    assert all(r[1] is not None for r in rows), "the speed ruler still reports a gap"
    corner_entry = [r for r in rows if 8.0 <= r[0] <= 11.0]
    assert max((r[2] or 0.0) for r in corner_entry) > thresh, \
        "distance over the trailing car's speed reads the car ahead braking as a catch"
