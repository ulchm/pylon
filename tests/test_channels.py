"""One frame's per-car channels, read once and guarded once.

The failure this pins: `update()` read some CarIdx lists bare and others guarded, so a
feed missing a channel worked in one method and raised IndexError on frame one in the
next. Unlikely live (the bridge streams a fixed list) but the guarding was inconsistent,
and a feed IS allowed to lack channels: the synthetic source has no lap times, an older
capture has no est times.
"""

from pylon.telemetry.constants import (
    DRIVER_FLAG_MASK,
    MAX_CARS,
    SessionFlag,
    SessionState,
    TrackSurface,
)
from pylon.telemetry.frame import Frame, SessionInfo
from pylon.world import WorldModel
from pylon.world.channels import Channels


def _frame(values, t=10.0, tick=1):
    return Frame(tick=tick, session_time=t, values={
        "SessionTime": t, "SessionNum": 0, "SessionState": SessionState.RACING, **values})


def _info(n=2):
    return SessionInfo({
        "WeekendInfo": {"TrackDisplayName": "Test", "TrackLength": "4.00 km",
                        "EventType": "Race"},
        "DriverInfo": {"Drivers": [
            {"CarIdx": i, "CarNumber": str(i + 1), "UserName": f"D{i}", "CarClassID": 1}
            for i in range(n)]},
    })


def test_a_feed_missing_channels_still_builds_a_world_on_frame_one():
    """Only the surface and lap-distance channels: no positions, no laps completed, no
    pit-road flags, no lap times. Positions, laps completed and pit road were all indexed
    bare in update(), so this frame used to be an IndexError."""
    surf = [TrackSurface.NOT_IN_WORLD] * MAX_CARS
    ldp = [-1.0] * MAX_CARS
    for i in (0, 1):
        surf[i] = TrackSurface.ON_TRACK
        ldp[i] = 0.2 + 0.1 * i
    wm = WorldModel(_info())
    values = {"CarIdxTrackSurface": surf, "CarIdxLapDistPct": ldp}
    wm.update(_frame(values))
    snap = wm.update(_frame(values, t=10.1, tick=2))
    assert sorted(snap.cars) == [0, 1]
    car = snap.cars[0]
    assert (car.position, car.lap_completed, car.on_pit_road) == (0, -1, False)
    assert car.last_lap is None and car.best_lap is None and car.to_leader is None
    assert snap.order == [1, 0]          # unscored: furthest round the road first
    assert snap.events == []


def test_guarded_reads_answer_no_data_for_an_absent_or_short_channel():
    ch = Channels(_frame({"CarIdxTrackSurface": [TrackSurface.ON_TRACK] * 3,
                          "CarIdxPosition": [1]}))
    assert ch.n == 3
    assert ch.position(0) == 1 and ch.position(2) == 0        # a short list
    assert ch.lap(5) == -1 and ch.lap_completed(5) == -1       # an absent list
    assert ch.lap_dist_pct(5) == -1.0
    assert ch.surface(9) == TrackSurface.NOT_IN_WORLD
    assert ch.on_pit_road(0) is False and ch.car_class(0) is None
    assert ch.driver_flags(0) == 0


def test_the_sims_sentinels_die_at_the_channel():
    """-1.0 is "no lap yet", 0.0 is not a lap, a non-positive est time is "no data".
    They die here so a None downstream never means "a value we failed to parse"."""
    ch = Channels(_frame({
        "CarIdxTrackSurface": [1, 1, 1],
        "CarIdxF2Time": [-1.0, 0.0, 12.5],
        "CarIdxEstTime": [0.0, -1.0, 40.25],
        "CarIdxLastLapTime": [-1.0, 0.0, 95.1],
        "CarIdxBestLapTime": [-1.0, 94.0, 95.1],
    }))
    assert [ch.f2_time(i) for i in range(3)] == [None, 0.0, 12.5]
    assert [ch.est_time(i) for i in range(3)] == [None, None, 40.25]
    assert [ch.last_lap(i) for i in range(3)] == [None, None, 95.1]
    assert [ch.best_lap(i) for i in range(3)] == [None, 94.0, 95.1]


def test_background_flag_bits_are_masked_off_the_driver_flags():
    """`servicible` is set on nearly every car nearly always; a nonzero here must mean
    the car has actually been shown a flag."""
    ch = Channels(_frame({"CarIdxTrackSurface": [1, 1],
                          "CarIdxSessionFlags": [SessionFlag.SERVICIBLE,
                                                 SessionFlag.SERVICIBLE | SessionFlag.BLACK]}))
    assert ch.driver_flags(0) == 0
    assert ch.driver_flags(1) == SessionFlag.BLACK
    assert ch.driver_flags(1) & DRIVER_FLAG_MASK == ch.driver_flags(1)
