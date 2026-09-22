from itertools import pairwise

from pylon.overlay import snapshot_to_model
from pylon.telemetry import SyntheticSource
from pylon.telemetry.constants import SessionFlag, TrackSurface
from pylon.world import WorldModel


def test_structure_and_scenarios():
    src = SyntheticSource(num_cars=12, duration_s=120.0, hz=10.0, seed=7, incident_at=60.0)
    frames = list(src.frames())
    assert len(frames) == 1200

    active_counts = set()
    positions_ok = True
    ldp_ok = True
    saw_battle = False
    saw_incident = False
    saw_yellow = False

    for fr in frames:
        pos = fr.get("CarIdxPosition")
        ldp = fr.get("CarIdxLapDistPct")
        surf = fr.get("CarIdxTrackSurface")
        f2 = fr.get("CarIdxF2Time")

        active = [i for i, p in enumerate(pos) if p > 0]
        active_counts.add(len(active))

        # positions are a clean 1..N permutation of the active cars
        if sorted(pos[i] for i in active) != list(range(1, len(active) + 1)):
            positions_ok = False
        for i in active:
            if not (0.0 <= ldp[i] <= 1.0):
                ldp_ok = False

        order = sorted(active, key=lambda i: pos[i])
        for a, b in pairwise(order):
            if f2[a] >= 0 and f2[b] >= 0 and 0.0 <= (f2[b] - f2[a]) < 0.5:
                saw_battle = True

        if any(surf[i] == TrackSurface.OFF_TRACK for i in active):
            saw_incident = True
        if fr.get("SessionFlags") & SessionFlag.YELLOW:
            saw_yellow = True

    assert active_counts == {12}
    assert positions_ok
    assert ldp_ok
    assert saw_battle
    assert saw_incident
    assert saw_yellow


def test_deterministic_given_seed():
    a = list(SyntheticSource(num_cars=6, duration_s=3.0, hz=10.0, seed=42).frames())
    b = list(SyntheticSource(num_cars=6, duration_s=3.0, hz=10.0, seed=42).frames())
    assert [f.values["CarIdxLapDistPct"] for f in a] == [f.values["CarIdxLapDistPct"] for f in b]


def test_arrays_are_max_cars_length():
    fr = next(SyntheticSource(num_cars=20, duration_s=1.0, hz=1.0).frames())
    assert len(fr.get("CarIdxLapDistPct")) == 64
    assert len(fr.get("CarIdxPosition")) == 64


# --------------------------------------------------------------------------- #
# CarIdxEstTime (#61). Without it every synthetic-backed test ran the FALLBACK
# gap path while live ran the est one, so the shipped code was unexercised by
# anything in CI: the mirror image of this repo's usual failure mode.
# --------------------------------------------------------------------------- #


def test_est_time_is_one_shared_ruler_not_a_per_car_pace():
    """The property est_track_gap rests on: EstTime maps TRACK POSITION to seconds,
    identically for every car. The roster deliberately runs a spread of lap times, so
    a per-car map (the tempting mistake) would make est/ldp differ car to car, and
    would leave every cross-car difference meaningless while still looking populated.
    """
    src = SyntheticSource(num_cars=16, duration_s=30.0, hz=10.0, seed=4)
    assert len({round(c["lap_time"], 3) for c in src._cars}) > 1  # pace really does vary

    fr = list(src.frames())[-1]
    ldp, est = fr.get("CarIdxLapDistPct"), fr.get("CarIdxEstTime")
    rulers = {round(est[i] / ldp[i], 6) for i in range(16) if ldp[i] > 0.01}
    assert len(rulers) == 1
    assert rulers.pop() == round(src.est_lap_s, 6)


def test_est_time_uses_the_no_data_sentinel_off_the_roster():
    """Slots for cars that are not in the world read <= 0, like CarIdxF2Time: the
    builder spells 'no reading' as non-positive, so a 0.0 there would become a real
    one. In-world cars are never 0.0 for the same reason, even sitting on the line."""
    fr = next(SyntheticSource(num_cars=8, duration_s=1.0, hz=1.0).frames())
    est = fr.get("CarIdxEstTime")
    assert all(est[i] > 0.0 for i in range(8))
    assert all(est[i] <= 0.0 for i in range(8, 64))


def test_the_est_span_is_learned_from_a_synthetic_run():
    """End to end through the world model: est_lap was None on every synthetic feed,
    which is what kept the est branch from ever engaging."""
    src = SyntheticSource(num_cars=12, duration_s=200.0, hz=10.0, seed=6)
    wm = WorldModel(src.session_info())
    snap = None
    for fr in src.frames():
        snap = wm.update(fr)

    assert snap.est_lap is not None
    assert abs(snap.est_lap - src.est_lap_s) < 0.5
    assert all(snap.cars[i].est_time is not None for i in snap.order)


def test_the_synthetic_interval_column_no_longer_jumps():
    """What #61 is FOR. The same like-for-like filter #55 measured with (consecutive
    frames, same car ahead, lead lap, neither car pitting): 20 jumps above 0.5s on
    this run through the fallback chain, and none once the ruler is emitted."""
    src = SyntheticSource(num_cars=8, duration_s=120.0, hz=10.0, seed=7)
    wm = WorldModel(src.session_info())
    prev, jumps = {}, 0
    for fr in src.frames():
        snap = wm.update(fr)
        model = snapshot_to_model(snap, "Test", track_len=src.track_length_m)
        cur = {}
        for row in model["cars"]:
            c = snap.cars.get(row["id"])
            ahead = snap.cars.get(c.car_ahead_idx) if c and c.car_ahead_idx is not None else None
            key = (c.car_ahead_idx, row["intervalLaps"]) if c else None
            cur[row["id"]] = (row["interval"], key)
            if ahead is None or row["intervalLaps"] or ahead.on_pit_road or c.on_pit_road:
                continue
            was = prev.get(row["id"])
            if was is not None and was[1] == key and abs(row["interval"] - was[0]) > 0.5:
                jumps += 1
        prev = cur

    assert jumps == 0
