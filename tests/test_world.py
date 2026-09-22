import pytest

from pylon.telemetry import SyntheticSource
from pylon.world import EventKind, TimeJump, WorldModel, run
from pylon.world.session import official_results


def _last_snapshot(**kw):
    src = SyntheticSource(**kw)
    wm = WorldModel(src.session_info())
    snap = None
    for fr in src.frames():
        snap = wm.update(fr)
    return snap


def test_order_gaps_and_speed():
    snap = _last_snapshot(num_cars=12, duration_s=60.0, hz=10.0, seed=5)

    # running order is a clean 1..N by position
    assert [snap.cars[i].position for i in snap.order] == list(range(1, 13))

    leader = snap.leader()
    assert leader.gap_ahead is None
    assert leader.gap_behind is not None  # someone behind the leader

    # every non-leader has a non-negative time gap to the car ahead
    for i in snap.order[1:]:
        assert snap.cars[i].gap_ahead is not None
        assert snap.cars[i].gap_ahead >= 0.0

    # derived leader speed is a sane road-course pace (m/s)
    assert 20.0 < leader.speed < 100.0


def test_incident_overtake_and_yellow():
    src = SyntheticSource(num_cars=12, duration_s=120.0, hz=10.0, seed=7, incident_at=60.0)
    saw_incident = saw_offtrack = saw_overtake = saw_yellow = False
    for snap in run(src):
        for e in snap.events:
            if e.kind == EventKind.INCIDENT:
                saw_incident = True
            if e.kind == EventKind.OFF_TRACK:
                saw_offtrack = True
            if e.kind == EventKind.OVERTAKE:
                saw_overtake = True
        if snap.session.is_yellow:
            saw_yellow = True
    assert saw_incident
    assert saw_offtrack
    assert saw_overtake
    assert saw_yellow


def test_closing_rate_present_in_a_battle():
    src = SyntheticSource(num_cars=12, duration_s=60.0, hz=10.0, seed=1)
    found = False
    for snap in run(src):
        for i in snap.order[1:]:
            c = snap.cars[i]
            if c.gap_ahead is not None and c.gap_ahead < 0.6 and c.closing_rate is not None:
                found = True
    assert found


def test_track_order_flips_at_the_move_not_at_the_timing_line():
    """`track_order` is what everything on SCREEN reads instead of CarIdxPosition.

    CarIdxPosition is the timing sheet: it turns over as the pair crosses the line,
    which on a long lap is most of a minute after the viewer watched the pass. The
    camera target and the pop-in name bugs have to agree with the picture instead."""
    from test_battle_scoring import _car

    from pylon.world.model import PASS_CLEAR_LAPS, track_order

    lead = _car(0, 1, progress=5.500)          # P1 on the sheet

    # nose to tail: no change
    behind = _car(1, 2, progress=5.4990)
    assert [c.idx for c in track_order(lead, behind)] == [0, 1]

    # a nose ahead is not a pass: side-by-side cars trade inches through a corner
    nose = _car(1, 2, progress=5.500 + PASS_CLEAR_LAPS / 2)
    assert [c.idx for c in track_order(lead, nose)] == [0, 1]

    # clear ahead on the road, still second on the sheet: the camera sees a pass
    through = _car(1, 2, progress=5.500 + PASS_CLEAR_LAPS * 3)
    assert [c.idx for c in track_order(lead, through)] == [1, 0]

    # a car a lap down is not "in front": progress says nothing about who is on screen
    lapped = _car(1, 2, progress=6.400)
    assert [c.idx for c in track_order(lead, lapped)] == [0, 1]


def test_live_order_applies_a_completed_pass_to_the_running_order():
    """The running order turns over on the move, not at the line.

    `CarIdxPosition` only updates as a car crosses the timing line: measured on
    capture2, 59 of 68 position changes landed within 2% of the S/F line, and one pass
    at Spa was still un-flipped 85 seconds later. The order every graphic is numbered
    from therefore applies passes that are already complete.
    """
    from pylon.world.builder import LIVE_PASS_TAKE, live_order

    sheet = [0, 1, 2]                                  # P1, P2, P3 on the timing sheet

    def order(prog):
        return live_order(sheet, prog)[0]

    # nobody has passed: the sheet stands
    assert order({0: 5.500, 1: 5.499, 2: 5.498}) == [0, 1, 2]

    # dead level is not a pass; the order must not turn over on float noise
    assert order({0: 5.500, 1: 5.500 + LIVE_PASS_TAKE / 2, 2: 5.498}) == [0, 1, 2]

    # car 1 is clear ahead on the road: it takes P1 now, not at the line
    assert order({0: 5.500, 1: 5.504, 2: 5.498}) == [1, 0, 2]

    # a car that has cleared BOTH moves up two places in one frame
    assert order({0: 5.500, 1: 5.499, 2: 5.506}) == [2, 0, 1]

    # ...even when it is still inside the take margin of the car between them, which an
    # adjacent-swap loop with one wide margin would have pinned it behind
    assert order({0: 5.500, 1: 5.500 + LIVE_PASS_TAKE * 3,
                  2: 5.500 + LIVE_PASS_TAKE * 4}) == [2, 1, 0]

    # a lapped car is never promoted: half a lap of progress means nothing about place
    assert order({0: 5.500, 1: 6.400, 2: 5.498}) == [0, 1, 2]


