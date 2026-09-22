"""World-model data types: the per-tick picture the director will read.

These are immutable snapshots derived from raw frames by WorldModel (builder.py).
This is the interpreted layer: gaps in seconds, closing rates, running order, and
transient events, as opposed to the raw channel dump in a Frame.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..telemetry.constants import TrackSurface
from .sectors import LapPace


class EventKind:
    OVERTAKE = "overtake"
    OFF_TRACK = "off_track"
    INCIDENT = "incident"
    # A car DROVE into the pit lane: it was on the road last frame and is on pit road
    # now, with the lap distance continuous across the two. This is a pit stop, or the
    # start of one, and it is the only kind of arrival that is.
    PIT_ENTRY = "pit_entry"
    # A car is in the pits without having driven there: back from a tow, an Escape to
    # the pit box, a reset after the flag. The sim reports all of these exactly as it
    # reports a stop (`CarIdxOnPitRoad` goes true), and Round 1 (2026-09-20) had the
    # "a pit stop" reported for finishers resetting, and the pit line on cars that
    # had been put there on a flatbed. Told apart in the builder (see _events): the car
    # arrived from NOT_IN_WORLD, or landed IN_PIT_STALL on the frame it appeared in the
    # lane (a drive-in enters via the approach road), or its lap distance stepped by
    # more than any car can drive in one tick. Never a shot, never a "stop".
    PIT_RESET = "pit_reset"
    PIT_EXIT = "pit_exit"


class SessionKind:
    """What kind of session is on track, normalised from iRacing's SessionType.

    Decided once, here, because the raw strings are a small zoo: qualifying alone is
    "Lone Qualify" (one car on track at a time) or "Open Qualify", and testing is
    "Offline Testing". Everyone downstream switches on these constants instead of
    matching substrings of their own.

    This matters well beyond a label on the overlay: practice reports NO running order
    (`CarIdxPosition` is 0 for the whole field, DESIGN.md 14), qualifying has cars
    circulating alone rather than racing each other, and neither has a leader, an
    interval that means anything, or an overtake worth calling.

    UNKNOWN is a real answer, not an error: a feed with no Sessions block (the
    synthetic source) and a future iRacing session type both land here, and the rule
    for a consumer is to fall back to its race behaviour rather than go silent.
    """

    PRACTICE = "practice"
    QUALIFY = "qualify"
    WARMUP = "warmup"
    RACE = "race"
    TESTING = "testing"
    UNKNOWN = ""


def session_kind(session_type: str | None) -> str:
    """Classify one iRacing SessionType string into a SessionKind.

    Matched on substrings, in an order that is load-bearing: "Offline Testing" is
    tested before "race" would ever be reached, and the qualifying check has to be
    "qualif" so it catches Lone, Open and plain "Qualifying" alike.
    """
    t = (session_type or "").strip().lower()
    if not t:
        return SessionKind.UNKNOWN
    if "qualif" in t:
        return SessionKind.QUALIFY
    if "practice" in t:
        return SessionKind.PRACTICE
    if "warm" in t:
        return SessionKind.WARMUP
    if "test" in t:
        return SessionKind.TESTING
    if "race" in t:
        return SessionKind.RACE
    return SessionKind.UNKNOWN


def in_pit_lane(on_pit_road: bool, surface: int | None) -> bool:
    """Is this car in the pits: lane, stall or approach?

    ONE definition, in the raw-signal form, because three different layers ask the
    question and they were not all asking it. The flag LAGS the surface on entry and
    exit, so either alone is wrong for a second or so at exactly the moment that
    matters: a car peeling off into the lane is losing a place per second right there.

    What it is for: a car in the pits is not racing anybody for position. The place it
    hands over is a stop, not a pass, so it must not fire an overtake call, must not
    earn the camera's pass bonus, and above all must not roll an instant replay in slow
    motion of a lead change where nobody overtook anybody.
    """
    return bool(on_pit_road) or surface in (TrackSurface.IN_PIT_STALL,
                                            TrackSurface.APPROACHING_PITS)


def car_in_pits(c) -> bool:
    """in_pit_lane for anything CarState-shaped."""
    return in_pit_lane(c.on_pit_road, c.surface)


def is_race_kind(kind: str) -> bool:
    """May this session be narrated as a race: a running order, a leader, a winner?

    ONE definition, shared by the director (whether a moment is worth a cut) and the
    phrasing (how to word it), because the two disagreeing is worse than either
    being wrong: a suppressed win call with race wording left in the pit lines is
    a broadcast that sounds broken rather than one that sounds thin.

    UNKNOWN counts as a race, deliberately. A feed with no Sessions block (the
    synthetic source) lands there, it generates races, and going quiet on an
    unrecognised session type is a worse failure than the wording being off.
    """
    return kind in (SessionKind.RACE, SessionKind.UNKNOWN)


class IncidentSeverity:
    """How bad an incident is: decided ONCE here, read by everyone downstream.

    The director (which shots may preempt the broadcast) and anything downstream of
    it needs this distinction, so it is data on the event rather than a heuristic each
    of them reinvents.

    MINOR     a brush with the grass or the exit road. A quick lap uses the kerbs
              and the runoff: normal fast driving, not broadcast material.
    MODERATE  a single-car moment that actually cost something: still off the
              road a beat later AND well down on the pace it carried in (a spin,
              a trip through the gravel).
    MAJOR     more than one car off in the same moment, in the same place: contact.

    Not represented here: a car that is simply GONE (stopped / beached / out of
    it). The director infers that from telemetry over time (`_detect_trouble`),
    which is a stateful per-car judgement rather than a property of one event.
    """

    MINOR = "minor"
    MODERATE = "moderate"
    MAJOR = "major"


_SEVERITY_RANK = {
    IncidentSeverity.MINOR: 1,
    IncidentSeverity.MODERATE: 2,
    IncidentSeverity.MAJOR: 3,
}


def severity_rank(severity: str) -> int:
    """Rank of a severity string. An unclassified event ranks as MINOR on purpose:
    a producer that forgets to tier its events can never preempt the broadcast."""
    return _SEVERITY_RANK.get(severity, _SEVERITY_RANK[IncidentSeverity.MINOR])


def severity_at_least(severity: str, floor: str) -> bool:
    return severity_rank(severity) >= severity_rank(floor)


@dataclass(frozen=True)
class Event:
    kind: str
    car_idx: int
    other_idx: int | None = None
    position: int | None = None
    detail: str = ""
    # IncidentSeverity, on INCIDENT events. Empty on every other kind (and treated
    # as the bottom tier if it ever isn't).
    severity: str = ""
    # Session time the thing itself HAPPENED, when that is earlier than the frame
    # reporting it. Most events are instantaneous and leave this None; an incident is
    # not: it is classified once there is enough evidence, and the evidence arrives
    # after the fact: contact is only recognisable when the SECOND car leaves the road,
    # which can be INCIDENT_PAIR_WINDOW after the first. Anything that goes back to the
    # tape (the replay machine) must seek to this, not to the frame, or it lands on the
    # aftermath: measured on air 2026-09-14, slow motion of a car already stopped.
    at: float | None = None


@dataclass(frozen=True)
class CarState:
    idx: int
    number: str
    name: str | None
    class_id: int | None
    # LIVE place, counted off WorldSnapshot.order (the timing sheet with completed
    # on-track passes applied: see builder.live_order), NOT raw `CarIdxPosition`.
    # The sheet only turns over at the line, so a car can finish a pass into turn one
    # and stay listed second for the rest of the lap; every consumer here wants the
    # place the viewer can see. 0 keeps its meaning: THIS SESSION HAS NO RUNNING ORDER
    # (practice scores nobody), and it is never filled in with a rank.
    position: int
    class_position: int
    lap: int
    lap_completed: int
    lap_dist_pct: float
    progress: float  # laps completed + fraction of current lap (monotonic)
    speed: float  # m/s, derived and smoothed
    on_pit_road: bool
    surface: int  # TrackSurface value
    on_track: bool
    gap_ahead: float | None  # seconds to the car directly ahead in the order (F2Time interval)
    gap_behind: float | None
    to_leader: float | None  # seconds behind the leader (from CarIdxF2Time)
    # Continuous on-track gap to the car ahead, in seconds, derived from lap-distance
    # (not CarIdxF2Time). Only populated for physically-near, same-lap pairs; None
    # otherwise. This is the live, every-frame signal battles are scored on: F2Time
    # is a per-lap staircase (flat between line crossings, 0 until a car completes a
    # lap), so its derivative is useless for "is this car actually catching?".
    track_gap_ahead: float | None
    closing_rate: float | None  # seconds/second; positive means the on-track gap is shrinking
    car_ahead_idx: int | None
    # CarIdxEstTime: seconds from the start/finish line to this car's track position,
    # off the TRACK'S OWN speed profile. This is the ruler every reported gap is measured
    # with (see est_track_gap), and it is the channel that fixed #55.
    #
    # It is a position-to-time map, NOT a pace: measured on recordings/capture2, all 28
    # cars agree on the value at a shared track position to within 0.03-0.06s, and the
    # map is deliberately nonlinear (it already knows La Source is slow and Kemmel is
    # fast). That is exactly what dividing a distance by the trailing car's INSTANTANEOUS
    # speed was failing to approximate, and why that method made a fixed separation
    # breathe by 4.6x once a lap.
    #
    # Sentinel-free, like every other channel here: slots for cars not in the world read
    # <= 0, and that dies in the builder. None means "no data", never a zero gap.
    est_time: float | None = None
    # Last completed lap, in seconds, or None when this car has not set one yet.
    # iRacing spells "no lap yet" as the -1.0 sentinel, and that sentinel dies HERE,
    # in the builder, rather than in each consumer: the same rule SessionLapsRemain's
    # 32767 gets. Measured on recordings/capture2: -1.0 is the ONLY negative the
    # channel ever produces (245,832 sentinel car-frames against 62,256 real ones),
    # so a None here means "no lap", never "a lap we failed to parse". See DESIGN.md 14.
    last_lap: float | None = None
    # This car's best lap of the SESSION so far, same sentinel and same treatment.
    # Absent for far longer than you would guess: on the practice capture both lap
    # channels are -1.0 in all 3020 frames for all 64 slots, because that is the
    # opening ~200s of practice and nobody has completed a lap yet. "No time yet" is
    # the NORMAL state early in a session, not an edge case (DESIGN.md 14).
    best_lap: float | None = None
    # Flags shown to THIS car (CarIdxSessionFlags), already masked to the ones that mean
    # something: a black flag, a disqualification, a meatball, a furled warning. 0 is the
    # normal state and the overwhelmingly common one.
    #
    # The masking happens in the builder, like every other sentinel in this file, and it
    # is not cosmetic: the raw channel sets `servicible` on essentially every car for
    # essentially the whole session (119,994 car-frames on capture2), so a consumer
    # reading the raw value as a boolean marks the entire field for the entire race. See
    # telemetry.constants.DRIVER_FLAG_MASK.
    driver_flags: int = 0
    # What the car IS, so a caption can name it by its make ("the #5 BMW of Florian
    # Chevrue"). iRacing's raw strings, carried unresolved on purpose: `CarScreenNameShort`
    # is a display name ("BMW M4 GT4", "Cadillac V-Series.R") and `CarPath` is the stable
    # internal id ("bmwm4gt4", "porsche963gtp"). Turning either into something a voice can
    # say is a PRONUNCIATION question, and that is settled wherever something speaks,
    # not here. Both are None on a feed with no DriverInfo at all, which is the
    # synthetic source and the bridge echo.
    car_model: str | None = None
    car_path: str | None = None
    # `DriverInfo.FlairName`, iRacing's per-driver country, with its non-country values
    # already normalised away by SessionInfo.drivers(). Carried raw for the same reason the
    # model is: the overlay wants a flag out of it (flair.flag) and a caption or a
    # voice would want the adjective, which is a different question. Carried raw so
    # neither answer is decided here.
    country: str | None = None
    # Is this car sitting in the GARAGE, classified on a time it has already set but not
    # running? True only in a non-race session, where going in between runs is the normal
    # rhythm rather than the end of somebody's day (see builder._garaged, issue #46).
    #
    # Such a car is deliberately NOT in WorldSnapshot.order: it holds no place, it is in
    # no gap, and it cannot be in a battle or a pass. Everything derived from motion is
    # neutralised on it (speed 0, no gaps, no closing rate, position 0) so nothing
    # downstream can mistake a parked car for a moving one. What it keeps is its identity
    # and its lap times, because a time that has been set does not un-set.
    in_garage: bool = False
    # Out of the world in a RACE: being towed, or retired, and at the moment it happens
    # nobody can tell which; only time does. Unlike a garaged car it KEEPS its place in
    # the running order, at the track position it left the world from, so the field
    # passes it and it falls back exactly as a car stopped on the road would, and as the
    # timing sheet will show once the sheet catches up at the line. Motion is neutralised
    # the same way as the garage (speed 0, no gaps, no est reading): nothing downstream
    # films it, races it, or calls a pass on it. Chosen 2026-09-02 over the old behaviour,
    # which dropped the car from the board for the ~30 s of a tow and brought it back.
    towed: bool = False
    # This car's current lap, sector by sector, against its own best and the session's
    # (world.sectors.LapPace), or None: no sector boundaries on this feed, no clean lap
    # in progress (an out lap, a lap through the pits), or no boundary crossed yet this
    # lap. Carried by the racing car only; a garaged or towed car has no lap in hand.
    pace: LapPace | None = None


# Laps of clear air before a pass is believed to have happened ON TRACK. Roughly a
# car length or two at a typical circuit: enough to reject the nose-ahead,
# nose-behind wobble of a pair running side by side through a corner.
PASS_CLEAR_LAPS = 0.0015
_SAME_LAP_LAPS = 0.5


# Laps by which the car listed BEHIND may be ahead on track before the pair is not a
# pair at all. Inside this margin they are side by side mid-pass and the gap is 0; beyond
# it the order and the road disagree too much to report a number (a lapping, or a swap
# artifact). Same value and same reasoning as builder.TRACK_GAP_CROSS_LAPS.
EST_GAP_CROSS_LAPS = 0.015

# How far below zero an est difference may sit and still be a DEAD HEAT rather than a
# start/finish wrap (see est_track_gap). Sized to sit in the empty space between the two:
# the largest cross-car disagreement measured at a shared track position is 0.43s, and the
# smallest genuine wrap is half a lap (63s at Spa, and this is the SHORT side: a wrap on
# a nose-to-tail pair reads about -125s). Ten times the noise, sixty times under the
# signal, so nothing lands near it.
EST_GAP_DEAD_HEAT = 4.0


def est_gap(ahead_est: float | None, behind_est: float | None, dprog: float,
            est_lap: float | None) -> float | None:
    """`est_track_gap` on raw readings: the two est times and `dprog`, the car listed
    ahead's progress minus the car behind's, in laps. The world builder measures its
    per-frame on-track gap with this before any CarState exists; everything downstream
    of a CarState uses est_track_gap. One rule, two entry points."""
    if ahead_est is None or behind_est is None or not est_lap:
        return None
    if dprog < 0.0:
        # The car listed ahead is BEHIND on track: a pass in progress. Report side by
        # side so a battle holds through the pass, but only within a car length or two.
        return 0.0 if dprog > -EST_GAP_CROSS_LAPS else None
    gap = ahead_est - behind_est
    if gap < 0.0:
        return 0.0 if gap > -EST_GAP_DEAD_HEAT else gap + est_lap
    return gap


def est_track_gap(ahead: CarState, behind: CarState, est_lap: float | None) -> float | None:
    """On-track time separation of two cars, in seconds, off the track's own ruler.

    THE one gap definition (#55). Every gap the broadcast shows (the tower's interval
    column, the battle chip) comes through here, because the bug it fixes was not a
    tuning problem: it was ONE column fed by THREE quantities that do not measure the
    same thing (the builder's speed-derived on-track gap, a distance-over-instantaneous-
    speed estimate, and CarIdxF2Time, which is time behind the LEADER). The reported
    number switched between them per frame as conditions flipped, and every switch was a
    step discontinuity. That is the mechanism behind a 44s jump between adjacent frames.

    `est_time` is a shared, car-independent map from track position to seconds, so the
    separation is just the difference of two readings: no instantaneous speed, no
    leader-relative quantity, no per-lap staircase.

    Returns the WITHIN-LAP separation only. Whole laps stay a separate concept
    (`intervalLaps` / "+1 L"), exactly as they are today: do not try to encode laps into
    this number.

    The start/finish wrap is resolved by the MAGNITUDE of a negative difference, because
    the two things that produce one are separated by three orders of magnitude:

      * A genuine wrap is colossal. The car ahead has crossed and reset while the car
        behind has not, so the difference is -(est_lap - gap): about -125s at Spa for a
        1.4s gap, and never smaller in magnitude than est_lap/2 for a pair close enough
        to be called a pair at all.
      * A dead heat is a hair. The est map is shared but not IDENTICAL across cars
        (measured on capture2: they agree at a shared track position only to within
        0.03-0.06s), so two cars genuinely alongside routinely read about -0.002s with no
        lap boundary anywhere near.

    Both of the tidier-looking rules are wrong, and each is wrong where the other is
    right. A bare `% est_lap` turns the dead heat into a full lap (+126.6s at Spa; it
    fires on 71 same-lap pairs of the Spa capture). Deciding the wrap from
    `lap_dist_pct` instead looks airtight (est is a function of position, so they cannot
    disagree), but they do, for one frame, at the only place it matters: iRacing reports
    `lap_dist_pct` saturating at exactly 1.00000 while `est_time` has ALREADY reset to
    ~0.0001. A position-based rule reads that as "the car ahead is round the far side of
    the line" and adds a phantom lap. Measured, it costs 6 spurious 126.6s jumps that the
    modulus does not have.
    """
    return est_gap(ahead.est_time, behind.est_time, ahead.progress - behind.progress, est_lap)


def passed_on_track(ahead_progress: float, behind_progress: float) -> bool:
    """Has the car listed BEHIND physically cleared the one listed ahead?

    The one predicate behind both `track_order` (which pair goes on which side of a
    graphic) and the live running order (what number each car wears). They were two
    margins once and drifted apart, which is how the tower and the pop-ins ended up
    disagreeing about the same pass.

    Clearing needs PASS_CLEAR_LAPS of daylight, so a pair running side by side does
    not flicker the order every frame; and it needs to be under half a lap, so a
    lapping or a car that has pitted is never read as a pass.
    """
    clear = behind_progress - ahead_progress
    return PASS_CLEAR_LAPS < clear < _SAME_LAP_LAPS


def track_order(ahead: CarState, behind: CarState) -> tuple[CarState, CarState]:
    """One pair as the CAMERA sees it, given the pair in timing-sheet order.

    `CarIdxPosition` is the timing sheet: it flips as the pair crosses the line,
    which on a long lap is most of a minute after the viewer watched the move
    (measured on `capture2`: 85 seconds at Spa). So everything that describes what is
    ON SCREEN (which car to point the camera at, which name bug is which) reads
    TRACK order. The tower does too, via `live_order`, because a broadcast that puts
    one number on the graphic and another on the sheet is telling the viewer the
    overlay is broken. This returns (front, back) by track position: still
    (ahead, behind) until the car behind has physically cleared the one in front.

    A pair more than half a lap apart in progress is never reordered: that is a
    lapping or a car that pitted, and their progress difference says nothing about
    who the camera sees in front.
    """
    return ((behind, ahead) if passed_on_track(ahead.progress, behind.progress)
            else (ahead, behind))


@dataclass(frozen=True)
class Weather:
    """Conditions, as far as they are worth trusting. See DESIGN.md section 14 for
    what each channel actually does over a session: the short version:

    - `track_temp_c` is the ONE good dynamic signal: slow, quantised steps.
    - `air_temp_c` is a scene-setting absolute only. It changes every frame and its
      entire range over six minutes of real capture was five hundredths of a degree,
      so a trend on it is a trend on float noise.
    - `skies` and `wetness` are iRacing int enums, static per session in all the data
      we have.
    - `precipitation` is UNVALIDATED: it is flat zero in every capture, because every
      capture is dry. Nothing here proves how it behaves in the wet.
    - Density, pressure and humidity are deliberately absent. They are every-frame
      noise with nothing to say, and humidity is a 0..1 fraction that disagrees with
      the session-info string that reports it as a percentage.

    Every field is optional and the whole object is None when the feed carries no
    weather at all, which is the normal case for the synthetic source and for the
    live bridge until the sim-box agent is rebuilt.
    """

    track_temp_c: float | None = None
    air_temp_c: float | None = None
    skies: int | None = None          # 0 clear, 1 partly cloudy, 2 mostly cloudy, 3 overcast
    wetness: int | None = None        # 1 dry, rising with how wet the surface is
    precipitation: float | None = None  # 0..1 fraction
    wind_ms: float | None = None

    @property
    def known(self) -> bool:
        return any(v is not None for v in
                   (self.track_temp_c, self.air_temp_c, self.skies, self.wetness,
                    self.precipitation, self.wind_ms))


@dataclass(frozen=True)
class OfficialResult:
    """One row of a session's OFFICIAL classification, as the sim itself scored it.

    From `SessionInfo.Sessions[n].ResultsPositions`, which is not a live standings table:
    measured on both Spa captures, the table is absent for the session that is CURRENTLY
    RUNNING and present with a full field for every session already finished. Its arrival
    is therefore the signal that a result has stopped moving, which is the one thing the
    live running order cannot tell you (see TalkPolicy._winner_call).

    A recording carries SessionInfo once, in its header, so a REPLAY never sees this
    appear: only a live feed does, and only when the world model is handed fresh info
    (run_bridge refreshes on a timer). Anything reading this must have an answer for
    never getting it.
    """
    position: int
    car_idx: int
    laps_complete: int
    fastest_time: float | None   # the session's per-car best; None for the no-lap sentinel


@dataclass(frozen=True)
class SessionSnapshot:
    session_time: float
    flags: int
    state: int | None
    is_green: bool
    is_yellow: bool
    is_red: bool
    is_checkered: bool
    time_remaining: float | None
    laps_remaining: int | None
    is_last_lap: bool
    event_type: str | None            # WeekendInfo.EventType: the EVENT, "Race" all weekend
    # Has the field been released to race, or is it still being led round? False through
    # get-in-car, warm-up, the parade laps, and a start being held. See world.session.session_snapshot
    # for why the pace car's own position cannot answer this (#63). Defaults TRUE, and a
    # feed with no SessionState reports True: absence of a parade is a race, not a wait.
    field_released: bool = True
    time_total: float | None = None   # SessionTimeTotal: full event length (timed races)
    # Are we re-broadcasting a saved replay, rather than a race happening right now?
    # NOT bare IsReplayPlaying, which is 1 for a spectator watching live through the
    # replay viewer: the discriminator is whether the tape is still being written. See
    # world.session.TapeBadge for the measurement (#62). Absent channels read False.
    is_replay: bool = False
    # Which session of the weekend is actually running. `event_type` above cannot answer
    # this (it reads "Race" during practice too), so these come from the frame's
    # SessionNum indexed into SessionInfo.Sessions. See SessionKind.
    session_num: int | None = None
    session_type: str | None = None            # raw iRacing string ("Lone Qualify")
    session_kind: str = SessionKind.UNKNOWN    # normalised (SessionKind.QUALIFY)
    # What comes NEXT in the weekend, or UNKNOWN in the last session (and in any feed
    # with no Sessions block). Where a session sits in the weekend is most of what
    # makes it make sense: an official iRacing practice is 180 seconds, which is a
    # shakedown before qualifying rather than a practice programme, and the difference
    # is not visible from anything inside the session itself.
    next_session_kind: str = SessionKind.UNKNOWN
    # The sim's own classification of THIS session, P1 first, or None while it is still
    # running. See OfficialResult: the table only appears once a session is over, so
    # `official is not None` means "the result has stopped moving" and nothing else.
    official: tuple[OfficialResult, ...] | None = None


class TimeJump:
    """Why the session clock stopped being continuous with the previous frame.

    Session time is every stateful component's clock: the director's shot holds and
    cooldowns, TalkPolicy's per-topic cooldowns, SessionMemory's series, and the world
    model's own dt-derived speeds and closing rates. When it jumps, every one of those
    is holding a value from a different timeline, and the damage is not symmetric:
    a jump BACKWARDS puts them all in the future at once, which reads as "wait forever"
    rather than "expire now" (issue #41: the director went silent for 20 minutes).

    Detected once, here, rather than in each consumer: they share one clock, so they
    should share one answer about when it broke.
    """

    BACK = "back"        # scrubbed back / session reloaded / a replay seek
    FORWARD = "forward"  # scrubbed ahead, or a stall long enough to be a discontinuity
    SESSION = "session"  # SessionNum rolled over: practice -> qualifying -> race


@dataclass(frozen=True)
class CameraView:
    """Where the sim's camera actually is: not where the director asked it to go.

    Every consumer downstream of the director used to render its INTENT: the tower's
    on-camera car, the shot's subject, the bridge's shot echo. Intent is not the
    picture, and at Oulton Park on 2026-08-02 the two diverged for a long stretch with
    no signal anywhere (#68). This is the readback that makes the difference visible.

    `car_idx` is `CamCarIdx`. **None means we do not know**, and that is a different
    answer from any car index: the channel is absent from the synthetic source, and
    absent from the two-box live path until the sim-box agent is rebuilt. Folding
    absent into a real value is exactly the bug that badged live races REPLAY (#62), so
    the whole object is None when the feed carries no camera channels and `car_idx` is
    None for the sim's own negative sentinel.

    The GROUP is carried but is deliberately NOT authoritative for anything: the angle
    rotator changes group on purpose every few seconds, so a group that differs from
    the last command is the system working. Only the target CAR is reconciled.
    """

    car_idx: int | None = None
    group: int | None = None

    @property
    def known(self) -> bool:
        return self.car_idx is not None or self.group is not None


@dataclass(frozen=True)
class WorldSnapshot:
    tick: int
    session_time: float
    session: SessionSnapshot
    cars: dict[int, CarState]
    order: list[int]  # car indices, P1 first
    events: list[Event]
    weather: Weather | None = None   # None when the feed carries no weather channels
    # Where the camera actually ended up, or None when the feed does not say (#68). Set
    # beside `weather` and read the same way: a consumer that wants to know whether the
    # picture matches the shot has to handle not knowing, because most feeds do not.
    camera: CameraView | None = None
    # The full-lap span of CarIdxEstTime for this track, in seconds: the modulus every
    # est-based gap is reduced by, because EstTime runs 0..est_lap and resets at the line.
    # Learned from the feed rather than configured (it is the running maximum of the
    # channel), so a track we have never seen needs no asset. None until a car has been
    # seen near the line, which is why est_track_gap answers None then and the caller
    # keeps its old fallback. Measured at Spa: 126.627s, against a real lap of 132.4s:
    # this is the track's reference ruler, NOT anybody's lap time.
    est_lap: float | None = None
    # A TimeJump.* when this frame is not continuous with the last one, else None.
    # Consumers holding session-time state must re-seat it; this frame's derived
    # motion (speed, closing rate) is not meaningful and its events are suppressed.
    time_jump: str | None = None

    def car(self, idx: int) -> CarState | None:
        return self.cars.get(idx)

    def leader(self) -> CarState | None:
        return self.cars.get(self.order[0]) if self.order else None

    def running_order(self) -> list[CarState]:
        return [self.cars[i] for i in self.order]
