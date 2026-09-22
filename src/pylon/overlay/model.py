"""The tower model: a WorldSnapshot to the JSON the page renders.

`snapshot_to_model` is the seam between the world model and the browser overlay
(overlays/broadcast-overlay.html and overlay.js); every field on the wire is documented
where it is built. `TowerModel` is the stateful wrapper that holds the class map, the
session's fastest lap and the shot source, and builds one model per snapshot.
"""

from __future__ import annotations

from typing import Protocol

from ..director import Director
from ..director.model import Shot
from ..telemetry.constants import SessionFlag
from ..telemetry.frame import SessionInfo
from ..world import CarState, SessionKind, TimeJump, WorldSnapshot, is_race_kind
from ..world.model import est_track_gap
from .identity import (
    _DEFAULT_CLASS_COLOR,
    _display_name,
    _name_parts,
    _tla,
    class_meta,
    driver_meta,
    tla_sheet,
)


def _focus_car(idx: int, on_cam: bool, snap: WorldSnapshot, classes: dict[int, dict],
               people: dict[int, dict] | None = None) -> dict | None:
    c = snap.cars.get(idx)
    if c is None:
        return None
    cm = classes.get(idx, {})
    first, last = _name_parts(c.name)
    return {
        "id": c.idx,
        "num": c.number,
        "tla": tla_sheet(snap.cars).get(idx) or _tla(c.name, c.number),
        "full": _display_name(c.name),
        # the battle bar's two-line treatment: given name small over the SURNAME large.
        # Both "" for a car with no name at all, which sends the card back to `full`.
        "first": first,
        "last": last,
        # the same live place the tower row prints: one number, one source
        "pos": max(0, c.position),
        "classPos": max(0, c.class_position),
        "className": cm.get("name", ""),
        "carTag": cm.get("tag", ""),
        "carModel": cm.get("model", ""),
        "classColor": cm.get("color", 0x35E3FF),
        "onCam": on_cam,
        **(people or {}).get(idx, {}),
    }


# Gap math. We compute time gaps from lap-distance every frame rather than trusting
# iRacing's CarIdxF2Time, which is a per-lap staircase (flat between line crossings, and
# often a stale 0.0 for spread cars), that staircase is why the timing "relatives" and
# the battle chip used to freeze for ~a lap and read 0.0.
_GAP_MIN_SPEED = 5.0    # m/s floor (matches the world builder) so a slow car isn't an infinite gap
_SAME_LAP_LAPS = 0.5    # within half a lap of the car ahead == racing on the same lap


# A car's flag, decoded to ONE broadcast word. The page gets a token, never the raw
# bitfield: iRacing's bit layout is telemetry's business, and a browser source that has
# to know which bit is the meatball is a browser source that goes wrong on an SDK change.
#
# Ordered by what a viewer needs to know first. Disqualification outranks the black flag
# that led to it (DQ_SCORING_INVALID sets DISQUALIFY too, so DSQ wins for both), a
# standing penalty outranks a mechanical order, and the furled black is last because it
# is only a warning. `driver_flags` is already masked, so the background `servicible` bit
# that rides along on nearly every car of nearly every frame is not in play here.
_FLAG_WORDS = (
    (SessionFlag.DISQUALIFY, "dsq"),
    (SessionFlag.BLACK, "black"),
    (SessionFlag.REPAIR, "repair"),     # the meatball
    (SessionFlag.FURLED, "warn"),
)


def _flag_token(c: CarState) -> str:
    """Which flag this car is under, as a token, or "" for the normal state."""
    for bit, token in _FLAG_WORDS:
        if c.driver_flags & bit:
            return token
    return ""