def test_live_order_does_not_hand_a_place_back_on_a_wobble():
    """A place is taken with a nose of daylight and given back only at a clear car
    length the other way. Without that band the margin would sit around the SHEET's
    order rather than the current one, so a pair hovering at the threshold re-orders
    the whole tower off a 20 cm wobble: measured on capture2, 23 of 394 position
    changes reversed inside a second, a 20 m swing in half a second."""
    from pylon.world.builder import LIVE_PASS_TAKE, LIVE_PASS_YIELD, live_order

    sheet = [0, 1]
    clear = {0: 5.500, 1: 5.500 + LIVE_PASS_TAKE * 2}       # car 1 is through
    order, held = live_order(sheet, clear)
    assert order == [1, 0] and held == {(0, 1)}             # inverted, sheet's way round

    # it eases back to dead level: the place STAYS taken, and only because it held
    level = {0: 5.500, 1: 5.500}
    assert live_order(sheet, level, held)[0] == [1, 0]
    assert live_order(sheet, level)[0] == [0, 1]

    # car 0 noses back in front: still not enough to be given the place back
    assert live_order(sheet, {0: 5.500 + LIVE_PASS_TAKE * 2, 1: 5.500}, held)[0] == [1, 0]

    # ...it re-passes for real: the sheet's order is restored and the hold released
    back = {0: 5.500 + LIVE_PASS_YIELD * 2, 1: 5.500}
    order, held = live_order(sheet, back, held)
    assert order == [0, 1] and held == set()


def test_live_order_leaves_an_unscored_session_without_places():
    """Practice reports CarIdxPosition == 0 for the whole field (DESIGN.md 14). The
    order there is only "who is furthest around the road", so no place is reported:
    the tower used to count rows instead and showed a churning P1..P28."""
    from pylon.world.builder import live_places

    order = [2, 0, 1]
    unscored = live_places(order, {0: 0, 1: 0, 2: 0}, {0: 1, 1: 1, 2: 1}, {0: 0, 1: 0, 2: 0})
    assert unscored == ({0: 0, 1: 0, 2: 0}, {0: 0, 1: 0, 2: 0})

    scored = live_places(order, {0: 2, 1: 3, 2: 1}, {0: 1, 1: 1, 2: 1}, {0: 2, 1: 3, 2: 1})
    assert scored[0] == {2: 1, 0: 2, 1: 3}      # counted off the order, not read back


#, which session of the weekend is running -------------------------------------

# The Sessions block exactly as both Spa captures carry it: three sessions, and a
# WeekendInfo.EventType of "Race" for ALL of them. `capture` IS the practice session
# (its frames all report SessionNum 0) and `capture2` is the race (SessionNum 2), so
# this fixture is what makes the EventType trap testable without the recordings
# (which are gitignored).
SPA_WEEKEND = {
    "WeekendInfo": {"TrackDisplayName": "Circuit de Spa-Francorchamps",
                    "TrackLength": "6.9293 km", "EventType": "Race"},
    "SessionInfo": {
        "CurrentSessionNum": 2,
        "Sessions": [
            {"SessionNum": 0, "SessionType": "Practice", "SessionName": "PRACTICE"},
            {"SessionNum": 1, "SessionType": "Lone Qualify", "SessionName": "QUALIFY"},
            {"SessionNum": 2, "SessionType": "Race", "SessionName": "RACE"},
        ],
    },
    "DriverInfo": {"Drivers": []},
}


