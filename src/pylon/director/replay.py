"""The live-to-replay mode machine (DESIGN.md section 7, issue #18).

Marks moments worth revisiting, waits for a lull, seeks the sim's own tape, rolls the
moment from a fresh angle, and comes home. The director decides what is on camera
live; this decides when to stop showing live at all.

Two things here are not in the issue, and both came out of measuring the real sim.

**Where the isolation lives.** #18 frames replay-poisoning as a world-model problem
and offers "gate ingestion on IsReplayPlaying, or run a second throwaway WorldModel".
Neither is used. `drive_live` simply does not feed the live world model while a replay
is rolling: the frames describing the replayed moment never reach it, so there is no
gate to get wrong and no second model to leak into SessionMemory. Isolation by
construction beats isolation by discipline, and it covers the overlay and
the recorder too, which a fix inside the world model would not.

It also means we do NOT gate on `IsReplayPlaying`. WE issue the seek, so we know. The
flag round-trips Linux -> HWND_BROADCAST -> sim -> mmap -> LAN, and the frames arriving
during that window are replayed frames still flagged live: precisely the ones that
would do the damage. The flag is confirmation and a safety net for a HUMAN scrubbing
the sim box, not the gate.

**A replay costs nothing on a tape.** #18's budget language ("cap replay length so we
don't miss live developments") assumes the sim is spectating a live session, where the
race carries on without us. Broadcasting a saved endurance replay (this project's
other production path, DESIGN section 14), the tape does not advance while we are
away, so an excursion misses NOTHING; it only puts the broadcast a few seconds further
behind a timeline nobody can see. Hence `source_is_tape`, and caps that differ by an
order of magnitude between the two.

**What we cannot do:** abort a rolling replay because something big happened live.
While the sim is in replay, telemetry describes the replayed moment, so the live race
is unobservable by construction: there is no channel that reports it. #18 lists
handling that case; the honest answer is that it cannot be detected, only made
unlikely, by keeping replays short. On a tape it cannot happen at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..world import (
    EventKind,
    IncidentSeverity,
    TimeJump,
    WorldSnapshot,
    car_in_pits,
    is_race_kind,
    severity_at_least,
)
from .model import ShotKind


@dataclass(frozen=True)
class ReplayMove:
    """One thing the machine wants the tape to do.

    Pure intent, deliberately: the director never builds camera commands, that is the
    Actuator's job, and it is why the entire brain is testable with no camera, no sim
    and no Win32 anywhere near it. `show/live.py` translates these into CamCommands
    the same way it translates a Shot.

    (It is also what keeps the import graph acyclic: show/live.py imports both the
    director and camera/, and neither imports it back. The shot vocabulary the two
    share is show/contract.py, which imports nothing.)
    """

    kind: str                    # "seek" | "to_end" | "speed"
    session_num: int = 0
    session_time: float = 0.0    # seek: where to, in session SECONDS
    speed: int = 1               # speed: 1 real time, 0 pause, negative rewind
    slow_motion: bool = False
    label: str = ""


# How close to where live was an excursion may run (tape seconds). `arrived` treats
# anything within this of `return_to` as home, so the tape must never be shown up to it
# from the far side; and the outcome has to fit under it before a replay is worth rolling.
LIVE_MARGIN = 0.5

# How far the live session clock may run ahead of the tape before the excursion knows
# it is inside a LIVE session (tape seconds). Spectating live, the tape sits ~1s behind
# SessionTime at the live edge; a seek puts it lead_in (6s) or more behind.
LIVE_CLOCK_GAP = 3.0


def tape_time(frame) -> float:
    """Where the sim's tape is, in session seconds.

    On a saved tape SessionTime IS the tape, and every replay test ever run was on one.
    Spectating a LIVE session it is not: the sim is in replay mode even at the live edge
    (IsReplayPlaying stays true, ~1s behind), SessionTime is the server's clock and
    keeps running while the tape goes wherever we sent it, and only ReplaySessionTime
    says where that is. Measured on air 2026-09-20: a seek that the sim honoured in
    0.1s never moved SessionTime, `left` never went true, and `settle` abandoned the
    excursion with the tape parked 13s in the past and nobody sending it home.
    """
    if frame.get("IsReplayPlaying"):
        rst = frame.get("ReplaySessionTime")
        if rst is not None:
            return float(rst)
    return frame.session_time


class ReplayState:
    IDLE = "idle"            # nothing worth showing
    ARMED = "armed"          # a moment is marked, waiting for a lull (and for VO, #19)
    ROLLING = "rolling"      # the tape is elsewhere and on air
    RETURNING = "returning"  # asked to come home, waiting for the pictures to agree


@dataclass(frozen=True)
class ReplayConfig:
    # OFF by default. Everything else in the director only chooses between live
    # pictures; this one takes the broadcast somewhere else entirely, so it is opt-in.
    enabled: bool = False

    # What earns a replay. Higher bar than an interrupt: the director already cuts to
    # incidents, and a replay interrupts the RACE rather than the current shot.
    min_severity: str = IncidentSeverity.MAJOR
    # How far back a moment can be and still deserve a replay. Absolute floor OR a
    # fraction of the field, whichever reaches deeper, because a flat 20 means
    # something quite different in a 20-car sprint (the whole grid) and a 57-car
    # endurance race (the top third, with a huge pile-up at P25 silently ignored).
    max_pos: int = 20
    max_pos_frac: float = 0.6
    lead_change_only_front: int = 3   # a "pass for the lead" counts inside the top this many
    # How long after a car has been in the pit lane a change of lead involving it is
    # still a STOP rather than a pass. Checking "is it in the pits right now" is not
    # enough, and that is the bug this exists for: the order settles over the whole
    # pit cycle, and by the time it does the car is back on track with the flag clear,
    # so the instant-in-time check waves the phantom lead change straight through.
    # Generous on purpose: a car that has just stopped cannot genuinely retake the
    # lead inside half a minute, so there is no real moment for this to swallow.
    pit_grace: float = 30.0

    # Ramp to slow motion AT the moment: run up to it at real speed so the viewer reads
    # the closing rate, then drop to half for the hit itself.
    #
    # OFF by default since Round 1 (2026-09-20). The sim plays no audio in slow motion,
    # and the rate is not the "half speed" it was tuned as: speed=2 measured 0.49x in
    # July and 0.34x on air, so the 2.5 tape seconds around the hit ran for 7.3 wall
    # seconds of silent slow motion in every replay. Real speed keeps the engine note
    # and the replay to its 10 seconds. `--replay-slow-mo` turns it back on.
    #
    # Measured on the rig, because `slow_motion` DIVIDES by `speed` and the mapping is
    # not obvious: speed=1 is 0.74x (barely slow), 2 is 0.49x, 3 is 0.37x, 4 is 0.29x.
    # So 2 is the half speed a replay wants, and 1 is nearly pointless.
    slow_at_moment: bool = False
    slow_speed: int = 2          # with slow_motion=True, ~0.5x
    slow_lead: float = 1.0       # start slowing this long BEFORE the marked instant,
                                 # so the drop lands on the approach, not the aftermath

    # Timing. The excursion is laid out on the TAPE, around the moment:
    #
    #     at - lead_in ............ at - slow_lead ..... at ..... at + slow_after ..... end
    #     |<-- run-up, real speed -->|<-- slow motion, through the hit -->|<-- outcome -->|
    #
    # lead_in/lead_out frame the moment rather than starting on top of it: a replay
    # that opens at the impact has already missed the cause, and one that leaves at the
    # impact has missed the point. It used to be a wall-clock `roll` from the seek
    # landing, and the slow-motion ramp ate the end of it: measured on air 2026-09-14,
    # the tape came home 0.5s past the mark, every time: slow motion of a car
    # arriving at its accident, then live, and the viewer never saw what happened.
    # Tape seconds, so the content is the same whatever rate the sim actually plays
    # slow motion at (it varies: 0.33x and 0.49x both measured for the same setting).
    lead_in: float = 6.0         # seconds of tape BEFORE the moment
    slow_after: float = 1.5      # slow motion continues this long PAST the moment
    lead_out: float = 2.5        # then real speed for this long: the outcome
    roll: float = 20.0           # WALL seconds: the excursion ends here whatever the tape
                                 # says. A backstop for a tape that does not advance, not
                                 # the length of a replay.
    settle: float = 1.5          # grace for the seek to actually land before we count
                                 # the roll: measured, not assumed: see pylon replay-probe

    # Caps and pacing. The tape case is generous because an excursion is genuinely
    # free there; the live case is not.
    cooldown: float = 90.0       # seconds between replays, so they stay an event
    cooldown_tape: float = 45.0
    max_age: float = 120.0       # a moment older than this is not "instant" any more
    hold_for_lull: float = 25.0  # how long an armed candidate waits for a safe gap
    return_timeout: float = 6.0  # give up waiting for the pictures and re-issue home

    # What counts as a lull. Deliberately narrow: never cut away from a live fight.
    allow_under_yellow: bool = True
    allow_on_solo_leader: bool = True
    # ...and never from a shot the director has only just cut to. The lull test asks
    # what KIND of shot is up, not how long it has been up, and on air (Round 1) the
    # director cut to a car leaving the pits, the graphics named the car, and
    # the replay rolled over the top of him a second later. A fresh shot gets this long
    # to be a shot before it can be left; a yellow needs no such grace.
    shot_settle: float = 4.0

    # ...but a wreck is the exception, and without this the best replays never fire.
    # A crash makes the director cut to TROUBLE and hold it, so the shot that PROVES a
    # replay is warranted was also the shot forbidding it, and the candidate aged out
    # every time: measured over one Watkins run, 101 consecutive refusals on "shot is
    # trouble" and not one wreck replay in the whole session (#66).
    #
    # Leaving is safe here for a reason that does not apply to a battle at the front: a
    # stricken car is a picture that has already finished developing, and holding on
    # stationary wreckage is worse television than showing how it happened.
    allow_on_own_trouble: bool = True
    wreck_dwell: float = 5.0     # show it live this long first, so the replay follows
                                 # the moment rather than racing it

    # "Contact" is the world model's word for two cars leaving the road together
    # (world/incidents: within INCIDENT_PAIR_WINDOW and INCIDENT_PAIR_M). That is the
    # right trigger for a LIVE cut, which has to be instant, but it is also what two
    # cars running wide together at the same kerb look like, and a slow-motion replay
    # of that is a replay of nothing. The replay waits `wreck_dwell` anyway, so by the
    # time it could roll there is hindsight to use: if neither car is in trouble and
    # neither has lost a place since, nothing came of it and it is dropped.
    contact_needs_consequence: bool = True

    # Outside a race, a stricken car is not a replay unless it LEFT THE ROAD AT PACE
    # first. The trouble latch reads "stopped while the field flies by", which in a
    # race is a crash and in practice is mostly a driver pulling over to reset: 24
    # replays in Round 1's practice, one every cooldown, of cars slowing to a halt
    # (the user: "stopping / resetting was triggering it"). A spin or a trip into the
    # gravel goes off at racing pace and still qualifies; a driver rolling onto the
    # grass at walking pace to press reset does not. Contact is already confirmed by
    # its second car and is not gated here.
    confirm_trouble_outside_race: bool = True
    crash_pace: float = 22.0     # m/s: off the road faster than this is a crash, not a
                                 # park. The director's trouble_recover_speed.

    # A fight AT THE FRONT outranks any replay. A midfield scrap does not outrank a
    # huge wreck, and treating every battle as an absolute veto meant wrecks were never
    # replayed at all: measured over five minutes of a 57-car restart at Watkins, 26 of
    # 29 shots were BATTLE and exactly ONE was LEADER, so the only window the old rule
    # allowed came round far less often than `hold_for_lull`. A big crash aged out
    # un-replayed on air.
    battle_veto_pos: int = 6     # a battle this far forward is still absolute
    # ...and a wreck is worth waiting longer for than a pass is. Same reason: in a busy
    # field the window is rare, and the whole point of a replay is the big moment.
    hold_for_lull_wreck: float = 60.0



@dataclass
class ReplayCandidate:
    """A marked moment, with the context a replay of it will need."""

    # Session time of the moment ITSELF, which is not when we noticed it. Contact is
    # recognised when the second car leaves the road; a stricken car is recognised
    # once it has been crawling for a while. Both are the aftermath. The tape is
    # sought relative to this, so it has to be the event: see Event.at and
    # Director.trouble_since for where each kind gets it.
    at: float
    session_num: int
    car_idx: int
    car_number: str
    driver: str
    kind: str                 # "contact" | "trouble" | "lead_change"
    label: str
    position: int
    other_idx: int | None = None
    other_number: str = ""
    other_driver: str = ""
    other_position: int = 0
    seen_on_camera: bool = False   # was the director already showing it when it happened?
    marked_at: float = 0.0         # session time we NOTICED; >= at, by the detection lag

    @property
    def key(self) -> str:
        """Stable identity for this moment, so ANOTHER PROCESS can name it.

        Anything preparing graphics for a moment does so while the director decides when
        to roll, and they
        are different processes talking through shotlink, so "which moment" has to
        survive JSON. Same shape as ReplayDirector._seen, deliberately.
        """
        return f"{self.kind}:{self.car_idx}:{self.at:.1f}"

    @property
    def score(self) -> float:
        """Rank among competing candidates. Contact beats a stricken car beats a pass,
        the front of the field beats the back, and a moment we DID show live is docked
        hard rather than dropped: a replay of what the viewer just watched adds nothing,
        but it still beats having nothing to show."""
        base = {"contact": 10.0, "trouble": 7.0, "lead_change": 6.0}.get(self.kind, 4.0)
        front = 3.0 / max(self.position, 1)
        return base + front - (5.0 if self.seen_on_camera else 0.0)


@dataclass
class ReplayDirector:
    cfg: ReplayConfig = field(default_factory=ReplayConfig)
    # Is the sim playing a SAVED tape rather than spectating live? Decides both the
    # caps and, critically, how we come home (see camera/probe.home_commands).
    source_is_tape: bool | None = None

    state: str = ReplayState.IDLE
    candidate: ReplayCandidate | None = None
    armed_at: float = 0.0        # session time the candidate was armed
    return_to: float = 0.0       # where live was when we left, so we can come back
    return_session: int = 0
    left: bool = False           # has the tape actually moved yet? (the seek may be ignored)
    live_clock: bool = False     # SessionTime kept running while the tape was in the past:
                                 # a live session, whatever IsReplayPlaying says, and the
                                 # way home is the live edge, not where the clock was
    slowed: bool = False         # has the slow-motion ramp already fired this excursion?
    unslowed: bool = False       # ...and has it come back out, for the outcome?
    started_wall: float = 0.0    # monotonic, when the seek went out
    rolled_from: float = 0.0     # monotonic, when the replay pictures actually began
    returning_from: float = 0.0  # monotonic, when we asked to come home
    last_replay_end: float = -1e9
    replays: int = 0
    failed_seeks: int = 0        # seeks the sim never honoured
    dropped_contacts: int = 0    # contacts that cost nobody anything (see the config)
    _last_leader: int | None = None
    _seen: set[str] = field(default_factory=set)
    # car idx -> session time it was last seen in the pit lane. See cfg.pit_grace.
    _pit_at: dict[int, float] = field(default_factory=dict)
    # car idx -> session time it last left the road at cfg.crash_pace or better. See
    # cfg.confirm_trouble_outside_race.
    _off_at: dict[int, float] = field(default_factory=dict)

    #: observation (live frames only) -------------------------------------
    def observe(self, snap: WorldSnapshot, current_shot) -> None:
        """Watch live frames for moments worth revisiting. Never emits commands."""
        if not self.cfg.enabled:
            return
        # Somebody moved the tape underneath us: a human scrubbing the sim box, or a
        # restart of the same replay from the top. `_seen` is keyed on SESSION TIME, so
        # replaying a stretch we have already watched would match every key we already
        # fired and silently skip every moment in it: the second pass over a race
        # would produce no replays at all, and look exactly like the feature being
        # broken. Our OWN excursions never reach here (those frames never touch the
        # world model, which is the isolation in #18), so this only ever fires for a
        # jump we did not cause.
        if snap.time_jump in (TimeJump.BACK, TimeJump.SESSION):
            self.reset_connection()
            self._reseat_clock()
        # Who is in the lane, stamped every tick. ABOVE the state gate on purpose: a car
        # can pit while a replay is armed or rolling, and if those frames are skipped the
        # stamp is stale exactly when the order is about to settle around that stop.
        for idx, car in snap.cars.items():
            if car_in_pits(car):
                self._pit_at[idx] = snap.session_time
        # ...and who has just left the road at pace, for the same reason: the off comes
        # BEFORE the stop it leads to, so it has to be on record by the time the
        # director's latch fires, whatever state this machine was in when it happened.
        for e in snap.events:
            if e.kind != EventKind.OFF_TRACK:
                continue
            c = snap.cars.get(e.car_idx)
            if c is not None and c.speed >= self.cfg.crash_pace:
                self._off_at[e.car_idx] = snap.session_time
        if self.state != ReplayState.IDLE:
            # The leader too, for the same reason: `_moments` compares against the
            # last leader IT saw, and it is not called while a replay is armed or
            # rolling. A pass in that stretch was "discovered" on the first idle frame
            # afterwards and marked at THAT moment, so the replay sought to a spot on
            # the tape where nothing happens. Reproduced: a pass at t=102 during an
            # armed wreck that aged out, a lead-change replay armed at t=171.
            self._last_leader = snap.order[0] if snap.order else None
            return
        on_cam = {current_shot.target_idx} if current_shot is not None else set()
        if current_shot is not None and current_shot.pair:
            on_cam |= set(current_shot.pair)

        for cand in self._moments(snap, on_cam):
            key = f"{cand.kind}:{cand.car_idx}:{cand.at:.1f}"
            if key in self._seen:
                continue
            self._seen.add(key)
            if self.candidate is None or cand.score > self.candidate.score:
                self.candidate = cand
                self.armed_at = snap.session_time
                self.state = ReplayState.ARMED

    def _pitted_recently(self, car, t: float) -> bool:
        """Is this car in the pits, or was it inside cfg.pit_grace ago?

        The `or` is what makes it work. A car is only flagged in the lane for part of a
        stop, while the running order goes on rearranging itself around that stop for
        the whole cycle, so "in the pits right now" answers no at precisely the moment
        the phantom lead change lands.
        """
        return car_in_pits(car) or (t - self._pit_at.get(car.idx, -1e9)) <= self.cfg.pit_grace

    def _moments(self, snap: WorldSnapshot, on_cam: set[int]):
        cars = snap.cars
        for e in snap.events:
            if e.kind != EventKind.INCIDENT:
                continue
            if not severity_at_least(e.severity, self.cfg.min_severity):
                continue
            c = cars.get(e.car_idx)
            if c is None or c.position <= 0 or c.position > self._max_pos(snap):
                continue
            other = cars.get(e.other_idx) if e.other_idx is not None else None
            yield ReplayCandidate(
                at=(e.at if e.at is not None else snap.session_time),
                session_num=snap.session.session_num or 0,
                car_idx=c.idx, car_number=c.number, driver=c.name or "",
                kind="contact", label=e.detail or "contact", position=c.position,
                other_idx=(other.idx if other else None),
                other_number=(other.number if other else ""),
                other_driver=((other.name or "") if other else ""),
                other_position=(other.position if other else 0),
                seen_on_camera=e.car_idx in on_cam,
                marked_at=snap.session_time,
            )

        # a change of race leader: the one pass that is always worth a second look
        #
        # ...but only once the field is actually racing. Cars really do change places
        # forming up on the grid and behind the pace car, and every one of those read as
        # a lead change worth replaying: five of them during the gridding at Watkins
        # before this gate existed. Same fault as #63, one lane over, and
        # the same predicate fixes it: `is_green` covers the parade laps and every
        # neutralisation, `field_released` covers a start held with the field already in
        # RACING.
        #
        # `_last_leader` is still updated below whatever happens, and that matters: skip
        # the bookkeeping during the formation and the first comparison after green
        # would be against a pre-grid leader and fire a bogus replay at the worst
        # possible moment. Detection is untouched, only the CANDIDATE is suppressed.
        # And only in a RACE. Qualifying and practice have an order too (by best lap),
        # and its "leader" changes every time somebody goes quicker: a lead-change
        # replay of a car crossing the line was armed twice in Round 1's qualifying,
        # saved from air only by the cooldown bug that was blocking every replay.
        leader = snap.order[0] if snap.order else None
        if (leader is not None and self._last_leader is not None
                and leader != self._last_leader
                and is_race_kind(snap.session.session_kind)
                and snap.session.is_green and snap.session.field_released):
            c, prev = cars.get(leader), cars.get(self._last_leader)
            # ...and only when the lead actually changed hands ON TRACK. A leader
            # peeling into the pit lane hands the lead to whoever is behind without
            # anybody overtaking anybody, and this fired on it: an instant replay, in
            # slow motion, of a routine stop.
            #
            # RECENTLY, not right now, and that distinction is the whole fix. Checking
            # the pit flag at the instant `order[0]` changes catches the stop itself and
            # misses the rest of the cycle: the order goes on settling while the car
            # rejoins, and by the time it flips back the flag is long clear, so a second
            # phantom lead change sailed through the instant check. Reported live at
            # Watkins after the first version of this guard was already deployed.
            #
            # Both cars, because a stop produces the phantom in both directions: the
            # car that stopped is the one losing the lead going in and the one taking it
            # back on the way out.
            #
            # A stop is not merely a weak replay, it is the wrong one, so it is
            # dropped from candidacy rather than scored lower, the same lesson `_shown`
            # and the incident tiers already taught us.
            stopped = any(self._pitted_recently(x, snap.session_time)
                          for x in (c, prev) if x is not None)
            if c is not None and not stopped \
                    and c.position <= self.cfg.lead_change_only_front:
                yield ReplayCandidate(
                    at=snap.session_time, session_num=snap.session.session_num or 0,
                    car_idx=c.idx, car_number=c.number, driver=c.name or "",
                    kind="lead_change", label="for the lead", position=c.position,
                    other_idx=(prev.idx if prev else None),
                    other_number=(prev.number if prev else ""),
                    other_driver=((prev.name or "") if prev else ""),
                    seen_on_camera=bool(on_cam & {leader, self._last_leader}),
                    marked_at=snap.session_time,
                )
        self._last_leader = leader

    # How far back a stricken car's "last at pace" may be and still be the moment. The
    # latch requires the car to have been racing within trouble_recent (5s) of the crawl
    # starting, plus trouble_confirm; anything older is a car that has been slow for
    # other reasons, and the frame is the safer mark.
    TROUBLE_LOOKBACK = 15.0

    def note_trouble(self, snap: WorldSnapshot, car_idx: int, on_cam: bool,
                     since: float | None = None) -> None:
        """A stricken car, from the director's own trouble latch (it infers damage the
        world model cannot see, so the signal only exists over there).

        `since` is when the car was last at racing pace (Director.trouble_since): the
        latch fires well into the aftermath, and a replay marked at the latch showed a
        parked car in slow motion. The moment is where the pace went."""
        if not self.cfg.enabled or self.state != ReplayState.IDLE:
            return
        c = snap.cars.get(car_idx)
        if c is None or c.position <= 0 or c.position > self._max_pos(snap):
            return
        t = snap.session_time
        # Outside a race the latch has to be corroborated by an off at pace (see the
        # config). Checked before `_seen` so it is asked again every frame the car
        # stays latched: cheap, and the off it needs is normally already on record.
        if (self.cfg.confirm_trouble_outside_race
                and not is_race_kind(snap.session.session_kind)
                and not (0.0 <= t - self._off_at.get(car_idx, -1e9) <= self.TROUBLE_LOOKBACK)):
            return
        key = f"trouble:{car_idx}"
        if key in self._seen:
            return
        self._seen.add(key)
        at = since if since is not None and 0.0 <= t - since <= self.TROUBLE_LOOKBACK else t
        cand = ReplayCandidate(
            at=at, session_num=snap.session.session_num or 0,
            car_idx=c.idx, car_number=c.number, driver=c.name or "", kind="trouble",
            label="in trouble", position=c.position, seen_on_camera=on_cam,
            marked_at=t)
        if self.candidate is None or cand.score > self.candidate.score:
            self.candidate, self.armed_at = cand, snap.session_time
            self.state = ReplayState.ARMED

    #: the lull ------------------------------------------------------------
    def _cooldown(self) -> float:
        return self.cfg.cooldown_tape if self.source_is_tape else self.cfg.cooldown

    def _max_pos(self, snap: WorldSnapshot) -> int:
        """How far back a moment still counts, for THIS field size."""
        return max(self.cfg.max_pos,
                   int(len(snap.order) * self.cfg.max_pos_frac))

    def _wreck_ready(self, snap: WorldSnapshot) -> bool:
        """Is the armed candidate a wreck that has had its moment live?"""
        return bool(self.cfg.allow_on_own_trouble
                    and self.candidate is not None
                    and self.candidate.kind in ("trouble", "contact")
                    and snap.session_time - self.armed_at >= self.cfg.wreck_dwell)

    def _hold_for_lull(self) -> float:
        """How long this candidate waits for a window. A wreck waits longer."""
        if self.candidate is not None and self.candidate.kind in ("trouble", "contact"):
            return max(self.cfg.hold_for_lull, self.cfg.hold_for_lull_wreck)
        return self.cfg.hold_for_lull

    def safe_now(self, snap: WorldSnapshot, current_shot,
                 shot_since: float | None = None) -> bool:
        """Is this a moment we can leave? Narrow by design: #18's rule is never to
        cut away from a developing live fight, and the cost of waiting is only that a
        replay is missed, while the cost of going early is stepping on the race.

        `shot_since` is the session time the current shot went up (Director.started);
        None means nobody said, and the settle gate is skipped."""
        t = snap.session_time
        if t - self.last_replay_end < self._cooldown():
            return False
        if self.cfg.allow_under_yellow and snap.session.is_yellow:
            return True
        if current_shot is None:
            return False
        if shot_since is not None and t - shot_since < self.cfg.shot_settle:
            return False                        # let the shot be a shot first
        # Never cut away from a fight at the FRONT. Midfield is negotiable, and it has
        # to be: with an absolute battle veto a 57-car field never offers a window.
        if current_shot.kind == ShotKind.BATTLE:
            front = min((c.position for c in
                         (snap.cars.get(i) for i in (current_shot.pair or ()))
                         if c is not None and c.position > 0), default=1)
            if front <= self.cfg.battle_veto_pos:
                return False
            # A scrap for P42 does not outrank a wreck. Anything else still does.
            return self._wreck_ready(snap)
        # Nor from a front-runner's pit stop: it is the strategy of the race being
        # decided, it lasts half a minute, and the replay will still be there after it.
        # The car's place is read off the board, which the lane is dropping it down, so
        # the veto is generous by a place or two, that is the right side to err on.
        if current_shot.kind == ShotKind.PIT:
            c = snap.cars.get(current_shot.target_idx)
            if c is not None and 0 < c.position <= self.cfg.battle_veto_pos:
                return False
            return self._wreck_ready(snap)
        # Showing SOMEONE ELSE's incident live is its own drama. Do not leave one wreck
        # to replay a different one.
        if (current_shot.kind in (ShotKind.TROUBLE, ShotKind.INCIDENT)
                and self.candidate is not None
                and current_shot.target_idx != self.candidate.car_idx):
            return False
        # The wreck exception (#66), now the ordinary path rather than a special case.
        # With `interrupt_only_if_on_camera` the director no longer cuts to an
        # off-camera incident at all, so a wreck candidate is normally armed while we
        # are on the leader or a follow shot, and making it wait for a LEADER lull
        # would just let the best replay of the race age out. Any non-battle shot will
        # do, once the moment has had its dwell.
        if self._wreck_ready(snap):
            return True
        # An interrupt shot IS the action; a battle may be about to pay off. The lone
        # leader is the director's own "nothing is happening" signal, so it is ours.
        if not self.cfg.allow_on_solo_leader:
            return False
        return current_shot.kind == ShotKind.LEADER

    #: driving -------------------------------------------------------------
    @property
    def rolling(self) -> bool:
        """True while the sim is showing the past. `drive_live` must not feed the live
        world model in this state, that is the whole isolation mechanism."""
        return self.state in (ReplayState.ROLLING, ReplayState.RETURNING)

    def maybe_start(self, snap: WorldSnapshot, current_shot,
                    now: float = 0.0, trouble: frozenset[int] = frozenset(),
                    tape_now: float | None = None,
                    shot_since: float | None = None) -> list[ReplayMove]:
        """On a live frame: start the replay if there is one and this is the moment.

        `trouble` is the director's latch (Director.trouble), lent for the contact
        verdict below; the replay machine never derives it itself. `tape_now` is
        where the tape is on this frame (`tape_time`), which is where "back where we
        were" means; without it the session clock stands in, as on a saved tape.
        `shot_since` is when the current shot went up, for `safe_now`.
        """
        if self.state != ReplayState.ARMED or self.candidate is None:
            return []
        t = snap.session_time
        if t - self.candidate.at > self.cfg.max_age:
            self._disarm()                      # stale: an "instant" replay has a shelf life
            return []
        # The outcome has to EXIST before it can be replayed. A lead change is marked
        # at the live edge, and rolling there means the excursion is capped at where
        # live was (tape_end), so it came home before the pass. Wrecks get this from
        # wreck_dwell already; this is the floor for everything.
        if t < self.candidate.at + self.cfg.slow_after + self.cfg.lead_out + LIVE_MARGIN:
            return []
        if not self.safe_now(snap, current_shot, shot_since=shot_since):
            if t - self.armed_at > self._hold_for_lull():
                self._disarm()                  # the lull never came; let it go
            return []
        # Did the contact actually cost anyone anything? Judged with hindsight, which
        # is only available once the moment has had its dwell, and a yellow or a lone
        # leader can make `safe_now` true before then, so wait for it here rather than
        # deciding on the first frame after the wheels left the road.
        if self.candidate.kind == "contact" and self.cfg.contact_needs_consequence:
            if t - self.armed_at < self.cfg.wreck_dwell:
                return []
            if not self._cost_someone(snap, trouble):
                self.dropped_contacts += 1
                self._disarm()
                return []
        return self.start(snap, now, tape_now=tape_now)

    def _cost_someone(self, snap: WorldSnapshot, trouble: frozenset[int]) -> bool:
        """Has either car in the armed contact suffered for it since the mark?

        Suffering is what the telemetry can see: latched as stricken by the director, or
        further back in the order than when it happened. Two cars that ran wide together
        and carried on have neither, and there is nothing to replay.
        """
        cand = self.candidate
        for idx, was in ((cand.car_idx, cand.position), (cand.other_idx, cand.other_position)):
            if idx is None:
                continue
            if idx in trouble:
                return True
            c = snap.cars.get(idx)
            if c is None or c.position <= 0:
                return True                     # gone from the running: worse than a place
            if was > 0 and c.position > was:
                return True
        return False

    def start(self, snap: WorldSnapshot, now: float = 0.0,
              tape_now: float | None = None) -> list[ReplayMove]:
        cand = self.candidate
        # A tape position, so `left` and `arrived` compare like with like. Spectating
        # live the tape is already ~1s behind the clock here; measured against the
        # clock that reads as "left" on the very frame we start, and the excursion
        # comes home before the seek has landed.
        self.return_to = snap.session_time if tape_now is None else tape_now
        self.return_session = snap.session.session_num or cand.session_num
        self.state = ReplayState.ROLLING
        self.left = False
        self.live_clock = False
        self.slowed = False
        self.unslowed = False
        self.started_wall = now
        self.replays += 1
        target = max(0.0, cand.at - self.cfg.lead_in)
        # SPEED FIRST, THEN SEEK. Not style: measured on the rig 2026-07-31, and
        # getting it backwards silently breaks every replay:
        #
        #   seek alone                    -> lands in 0.12s, exactly on target
        #   seek THEN speed immediately   -> NEVER MOVES BACK
        #   speed THEN seek               -> lands in 0.12s (also true for slow motion)
        #
        # A play-speed message arriving right behind a seek discards the seek that has
        # not been serviced yet. This cost a whole excursion every time: the tape stayed
        # live, `left` never went true, and `tick` disarmed the candidate at `settle`
        # as a failed seek, which reads as "the sim ignored us" and is how #42 looked
        # from a distance. `home()` already had this order and said why.
        return [
            ReplayMove("speed", speed=1, label="replay: real time"),
            ReplayMove("seek", session_num=cand.session_num, session_time=target,
                       label=f"replay: {cand.label} #{cand.car_number}"),
        ]

    def tape_end(self) -> float:
        """Session time the excursion is over: the outcome shown, or where live was,
        whichever the tape reaches first. Never past `return_to`: on a saved tape the
        tape WILL carry on past where we left, and showing the viewer the next few
        seconds inside the replay only to seek back and show them again live is not a
        replay, it is a stutter. (A lead change marked at the live edge is the case:
        there is no outcome to run on to.)"""
        cfg, cand = self.cfg, self.candidate
        return min(cand.at + cfg.slow_after + cfg.lead_out, self.return_to - LIVE_MARGIN)

    def tick(self, frame, now: float) -> list[ReplayMove]:
        """One frame while the excursion is in progress. These frames never reach the
        live world model, that IS the isolation.

        Where the tape is comes from session time, which is the only thing that says
        so, and the excursion is laid out on it (see ReplayConfig): the slow-motion
        ramp, the return to real speed for the outcome, and the end are all tape
        positions. `now` is a wall clock (time.monotonic), and it is only the backstop:
        a tape that does not advance (parked at the end, a speed the sim ignored) must
        still come home.
        """
        st = tape_time(frame)
        if frame.session_time - st > LIVE_CLOCK_GAP:
            self.live_clock = True
        if self.state == ReplayState.ROLLING:
            if not self.left:
                # Waiting for the seek to land. If it never does (an agent too old to
                # know the op, a sim that ignored it), we must not sit here on air
                # forever waiting for a picture that is not coming.
                if st < self.return_to - 1.0:
                    self.left = True
                    self.rolled_from = now
                elif now - self.started_wall > self.cfg.settle:
                    self.failed_seeks += 1
                    self._disarm()
                return []
            cfg, cand = self.cfg, self.candidate
            if st >= self.tape_end() or now - self.rolled_from >= cfg.roll:
                self.state = ReplayState.RETURNING
                self.returning_from = now
                return self.home()      # speed 1 first, which also clears slow motion
            end_slow = cand.at + cfg.slow_after
            # Out of slow motion for the outcome. Fires once, and only if there is an
            # outcome to show at real speed (tape_end caps it at where live was).
            if self.slowed and not self.unslowed and st >= end_slow:
                self.unslowed = True
                return [ReplayMove("speed", speed=1, label="replay: the outcome")]
            # The ramp. Fires once, as the tape reaches the marked instant.
            if (cfg.slow_at_moment and not self.slowed
                    and cand.at - cfg.slow_lead <= st < end_slow):
                self.slowed = True
                return [ReplayMove("speed", speed=cfg.slow_speed, slow_motion=True,
                                   label="replay: slow motion")]
            return []
        if self.state == ReplayState.RETURNING and now - self.returning_from > \
                self.cfg.return_timeout:
            self.returning_from = now
            return self.home()          # the pictures never agreed; ask again
        return []
    def arrived(self, frame) -> bool:
        """Are we back where we started? True only once we actually left, so the frames
        still in flight between issuing a seek and the sim honouring it (which still
        read as live), cannot be mistaken for an instant return."""
        return (self.state == ReplayState.RETURNING and self.left
                and tape_time(frame) >= self.return_to - LIVE_MARGIN)

    def home(self) -> list[ReplayMove]:
        """Come back. Never `to_end` unless we KNOW we came from a live edge: on a
        saved tape that is the finish, not live (see camera/probe.home_commands).

        We know it two ways: IsReplayPlaying was false before we left (in the car), or
        the session clock ran on while the tape was back (`live_clock`: spectating, where
        IsReplayPlaying is true even at the live edge). Seeking to `return_to` in a live
        session would land the excursion's length behind live and stay there.

        Speed first in both cases, so a slow-motion replay does not crawl its way home."""
        if self.source_is_tape is False or self.live_clock:
            return [ReplayMove("speed", speed=1, label="replay: real time"),
                    ReplayMove("to_end", label="replay: back to live")]
        return [ReplayMove("speed", speed=1, label="replay: real time"),
                ReplayMove("seek", session_num=self.return_session,
                           session_time=self.return_to, label="replay: back where we were")]

    def finish(self, session_time: float) -> None:
        """Called on the first frame back home. Ends the excursion and starts the
        cooldown, so the next replay is paced from when this one ENDED."""
        self.last_replay_end = session_time
        self._disarm()

    def reset_connection(self) -> None:
        """A new bridge connection. If the socket dropped mid-excursion we are NOT
        still rolling, whatever this object thinks, and the sim may well have been
        left showing the past, which the next seek or a human has to fix. Pacing
        (cooldown, counters) deliberately survives: it is about the broadcast, not
        about the socket."""
        self._disarm()
        self._seen.clear()
        self._last_leader = None

    def _reseat_clock(self) -> None:
        """The session clock restarted (a new session of the weekend, or a scrub back).
        Every session-time stamp here now reads from a timeline this one cannot reach.

        The cooldown is the one that bit: `last_replay_end` was stamped at t=3303 of
        Round 1's practice and never touched again, so `t - last_replay_end < cooldown`
        held for every second of qualifying (0..1950) and of the race (0..3312). Not
        one replay could roll after practice, and the machine reported nothing wrong:
        four candidates armed in the race and aged out waiting for a lull that was
        there the whole time. Same class of bug as issue #41 in the director, whose
        `_reseat_clock` this mirrors."""
        self.last_replay_end = -1e9
        self._pit_at.clear()
        self._off_at.clear()

    def _disarm(self) -> None:
        self.state = ReplayState.IDLE
        self.candidate = None
        self.left = False
        self.live_clock = False
        self.slowed = False
        self.unslowed = False
