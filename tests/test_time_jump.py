"""Session-clock discontinuities: a backwards jump must not wedge the broadcast.

Observed live on 2026-07-25 (issue #41). The sim box was in replay and the tape was
scrubbed back, so `session_time` went from 1800.6 to 557.6. The director stopped
cutting entirely and stayed stopped for the rest of the session: `held = t - started`
was about -1243s, which is under `min_shot` on every subsequent frame, so `update()`
returned None forever while the process looked perfectly healthy (bridge ESTAB, frames
arriving, log silent). The sim camera sat where it last pointed.

Session time is every stateful component's clock, so the same jump reaches the world
model (negative progress deltas), TalkPolicy (cooldowns stored as `t + dur` land in
the far future) and SessionMemory (sample clocks and time series). The discontinuity is
therefore detected ONCE in the world model and published on the snapshot as
`time_jump`; these tests pin both the detection and each consumer's reaction.

This matters beyond the bug: an instant replay (#16-#20) seeks the sim's tape
deliberately, so it drives a backwards jump on purpose, every time it fires.
"""

from pylon.director import Director, DirectorConfig
from pylon.telemetry.constants import (
    MAX_CARS,
    SessionFlag,
    SessionState,
    TrackSurface,
)
from pylon.telemetry.frame import Frame, SessionInfo
from pylon.world import TimeJump, WorldModel

TRACK_M = 5000.0


def _session_info(n_cars: int = 4) -> SessionInfo:
    return SessionInfo({
        "WeekendInfo": {"TrackDisplayName": "Test", "TrackLength": "5.00 km",
                        "EventType": "Race"},
        "DriverInfo": {"Drivers": [
            {"CarIdx": i, "CarNumber": str(i + 1), "UserName": f"D{i}",
             "CarClassID": 1, "CarIsPaceCar": False, "CarIsAI": True}
            for i in range(n_cars)
        ]},
    })


def _frame(tick: int, t: float, dist: dict[int, float], *, session_num: int = 2,
           surfaces: dict[int, int] | None = None) -> Frame:
    """One frame at an explicit session time, from absolute per-car distances."""
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
        surf[i] = (surfaces or {}).get(i, TrackSurface.ON_TRACK)
        f2full[i] = 0.5 * (p - 1)
    return Frame(tick=tick, session_time=t, values={
        "SessionTime": t, "SessionTick": tick, "SessionNum": session_num,
        "SessionState": SessionState.RACING, "SessionFlags": SessionFlag.GREEN,
        "SessionTimeRemain": 999.0, "SessionLapsRemain": -1,
        "CarIdxLapDistPct": ldp, "CarIdxLap": lap, "CarIdxLapCompleted": lapc,
        "CarIdxPosition": pos, "CarIdxClassPosition": cpos, "CarIdxClass": cls,
        "CarIdxOnPitRoad": onpit, "CarIdxTrackSurface": surf, "CarIdxF2Time": f2full,
    })


def _fight(t: float, *, tight_pair: tuple[int, int]) -> dict[int, float]:
    """Four cars, with one chosen pair nose-to-tail and the other pair strung out.

    Which pair is close decides what the director wants to be on, so moving it is how
    these tests make a cut genuinely warranted rather than merely permitted.
    """
    a, b = tight_pair
    base = {i: 3000.0 + 55.0 * t - 400.0 * i for i in range(4)}
    base[b] = base[a] - 8.0     # ~0.15s back: side by side
    return base


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

def test_ordinary_telemetry_is_never_a_jump():
    """The guard has to be invisible in normal running, including across a dropped
    frame: a stream hiccup is a forward GAP, not a scrub, and resetting on one would
    throw away speed and closing state for no reason."""
    wm = WorldModel(_session_info())
    t = 100.0
    jumps = []
    for k in range(60):
        t += 5.0 if k == 30 else 0.1     # one 5s stall partway through
        jumps.append(wm.update(_frame(k, t, _fight(t, tight_pair=(0, 1)))).time_jump)
    assert jumps[0] is None              # even the first frame is not a "jump"
    assert set(jumps) == {None}


def test_backwards_scrub_is_flagged_and_forward_scrub_is_too():
    wm = WorldModel(_session_info())
    for k in range(20):
        t = 1800.0 + k * 0.1
        wm.update(_frame(k, t, _fight(t, tight_pair=(0, 1))))
    back = wm.update(_frame(20, 557.6, _fight(557.6, tight_pair=(0, 1))))
    assert back.time_jump == TimeJump.BACK

    for k in range(20):                  # settle again on the new clock
        t = 557.7 + k * 0.1
        wm.update(_frame(k, t, _fight(t, tight_pair=(0, 1))))
    fwd = wm.update(_frame(40, 3000.0, _fight(3000.0, tight_pair=(0, 1))))
    assert fwd.time_jump == TimeJump.FORWARD


