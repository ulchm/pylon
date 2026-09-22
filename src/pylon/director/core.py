"""The director: score candidate shots, then run a shot state machine.

Scoring (per DESIGN.md section 5) turns each candidate shot into an interest
number. The state machine (section 6) turns a stream of scored candidates into a
watchable shot list, using hysteresis (min/max hold, cut margin) and a separate
interrupt lane for incidents.

Dry-run: it emits Decision objects (a shot list), it does not drive any camera.
Phase 3 maps a Shot.target_idx to an iRacing cam_switch_num on a TV group.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass, replace

from ..show.contract import subject_of
from ..telemetry.constants import TrackSurface
from ..world import (
    CarState,
    Event,
    EventKind,
    WorldSnapshot,
    car_in_pits,
    is_race_kind,
    severity_at_least,
    track_order,
)
from ..world import run as run_world
from ..world.finish import FinishTracker
from .config import DirectorConfig
from .model import Decision, Shot, ShotFlavor, ShotKind


def _battle_label(a: CarState, b: CarState, gap: float) -> str:
    return f"P{a.position} #{a.number} vs P{b.position} #{b.number}  {gap:.2f}s"


def _lap_str(secs: float) -> str:
    """A lap time for a LOG line ("2:08.573"). Digits on purpose: this is the shot label,
    which is read by a person watching the director, never by the voice."""
    m, rest = divmod(secs, 60.0)
    return f"{int(m)}:{rest:06.3f}" if m else f"{rest:.3f}"


# Was defined here; it now lives in the world model, because the pass detectors in
# world/builder.py and director/replay.py ask the same question and were not asking it
# at all. See world.in_pit_lane for what it is for.
_in_pits = car_in_pits


def _battle_score(a: CarState, b: CarState, gap: float, snap: WorldSnapshot,
                  cfg: DirectorConfig) -> float:
    closeness = min(1.0, max(0.0, 1.0 - gap / cfg.battle_max_gap))  # 1 at 0s, 0 at battle_max_gap
    closing = max(0.0, b.closing_rate or 0.0)
    side_by_side = gap < cfg.side_by_side_gap
    # A battle is only "alive" if a pass is imminent (side-by-side) or the gap is
    # actually shrinking. A static gap lap after lap is a train: we still rank it,
    # but damped, so a genuinely live fight or the leader outranks it and we never
    # sit on a trailing car that's doing nothing.
    active = side_by_side or closing >= cfg.closing_thresh
    # side-by-side is folded into intensity BEFORE the position weight, so a
    # backmarker dice can't outscore the lead fight on the sbs bonus alone.
    intensity = closeness + (cfg.side_by_side_bonus if side_by_side else 0.0)
    pos_weight = 1.0 + cfg.pos_k / max(a.position, 1)         # P1 fight >> midfield
    stakes = 1.0 + (cfg.podium_bonus if a.position <= 3 else 0.0)
    # The chequer counts as the last lap: a pair still to cross is racing for the line.
    if snap.session.is_last_lap or snap.session.is_checkered:
        stakes *= cfg.last_lap_mult
    momentum = min(cfg.momentum_cap, closing * cfg.momentum_w)
    score = intensity * pos_weight * stakes + momentum
    return score if active else score * cfg.stable_damp


def _append_tour(out: list, snap: WorldSnapshot, cfg: DirectorConfig,
                 hot: dict[int, tuple[float, float, bool]] | None) -> None:
    """The baseline for a session with NO RUNNING ORDER: tour the field (#49).

    There is no leader in practice or qualifying. `order[0]` is whoever is quickest or
    merely furthest around the road, and following it made the camera camp: measured over
    120 live cuts at Spa, 59 were LEADER shots and two cars took 39 of them while fourteen
    were running. So every car that is actually running is a candidate here, all at the
    same base score. Rank is deliberately NOT weighted: treating a lap-time rank as a
    leader is the same mistake in a new place, and it is the one the viewer complained
    about.

    Equal scores mean no shot can ever steal another (cut_margin sees to that), so the
    rotation is driven entirely by the machinery that already exists: `max_shot` retires
    a shot on the variety timer and `_fatigue` docks the cars we have just shown, so the
    pick lands on somebody new. What CAN steal is a driver who has just improved their own
    best: the one thing in a practice session that is actually worth cutting to.

    `hot` is the Director's latch of recent personal bests (idx -> (when, lap, took the
    session best)). None means "no elevation", which is the pure-function behaviour: a
    flat tour is a complete answer, and a first-ever call has watched nobody set a lap.

    The lap in progress counts too, and counts more: `CarState.pace` is the world's
    sector-by-sector reading of the lap each car is on, and a car on the session's best
    pace is elevated from the first boundary it clears rather than from the line (the
    time is the payoff; the lap is the story). The latch above then carries the shot
    through the result. See `Director._on_quick_lap` for the hold.
    """
    t = snap.session_time
    for idx in snap.order:                 # the running set: the world model keeps a car in
        c = snap.cars.get(idx)             # the garage out of `order` entirely (#46)
        if c is None or c.in_garage or _in_pits(c):
            continue
        label = f"#{c.number} {c.name or ''}".rstrip()
        score = cfg.follow_base
        h = (hot or {}).get(idx)
        if h is not None and t - h[0] <= cfg.follow_hot_window:
            _when, lap, took_session = h
            score += cfg.follow_hot_bonus + (cfg.follow_best_bonus if took_session else 0.0)
            label += f"  {'session best' if took_session else 'personal best'} {_lap_str(lap)}"
        elif c.pace is not None and not c.pace.done:
            if c.pace.on_session_pace(cfg.pace_margin):
                score += cfg.pace_bonus
                label += f"  on pace S{c.pace.sector} {c.pace.vs_best:+.2f}"
            elif c.pace.up_on_own:
                score += cfg.own_pace_bonus
                label += f"  up on own S{c.pace.sector} {c.pace.vs_own:+.2f}"
        # follow:{idx}, so the key is stable per SUBJECT the way leader:{idx} is: the
        # scorer compares keys to answer "same shot?", and a key that moved with the
        # rotation would make every tick look like a brand new shot.
        out.append((Shot(ShotKind.FOLLOW, subject_of(ShotKind.FOLLOW, idx), idx, label),
                    score, False))


def _to_line(c: CarState, snap: WorldSnapshot) -> float:
    """Seconds from this car to the start/finish line, along the track's own speed
    profile (CarIdxEstTime runs 0..est_lap and resets at the line, so the remainder is
    the time to get there). Without the ruler, a nominal lap scaled by lap distance."""
    if snap.est_lap and c.est_time is not None:
        return max(0.0, snap.est_lap - c.est_time)
    return (1.0 - c.lap_dist_pct) * (snap.est_lap or 100.0)


def _finish_shot(c: CarState, crossed: bool) -> Shot:
    what = "takes the flag" if crossed else "to the flag"
    return Shot(ShotKind.FINISH, subject_of(ShotKind.FINISH, c.idx), c.idx,
                f"#{c.number} {c.name or ''} {what}".replace("  ", " "))


def _append_finish(out: list, snap: WorldSnapshot, cfg: DirectorConfig,
                   finish: FinishTracker) -> None:
    """The finish, car by car: the chequer is out and the field is still arriving.

    The shot is the next car to the line, or one that has just crossed and gets a
    beat (`finish_linger`) before the camera moves on. The winner's own crossing is
    scored to take the camera (`flag_base`); every later one is scored like a lone
    leader, weighted by place, so a live fight for the line (a BATTLE candidate, still
    scored under the chequer for a pair that has not crossed) outranks it and the
    camera is on that pair as they arrive. When everyone is home the winner's lap is
    the picture while it lasts, then whoever is still out there.
    """
    t, order, cars = snap.session_time, snap.order, snap.cars
    running = [i for i in order
               if (c := cars.get(i)) is not None
               and not c.in_garage and not _in_pits(c) and not c.towed]
    nxt = next((i for i in running
                if not finish.finished(i) or finish.crossed_within(i, t, cfg.finish_linger)),
               None)
    if nxt is not None:
        c = cars[nxt]
        crossed = finish.finished(nxt)
        if nxt == order[0]:
            # the winner: the flag itself takes the camera, the run-up does not
            near = crossed or _to_line(c, snap) <= cfg.flag_lead
            score = cfg.flag_base if near else cfg.leader_base
        else:
            score = cfg.finish_base * (1.0 + cfg.pos_k / max(c.position, 1))
        out.append((_finish_shot(c, crossed), score, False))
        return
    if running:
        c = cars[running[0]]
        out.append((Shot(ShotKind.LEADER, subject_of(ShotKind.LEADER, c.idx), c.idx,
                         f"#{c.number} {c.name or ''}".rstrip(), flavor=ShotFlavor.SOLO),
                    cfg.leader_solo_base, False))


@dataclass
class PitVisit:
    """One car's trip through the lane, as the director tracks it (see _track_pits).

    Opened by a PIT_ENTRY (a car that DROVE in; a reset opens nothing), and closed
    by the exit, after which `t_out` holds the time and the car is followed out for
    `pit_rejoin_hold`. `pos_in` is where the car was running as it came in: the stop's
    weight, because a leader's stop matters however far back the lane drops it."""

    t_in: float
    pos_in: int
    t_out: float | None = None