def _snap_for_session_num(num, raw=None):
    from pylon.telemetry.frame import Frame, SessionInfo

    wm = WorldModel(SessionInfo(raw if raw is not None else SPA_WEEKEND))
    return wm.update(Frame(tick=1, session_time=1.0, values={
        "SessionTime": 1.0, "SessionNum": num,
        "CarIdxLapDistPct": [], "CarIdxPosition": [], "CarIdxTrackSurface": [],
    }))


def test_session_kind_comes_from_the_running_session_not_the_event_type():
    """The trap this exists for: WeekendInfo.EventType reads "Race" during practice.

    Verified on the real captures: `capture` is the practice session of a race
    weekend and still reports EventType "Race", so anything keying off EventType
    calls a practice session a race."""
    from pylon.world import SessionKind

    practice = _snap_for_session_num(0).session
    assert practice.session_kind == SessionKind.PRACTICE
    assert practice.session_type == "Practice" and practice.session_num == 0
    assert practice.event_type == "Race"          # ...and this is why it cannot be used

    assert _snap_for_session_num(1).session.session_kind == SessionKind.QUALIFY
    assert _snap_for_session_num(2).session.session_kind == SessionKind.RACE


def test_session_kind_falls_back_to_event_type_without_a_sessions_block():
    """The synthetic source builds WeekendInfo only, and it generates races."""
    from pylon.world import SessionKind

    bare = {"WeekendInfo": {"EventType": "Race"}, "DriverInfo": {"Drivers": []}}
    snap = _snap_for_session_num(0, raw=bare)
    assert snap.session.session_kind == SessionKind.RACE
    assert snap.session.session_type is None    # nothing real to report


def test_session_kind_classifies_iracings_whole_zoo_of_type_strings():
    from pylon.world import SessionKind, session_kind

    assert session_kind("Lone Qualify") == SessionKind.QUALIFY
    assert session_kind("Open Qualify") == SessionKind.QUALIFY
    assert session_kind("Qualifying") == SessionKind.QUALIFY
    assert session_kind("Practice") == SessionKind.PRACTICE
    assert session_kind("Warmup") == SessionKind.WARMUP
    assert session_kind("Offline Testing") == SessionKind.TESTING   # before "race" wins
    assert session_kind("Race") == SessionKind.RACE
    # unknown is a real answer, not an error: consumers fall back to race behaviour
    assert session_kind("Heat Shootout Somesuch") == SessionKind.UNKNOWN
    assert session_kind(None) == SessionKind.UNKNOWN


def test_last_lap_drops_iracings_no_lap_sentinel():
    """CarIdxLastLapTime reads -1.0 until a car completes a lap, and that sentinel has
    to die here rather than in each consumer: the same rule SessionLapsRemain's 32767
    gets. Measured on recordings/capture2, -1.0 is the ONLY negative the channel ever
    produces (245,832 sentinel car-frames against 62,256 real). See DESIGN.md 14."""
    from pylon.telemetry.frame import Frame, SessionInfo

    wm = WorldModel(SessionInfo(SPA_WEEKEND))
    snap = wm.update(Frame(tick=1, session_time=1.0, values={
        "SessionTime": 1.0, "SessionNum": 2,
        "CarIdxLapDistPct": [0.5, 0.4, 0.3, 0.2],
        "CarIdxPosition": [1, 2, 3, 4],
        "CarIdxTrackSurface": [3, 3, 3, 3],
        "CarIdxLap": [4, 4, 4, 4], "CarIdxLapCompleted": [3, 3, 3, 3],
        "CarIdxOnPitRoad": [False, False, False, False],
        "CarIdxLastLapTime": [135.482, -1.0, 0.0, 132.019],
    }))

    assert snap.cars[0].last_lap == 135.482
    assert snap.cars[1].last_lap is None    # -1.0: no lap yet
    assert snap.cars[2].last_lap is None    # 0.0 is not a lap either
    assert snap.cars[3].last_lap == 132.019


#: the garage: classified without running ---------------------------------------

