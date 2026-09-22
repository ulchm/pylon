"""Session interpretation: which session this is, where it stands, whether it is now.

Read by everything downstream: the director's scene switcher,
the overlay's badge and clock, the replay machine's gate. It lived inside WorldModel
beside the motion maths, and it is what every planned feature reads and extends, so it
has its own module. Nearly all of it is a pure function of session info and one frame;
the exception is the tape badge, which is a claim about what the feed does over time
and so is a small state machine of its own.
"""

from __future__ import annotations

from ..telemetry.constants import SessionFlag, SessionState
from ..telemetry.frame import Frame, SessionInfo
from .model import OfficialResult, SessionKind, SessionSnapshot, session_kind

# The two sides of the same test, each in its own units. A live tape gains 60 frames a
# second, so TAPE_GROWTH_FRAMES is two seconds of growth: more than jitter in a pair of
# frame counters can fake. TAPE_SETTLE_S is how long a tape must stay FLAT before we
# believe it is a finished file instead.
#
# The settle is deliberately the LONGER of the two. They race on every live feed (the
# tape is proving it grows while the clock runs down on calling it flat), and equal
# windows make that a tie decided by sampling jitter. Set to the same 2s, the Spa
# captures each flickered REPLAY for a frame or four at the boundary; the extra second
# is the margin that lets growth always win where there is growth to find.
TAPE_GROWTH_FRAMES = 120
TAPE_SETTLE_S = 3.0

# Real iRacing data (Spa capture) showed the plain `green` bit only flashes at
# starts/restarts; normal green-flag racing carries only background bits
# (servicible/startHidden). So "green" is derived from state + absence of caution,
# not from the green bit. Caution can appear as yellow OR caution (full-course) bits.
YELLOW_MASK = (
    SessionFlag.YELLOW
    | SessionFlag.YELLOW_WAVING
    | SessionFlag.CAUTION
    | SessionFlag.CAUTION_WAVING
)

# iRacing's "unlimited" sentinels. int16 max for a lap count (timed races); about a week
# in seconds for a session clock.
_UNLIMITED_LAPS = 32767
_UNLIMITED_S = 604800


class TapeBadge:
    """Is this race happening RIGHT NOW, or are we re-broadcasting a saved replay?

    `IsReplayPlaying` cannot answer it. A broadcast/spectator client watches through
    the replay viewer, so the flag is 1 for an entirely live session: it means "is
    the replay viewer active", not "are we showing tape". The overlay badge sat on
    REPLAY through a live broadcast at Spa (2026-07-26) and was pinned to LIVE with
    a constant to stop it, which then badged a saved-replay broadcast LIVE for its
    whole duration (Nordschleife, 2026-07-30, #62).

    `ReplayFrameNumEnd > 0` does not answer it either, though it looks like it does.
    It is frames REMAINING to the end of the tape, so "0 == the live edge" reads as
    a clean discriminator, but measured on the Spa captures, a client sitting at
    the live edge reports 1, 19, or a flat 1201 depending on how far behind the
    viewer parks. Testing it against zero re-badges a live race REPLAY.

    WHAT SEPARATES THEM IS WHETHER THE TAPE IS STILL BEING WRITTEN. A saved replay
    is a finished file of fixed length, so position and remainder trade off exactly
    (Nordschleife, every sample summing to 111250):

        FrameNum=27215 FrameNumEnd=84035        FrameNum=1148  FrameNumEnd=1
        FrameNum=27299 FrameNumEnd=83951        FrameNum=9911  FrameNumEnd=1201
        FrameNum=27382 FrameNumEnd=83868        FrameNum=11899 FrameNumEnd=1201
        total fixed at 111250 -> TAPE           total 1149 -> 11112 -> 13100 -> LIVE

    A live session is being recorded as we watch, so the total climbs at the tape's
    own 60fps whatever the viewer is doing. That is the same question the badge asks
    (is the race occurring in real time) rather than a proxy for it.

    NEITHER VERDICT IS AVAILABLE IMMEDIATELY: both are claims about what the tape
    does over time, and one frame shows no motion. Growth latches, because it is
    proof and does not expire: iRacing's replay buffer is finite, so a long live
    session eventually stops gaining frames, and a capped tape must not turn the
    race into a replay retrospectively. A flat tape is only believed once it has
    stayed flat for as long as a growing one would have taken to give itself away.

    The two-second gap between them falls to LIVE, deliberately. A real race badged
    REPLAY lies about every broadcast we do; a saved replay badged LIVE for its
    first two seconds costs the opening shot of a tape. Feeds carrying no replay
    channels at all (the synthetic source, older captures, a sim-box agent built
    before they were streamed) stay on that default forever, which is the same
    judgement: an absent channel is not evidence of tape.

    This is identity of the FEED, the way the track's est-lap span is identity of the
    track: a motion reset must not touch it, because a scrub or a session rollover does
    not turn a saved replay into a live race. `reset()` is for a clock jump only: the
    settle window is measured on the session clock, and after a scrub back its start
    is in the future, so the badge fell to LIVE for as long as the scrub was. A live
    feed proves it is growing again within seconds.
    """

    def __init__(self) -> None:
        self._frames: float | None = None    # the shortest tape total seen
        self._since: float | None = None     # session time that baseline was set
        self._growing = False                # latched: the tape has been seen to grow

    def reset(self) -> None:
        self._frames, self._since, self._growing = None, None, False

    def showing_tape(self, frame: Frame) -> bool:
        if not frame.get("IsReplayPlaying"):
            return False
        pos, end = frame.get("ReplayFrameNum"), frame.get("ReplayFrameNumEnd")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                   for v in (pos, end)):
            return False

        total = float(pos) + float(end)
        # Baseline is the SHORTEST tape seen, so slow growth still accumulates against
        # it instead of being re-based away one frame at a time.
        if self._frames is None or total < self._frames:
            self._frames, self._since = total, frame.session_time
        if total - self._frames > TAPE_GROWTH_FRAMES:
            self._growing = True
        if self._growing:
            return False
        # `is None`, not `or`: a feed whose first frame lands at session time 0.0 has a
        # start time, and a falsy one would restart the window on every frame.
        started = frame.session_time if self._since is None else self._since
        return frame.session_time - started >= TAPE_SETTLE_S