def test_session_rollover_is_flagged_even_though_time_looks_sane():
    """A practice -> qualifying -> race weekend crosses SessionNum twice, and session
    time restarts at each. Caught on SessionNum rather than on the clock, because the
    new session's time can legitimately be *larger* than the old one's."""
    wm = WorldModel(_session_info())
    for k in range(20):
        t = 100.0 + k * 0.1
        wm.update(_frame(k, t, _fight(t, tight_pair=(0, 1)), session_num=1))
    snap = wm.update(_frame(20, 102.1, _fight(102.1, tight_pair=(0, 1)), session_num=2))
    assert snap.time_jump == TimeJump.SESSION


def test_no_events_are_emitted_off_a_discontinuity():
    """The replay-poisoning requirement from #18, and a correctness one for #41: a car
    that has been in the gravel for a minute must not read as having JUST gone off
    because the frame before it belongs to another timeline."""
    wm = WorldModel(_session_info())
    for k in range(20):
        t = 1800.0 + k * 0.1
        wm.update(_frame(k, t, _fight(t, tight_pair=(0, 1))))
    # scrub back, and have a car off the road at the destination
    snap = wm.update(_frame(20, 500.0, _fight(500.0, tight_pair=(0, 1)),
                            surfaces={2: TrackSurface.OFF_TRACK}))
    assert snap.time_jump == TimeJump.BACK
    assert snap.events == []
    # derived motion is zeroed rather than garbage: a negative progress delta over a
    # negative dt would otherwise hand the director a nonsense speed for every car.
    assert all(c.speed == 0.0 for c in snap.cars.values())
    assert all(c.closing_rate in (None, 0.0) for c in snap.cars.values())


# --------------------------------------------------------------------------- #
# The director: the actual live failure
# --------------------------------------------------------------------------- #

def _drive(wm: WorldModel, director: Director, t0: float, n: int, *,
           tight_pair: tuple[int, int], session_num: int = 2, tick0: int = 0):
    """Run n frames at 10Hz from t0, returning (session time, Decision) for each cut."""
    cuts = []
    for k in range(n):
        t = t0 + k * 0.1
        dec = director.update(wm.update(
            _frame(tick0 + k, t, _fight(t, tight_pair=tight_pair), session_num=session_num)))
        if dec is not None:
            cuts.append((t, dec))
    return cuts


def test_backwards_jump_does_not_stall_the_director():
    """The regression, at the live numbers. Before the fix the director cut at 1800.6
    and then never again: it would have had to wait for the clock to climb back past
    1800.6, about twenty minutes of real time, and another scrub restarted the wait."""
    cfg = DirectorConfig()
    wm, director = WorldModel(_session_info()), Director(cfg)
    _drive(wm, director, 1795.0, 120, tight_pair=(0, 1))
    assert director.current is not None
    before = director.current.key

    # the scrub, then run past min_shot on the new clock. The fight has moved to the
    # other pair, so a cut is warranted: the question is whether one is POSSIBLE.
    after = _drive(wm, director, 557.6, 120, tight_pair=(2, 3), tick0=200)

    assert after, "director went silent after the backwards jump"
    first_t = after[0][0]
    assert first_t - 557.6 <= cfg.min_shot + 0.5, (
        f"first cut came {first_t - 557.6:.1f}s after the jump, not within min_shot")
    assert director.current.key != before


def test_backwards_jump_holds_the_shot_rather_than_twitching_the_camera():
    """#41 is explicit that cutting fresh on every backwards blip is the wrong fix: the
    hysteresis that stops the camera twitching is the whole game (DESIGN.md section 6).
    So the jump frame itself must not produce a cut: it re-seats the clock and holds."""
    wm, director = WorldModel(_session_info()), Director(DirectorConfig())
    _drive(wm, director, 1795.0, 120, tight_pair=(0, 1))
    held = director.current

    t = 557.6
    dec = director.update(wm.update(_frame(500, t, _fight(t, tight_pair=(0, 1)))))
    assert dec is None                       # no cut on the discontinuity itself
    assert director.current is held          # same shot...
    assert director.started == t             # ...re-seated onto the new clock


def test_latched_incidents_do_not_survive_a_backwards_jump():
    """An incident latched at t=1800 can never expire against a clock reading t=557:
    `t - t0 > incident_hold` is negative forever, so it would sit in the candidate list
    at interrupt priority for the rest of the session."""
    wm, director = WorldModel(_session_info()), Director(DirectorConfig())
    _drive(wm, director, 1795.0, 60, tight_pair=(0, 1))
    director._incident_at[3] = ("incident:3:1800.0", 1800.0, "contact")
    director._shown.add("incident:3:1800.0")

    t = 557.6
    director.update(wm.update(_frame(300, t, _fight(t, tight_pair=(0, 1)))))
    assert director._incident_at == {}
    assert director._shown == set()


# --------------------------------------------------------------------------- #
# The world model: same clock, same exposure
# --------------------------------------------------------------------------- #

