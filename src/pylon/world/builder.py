"""WorldModel: stateful builder that turns a stream of Frames into WorldSnapshots.

Stateful because gaps-as-time, per-car speed, closing rates, and events all need
memory of the previous frame(s). Feed frames in order via `update()`, or use
`run(source)` to iterate a whole source.

Gaps are computed from lap-distance deltas (progress in laps -> metres) divided by
the trailing car's derived speed, rather than trusting a single SDK timing
channel (see DESIGN.md section 4). Per-car speed is derived from progress deltas
because iRacing exposes a live speed only for the player car.

This module is the running order, the clock's continuity, and the motion maths, plus
the orchestration that ties a frame together. The other jobs have their own modules:
channels.py reads a frame's per-car arrays once with guarded indexing, session.py
interprets the session (kind, clock, flags, the tape badge, official results), and
incidents.py is the off-track episode machine.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import replace

from ..telemetry.constants import TrackSurface
from ..telemetry.frame import Frame, SessionInfo
from ..telemetry.source import TelemetrySource
from .channels import Channels
from .incidents import IncidentTracker
from .model import (
    _SAME_LAP_LAPS,
    PASS_CLEAR_LAPS,
    CameraView,
    CarState,
    Event,
    EventKind,
    TimeJump,
    Weather,
    WorldSnapshot,
    est_gap,
    in_pit_lane,
    is_race_kind,
)
from .sectors import SectorTracker, boundaries_from_info
from .session import TapeBadge, session_snapshot

# The live order's two thresholds, deliberately asymmetric: a car TAKES a place with a
# couple of metres of daylight, and only gives it back when the other car is a clear
# car-length-plus in front. Stickiness is what stops the flicker, so the taking margin
# does not have to be wide, and it must not be, because "has cleared" is not a
# transitive relation: with one wide margin both ways, a car could clear the car two
# rows up while still inside the margin of the one between them, and the adjacent-swap
# loop would pin it there. Measured on capture2: car #128 was displayed P16 while
# running 11.2 m clear of the P14 car, trapped behind a car it led by 8.8 m.
LIVE_PASS_TAKE = PASS_CLEAR_LAPS / 5     # ~2 m at Spa: enough to be a nose, not noise
LIVE_PASS_YIELD = PASS_CLEAR_LAPS        # ~10.4 m the other way before a place goes back


def _num(frame: Frame, channel: str) -> float | None:
    """One numeric weather channel, or None if the feed does not carry it.

    Absence is the normal case, not an error: the synthetic source has no weather at
    all, and the live bridge streams a fixed channel list that does not include it
    until the sim-box agent is rebuilt (DESIGN.md section 14). Captures, which
    enumerate every channel, do have it.
    """
    v = frame.get(channel)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _weather(frame: Frame) -> Weather | None:
    w = Weather(
        track_temp_c=_num(frame, "TrackTemp"),
        air_temp_c=_num(frame, "AirTemp"),
        skies=(int(s) if (s := _num(frame, "Skies")) is not None else None),
        wetness=(int(x) if (x := _num(frame, "TrackWetness")) is not None else None),
        precipitation=_num(frame, "Precipitation"),
        wind_ms=_num(frame, "WindVel"),
    )
    return w if w.known else None


def _camera(frame: Frame) -> CameraView | None:
    """Where the sim says its camera is, or None when the feed does not say (#68).

    Read exactly like `_weather` above and for the same reason: the channels are
    optional. They are absent from the synthetic source entirely, and absent from a
    live two-box feed until the sim-box agent is rebuilt with them in
    `telemetry.live.DEFAULT_CHANNELS`.

    `CamCarIdx` is negative when the camera is not on a car at all, and that reading
    is folded into None here rather than passed on: a consumer asking "is the picture
    on the car we asked for?" wants "no idea" for both cases, and neither of them is
    car index minus one. Absent must never read as car ZERO, which is a real and very
    ordinary car: that folding is the bug that badged live races REPLAY (#62).
    """
    idx = _num(frame, "CamCarIdx")
    group = _num(frame, "CamGroupNumber")
    view = CameraView(
        car_idx=(int(idx) if idx is not None and idx >= 0 else None),
        group=(int(group) if group is not None and group >= 0 else None),
    )
    return view if view.known else None


MIN_GAP_SPEED = 5.0  # m/s floor so a stopped car doesn't yield an infinite time gap

# On-track gap (lap-distance based) is only trusted for pairs that are physically
# nose-to-tail on the SAME lap. Beyond ~half a lap of progress difference the
# fold-to-modulo-1 math conflates a lapped car with a side-by-side one (the "+103s
# gap" artifact that killed the pure on-track method for the race interval). So we
# only surface it as a battle proximity signal when it's small.
TRACK_GAP_MAX_LAPS = 0.5   # progress difference beyond this is not a nose-to-tail pair
TRACK_GAP_MAX_S = 4.0      # on-track seconds beyond this: not battle-range, report None
EST_LAP_MAX = 1200.0       # sanity cap on the learned CarIdxEstTime span (see _learn_est_lap).
                           # 20 minutes: far above any circuit iRacing ships (the Nordschleife
                           # reference lap is ~8), and low enough to reject a garbage frame.
TRACK_GAP_CROSS_LAPS = 0.015  # tolerance for the trailing-by-position car being AHEAD on
                              # track mid-overtake: report ~0 (side by side) so the battle
                              # doesn't blink out during the pass instead of going None
CLOSING_TAU = 0.6          # seconds; EMA time-constant for the closing-rate smoother
CLOSING_CLAMP = 3.0        # s/s; guard against single-frame lap-wrap discontinuities

# Live running order. `CarIdxPosition` only turns over at the timing line, so between
# a pass and the next line crossing the sheet still lists the loser ahead: 85 seconds
# of it on `capture2` at Spa, and 60% of all the time a battle graphic was on screen.
# The order is therefore the sheet with completed on-track passes applied (see
# `live_order`). Adjacent swaps rather than sorted(): the "has cleared" test is
# deliberately not a total order (it refuses to compare cars half a lap apart, and its
# margin depends on what was already applied), so handing it to a sort would scramble
# the field. Each pass only moves a car past cars it has actually cleared, and the loop
# runs to a fixed point: at most one pass per car, the bubble-sort bound.
#
# It really does need all of them on occasion. A first lap starts from the GRID order
# and the sheet does not update until the field crosses the line, so the pack can have
# rearranged itself completely before a single position changes: capping the passes at
# 6 left 1014 of capture2's 4279 frames half-sorted, and which cars got left behind
# moved from frame to frame, which read as the order flickering. The early break makes
# the normal case (nothing has changed) a single pass.

# Session-clock discontinuity (see TimeJump). Backwards is the dangerous direction and
# gets a tight threshold: a replay seek or a session reload lands far away, while the
# sim can still deliver a slightly jittery frame, so the epsilon is small but not zero.
# Forwards needs a LOOSE one: a dropped frame or a LAN hiccup is a normal forward gap
# and must not be mistaken for a scrub, so only a gap far longer than any stream stall
# counts. Between them, ordinary 10Hz telemetry (dt=0.1) is never a jump.
TIME_JUMP_BACK = 0.5       # seconds backwards before the clock is a different timeline
TIME_JUMP_FORWARD = 30.0   # seconds forwards; below this it is just a gap in the stream

# The most a car's lap distance may step in one tick and still have DRIVEN there. A car
# that appears on pit road further from where it was than this was put there (an
# Escape, a reset), and its arrival is a PIT_RESET, not a stop. Sized well over any
# tick (at 10Hz a car moves a thousandth of a lap or so, and a stalled feed catching up
# stays under a few hundredths) and well under a reset from anywhere but the pit
# entry itself; a reset from right there lands IN_PIT_STALL on its first frame, which
# is the other half of the test (see _events).
RESET_STEP_LAPS = 0.05

# How close two cars have to be, in laps of progress, for a place changing hands between
# them to be an OVERTAKE rather than merely a change of position. Mirrors the director's
# `pass_prox_laps`, which exists for the same reason and holds the same value: ~210 m at
# Spa. A place can change hands with nobody going past (the SHEET reorders two cars a
# quarter of a lap apart after a stop or a penalty), and the call that would produce is
# the worst-sounding one available: "makes it stick, past" a car most of a lap away.
PASS_PROX_LAPS = 0.03


def live_order(
    scored: list[int], progress: dict[int, float], held: Iterable[tuple[int, int]] = (),
) -> tuple[list[int], set[tuple[int, int]]]:
    """The running order as the viewer sees it: the timing sheet, with on-track passes
    that are already complete moved to where they belong.

    `scored` is the order by `CarIdxPosition` (P1 first), `progress` each car's laps
    completed plus fraction, `held` the pairs this returned inverted last frame, each
    written in the SHEET's direction as (listed-ahead, listed-behind). Returns the
    order and the new held set, which the caller feeds back in.

    That direction is load-bearing: an unordered pair key matches both ways round, so
    the relaxed margin would re-apply to the pair the instant it had been swapped and
    the loop would trade the two cars back and forth on every pass.

    That feedback is what makes the order STABLE. A place is taken at LIVE_PASS_TAKE
    and given back only at LIVE_PASS_YIELD the other way, so between the two whoever
    got there first keeps it. Without the memory the margin would sit around the
    SHEET's order rather than the current one, and a pair hovering at the threshold
    would re-order the tower off a 20 cm wobble: measured on capture2: 23 of 394
    position changes reversed inside a second, a 20 m swing in half a second, which is
    not something two cars can actually do.

    This is what the timing tower and the pop-ins are both numbered from, so the two
    graphics cannot contradict each other about who is running where. The sheet is
    still the input, so nothing here invents an order out of raw track position: a
    field that has not been scored yet (practice, the grid) arrives already sorted by
    progress and passes through untouched.
    """
    order = list(scored)
    held = set(held)
    for _ in range(len(order)):
        swapped = False
        for r in range(len(order) - 1):
            a, b = order[r], order[r + 1]
            lead = progress[b] - progress[a]      # how far the car behind is in front
            if abs(lead) >= _SAME_LAP_LAPS:
                continue                          # a lapping: progress says nothing
            # A held pair is governed by the yield threshold in BOTH directions. Only
            # the low take margin applies to a pair we have not already moved: letting
            # it apply to a held pair too means the hold swaps the pair and the take
            # rule immediately swaps it back, so the result comes down to whether the
            # loop happened to stop on an odd or an even pass.
            if (a, b) in held:
                margin = -LIVE_PASS_YIELD      # b is held in front: keep it there
            elif (b, a) in held:
                margin = LIVE_PASS_YIELD       # a is held in front: needs a real pass
            else:
                margin = LIVE_PASS_TAKE
            if lead > margin:
                order[r], order[r + 1] = b, a
                swapped = True
        if not swapped:
            break
    srank = {i: r for r, i in enumerate(scored)}
    return order, {
        (order[r + 1], order[r])                      # written the sheet's way round
        for r in range(len(order) - 1)
        if srank[order[r]] > srank[order[r + 1]]
    }


def live_places(order: list[int], pos, cls, cpos) -> tuple[dict[int, int], dict[int, int]]:
    """Each car's overall and in-class place, counted off the live order.

    Both come from the order rather than being read back out of `CarIdxPosition` /
    `CarIdxClassPosition`, because those are the lagging sheet: taking the place
    from one source and the ordering from another is what let a car sit in the P1 row
    wearing a "2".

    A place of 0 means "this session has no running order" and is preserved, not
    filled in: practice reports `CarIdxPosition == 0` for the whole field (DESIGN.md
    14), and the order there is only "who is furthest around the road". Everything
    downstream (the battle detector, the pop-ins, the tower column)
    already treats 0 as "say nothing", and inventing a number here would quietly
    switch all of them back on in a session that has no places to report.

    `pos`, `cls` and `cpos` are the raw channels and may be shorter than the field (or
    empty, for a feed that lacks one); an absent entry reads as unscored / no class.
    """
    overall: dict[int, int] = {}
    in_class: dict[int, int] = {}
    seen: dict[int | None, int] = {}
    for r, i in enumerate(order):
        c = cls[i] if i < len(cls) else None
        seen[c] = seen.get(c, 0) + 1
        scored = (i < len(pos) and pos[i] > 0) or (i < len(cpos) and cpos[i] > 0)
        overall[i] = r + 1 if scored else 0
        in_class[i] = seen[c] if scored else 0
    return overall, in_class


def _garaged(prev: CarState) -> CarState:
    """One car as it should read while it sits in the garage: classified, not running.

    Built from the last state the car had in the world rather than from this frame's
    channels, because there is nothing trustworthy in them for a car that is not in the
    world, and its lap times are facts it already earned. Everything derived from
    MOTION is neutralised instead of carried, so no consumer can read a parked car as a
    moving one: no place, no gaps, no closing rate, no speed. `progress` stays because it
    is an odometer (laps completed plus a fraction), which does not lie when parked; the
    lap-distance percentage goes to the sim's own -1.0 unknown, because "where on the
    road" has no answer here.
    """
    return replace(
        prev,
        position=0,          # holds no place: a session with a garage in it scores nobody
        class_position=0,
        lap_dist_pct=-1.0,
        speed=0.0,
        on_pit_road=False,   # in the garage is not on pit road, and must not read PIT
        surface=TrackSurface.NOT_IN_WORLD,
        on_track=False,
        gap_ahead=None,
        gap_behind=None,
        to_leader=None,
        track_gap_ahead=None,   # so nothing can score a battle against a parked car
        closing_rate=None,
        car_ahead_idx=None,
        est_time=None,          # a stale ruler reading must not measure anybody's gap
        driver_flags=0,
        in_garage=True,
    )


def _towed(prev: CarState, place: int, class_place: int) -> CarState:
    """One car as it should read while it is out of the world in a RACE: on the board at
    the live place it is falling back through, and inert. See CarState.towed.

    Built from the last state the car had in the world, like _garaged, and for the same
    reason: the channels for a slot with nobody in it say nothing trustworthy. The place
    is the live one, counted off an order this car still holds a spot in by its frozen
    progress, so it reads P1 until somebody drives past where it stopped.
    """
    return replace(
        prev,
        position=place,
        class_position=class_place,
        lap_dist_pct=-1.0,
        speed=0.0,
        on_pit_road=False,
        surface=TrackSurface.NOT_IN_WORLD,
        on_track=False,
        gap_ahead=None,
        gap_behind=None,
        to_leader=None,
        track_gap_ahead=None,
        closing_rate=None,
        car_ahead_idx=None,
        est_time=None,
        driver_flags=0,
        towed=True,
    )


class WorldModel:
    def __init__(self, session_info: SessionInfo, speed_alpha: float = 0.3):
        self.info = session_info
        self.track_len = session_info.track_length_m() or 4000.0
        self._drivers = session_info.drivers_by_idx()
        # Pace car is a real in-world CarIdx (idx 64 in the Spa capture); exclude it
        # from the competitor order and gap math.
        self._pace_idxs = {
            d.car_idx for d in session_info.drivers(include_pace_car=True) if d.is_pace_car
        }
        self.speed_alpha = speed_alpha

        self._prev_progress: dict[int, float] = {}
        self._prev_time: float | None = None
        self._speed: dict[int, float] = {}
        # idx -> (ahead_idx, on-track gap_s, ruler) from the previous frame, for closing rate
        self._prev_track_gap: dict[int, tuple[int, float, str | None]] = {}
        self._gap_ruler: dict[int, str] = {}      # which ruler measured each gap THIS frame
        # The track's CarIdxEstTime span. IDENTITY, not motion, so it deliberately outlives
        # every reset here: `_reset_motion` drops what "changed since last frame", and a
        # circuit is not that. Re-learning it after a replay scrub or a session change would
        # leave gaps unwrappable for the first lap back for no reason: the track is the
        # same one. It needs no lifecycle beyond the model's own, because `track_len` above
        # is fixed at construction for exactly the same reason: one model, one track.
        self._est_lap: float | None = None
        # Whether the feed is a saved tape or a live race. IDENTITY of the FEED, like
        # _est_lap is identity of the track: it survives every motion reset. See TapeBadge.
        self._tape = TapeBadge()
        self._closing_ema: dict[int, float] = {}  # idx -> smoothed closing rate
        # The LIVE place per car last frame, which is what a pass is detected off.
        # NOT the raw CarIdxPosition: see _events.
        self._prev_places: dict[int, int] = {}
        # pairs the live running order currently has inverted against the timing sheet,
        # fed back into live_order so an applied pass is not given back on a wobble
        self._live_held: set[tuple[int, int]] = set()
        self._prev_pit: dict[int, bool] = {}
        # Last frame's surface per active car, and the cars that were OUT of the world
        # last frame having been in it before: between them, how a car that is now on
        # pit road got there (see _events). A car back from NOT_IN_WORLD is not in
        # _prev_pit at all, so without _prev_out its arrival would fire nothing and a
        # tow would end in silence.
        self._prev_surface: dict[int, int] = {}
        self._prev_out: set[int] = set()
        # the off-track episode machine: per-car surface history and severity state
        self._incidents = IncidentTracker(self.track_len)
        # Sector pace for the whole field, measured at the boundaries SplitTimeInfo
        # gives (see sectors.py). Disabled, and CarState.pace stays None, on a feed
        # that has no boundaries.
        self._sectors = SectorTracker(boundaries_from_info(session_info))
        self._prev_session_num: int | None = None
        # The last state each car had while it was IN THE WORLD. A car that goes to the
        # garage between runs is classified from this rather than from the channels, which
        # is what makes it independent of whatever the sim reports for a slot with nobody
        # in it, and a lap time we watched being set cannot be taken away by a channel
        # going quiet. See _garaged.
        self._last_seen: dict[int, CarState] = {}

    def refresh_info(self, session_info: SessionInfo) -> None:
        """Adopt fresh session info mid-stream without resetting motion state.

        In team sessions DriverInfo is a moving target: UserName is whoever
        holds the seat NOW, so it changes at every driver swap. A long-lived
        world model must be able to re-learn names (a 6h endurance race had 21
        of 44 cars renamed under a model built at connect time) while keeping
        the per-car speed/gap/closing state, which is index-keyed and survives
        the swap untouched."""
        self.info = session_info
        self._drivers = session_info.drivers_by_idx()
        self._pace_idxs = {
            d.car_idx for d in session_info.drivers(include_pace_car=True) if d.is_pace_car
        }

    def _detect_jump(self, st: float, frame: Frame) -> str | None:
        """Is this frame continuous with the last one? See TimeJump."""
        num = frame.get("SessionNum")
        prev_num, self._prev_session_num = self._prev_session_num, num
        if self._prev_time is None:
            return None  # first frame of the stream: nothing to be discontinuous with
        if num is not None and prev_num is not None and num != prev_num:
            return TimeJump.SESSION  # a weekend crosses this twice; time restarts at each
        if st < self._prev_time - TIME_JUMP_BACK:
            return TimeJump.BACK
        if st > self._prev_time + TIME_JUMP_FORWARD:
            return TimeJump.FORWARD
        return None

    def _reset_motion(self) -> None:
        """Drop every piece of per-car state derived from frame-to-frame deltas.

        Everything here answers "what changed since last frame", and after a jump the
        last frame belongs to another timeline: progress deltas go negative (garbage
        speed), a car that has been off the road for a minute looks like it just went
        off, and the same overtake gets detected a second time. Dropping the lot makes
        the next frame behave exactly like the first frame of a stream, which is the
        one case every detector below already handles correctly: they all skip when
        there is no previous value, so no events fire off a discontinuity.

        Identity state (drivers, pace cars, track length, the tape badge) is NOT motion
        and stays."""
        self._prev_progress.clear()
        self._prev_time = None       # -> dt of 0, so no speed is derived from the jump
        self._speed.clear()
        self._prev_track_gap.clear()
        self._gap_ruler.clear()
        self._closing_ema.clear()
        self._prev_places.clear()
        # a held pass belongs to the timeline it was seen on: after a scrub the sheet
        # is authoritative again until we watch a car actually clear another
        self._live_held.clear()
        self._prev_pit.clear()
        self._prev_surface.clear()
        self._prev_out.clear()
        self._incidents.reset()
        self._sectors.reset(keep_refs=True)

    def resync(self) -> None:
        """Forget frame-to-frame motion, keeping identity and the session clock.

        For a consumer that STOPPED FEEDING US on purpose and is now resuming: the
        instant-replay path holds the live world model while the sim's tape is
        elsewhere, so the frame that arrives on return is not continuous with the last
        one this model saw, and `_detect_jump` cannot know that (on a saved tape the
        return lands within a second of where we left, which is not a jump by any
        threshold). Without this the first live frame back derives speed across the
        whole excursion, and a progress delta over several seconds can wrap a lap.
        """
        self._reset_motion()

    def update(self, frame: Frame) -> WorldSnapshot:
        st = frame.session_time
        jump = self._detect_jump(st, frame)
        if jump is not None:
            self._reset_motion()
            # The tape badge measures its settle window from a fixed start on the
            # session clock. After a scrub back that start is in the future, and the
            # badge fell to LIVE for as long as the scrub was. Re-base on the new
            # timeline; a live feed proves it is growing again within seconds.
            self._tape.reset()
            if jump == TimeJump.SESSION:
                # A lap time belongs to its session. The garage rule classifies a car
                # on the last time we SAW it set, and across a rollover that would be
                # practice's time on qualifying's board for every car still inside.
                self._last_seen.clear()
                self._sectors.reset(keep_refs=False)   # ...and so does a reference lap
        dt = (st - self._prev_time) if self._prev_time is not None else 0.0

        ch = Channels(frame)
        n = ch.n
        active = [
            i for i in range(n)
            if ch.surface(i) != TrackSurface.NOT_IN_WORLD and i not in self._pace_idxs
        ]
        # Cars in the GARAGE. NOT_IN_WORLD is one value doing two jobs: it is mostly "this
        # slot has nobody in it" (64 slots against 28 entries on capture2, so ~50 of them
        # read it for the whole session), and it is also a real car sitting in its garage
        # between runs. The discriminator is a lap time we have already watched this car
        # set (an empty slot never has one), and NOT the channels for this frame, which
        # is what makes it independent of what the sim reports for a car that is not in
        # the world.
        #
        # In a RACE the rhythm is different: out of the world means towed or retired, and
        # such a car stays ON the board, holding its spot in the order by the track
        # position it left from until the field drives past it (see CarState.towed). In
        # practice and qualifying going in is the rhythm of the session, and a driver who
        # has set a time stays classified on it for the rest of the session, off the
        # order, which is what every real timing screen does.
        session = session_snapshot(self.info, frame, is_replay=self._tape.showing_tape(frame))
        racing = is_race_kind(session.session_kind)
        out_of_world = [
            i for i in range(n)
            if ch.surface(i) == TrackSurface.NOT_IN_WORLD and i not in self._pace_idxs
            and i in self._last_seen
        ]
        garaged = [] if racing else [i for i in out_of_world
                                     if self._last_seen[i].best_lap is not None]
        towing = set(out_of_world) if racing else set()
        f2val = {i: ch.f2_time(i) for i in active}
        self._learn_est_lap(ch.est)

        # Laps completed is -1 until the car first crosses the line, and on a grid the
        # cars sit just BEFORE it at pct ~0.9999: progress there is -0.0001, and it must
        # be, because clamping the lap to 0 read 0.9999 and then 0.0001 one frame later.
        # That is a full lap backwards at the exact moment every car in the field
        # crosses, which is the start (13 cars in four seconds on capture2): speed
        # collapsed, the on-track gap to the car behind read None until IT crossed too,
        # and the tower's interval fell through to F2Time, which is 0.0 on lap 0.
        # Everything downstream works on DIFFERENCES between cars, so negative is fine.
        progress = {
            i: ch.lap_completed(i) + max(ch.lap_dist_pct(i), 0.0)
            for i in active
        }
        self._update_speed(active, progress, dt)
        self._sectors.update(st, {i: (progress[i], ch.on_pit_road(i)) for i in active},
                             {i: ch.best_lap(i) for i in active})

        # A towed car holds its spot by the progress it left the world at, so the order
        # is built over the racing cars AND the towed ones. The sheet position is what
        # the sim still reports for it, else the live place it last held: without the
        # channel it would sort to the back at once instead of falling back as passed.
        placing = {**progress, **{i: self._last_seen[i].progress for i in towing}}
        sheet = list(ch.pos)
        for i in towing:
            if not ch.position(i):
                sheet += [0] * max(0, i + 1 - len(sheet))
                sheet[i] = self._last_seen[i].position or 0
        scored = sorted([*active, *towing],
                        key=lambda i: ((sheet[i] if i < len(sheet) else 0) or 10_000,
                                       -placing[i]))
        order, self._live_held = live_order(scored, placing, self._live_held)
        rank = {idx: r for r, idx in enumerate(order)}
        places, class_places = live_places(order, sheet, ch.cls, ch.cpos)

        # Gaps, closing rates and passes are between cars that are actually on the road:
        # a towed car is a spot in the order, not a car anybody is racing.
        racing_order = [i for i in order if i not in towing]
        gap_ahead, gap_behind, ahead_of = self._gaps(racing_order, progress, f2val)
        track_gap = self._track_gaps(racing_order, progress, ch)
        closing = self._closing_rates(racing_order, track_gap, ahead_of, dt)
        events = self._events(active, order, rank, ch, places, st, placing, towing)

        cars = {
            i: self._car_state(
                i, ch, places=places, class_places=class_places, progress=progress,
                gap_ahead=gap_ahead, gap_behind=gap_behind, ahead_of=ahead_of,
                track_gap=track_gap, closing=closing,
            )
            for i in active
        }
        # Remembered from the racing set only, so a garage or tow state can never be
        # re-derived from an earlier one and drift further from the last real frame.
        self._last_seen.update(cars)
        # Out of the world in a race: on the board and in the order, inert (see _towed).
        cars.update({i: _towed(self._last_seen[i], places[i], class_places[i])
                     for i in towing})
        # Classified but not running (non-race). Added to `cars` and deliberately NOT to
        # `order`: nothing there is racing anybody, and everything that reads the order
        # (the director's shot candidates, gaps, passes, the field count) must
        # keep seeing exactly the cars on the road. The overlay owns how a non-race
        # session is CLASSIFIED and picks these up there.
        cars.update({i: _garaged(self._last_seen[i]) for i in garaged})
        snap = WorldSnapshot(
            tick=frame.tick,
            session_time=st,
            session=session,
            cars=cars,
            order=order,
            events=events,
            weather=_weather(frame),
            camera=_camera(frame),
            time_jump=jump,
            est_lap=self._est_lap,
        )

        self._prev_progress = progress
        self._prev_time = st
        self._prev_track_gap = {
            i: (ahead_of[i], track_gap[i], self._gap_ruler.get(i))
            for i in racing_order
            if track_gap.get(i) is not None and ahead_of.get(i) is not None
        }
        self._prev_places = {i: places[i] for i in active}
        self._prev_pit = {i: ch.on_pit_road(i) for i in active}
        self._prev_surface = {i: ch.surface(i) for i in active}
        self._prev_out = set(out_of_world)
        return snap

    def _learn_est_lap(self, est) -> None:
        """Learn this track's full CarIdxEstTime span: the modulus est gaps reduce by.

        The running maximum of the channel. EstTime climbs 0..span across the lap and
        resets at the line, so the largest value ever seen IS the span: at Spa it
        converges to 126.627s, and the leader sets it on its way to the first crossing,
        which is the first moment a wrap can happen. That ordering is why no configured
        per-track asset is needed: the value is always learned before it is wanted.

        Capped because this is a raw channel and the cap is what keeps one bad frame from
        poisoning the modulus for the rest of the session: a spuriously huge reading would
        stick forever (a maximum never comes back down) and inflate every wrapped gap.
        """
        for v in est:
            if (isinstance(v, (int, float)) and 0.0 < v < EST_LAP_MAX
                    and (self._est_lap is None or v > self._est_lap)):
                self._est_lap = float(v)

    def _update_speed(self, active: list[int], progress: dict[int, float], dt: float) -> None:
        for i in active:
            if dt > 0 and i in self._prev_progress:
                raw = (progress[i] - self._prev_progress[i]) * self.track_len / dt
                raw = max(raw, 0.0)
                prev = self._speed.get(i, raw)
                self._speed[i] = self.speed_alpha * raw + (1 - self.speed_alpha) * prev
            else:
                self._speed.setdefault(i, 0.0)

    def _gaps(self, order, progress, f2val):
        gap_ahead: dict[int, float | None] = {}
        gap_behind: dict[int, float | None] = {}
        ahead_of: dict[int, int | None] = {}
        for r, i in enumerate(order):
            if r == 0:
                gap_ahead[i] = None
                ahead_of[i] = None
            else:
                a = order[r - 1]
                if f2val.get(i) is not None and f2val.get(a) is not None:
                    # Interval from the SDK time-behind-leader. This is the real race
                    # gap and is immune to lap-boundary / lapped-car artifacts that
                    # broke the on-track-distance method (real Spa data: a +103s gap).
                    g = max(0.0, f2val[i] - f2val[a])
                else:
                    # fallback before timing settles: forward on-track distance / speed
                    dfrac = (progress[a] - progress[i]) % 1.0
                    g = dfrac * self.track_len / max(self._speed.get(i, 0.0), MIN_GAP_SPEED)
                gap_ahead[i] = g
                ahead_of[i] = a
                gap_behind[a] = g
        for i in order:
            gap_behind.setdefault(i, None)
        return gap_ahead, gap_behind, ahead_of

    def _track_gaps(self, order, progress, ch: Channels):
        """Continuous on-track time gap to the car directly ahead.

        Off the track's own ruler (CarIdxEstTime; see model.est_track_gap, #55) whenever
        both cars carry a reading and the lap span has been learned; else lap-distance
        over the trailing car's speed, which is what every feed without the channel
        (the synthetic source, the older captures) still gets.

        The ruler matters because this gap's DERIVATIVE is the closing rate the
        director's aliveness gate reads. Distance over instantaneous speed swells
        through every slow corner (a fixed separation breathed 4.6x per lap when #55
        measured it), so its derivative read the car ahead braking as a catch. Measured
        on capture2 before this change: of 52,466 battle-range pair-frames, 7.4% passed
        the gate on the speed-derived rate while the est gap was flat, and 3.1% were
        genuine catches the speed-derived rate missed; after it, 0.2% and 0.0%. The
        director's shot list on the same capture kept its 90/7/3 battle/leader/trouble
        split and the same pairs within a second, 40 cuts against 42.

        Unlike the F2Time interval (a per-lap staircase), this updates every frame, so
        its derivative is a real closing rate. Only populated when the pair is
        physically nose-to-tail on the same lap and within battle range; None else.
        `_gap_ruler` records which ruler measured each gap, so the closing rate never
        differentiates across a change of ruler (est arrives once the span is learned).
        """
        track_gap: dict[int, float | None] = {}
        ruler: dict[int, str] = {}
        for r, i in enumerate(order):
            if r == 0:
                track_gap[i] = None
                continue
            a = order[r - 1]
            dprog = progress[a] - progress[i]  # laps; a is ahead in the order
            if dprog >= TRACK_GAP_MAX_LAPS:
                track_gap[i] = None  # more than half a lap apart: not a nose-to-tail pair
                continue
            if dprog < 0.0:
                # the trailing-by-position car is ahead on track: an overtake in
                # progress. Report side-by-side (0) so the battle holds through the
                # pass, but only within a small margin (else it's a lapped-car / swap
                # artifact where the on-track math can't be trusted).
                track_gap[i] = 0.0 if dprog > -TRACK_GAP_CROSS_LAPS else None
                continue
            g = est_gap(ch.est_time(a), ch.est_time(i), dprog, self._est_lap)
            if g is None:
                g = dprog * self.track_len / max(self._speed.get(i, 0.0), MIN_GAP_SPEED)
                ruler[i] = "speed"
            else:
                ruler[i] = "est"
            track_gap[i] = g if g <= TRACK_GAP_MAX_S else None
        self._gap_ruler = ruler
        return track_gap

    def _closing_rates(self, order, track_gap, ahead_of, dt):
        """EMA-smoothed derivative of the on-track gap. Positive = the car behind is
        catching. Smoothing (CLOSING_TAU) is essential: real catches are ~0.02 s/s,
        below the frame-to-frame quantisation noise of raw lap-distance."""
        closing: dict[int, float | None] = {}
        alpha = min(1.0, dt / CLOSING_TAU) if dt > 0 else 0.0
        for i in order:
            tg = track_gap.get(i)
            a = ahead_of.get(i)
            prev = self._prev_track_gap.get(i)
            ruler = self._gap_ruler.get(i)
            if (tg is None or a is None or dt <= 0 or prev is None or prev[0] != a
                    or (prev[2] is not None and ruler is not None and prev[2] != ruler)):
                # gap invalid, no prior sample, the car ahead changed, or the gap changed
                # ruler: no rate, and drop the smoother's memory so a stale value can't
                # leak across.
                closing[i] = None
                self._closing_ema.pop(i, None)
                continue
            raw = max(-CLOSING_CLAMP, min(CLOSING_CLAMP, (prev[1] - tg) / dt))
            ema = self._closing_ema.get(i)
            ema = raw if ema is None else alpha * raw + (1 - alpha) * ema
            self._closing_ema[i] = ema
            closing[i] = ema
        return closing

    def _events(self, active, order, rank, ch: Channels, places, t, progress,
                towing: set[int] = frozenset()) -> list[Event]:
        racing_progress = {i: progress[i] for i in active}
        events: list[Event] = self._incidents.events(active, ch, t, racing_progress,
                                                     self._speed)
        for i in active:
            pp = self._prev_pit.get(i)
            now = ch.on_pit_road(i)
            if pp is None:
                # Not in the world last frame. Back from a tow (or the garage) straight
                # onto pit road is an arrival, and one nobody drove: a car that has never
                # been seen at all is a slot filling at session start, and says nothing.
                if now and i in self._prev_out:
                    events.append(Event(EventKind.PIT_RESET, i))
            elif not pp and now:
                kind = (EventKind.PIT_ENTRY if self._drove_in(i, ch, progress)
                        else EventKind.PIT_RESET)
                events.append(Event(kind, i))
            elif pp and not now:
                events.append(Event(EventKind.PIT_EXIT, i))

        # A pass fires when the LIVE place changes, not when the timing sheet catches up.
        # `CarIdxPosition` only turns over as the pair crosses the line (most of a
        # minute at Spa or Road America), so this used to announce a move the viewer had
        # watched most of a lap earlier, quite possibly over pictures of something else,
        # and a lead change landed long after the tower had already shown it.
        #
        # `places` is counted off `live_order`, which is the one running order the tower,
        # the pop-ins and the tower all read, so the cut and the graphics now
        # turn over on the same frame by construction rather than by coincidence.
        # `position` is therefore the place actually being taken, which is what the graphics
        # ranks a pass by. 0 still means "no running order" and is skipped, same as ever.
        #
        # There is deliberately NO hold-it-for-N-seconds confirmation of the kind the
        # director runs on its own detector. The live order's margins already are one, in
        # space rather than time: a place is taken with ~2 m of daylight and given back
        # only at ~10.4 m, so reverting a flip inside the director's 0.4s would take about
        # 31 m/s of relative velocity between two adjacent cars. That is structural, not
        # luck, and measured over capture2, grid_24 and sprint_20_a, a temporal confirm
        # rejected 0 of 276 flips. Machinery that never fires is worse than none.
        for i in active:
            pnow = places.get(i, 0)
            pprev = self._prev_places.get(i)
            if pprev is None or pnow <= 0 or pnow >= pprev:
                continue
            r = rank[i]
            if r + 1 >= len(order):
                continue
            b = order[r + 1]
            if b in towing:
                continue      # driving past a car on a flatbed is not an overtake
            if self._prev_places.get(b, 10_000) >= pprev:
                continue
            if abs(progress[i] - progress[b]) >= PASS_PROX_LAPS:
                continue      # a place changed hands, but not by anybody going past
            # ...nor by anybody STOPPING. A car peeling into the lane hands its place to
            # whoever is behind, and on the approach it is still close enough in
            # lap-distance to clear the proximity gate above, so a routine stop was
            # being called as an overtake, and a leader's stop as a lead change. Both
            # sides checked: the pitting car may be either the one losing the place or,
            # on the way back out at pit-lane speed, the one being gone past.
            if (in_pit_lane(ch.on_pit_road(i), ch.surface(i))
                    or in_pit_lane(ch.on_pit_road(b), ch.surface(b))):
                continue
            events.append(Event(EventKind.OVERTAKE, i, other_idx=b, position=pnow))
        return events

    def _drove_in(self, i: int, ch: Channels, progress: dict[int, float]) -> bool:
        """Did this car, on pit road for the first time this frame, DRIVE there?

        The sim reports a stop and a reset identically (`CarIdxOnPitRoad` goes true),
        and Round 1 (2026-09-20) had a pit stop reported for every finisher pressing
        Escape. Three things separate them, any one of which is a reset: the car was out
        of the world last frame (a tow ends in the pit box), it is IN_PIT_STALL on the
        very frame it appears on pit road (a drive-in enters through the approach road;
        measured on capture2, the surface reads APPROACHING_PITS for three seconds
        before the flag flips), or its lap distance stepped further than a car can
        drive in a tick. All three answer for a reset from anywhere: a car put down
        right by the pit entry moves too little to trip the step, and lands in its
        stall instead.
        """
        prev_surf = self._prev_surface.get(i)
        if prev_surf is None or prev_surf == TrackSurface.NOT_IN_WORLD:
            return False
        if ch.surface(i) == TrackSurface.IN_PIT_STALL:
            return False
        before = self._prev_progress.get(i)
        return before is None or abs(progress[i] - before) < RESET_STEP_LAPS

    def _car_state(self, i: int, ch: Channels, *, places, class_places, progress,
                   gap_ahead, gap_behind, ahead_of, track_gap, closing) -> CarState:
        d = self._drivers.get(i)
        return CarState(
            idx=i,
            number=d.number if d else str(i),
            name=d.name if d else None,
            car_model=d.car if d else None,
            car_path=d.car_path if d else None,
            country=d.country if d else None,
            class_id=ch.car_class(i),
            position=places[i],
            class_position=class_places[i],
            lap=ch.lap(i),
            lap_completed=ch.lap_completed(i),
            lap_dist_pct=ch.lap_dist_pct(i),
            progress=progress[i],
            speed=self._speed.get(i, 0.0),
            on_pit_road=ch.on_pit_road(i),
            surface=ch.surface(i),
            on_track=ch.surface(i) == TrackSurface.ON_TRACK,
            gap_ahead=gap_ahead.get(i),
            gap_behind=gap_behind.get(i),
            to_leader=ch.f2_time(i),
            track_gap_ahead=track_gap.get(i),
            closing_rate=closing.get(i),
            car_ahead_idx=ahead_of.get(i),
            est_time=ch.est_time(i),
            last_lap=ch.last_lap(i),
            best_lap=ch.best_lap(i),
            driver_flags=ch.driver_flags(i),
            pace=self._sectors.pace.get(i),
        )


def run(source: TelemetrySource) -> Iterator[WorldSnapshot]:
    """Iterate a source through a fresh WorldModel, yielding one snapshot per frame."""
    wm = WorldModel(source.session_info())
    for fr in source.frames():
        yield wm.update(fr)
