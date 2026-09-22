"""Session interpretation on its own: the tape badge's state machine and the pure
snapshot reader, tested without a world model around them.
"""

from pylon.telemetry.constants import SessionFlag, SessionState
from pylon.telemetry.frame import Frame, SessionInfo
from pylon.world import SessionKind
from pylon.world.session import TapeBadge, official_results, session_snapshot

TAPE_TOTAL = 111250   # a saved Nordschleife replay: every sample summed to this


def _frame(t, **values):
    return Frame(tick=int(t * 10), session_time=t, values={"SessionTime": t, **values})


def _tape(t, *, at):
    """A frame from a SAVED tape: position plus remainder is constant."""
    return _frame(t, IsReplayPlaying=1, ReplayFrameNum=at, ReplayFrameNumEnd=TAPE_TOTAL - at)


def _live(t, *, total):
    """A frame from a LIVE session: the tape is being written, so the total climbs."""
    return _frame(t, IsReplayPlaying=1, ReplayFrameNum=total // 2, ReplayFrameNumEnd=total - total // 2)


# --- the tape badge ---------------------------------------------------------------

def test_a_flat_tape_is_believed_only_once_it_has_had_time_to_grow():
    badge = TapeBadge()
    assert badge.showing_tape(_tape(0.0, at=100)) is False
    assert badge.showing_tape(_tape(2.0, at=220)) is False      # inside the settle window
    assert badge.showing_tape(_tape(3.5, at=310)) is True


def test_growth_latches_live_even_when_the_buffer_later_caps():
    """iRacing's replay buffer is finite: a long live session stops gaining frames, and
    that must not turn the race into a replay retrospectively."""
    badge = TapeBadge()
    badge.showing_tape(_live(0.0, total=1100))
    assert badge.showing_tape(_live(1.0, total=1400)) is False   # grew: LIVE, latched
    for t in range(2, 60):
        assert badge.showing_tape(_live(float(t), total=1400)) is False


def test_no_replay_channels_is_live_and_so_is_the_viewer_flag_alone():
    """IsReplayPlaying is 1 for a spectator watching live through the replay viewer."""
    badge = TapeBadge()
    for t in range(10):
        assert badge.showing_tape(_frame(float(t))) is False
    assert badge.showing_tape(_frame(20.0, IsReplayPlaying=1)) is False


def test_reset_rebases_the_settle_window_after_a_scrub_back():
    """A saved tape scrubbed back 40s: the window's start is now in the future, and
    without a reset the badge read LIVE for as long as the scrub was."""
    badge = TapeBadge()
    for t in (100.0, 102.0, 104.0):
        badge.showing_tape(_tape(t, at=int(t * 60)))
    assert badge.showing_tape(_tape(105.0, at=6300)) is True
    assert badge.showing_tape(_tape(60.0, at=3600)) is False     # the fall this guards
    badge.reset()
    assert badge.showing_tape(_tape(60.0, at=3600)) is False     # window restarts here
    assert badge.showing_tape(_tape(63.5, at=3810)) is True


# --- the snapshot reader ----------------------------------------------------------

def _info(sessions=None, event_type="Race"):
    return SessionInfo({
        "WeekendInfo": {"TrackDisplayName": "Test", "TrackLength": "4.00 km",
                        "EventType": event_type},
        "SessionInfo": {"Sessions": sessions or []},
    })


def test_the_clock_dies_past_the_flag():
    """Past the chequer SessionTimeRemain is the cool-down clock, not this session's."""
    snap = session_snapshot(_info(), _frame(50.0, SessionState=SessionState.CHECKERED,
                                            SessionTimeRemain=603.0, SessionTimeTotal=1800.0),
                            is_replay=False)
    assert snap.is_checkered and snap.time_remaining is None
    assert snap.time_total == 1800.0


def test_a_lap_limited_session_has_no_clock():
    """iRacing fills a lap-limited session's SessionTime with one day, which sails past
    the week-long sentinel. SessionLaps being a NUMBER is the discriminator."""
    info = _info([{"SessionNum": 1, "SessionType": "Lone Qualify", "SessionLaps": 2,
                   "SessionTime": "86400.0000 sec"}])
    snap = session_snapshot(info, _frame(5.0, SessionNum=1, SessionTimeRemain=86000.0,
                                         SessionTimeTotal=86400.0), is_replay=False)
    assert snap.session_kind == SessionKind.QUALIFY
    assert snap.time_total is None and snap.time_remaining is None


def test_the_unlimited_sentinels_read_as_no_limit():
    snap = session_snapshot(_info(), _frame(1.0, SessionLapsRemain=32767, SessionTimeRemain=604800.0),
                            is_replay=False)
    assert snap.laps_remaining is None and snap.time_remaining is None


def test_the_field_is_released_unless_something_says_it_is_being_held():
    """Absence of a parade is a race, not a wait: a feed with no SessionState must not
    leave the broadcast on one card for a whole race."""
    assert session_snapshot(_info(), _frame(1.0), is_replay=False).field_released is True
    held = session_snapshot(_info(), _frame(1.0, SessionState=SessionState.RACING,
                                            SessionFlags=SessionFlag.ONE_LAP_TO_GREEN),
                            is_replay=False)
    assert held.field_released is False
    parade = session_snapshot(_info(), _frame(1.0, SessionState=SessionState.PARADE_LAPS),
                              is_replay=False)
    assert parade.field_released is False


def test_green_is_derived_from_state_and_the_absence_of_a_caution():
    racing = session_snapshot(_info(), _frame(1.0, SessionState=SessionState.RACING,
                                              SessionFlags=0), is_replay=False)
    assert racing.is_green
    caution = session_snapshot(_info(), _frame(1.0, SessionState=SessionState.RACING,
                                               SessionFlags=SessionFlag.CAUTION), is_replay=False)
    assert caution.is_yellow and not caution.is_green


def test_the_session_kind_comes_from_the_schedule_then_the_event_type():
    info = _info([{"SessionNum": 0, "SessionType": "Practice"},
                  {"SessionNum": 1, "SessionType": "Race"}])
    snap = session_snapshot(info, _frame(1.0, SessionNum=0), is_replay=False)
    assert snap.session_kind == SessionKind.PRACTICE
    assert snap.next_session_kind == SessionKind.RACE
    bare = session_snapshot(_info(event_type="Race"), _frame(1.0, SessionNum=0), is_replay=False)
    assert bare.session_kind == SessionKind.RACE          # no schedule: the event type


def test_the_badge_verdict_is_passed_through_not_re_derived():
    snap = session_snapshot(_info(), _frame(1.0, IsReplayPlaying=1), is_replay=True)
    assert snap.is_replay is True


def test_official_results_are_sorted_and_absent_while_the_session_runs():
    info = _info([
        {"SessionNum": 0, "SessionType": "Practice", "ResultsPositions": [
            {"Position": 2, "CarIdx": 7, "LapsComplete": 9, "FastestTime": 101.5},
            {"Position": 1, "CarIdx": 3, "LapsComplete": 10, "FastestTime": -1.0},
        ]},
        {"SessionNum": 1, "SessionType": "Race"},
    ])
    rows = official_results(info, 0)
    assert [r.car_idx for r in rows] == [3, 7]
    assert rows[0].fastest_time is None and rows[1].fastest_time == 101.5
    assert official_results(info, 1) is None
    assert official_results(info, None) is None