def _pair_gap(shot: Shot, snap: WorldSnapshot, track_len: float | None) -> float | None:
    """Live on-track time gap between a battle's two cars, off the track's own ruler.

    Measured from whichever car is BEHIND ON TRACK, not from whichever the timing line has
    credited with the position, which is what keeps it correct THROUGH a pass: it closes to
    ~0 as they swap and opens again (the old directional gap stuck at 0.0 while position
    lagged the pass).

    Uses `est_track_gap` rather than dividing their separation by the trailing car's
    instantaneous speed (#55). The chip sits next to two cars the viewer can see are
    nose to tail, so a number that swelled through every slow corner was visibly wrong."""
    if not shot.pair:
        return None
    a = snap.cars.get(shot.pair[0])
    b = snap.cars.get(shot.pair[1])
    if a is None or b is None:
        return None
    if abs(a.progress - b.progress) >= _SAME_LAP_LAPS:  # not a nose-to-tail pair
        return None
    ahead, behind = (a, b) if a.progress >= b.progress else (b, a)
    gap = est_track_gap(ahead, behind, snap.est_lap)
    if gap is None:
        # No est reading for one of them (a feed without the channel, or the first lap
        # before the span is learned). Fall back to the old distance/speed estimate rather
        # than dropping the chip: it is a worse number, but the pair really is nose to
        # tail, so it is small and the error with it.
        if not track_len:
            return None
        dprog = abs(a.progress - b.progress)
        gap = dprog * track_len / max(behind.speed, _GAP_MIN_SPEED)
    return round(gap, 2)


def _battle_cards(shot: Shot, snap: WorldSnapshot, classes: dict[int, dict],
                  people: dict[int, dict] | None) -> list[dict]:
    """The two pop-ins for a battle, leading car first.

    Ordered by the running order, which is already live (`builder.live_order`), and
    NOT by a second reading of track position. That is the whole fix: one order decides
    both which card is on the left and what number is printed on it, so the left-hand
    card is always the higher tower row. Applying a track-position tiebreak on top
    would reintroduce the bug in miniature: its margin is a clear car length while
    the running order takes a place at a couple of metres, so in the window between
    them the cards would come out left-to-right P2, P1.

    The pair is re-read from the snapshot rather than trusted in the order the shot
    lists it: the overlay can receive the shot back over the bridge link, so its pair
    tuple may pre-date the last change of order.
    """
    a, b = (snap.cars.get(shot.pair[0]), snap.cars.get(shot.pair[1]))
    if a is None or b is None:  # a car left the world mid-shot: fall back to the pair
        return [c for c in (_focus_car(i, i == shot.target_idx, snap, classes, people)
                            for i in shot.pair) if c]
    front, back = (a, b) if a.position <= b.position else (b, a)
    return [c for c in (_focus_car(car.idx, car.idx == shot.target_idx, snap, classes, people)
                        for car in (front, back)) if c]


def _focus_group(shot: Shot | None, snap: WorldSnapshot, classes: dict[int, dict],
                 track_len: float | None = None,
                 people: dict[int, dict] | None = None) -> dict | None:
    """The cars the broadcast is on, for the pop-in name bug(s).

    A battle is two cars (the pair), so it yields two pop-ins; every other shot is
    one. `onCamId` is the car the camera actually frames (the shot target). A battle
    also carries the live `gap` between the pair for the pop-in's gap chip.

    THE BATTLE TREATMENT IS RACE-ONLY (#53). Two cars running close together in practice
    or qualifying are not fighting (they are sharing a circuit, usually one on a hot lap
    and one on an out lap), and a two-car bar with a live gap chip tells the viewer
    something false. Same principle as the tower's lap-time classification (#40): a
    session with no positions to fight over shows nothing that implies a fight.

    The gate is here rather than upstream because the director has no session awareness
    at all yet (#49) and keeps handing over pairs in practice. The overlay must be right
    on its own, whatever shot it is given, so a pair in a non-race session falls through
    to a single pop-in on the camera's own car."""
    if shot is None:
        return None
    racing = is_race_kind(snap.session.session_kind)
    # battle: show both cars (the one in front on TRACK first); else just the target
    if racing and shot.pair and len(shot.pair) == 2:
        cars = _battle_cards(shot, snap, classes, people)
    else:
        cars = [c for c in [_focus_car(shot.target_idx, True, snap, classes, people)] if c]
    if not cars:
        return None
    group: dict = {"kind": shot.kind, "onCamId": shot.target_idx, "cars": cars}
    # No pair means no gap chip: the chip is the claim that these two are racing, and one
    # card has nothing to be a gap TO. (The page already needs two cards to draw it, so
    # this is belt and braces on a model that should not carry the number at all.)
    if racing and shot.pair:
        gap = _pair_gap(shot, snap, track_len)
        if gap is not None:
            group["gap"] = gap
    return group