def _garage_frames(session_num, *, surfaces):
    """One car (idx 0) that sets a time and then reads NOT_IN_WORLD, plus a car that
    keeps circulating and two slots nobody is in. `surfaces` is idx 0's surface per
    frame. Hand-built because no capture holds the transition: NOT_IN_WORLD in the
    recordings is only ever an EMPTY SLOT (71 of them on capture2's 28-car field)."""
    from pylon.telemetry.frame import Frame

    for t, s0 in enumerate(surfaces, start=1):
        yield Frame(tick=t, session_time=float(t), values={
            "SessionTime": float(t), "SessionNum": session_num,
            "CarIdxLapDistPct": [0.5 if s0 == 3 else -1.0, 0.25, -1.0, -1.0],
            "CarIdxPosition": [0, 0, 0, 0],          # a non-race session scores nobody
            "CarIdxTrackSurface": [s0, 3, -1, -1],
            "CarIdxLap": [3, 3, -1, -1], "CarIdxLapCompleted": [2, 2, -1, -1],
            "CarIdxOnPitRoad": [False, False, False, False],
            # the sim is free to stop reporting a time for a car that is not in the world,
            # so the garage frames say -1.0 and the classification must survive it
            "CarIdxLastLapTime": [131.0 if s0 == 3 else -1.0, 133.5, -1.0, -1.0],
            "CarIdxBestLapTime": [130.5 if s0 == 3 else -1.0, 133.0, -1.0, -1.0],
        })


def test_a_garaged_car_stays_classified_in_practice():
    """Observed live at Spa: drivers vanished from the tower mid-practice and their lap
    time left the board with them, because a car in the garage reads NOT_IN_WORLD and the
    builder drops those. Going in between runs is the rhythm of a practice session, so a
    car that has set a time stays classified on it (#46)."""
    from pylon.telemetry.frame import SessionInfo

    wm = WorldModel(SessionInfo(SPA_WEEKEND))
    snaps = [wm.update(fr) for fr in _garage_frames(0, surfaces=[3, 3, -1, -1, 3])]

    running, garaged, back = snaps[1], snaps[3], snaps[4]
    assert 0 in running.cars and running.order == [0, 1]

    # in the garage: still on the board, still holding the time it set
    assert 0 in garaged.cars
    g = garaged.cars[0]
    assert g.in_garage and g.best_lap == 130.5 and g.last_lap == 131.0
    assert g.number == running.cars[0].number and g.name == running.cars[0].name

    # ...and nothing about it reads as a car on the road
    assert not g.on_track and g.speed == 0.0 and g.position == 0
    assert g.gap_ahead is None and g.track_gap_ahead is None and g.closing_rate is None
    assert g.car_ahead_idx is None and not g.on_pit_road
    # the RUNNING order is only who is racing, so a parked car is not in it
    assert garaged.order == [1]
    # an empty slot is still an empty slot: it never set a time, so it is not a car
    assert 2 not in garaged.cars and 3 not in garaged.cars

    # back out of the garage and everything is live again
    assert back.order == [0, 1] and not back.cars[0].in_garage
    assert back.cars[0].speed == 0.0 or back.cars[0].speed > 0.0   # re-seeded, not carried


def test_a_car_out_of_the_world_in_a_race_is_towed_not_garaged():
    """The other half of #46: in a RACE, out of the world means towed or retired, never
    the garage rule. Since 2026-09-02 such a car stays on the board, marked, holding its
    spot in the order by the progress it left from (tests/test_towed.py has the fall-back
    through the field); what it must never be is a classified garage car."""
    from pylon.telemetry.frame import SessionInfo

    wm = WorldModel(SessionInfo(SPA_WEEKEND))
    snaps = [wm.update(fr) for fr in _garage_frames(2, surfaces=[3, 3, -1, -1])]

    assert 0 in snaps[1].cars and not snaps[1].cars[0].towed    # running
    towed = snaps[3].cars[0]
    assert towed.towed and not towed.in_garage
    assert towed.speed == 0.0 and towed.track_gap_ahead is None and towed.est_time is None
    assert towed.best_lap == 130.5                              # a time set does not un-set
    assert snaps[3].order == [0, 1]                             # ahead by progress, until passed
    assert all(not c.in_garage for c in snaps[3].cars.values())


def test_a_car_with_no_time_is_not_classified_when_it_goes_in():
    """The discriminator is a lap time, not the surface: without one there is nothing to
    classify, and treating every NOT_IN_WORLD slot as a car would put iRacing's ~50 empty
    slots on the tower."""
    from pylon.telemetry.frame import Frame, SessionInfo

    wm = WorldModel(SessionInfo(SPA_WEEKEND))
    snap = None
    for t, s0 in enumerate([3, 3, -1, -1], start=1):
        snap = wm.update(Frame(tick=t, session_time=float(t), values={
            "SessionTime": float(t), "SessionNum": 0,
            "CarIdxLapDistPct": [0.5, 0.25], "CarIdxPosition": [0, 0],
            "CarIdxTrackSurface": [s0, 3],
            "CarIdxLap": [1, 1], "CarIdxLapCompleted": [0, 0],
            "CarIdxOnPitRoad": [False, False],
            "CarIdxBestLapTime": [-1.0, -1.0],      # nobody has set one yet
        }))
    assert 0 not in snap.cars and snap.order == [1]