def _append_pits(out: list, snap: WorldSnapshot, cfg: DirectorConfig,
                 pits: dict[int, PitVisit]) -> None:
    """A stop while it lasts, then the rejoin: two shots, because the lane cameras
    cannot see the road the car merges into and only a second cut gets the camera
    there. The stop is `ShotKind.PIT`; the way out is a FOLLOW with the REJOIN flavor,
    keyed as the plain follow so the shot's subject is still just that car.

    Scored by the place the car came in FROM, weighted like a battle (1 + pos_k /
    position): the lane drops a car down the order as it goes, and the stop of a
    leader is not a P8 stop by the time it reaches its box. The rejoin keeps the same
    score, so it is the natural next shot when the stop ends and nothing has changed.
    """
    for idx, v in pits.items():
        c = snap.cars.get(idx)
        if c is None or c.towed:
            continue
        pos = v.pos_in if v.pos_in > 0 else (c.position if c.position > 0 else 999)
        score = cfg.pit_base * (1.0 + cfg.pos_k / max(pos, 1))
        if pos <= 3:
            score += cfg.pit_podium_bonus
        who = f"#{c.number} {c.name or ''}".rstrip()
        if v.t_out is None:
            shot = Shot(ShotKind.PIT, subject_of(ShotKind.PIT, idx), idx,
                        f"{who} pits from P{pos}" if pos < 999 else f"{who} pits")
        else:
            now = f" P{c.position}" if c.position > 0 else ""
            shot = Shot(ShotKind.FOLLOW, subject_of(ShotKind.FOLLOW, idx), idx,
                        f"{who} rejoins{now}", flavor=ShotFlavor.REJOIN)
        out.append((shot, score, False))