def snapshot_to_model(
    snap: WorldSnapshot,
    track: str | None = None,
    *,
    classes: dict[int, dict] | None = None,
    focus: dict | None = None,
    replay: bool | None = None,
    track_len: float | None = None,
    people: dict[int, dict] | None = None,
    fastest: dict | None = None,
) -> dict:
    """Turn a WorldSnapshot into the overlay's model (see render() in the HTML).

    `fastest` is the TowerModel's memory of the session's quickest lap ({idx, lap}),
    mutated here; see the purple-marker walk below for why a memory is needed.
    """
    order, cars = snap.order, snap.cars
    tlas = tla_sheet(cars)          # distinct across the field, see identity.tla_sheet
    classes = classes or {}
    people = people or {}
    track_len_m = track_len or 4000.0

    # Prefer the SDK's real gap-to-leader (CarIdxF2Time via CarState.to_leader);
    # fall back to a cumulative-interval estimate only where it's missing. Computed off
    # the RUNNING order (snap.order, never the classification below), so the field keeps
    # meaning what its name says whatever the rows end up sorted by, and so a car that
    # is not running is simply absent from it.
    to_leader: dict[int, float] = {}
    cumulative = 0.0
    for rank, i in enumerate(snap.order):
        if rank == 0:
            to_leader[i] = 0.0
        else:
            cumulative += cars[i].gap_ahead or 0.0
            tl = cars[i].to_leader
            to_leader[i] = tl if tl is not None else cumulative

    s = snap.session
    # Is there a race on? `is_race_kind` is deliberately the same predicate the director
    # gates its wording on, because the tower and the voice disagreeing about whether
    # this is a race is worse than either being wrong on its own: the same argument
    # that put ONE definition in world/model.py. It answers no for practice, qualifying,
    # warm-up and testing: none of them has a leader, and none of them has an interval
    # that means anything (DESIGN.md 14).
    by_lap = not is_race_kind(s.session_kind)
    if by_lap:
        # A non-race session is classified by LAP TIME, which is how every real practice
        # and qualifying timing screen works, and the running order cannot do that job:
        # `CarIdxPosition` is 0 for the whole field, so `snap.order` is left sorting by
        # track progress: "P1" becomes whoever is furthest around the lap at this
        # instant, it reorders constantly, and it means nothing.
        #
        # Classified off `cars` rather than `order`, which is what keeps a driver on the
        # board while they sit in the garage between runs (#46). `order` is who is
        # RACING, and the world model deliberately leaves a garaged car out of it; the
        # classification is a different question, and a set time is what answers it.
        #
        # Cars with no time yet go to the BOTTOM, keyed on `idx` and not on anything
        # live, because that is most of the field for the opening stretch of a session
        # (on the practice capture it is all 64 slots of all 3020 frames) and a bottom
        # block that shuffled itself every frame would be the same churn moved down the
        # tower. `best_lap` is already sentinel-free, so None means "no lap": a naive
        # sort over the raw channel would put -1.0 first and top the tower with whoever
        # has not turned a wheel.
        order = sorted(cars, key=lambda i: (cars[i].best_lap is None,
                                            cars[i].best_lap or 0.0, i))

    # Purple: whoever holds the quickest lap of the session, and the time itself.
    #
    # `best_lap` is already sentinel-free: iRacing spells "no valid lap yet" as -1.0 and
    # the builder drops it, so a None here means "no lap" rather than "a lap we failed to
    # parse". That is what keeps this honest: "no time yet" is the NORMAL state for most
    # of the field for a long while (measured on capture2: 291,677 sentinel car-frames
    # against 16,411 real ones; on the practice capture it is every slot of every frame),
    # and a plain min() over the raw channel returns -1.0 and hands the marker to a car
    # that has not turned a wheel. The `> 0.0` below is belt and braces for that one bug.
    #
    # Walked in the order the tower shows, so a dead heat on the same thousandth resolves
    # to the higher row and exactly one row can ever wear the marker. In a non-race
    # session that means a car in the garage keeps the purple it earned: the time is the
    # session's quickest whether or not its driver is still on the road.
    fastest_idx: int | None = None
    fastest_lap: float | None = None
    for i in order:
        bl = cars[i].best_lap
        if bl is not None and bl > 0.0 and (fastest_lap is None or bl < fastest_lap):
            fastest_idx, fastest_lap = i, bl
    # The marker is STICKY across the holder's absence. In a race a towed or retired
    # car leaves the board entirely (world.builder), so the walk above handed the
    # purple to the next-best car and the page toasted "FASTEST LAP" for a lap nobody
    # had just set: then again in reverse when the tow brought the car back. The
    # session's quickest lap does not change because its author is on a flatbed: the
    # time stays on the cap, nobody wears the purple until they are back, and only a
    # genuinely quicker lap moves it. A new session starts the memory over.
    if fastest is not None:
        if snap.time_jump == TimeJump.SESSION:
            fastest.clear()
        held_idx, held_lap = fastest.get("idx"), fastest.get("lap")
        if held_lap is not None and (fastest_lap is None or held_lap <= fastest_lap):
            fastest_lap = held_lap
            fastest_idx = held_idx if held_idx in cars else None
        else:
            fastest["idx"], fastest["lap"] = fastest_idx, fastest_lap

    # The final lap, which the cap states in WORDS rather than leaving to a colour (#65).
    # Race-only: practice and qualifying have no last lap in this sense, and `is_last_lap`
    # would otherwise put a final-lap chip on the end of every practice session.
    #
    # Dropped the moment the checkered is out, because "one lap to go" stops being true
    # there, and that is the one confusion this must not create, since --flag-checkered
    # is a near-white already. The words are what tell the two states apart; the bar's
    # colour only agrees with them.
    last_lap = (not by_lap) and s.is_last_lap and not s.is_checkered
    # White sits between checkered and green, so the more urgent CONDITION always keeps
    # the bar: a caution on the last lap shows the caution. That costs nothing, because
    # unlike the others white is not a state of the track (it says how much racing is
    # left), and the cap says so in words regardless of what colour the bar is wearing.
    flag = ("red" if s.is_red else "yellow" if s.is_yellow
            else "checkered" if s.is_checkered
            else "white" if (last_lap and s.is_green)
            else "green" if s.is_green else "none")
    # The leader's lap, which is only a session-wide fact in a race. In practice every car
    # is on its own lap number and the top row's is nobody else's business, so the cap is
    # sent no lap at all rather than a number that reads like one.
    lap = 0 if by_lap else (cars[order[0]].lap if order else 0)
    leader_prog = cars[order[0]].progress if order else 0.0
    # event clock: for a timed race, elapsed = total - remaining (session_time also
    # counts warmup); for a lap race there's no total, so fall back to session_time.
    elapsed = (s.time_total - s.time_remaining
               if (s.time_total is not None and s.time_remaining is not None)
               else s.session_time)

    out = []
    for rank, i in enumerate(order):
        c = cars[i]
        cm = classes.get(i, {})
        laps_down = max(0, int(leader_prog - c.progress))
        # interval to the car directly ahead in the order (the useful column in
        # endurance, where to-leader is just "+5 LAPS"). Computed from lap-distance every
        # frame so it stays LIVE: iRacing's CarIdxF2Time is a per-lap staircase that freezes
        # (and reads a stale 0.0) between line crossings, which is why spread cars' relatives
        # used to sit unchanged for ~a lap. intervalLaps is whole laps to that car (so a
        # lapped car reads "+1 L" to the car ahead, not "+5 LAPS" to the leader).
        ahead = cars[order[rank - 1]] if rank > 0 else None
        if by_lap:
            # None of the race relatives survives a lap-time classification: the row above
            # is not a car this one is racing, so an on-track gap to it is two unrelated
            # cars that happen to be near each other, and "laps down" is meaningless when
            # everyone is running their own programme. Zeroed rather than left to compute
            # something the page might print.
            interval_laps = 0
            interval = 0.0
            laps_down = 0
        elif ahead is not None:
            dprog = ahead.progress - c.progress
            interval_laps = max(0, int(dprog))
            # ONE source for this column (#55). It used to be three (the builder's
            # speed-derived on-track gap, a distance-over-instantaneous-speed estimate, and
            # CarIdxF2Time (which is time behind the LEADER, a different question entirely)
            #) picked per frame by conditions that flip. Every switch was a step in a
            # number a viewer reads as continuous, and that is what produced 44s jumps
            # between adjacent frames. Measured over 105,630 like-for-like frame pairs on
            # the Spa race capture: 190 jumps above 0.5s before, 1 after, and that one is a
            # car being physically teleported by a tow.
            est = est_track_gap(ahead, c, snap.est_lap)
            if est is not None:
                interval = round(est, 2)
            elif c.track_gap_ahead is not None and c.track_gap_ahead > 0.0:
                # Fallbacks, in order, for a feed with no CarIdxEstTime (the synthetic
                # source, an older capture) or the opening lap before the span is learned.
                # Kept because a wrong-but-close number beats a blank column; they are no
                # longer in play frame to frame, so they can no longer fight each other.
                interval = round(c.track_gap_ahead, 2)
            elif abs(dprog) < _SAME_LAP_LAPS:                    # same lap: live gap
                interval = round(abs(dprog) * track_len_m / max(c.speed, _GAP_MIN_SPEED), 2)
            elif c.gap_ahead is not None:                        # lapped / far: F2Time fallback
                interval = round(c.gap_ahead, 2)
            else:
                interval = 0.0
        else:
            interval_laps = 0
            interval = 0.0
        out.append({
            "id": c.idx,
            # The live place, straight from the world model, and the SAME field the
            # pop-in prints. No rank fallback: a 0 means the session scores nobody
            # (practice), and the tower used to answer that by counting rows, so it
            # showed a churning P1..P28 by who was furthest around the road while the
            # name bug over the same car showed a blank. The column goes empty instead.
            "pos": max(0, c.position),
            "num": c.number,
            "tla": tlas.get(c.idx) or _tla(c.name, c.number),
            "full": _display_name(c.name),
            "classId": c.class_id,
            "className": cm.get("name", ""),
            "carTag": cm.get("tag", ""),
            "carModel": cm.get("model", ""),
            "classColor": cm.get("color", _DEFAULT_CLASS_COLOR),
            "classPos": max(0, c.class_position),
            "classLeader": c.class_position == 1,
            "interval": interval,
            "intervalLaps": interval_laps,
            # 0.0 for a car that is not in the running order at all (one sitting in the
            # garage): there is no gap between a parked car and the leader.
            "toLeader": round(to_leader.get(i, 0.0), 2),
            "lapsDown": laps_down,
            # Nobody LEADS a practice session, so nothing wears the leader treatment
            # there. The quickest car still stands out, and by the purple it earned
            # rather than by a chip claiming a race position: in a lap-time
            # classification the fastest-lap holder IS the top row, by construction.
            # ...and nobody LEADS from a flatbed: a towed car holds P1 in the order until
            # passed, and the row says TOW instead of LEADER for as long as it does.
            "isLeader": rank == 0 and not by_lap and not c.towed,
            "onPit": c.on_pit_road,
            # Out of the world in a race (CarState.towed): on the board at the place it is
            # falling back through, quiet, TOW in the outrigger, no interval. The row
            # above a towed car still measures its gap to where the car stopped.
            "tow": c.towed,
            # In the garage between runs, still classified on the time it set (#46). Not a
            # kind of PIT: the two are mutually exclusive, and this car is not coming back
            # out this minute. The row goes quiet and the outrigger stops offering a last
            # lap the car is no longer improving on.
            "garage": c.in_garage,
            # A non-race session's classification number: this car's own best lap, and
            # how far it sits off the quickest one (0.0 for the car setting the pace).
            # Both null outside a lap-time session, and null for a car with no lap yet.
            "bestLap": round(c.best_lap, 3) if (by_lap and c.best_lap is not None) else None,
            "lapGap": (round(c.best_lap - fastest_lap, 3)
                       if (by_lap and c.best_lap is not None and fastest_lap is not None)
                       else None),
            # Outrigger column: last completed lap, or null for a car that has not set
            # one. Null rather than 0 or -1 on purpose: the overlay renders nothing at
            # all for it, and the sentinel never reaches a comparison (DESIGN.md 14).
            "lastLap": round(c.last_lap, 3) if c.last_lap is not None else None,
            # highlight a row as "battling" from the continuous on-track gap, but never
            # when this car or the one ahead is in the pits (a car alongside the pit exit
            # isn't fighting for position), and never outside a RACE (#53): the row above
            # is not a car this one is racing in a lap-time session, so two cars a
            # half-second apart there are a hot lap and an out lap, not a fight.
            "battle": (not by_lap
                       and c.track_gap_ahead is not None and c.track_gap_ahead < 0.75
                       and not c.on_pit_road and (ahead is None or not ahead.on_pit_road)),
            "fastest": i == fastest_idx,
            # Which flag this car is under, "" for the normal state, which is almost
            # every car almost always. NOT "flag": the credentials spread below already
            # owns that key for the driver's COUNTRY flag emoji, and it would silently
            # win, since it is spread last.
            "penalty": _flag_token(c),
            **people.get(i, {}),
        })
    return {
        "session": {
            "flag": flag, "track": track, "lap": lap, "totalLaps": None,
            # The cap prints "LAST LAP" off this and demotes the lap/clock readout to the
            # caption under it. Separate from `flag` on purpose: under a last-lap caution
            # the bar is yellow and this is still true, and both facts belong on screen.
            "lastLap": last_lap,
            # WHICH session of the weekend this is. `type` is the SessionKind constant
            # (for anything that wants to branch), `typeLabel` the words to print.
            "type": s.session_kind or None,
            "typeLabel": _session_label(s),
            # LIVE/REPLAY badge: is this race happening right now? serve() forces True,
            # because playing back a recording is never live whatever the captured frames
            # say. The bridge path passes None and lets the world model answer, which it
            # does by asking whether the sim's replay tape is still being written: see
            # world.session.TapeBadge, and do not substitute bare IsReplayPlaying (#62).
            "replay": s.is_replay if replay is None else replay,
            "elapsed": round(elapsed, 1),
            "timeRemaining": round(s.time_remaining, 1) if s.time_remaining is not None else None,
            "timeTotal": round(s.time_total, 1) if s.time_total is not None else None,
            "lapsRemaining": s.laps_remaining,
            # The quickest lap anyone has turned, or null while nobody has set one,
            # which is a long opening stretch of every session, not an edge case. The
            # row carrying `fastest` says WHO; this is the time, and the two are read
            # together for the toast that announces a change of hands.
            "fastestLap": round(fastest_lap, 3) if fastest_lap is not None else None,
        },
        "focus": focus,
        "cars": out,
    }