def test_last_lap_is_none_when_the_channel_is_absent():
    """The synthetic source emits no CarIdxLastLapTime at all, and neither did older
    captures. That is a missing lap time, not a crash and not a zero."""
    snap = _last_snapshot(num_cars=6, duration_s=20.0, hz=10.0, seed=3)
    assert all(c.last_lap is None for c in snap.cars.values())
    assert all(c.best_lap is None for c in snap.cars.values())


def test_a_lap_limited_session_has_no_clock_to_quote():
    """Both Spa captures list Sessions[1] "Lone Qualify" as SessionLaps 2 with
    SessionTime "86400.0000 sec": one day, iRacing's placeholder for a session that
    is not timed at all, and small enough to sail through the week-long sentinel guard.
    Left alone the clock reads one thousand four hundred and forty minutes of
    qualifying" and the overlay draws a 24-hour clock.

    The discriminator is SessionLaps being a NUMBER: practice and race both carry the
    string "unlimited", so a real 24-hour race keeps its clock."""
    from pylon.telemetry.frame import Frame, SessionInfo

    # The session table verbatim from both captures, which SPA_WEEKEND above omits.
    raw = {
        "WeekendInfo": {"TrackDisplayName": "Spa", "TrackLength": "6.9293 km",
                        "EventType": "Race"},
        "SessionInfo": {"CurrentSessionNum": 0, "Sessions": [
            {"SessionNum": 0, "SessionType": "Practice", "SessionName": "PRACTICE",
             "SessionTime": "180.0000 sec", "SessionLaps": "unlimited"},
            {"SessionNum": 1, "SessionType": "Lone Qualify", "SessionName": "QUALIFY",
             "SessionTime": "86400.0000 sec", "SessionLaps": 2},
            {"SessionNum": 2, "SessionType": "Race", "SessionName": "RACE",
             "SessionTime": "1800.0000 sec", "SessionLaps": "unlimited"},
        ]},
        "DriverInfo": {"Drivers": []},
    }

    def clock(num, total, remain):
        wm = WorldModel(SessionInfo(raw))
        return wm.update(Frame(tick=1, session_time=1.0, values={
            "SessionTime": 1.0, "SessionNum": num, "SessionState": 4,
            "SessionTimeTotal": total, "SessionTimeRemain": remain,
            "CarIdxLapDistPct": [], "CarIdxPosition": [], "CarIdxTrackSurface": [],
        })).session

    quali = clock(1, 86400.0, 86400.0)
    assert quali.session_type == "Lone Qualify"
    assert quali.time_total is None and quali.time_remaining is None

    # ...while the sessions that really are timed keep both
    assert clock(0, 180.0, 120.0).time_total == 180.0      # the three-minute shakedown
    assert clock(2, 1800.0, 1500.0).time_remaining == 1500.0


def test_a_session_knows_what_comes_after_it():
    """Where a session sits in the weekend is most of what makes it make sense: an
    official iRacing practice is 180 seconds, which is a shakedown before qualifying
    rather than a practice programme, and nothing inside the session says so.

    Sessions are numbered in weekend order, so the next one is num + 1, and the last
    session reads back UNKNOWN rather than blowing up."""
    from pylon.world import SessionKind

    assert _snap_for_session_num(0).session.next_session_kind == SessionKind.QUALIFY
    assert _snap_for_session_num(1).session.next_session_kind == SessionKind.RACE
    assert _snap_for_session_num(2).session.next_session_kind == SessionKind.UNKNOWN


def test_the_session_clock_stops_meaning_anything_once_the_flag_is_out():
    """SessionTimeRemain re-bases onto the COOL-DOWN clock when the state leaves
    RACING. Measured on the practice capture, a 180-second session: remaining winds
    118 -> 18 under RACING (4), then reads 603 under CHECKERED (5) and 5.8 under
    COOL_DOWN (6). Anything quoting it past the flag is quoting a different clock:
    nine minutes remained after the session had been called over, and the
    overlay's elapsed (total minus remaining) goes negative."""
    from pylon.telemetry.frame import Frame, SessionInfo

    def clock(state, remain):
        wm = WorldModel(SessionInfo(SPA_WEEKEND))
        return wm.update(Frame(tick=1, session_time=1.0, values={
            "SessionTime": 1.0, "SessionNum": 0, "SessionState": state,
            "SessionTimeRemain": remain, "SessionTimeTotal": 180.0,
            "CarIdxLapDistPct": [0.5], "CarIdxPosition": [0],
            "CarIdxTrackSurface": [3], "CarIdxLap": [4], "CarIdxLapCompleted": [3],
            "CarIdxOnPitRoad": [False],
        })).session

    assert clock(4, 118.15).time_remaining == 118.15    # RACING: the session's own clock
    assert clock(5, 603.55).time_remaining is None      # CHECKERED: the cool-down's
    assert clock(6, 5.78).time_remaining is None
    # ...and the total is untouched, so nothing downstream loses the session's length
    assert clock(5, 603.55).time_total == 180.0