def official_results(info: SessionInfo, num: int | None) -> tuple[OfficialResult, ...] | None:
    """This session's official classification, or None while it is still running.

    `ResultsPositions` is NOT a live standings table. Measured on both Spa captures:
    the currently-running session has no such key at all, while every session already
    finished carries a full 28-row table. capture2 was taken during the race and has
    tables for practice and qualifying and nothing for the race; capture was taken
    during practice and has none of the three. So its arrival is a signal in itself
    (the sim has stopped scoring this session), and that is what the winner call
    waits for rather than reading a running order that is still settling.

    Rows are sorted by Position rather than trusted in file order, and the -1.0
    no-lap sentinel dies here the same as everywhere else (see CarState.best_lap).
    """
    if num is None:
        return None
    rows = info.session(num).get("ResultsPositions")
    if not isinstance(rows, list) or not rows:
        return None
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        pos, idx = r.get("Position"), r.get("CarIdx")
        if not isinstance(pos, int) or not isinstance(idx, int):
            continue
        best = r.get("FastestTime")
        best = float(best) if isinstance(best, (int, float)) and best > 0 else None
        laps = r.get("LapsComplete")
        out.append(OfficialResult(position=pos, car_idx=idx,
                                  laps_complete=laps if isinstance(laps, int) else 0,
                                  fastest_time=best))
    out.sort(key=lambda r: r.position)
    return tuple(out) or None