# What the header calls each kind of session. Broadcast wording, not iRacing's:
# "QUALIFYING" rather than the raw "Lone Qualify". The label is always shown, race
# included, because a badge that only appears sometimes is a badge nobody reads,
# and a practice tower looks enough like a race one (same rows, same gaps, but no
# positions and nobody actually racing) that the viewer has to be told which it is.
_SESSION_LABELS = {
    SessionKind.PRACTICE: "PRACTICE",
    SessionKind.QUALIFY: "QUALIFYING",
    SessionKind.WARMUP: "WARM-UP",
    SessionKind.RACE: "RACE",
    SessionKind.TESTING: "TESTING",
}


def _session_label(s) -> str:
    """Header text for the session. An iRacing session type we do not classify still
    gets printed (uppercased, as-is) rather than dropped: a strange label beats the
    viewer being told nothing about what they are watching."""
    return _SESSION_LABELS.get(s.session_kind) or (s.session_type or "").upper()


def _shot_from_echo(d: dict | None) -> Shot | None:
    """Rebuild the live director's shot from the bridge echo (see BridgeServer)."""
    if not d or d.get("target") is None:
        return None
    pair = d.get("pair")
    return Shot(kind=d.get("kind", ""), key="", target_idx=int(d["target"]),
                label="", pair=tuple(pair) if pair else None)