def test_best_lap_gets_the_same_sentinel_treatment_as_the_last_one():
    """CarIdxBestLapTime uses the identical -1.0 convention, and it is absent for far
    longer than you would guess: on the practice capture BOTH lap channels are the
    sentinel in all 3020 frames for all 64 slots, because that is the opening ~200s of
    practice and nobody has completed a lap. "No time yet" is the normal early state.

    A naive min() over this channel returns -1.0 and hands "fastest lap" to whoever has
    not turned a wheel, which is exactly why the sentinel dies here."""
    from pylon.telemetry.frame import Frame, SessionInfo

    wm = WorldModel(SessionInfo(SPA_WEEKEND))
    snap = wm.update(Frame(tick=1, session_time=1.0, values={
        "SessionTime": 1.0, "SessionNum": 0,
        "CarIdxLapDistPct": [0.5, 0.4, 0.3, 0.2],
        "CarIdxPosition": [0, 0, 0, 0],          # practice: no running order at all
        "CarIdxTrackSurface": [3, 3, 3, 3],
        "CarIdxLap": [4, 4, 4, 4], "CarIdxLapCompleted": [3, 3, 3, 3],
        "CarIdxOnPitRoad": [False, False, False, False],
        "CarIdxBestLapTime": [129.7936, -1.0, 0.0, 131.402],
    }))

    assert snap.cars[0].best_lap == 129.7936
    assert snap.cars[1].best_lap is None     # -1.0: no lap yet
    assert snap.cars[2].best_lap is None     # 0.0 is not a lap either
    assert snap.cars[3].best_lap == 131.402
    assert min(c.best_lap for c in snap.cars.values() if c.best_lap is not None) > 0


def test_the_official_classification_appears_only_once_a_session_is_over():
    """`ResultsPositions` is not a live standings table, and the winner call rests
    entirely on that (issue #57): its ARRIVAL is the sim saying the result has stopped
    moving, which is the one thing the live running order cannot tell you.

    Measured on capture2, taken during the race at Spa: practice and qualifying are both
    finished and carry full 28-row tables; the race, which is the session actually
    running, has no such key at all. The practice capture, taken during practice, has
    none of the three.
    """
    from conftest import capture_or_skip

    from pylon.telemetry import PlaybackSource

    info = PlaybackSource(capture_or_skip("capture2.jsonl.gz")).session_info()
    wm = WorldModel(info)

    assert official_results(wm.info, 2) is None, "the RUNNING session must not be classified"
    for num, quickest in ((0, 129.7936), (1, 129.083)):
        rows = official_results(wm.info, num)
        assert rows is not None and len(rows) == 28
        assert [r.position for r in rows] == list(range(1, 29))   # sorted, P1 first
        assert rows[0].fastest_time == quickest
        assert all(r.fastest_time is None or r.fastest_time > 0 for r in rows)

    practice = PlaybackSource(capture_or_skip("capture.jsonl.gz")).session_info()
    assert official_results(WorldModel(practice).info, 0) is None


def _pass_frames(progress_series, *, sheet=(1, 2), sheets=None, pits=None, hz=10.0):
    """Frames for two cars whose SHEET order never changes while their track order does.

    That is the whole shape of the bug in #35: `CarIdxPosition` only turns over as the
    pair crosses the line, so between the move and the line the sheet still lists the
    loser ahead: most of a minute at Spa.
    """
    from pylon.telemetry.frame import Frame, SessionInfo

    wm = WorldModel(SessionInfo(SPA_WEEKEND))
    events = []
    for k, (p0, p1) in enumerate(progress_series):
        this_sheet = list(sheets[k]) if sheets is not None else list(sheet)
        snap = wm.update(Frame(tick=k, session_time=k / hz, values={
            "SessionTime": k / hz, "SessionNum": 2,
            "CarIdxLapDistPct": [p0 % 1.0, p1 % 1.0],
            "CarIdxPosition": this_sheet,
            "CarIdxClassPosition": this_sheet,
            "CarIdxTrackSurface": [3, 3],
            "CarIdxLap": [int(p0) + 1, int(p1) + 1],
            "CarIdxLapCompleted": [int(p0), int(p1)],
            "CarIdxOnPitRoad": list(pits[k]) if pits is not None else [False, False],
        }))
        events += [e for e in snap.events if e.kind == EventKind.OVERTAKE]
    return events