def candidates(snap: WorldSnapshot, cfg: DirectorConfig,
               incidents: dict[int, tuple[str, float, str]] | None = None,
               shown: frozenset[str] | set[str] = frozenset(),
               hot: dict[int, tuple[float, float, bool]] | None = None,
               finish: FinishTracker | None = None,
               pits: dict[int, PitVisit] | None = None,
               ) -> list[tuple[Shot, float, bool]]:
    """Return (shot, score, is_interrupt) for every shot worth considering now.

    `incidents` is the Director's latch of recent incidents (idx -> (key, t0, detail));
    with None we fall back to this tick's events, which is the pure-function behaviour
    used by tests. `shown` holds interrupt shot keys that have already had the camera
    and must not grab it again (see Director._shown). `hot` is the latch of recent
    personal bests, used only outside a race (see _append_tour). `finish` is the
    Director's account of who has taken the flag; None (the tests' pure form) means
    the finish is not being tracked and the chequer ends the racing as it used to.
    `pits` is the Director's account of who is in the lane having driven there (see
    _track_pits); None means no stop is being tracked.
    """
    out: list[tuple[Shot, float, bool]] = []
    order, cars = snap.order, snap.cars
    if not order:
        return out
    flag_out = finish is not None and finish.flag_out

    # Is there a race on? `is_race_kind` is the same predicate the tower and the overlay
    # already gate on: one definition in world/model.py, so the camera, the graphics and
    # the voice cannot disagree about whether a race is happening.
    if not is_race_kind(snap.session.session_kind):
        _append_tour(out, snap, cfg, hot)
    elif flag_out:
        _append_finish(out, snap, cfg, finish)
    else:
        # baseline: follow the RACE LEADER, but the one actually racing, never a car
        # sitting in its pit box. A leader's stop is not "follow the leader", and camping on
        # a parked car is the "why are we watching the pits" failure. Fall through pitting
        # cars to the first on-track leader. The key encodes the target so the camera
        # FOLLOWS when the lead changes hands (a pit cycle or an on-track pass) instead of
        # freezing on the old car. Tag SOLO when that leader is running free (an in-car
        # angle looks best); the chaser gap uses the follower's on-track gap, not F2Time
        # (stale mid-lap).
        # ...nor a car on a flatbed: a towed car holds its place in the order until the
        # field passes it (CarState.towed), and there is nothing to point a camera at.
        lead_rank = next((r for r, i in enumerate(order)
                          if not _in_pits(cars[i]) and not cars[i].towed), None)
        if lead_rank is not None:
            lead = cars[order[lead_rank]]
            nxt = cars[order[lead_rank + 1]] if lead_rank + 1 < len(order) else None
            lead_chaser_gap = (nxt.track_gap_ahead
                               if (nxt is not None and not _in_pits(nxt) and not nxt.towed)
                               else None)
            solo = lead_chaser_gap is None or lead_chaser_gap > cfg.solo_gap
            out.append((
                Shot(ShotKind.LEADER, subject_of(ShotKind.LEADER, lead.idx), lead.idx,
                     f"#{lead.number} {lead.name or ''}".rstrip(),
                     flavor=ShotFlavor.SOLO if solo else ""),
                cfg.leader_solo_base if solo else cfg.leader_base, False,
            ))
            # The flag: on the last lap, the leader's last few seconds to the line are
            # the winner taking the chequer, and that shot is worth more than the same
            # car followed from the same camera it has had all race.
            if (finish is not None and snap.session.is_last_lap and lead_rank == 0
                    and _to_line(lead, snap) <= cfg.flag_lead):
                out.append((_finish_shot(lead, False), cfg.flag_base, False))

    # Pit stops: in a race, under green or yellow alike (a stop under the caution is
    # the strategy call of the afternoon), and never once the flag is out, when a car
    # in the lane is a finisher going home.
    if pits and is_race_kind(snap.session.session_kind) and not flag_out:
        _append_pits(out, snap, cfg, pits)

    # battles are only considered under green: a caution/parade lap has cars close
    # together going nowhere, which is not a fight. The chequer is the exception: a
    # pair that has not crossed yet is racing for the line, which is the finish.
    if not (snap.session.is_green or flag_out):
        _append_incidents(out, snap, cfg, incidents, shown)
        return out

    # battles: adjacent pairs within range (target whoever is behind ON TRACK, so the
    # camera is on the car with the move to make, or the one that just lost it).
    for rank in range(1, len(order)):
        a, b = cars[order[rank - 1]], cars[order[rank]]
        gap = b.track_gap_ahead
        if a.position <= 0 or b.position <= 0:
            continue
        # It's only a battle if they're racing for position on the SAME lap. A car being
        # lapped is ≈a full lap apart in progress, so track_gap_ahead is None for it and
        # it never registers here: i.e. the leader (or anyone) reeling in a backmarker
        # is deliberately NOT a battle. Belt-and-suspenders: a sub-second interval whose
        # lap counters differ by more than one is a timing artifact, not a nose-to-tail
        # pair straddling the S/F line.
        if gap is None or gap > cfg.battle_max_gap:
            continue
        if abs(a.lap_completed - b.lap_completed) > 1:
            continue
        # never a battle if either car is in the pits (pit road / stall / approach), nor
        # with a car that is out of the world (its gap is None anyway; belt and braces).
        if _in_pits(a) or _in_pits(b) or a.towed or b.towed:
            continue
        # a car that has taken the flag is not racing anyone any more
        if flag_out and (finish.finished(a.idx) or finish.finished(b.idx)):
            continue
        # different classes aren't racing each other for position (it's a lapping):
        # skip so multiclass traffic doesn't masquerade as a battle.
        if a.class_id is not None and b.class_id is not None and a.class_id != b.class_id:
            continue
        # overlapping/nose-to-tail pairs get the wheel-to-wheel angles (chase, nose)
        flavor = ShotFlavor.SIDE_BY_SIDE if gap < cfg.side_by_side_gap else ""
        # Order the pair by TRACK position, and target whichever car is BEHIND on the
        # road. Between the move and the line those are not the same as the timing
        # sheet, and the sheet's answer points the camera at the winner driving away:
        # the shot worth having is from the car that just lost the place, watching the
        # other one come back across. The pop-ins read this ordering too.
        front, back = track_order(a, b)
        # The key is pair identity, order-free. Keying it on the running order made a
        # swap look like a brand new shot, which retired the fight's fatigue history
        # and cut the camera to some other battle at the exact moment this one paid off.
        key = subject_of(ShotKind.BATTLE, back.idx, (a.idx, b.idx))
        shot = Shot(ShotKind.BATTLE, key, back.idx,
                    _battle_label(a, b, gap), (front.idx, back.idx), flavor=flavor)
        out.append((shot, _battle_score(a, b, gap, snap, cfg), False))

    _append_incidents(out, snap, cfg, incidents, shown)
    return out


def preempting(e: Event, cfg: DirectorConfig) -> bool:
    """Is this event an incident bad enough to take the camera off live racing?

    The gate is severity, classified once in the world model (see IncidentSeverity).
    Below the bar the incident is dropped from candidacy altogether: not demoted.
    An interrupt scores 9-12, so a "small" incident left in the normals lane would
    still out-score every battle and steal the camera the moment min_shot expired;
    that is exactly the trap `_shown` exists to close.
    """
    return e.kind == EventKind.INCIDENT and severity_at_least(e.severity,
                                                              cfg.incident_min_severity)


def incident_key(car_idx: int, t0: float) -> str:
    """Stable key for one incident occurrence. Stable is the point: keying on the
    *current* tick's time (as this used to) made the shot unrecognisable to the state
    machine a frame later, so its score read None and it was cut at min_shot on the dot."""
    return f"incident:{car_idx}:{t0:.1f}"