class ShotSource(Protocol):
    """Where the camera focus for a model comes from. See LocalDirector (a recording)
    and transport.PublishedShots (the live show); TowerModel asks it only when build() is
    not handed a shot."""

    def current(self, snap: WorldSnapshot) -> Shot | None: ...


class LocalDirector:
    """A recording has no live director, so a local one mirrors what the live one would
    pick. For serve() and previews only: live, the real director's shot is published
    (show/shotlink) and a simulated one would follow a camera that is not there."""

    def __init__(self) -> None:
        self.director = Director()

    def current(self, snap: WorldSnapshot) -> Shot | None:
        self.director.update(snap)   # advances shot state; .current holds the shot
        return self.director.current


class TowerModel:
    """Stateful overlay-model builder: holds the class map and turns snapshots into
    tower models, tagged with the shot the broadcast is currently on.

    The shot is either handed to build() by the caller (the live pump, from the
    director's own publications) or asked of `shots` (a LocalDirector, for a recording).
    With neither, the model carries no focus, which is the safe default: nothing here
    ever invents a camera position."""

    def __init__(self, info: SessionInfo | None, *, shots: ShotSource | None = None,
                 force_replay: bool | None = None):
        info = info or SessionInfo({})
        self.track = info.track_name
        self.track_len = info.track_length_m() or 4000.0
        self.classes = class_meta(info)
        self.people = driver_meta(info)
        self.shots = shots
        self.force_replay = force_replay  # None -> use the frame's IsReplayPlaying
        self._fastest: dict = {}          # the session's quickest lap, remembered

    def refresh_info(self, info: SessionInfo) -> None:
        """Adopt fresh session info: team driver swaps rename cars mid-race, and swap
        in a different driver's iRating, licence and country along with the name."""
        self.track = info.track_name or self.track
        self.track_len = info.track_length_m() or self.track_len
        self.classes = class_meta(info)
        self.people = driver_meta(info)

    def build(self, snap: WorldSnapshot, shot: Shot | None = None) -> dict:
        if shot is None and self.shots is not None:
            shot = self.shots.current(snap)
        focus = _focus_group(shot, snap, self.classes, self.track_len, self.people)
        return snapshot_to_model(snap, self.track, classes=self.classes, focus=focus,
                                 replay=self.force_replay, track_len=self.track_len,
                                 people=self.people, fastest=self._fastest)