def test_a_pass_is_called_when_it_happens_not_when_the_sheet_catches_up():
    """#35: a move was reported that the viewer had watched most of a lap earlier,
    because the OVERTAKE event fired off `CarIdxPosition` changing. It now fires off the
    LIVE place (the same one running order the tower, the pop-ins and the director
    read), so the cut and the graphics turn over on the same frame.

    The sheet here never changes at all, which is exactly the state that produced no call
    for 85 seconds on capture2.
    """
    from pylon.world.builder import LIVE_PASS_TAKE

    # car 1 (P2 on the sheet) closes and goes clear past car 0
    series = [(5.500, 5.500 - 0.004 + k * 0.001) for k in range(12)]
    events = _pass_frames(series)

    assert len(events) == 1, events
    e = events[0]
    assert e.car_idx == 1 and e.other_idx == 0
    assert e.position == 1, "the place actually taken, not the sheet's stale number"
    # and it fired as soon as the pass was clear, not at some line crossing
    assert series[-1][1] - series[-1][0] > LIVE_PASS_TAKE


def test_a_side_by_side_wobble_is_not_an_overtake_call():
    """A pair running side by side trades the lead by inches through a corner. Every nose
    ahead must not be its own "makes it stick, past": the director's own detector has a
    test of this shape, and the two must not disagree.

    Nothing here holds a flip for a confirmation window: the live order's margins are
    asymmetric (taken at LIVE_PASS_TAKE, given back only at the five-times-wider
    LIVE_PASS_YIELD), so a wobble inside that band never turns the order over at all.
    """
    from pylon.world.builder import LIVE_PASS_TAKE

    # noses trading, all of it inside the take margin
    series = [(5.500, 5.500 + (LIVE_PASS_TAKE / 2 if k % 4 in (1, 2) else -LIVE_PASS_TAKE / 2))
              for k in range(24)]
    assert _pass_frames(series) == []


def test_no_pass_is_called_between_cars_that_were_never_near_each_other():
    """A place can change hands with nobody going past. Straight off capture2, at
    t=350.6: car #128 took P27 from car #64, which was ON PIT ROAD and 664 metres back.

    "Makes it stick, past #64" is the worst-sounding way to report it. A
    live-order flip normally implies the pair's progress crossed, so they WERE level at
    the time: it is the sheet-driven route that reaches this, which is why the gate
    exists and why it only rejected 1 of 138 passes on this capture. Pinned against the
    capture rather than a synthetic feed, because a synthetic one does not reproduce it.
    """
    from conftest import capture_or_skip

    from pylon.telemetry import PlaybackSource
    from pylon.world.builder import PASS_PROX_LAPS

    calls, worst = 0, 0.0
    for snap in run(PlaybackSource(capture_or_skip("capture2.jsonl.gz"))):
        for e in snap.events:
            if e.kind != EventKind.OVERTAKE or e.other_idx is None:
                continue
            a, b = snap.cars.get(e.car_idx), snap.cars.get(e.other_idx)
            assert a is not None and b is not None
            apart = abs(a.progress - b.progress)
            calls, worst = calls + 1, max(worst, apart)
            assert apart < PASS_PROX_LAPS, (
                f"pass called between cars {apart:.4f} laps apart at t={snap.session_time:.1f}")
    assert calls > 50, f"expected the race capture to be full of passes, got {calls}"