def session_snapshot(info: SessionInfo, frame: Frame, *, is_replay: bool) -> SessionSnapshot:
    """Everything about the session (not the cars) that this frame can tell us.

    `is_replay` is passed in because it is the one thing here that needs history: see
    TapeBadge. Everything else is read off the frame and the session info.
    """
    flags = frame.get("SessionFlags") or 0
    state = frame.get("SessionState")
    tr = frame.get("SessionTimeRemain")
    tt = frame.get("SessionTimeTotal")
    lr = frame.get("SessionLapsRemain")
    # 32767 is iRacing's int16 "unlimited" sentinel (timed races): not a real count.
    laps_remaining = lr if (isinstance(lr, (int, float)) and 0 <= lr < _UNLIMITED_LAPS) else None
    # iRacing's max "unlimited" sentinel is a ~604800s (a week); treat as no cap.
    time_total = tt if (isinstance(tt, (int, float)) and 0 <= tt < _UNLIMITED_S) else None
    time_remaining = tr if (isinstance(tr, (int, float)) and 0 <= tr < _UNLIMITED_S) else None
    # ...and it re-bases onto the COOL-DOWN clock the moment the session state
    # leaves RACING. Measured on the practice capture (a 180s session): remaining
    # winds 118 -> 18 under RACING, then reads 603 under CHECKERED and 5.8 under
    # COOL_DOWN. Past the flag it is not this session's clock at all, so it dies
    # here rather than downstream: the checkered was called and then
    # announced nine minutes remaining, and the overlay's elapsed (total minus
    # remaining) goes NEGATIVE. See DESIGN.md 14.
    if state in (SessionState.CHECKERED, SessionState.COOL_DOWN):
        time_remaining = None

    is_yellow = bool(flags & YELLOW_MASK)
    is_red = bool(flags & SessionFlag.RED)
    is_checkered = bool(flags & SessionFlag.CHECKERED) or state == SessionState.CHECKERED
    # Green = actually racing with no caution/red/checkered, rather than the
    # (rarely-set) green bit. See YELLOW_MASK note above.
    is_green = state == SessionState.RACING and not (is_yellow or is_red or is_checkered)
    # Has the field been let go, or is it still being led round? (#63)
    #
    # Two ways to still be forming up. The state machine covers the ordinary ones
    # (GET_IN_CAR, WARMUP and PARADE_LAPS are all "not yet"), and the pre-start flag
    # bits cover the case the state machine does not: a start held with the field
    # already in SessionState.RACING. Measured on capture2, ONE_LAP_TO_GREEN is set
    # through the whole pre-start and for one frame after RACING begins, so that
    # overlap is real rather than theoretical.
    #
    # ABSENCE OF A PARADE MEANS THE RACE IS ON, not "keep waiting". A standing start
    # never reports PARADE_LAPS at all (capture2 goes GET_IN_CAR -> WARMUP -> RACING),
    # and a feed with no SessionState channel reports None: both have to end up
    # released or the broadcast sits on one card for an entire race.
    #
    # NOT the pace car's presence, which is the signal this looks like it should use.
    # CarIsPaceCar resolves an idx from the roster, but that idx is in the world all
    # session: on capture2 it reads APPROACHING_PITS for all 4279 frames, green-flag
    # racing included. It says where the pace car is parked, not whether it is out.
    held = bool(flags & (SessionFlag.ONE_LAP_TO_GREEN | SessionFlag.GREEN_HELD))
    field_released = state not in (
        SessionState.GET_IN_CAR, SessionState.WARMUP, SessionState.PARADE_LAPS,
    ) and not held

    # Which session of the weekend this is. The frame's SessionNum is the per-tick
    # truth and indexes SessionInfo.Sessions; CurrentSessionNum (used when the
    # channel is absent) rides the YAML, which live refreshes only every 60s.
    num = frame.get("SessionNum")
    num = int(num) if isinstance(num, (int, float)) and not isinstance(num, bool) else None
    session_type = info.session_type(num)
    kind = session_kind(session_type)
    if not kind:
        # No Sessions block at all: the synthetic source builds WeekendInfo only,
        # and it generates races. EventType is wrong for a real weekend's practice
        # (it says "Race" there too) but it is all a feed like that offers.
        kind = session_kind(info.weekend.get("EventType"))

    # A LAP-limited session has no clock, and iRacing fills the field with a
    # placeholder rather than omitting it: both Spa captures show Sessions[1],
    # "Lone Qualify", as SessionLaps 2 with SessionTime "86400.0000 sec": one
    # day, which sails through the week-long sentinel guard above. Left alone the
    # clock reads one thousand four hundred and forty minutes of qualifying.
    # The discriminator is SessionLaps being a NUMBER: practice and race both carry
    # the string "unlimited", so a genuine 24-hour race (which reports the same
    # 86400 seconds) keeps its clock. NOTE: read from the session YAML, which we
    # hold for both captures; the live channels during a qualifying session are
    # unverified, since we have no qualifying capture. (A "N laps or T minutes"
    # race would carry both and lose its clock here; no capture of one exists yet.)
    session_laps = info.session(num).get("SessionLaps") if num is not None else None
    if isinstance(session_laps, (int, float)) and not isinstance(session_laps, bool):
        time_total = time_remaining = None

    return SessionSnapshot(
        session_time=frame.session_time,
        flags=flags,
        state=state,
        is_green=is_green,
        field_released=field_released,
        is_yellow=is_yellow,
        is_red=is_red,
        is_checkered=is_checkered,
        time_remaining=time_remaining,
        laps_remaining=laps_remaining,
        is_last_lap=(laps_remaining == 0) or bool(flags & SessionFlag.WHITE),
        event_type=info.weekend.get("EventType"),
        time_total=time_total,
        is_replay=is_replay,
        session_num=num,
        session_type=session_type,
        session_kind=kind,
        # Sessions are numbered in weekend order, so the next one is simply num + 1;
        # a missing entry (the last session) reads back as UNKNOWN.
        next_session_kind=(session_kind(info.session_type(num + 1))
                           if num is not None else SessionKind.UNKNOWN),
        official=official_results(info, num),
    )