def _append_incidents(out, snap: WorldSnapshot, cfg: DirectorConfig,
                      incidents: dict[int, tuple[str, float, str]] | None = None,
                      shown: frozenset[str] | set[str] = frozenset()) -> None:
    cars = snap.cars
    if incidents is None:
        # No latch supplied: fall back to this tick's events only.
        incidents = {
            e.car_idx: (incident_key(e.car_idx, snap.session_time), snap.session_time,
                        e.detail or "incident")
            for e in snap.events if preempting(e, cfg)
        }

    # incidents: interrupt lane, ignoring backmarker offs
    for idx, (key, _t0, detail) in incidents.items():
        c = cars.get(idx)
        if c is None:
            continue
        pos = c.position if c.position > 0 else 999
        if pos > cfg.incident_max_pos:
            continue
        # Already had its moment: drop it entirely rather than leaving a 9-12 point
        # candidate sitting in the normals lane, where it would out-score every battle
        # and yank the camera straight back the moment min_shot expired.
        if key in shown:
            continue
        score = cfg.incident_base + (cfg.incident_leader_bonus if pos <= 3 else 0.0)
        shot = Shot(ShotKind.INCIDENT, key, idx, f"#{c.number} {detail} P{pos}")
        out.append((shot, score, True))


class Director:
    def __init__(self, cfg: DirectorConfig | None = None):
        self.cfg = cfg or DirectorConfig()
        # An optional `hold_shot(shot_key) -> seconds remaining` veto: something outside
        # the director asking for the camera to stay where it is a moment longer.
        # Injected rather than imported so the director stays a pure function of the
        # world in tests. None (the default, and what Pylon runs with) means nobody
        # is asking and the director cuts on its own cadence alone.
        self.talking_about = None
        self._hold_from: float | None = None   # session time the claim first blocked a cut
        self.current: Shot | None = None
        self.started = 0.0
        self.current_score = 0.0
        self._last_eval = -1e9
        self._interrupt_ok_at = -1e9
        self._last_end: dict[str, float] = {}  # shot key -> when we last cut away from it
        self._pair_ahead: dict[frozenset[int], int] = {}  # close pair -> which car is ahead ON TRACK
        self._pass_at: dict[frozenset[int], float] = {}   # close pair -> time of last lead change
        # close pair -> (car that has just edged ahead, when) while we wait out pass_confirm
        self._pass_pending: dict[frozenset[int], tuple[int, float]] = {}
        self._retarget_ok_at = -1e9   # rate limit on re-pointing at a pass (see update)
        self._slow_since: dict[int, float] = {}  # car idx -> when it started crawling (trouble)
        self._fast_at: dict[int, float] = {}     # car idx -> last time it was at racing pace
        self._trouble: set[int] = set()          # cars latched "in trouble" until they recover
        # Recent incidents, latched so the shot stays scorable for cfg.incident_hold:
        #   car idx -> (stable shot key, when it happened, detail)
        self._incident_at: dict[int, tuple[str, float, str]] = {}
        # Interrupt shot keys (incident:* / trouble:*) that have already had the camera
        # and been cut away from. They are dropped from candidacy entirely: an interrupt
        # scores 9-12, so leaving one in the running meant it re-won the normals lane on
        # the very next evaluation and the broadcast ping-ponged back to it forever.
        self._shown: set[str] = set()
        # Personal bests, for the non-race tour (#49). A lap is a one-frame event like an
        # incident, so it is latched to stay a scorable candidate:
        #   car idx -> (when, the lap, whether it took the session best)
        self._hot: dict[int, tuple[float, float, bool]] = {}
        self._best: dict[int, float] = {}       # best lap seen per car
        self._session_best: float | None = None
        self._finish = FinishTracker()          # who has taken the flag (world.finish)
        # Cars in the pit lane having DRIVEN there, and cars just out of it, by index
        # (see _track_pits). What the PIT and REJOIN shots are scored from.
        self._pits: dict[int, PitVisit] = {}
        # Cars we have already laid eyes on. A time that was on the board the first time we
        # saw a car was not set in front of us, so it is adopted in silence: `pylon live`
        # rebuilds the director on every bridge connection, and without this a mid-session
        # reconnect would treat the entire field's existing times as fresh laps at once.
        self._seen: set[int] = set()

    @property
    def trouble(self) -> frozenset[int]:
        """Cars currently latched as stricken. Read-only view of a signal that exists
        nowhere else: no per-car damage channel is exposed over the SDK, so this is
        inferred here and the replay machine (#18) has to borrow it rather than
        re-derive it."""
        return frozenset(self._trouble)

    def trouble_since(self, idx: int) -> float | None:
        """Session time a stricken car was last at racing pace: the closest thing to
        WHEN it went wrong that the telemetry offers.

        The latch fires `trouble_confirm` after the car is already crawling, and the
        crawl itself began seconds after the hit, so the latch time is the aftermath,
        twice over. A replay that seeks to it shows a parked car in slow motion (on air,
        2026-09-14). The last frame above `trouble_recover_speed` is when the car was
        still a racing car, and everything worth seeing happens just after it. None for
        a car this director never saw at pace."""
        return self._fast_at.get(idx)

    def _fatigue(self, key: str, t: float) -> float:
        """Recency penalty (the DESIGN's `- fatigue` term): a shot we just left is
        docked, decaying over its tau, so variety spreads across the field's live
        battles instead of ping-ponging between the same one or two pairs.

        Battles and the non-race tour fatigue on the main curve. For the tour it is
        load-bearing rather than a nicety: every running car scores the same, so without a
        recency penalty the variety timer would hand the camera back to the same two cars
        forever, which is the bug (#49), just arriving by a different route.

        THE LEADER FATIGUES TOO, on its own shallower and faster curve. It used to be
        exempt outright, the reasoning being that it is the evergreen fallback and that a
        dredged-up stale midfield gap is worse than the front. That is true of a quiet
        race and false of a busy one: exempt, it was the only shot in the ranking that
        never got cheaper, so every time anything else's penalty bit, the ranking handed
        the camera back to the front, and the front is where the broadcast kept ending
        up, seconds after having just been there.

        Shallower and faster (amp 1.1 / tau 9s against 1.6 / 18s) is the whole design:
        it recovers to full value in well under a minute, so a race where nothing is
        happening still sits on the leader, and it cannot be the answer twice running
        while there is anything else worth watching.

        Incidents are a separate interrupt lane and do not fatigue at all: `_shown`
        handles them, by dropping them from candidacy entirely once they have had the
        camera, which is a stronger thing than a penalty."""
        if key.startswith("leader:"):
            amp, tau = self.cfg.leader_fatigue_amp, self.cfg.leader_fatigue_tau
        elif key.startswith(("battle:", "follow:")):
            amp, tau = self.cfg.fatigue_amp, self.cfg.fatigue_tau
        else:
            return 0.0
        last = self._last_end.get(key)
        if last is None:
            return 0.0
        return amp * math.exp(-(t - last) / tau)

    def _is_favourite(self, snap: WorldSnapshot, shot: Shot) -> bool:
        """Is one of the operator's cars the subject of this shot?

        Both cars of a battle count. `number` is the string iRacing reports, compared
        stripped, because a feed can pad it and a person never does.
        """
        idxs = list(shot.pair) if shot.pair else ([shot.target_idx]
                                                  if shot.target_idx is not None else [])
        for idx in idxs:
            car = snap.cars.get(idx)
            if car is not None and str(car.number or "").strip() in self.cfg.favourites:
                return True
        return False

    def _talking_hold(self) -> float:
        """Seconds an outside voice still wants on the CURRENT shot, capped.

        Pylon itself never asks, since `talking_about` is None and this is always 0.0, but
        the hook is what makes the director embeddable: anything that speaks over the
        pictures (a human commentator's push-to-talk, a caption engine, a bot) can ask
        the camera to stay a moment longer so the words land on the thing they describe.

        CAPPED by vo_hold_max, because this is an outside process telling the director
        what to do: a caller that wedged with a long duration written down must not be
        able to camp the camera. Zero whenever there is no hold to honour, which is
        also what every failure answers."""
        if self.current is None or self.talking_about is None:
            return 0.0
        return min(max(0.0, self.talking_about(self.current.key)), self.cfg.vo_hold_max)

    def _detect_passes(self, snap: WorldSnapshot, t: float) -> None:
        """Record a pass inside any physically-close pair, when it happens ON TRACK.

        This used to compare race positions, which flip as the pair crosses the line:
        on a long lap that is most of a minute after the viewer watched the move, so
        the camera re-pointed and the pop-ins turned over long after the event they
        were describing. Track order flips at the move itself.

        Proximity is gated on lap-distance (which stays tiny right through a pass, where
        a track-gap gate would blink the pair out mid-overtake). The flip then has to
        HOLD for pass_confirm before it is believed, so a pair running side by side
        through a corner does not register a pass every time a nose edges ahead."""
        order, cars = snap.order, snap.cars
        seen: dict[frozenset[int], int] = {}
        for r in range(1, len(order)):
            a, b = cars[order[r - 1]], cars[order[r]]
            if abs(a.progress - b.progress) >= self.cfg.pass_prox_laps:
                continue
            # A car in the lane is not racing this one for position. It is still close
            # enough in lap-distance to clear the gate above (the pit lane runs
            # alongside the track), so without this a stop registered as a pass, and
            # collected the pass bonus and the camera re-point that go with one.
            # Skipping also drops the pair from `seen`, so the state is forgotten and
            # the car rejoining is a first sighting rather than a second false pass.
            if _in_pits(a) or _in_pits(b):
                continue
            pair = frozenset((a.idx, b.idx))
            front, _back = track_order(a, b)          # a leads b on the timing sheet
            was = self._pair_ahead.get(pair)
            if was is None or front.idx == was:       # first sighting, or no change
                self._pass_pending.pop(pair, None)
                seen[pair] = front.idx if was is None else was
                continue
            pending = self._pass_pending.get(pair)
            if pending is None or pending[0] != front.idx:
                self._pass_pending[pair] = (front.idx, t)
                seen[pair] = was                      # a nose ahead, not yet a pass
            elif t - pending[1] >= self.cfg.pass_confirm:
                self._pass_pending.pop(pair, None)
                self._pass_at[pair] = t               # held long enough: the place changed
                seen[pair] = front.idx
            else:
                seen[pair] = was
        self._pair_ahead = seen
        self._pass_pending = {p: v for p, v in self._pass_pending.items() if p in seen}
        self._pass_at = {p: ts for p, ts in self._pass_at.items() if t - ts <= self.cfg.pass_window}

    def _passed_recently(self, shot: Shot | None, t: float) -> bool:
        if shot is None or not shot.pair:
            return False
        return t - self._pass_at.get(frozenset(shot.pair), -1e9) <= self.cfg.pass_window

    def _battle_active(self, shot: Shot | None, snap: WorldSnapshot, t: float) -> bool:
        """Is this battle still worth holding? A pass just happened, the pair is close
        enough to be a live fight (within battle_hold_gap), or still closing. The hold
        gap is wider than the wheel-to-wheel gap on purpose: once we're on a genuine
        battle we want to sit on it for laps, not bail the instant a closing burst eases."""
        if shot is None or not shot.pair:
            return False
        if self._passed_recently(shot, t):
            return True
        for idx in shot.pair:
            c = snap.cars.get(idx)
            if c is None or c.car_ahead_idx not in shot.pair or c.track_gap_ahead is None:
                continue  # want the trailing car of the pair (its car-ahead is the mate)
            if c.track_gap_ahead < self.cfg.battle_hold_gap:
                return True
            if (c.closing_rate or 0.0) >= self.cfg.closing_thresh:
                return True
        return False

    def _on_quick_lap(self, idx: int, snap: WorldSnapshot, t: float) -> bool:
        """Is this car's lap the reason we are on it? On the session's best pace at the
        last boundary, or across the line with a fresh best inside the latch window:
        the two halves of one story, and the shot is held through both. A lap that
        falls off the pace, or goes into the pits, ends it and the tour resumes."""
        c = snap.cars.get(idx)
        if c is None or _in_pits(c):
            return False
        h = self._hot.get(idx)
        if h is not None and t - h[0] <= self.cfg.follow_hot_window:
            return True
        p = c.pace
        return p is not None and not p.done and p.on_session_pace(self.cfg.pace_margin)

    def _track_hot_laps(self, snap: WorldSnapshot, t: float) -> None:
        """Watch for a driver improving their own best lap, and latch it.

        In a session with nothing to race for, this is the one thing that genuinely
        happens: somebody goes quicker than they have gone before, and sometimes quicker
        than anyone has. Read off `CarState.best_lap` dropping: the sim's own answer,
        already sentinel-free (None means no lap, never -1.0), so there is no second
        opinion about what a lap time is.

        A car's FIRST sighting adopts its time silently. We did not watch that lap; and on
        a mid-session reconnect (a fresh Director per bridge connection) every car in the
        field arrives with a time at once, which would otherwise read as the whole grid
        setting a personal best on the same frame.
        """
        for idx, c in snap.cars.items():
            first_sight = idx not in self._seen
            self._seen.add(idx)
            bl = c.best_lap
            if bl is None or bl <= 0.0:
                continue
            prev = self._best.get(idx)
            if prev is not None and bl >= prev - 1e-4:
                continue                       # same time, or slower: not an improvement
            self._best[idx] = bl
            took_session = self._session_best is None or bl < self._session_best - 1e-4
            if took_session:
                self._session_best = bl
            if not first_sight:
                self._hot[idx] = (t, bl, took_session)
        window = self.cfg.follow_hot_window
        self._hot = {i: v for i, v in self._hot.items() if t - v[0] <= window}

    def _detect_trouble(self, snap: WorldSnapshot, t: float) -> set[int]:
        """Cars crashed / stranded / spun (something worth staying with), latched
        until they recover or pit.

        No per-car damage channel exists over the SDK, so this infers it: a car
        essentially STOPPED, or slow AND off-track (beached in the gravel after a hit),
        while the field flies by under green. Narrow on purpose: a pit-lane brake or a
        crawling hairpin must not trip it. The latch survives one-frame speed wobble and,
        crucially, does NOT release just because the car sits off-track (a beached car is
        exactly what we want to hold): it releases only when the car drives away or pits."""
        cfg = self.cfg
        if not snap.session.is_green:
            self._slow_since.clear()
            for idx in self._trouble:
                self._shown.discard(f"trouble:{idx}")
            self._trouble.clear()
            return set()
        cars = snap.cars
        speeds = sorted(c.speed for c in cars.values() if c.on_track and c.speed > 1.0)
        median = speeds[len(speeds) // 2] if len(speeds) >= 3 else 0.0
        field_flying = median >= cfg.trouble_speed * 2  # a moving field, not a full-course crawl
        for idx, c in cars.items():
            if c.towed or c.in_garage:
                continue   # out of the world reads as stopped; it is not a car to stay with
            if c.speed > cfg.trouble_recover_speed:
                self._fast_at[idx] = t   # remember it was racing (so a grid start isn't 'trouble')
            if idx in self._trouble:
                if c.speed > cfg.trouble_recover_speed or c.on_pit_road:  # drove away / pitted
                    self._trouble.discard(idx)
                    self._shown.discard(f"trouble:{idx}")  # a fresh crash may preempt again
                    self._slow_since.pop(idx, None)
                continue
            was_racing = t - self._fast_at.get(idx, -1e9) <= cfg.trouble_recent
            stopped = c.speed < cfg.trouble_stopped_speed                       # parked / stalled
            off_and_slow = c.speed < cfg.trouble_speed and c.surface == TrackSurface.OFF_TRACK
            in_trouble = (field_flying and was_racing and not c.on_pit_road and c.position > 0
                          and (stopped or off_and_slow))
            if in_trouble:
                first = self._slow_since.setdefault(idx, t)
                if t - first >= cfg.trouble_confirm:
                    self._trouble.add(idx)
                    self._slow_since.pop(idx, None)
            else:
                self._slow_since.pop(idx, None)
        # forget any latched car that left the world (towed / garaged) or dropped out
        for idx in list(self._trouble):
            if idx not in cars or cars[idx].position <= 0 or cars[idx].towed:
                self._trouble.discard(idx)
                self._shown.discard(f"trouble:{idx}")
        return set(self._trouble)

    def _track_pits(self, snap: WorldSnapshot, t: float) -> None:
        """Latch every drive-in stop from the entry to a beat after the exit.

        Opened on PIT_ENTRY only. A PIT_RESET is a car that was put in the lane (a
        tow, an Escape), and there is nothing to watch: it closes any visit the car had
        open (a car that drove in and then reset to the garage is not still stopping).
        A visit is dropped once the car has been followed out for `pit_rejoin_hold`,
        once it has been in the lane for `pit_max_hold` (a car being rebuilt for a
        minute is a parked car, and the story has been told), or when the car leaves
        the world. The exit is taken from the event and, belt and braces, from the car
        no longer being in the lane, so a missed event cannot pin the shot."""
        cfg = self.cfg
        for e in snap.events:
            if e.kind == EventKind.PIT_ENTRY:
                c = snap.cars.get(e.car_idx)
                self._pits[e.car_idx] = PitVisit(t, c.position if c is not None else 0)
            elif e.kind == EventKind.PIT_RESET:
                self._pits.pop(e.car_idx, None)
            elif e.kind == EventKind.PIT_EXIT:
                v = self._pits.get(e.car_idx)
                if v is not None and v.t_out is None:
                    v.t_out = t
        for idx, v in list(self._pits.items()):
            c = snap.cars.get(idx)
            if c is None or c.towed:
                self._pits.pop(idx)
            elif v.t_out is None:
                if not _in_pits(c):
                    v.t_out = t
                elif t - v.t_in > cfg.pit_max_hold:
                    self._pits.pop(idx)
            elif t - v.t_out > cfg.pit_rejoin_hold:
                self._pits.pop(idx)

    def _track_incidents(self, snap: WorldSnapshot, t: float) -> None:
        """Latch this tick's incidents and expire old ones.

        An incident is derived from a one-frame surface change, so it appears in
        snap.events for a single tick. Latching it for cfg.incident_hold keeps a
        stably-keyed candidate alive long enough for the state machine to recognise
        the shot it is already on and let it breathe.

        Only incidents that clear the severity bar are latched at all: a lone car
        putting a wheel on the grass never becomes a candidate, so it can neither
        preempt nor linger."""
        for e in snap.events:
            if not preempting(e, self.cfg):
                continue
            prev = self._incident_at.get(e.car_idx)
            if prev is None or t - prev[1] > self.cfg.incident_hold:
                self._incident_at[e.car_idx] = (
                    incident_key(e.car_idx, t), t, e.detail or "incident")
        for idx, (key, t0, _detail) in list(self._incident_at.items()):
            if t - t0 > self.cfg.incident_hold:
                self._incident_at.pop(idx)
                self._shown.discard(key)

    def _reseat_clock(self, t: float) -> None:
        """Re-seat every session-time-keyed value onto the new clock (see TimeJump).

        Each field below is a "wall clock" reading in session-time terms, so a jump
        BACKWARDS puts all of them in the future at once. That is what stalled the
        director for twenty minutes live (issue #41): `held = t - self.started` went to
        about -1243s, which is under min_shot on every subsequent frame, so update()
        returned None forever and the sim camera sat where it last pointed. The dicts
        are worse than merely stale: an incident latched at t=1800 never expires
        against a clock reading t=557, because `t - t0 > incident_hold` is negative.

        The current SHOT is deliberately kept and its start re-seated rather than cut
        fresh. Cutting on every backwards blip would twitch the camera, and the hysteresis
        that stops it twitching is the whole game (DESIGN.md section 6). If the shot
        genuinely no longer exists in the new timeline, `cur_score is None` retires it on
        the normal path once min_shot has passed, which is exactly the intended behaviour.
        """
        self.started = t
        self._last_eval = -1e9
        self._interrupt_ok_at = -1e9
        self._retarget_ok_at = -1e9
        self._last_end.clear()
        self._pair_ahead.clear()
        self._pass_at.clear()
        self._pass_pending.clear()
        self._slow_since.clear()
        self._fast_at.clear()
        self._trouble.clear()
        self._incident_at.clear()
        self._shown.clear()
        # Lap times belong to the timeline they were set on: a SESSION jump starts a new
        # sheet, and a scrub means the bests we remember are from somewhere else on the
        # tape. Cleared together with `_seen`, so the next frame re-adopts the field's
        # times in silence instead of reading them all as fresh laps.
        self._hot.clear()
        self._best.clear()
        self._seen.clear()
        self._session_best = None
        self._finish.reset()
        self._pits.clear()

    def update(self, snap: WorldSnapshot) -> Decision | None:
        """Feed one snapshot. Returns a Decision when the shot changes, else None."""
        cfg, t = self.cfg, snap.session_time
        if snap.time_jump is not None:
            self._reseat_clock(t)
        self._detect_passes(snap, t)
        self._track_incidents(snap, t)
        self._track_hot_laps(snap, t)
        self._finish.update(snap, t)
        self._track_pits(snap, t)
        cands = candidates(snap, cfg, self._incident_at, self._shown, self._hot, self._finish,
                           self._pits)
        # The operator's favourites: a car someone asked to see is worth more than its
        # position alone says. Applied to every shot the car is IN, including the far
        # side of a battle, because "show me #64" means the fight it is having as much
        # as the car on its own. Never applied to an interrupt: an incident is already
        # the most important thing on track and does not need the help, and lifting one
        # would let a favourite's spin outrank a crash that stopped the race.
        if cfg.favourites:
            cands = [
                (s, sc + cfg.favourite_bonus, i)
                if (not i and self._is_favourite(snap, s)) else (s, sc, i)
                for (s, sc, i) in cands
            ]

        # reward pairs that just swapped places: an actual overtake outranks a static
        # gap up front, so the camera goes where the racing is (and holds for the pass).
        if self._pass_at:
            cands = [
                (s, sc + cfg.pass_bonus, i)
                if (s.kind == ShotKind.BATTLE and s.pair and frozenset(s.pair) in self._pass_at)
                else (s, sc, i)
                for (s, sc, i) in cands
            ]

        # a car in trouble (crashed / stranded / limping) is high drama: cut to it and
        # stay. It preempts like an incident the FIRST time we see it; it remains a
        # candidate while we are ON it (so the hold works), and is dropped once we cut
        # away, so a parked wreck can't yank the camera back for the rest of the race.
        for idx in self._detect_trouble(snap, t):
            c = snap.cars.get(idx)
            if c is None:
                continue
            key = f"trouble:{idx}"
            if key in self._shown and (self.current is None or self.current.key != key):
                continue
            shot = Shot(ShotKind.TROUBLE, key, idx, f"#{c.number} {c.name or ''}".rstrip())
            cands.append((shot, cfg.trouble_base, key not in self._shown))

        # Do not cut to an incident we were not already watching (see the knob's note
        # in config.py). Cutting live to a car that has already stopped shows only the
        # aftermath; the replay shows the run-up and the hit. An incident on a car we
        # are ALREADY on is live drama and stays, and so does the shot we are currently
        # holding: dropping that would cut away mid-interrupt, which is worse than
        # either behaviour on its own.
        if cfg.interrupt_only_if_on_camera:
            on_cam: set[int] = set()
            if self.current is not None:
                on_cam.add(self.current.target_idx)
                on_cam |= set(self.current.pair or ())
            cur_key = self.current.key if self.current is not None else None
            cands = [
                (s, sc, i) for (s, sc, i) in cands
                if s.kind not in (ShotKind.INCIDENT, ShotKind.TROUBLE)
                or s.target_idx in on_cam
                or s.key == cur_key
            ]

        cand_by_key = {s.key: (s, sc) for (s, sc, _) in cands}
        by_key = {k: sc for k, (_s, sc) in cand_by_key.items()}

        # 1) interrupt lane: incidents preempt, gated by cooldown
        interrupts = [(s, sc) for (s, sc, is_int) in cands if is_int]
        if interrupts and t >= self._interrupt_ok_at:
            s, sc = max(interrupts, key=lambda x: x[1])
            if self.current is None or s.key != self.current.key:
                self._interrupt_ok_at = t + cfg.interrupt_cooldown
                return self._cut(s, sc, t, "incident")

        # 2) a pass inside the battle we are ON: re-point to the car that just lost the
        # place, so its camera catches the winner coming back across in front of it.
        # Deliberately ahead of both min_shot and the decision cadence: the pass IS
        # the moment, and a shot that arrives a beat later has missed it. Rate-limited
        # so a pair swapping back and forth cannot ping-pong the camera.
        if self.current is not None and self.current.kind == ShotKind.BATTLE:
            fresh, fresh_sc = cand_by_key.get(self.current.key, (None, 0.0))
            if (fresh is not None and fresh.target_idx != self.current.target_idx
                    and self._passed_recently(fresh, t) and t >= self._retarget_ok_at):
                self._retarget_ok_at = t + cfg.pass_window
                return self._cut(fresh, fresh_sc, t, "pass")

        # 3) normal cadence
        if self.current is not None and t - self._last_eval < cfg.decision_interval:
            return None
        self._last_eval = t

        normals = [(s, sc) for (s, sc, is_int) in cands if not is_int]
        if not normals:
            return None
        # rank challengers by a fatigue-adjusted score so a recently-shown shot has to
        # be clearly better to win again; the current shot is scored raw (no penalty).
        best_s, best_sc = max(normals, key=lambda x: x[1] - self._fatigue(x[0].key, t))

        if self.current is None:
            return self._cut(best_s, best_sc, t, "opening")

        held = t - self.started
        cur_score = by_key.get(self.current.key)

        # The anti-flicker floor, and above it an outside claim on this shot. Both sit
        # BELOW the interrupt lane on purpose: a wreck still takes the camera mid-word,
        # because a line about a battle is not worth missing a crash for.
        #
        # The claim is bounded on OUR clock, not the caller's. The hook reports
        # seconds remaining and is re-read every tick, so a caller that asked for a long
        # duration and died answers "still talking" on every tick, and clamping that
        # answer changes nothing when all we ask is whether it is positive. So the hold
        # runs from the first tick it blocked a cut, for at most vo_hold_max.
        if self._talking_hold() > 0.0:
            if self._hold_from is None:
                self._hold_from = t
            talking = t - self._hold_from < cfg.vo_hold_max
        else:
            talking = False
            self._hold_from = None
        if held < cfg.min_shot or talking:
            if cur_score is not None:
                self.current_score = cur_score
            return None
        if cur_score is None:
            return self._cut(best_s, best_sc, t, "shot ended")
        self.current_score = cur_score

        # is the leader currently on camera AND running by itself? A lone leader is the
        # evergreen fallback, not "action": we never force a timed cut off it (that would
        # swing the camera to a boring static train just for variety) and we let a live
        # shot steal it cheaply below, so the moment a battle heats up we go there.
        cur_is_solo_leader = (
            self.current.kind == ShotKind.LEADER
            and any(s.kind == ShotKind.LEADER and s.flavor == ShotFlavor.SOLO
                    for (s, _sc, _i) in cands)
        )

        # "Sticky" shots are never cut on the variety TIMER: a LIVE battle holds for as
        # long as it stays live (entire laps if nothing better happens), a stricken car
        # is held while we 'stay with them', and a lone leader is the fallback we sit on
        # until real action appears. All three still yield to a clearly stronger shot
        # (the steal below); stale battles and the with-company leader rotate as before.
        is_battle = self.current.kind == ShotKind.BATTLE
        is_trouble = self.current.kind == ShotKind.TROUBLE
        is_live_battle = is_battle and self._battle_active(self.current, snap, t)
        # A finish shot ends itself (the car crosses and lingers out of candidacy) or is
        # stolen by a fight for the line; the variety timer must not swing the camera
        # off a car in the last seconds before the flag.
        is_finish = self.current.kind == ShotKind.FINISH
        # A car on a quick lap is held to the line and through the result: cutting off a
        # flying lap for variety is the practice-session version of leaving a fight.
        is_quick_lap = (self.current.kind == ShotKind.FOLLOW
                        and self._on_quick_lap(self.current.target_idx, snap, t))
        # A stop is held while it lasts: the candidate retires itself at the exit (and
        # at pit_max_hold), and the variety timer must not cut off a car in its box
        # halfway through the tyres. A stronger shot can still steal it.
        is_pit = self.current.kind == ShotKind.PIT
        sticky = (is_live_battle or cur_is_solo_leader or is_finish or is_quick_lap
                  or is_pit or (is_trouble and held < cfg.trouble_hold))
        max_hold = cfg.battle_max_shot if is_battle else cfg.max_shot
        # backstop so nothing camps forever (a stuck reading, or a parked wreck).
        backstop = (is_battle and held > cfg.battle_hard_max) or (is_trouble and held >= cfg.trouble_hold)
        if (held > max_hold and not sticky) or backstop:
            others = [(s, sc) for (s, sc) in normals if s.key != self.current.key]
            if others:
                s2, sc2 = max(others, key=lambda x: x[1] - self._fatigue(x[0].key, t))
                return self._cut(s2, sc2, t, "variety")

        cut_margin = cfg.solo_leader_cut_margin if cur_is_solo_leader else cfg.cut_margin
        if best_s.key != self.current.key and \
                best_sc - self._fatigue(best_s.key, t) > cur_score + cut_margin:
            return self._cut(best_s, best_sc, t, "stronger")
        return None

    def _cut(self, shot: Shot, score: float, t: float, reason: str) -> Decision:
        prev_held = (t - self.started) if self.current is not None else 0.0
        if self.current is not None:
            self._last_end[self.current.key] = t  # start this shot's fatigue clock
            # Leaving an interrupt shot (a wreck, an off): it has had its moment, so
            # retire the key. It stays latched (in _trouble / _incident_at) for as long
            # as the situation lasts, it just stops being a shot we can cut to again.
            if self.current.kind in (ShotKind.TROUBLE, ShotKind.INCIDENT):
                self._shown.add(self.current.key)
        # if the pair just swapped, override the angle hint to the look-back so the
        # completed move stays in frame (rear angle on the now-trailing, passed car).
        if shot.kind == ShotKind.BATTLE and self._passed_recently(shot, t):
            shot = replace(shot, flavor=ShotFlavor.PASS)
        self.current = shot
        self.started = t
        self.current_score = score
        self._hold_from = None
        return Decision(time=t, shot=shot, score=score, reason=reason, prev_held=prev_held)


def run(source, cfg: DirectorConfig | None = None) -> Iterator[Decision]:
    """Iterate a telemetry source through the world model + director; yield cuts."""
    d = Director(cfg)
    for snap in run_world(source):
        dec = d.update(snap)
        if dec is not None:
            yield dec