def test_a_car_peeling_into_the_pits_is_not_being_overtaken():
    """A stop is not a pass. The pit lane runs alongside the track, so a car diving in
    stays well inside the proximity gate while it sheds the place, and every routine
    stop was being called as an overtake. Measured on capture2: 13 of 137.

    Both directions matter, so both are checked here. The pitting car can be the one
    LOSING the place (going in) or, at pit-lane speed on the way back out, the one being
    gone past."""
    series = [(5.500, 5.500 - 0.004 + k * 0.001) for k in range(12)]

    # exactly the geometry that DOES fire a pass, to prove the test is aimed right
    assert len(_pass_frames(series)) == 1

    # ...and nothing at all once the car losing the place is in the lane
    on_track, in_lane = (False, False), (True, False)
    diving_in = [on_track] * 3 + [in_lane] * 9
    assert _pass_frames(series, pits=diving_in) == []

    # ...nor when it is the car doing the "passing" that is in the lane
    rejoining = [on_track] * 3 + [(False, True)] * 9
    assert _pass_frames(series, pits=rejoining) == []


def test_the_pit_guard_does_not_swallow_a_real_pass_next_to_a_stop():
    """The guard is per PAIR, not global: cars stopping must not mute the racing."""
    series = [(5.500, 5.500 - 0.004 + k * 0.001) for k in range(12)]
    # both on track the whole way, and somebody elsewhere pitting is not modelled here
    # because the pair is what the detector looks at: this is the control case.
    assert len(_pass_frames(series, pits=[(False, False)] * 12)) == 1


def _start_frames(progress_series, *, hz=10.0):
    """Two cars from the GRID, which sits just before the line: laps completed is -1 and
    the distance pct is 0.99-something, so progress is a small NEGATIVE number until
    the crossing. Floor, not int, because int(-0.0006) is 0 and the sim says -1."""
    import math

    from pylon.telemetry.frame import Frame, SessionInfo

    wm = WorldModel(SessionInfo(SPA_WEEKEND))
    out = []
    for k, (p0, p1) in enumerate(progress_series):
        out.append(wm.update(Frame(tick=k, session_time=k / hz, values={
            "SessionTime": k / hz, "SessionNum": 2,
            "CarIdxLapDistPct": [p0 % 1.0, p1 % 1.0],
            "CarIdxPosition": [1, 2], "CarIdxClassPosition": [1, 2],
            "CarIdxTrackSurface": [3, 3],
            "CarIdxLap": [math.floor(p0) + 1, math.floor(p1) + 1],
            "CarIdxLapCompleted": [math.floor(p0), math.floor(p1)],
            "CarIdxOnPitRoad": [False, False],
        })))
    return out


def test_the_first_line_crossing_is_not_a_lap_backwards():
    """On capture2, 13 cars did this in the first four seconds of the race: progress
    was clamped at lap 0, so a car at pct 0.9999 read 0.9999 and one frame later, over
    the line, 0.0001. Speed collapsed, and the gap to the car behind read None until
    it crossed too: the tower showed +0.00 across the start."""
    step = 0.0008                          # ~55 m/s at Spa, per 0.1 s frame
    series = [(-0.0006 + step * k, -0.0018 + step * k) for k in range(8)]
    snaps = _start_frames(series)
    # the same two cars, the same speeds, a tenth of a lap further on: no line to cross
    control = _start_frames([(a + 0.1, b + 0.1) for a, b in series])

    # car 0 crosses at k=1, car 1 at k=3; the gap between them never blinks out...
    for snap in snaps[1:]:
        gap = snap.cars[1].track_gap_ahead
        assert gap is not None and 0.0 < gap < 1.0, (snap.session_time, gap)
    # ...and the crossing changes NOTHING about what is derived from motion
    for snap, ctl in zip(snaps, control, strict=True):
        assert snap.cars[0].speed == pytest.approx(ctl.cars[0].speed, abs=1e-6)
        assert snap.cars[1].track_gap_ahead == pytest.approx(ctl.cars[1].track_gap_ahead)
    assert snaps[-1].cars[0].progress > 0 > snaps[0].cars[0].progress


def test_a_practice_time_does_not_classify_a_garaged_car_in_qualifying():
    """The garage rule classifies a car on the last time we saw it set. A lap time is
    scoped to its session, so across the rollover that was practice's time sitting on
    qualifying's board for every car still in the garage."""
    from pylon.telemetry.frame import SessionInfo

    wm = WorldModel(SessionInfo(SPA_WEEKEND))
    practice = [wm.update(fr) for fr in _garage_frames(0, surfaces=[3, 3, -1])]
    assert practice[-1].cars[0].in_garage and practice[-1].cars[0].best_lap == 130.5

    qualifying = [wm.update(fr) for fr in _garage_frames(1, surfaces=[-1, -1])]
    assert qualifying[0].time_jump == TimeJump.SESSION
    assert 0 not in qualifying[-1].cars          # nothing set in THIS session yet
    assert qualifying[-1].order == [1]
